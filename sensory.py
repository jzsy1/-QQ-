"""SensorySystem — 操作系统级实时感官系统。

================================================================
与静态快照的本质区别
================================================================
静态 WorldState：定时拍一张"当前是什么"的照片，被动记录。
感官系统：       持续感知"发生了什么变化"，主动产生事件流。

  ┌─────────────────────────────────────────────────────┐
  │  Windows 事件钩子 (SetWinEventHook)                 │  ← 真正实时
  │    窗口焦点切换 / 创建 / 销毁 / 标题变化             │
  ├─────────────────────────────────────────────────────┤
  │  变化检测源 (轮询 + diff + 去抖)                     │  ← 高频感知
  │    进程启停 / 剪贴板 / 电源 / 网络 / 文件 / 鼠标空闲 │
  ├─────────────────────────────────────────────────────┤
  │  外部事件 hook (由 AgentRunner 注入)                 │  ← 任务语义
  │    用户发言 / 工具开始 / 工具结束 / 轮次结束         │
  └─────────────────────────────────────────────────────┘
                       │ 原始信号
                       ▼
              ┌─────────────────┐
              │  语义化 + 评分   │  原始信号 → SenseEvent
              │  显著性过滤      │  噪音丢弃
              └─────────────────┘
                       │
                       ▼
              ┌─────────────────┐
              │  SenseEventBus  │  事件流（时间序列）
              │  + 订阅者通知    │  外部模块可订阅
              └─────────────────┘
                       │
                       ▼
              ┌─────────────────┐
              │  动态叙事生成    │  "最近 30 秒发生了什么"
              │  状态重建        │  而非 "当前是什么"
              └─────────────────┘

LLM 看到的是叙事："12 秒前用户切到 Chrome 打开了 github.com，
8 秒前复制了一段代码，3 秒前回到 VSCode" —— 这才是"不需要重新看世界"。
================================================================
"""

from __future__ import annotations

import ctypes
import logging
import os
import platform
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Windows API 绑定（非 Windows 平台静默降级为纯轮询模式）
# ----------------------------------------------------------------------

_IS_WINDOWS = platform.system() == "Windows"
_user32 = None
_kernel32 = None

if _IS_WINDOWS:
    try:
        _user32 = ctypes.windll.user32
        _kernel32 = ctypes.windll.kernel32

        class _MSG(ctypes.Structure):
            _fields_ = [
                ("hwnd", ctypes.c_void_p),
                ("message", ctypes.c_uint),
                ("wParam", ctypes.c_void_p),
                ("lParam", ctypes.c_long),
                ("time", ctypes.c_uint),
                ("pt", ctypes.c_long * 2),
            ]

        # WinEvent 回调签名
        _WINEVENTPROC = ctypes.WINFUNCTYPE(
            None,
            ctypes.c_void_p,  # hWinEventHook
            ctypes.c_uint,    # event
            ctypes.c_void_p,  # hwnd
            ctypes.c_long,    # idObject
            ctypes.c_long,    # idChild
            ctypes.c_uint,    # dwEventThread
            ctypes.c_uint,    # dwmsEventTime
        )

        # 事件常量
        _EVENT_SYSTEM_FOREGROUND = 0x0003
        _EVENT_OBJECT_CREATE = 0x8000
        _EVENT_OBJECT_DESTROY = 0x8001
        _EVENT_OBJECT_NAMECHANGE = 0x800C
        _WINEVENT_OUTOFCONTEXT = 0x0000
        _OBJID_WINDOW = 0

        _PM_REMOVE = 0x0001

        class _SYSTEM_POWER_STATUS(ctypes.Structure):
            _fields_ = [
                ("ACLineStatus", ctypes.c_ubyte),
                ("BatteryFlag", ctypes.c_ubyte),
                ("BatteryLifePercent", ctypes.c_ubyte),
                ("Reserved1", ctypes.c_ubyte),
                ("BatteryLifeTime", ctypes.c_ulong),
                ("BatteryFullLifeTime", ctypes.c_ulong),
            ]
    except Exception:  # noqa: BLE001
        logger.debug("[Sensory] Windows API 绑定失败，降级为纯轮询", exc_info=True)


# ----------------------------------------------------------------------
# 默认配置（零硬编码：全部可被 config.yaml 的 sensory: 块覆盖）
# ----------------------------------------------------------------------

_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "event_stream_size": 200,        # 事件流保留长度
    "narrative_max_chars": 500,      # 叙事文本上限
    "narrative_default_events": 8,   # 默认叙事包含几个事件
    "idle_threshold_sec": 30,        # 鼠标空闲多久算"空闲"事件
    "sources": {
        "window_hook": True,         # SetWinEventHook（实时）
        "processes": True,           # 进程启停
        "clipboard": True,           # 剪贴板变化
        "power": True,               # 电源状态
        "network": True,             # 网络变化
        "filesystem": True,          # 桌面/下载文件变化
        "mouse_idle": True,          # 鼠标空闲检测
    },
    "intervals_sec": {
        "processes": 3.0,
        "clipboard": 1.0,
        "power": 10.0,
        "network": 10.0,
        "filesystem": 5.0,
        "mouse_idle": 2.0,
    },
    "filesystem_paths": None,        # 默认桌面+下载，None=自动探测
    "filesystem_max_names": 12,
    "noise_processes": (            # 这些进程启停不算事件（系统噪音）
        "conhost", "dllhost", "svchost", "csrss", "lsass",
        "fontdrvhost", "dwm", "RuntimeBroker", "SearchHost",
        "StartMenuExperienceHost", "ShellExperienceHost", "sihost",
        "taskhostw", "ctfmon", " TextInputHost", "explorer",
    ),
}

# 触发叙事注入的意图关键词
_NARRATIVE_INTENT_KEYWORDS = (
    "刚才", "最近", "刚刚", "发生了", "现在", "当前", "状态", "怎么样",
    "什么程序", "打开了什么", "在干嘛", "在做什么", "切到", "切过去",
    "动了什么", "变了", "什么变化",
)


def _merge_cfg(user: dict | None) -> dict:
    cfg: dict[str, Any] = {
        k: (v.copy() if isinstance(v, dict) else list(v) if isinstance(v, (list, tuple)) else v)
        for k, v in _DEFAULTS.items()
    }
    if not isinstance(user, dict):
        return cfg
    for k, v in user.items():
        if k in ("sources", "intervals_sec") and isinstance(v, dict):
            cfg[k].update(v)
        elif k == "noise_processes" and isinstance(v, (list, tuple)):
            cfg[k] = tuple(str(x).lower() for x in v)
        else:
            cfg[k] = v
    return cfg


# ----------------------------------------------------------------------
# 事件模型
# ----------------------------------------------------------------------


@dataclass
class SenseEvent:
    """一次感官事件 —— 系统感知到的"发生了什么"。"""

    kind: str           # window_focus / window_create / window_destroy /
                        # window_rename / process_start / process_exit /
                        # clipboard_change / power_change / network_change /
                        # file_change / mouse_idle / mouse_active /
                        # user_turn / tool_start / tool_event / turn_end
    summary: str        # 人话："用户切到 Chrome"
    details: dict = field(default_factory=dict)
    at: float = field(default_factory=time.time)
    salience: float = 0.5   # 0~1，用于过滤噪音

    def age_sec(self, now: float | None = None) -> float:
        return (now or time.time()) - self.at


# ----------------------------------------------------------------------
# 事件总线
# ----------------------------------------------------------------------


class SenseEventBus:
    """线程安全的事件流 + 订阅通知。"""

    def __init__(self, max_events: int = 200):
        self._events: deque[SenseEvent] = deque(maxlen=max_events)
        self._subs: dict[str, list[Callable[[SenseEvent], None]]] = {}
        self._lock = threading.Lock()

    def publish(self, event: SenseEvent) -> None:
        try:  # KEKESI_PCE_P1_20261101
            from kekesi.memory.perception import record_sensory_event
            record_sensory_event(event)
        except Exception:
            pass
        with self._lock:
            self._events.append(event)
        # 通知订阅者（通配 "*" 收所有）
        callbacks: list[Callable] = []
        callbacks.extend(self._subs.get(event.kind, []))
        callbacks.extend(self._subs.get("*", []))
        for cb in callbacks:
            try:
                cb(event)
            except Exception:  # noqa: BLE001
                logger.debug("[Sensory] 订阅者回调异常", exc_info=True)

    def subscribe(self, kind: str, callback: Callable[[SenseEvent], None]) -> None:
        """订阅某类事件；kind='*' 收所有事件。"""
        self._subs.setdefault(kind, []).append(callback)

    def recent(
        self,
        n: int = 20,
        kinds: tuple[str, ...] | None = None,
        min_salience: float = 0.0,
        max_age_sec: float | None = None,
    ) -> list[SenseEvent]:
        """返回最近 N 个事件（可按类型/显著性/年龄过滤）。"""
        now = time.time()
        with self._lock:
            items = list(self._events)
        out: list[SenseEvent] = []
        for e in reversed(items):
            if kinds and e.kind not in kinds:
                continue
            if e.salience < min_salience:
                continue
            if max_age_sec is not None and e.age_sec(now) > max_age_sec:
                continue
            out.append(e)
            if len(out) >= n:
                break
        out.reverse()
        return out


# ----------------------------------------------------------------------
# 辅助：进程名查询（容错）
# ----------------------------------------------------------------------


def _pid_to_name(pid: int) -> str:
    if not pid or not _kernel32:
        return ""
    try:
        import psutil  # type: ignore

        return psutil.Process(int(pid)).name() or ""
    except Exception as e:  # noqa: BLE001
        logger.debug("[Sensory] psutil 获取进程名失败: %s", e)
    try:
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = _kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid)
        )
        if not handle:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = ctypes.c_ulong(1024)
            if _kernel32.QueryFullProcessImageNameW(
                handle, 0, buf, ctypes.byref(size)
            ):
                return os.path.basename(buf.value)
            return ""
        finally:
            _kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001
        return ""


def _window_title(hwnd: int) -> str:
    if not _user32 or not hwnd:
        return ""
    try:
        length = _user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(length + 1)
        _user32.GetWindowTextW(hwnd, buf, length + 1)
        return buf.value.strip()
    except Exception:  # noqa: BLE001
        return ""


def _is_window_visible(hwnd: int) -> bool:
    if not _user32 or not hwnd:
        return False
    try:
        return bool(_user32.IsWindowVisible(hwnd))
    except Exception:  # noqa: BLE001
        return False


def _window_pid(hwnd: int) -> int:
    if not _user32 or not hwnd:
        return 0
    try:
        pid = ctypes.c_ulong()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value)
    except Exception:  # noqa: BLE001
        return 0


# ----------------------------------------------------------------------
# 感官系统主类
# ----------------------------------------------------------------------


class SensorySystem:
    """实时感官系统：事件驱动 + 变化检测 → 事件流 → 叙事。

    用法（调用方全部容错）：
        sense = SensorySystem(cfg)
        sense.start()
        sense.on_user_turn_start(text, session_id)
        sense.on_tool_event(func, ok, summary)
        sense.format_for_llm(user_text)   # 按意图返回动态叙事
        sense.subscribe("window_focus", my_callback)
    """

    def __init__(
        self,
        cfg: dict | None = None,
        *,
        session_store=None,
        qs_store=None,
    ):
        self._cfg = _merge_cfg(cfg)
        self._enabled = bool(self._cfg.get("enabled", True))
        self._sources = self._cfg.get("sources", {})
        self._intervals = self._cfg.get("intervals_sec", {})
        self._noise = tuple(
            str(x).lower() for x in (self._cfg.get("noise_processes") or ())
        )
        self._session_store = session_store
        self._qs_store = qs_store

        self.bus = SenseEventBus(
            max_events=int(self._cfg.get("event_stream_size", 200))
        )

        # 当前状态视图（从事件流重建，供 snapshot 用）
        self._state: dict[str, Any] = {
            "user": None,
            "active_window": None,
            "open_windows": [],         # list[{"title","process","pid"}]
            "processes": [],
            "clipboard": None,
            "power": None,
            "network": None,
            "desktop_files": [],
            "mouse_position": None,
            "mouse_idle_sec": 0.0,
            "last_tool": None,
            "task_phase": "idle",
            "task_text": None,
        }
        self._state_lock = threading.Lock()

        # 后台线程
        self._stop_evt = threading.Event()
        self._threads: list[threading.Thread] = []
        self._raw_queue: "queue.Queue[tuple]" = queue.Queue(maxsize=2000)
        self._win_hook_handles: list[int] = []
        self._last_mouse_pos: tuple[int, int] | None = None
        self._last_mouse_move_ts: float = time.time()
        self._idle_announced: bool = False

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def start(self) -> None:
        if not self._enabled or self._threads:
            return

        # 消费线程（把原始信号 → SenseEvent）
        self._threads.append(threading.Thread(
            target=self._consumer_loop,
            name="sensory-consumer",
            daemon=True,
        ))

        # 窗口事件钩子（实时）
        if self._sources.get("window_hook", True) and _user32:
            self._threads.append(threading.Thread(
                target=self._window_hook_loop,
                name="sensory-window-hook",
                daemon=True,
            ))

        # 各轮询源（各自独立线程，独立周期）
        poll_specs: list[tuple[str, Callable, float]] = []
        if self._sources.get("processes", True):
            poll_specs.append((
                "sensory-processes",
                self._poll_processes,
                float(self._intervals.get("processes", 3.0)),
            ))
        if self._sources.get("clipboard", True):
            poll_specs.append((
                "sensory-clipboard",
                self._poll_clipboard,
                float(self._intervals.get("clipboard", 1.0)),
            ))
        if self._sources.get("power", True):
            poll_specs.append((
                "sensory-power",
                self._poll_power,
                float(self._intervals.get("power", 10.0)),
            ))
        if self._sources.get("network", True):
            poll_specs.append((
                "sensory-network",
                self._poll_network,
                float(self._intervals.get("network", 10.0)),
            ))
        if self._sources.get("filesystem", True):
            poll_specs.append((
                "sensory-fs",
                self._poll_filesystem,
                float(self._intervals.get("filesystem", 5.0)),
            ))
        if self._sources.get("mouse_idle", True):
            poll_specs.append((
                "sensory-mouse",
                self._poll_mouse_idle,
                float(self._intervals.get("mouse_idle", 2.0)),
            ))

        for name, fn, interval in poll_specs:
            self._threads.append(threading.Thread(
                target=self._poll_loop,
                args=(fn, interval),
                name=name,
                daemon=True,
            ))

        for t in self._threads:
            t.start()
        logger.info(
            "[Sensory] 启动 %d 个感知线程（window_hook=%s）",
            len(self._threads),
            bool(self._sources.get("window_hook", True) and _user32),
        )

    def stop(self) -> None:
        self._stop_evt.set()
        for t in self._threads:
            if t.is_alive():
                t.join(timeout=1.0)
        self._threads.clear()
        # 卸载 WinEvent hook
        if _user32:
            for h in self._win_hook_handles:
                try:
                    _user32.UnhookWinEvent(h)
                except Exception as e:  # noqa: BLE001
                    logger.debug("[Sensory] 取消 WinEvent hook 失败: %s", e)
        self._win_hook_handles.clear()

    # ------------------------------------------------------------------
    # 外部事件 hook（由 AgentRunner 调用）
    # ------------------------------------------------------------------

    def on_user_turn_start(self, text: str, session_id: str = "default") -> None:
        if not self._enabled:
            return
        try:
            with self._state_lock:
                self._state["user"] = session_id
                self._state["task_phase"] = "user_input"
                self._state["task_text"] = (text or "")[:120]
            self.bus.publish(SenseEvent(
                kind="user_turn",
                summary=f"用户发言：{(text or '')[:60]}",
                details={"text": (text or "")[:120], "session": session_id},
                salience=0.8,
            ))
        except Exception as e:  # noqa: BLE001
            logger.debug("[Sensory] 记录用户回合事件失败: %s", e)

    def on_tool_start(self, func: str) -> None:
        if not self._enabled:
            return
        try:
            with self._state_lock:
                self._state["task_phase"] = "executing"
                self._state["last_tool"] = {
                    "func": func, "phase": "executing", "at": time.time()
                }
            self.bus.publish(SenseEvent(
                kind="tool_start",
                summary=f"开始执行工具：{func}",
                details={"func": func},
                salience=0.6,
            ))
        except Exception:  # noqa: BLE001
            pass

    def on_tool_event(self, func: str, ok: bool, summary: str) -> None:
        if not self._enabled:
            return
        try:
            with self._state_lock:
                self._state["last_tool"] = {
                    "func": func, "ok": ok,
                    "summary": (summary or "")[:120], "at": time.time()
                }
                self._state["task_phase"] = "tool_done"
            self.bus.publish(SenseEvent(
                kind="tool_event",
                summary=f"工具 {func} {'成功' if ok else '失败'}：{(summary or '')[:60]}",
                details={"func": func, "ok": ok, "summary": (summary or "")[:120]},
                salience=0.7 if not ok else 0.5,
            ))
        except Exception:  # noqa: BLE001
            pass

    def on_user_turn_end(self, reply: str | None, tool_results: list) -> None:
        if not self._enabled:
            return
        try:
            with self._state_lock:
                self._state["task_phase"] = "idle"
            self.bus.publish(SenseEvent(
                kind="turn_end",
                summary=f"轮次结束，回复 {(reply or '')[:40]}，工具 {len(tool_results or [])} 个",
                details={
                    "reply": (reply or "")[:120],
                    "tool_count": len(tool_results or []),
                },
                salience=0.4,
            ))
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    # 窗口事件钩子（SetWinEventHook —— 真正实时）
    # ------------------------------------------------------------------

    def _window_hook_loop(self) -> None:
        """后台线程：注册 WinEvent 钩子 + 跑消息循环。"""
        if not _user32:
            return

        # 回调必须极简：只塞队列
        def _cb(hook, event, hwnd, id_obj, id_child, thread_id, ms_time):
            try:
                if id_obj != _OBJID_WINDOW or not hwnd:
                    return
                # 只关心可见窗口
                if event in (_EVENT_OBJECT_CREATE, _EVENT_OBJECT_NAMECHANGE):
                    if not _is_window_visible(hwnd):
                        return
                self._raw_queue.put_nowait(
                    ("win", event, int(hwnd), time.time())
                )
            except queue.Full:
                pass
            except Exception:  # noqa: BLE001
                pass

        proc = _WINEVENTPROC(_cb)  # 必须保活，防止 GC

        events_to_hook = (
            _EVENT_SYSTEM_FOREGROUND,
            _EVENT_OBJECT_CREATE,
            _EVENT_OBJECT_DESTROY,
            _EVENT_OBJECT_NAMECHANGE,
        )
        for ev in events_to_hook:
            try:
                h = _user32.SetWinEventHook(
                    ev, ev, 0, proc, 0, 0, _WINEVENT_OUTOFCONTEXT
                )
                if h:
                    self._win_hook_handles.append(int(h))
            except Exception:  # noqa: BLE001
                logger.debug("[Sensory] SetWinEventHook(%#x) 失败", ev, exc_info=True)

        if not self._win_hook_handles:
            return

        # 消息循环（PeekMessage + 心跳，便于及时响应 stop）
        msg = _MSG()
        logger.debug("[Sensory] 窗口钩子消息循环开始")
        while not self._stop_evt.is_set():
            try:
                while _user32.PeekMessageW(
                    ctypes.byref(msg), 0, 0, 0, _PM_REMOVE
                ):
                    _user32.TranslateMessage(ctypes.byref(msg))
                    _user32.DispatchMessageW(ctypes.byref(msg))
            except Exception:  # noqa: BLE001
                pass
            self._stop_evt.wait(0.05)
        logger.debug("[Sensory] 窗口钩子消息循环结束")

    # ------------------------------------------------------------------
    # 消费者：原始信号 → 语义化 SenseEvent
    # ------------------------------------------------------------------

    def _consumer_loop(self) -> None:
        while not self._stop_evt.is_set():
            try:
                item = self._raw_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._process_raw(item)
            except Exception:  # noqa: BLE001
                logger.debug("[Sensory] 原始信号处理异常", exc_info=True)

    def _process_raw(self, item: tuple) -> None:
        kind = item[0]
        if kind == "win":
            self._process_win_event(item[1], item[2], item[3])
        elif kind == "proc_diff":
            self._process_proc_diff(item[1], item[2])
        elif kind == "clipboard":
            self._process_clipboard_change(item[1])
        elif kind == "power":
            self._process_power_change(item[1])
        elif kind == "network":
            self._process_network_change(item[1])
        elif kind == "fs_diff":
            self._process_fs_diff(item[1], item[2])
        elif kind == "mouse":
            self._process_mouse(item[1], item[2])

    def _process_win_event(self, event: int, hwnd: int, at: float) -> None:
        title = _window_title(hwnd)
        pid = _window_pid(hwnd)
        proc = _pid_to_name(pid) if pid else ""

        if event == _EVENT_SYSTEM_FOREGROUND:
            # 前台窗口切换 —— 高显著性
            with self._state_lock:
                prev = self._state["active_window"]
                self._state["active_window"] = {
                    "title": title, "process": proc, "pid": pid,
                }
            # 同窗口标题相同 → 焦点没真正切换，丢弃
            if prev and prev.get("title") == title and prev.get("pid") == pid:
                return
            self.bus.publish(SenseEvent(
                kind="window_focus",
                summary=f"焦点切到：{title[:50] or proc or '未知窗口'}",
                details={"title": title, "process": proc, "pid": pid},
                at=at,
                salience=0.7,
            ))
        elif event == _EVENT_OBJECT_CREATE:
            if not title or len(title) < 2:
                return
            self.bus.publish(SenseEvent(
                kind="window_create",
                summary=f"窗口出现：{title[:50]}",
                details={"title": title, "process": proc, "pid": pid},
                at=at,
                salience=0.5,
            ))
        elif event == _EVENT_OBJECT_DESTROY:
            if not title:
                return
            self.bus.publish(SenseEvent(
                kind="window_destroy",
                summary=f"窗口关闭：{title[:50]}",
                details={"title": title, "process": proc, "pid": pid},
                at=at,
                salience=0.5,
            ))
        elif event == _EVENT_OBJECT_NAMECHANGE:
            if not title or len(title) < 2:
                return
            self.bus.publish(SenseEvent(
                kind="window_rename",
                summary=f"窗口标题变更：{title[:50]}",
                details={"title": title, "process": proc, "pid": pid},
                at=at,
                salience=0.4,
            ))

    def _process_proc_diff(self, started: list[str], exited: list[str]) -> None:
        for name in started:
            self.bus.publish(SenseEvent(
                kind="process_start",
                summary=f"进程启动：{name}",
                details={"name": name},
                salience=0.3,
            ))
        for name in exited:
            self.bus.publish(SenseEvent(
                kind="process_exit",
                summary=f"进程退出：{name}",
                details={"name": name},
                salience=0.3,
            ))

    def _process_clipboard_change(self, text: str) -> None:
        with self._state_lock:
            self._state["clipboard"] = text[:80] if text else ""
        self.bus.publish(SenseEvent(
            kind="clipboard_change",
            summary=f"剪贴板变化：{(text or '')[:40]}",
            details={"preview": (text or "")[:80]},
            salience=0.7,
        ))

    def _process_power_change(self, info: dict) -> None:
        with self._state_lock:
            self._state["power"] = info
        ac = info.get("on_ac")
        batt = info.get("battery_percent")
        if ac is not None:
            summary = "接通电源" if ac else "切换到电池"
            if batt is not None:
                summary += f"（{batt}%）"
        else:
            summary = f"电源状态变化（{batt}%）" if batt is not None else "电源状态变化"
        self.bus.publish(SenseEvent(
            kind="power_change",
            summary=summary,
            details=info,
            salience=0.7,
        ))

    def _process_network_change(self, info: dict) -> None:
        with self._state_lock:
            self._state["network"] = info
        bits = [f"{k}={v}" for k, v in info.items() if v not in (None, "", "unknown")]
        self.bus.publish(SenseEvent(
            kind="network_change",
            summary="网络变化：" + "，".join(bits[:4]) if bits else "网络变化",
            details=info,
            salience=0.6,
        ))

    def _process_fs_diff(self, added: list[str], removed: list[str]) -> None:
        for p in added[:6]:
            self.bus.publish(SenseEvent(
                kind="file_change",
                summary=f"新增文件：{os.path.basename(p)}",
                details={"path": p, "op": "added"},
                salience=0.6,
            ))
        for p in removed[:6]:
            self.bus.publish(SenseEvent(
                kind="file_change",
                summary=f"删除文件：{os.path.basename(p)}",
                details={"path": p, "op": "removed"},
                salience=0.6,
            ))

    def _process_mouse(self, pos: tuple[int, int], idle_sec: float) -> None:
        with self._state_lock:
            self._state["mouse_position"] = {"x": pos[0], "y": pos[1]}
            self._state["mouse_idle_sec"] = idle_sec
        threshold = float(self._cfg.get("idle_threshold_sec", 30))
        if idle_sec >= threshold and not self._idle_announced:
            self._idle_announced = True
            self.bus.publish(SenseEvent(
                kind="mouse_idle",
                summary=f"鼠标空闲 {int(idle_sec)} 秒",
                details={"idle_sec": idle_sec, "pos": list(pos)},
                salience=0.5,
            ))
        elif idle_sec < threshold and self._idle_announced:
            self._idle_announced = False
            self.bus.publish(SenseEvent(
                kind="mouse_active",
                summary="用户重新活动",
                details={"pos": list(pos)},
                salience=0.4,
            ))

    # ------------------------------------------------------------------
    # 轮询源：各自独立线程，独立周期
    # ------------------------------------------------------------------

    def _poll_loop(self, fn: Callable, interval: float) -> None:
        while not self._stop_evt.is_set():
            try:
                fn()
            except Exception:  # noqa: BLE001
                logger.debug("[Sensory] 轮询源 %s 异常", fn.__name__, exc_info=True)
            self._stop_evt.wait(max(0.2, interval))

    def _poll_processes(self) -> None:
        """进程列表 diff：检测启停。"""
        names = self._snapshot_processes()
        if names is None:
            return
        curr = set(n.lower() for n in names)
        with self._state_lock:
            prev = set(self._state.get("_proc_set", []))
            self._state["_proc_set"] = list(curr)
            self._state["processes"] = names
        started = [n for n in names if n.lower() in (curr - prev)]
        exited = [n for n in (prev - curr)]
        # 过滤系统噪音
        started = [n for n in started if n.lower() not in self._noise]
        exited = [n for n in exited if n.lower() not in self._noise]
        if started or exited:
            self._raw_queue.put_nowait(("proc_diff", started, list(exited)))

    def _snapshot_processes(self) -> list[str] | None:
        try:
            import psutil  # type: ignore

            seen: list[str] = []
            seen_set: set[str] = set()
            for p in psutil.process_iter(["name"]):
                name = (p.info.get("name") or "").strip()
                if not name or name.lower() in seen_set:
                    continue
                seen_set.add(name.lower())
                seen.append(name)
            return seen
        except Exception:  # noqa: BLE001
            pass
        try:
            import subprocess

            out = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=3,
                encoding="utf-8", errors="ignore",
            ).stdout
            seen: list[str] = []
            seen_set: set[str] = set()
            for line in out.splitlines():
                parts = line.split('","')
                if not parts:
                    continue
                name = parts[0].strip('"').strip()
                if not name or name.lower() in seen_set:
                    continue
                seen_set.add(name.lower())
                seen.append(name)
            return seen
        except Exception:  # noqa: BLE001
            return None

    def _poll_clipboard(self) -> None:
        try:
            from .tools.winops import get_clipboard

            text = (get_clipboard() or "").strip()
            limit = 80
            if len(text) > limit:
                text = text[:limit] + "…"
        except Exception:  # noqa: BLE001
            return
        with self._state_lock:
            prev = self._state.get("clipboard")
        if text != prev:
            self._raw_queue.put_nowait(("clipboard", text))

    def _poll_power(self) -> None:
        if not _user32 or not _kernel32:
            return
        try:
            sps = _SYSTEM_POWER_STATUS()
            if not _kernel32.GetSystemPowerStatus(ctypes.byref(sps)):
                return
            info = {
                "on_ac": sps.ACLineStatus == 1,
                "battery_percent": None if sps.BatteryLifePercent == 255
                                   else int(sps.BatteryLifePercent),
            }
        except Exception:  # noqa: BLE001
            return
        with self._state_lock:
            prev = self._state.get("power")
        # 只在充放电切换 / 电量变化 ≥5% 时报事件
        if prev is None:
            self._raw_queue.put_nowait(("power", info))
            return
        if prev.get("on_ac") != info["on_ac"]:
            self._raw_queue.put_nowait(("power", info))
            return
        pb = prev.get("battery_percent")
        cb = info["battery_percent"]
        if pb is not None and cb is not None and abs(cb - pb) >= 5:
            self._raw_queue.put_nowait(("power", info))

    def _poll_network(self) -> None:
        try:
            from .tools.network_ops import get_network_info

            ok, raw = get_network_info()
            if not ok:
                return
            import json as _json

            try:
                data = _json.loads(raw)
            except Exception:  # noqa: BLE001
                data = {"raw": (raw or "")[:120]}
            info = {
                k: data.get(k)
                for k in ("online", "ip", "ssid", "interface", "dns")
                if k in data
            } or {"raw": (raw or "")[:120]}
        except Exception:  # noqa: BLE001
            return
        with self._state_lock:
            prev = self._state.get("network")
        if prev != info:
            self._raw_queue.put_nowait(("network", info))

    def _poll_filesystem(self) -> None:
        paths = self._fs_watch_paths()
        if not paths:
            return
        max_names = int(self._cfg.get("filesystem_max_names", 12))
        curr: set[str] = set()
        for d in paths:
            try:
                for name in os.listdir(d):
                    curr.add(os.path.join(d, name))
            except Exception:  # noqa: BLE001
                continue
        curr_list = sorted(curr)[:max_names * 2]
        with self._state_lock:
            prev_set = set(self._state.get("_fs_set", []))
            self._state["_fs_set"] = curr_list
            self._state["desktop_files"] = [
                os.path.basename(p) for p in curr_list[:max_names]
            ]
        added = [p for p in curr_list if p not in prev_set]
        removed = [p for p in prev_set if p not in curr]
        if added or removed:
            self._raw_queue.put_nowait(("fs_diff", added[:6], list(removed)[:6]))

    def _fs_watch_paths(self) -> list[str]:
        custom = self._cfg.get("filesystem_paths")
        if custom:
            return [str(p) for p in custom if os.path.isdir(p)]
        # 默认：桌面 + 下载
        paths: list[str] = []
        try:
            home = os.path.expanduser("~")
            for sub in ("Desktop", "Downloads"):
                p = os.path.join(home, sub)
                if os.path.isdir(p):
                    paths.append(p)
        except Exception:  # noqa: BLE001
            pass
        return paths

    def _poll_mouse_idle(self) -> None:
        if not _user32:
            return
        try:
            pt = ctypes.c_long * 2
            point = pt()
            if not _user32.GetCursorPos(ctypes.byref(point)):
                return
            pos = (int(point[0]), int(point[1]))
        except Exception:  # noqa: BLE001
            return
        now = time.time()
        if self._last_mouse_pos is None or pos != self._last_mouse_pos:
            self._last_mouse_pos = pos
            self._last_mouse_move_ts = now
            self._idle_announced = False
        idle = now - self._last_mouse_move_ts
        self._raw_queue.put_nowait(("mouse", pos, idle))

    # ------------------------------------------------------------------
    # 对外查询
    # ------------------------------------------------------------------

    def snapshot(self) -> dict:
        """当前状态视图（从事件流重建的实时快照）。"""
        import copy

        with self._state_lock:
            s = copy.deepcopy(self._state)
        # 清理内部字段
        s.pop("_proc_set", None)
        s.pop("_fs_set", None)
        return s

    def recent_events(self, n: int = 20, **kw) -> list[SenseEvent]:
        return self.bus.recent(n=n, **kw)

    def subscribe(self, kind: str, callback: Callable[[SenseEvent], None]) -> None:
        self.bus.subscribe(kind, callback)

    def format_narrative(self, n: int | None = None) -> str:
        """生成最近 N 个事件的动态叙事。

        输出形如：
          【最近动态】
          - 12秒前 焦点切到 Chrome
          - 8秒前 窗口标题变更：github.com - Google Chrome
          - 3秒前 剪贴板变化：import ctypes
        """
        if n is None:
            n = int(self._cfg.get("narrative_default_events", 8))
        events = self.bus.recent(n=n, min_salience=0.3)
        if not events:
            return ""
        now = time.time()
        lines: list[str] = ["【最近动态】"]
        for e in events:
            age = int(e.age_sec(now))
            if age < 60:
                age_str = f"{age}秒前"
            else:
                age_str = f"{age // 60}分{age % 60}秒前"
            lines.append(f"- {age_str} {e.summary}")
        body = "\n".join(lines)
        limit = int(self._cfg.get("narrative_max_chars", 500))
        if len(body) > limit:
            body = body[: limit - 1] + "…"
        return body

    def format_working_snapshot(self, window_sec: float | None = None) -> str:
        """第二操作者的「共享工作台」快照：无需意图关键词，每轮可注入。

        汇总最近 window_sec 秒内：当前前台窗口、最近切换过的窗口、
        新增文件完整路径、剪贴板摘要。让「刚才那个文件 / 现在这个人」可直接解析。
        """
        if not self._enabled:
            return ""
        win = float(window_sec or self._cfg.get("working_snapshot_sec", 180))
        max_chars = int(self._cfg.get("working_snapshot_max_chars", 360))
        now = time.time()

        def _age(e: SenseEvent) -> str:
            a = int(e.age_sec(now))
            return f"{a}秒前" if a < 60 else f"{a // 60}分钟前"

        lines: list[str] = []
        with self._state_lock:
            active = dict(self._state.get("active_window") or {})
            clip = str(self._state.get("clipboard") or "").strip()
        if active.get("title") or active.get("process"):
            lines.append(
                f"- 当前前台：{(active.get('title') or '')[:50]}"
                f"（{active.get('process') or '?'}）"
            )

        seen_titles: set[str] = {str(active.get("title") or "")}
        focus_bits: list[str] = []
        for e in self.bus.recent(n=30, kinds=("window_focus",), max_age_sec=win):
            title = str(e.details.get("title") or "").strip()
            if not title or title in seen_titles:
                continue
            seen_titles.add(title)
            focus_bits.append(f"{_age(e)} {title[:36]}")
            if len(focus_bits) >= 3:
                break
        if focus_bits:
            lines.append("- 最近切换过：" + "；".join(focus_bits))

        file_bits: list[str] = []
        for e in self.bus.recent(n=20, kinds=("file_change",), max_age_sec=win):
            if e.details.get("op") != "added":
                continue
            path = str(e.details.get("path") or "")
            if path and all(path not in b for b in file_bits):
                file_bits.append(f"{_age(e)} {path}")
            if len(file_bits) >= 3:
                break
        if file_bits:
            lines.append("- 新出现的文件：" + "；".join(file_bits))

        clip_evts = self.bus.recent(n=1, kinds=("clipboard_change",), max_age_sec=win)
        if clip and clip_evts:
            lines.append(f"- 刚复制的内容（{_age(clip_evts[0])}）：{clip[:60]}")

        if not lines:
            return ""
        body = (
            "【共享桌面态势·用户最近在电脑上做的事】"
            "用户说「刚才那个/这个文件/现在这个人/刚复制的」时优先据此解析，"
            "不要反问；与本轮无关则忽略。\n" + "\n".join(lines)
        )
        if len(body) > max_chars:
            body = body[: max_chars - 1] + "…"
        return body

    def format_for_llm(self, user_text: str = "") -> str:
        """按意图返回动态叙事；不需要时返回空串。

        纪律：默认不塞；仅当用户话含"最近/刚才/状态/发生了"等意图时附加。
        与 build_extra_context 的"按意图注入"理念一致。
        """
        if not self._enabled:
            return ""
        t = (user_text or "").strip()
        if not t:
            return ""
        if not any(k in t for k in _NARRATIVE_INTENT_KEYWORDS):
            return ""
        return self.format_narrative()


# ----------------------------------------------------------------------
# 模块级单例（可选）
# ----------------------------------------------------------------------

_global: SensorySystem | None = None


def get_global() -> SensorySystem | None:
    return _global


def set_global(sense: SensorySystem | None) -> None:
    global _global
    _global = sense
