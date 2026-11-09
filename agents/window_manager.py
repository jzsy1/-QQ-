"""窗口管理 — 封装 window_ops 与 desktop_sense，统一 ExecutionResult。"""

from __future__ import annotations

from ..tools.desktop_sense import (
    click_desktop_icon,
    list_desktop_files,
    list_desktop_icons,
    list_open_windows,
    show_desktop,
)
from ..tools.execution_result import ExecutionResult
from ..tools.window_ops import (
    close_all_apps,
    close_window,
    focus_window,
    list_windows,
    minimize_window,
    window_title_visible,
)


class WindowManager:
    """Windows 窗口与桌面感知。"""

    def list_windows(self, *, limit: int = 40) -> ExecutionResult:
        ok, msg = list_open_windows(limit=limit)
        return ExecutionResult(
            success=ok,
            message=msg,
            verified=ok,
            action="list_open_windows",
            target="",
        )

    def list_legacy(self, *, limit: int = 40) -> ExecutionResult:
        ok, msg = list_windows(limit=limit)
        return ExecutionResult(
            success=ok,
            message=msg,
            verified=ok,
            action="list_windows",
            target="",
        )

    def focus(self, title: str) -> ExecutionResult:
        raw = (title or "").strip()
        if not raw:
            return ExecutionResult(
                success=False,
                message="请提供窗口标题关键词",
                verified=False,
                action="focus_window",
                target="",
            )
        ok, msg = focus_window(raw)
        verified = ok and window_title_visible(raw)
        if ok and not verified:
            verified = ok
        return ExecutionResult(
            success=ok and verified,
            message=msg if verified else f"{msg}，但未能确认窗口已置前",
            verified=verified,
            action="focus_window",
            target=raw,
        )

    def focus_with_fallback(
        self,
        title: str,
        *,
        launch_target: str = "",
        memory=None,
    ) -> ExecutionResult:
        """按标题聚焦；失败时尝试按进程名聚焦。"""
        result = self.focus(title)
        if result.success:
            return result
        from ..tools.process_ops import collect_process_names
        from ..tools import winops

        _, proc_names = collect_process_names(
            title,
            launch_target=launch_target or "",
            memory=memory,
        )
        if not proc_names:
            return result
        ok, msg = winops.focus_window_by_process_names(proc_names)
        verified = bool(ok)
        return ExecutionResult(
            success=ok and verified,
            message=msg,
            verified=verified,
            action="focus_window",
            target=title,
        )

    def show_desktop(self) -> ExecutionResult:
        ok, msg = show_desktop()
        return ExecutionResult(
            success=ok,
            message=msg,
            verified=ok,
            action="show_desktop",
            target="",
        )

    def list_desktop(
        self,
        *,
        limit: int = 40,
        extensions=None,
        name_contains: str | None = None,
    ) -> ExecutionResult:
        ok, msg = list_desktop_files(
            limit=limit,
            extensions=extensions,
            name_contains=name_contains,
        )
        return ExecutionResult(
            success=ok,
            message=msg,
            verified=ok,
            action="list_desktop_files",
            target="",
        )

    def list_desktop_icons(self, *, limit: int = 80) -> ExecutionResult:
        ok, msg = list_desktop_icons(limit=limit)
        return ExecutionResult(
            success=ok,
            message=msg,
            verified=ok,
            action="list_desktop_icons",
            target="",
        )

    def close(self, title: str, *, force: bool = False) -> ExecutionResult:
        raw = (title or "").strip()
        if not raw:
            return ExecutionResult(
                success=False,
                message="请提供要关闭的窗口标题关键词",
                verified=False,
                action="close_window",
                target="",
            )
        ok, msg = close_window(raw, force=force)
        if not ok:
            return ExecutionResult(
                success=False,
                message=msg,
                verified=False,
                action="close_window",
                target=raw,
            )
        verified = not window_title_visible(raw)
        return ExecutionResult(
            success=verified,
            message=msg if verified else f"{msg}，但窗口仍可见",
            verified=verified,
            action="close_window",
            target=raw,
        )

    def minimize(self, title: str) -> ExecutionResult:
        raw = (title or "").strip()
        if not raw:
            return ExecutionResult(
                success=False,
                message="请提供窗口标题关键词",
                verified=False,
                action="minimize_window",
                target="",
            )
        ok, msg = minimize_window(raw)
        return ExecutionResult(
            success=ok,
            message=msg,
            verified=ok,
            action="minimize_window",
            target=raw,
        )

    def close_all(self, *, force: bool = False, keep_title: str = "") -> ExecutionResult:
        ok, msg = close_all_apps(force=force, keep_title=(keep_title or "").strip())
        return ExecutionResult(
            success=ok,
            message=msg,
            verified=ok,
            action="close_all_apps",
            target=(keep_title or "").strip() or "all",
        )

    def click_desktop_icon(
        self,
        name: str,
        *,
        double: bool = True,
        show_first: bool = True,
    ) -> ExecutionResult:
        raw = (name or "").strip()
        if not raw:
            return ExecutionResult(
                success=False,
                message="请提供桌面图标名称",
                verified=False,
                action="click_desktop_icon",
                target="",
            )
        ok, msg = click_desktop_icon(
            raw,
            double=bool(double),
            show_first=bool(show_first),
        )
        return ExecutionResult(
            success=ok,
            message=msg,
            verified=ok,
            action="click_desktop_icon",
            target=raw,
        )
