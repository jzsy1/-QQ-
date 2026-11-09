"""鼠标控制 — 移动、点击、双击、右键、拖拽、滚动。

使用 Win32 user32.dll API（ctypes 直接调用，无需 PowerShell，延迟最低）。
坐标原点为屏幕左上角，多显示器时可为负数。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import time
from dataclasses import dataclass

# Win32 常量
_MOUSEEVENTF_MOVE = 0x0001
_MOUSEEVENTF_LEFTDOWN = 0x0002
_MOUSEEVENTF_LEFTUP = 0x0004
_MOUSEEVENTF_RIGHTDOWN = 0x0008
_MOUSEEVENTF_RIGHTUP = 0x0010
_MOUSEEVENTF_MIDDLEDOWN = 0x0020
_MOUSEEVENTF_MIDDLEUP = 0x0040
_MOUSEEVENTF_WHEEL = 0x0800
_MOUSEEVENTF_ABSOLUTE = 0x8000


def _ensure_dpi_awareness() -> None:
    """进程级 DPI 感知（幂等，导入时调用一次）：统一物理像素坐标系。

    屏幕识别与鼠标必须在同一坐标空间：PIL 全屏截图按物理分辨率返回
    （如 150% 缩放下 3840x2160），而 DPI 不感知进程的 GetSystemMetrics
    是逻辑值（2560x1440），OCR/UIA 坐标与鼠标换算混用两套空间会导致
    点击整体偏移缩放倍数。声明感知后全部统一为物理像素。
    """
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_AWARE
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


_ensure_dpi_awareness()

# 虚拟屏幕参数不再 import 时缓存：DPI 感知切换/分辨率变化后旧值会导致点击错位，
# 改为每次调用动态读取（GetSystemMetrics 开销可忽略）
def _virtual_screen() -> tuple[int, int, int, int]:
    """返回 (x, y, w, h)——虚拟屏幕原点与尺寸。"""
    user32 = ctypes.windll.user32
    return (
        int(user32.GetSystemMetrics(76)),  # SM_XVIRTUALSCREEN
        int(user32.GetSystemMetrics(77)),  # SM_YVIRTUALSCREEN
        int(user32.GetSystemMetrics(78)),  # SM_CXVIRTUALSCREEN
        int(user32.GetSystemMetrics(79)),  # SM_CYVIRTUALSCREEN
    )


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = (
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    )


class _INPUT(ctypes.Structure):
    class _UNION(ctypes.Union):
        _fields_ = (("mi", _MOUSEINPUT),)

    _anonymous_ = ("_u",)
    _fields_ = (
        ("type", wintypes.DWORD),
        ("_u", _UNION),
    )


def _to_absolute(x: int, y: int) -> tuple[int, int]:
    """将像素坐标转换为 SendInput 需要的 0..65535 归一化坐标（越界钳制）。"""
    vx, vy, vw, vh = _virtual_screen()
    if vw <= 0 or vh <= 0:
        return 0, 0
    ax = int(round((x - vx) * 65535 / vw))
    ay = int(round((y - vy) * 65535 / vh))
    return max(0, min(65535, ax)), max(0, min(65535, ay))


def _send_input(flags: int, mouse_data: int = 0, dx: int = 0, dy: int = 0) -> bool:
    """底层 SendInput 调用。dx/dy 为归一化坐标（仅在含 ABSOLUTE 时使用）。"""
    inp = _INPUT()
    inp.type = 0  # INPUT_MOUSE
    inp.mi = _MOUSEINPUT(
        dx=dx, dy=dy,
        mouseData=mouse_data,
        dwFlags=flags,
        time=0,
        dwExtraInfo=ctypes.pointer(ctypes.c_ulong(0)),
    )
    n = ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))
    return n == 1


def get_cursor_pos() -> tuple[int, int]:
    """获取当前鼠标位置。"""
    pt = wintypes.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def is_click_blocked_by_uipi(x: int, y: int) -> str | None:
    """检测 (x,y) 处目标窗口是否因 UIPI 无法接收点击。

    目标应用以管理员（高完整性）运行而本进程未提权时，系统会静默丢弃
    SendInput/PostMessage 点击，表现为"提示已点击但毫无反应"。
    返回 None 表示可正常点击；返回字符串为用户可读的原因说明。
    """
    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        pt = wintypes.POINT(int(x), int(y))
        hwnd = int(user32.WindowFromPoint(pt) or 0)
        if not hwnd:
            return None
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return None
        _QUERY_FULL = 0x0400
        _QUERY_LIMITED = 0x1000
        h_full = kernel32.OpenProcess(_QUERY_FULL, False, pid.value)
        if h_full:
            kernel32.CloseHandle(h_full)
            return None  # 目标同级或更低，可点击
        h_limited = kernel32.OpenProcess(_QUERY_LIMITED, False, pid.value)
        if h_limited:
            kernel32.CloseHandle(h_limited)
            # 完整查询被拒而受限查询可用 → 目标为高完整性进程
            return (
                "目标应用以管理员权限运行，科科斯当前权限不足，系统(UIPI)已拦截点击。"
                "请以管理员身份重启科科斯后重试（或让目标应用取消管理员运行）。"
            )
        return None  # 受限查询也失败（系统保护进程），放行由真实点击验证
    except Exception:
        return None


def move_to(x: int, y: int, *, smooth: bool = False) -> tuple[bool, str]:
    """移动鼠标到屏幕坐标 (x, y)。

    Args:
        smooth: 是否平滑移动（多步插值，更像人类操作）
    """
    try:
        x, y = int(x), int(y)
        if smooth:
            sx, sy = get_cursor_pos()
            steps = 20
            for i in range(1, steps + 1):
                ix = sx + (x - sx) * i // steps
                iy = sy + (y - sy) * i // steps
                ax, ay = _to_absolute(ix, iy)
                _send_input(_MOUSEEVENTF_MOVE | _MOUSEEVENTF_ABSOLUTE, dx=ax, dy=ay)
                time.sleep(0.01)
        else:
            ax, ay = _to_absolute(x, y)
            _send_input(_MOUSEEVENTF_MOVE | _MOUSEEVENTF_ABSOLUTE, dx=ax, dy=ay)
        return True, f"已移动到 ({x}, {y})"
    except Exception as e:
        return False, f"移动失败: {e}"


def click(x: int | None = None, y: int | None = None, *, button: str = "left", delay: float = 0.05) -> tuple[bool, str]:
    """在指定位置点击（不传坐标则在当前位置点击）。

    Args:
        button: left / right / middle
        delay: 按下与抬起之间的间隔（秒），过短可能被某些应用忽略
    """
    try:
        if x is not None and y is not None:
            ok, msg = move_to(int(x), int(y))
            if not ok:
                return False, msg
            time.sleep(0.05)

        button = (button or "left").lower()
        if button == "left":
            down, up = _MOUSEEVENTF_LEFTDOWN, _MOUSEEVENTF_LEFTUP
        elif button == "right":
            down, up = _MOUSEEVENTF_RIGHTDOWN, _MOUSEEVENTF_RIGHTUP
        elif button == "middle":
            down, up = _MOUSEEVENTF_MIDDLEDOWN, _MOUSEEVENTF_MIDDLEUP
        else:
            return False, f"未知按钮: {button}"

        _send_input(down)
        time.sleep(max(0.01, delay))
        _send_input(up)
        pos = get_cursor_pos()
        return True, f"已{button}键点击 ({pos[0]}, {pos[1]})"
    except Exception as e:
        return False, f"点击失败: {e}"


def double_click(x: int | None = None, y: int | None = None, *, delay: float = 0.08) -> tuple[bool, str]:
    """双击。"""
    try:
        ok, msg = click(x, y, delay=delay)
        if not ok:
            return False, msg
        time.sleep(delay)
        ok2, msg2 = click(delay=delay)
        if not ok2:
            return False, msg2
        return True, "已双击"
    except Exception as e:
        return False, f"双击失败: {e}"


def drag(
    x1: int, y1: int, x2: int, y2: int,
    *,
    button: str = "left",
    duration: float = 0.4,
) -> tuple[bool, str]:
    """从 (x1,y1) 拖拽到 (x2,y2)。

    Args:
        duration: 拖拽总时长（秒），分多步插值
    """
    try:
        ok, msg = move_to(int(x1), int(y1))
        if not ok:
            return False, msg
        time.sleep(0.1)

        button = (button or "left").lower()
        if button == "left":
            down, up = _MOUSEEVENTF_LEFTDOWN, _MOUSEEVENTF_LEFTUP
        elif button == "right":
            down, up = _MOUSEEVENTF_RIGHTDOWN, _MOUSEEVENTF_RIGHTUP
        else:
            return False, f"不支持的拖拽按钮: {button}"

        _send_input(down)
        time.sleep(0.05)

        steps = max(10, int(duration * 60))
        for i in range(1, steps + 1):
            ix = int(x1) + (int(x2) - int(x1)) * i // steps
            iy = int(y1) + (int(y2) - int(y1)) * i // steps
            ax, ay = _to_absolute(ix, iy)
            _send_input(_MOUSEEVENTF_MOVE | _MOUSEEVENTF_ABSOLUTE, dx=ax, dy=ay)
            time.sleep(duration / steps)

        time.sleep(0.05)
        _send_input(up)
        return True, f"已从 ({x1},{y1}) 拖拽到 ({x2},{y2})"
    except Exception as e:
        # 兜底释放按键
        try:
            _send_input(_MOUSEEVENTF_LEFTUP)
        except Exception:
            pass
        return False, f"拖拽失败: {e}"


def scroll(amount: int = -3, x: int | None = None, y: int | None = None) -> tuple[bool, str]:
    """滚动鼠标滚轮。

    Args:
        amount: 滚动量，正数向上，负数向下（一格约 120）
        x, y: 在指定位置滚动（不传则在当前位置）
    """
    try:
        if x is not None and y is not None:
            ok, msg = move_to(int(x), int(y))
            if not ok:
                return False, msg
            time.sleep(0.05)

        # mouseData 为 WHEEL_DELTA (120) 的倍数
        _send_input(_MOUSEEVENTF_WHEEL, mouse_data=int(amount) * 120)
        direction = "向上" if amount > 0 else "向下"
        return True, f"已{direction}滚动 {abs(amount)} 格"
    except Exception as e:
        return False, f"滚动失败: {e}"


@dataclass
class ScreenSize:
    width: int
    height: int
    x: int
    y: int


def get_screen_size() -> ScreenSize:
    """获取虚拟桌面（含所有显示器）的尺寸。"""
    vx, vy, vw, vh = _virtual_screen()
    return ScreenSize(width=vw, height=vh, x=vx, y=vy)
