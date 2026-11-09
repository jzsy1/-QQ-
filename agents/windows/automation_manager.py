"""AutomationManager — 等待条件与屏幕稳定检测。"""

from __future__ import annotations

from ...tools.execution_result import ExecutionResult, from_tuple
from ...tools import wait_ops


class AutomationManager:
    """自动化等待与条件轮询。"""

    def wait_until_found(
        self,
        text: str,
        *,
        control_type: str = "",
        app: str = "",
        timeout: float = 15.0,
        partial: bool = True,
    ) -> ExecutionResult:
        raw = (text or "").strip()
        if not raw and not (control_type or "").strip():
            return ExecutionResult(
                success=False,
                message="请提供要等待的文字或控件类型",
                verified=False,
                action="wait_until_found",
                target="",
            )
        ok, msg = wait_ops.wait_until_found(
            raw,
            control_type=control_type,
            app=app,
            timeout=timeout,
            partial=partial,
        )
        return from_tuple(ok, msg, verified=ok, action="wait_until_found", target=raw[:40])

    def wait_until_gone(
        self,
        text: str,
        *,
        app: str = "",
        timeout: float = 15.0,
    ) -> ExecutionResult:
        raw = (text or "").strip()
        if not raw:
            return ExecutionResult(
                success=False,
                message="请提供要等待消失的文字",
                verified=False,
                action="wait_until_gone",
                target="",
            )
        ok, msg = wait_ops.wait_until_gone(raw, app=app, timeout=timeout)
        return from_tuple(ok, msg, verified=ok, action="wait_until_gone", target=raw[:40])

    def wait_for_stable_screen(
        self,
        *,
        timeout: float = 10.0,
        stable_for: float = 0.8,
    ) -> ExecutionResult:
        ok, msg = wait_ops.wait_for_stable_screen(
            timeout=timeout,
            stable_for=stable_for,
        )
        return from_tuple(ok, msg, verified=ok, action="wait_for_stable_screen")

    def wait_for_file(
        self,
        path: str,
        *,
        timeout: float = 30.0,
        stable_for: float = 1.0,
    ) -> ExecutionResult:
        raw = (path or "").strip()
        if not raw:
            return ExecutionResult(
                success=False,
                message="请提供文件路径",
                verified=False,
                action="wait_for_file",
                target="",
            )
        ok, msg = wait_ops.wait_for_file(
            raw,
            timeout=timeout,
            stable_for=stable_for,
        )
        return from_tuple(ok, msg, verified=ok, action="wait_for_file", target=raw)
