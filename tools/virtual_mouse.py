# -*- coding: utf-8 -*-
"""虚拟鼠标 — 向目标窗投递鼠标消息，不移动物理光标。

策略：WindowFromPoint → 客户区坐标 → PostMessage(WM_*BUTTON*)。
部分应用（游戏/DirectInput/部分 Electron）可能忽略消息，此时应回退 SendInput。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import time

user32 = ctypes.windll.user32

_WM_MOUSEMOVE = 0x0200
_WM_LBUTTONDOWN = 0x0201
_WM_LBUTTONUP = 0x0202
_WM_LBUTTONDBLCLK = 0x0203
_WM_RBUTTONDOWN = 0x0204
_WM_RBUTTONUP = 0x0205
_WM_MBUTTONDOWN = 0x0207
_WM_MBUTTONUP = 0x0208
_WM_MOUSEWHEEL = 0x020A

_MK_LBUTTON = 0x0001
_MK_RBUTTON = 0x0002
_MK_MBUTTON = 0x0010

_CWP_SKIPINVISIBLE = 0x0001
_CWP_SKIPDISABLED = 0x0002
_CWP_SKIPTRANSPARENT = 0x0004


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def _lparam_client(cx: int, cy: int) -> int:
    return (int(cy) << 16) | (int(cx) & 0xFFFF)


def _resolve_hwnd_at(x: int, y: int) -> tuple[int, int, int]:
    """返回 (hwnd, client_x, client_y)。"""
    pt = _POINT(int(x), int(y))
    hwnd = int(user32.WindowFromPoint(pt) or 0)
    if not hwnd:
        return 0, 0, 0
    try:
        client = _POINT(int(x), int(y))
        user32.ScreenToClient(hwnd, ctypes.byref(client))
        child = int(
            user32.ChildWindowFromPointEx(
                hwnd,
                client,
                _CWP_SKIPINVISIBLE | _CWP_SKIPDISABLED | _CWP_SKIPTRANSPARENT,
            )
            or 0
        )
        if child and child != hwnd:
            hwnd = child
            client = _POINT(int(x), int(y))
            user32.ScreenToClient(hwnd, ctypes.byref(client))
        return hwnd, int(client.x), int(client.y)
    except Exception:
        client = _POINT(int(x), int(y))
        user32.ScreenToClient(hwnd, ctypes.byref(client))
        return hwnd, int(client.x), int(client.y)


def _post(hwnd: int, msg: int, wparam: int, lparam: int) -> bool:
    return bool(user32.PostMessageW(hwnd, msg, wparam, lparam))


def virtual_click(
    x: int,
    y: int,
    *,
    button: str = "left",
    double: bool = False,
    delay: float = 0.05,
) -> tuple[bool, str]:
    """在屏幕坐标虚拟点击，不移动物理鼠标。"""
    try:
        x, y = int(x), int(y)
        hwnd, cx, cy = _resolve_hwnd_at(x, y)
        if not hwnd:
            return False, f"({x},{y}) 处无窗口可投递"
        lp = _lparam_client(cx, cy)
        button = (button or "left").lower()
        if button == "left":
            down, up, mk = _WM_LBUTTONDOWN, _WM_LBUTTONUP, _MK_LBUTTON
            dbl = _WM_LBUTTONDBLCLK
        elif button == "right":
            down, up, mk = _WM_RBUTTONDOWN, _WM_RBUTTONUP, _MK_RBUTTON
            dbl = 0
        elif button == "middle":
            down, up, mk = _WM_MBUTTONDOWN, _WM_MBUTTONUP, _MK_MBUTTON
            dbl = 0
        else:
            return False, f"未知按钮: {button}"

        # 先投递 move，部分控件依赖悬停态
        _post(hwnd, _WM_MOUSEMOVE, 0, lp)
        time.sleep(0.01)

        if double and button == "left" and dbl:
            _post(hwnd, down, mk, lp)
            time.sleep(max(0.01, delay))
            _post(hwnd, up, 0, lp)
            time.sleep(max(0.01, delay))
            _post(hwnd, dbl, mk, lp)
            time.sleep(max(0.01, delay))
            ok = _post(hwnd, up, 0, lp)
            action = "虚拟双击"
        else:
            _post(hwnd, down, mk, lp)
            time.sleep(max(0.01, delay))
            ok = _post(hwnd, up, 0, lp)
            if double:
                time.sleep(max(0.01, delay))
                _post(hwnd, down, mk, lp)
                time.sleep(max(0.01, delay))
                ok = _post(hwnd, up, 0, lp) and ok
            action = "虚拟双击" if double else f"虚拟{button}键点击"

        if ok:
            return True, f"已{action} ({x},{y}) → hwnd={hex(hwnd)} client=({cx},{cy})"
        return False, f"{action} PostMessage 失败"
    except Exception as e:
        return False, f"虚拟点击失败: {e}"


def virtual_scroll(
    amount: int = -3,
    x: int | None = None,
    y: int | None = None,
) -> tuple[bool, str]:
    """在指定屏幕坐标虚拟滚轮（不挪光标）。amount 正上负下。"""
    try:
        if x is None or y is None:
            pt = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            x, y = int(pt.x), int(pt.y)
        else:
            x, y = int(x), int(y)
        hwnd, cx, cy = _resolve_hwnd_at(x, y)
        if not hwnd:
            return False, f"({x},{y}) 处无窗口"
        # wParam: high word = delta*120, low word = key flags
        delta = int(amount) * 120
        wparam = (delta << 16) & 0xFFFF0000
        # lParam 对 WM_MOUSEWHEEL 是屏幕坐标
        lparam = (int(y) << 16) | (int(x) & 0xFFFF)
        ok = _post(hwnd, _WM_MOUSEWHEEL, wparam, lparam)
        direction = "向上" if amount > 0 else "向下"
        if ok:
            return True, f"已虚拟{direction}滚动 {abs(int(amount))} 格 @({x},{y})"
        return False, "虚拟滚动 PostMessage 失败"
    except Exception as e:
        return False, f"虚拟滚动失败: {e}"
