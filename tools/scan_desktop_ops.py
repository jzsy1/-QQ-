# -*- coding: utf-8 -*-
"""一体桌面快照 — 窗口可见度 + 控件摘要 + 桌面空白区 + 任务栏。"""

from __future__ import annotations

import logging

from .occlusion_ops import (
    find_uncovered_regions,
    list_windows_visibility,
    virtual_screen_rect,
)

logger = logging.getLogger(__name__)


def _taskbar_info() -> str:
    try:
        import ctypes
        import ctypes.wintypes as wintypes

        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW("Shell_TrayWnd", None)
        if not hwnd:
            return "任务栏：未找到"
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        return (
            f"任务栏：hwnd={hex(int(hwnd))} "
            f"rect={rect.left},{rect.top},{rect.right},{rect.bottom}"
        )
    except Exception as e:
        return f"任务栏：读取失败 ({e})"


def _controls_summary(title: str, *, limit: int = 12) -> str:
    if not title:
        return ""
    try:
        from .uia_ops import list_controls

        ok, msg = list_controls(
            root_window_title=title[:40],
            scope="children",
            limit=limit,
        )
        if not ok:
            return f"  控件：{msg[:60]}"
        lines = [ln for ln in (msg or "").splitlines() if ln.strip()]
        # 跳过标题行
        body = lines[1: limit + 1] if len(lines) > 1 else lines[:limit]
        if not body:
            return "  控件：（无）"
        return "  控件：\n    " + "\n    ".join(b[:100] for b in body)
    except Exception as e:
        logger.debug("controls summary: %s", e)
        return f"  控件：跳过 ({e})"


def scan_desktop(
    *,
    limit: int = 20,
    include_controls: bool = True,
    include_regions: bool = True,
    grid: int = 6,
) -> tuple[bool, str]:
    """完整桌面扫描：屏幕、窗口可见度%、可选控件摘要、空白区、任务栏。"""
    limit = max(5, min(int(limit), 40))
    vx0, vy0, vx1, vy1 = virtual_screen_rect()
    parts: list[str] = [
        f"虚拟屏幕：({vx0},{vy0})-({vx1},{vy1}) 尺寸 {vx1 - vx0}x{vy1 - vy0}",
        _taskbar_info(),
    ]

    wins = list_windows_visibility(limit=limit, grid=grid)
    if not wins:
        parts.append("窗口：（无可见顶层窗）")
    else:
        parts.append(f"窗口共 {len(wins)} 个：")
        for w in wins:
            flag = "激活" if w.active else ("遮挡" if w.occluded else "可见")
            line = (
                f"- [{flag} {w.visibility_pct:g}%] {w.title[:50]}｜"
                f"{w.process}｜hwnd={hex(w.hwnd)}｜"
                f"{w.left},{w.top},{w.right},{w.bottom}"
            )
            parts.append(line)
            if include_controls and w.visibility_pct >= 30 and (w.active or not w.occluded):
                # 只给激活/较可见窗拉控件，控体积
                if w.active or w.visibility_pct >= 80:
                    parts.append(_controls_summary(w.title, limit=10))

    if include_regions:
        regions = find_uncovered_regions()
        if regions:
            parts.append(f"桌面露出空白区 {len(regions)} 块：")
            for i, r in enumerate(regions[:8], 1):
                parts.append(
                    f"  {i}. ({r.left},{r.top})-({r.right},{r.bottom}) 面积≈{r.area}"
                )
        else:
            parts.append("桌面露出空白区：无（或几乎被窗铺满）")

    return True, "\n".join(parts)
