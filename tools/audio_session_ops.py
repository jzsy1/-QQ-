"""按应用音量控制 — Windows Core Audio（MMDeviceAPI + AudioSes）。

微软官方开放的音频会话接口：每个正在发声的应用是系统里一个「音频会话」，
可独立读写音量与静音，互不影响主音量：
  IMMDeviceEnumerator.GetDefaultAudioEndpoint（默认播放设备）
  → IAudioSessionManager2.GetSessionEnumerator（枚举会话）
  → IAudioSessionControl2.GetProcessId（定位所属进程）
  → ISimpleAudioVolume.SetMasterVolume / SetMute（独立音量/静音）

纯 ctypes 直连 COM，无第三方依赖（pycaw 同源接口，这里手写虚表）。
注意：会话随应用结束立即消失，属于易失运行时状态，不落库。
"""

from __future__ import annotations

import ctypes
import logging
import threading
import uuid
from contextlib import contextmanager
from ctypes import POINTER, WINFUNCTYPE, byref, c_int, c_void_p, wintypes
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_HRESULT = ctypes.HRESULT

# CLSID / IID（uuid.bytes_le 正好是 GUID 的内存布局）
_CLSID_MMDeviceEnumerator = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
_IID_IMMDeviceEnumerator = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"
_IID_IAudioSessionManager2 = "{77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F}"
_IID_IAudioSessionControl2 = "{BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D}"
_IID_ISimpleAudioVolume = "{87CE5498-68D6-44E5-9215-6DA47EF883D8}"

# COM 常量
_CLSCTX_ALL = 0x17  # INPROC_SERVER|INPROC_HANDLER|LOCAL_SERVER|REMOTE_SERVER
_E_RENDER = 0  # eRender（播放设备）
_E_MULTIMEDIA = 1  # eMultimedia
_COINIT_APARTMENTTHREADED = 0x2
_RPC_E_CHANGED_MODE = 0x80010106
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

# 音频会话状态
_STATE_ACTIVE = 1

# vtable 下标（0/1/2 = QueryInterface/AddRef/Release）
_IDX_IMMDEV_ENUM_GET_DEFAULT = 4
_IDX_IMMDEV_ACTIVATE = 3
_IDX_MGR2_GET_SESSION_ENUM = 5
_IDX_SESS_ENUM_GET_COUNT = 3
_IDX_SESS_ENUM_GET_SESSION = 4
_IDX_CTL_GET_STATE = 3
_IDX_CTL2_GET_PID = 14
_IDX_CTL2_IS_SYSTEM_SOUNDS = 15
_IDX_SAV_SET_VOLUME = 3
_IDX_SAV_GET_VOLUME = 4
_IDX_SAV_SET_MUTE = 5
_IDX_SAV_GET_MUTE = 6

_SYSTEM_SOUNDS_ALIASES = ("系统音效", "系统声音", "system", "sounds")


@dataclass
class AudioSession:
    """单个应用的音频会话快照。"""

    pid: int
    process: str  # 进程名（含 .exe，未知时为空）
    level: int  # 音量 0-100
    muted: bool
    active: bool
    system_sounds: bool

    @property
    def display(self) -> str:
        if self.system_sounds:
            return "系统音效"
        return self.process or f"pid_{self.pid}"


def _guid_buf(iid: str):
    """GUID 字符串 → 16 字节缓冲（bytes_le 即 COM 内存布局）。"""
    return (ctypes.c_ubyte * 16).from_buffer_copy(uuid.UUID(iid).bytes_le)


def _fn(unknown: int, index: int, restype, *argtypes):
    """取 COM 对象虚表中第 index 个方法；调用时第一个参数传接口指针。

    注意 argtypes 需包含 self（c_void_p）作为第一个参数类型。"""
    vtbl = ctypes.cast(c_void_p(unknown), POINTER(c_void_p)).contents.value
    if not vtbl:
        raise ValueError("COM 对象指针为空")
    # POINTER(c_void_p) 下标访问直接返回 Python int（ctypes 简单类型自动转换）
    addr = ctypes.cast(c_void_p(vtbl), POINTER(c_void_p))[index]
    if not addr:
        raise ValueError(f"虚表第 {index} 项为空")
    return WINFUNCTYPE(restype, *argtypes)(addr)


def _hr_ok(hr: int) -> bool:
    return hr is not None and hr >= 0


def _release_all(ptrs) -> None:
    """按逆序 Release 一批 COM 指针（尽力而为，不影响主流程）。"""
    for p in reversed(ptrs or []):
        if not p:
            continue
        try:
            _fn(p, 2, _HRESULT, c_void_p)(p)
        except Exception:
            pass


def _query_interface(unknown: int, iid: str, out) -> int:
    """IUnknown::QueryInterface（虚表下标 0）。"""
    return _fn(unknown, 0, _HRESULT, c_void_p, c_void_p, POINTER(c_void_p))(
        unknown, byref(_guid_buf(iid)), out
    )


def _co_init() -> None:
    """COM 初始化（线程已初始化为其他模式时忽略）。"""
    coinit = ctypes.WinDLL("ole32").CoInitializeEx
    coinit.restype = _HRESULT
    coinit.argtypes = [c_void_p, c_int]
    hr = coinit(None, _COINIT_APARTMENTTHREADED)
    if hr < 0 and hr != _RPC_E_CHANGED_MODE:
        raise OSError(f"CoInitializeEx 失败: 0x{hr & 0xFFFFFFFF:08X}")


@contextmanager
def _session_enumerator():
    """打开默认播放设备的音频会话枚举器；退出时释放全部 COM 引用。

    yields (sess_enum_ptr, session_count)
    """
    dev_enum = device = mgr2 = sess_enum = None
    try:
        _co_init()
        ole32 = ctypes.oledll.ole32  # CoCreateInstance 失败直接抛 OSError

        dev_enum = c_void_p()
        ole32.CoCreateInstance(
            byref(_guid_buf(_CLSID_MMDeviceEnumerator)),
            None,
            _CLSCTX_ALL,
            byref(_guid_buf(_IID_IMMDeviceEnumerator)),
            byref(dev_enum),
        )
        if not dev_enum.value:
            raise OSError("CoCreateInstance(MMDeviceEnumerator) 未返回对象")

        device = c_void_p()
        hr = _fn(
            dev_enum.value,
            _IDX_IMMDEV_ENUM_GET_DEFAULT,
            _HRESULT,
            c_void_p,
            c_int,
            c_int,
            POINTER(c_void_p),
        )(dev_enum, _E_RENDER, _E_MULTIMEDIA, byref(device))
        if not _hr_ok(hr) or not device.value:
            raise OSError(f"GetDefaultAudioEndpoint 失败: 0x{hr & 0xFFFFFFFF:08X}")

        mgr2 = c_void_p()
        hr = _fn(
            device.value,
            _IDX_IMMDEV_ACTIVATE,
            _HRESULT,
            c_void_p,
            c_void_p,
            c_int,
            c_void_p,
            POINTER(c_void_p),
        )(
            device,
            byref(_guid_buf(_IID_IAudioSessionManager2)),
            _CLSCTX_ALL,
            None,
            byref(mgr2),
        )
        if not _hr_ok(hr) or not mgr2.value:
            raise OSError(f"IAudioSessionManager2 获取失败: 0x{hr & 0xFFFFFFFF:08X}")

        sess_enum = c_void_p()
        hr = _fn(
            mgr2.value,
            _IDX_MGR2_GET_SESSION_ENUM,
            _HRESULT,
            c_void_p,
            POINTER(c_void_p),
        )(mgr2, byref(sess_enum))
        if not _hr_ok(hr) or not sess_enum.value:
            raise OSError(f"GetSessionEnumerator 失败: 0x{hr & 0xFFFFFFFF:08X}")

        count = c_int(0)
        hr = _fn(
            sess_enum.value, _IDX_SESS_ENUM_GET_COUNT, _HRESULT, c_void_p, POINTER(c_int)
        )(sess_enum, byref(count))
        if not _hr_ok(hr):
            raise OSError(f"GetCount 失败: 0x{hr & 0xFFFFFFFF:08X}")

        yield sess_enum.value, max(0, int(count.value))
    finally:
        _release_all(
            [
                sess_enum.value if sess_enum else None,
                mgr2.value if mgr2 else None,
                device.value if device else None,
                dev_enum.value if dev_enum else None,
            ]
        )


def _iter_sessions():
    """取当前全部会话的 IAudioSessionControl 指针（上下文管理器）。

    指针在本上下文退出时统一 Release，调用方只在 with 块内使用——
    本模块所有公开函数都满足该约束。"""
    ptrs: list[int] = []

    @contextmanager
    def _ctx():
        with _session_enumerator() as (enum, count):
            try:
                for i in range(count):
                    ctl = c_void_p()
                    hr = _fn(
                        enum,
                        _IDX_SESS_ENUM_GET_SESSION,
                        _HRESULT,
                        c_void_p,
                        c_int,
                        POINTER(c_void_p),
                    )(enum, i, byref(ctl))
                    if _hr_ok(hr) and ctl.value:
                        ptrs.append(ctl.value)
                yield ptrs
            finally:
                _release_all(ptrs)

    return _ctx()


def _process_image_name(pid: int) -> str:
    """进程 id → 镜像名（如 QQMusic.exe）；无权限/已退出返回空串。"""
    if not isinstance(pid, int) or pid <= 0:
        return ""
    try:
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            if k32.QueryFullProcessImageNameW(h, 0, buf, byref(size)):
                return Path(buf.value).name
            return ""
        finally:
            k32.CloseHandle(h)
    except Exception:
        return ""


def _read_session(ctl: int) -> AudioSession:
    """从 IAudioSessionControl 读取单个会话快照（含 QI 派生接口）。"""
    ctl2 = sav = None
    try:
        state = c_int(0)
        _fn(ctl, _IDX_CTL_GET_STATE, _HRESULT, c_void_p, POINTER(c_int))(
            ctl, byref(state)
        )

        pid = 0
        system_sounds = False
        ctl2 = c_void_p()
        hr2 = _query_interface(ctl, _IID_IAudioSessionControl2, byref(ctl2))
        if _hr_ok(hr2) and ctl2.value:
            pid_val = wintypes.DWORD(0)
            _fn(
                ctl2.value,
                _IDX_CTL2_GET_PID,
                _HRESULT,
                c_void_p,
                POINTER(wintypes.DWORD),
            )(ctl2, byref(pid_val))
            pid = int(pid_val.value or 0)
            # S_OK(0)=系统音效会话，S_FALSE(1)=普通应用
            system_sounds = (
                _fn(ctl2.value, _IDX_CTL2_IS_SYSTEM_SOUNDS, _HRESULT, c_void_p)(ctl2)
                == 0
            )

        level_pct = 100
        muted = False
        sav = c_void_p()
        hr3 = _query_interface(ctl, _IID_ISimpleAudioVolume, byref(sav))
        if _hr_ok(hr3) and sav.value:
            vol = ctypes.c_float(1.0)
            _fn(
                sav.value,
                _IDX_SAV_GET_VOLUME,
                _HRESULT,
                c_void_p,
                POINTER(ctypes.c_float),
            )(sav, byref(vol))
            level_pct = int(round(max(0.0, min(1.0, float(vol.value))) * 100))
            mute_val = wintypes.BOOL(0)
            _fn(
                sav.value,
                _IDX_SAV_GET_MUTE,
                _HRESULT,
                c_void_p,
                POINTER(wintypes.BOOL),
            )(sav, byref(mute_val))
            muted = bool(mute_val.value)

        return AudioSession(
            pid=pid,
            process=_process_image_name(pid),
            level=level_pct,
            muted=muted,
            active=int(state.value) == _STATE_ACTIVE,
            system_sounds=system_sounds,
        )
    finally:
        _release_all([sav.value if sav else None, ctl2.value if ctl2 else None])


def _match_key(app: str) -> str:
    key = str(app or "").strip().lower()
    if key.endswith(".exe"):
        key = key[:-4]
    return key


def _session_matches(s: AudioSession, key: str) -> bool:
    if not key:
        return False
    if key in _SYSTEM_SOUNDS_ALIASES and s.system_sounds:
        return True
    name = (s.process or "").lower()
    if name.endswith(".exe"):
        name = name[:-4]
    if not name:
        return False
    return key == name or key in name or name in key


def list_app_volumes(limit: int | None = None) -> tuple[bool, str]:
    """列出当前所有应用的独立音量（发声中的排前）。"""
    try:
        with _iter_sessions() as ptrs:
            sessions = [_read_session(p) for p in ptrs]
    except Exception as e:
        logger.warning("枚举音频会话失败: %s", e)
        return False, f"读取音频会话失败：{e}"

    if not sessions:
        return True, "当前没有任何应用的音频会话（可能都没有在发声）"

    sessions.sort(key=lambda s: (not s.active, not s.system_sounds, s.display))
    try:
        cap = max(1, int(limit)) if limit is not None else 20
    except (TypeError, ValueError):
        cap = 20
    shown = sessions[:cap]

    lines = [f"共 {len(sessions)} 个音频会话："]
    for s in shown:
        state = "发声中" if s.active else "空闲"
        vol = "已静音" if s.muted else f"{s.level}%"
        suffix = "（系统音效）" if s.system_sounds else ""
        lines.append(f"- {s.display}{suffix}：音量 {vol}，{state}（pid {s.pid}）")
    if len(sessions) > len(shown):
        lines.append(f"…其余 {len(sessions) - len(shown)} 个未展示")
    return True, "\n".join(lines)


def set_app_volume(app: str, level: int) -> tuple[bool, str]:
    """把指定应用的所有会话音量设为 level（0-100），不动其他应用。"""
    key = _match_key(app)
    if not key:
        return False, "请提供应用名（如 QQMusic、chrome）"
    try:
        lvl = max(0, min(100, int(level)))
    except (TypeError, ValueError):
        return False, "音量必须是 0 到 100 的数字"

    changed: list[str] = []
    matched: list[int] = []

    try:
        with _iter_sessions() as ptrs:
            for p in ptrs:
                snap = _read_session(p)
                if not _session_matches(snap, key):
                    continue
                matched.append(p)
                sav = c_void_p()
                hr = _query_interface(p, _IID_ISimpleAudioVolume, byref(sav))
                if not _hr_ok(hr) or not sav.value:
                    continue
                try:
                    hr = _fn(
                        sav.value,
                        _IDX_SAV_SET_VOLUME,
                        _HRESULT,
                        c_void_p,
                        ctypes.c_float,
                        c_void_p,
                    )(sav, ctypes.c_float(lvl / 100.0), None)
                    if _hr_ok(hr):
                        changed.append(f"{snap.display}（原 {snap.level}%）")
                finally:
                    _release_all([sav.value])
    except Exception as e:
        logger.warning("设置应用音量失败: %s", e)
        return False, f"设置失败：{e}"

    if not matched:
        ok, listing = list_app_volumes()
        hint = listing if ok else "可先说「列出应用音量」查看"
        return False, f"没找到与「{app}」匹配的音频会话。当前有：\n{hint}"
    return True, f"已把 {'、'.join(changed)} 的音量设为 {lvl}%"


def mute_app(app: str, muted: bool = True) -> tuple[bool, str]:
    """静音/取消静音指定应用，不影响其他应用。"""
    key = _match_key(app)
    if not key:
        return False, "请提供应用名（如 QQMusic、chrome）"
    muted = bool(muted)

    changed: list[str] = []
    matched: list[int] = []

    try:
        with _iter_sessions() as ptrs:
            for p in ptrs:
                snap = _read_session(p)
                if not _session_matches(snap, key):
                    continue
                matched.append(p)
                sav = c_void_p()
                hr = _query_interface(p, _IID_ISimpleAudioVolume, byref(sav))
                if not _hr_ok(hr) or not sav.value:
                    continue
                try:
                    hr = _fn(
                        sav.value,
                        _IDX_SAV_SET_MUTE,
                        _HRESULT,
                        c_void_p,
                        wintypes.BOOL,
                        c_void_p,
                    )(sav, wintypes.BOOL(1 if muted else 0), None)
                    if _hr_ok(hr):
                        changed.append(snap.display)
                finally:
                    _release_all([sav.value])
    except Exception as e:
        logger.warning("应用静音失败: %s", e)
        return False, f"设置失败：{e}"

    if not matched:
        ok, listing = list_app_volumes()
        hint = listing if ok else "可先说「列出应用音量」查看"
        return False, f"没找到与「{app}」匹配的音频会话。当前有：\n{hint}"
    verb = "已静音" if muted else "已取消静音"
    return True, f"{verb} {'、'.join(changed)}"


# ---- 语音闪避：临时压低内部网页播放器音量，不中断播放 ----

# pywebview（EdgeChromium/WebView2）的音频进程名
_INNER_PLAYER_APP = "msedgewebview2"
_duck_lock = threading.Lock()
_duck_originals: dict[int, int] = {}
_duck_fallback = 100


def _set_session_level(ctl: int, level_pct: int) -> bool:
    """给单个会话设置独立音量（0-100），成功返回 True。"""
    sav = c_void_p()
    hr = _query_interface(ctl, _IID_ISimpleAudioVolume, byref(sav))
    if not _hr_ok(hr) or not sav.value:
        return False
    try:
        hr = _fn(
            sav.value,
            _IDX_SAV_SET_VOLUME,
            _HRESULT,
            c_void_p,
            ctypes.c_float,
            c_void_p,
        )(sav, ctypes.c_float(level_pct / 100.0), None)
        return _hr_ok(hr)
    finally:
        _release_all([sav.value])


def duck_volume(level: int, *, app: str = _INNER_PLAYER_APP) -> tuple[bool, str]:
    """把指定应用音量降到 level%，并记住原值供 restore_volume 恢复。"""
    global _duck_fallback
    key = _match_key(app)
    if not key:
        return False, "请提供应用名"
    try:
        lvl = max(0, min(100, int(level)))
    except (TypeError, ValueError):
        return False, "音量必须是 0 到 100 的数字"

    changed: list[str] = []
    matched = 0
    names: list[str] = []
    try:
        with _duck_lock, _iter_sessions() as ptrs:
            for p in ptrs:
                snap = _read_session(p)
                names.append(snap.display)
                if not _session_matches(snap, key):
                    continue
                matched += 1
                if snap.pid not in _duck_originals:
                    _duck_originals[snap.pid] = snap.level
                    _duck_fallback = snap.level
                if _set_session_level(p, lvl):
                    changed.append(f"{snap.display}（原 {snap.level}%）")
    except Exception as e:
        logger.warning("语音闪避降音量失败: %s", e)
        return False, f"闪避失败：{e}"

    if not matched:
        avail = "、".join(sorted(set(names))) or "无"
        return False, f"未找到「{app}」的音频会话（当前会话：{avail}）"
    if not changed:
        return False, f"「{app}」音量设置失败"
    return True, f"已闪避 {'、'.join(changed)} 至 {lvl}%"


def restore_volume(*, app: str = _INNER_PLAYER_APP) -> tuple[bool, str]:
    """把 duck_volume 压低过的应用音量恢复到原值。"""
    key = _match_key(app)
    if not key:
        return False, "请提供应用名"

    with _duck_lock:
        originals = dict(_duck_originals)
        _duck_originals.clear()
    if not originals:
        return True, "无需恢复音量"

    changed: list[str] = []
    try:
        with _iter_sessions() as ptrs:
            for p in ptrs:
                snap = _read_session(p)
                if not _session_matches(snap, key):
                    continue
                target = originals.get(snap.pid, _duck_fallback)
                if _set_session_level(p, target):
                    changed.append(f"{snap.display} → {target}%")
    except Exception as e:
        logger.warning("恢复应用音量失败: %s", e)
        return False, f"恢复失败：{e}"

    if not changed:
        return True, "闪避期间会话已结束，无需恢复"
    return True, f"已恢复音量 {'、'.join(changed)}"
