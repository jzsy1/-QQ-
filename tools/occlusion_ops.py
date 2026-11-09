# -*- coding: utf-8 -*-
"""窗口/控件遮挡检测 — 点检、网格可见度%、桌面空白区。"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
from dataclasses import dataclass, field

user32 = ctypes.windll.user32

_GA_ROOT = 2
_CWP_SKIPINVISIBLE = 0x0001
_CWP_SKIPDISABLED = 0x0002
_CWP_SKIPTRANSPARENT = 0x0004
_SM_XVIRTUALSCREEN = 76
_SM_YVIRTUALSCREEN = 77
_SM_CXVIRTUALSCREEN = 78
_SM_CYVIRTUALSCREEN = 79


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


@dataclass
class OcclusionResult:
    occluded: bool
    message: str
    target_hwnd: int = 0
    blocker_hwnd: int = 0
    blocker_title: str = ""
    at_x: int = 0
    at_y: int = 0
    visibility_pct: float = -1.0


@dataclass
class WindowVisibility:
    hwnd: int
    title: str
    process: str
    left: int
    top: int
    right: int
    bottom: int
    visibility_pct: float
    occluded: bool
    active: bool = False


@dataclass
class DesktopRegion:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def area(self) -> int:
        return max(0, self.right - self.left) * max(0, self.bottom - self.top)


def _window_title(hwnd: int) -> str:
    if not hwnd:
        return ""
    n = int(user32.GetWindowTextLengthW(hwnd) or 0)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return (buf.value or "").strip()


def _root_hwnd(hwnd: int) -> int:
    if not hwnd:
        return 0
    root = int(user32.GetAncestor(hwnd, _GA_ROOT) or 0)
    return root or int(hwnd)


def _is_same_or_related(a: int, b: int) -> bool:
    if not a or not b:
        return False
    if int(a) == int(b):
        return True
    if _root_hwnd(a) == _root_hwnd(b):
        return True
    if user32.IsChild(a, b) or user32.IsChild(b, a):
        return True
    return False


def _window_from_point(x: int, y: int) -> int:
    pt = _POINT(int(x), int(y))
    hwnd = int(user32.WindowFromPoint(pt) or 0)
    if not hwnd:
        return 0
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
        if child:
            return child
    except Exception:
        pass
    return hwnd


def _find_hwnd_by_title(title: str) -> int:
    needle = (title or "").strip().lower()
    if not needle:
        return 0
    found = ctypes.c_void_p(0)

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, _lp):  # type: ignore[misc]
        if not user32.IsWindowVisible(hwnd):
            return True
        t = _window_title(int(hwnd)).lower()
        if t and needle in t:
            found.value = hwnd
            return False
        return True

    user32.EnumWindows(_cb, 0)
    return int(found.value or 0)


def virtual_screen_rect() -> tuple[int, int, int, int]:
    x = int(user32.GetSystemMetrics(_SM_XVIRTUALSCREEN))
    y = int(user32.GetSystemMetrics(_SM_YVIRTUALSCREEN))
    w = int(user32.GetSystemMetrics(_SM_CXVIRTUALSCREEN))
    h = int(user32.GetSystemMetrics(_SM_CYVIRTUALSCREEN))
    return x, y, x + max(1, w), y + max(1, h)


def check_point_occlusion(
    x: int,
    y: int,
    *,
    expected_hwnd: int = 0,
    expected_title: str = "",
) -> OcclusionResult:
    x, y = int(x), int(y)
    top = _window_from_point(x, y)
    expected = int(expected_hwnd or 0)
    if not expected and expected_title:
        expected = _find_hwnd_by_title(expected_title)

    if not top:
        return OcclusionResult(
            True, f"({x},{y}) 处无窗口", target_hwnd=expected, at_x=x, at_y=y
        )

    if not expected:
        title = _window_title(_root_hwnd(top)) or _window_title(top)
        return OcclusionResult(
            False,
            f"({x},{y}) 命中「{title or hex(top)}」（未指定期望窗，视为未遮挡）",
            target_hwnd=int(top),
            blocker_hwnd=int(top),
            blocker_title=title,
            at_x=x,
            at_y=y,
        )

    if _is_same_or_related(top, expected):
        return OcclusionResult(
            False,
            f"({x},{y}) 未被遮挡，命中目标窗",
            target_hwnd=expected,
            at_x=x,
            at_y=y,
        )

    blocker_root = _root_hwnd(top)
    blocker_title = _window_title(blocker_root) or _window_title(top)
    return OcclusionResult(
        True,
        f"({x},{y}) 被「{blocker_title or hex(blocker_root)}」遮挡",
        target_hwnd=expected,
        blocker_hwnd=blocker_root,
        blocker_title=blocker_title,
        at_x=x,
        at_y=y,
    )


def grid_visibility(
    hwnd: int,
    *,
    grid: int = 8,
) -> float:
    """对窗口客户区做 grid×grid 采样，返回可见比例 0~100。"""
    root = _root_hwnd(int(hwnd or 0))
    if not root or not user32.IsWindow(root):
        return 0.0
    rect = _RECT()
    if not user32.GetWindowRect(root, ctypes.byref(rect)):
        return 0.0
    left, top, right, bottom = int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)
    w, h = right - left, bottom - top
    if w < 4 or h < 4:
        return 0.0
    n = max(2, min(int(grid), 16))
    hit = 0
    total = 0
    for iy in range(n):
        for ix in range(n):
            px = left + (ix * 2 + 1) * w // (n * 2)
            py = top + (iy * 2 + 1) * h // (n * 2)
            top_hwnd = _window_from_point(px, py)
            total += 1
            if top_hwnd and _is_same_or_related(top_hwnd, root):
                hit += 1
    return round(100.0 * hit / max(1, total), 1)


def check_window_occlusion(
    *,
    hwnd: int = 0,
    title: str = "",
    samples: int = 5,
    grid: int = 8,
) -> OcclusionResult:
    target = int(hwnd or 0)
    if not target and title:
        target = _find_hwnd_by_title(title)
    if not target or not user32.IsWindow(target):
        return OcclusionResult(True, f"未找到窗口「{title or hwnd}」")

    root = _root_hwnd(target)
    pct = grid_visibility(root, grid=grid)
    occluded = pct < 95.0
    name = _window_title(root) or hex(root)
    if pct <= 0:
        return OcclusionResult(
            True,
            f"窗口「{name}」完全遮挡/不可见（可见度 {pct}%）",
            target_hwnd=root,
            visibility_pct=pct,
        )
    if occluded:
        return OcclusionResult(
            True,
            f"窗口「{name}」部分遮挡（可见度 {pct}%）",
            target_hwnd=root,
            visibility_pct=pct,
        )
    return OcclusionResult(
        False,
        f"窗口「{name}」可见（可见度 {pct}%）",
        target_hwnd=root,
        visibility_pct=pct,
    )


def check_occlusion(
    *,
    x: int | None = None,
    y: int | None = None,
    hwnd: int = 0,
    title: str = "",
) -> tuple[bool, str]:
    if x is not None and y is not None:
        r = check_point_occlusion(
            int(x), int(y), expected_hwnd=int(hwnd or 0), expected_title=title
        )
        status = "遮挡" if r.occluded else "未遮挡"
        return True, f"{status}：{r.message}"
    if hwnd or title:
        r = check_window_occlusion(hwnd=int(hwnd or 0), title=title)
        status = "遮挡" if r.occluded else "未遮挡"
        extra = f"（可见度 {r.visibility_pct}%）" if r.visibility_pct >= 0 else ""
        return True, f"{status}：{r.message}{extra}"
    return False, "请提供坐标 (x,y) 或窗口 hwnd/title"


def _process_name(pid: int) -> str:
    if pid <= 0:
        return ""
    try:
        import psutil

        return psutil.Process(pid).name() or str(pid)
    except Exception:
        return str(pid)


def list_windows_visibility(*, limit: int = 40, grid: int = 6) -> list[WindowVisibility]:
    """枚举顶层可见窗并计算网格可见度%。"""
    limit = max(5, min(int(limit), 80))
    foreground = int(user32.GetForegroundWindow() or 0)
    results: list[WindowVisibility] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, _lp):  # type: ignore[misc]
        if len(results) >= limit:
            return False
        if not user32.IsWindowVisible(hwnd):
            return True
        if user32.GetWindow(hwnd, 4):  # GW_OWNER
            return True
        title = _window_title(int(hwnd))
        if not title:
            return True
        rect = _RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return True
        if rect.right - rect.left <= 0 or rect.bottom - rect.top <= 0:
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        pct = grid_visibility(int(hwnd), grid=grid)
        results.append(
            WindowVisibility(
                hwnd=int(hwnd),
                title=title,
                process=_process_name(int(pid.value)),
                left=int(rect.left),
                top=int(rect.top),
                right=int(rect.right),
                bottom=int(rect.bottom),
                visibility_pct=pct,
                occluded=pct < 95.0,
                active=int(hwnd) == foreground,
            )
        )
        return True

    user32.EnumWindows(_cb, 0)
    results.sort(key=lambda w: (not w.active, w.visibility_pct, w.title.lower()))
    return results


def find_uncovered_regions(*, cell: int = 80, min_cells: int = 4) -> list[DesktopRegion]:
    """洪水填充找屏幕上未被顶层应用窗盖住的空白区（桌面露出）。"""
    vx0, vy0, vx1, vy1 = virtual_screen_rect()
    cell = max(40, min(int(cell), 200))
    cols = max(1, (vx1 - vx0) // cell)
    rows = max(1, (vy1 - vy0) // cell)

    # 桌面类宿主：Progman / WorkerW / Shell_TrayWnd 视为「未遮挡桌面」
    def _is_desktop_host(hwnd: int) -> bool:
        root = _root_hwnd(hwnd)
        cls = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(root, cls, 64)
        name = (cls.value or "").lower()
        return name in ("progman", "workerw", "shell_traywnd", "shell_secondarytraywnd")

    free = [[False] * cols for _ in range(rows)]
    for ry in range(rows):
        for cx in range(cols):
            px = vx0 + cx * cell + cell // 2
            py = vy0 + ry * cell + cell // 2
            hit = _window_from_point(px, py)
            free[ry][cx] = (not hit) or _is_desktop_host(hit)

    visited = [[False] * cols for _ in range(rows)]
    regions: list[DesktopRegion] = []

    for ry in range(rows):
        for cx in range(cols):
            if not free[ry][cx] or visited[ry][cx]:
                continue
            # BFS
            stack = [(cx, ry)]
            visited[ry][cx] = True
            cells: list[tuple[int, int]] = []
            while stack:
                x, y = stack.pop()
                cells.append((x, y))
                for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                    if 0 <= nx < cols and 0 <= ny < rows and free[ny][nx] and not visited[ny][nx]:
                        visited[ny][nx] = True
                        stack.append((nx, ny))
            if len(cells) < min_cells:
                continue
            xs = [c[0] for c in cells]
            ys = [c[1] for c in cells]
            regions.append(
                DesktopRegion(
                    left=vx0 + min(xs) * cell,
                    top=vy0 + min(ys) * cell,
                    right=vx0 + (max(xs) + 1) * cell,
                    bottom=vy0 + (max(ys) + 1) * cell,
                )
            )

    regions.sort(key=lambda r: -r.area)
    return regions[:12]
