"""应用启动物理验证 — launch 命令发出后轮询进程/窗口。"""

from __future__ import annotations

import logging
import time

from .execution_result import ExecutionResult, can_report_success
from .process_ops import has_app_main_window, is_app_running

logger = logging.getLogger(__name__)


def wait_for_app_started(
    app_name: str,
    launch_target: str = "",
    memory=None,
    *,
    timeout: float = 8.0,
    require_window: bool = False,
    poll_interval: float = 0.3,
) -> bool:
    """轮询直到应用进程出现（可选要求可见主窗口）。"""
    if not (app_name or "").strip():
        return False
    deadline = time.monotonic() + max(0.5, timeout)
    while time.monotonic() < deadline:
        if require_window:
            if has_app_main_window(app_name, launch_target, memory):
                return True
        elif is_app_running(app_name, launch_target, memory):
            return True
        time.sleep(poll_interval)
    return False


def verify_launch_result(
    launched: bool,
    launch_msg: str,
    app_name: str,
    launch_target: str = "",
    memory=None,
    *,
    timeout: float = 8.0,
    require_window: bool = False,
) -> ExecutionResult:
    """将 launch_app 的启动结果与物理验证合并。"""
    display = (app_name or "").strip()
    if not launched:
        return ExecutionResult(
            success=False,
            message=launch_msg,
            verified=False,
            action="open_app",
            target=display,
        )
    verified = wait_for_app_started(
        display,
        launch_target,
        memory,
        timeout=timeout,
        require_window=require_window,
    )
    if verified:
        return ExecutionResult(
            success=True,
            message=launch_msg,
            verified=True,
            action="open_app",
            target=display,
        )
    logger.warning("启动命令已发出但未检测到进程: %s (%s)", display, launch_target[:80])
    return ExecutionResult(
        success=False,
        message=f"{launch_msg}，但未检测到「{display}」已启动",
        verified=False,
        action="open_app",
        target=display,
    )


def execution_to_ok_msg(result: ExecutionResult) -> tuple[bool, str]:
    """供仍使用 tuple[bool, str] 的调用方。"""
    return can_report_success(result), result.message
