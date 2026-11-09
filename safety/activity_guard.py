"""键鼠活跃检测 — 用户正在操作时避免抢控制权。

注意：助手自己的点击/打字也会刷新 GetLastInputInfo，
多步 GUI 时必须用 mark_agent_control 进入专属窗口，否则会自锁。
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
import time

logger = logging.getLogger(__name__)

_INPUT_BUSY_TOOLS = frozenset({
    "gui_action",
    "click_text",
    "click_at",
    "hover_text",
    "mouse_move",
    "mouse_click",
    "mouse_drag",
    "mouse_scroll",
    "type_text",
    "send_keys",
    "uia_click_element",
    "uia_set_value",
    "virtual_click",
    "virtual_scroll",
    "run_sequence",
    "click_image",
    "click_and_type",
    "focus_and_hotkey",
    "fill_form",
    "click_menu_item",
})

_lock = threading.Lock()
_agent_control_until = 0.0


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint),
        ("dwTime", ctypes.c_uint),
    ]


def mark_agent_control(duration_sec: float = 12.0) -> None:
    """本轮 Agent 正在控键鼠：后续步骤忽略自触发的「用户忙碌」。"""
    global _agent_control_until
    with _lock:
        _agent_control_until = max(
            _agent_control_until, time.time() + max(1.0, duration_sec)
        )


def clear_agent_control() -> None:
    global _agent_control_until
    with _lock:
        _agent_control_until = 0.0


def in_agent_control() -> bool:
    with _lock:
        return time.time() < _agent_control_until


def seconds_since_last_input() -> float | None:
    """距上次键鼠输入的秒数；非 Windows 或失败返回 None。"""
    if sys.platform != "win32":
        return None
    try:
        info = _LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(info)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        tick = ctypes.windll.kernel32.GetTickCount()
        idle_ms = int(tick) - int(info.dwTime)
        if idle_ms < 0:
            idle_ms = 0
        return idle_ms / 1000.0
    except Exception:
        logger.debug("GetLastInputInfo 失败", exc_info=True)
        return None


def check_user_idle(
    func: str,
    *,
    min_idle_sec: float = 1.2,
) -> tuple[bool, str]:
    """若工具会抢键鼠且用户刚输入，返回 (False, 原因)。"""
    name = (func or "").strip()
    if name not in _INPUT_BUSY_TOOLS:
        return True, ""

    if in_agent_control():
        return True, ""

    idle = seconds_since_last_input()
    if idle is None:
        return True, ""
    if idle < min_idle_sec:
        return (
            False,
            f"检测到您正在操作键鼠（空闲仅 {idle:.1f}s），暂不抢控制。"
            "请稍停一下再说「继续」或再试一次。",
        )
    return True, ""
