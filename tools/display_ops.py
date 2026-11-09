"""显示器亮度控制 — Windows 显示通道（DDC/CI + WMI 双通道）。

微软开放的两条正门，按显示器类型自动选择：
  1. DDC/CI（dxva2 物理显示器 API）：外接显示器通用标准，
     VCP code 0x10 = luminance，GetVCPFeatureAndVCPFeatureReply 读、SetVCPFeature 写
  2. WMI WmiMonitorBrightnessMethods（root/wmi）：笔记本内置面板，
     本机是台式机无此类，代码保留以兼容笔记本环境

注意：虚拟显示器（远程/采集卡如 OrayIddDriver、IddSampleDriver）不支持
DDC/CI，读写会失败——错误信息需明确区分「物理屏不支持」与「没有屏幕」。
纯 ctypes + PowerShell 实现，无第三方依赖。
"""

from __future__ import annotations

import ctypes
import logging
import subprocess
import time
from contextlib import contextmanager
from ctypes import POINTER, WINFUNCTYPE, byref, wintypes
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# DDC/CI VCP code（MCCS 标准）：亮度
_VCP_BRIGHTNESS = 0x10
# MCCS 保留 code，真实显示器必须拒绝（返回失败）。若"成功"说明应答层
# 被虚拟化软件/驱动劫持（如远程控制软件 hook 显示链路），读写值均不可信
_VCP_TRUST_PROBE = 0xFF

# dxva2 物理显示器 API 的描述缓冲长度（微软文档固定值）
_PHYSICAL_MONITOR_DESC_LEN = 128

# WMI 查询超时（秒）：PowerShell 冷启动一般 1-2s
_WMI_PS_TIMEOUT = 10

_PRIMARY_ALIASES = ("主屏", "主显示器", "primary", "main")


class _PHYSICAL_MONITOR(ctypes.Structure):
    _fields_ = [
        ("hPhysicalMonitor", wintypes.HANDLE),
        ("szPhysicalMonitorDescription", ctypes.c_wchar * _PHYSICAL_MONITOR_DESC_LEN),
    ]


@dataclass
class MonitorBrightness:
    """单个显示器亮度快照。"""

    index: int  # 枚举序号，从 1 开始
    desc: str  # 物理显示器描述（如 Generic PnP Monitor）
    level: int | None  # 当前亮度百分比；读取失败为 None
    max_level: int  # 亮度上限（DDC 返回的最大值）
    source: str  # "ddc" | "wmi" | "unknown"

    @property
    def display(self) -> str:
        return self.desc or f"显示器{self.index}"


def _get_dxva2():
    try:
        return ctypes.windll.dxva2
    except (AttributeError, OSError) as e:  # 非 Windows / 缺 dll
        raise OSError(f"无法加载 dxva2（DDC/CI 通道）：{e}") from e


@ctypes.WINFUNCTYPE(
    wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, POINTER(wintypes.RECT), wintypes.LPARAM
)
def _enum_proc(hmon, _hdc, _rect, _lparam):
    _ENUM_HMONS.append(hmon)
    return True


_ENUM_HMONS: list[int] = []


@contextmanager
def _physical_monitors():
    """枚举全部物理显示器句柄；退出时 DestroyPhysicalMonitor 释放。"""
    user32 = ctypes.windll.user32
    dxva2 = _get_dxva2()
    _ENUM_HMONS.clear()
    if not user32.EnumDisplayMonitors(None, None, _enum_proc, 0):
        raise OSError(f"EnumDisplayMonitors 失败 err={ctypes.GetLastError()}")
    items: list[tuple[int, str]] = []
    try:
        for hmon in _ENUM_HMONS:
            count = wintypes.DWORD(0)
            if not dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR(hmon, byref(count)):
                logger.debug("GetNumberOfPhysicalMonitorsFromHMONITOR 失败 hmon=%#x", hmon)
                continue
            if count.value <= 0:
                continue
            arr = (_PHYSICAL_MONITOR * count.value)()
            if not dxva2.GetPhysicalMonitorsFromHMONITOR(hmon, count.value, arr):
                logger.debug("GetPhysicalMonitorsFromHMONITOR 失败 hmon=%#x", hmon)
                continue
            for i in range(count.value):
                pm = arr[i]
                # 部分虚拟化/非标准驱动返回 NULL 句柄但 VCP 透传仍可用，
                # 统一归一为 int（None→0），真实性由 _ddc_channel_trusted 判定
                raw_handle = pm.hPhysicalMonitor
                handle = int(raw_handle) if raw_handle is not None else 0
                items.append((handle, str(pm.szPhysicalMonitorDescription)))
        yield items
    finally:
        for handle, _ in items:
            try:
                dxva2.DestroyPhysicalMonitor(wintypes.HANDLE(handle))
            except Exception:  # noqa: BLE001 — 释放尽力而为
                pass
        _ENUM_HMONS.clear()


def _ddc_read(handle: int) -> tuple[int, int] | None:
    """读 VCP 0x10 当前值/最大值；不支持 DDC 返回 None。"""
    dxva2 = _get_dxva2()
    cur = wintypes.DWORD(0)
    maxv = wintypes.DWORD(0)
    if not dxva2.GetVCPFeatureAndVCPFeatureReply(
        wintypes.HANDLE(handle), wintypes.BYTE(_VCP_BRIGHTNESS), None, byref(cur), byref(maxv)
    ):
        return None
    if maxv.value <= 0:
        return None
    return int(cur.value), int(maxv.value)


def _ddc_channel_trusted(handle: int) -> bool:
    """DDC 通道可用性判定（两道）：

    1. GetMonitorCapabilities（官方正门）：驱动未声明 MC_CAPS_* 能力
       → 显示器配置通道未建立（常见于 OSD 关闭 DDC/CI、驱动未启用、
       虚拟显示器驱动残留干扰）
    2. MCCS 保留 code 0xFF 探测：真实显示器/驱动必须拒绝；若"成功"
       说明应答层在伪造（stub 行为），一切读写值都不可信
    """
    dxva2 = _get_dxva2()
    caps = wintypes.DWORD(0)
    temperatures = wintypes.DWORD(0)
    if not dxva2.GetMonitorCapabilities(
        wintypes.HANDLE(handle), byref(caps), byref(temperatures)
    ):
        return False  # 驱动未声明显示器配置能力 → 通道未建立
    cur = wintypes.DWORD(0)
    maxv = wintypes.DWORD(0)
    return not dxva2.GetVCPFeatureAndVCPFeatureReply(
        wintypes.HANDLE(handle), wintypes.BYTE(_VCP_TRUST_PROBE), None, byref(cur), byref(maxv)
    )


# 通道未建立时的排查清单（按可能性排序，避免点名无关软件）
_CHANNEL_UNAVAILABLE_HINT = (
    "显示器未向系统建立亮度控制通道（DDC/CI）：驱动报告无此能力，"
    "读写均为无效应答。排查建议：①显示器 OSD 菜单里打开 DDC/CI 开关；"
    "②设备管理器禁用残留的虚拟显示器驱动（如 OrayIddDriver 等远控/采集卡驱动）后重试；"
    "③重装/更新显卡驱动。笔记本内置屏不受此限制（走 WMI 通道）"
)


def _ddc_write(handle: int, value: int) -> bool:
    """写 VCP 0x10（原始值，非百分比）。"""
    dxva2 = _get_dxva2()
    return bool(
        dxva2.SetVCPFeature(
            wintypes.HANDLE(handle), wintypes.BYTE(_VCP_BRIGHTNESS), wintypes.DWORD(value)
        )
    )


def _ps_run(script: str, timeout: float = _WMI_PS_TIMEOUT) -> str:
    """执行 PowerShell 片段，返回 stdout（失败抛 OSError/RuntimeError）。"""
    cmd = [
        "powershell",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        script,
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"WMI 通道超时（{timeout}s）") from e
    if proc.returncode != 0:
        raise RuntimeError(f"PowerShell 退出码 {proc.returncode}: {(proc.stderr or '').strip()[:200]}")
    return (proc.stdout or "").strip()


def _wmi_available() -> bool:
    """笔记本内置面板亮度 WMI 类是否存在。"""
    try:
        out = _ps_run(
            "if (Get-CimInstance -Namespace root/wmi -ClassName WmiMonitorBrightnessMethods "
            "-ErrorAction SilentlyContinue) { 'yes' } else { 'no' }"
        )
        return out.lower() == "yes"
    except (RuntimeError, OSError) as e:
        logger.debug("WMI 亮度探测失败: %s", e)
        return False


def _wmi_read() -> int | None:
    """读内置面板亮度（0-100）；失败返回 None。"""
    try:
        out = _ps_run(
            "(Get-CimInstance -Namespace root/wmi -ClassName WmiMonitorBrightness "
            "| Select-Object -First 1).CurrentBrightness"
        )
        return int(out)
    except (RuntimeError, OSError, ValueError) as e:
        logger.debug("WMI 读亮度失败: %s", e)
        return None


def _wmi_write(level: int) -> bool:
    """设置内置面板亮度（0-100）。"""
    lvl = max(0, min(100, int(level)))
    try:
        out = _ps_run(
            "(Get-CimInstance -Namespace root/wmi -ClassName WmiMonitorBrightnessMethods "
            f"| Select-Object -First 1).WmiSetBrightness(1, {lvl}); 'ok'"
        )
        return out.lower() == "ok"
    except (RuntimeError, OSError) as e:
        logger.debug("WMI 写亮度失败: %s", e)
        return False


def _snap_ddc(index: int, handle: int, desc: str) -> MonitorBrightness:
    raw = _ddc_read(handle)
    if raw is None:
        return MonitorBrightness(index=index, desc=desc, level=None, max_level=0, source="unknown")
    cur, maxv = raw
    return MonitorBrightness(
        index=index,
        desc=desc,
        level=int(round(cur * 100 / maxv)),
        max_level=maxv,
        source="ddc",
    )


def list_monitors() -> tuple[bool, str]:
    """列出所有显示器及其亮度支持情况。"""
    try:
        with _physical_monitors() as items:
            if not items:
                return False, "没有检测到任何显示器"
            if not _ddc_channel_trusted(items[0][0]):
                return False, _CHANNEL_UNAVAILABLE_HINT
            snaps = [_snap_ddc(i + 1, h, d) for i, (h, d) in enumerate(items)]
    except OSError as e:
        # DDC 通道整体不可用（非标准环境），尝试 WMI（笔记本）
        if _wmi_available():
            lvl = _wmi_read()
            text = "未定义" if lvl is None else f"{lvl}%"
            return True, f"内置面板（WMI 通道）：亮度 {text}"
        return False, f"读取显示器信息失败：{e}"

    lines = [f"共 {len(snaps)} 个显示器："]
    for s in snaps:
        if s.level is None:
            lines.append(f"- {s.display}：不支持亮度调节（虚拟屏或 OSD 锁定）")
        else:
            lines.append(f"- {s.display}：亮度 {s.level}%（DDC/CI 0-{s.max_level}）")
    if _wmi_available():
        lvl = _wmi_read()
        lines.append(f"- 内置面板（WMI）：亮度 {lvl if lvl is not None else '未定义'}%")
    return True, "\n".join(lines)


def _pick_monitor(monitor: str | None) -> tuple[bool, str]:
    """校验 monitor 参数：空/主屏/序号/描述子串。返回 (ok, 消息)。"""
    key = str(monitor or "").strip().lower()
    if not key:
        return True, ""
    try:
        with _physical_monitors() as items:
            descs = [d.lower() for _, d in items]
    except OSError:
        return True, ""  # 枚举失败时交由后续流程报错
    if key in _PRIMARY_ALIASES or key == "1":
        return True, ""
    if key.isdigit():
        idx = int(key)
        if 1 <= idx <= len(descs):
            return True, ""
        return False, f"没有第 {idx} 个显示器（当前共 {len(descs)} 个）"
    if any(key in d for d in descs):
        return True, ""
    ok, listing = list_monitors()
    hint = listing if ok else "可用 list_monitors 查看"
    return False, f"没找到与「{monitor}」匹配的显示器。当前有：\n{hint}"


def get_brightness(monitor: str | None = None) -> tuple[bool, str]:
    """读取显示器亮度（百分比）。"""
    ok, msg = _pick_monitor(monitor)
    if not ok:
        return False, msg

    try:
        with _physical_monitors() as items:
            if not items:
                return False, "没有检测到任何显示器"
            if not _ddc_channel_trusted(items[0][0]):
                return False, _CHANNEL_UNAVAILABLE_HINT
            want = str(monitor or "").strip().lower()
            results: list[MonitorBrightness] = []
            for i, (h, d) in enumerate(items):
                if want and want not in _PRIMARY_ALIASES and want != "1":
                    if want not in d.lower():
                        continue
                results.append(_snap_ddc(i + 1, h, d))
    except OSError:
        results = []

    results = [r for r in results if r.level is not None]
    if results:
        if len(results) == 1:
            s = results[0]
            return True, f"显示器 {s.display} 当前亮度 {s.level}%"
        return True, "；".join(f"{s.display} {s.level}%" for s in results)

    # DDC 全失败 → 内置面板 WMI
    if _wmi_available():
        lvl = _wmi_read()
        if lvl is not None:
            return True, f"内置面板当前亮度 {lvl}%"
        return False, "WMI 亮度类存在但读取失败，可能在驱动更新后需要重启"
    return False, "所有显示器都不支持亮度读取（虚拟屏/OSD 锁定/线缆不支持 DDC/CI）"


def set_brightness(level: int, monitor: str | None = None) -> tuple[bool, str]:
    """设置显示器亮度（0-100）。多屏时 monitor 可选，缺省全部可调屏。"""
    ok, msg = _pick_monitor(monitor)
    if not ok:
        return False, msg
    try:
        lvl = max(0, min(100, int(level)))
    except (TypeError, ValueError):
        return False, "亮度必须是 0 到 100 的数字"

    want = str(monitor or "").strip().lower()
    changed: list[str] = []
    failed: list[str] = []

    try:
        with _physical_monitors() as items:
            if not items:
                return False, "没有检测到任何显示器"
            if not _ddc_channel_trusted(items[0][0]):
                return False, _CHANNEL_UNAVAILABLE_HINT
            for i, (h, d) in enumerate(items):
                if want and want not in _PRIMARY_ALIASES and want != "1":
                    if want not in d.lower():
                        continue
                snap = _snap_ddc(i + 1, h, d)
                if snap.level is None or snap.max_level <= 0:
                    failed.append(snap.display)
                    continue
                raw = int(round(lvl * snap.max_level / 100))
                if not _ddc_write(h, raw):
                    failed.append(snap.display)
                    continue
                # 写后读回验证：DDC 应用有延迟，短轮询最多 ~1.5s
                verified = False
                for _ in range(6):
                    time.sleep(0.25)
                    rb = _ddc_read(h)
                    if rb and rb[1] > 0 and abs(int(round(rb[0] * 100 / rb[1])) - lvl) <= 2:
                        verified = True
                        break
                if verified:
                    changed.append(f"{snap.display}（原 {snap.level}%）")
                else:
                    failed.append(f"{snap.display}（写入未被显示器接受，OSD 可能锁定了 DDC/CI）")
    except OSError as e:
        return False, f"设置亮度失败：{e}"

    if changed:
        msg = f"已把 {'、'.join(changed)} 的亮度设为 {lvl}%"
        if failed:
            msg += f"；{('、'.join(failed))} 不支持调节（虚拟屏或 OSD 锁定）"
        return True, msg

    # DDC 全失败 → 内置面板 WMI
    if _wmi_available():
        if _wmi_write(lvl):
            return True, f"已把内置面板亮度设为 {lvl}%"
        return False, f"WMI 设置内置面板亮度失败（目标 {lvl}%）"

    detail = f"失败设备：{'、'.join(failed)}" if failed else "没有匹配的显示器"
    return False, f"亮度设置未生效（{detail}）。虚拟显示器不支持 DDC/CI，物理屏请检查 OSD 菜单是否锁定 DDC"


# ---------------------------------------------------------------------------
# 壁纸（个性化）— 微软正门 SystemParametersInfo（user32），样式存注册表
#   SPI_SETDESKWALLPAPER：设置全局壁纸（系统自动转码到 TranscodedWallpaper）
#   SPI_GETDESKWALLPAPER：读取当前壁纸路径
#   WallpaperStyle 注册表值：0=居中 2=拉伸 6=适应 10=填充(默认) 22=跨屏
# 支持格式：bmp/jpg/jpeg/png（webp 系统不支持，明确提示）
# ---------------------------------------------------------------------------

_SPI_SETDESKWALLPAPER = 0x0014
_SPI_GETDESKWALLPAPER = 0x0073
_SPIF_UPDATEINIFILE = 0x01
_SPIF_SENDCHANGE = 0x02

_WALLPAPER_MAX_PATH = 260
_WALLPAPER_IMAGE_EXTS = (".bmp", ".jpg", ".jpeg", ".png", ".dib")

# 样式别名 → 注册表 WallpaperStyle 值
_WALLPAPER_STYLES = {
    "填充": "10",
    "适应": "6",
    "拉伸": "2",
    "居中": "0",
    "跨屏": "22",
    "fill": "10",
    "fit": "6",
    "stretch": "2",
    "center": "0",
    "span": "22",
}

_WALLPAPER_STYLE_NAMES = {v: k for k, v in _WALLPAPER_STYLES.items() if len(k) == 2}
_DESKTOP_STYLE_REGKEY = (r"Control Panel\Desktop", "WallpaperStyle")


def _user32_wallpaper(spi: int, buf_or_path, ui_param: int = 0) -> bool:
    """SystemParametersInfoW 调壁纸参数。

    buf_or_path：GET 时为接收缓冲，SET 时为 c_wchar_p 路径；
    ui_param：GET 时必须传缓冲字符数，SET 传 0。
    """
    user32 = ctypes.windll.user32
    return bool(
        user32.SystemParametersInfoW(
            wintypes.UINT(spi),
            wintypes.UINT(ui_param),
            buf_or_path,
            wintypes.UINT(_SPIF_UPDATEINIFILE | _SPIF_SENDCHANGE),
        )
    )


def _read_wallpaper_style() -> str:
    """读当前壁纸样式（注册表值 → 中文名，未知值原样返回）。"""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _DESKTOP_STYLE_REGKEY[0]) as key:
            value, _ = winreg.QueryValueEx(key, _DESKTOP_STYLE_REGKEY[1])
        value = str(value)
        return _WALLPAPER_STYLE_NAMES.get(value, value)
    except OSError:
        return "默认"


def get_wallpaper() -> tuple[bool, str]:
    """读取当前壁纸路径与样式。"""
    user32 = ctypes.windll.user32
    buf = ctypes.create_unicode_buffer(_WALLPAPER_MAX_PATH)
    if not _user32_wallpaper(_SPI_GETDESKWALLPAPER, buf, _WALLPAPER_MAX_PATH):
        return False, f"读取壁纸失败 err={ctypes.GetLastError()}"
    path = buf.value or "（未设置）"
    if path == "（未设置）":
        return True, f"当前没有设置壁纸，显示样式：{_read_wallpaper_style()}"
    return True, f"当前壁纸：{path}（样式：{_read_wallpaper_style()}）"


def set_wallpaper(image_path: str | None = None, style: str | None = None) -> tuple[bool, str]:
    """设置桌面壁纸。style 可选：填充/适应/拉伸/居中/跨屏。"""
    if not image_path or not str(image_path).strip():
        return False, "需要提供图片路径。可以对我说『把 xxx 图片设为壁纸』，图片支持 bmp/jpg/png"
    path = str(image_path).strip().strip('"').strip("'")
    if path.startswith("~"):
        import os

        path = os.path.expanduser(path)
    p = Path(path)
    if not p.is_absolute():
        p = (Path.cwd() / p).resolve()
    if not p.exists():
        return False, f"图片不存在：{p}。如果是网络图片请先下载到本地"
    if p.suffix.lower() not in _WALLPAPER_IMAGE_EXTS:
        return False, (
            f"不支持的图片格式 {p.suffix or '（无扩展名）'}，"
            f"系统壁纸支持 {'/'.join(_WALLPAPER_IMAGE_EXTS)}（webp 请先转成 jpg/png）"
        )
    full = str(p)

    style_name = ""
    if style:
        key = str(style).strip().lower()
        reg_val = _WALLPAPER_STYLES.get(key)
        if reg_val is None:
            return False, (
                f"未知样式「{style}」，可选：{'、'.join(k for k in _WALLPAPER_STYLES if len(k) == 2)}"
            )
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, _DESKTOP_STYLE_REGKEY[0], 0, winreg.KEY_SET_VALUE
            ) as keyreg:
                winreg.SetValueEx(keyreg, _DESKTOP_STYLE_REGKEY[1], 0, winreg.REG_SZ, reg_val)
            style_name = _WALLPAPER_STYLE_NAMES.get(reg_val, reg_val)
        except OSError as e:
            return False, f"写入壁纸样式失败：{e}"

    if not _user32_wallpaper(_SPI_SETDESKWALLPAPER, ctypes.c_wchar_p(full)):
        return False, f"设置壁纸失败 err={ctypes.GetLastError()}（图片可能被占用或损坏）"

    msg = f"已把桌面壁纸设为 {full}"
    if style_name:
        msg += f"（样式：{style_name}）"
    return True, msg
