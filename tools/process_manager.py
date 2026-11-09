"""进程管理 — 统一关闭/杀进程与物理复核。"""

from __future__ import annotations

import logging

from .execution_result import ExecutionResult, can_report_success
from .process_ops import (
    close_app,
    find_process as find_process_impl,
    is_app_running,
    is_process_alive,
    kill_process,
    list_processes as list_processes_impl,
)

logger = logging.getLogger(__name__)


# CLOSED_LOOP_P2_20261030: waiting poll helpers (single snapshot -> poll)
def _wait_not_running(raw, launch_target, memory, tries=10, interval=0.5):
    import time
    for _ in range(tries):
        if not is_app_running(raw, launch_target, memory=memory):
            return True
        time.sleep(interval)
    return not is_app_running(raw, launch_target, memory=memory)


def _wait_not_alive(raw, tries=10, interval=0.5):
    import time
    for _ in range(tries):
        if not is_process_alive(raw):
            return True
        time.sleep(interval)
    return not is_process_alive(raw)


class ProcessManager:
    """Windows 进程操作封装，禁止未验证的假成功。"""

    def __init__(self, memory=None):
        self.memory = memory

    def close_app(
        self,
        app_name: str,
        launch_target: str = "",
        *,
        force: bool = True,
    ) -> ExecutionResult:
        raw = (app_name or "").strip()
        if not raw:
            return ExecutionResult(
                success=False,
                message="请提供要关闭的应用名称",
                verified=False,
                action="close_app",
                target="",
            )

        if not is_app_running(raw, launch_target, memory=self.memory):
            return ExecutionResult(
                success=True,
                message=f"「{raw}」未在运行",
                verified=True,
                action="close_app",
                target=raw,
            )

        ok, msg = close_app(
            raw,
            launch_target=launch_target,
            force=force,
            memory=self.memory,
        )
        verified = _wait_not_running(raw, launch_target, memory=self.memory)
        if ok and not verified and not force:
            ok2, msg2 = close_app(
                raw,
                launch_target=launch_target,
                force=True,
                memory=self.memory,
            )
            if ok2:
                msg = msg2
            verified = _wait_not_running(raw, launch_target, memory=self.memory)

        success = ok and verified
        if ok and not verified:
            msg = f"{msg}，但未能确认「{raw}」已完全关闭"
            logger.warning("close_app 未通过复核: %s", raw)

        return ExecutionResult(
            success=success,
            message=msg,
            verified=verified,
            action="close_app",
            target=raw,
        )

    def kill_process(
        self,
        target: str,
        *,
        force: bool = False,
    ) -> ExecutionResult:
        raw = (target or "").strip()
        if not raw:
            return ExecutionResult(
                success=False,
                message="请提供进程名或 PID",
                verified=False,
                action="kill_process",
                target="",
            )

        if raw.isdigit():
            if not is_process_alive(raw):
                return ExecutionResult(
                    success=True,
                    message=f"PID {raw} 未在运行",
                    verified=True,
                    action="kill_process",
                    target=raw,
                )
        elif not is_app_running(raw, memory=self.memory):
            return ExecutionResult(
                success=True,
                message=f"进程「{raw}」未在运行",
                verified=True,
                action="kill_process",
                target=raw,
            )

        ok, msg = kill_process(raw, force=force)
        if raw.isdigit():
            verified = _wait_not_alive(raw)
        else:
            verified = _wait_not_running(raw, '', memory=self.memory)

        success = ok and verified
        if ok and not verified:
            msg = f"{msg}，但未能确认进程已终止"
            logger.warning("kill_process 未通过复核: %s", raw)

        return ExecutionResult(
            success=success,
            message=msg,
            verified=verified,
            action="kill_process",
            target=raw,
        )

    def to_tool_tuple(self, result: ExecutionResult) -> tuple[bool, str]:
        return can_report_success(result), result.message

    def list_processes(self, *, limit: int = 40) -> ExecutionResult:
        ok, msg = list_processes_impl(limit=limit)
        return ExecutionResult(
            success=ok,
            message=msg,
            verified=ok,
            action="list_processes",
            target="",
        )

    def find_process(self, keyword: str, *, limit: int = 20) -> ExecutionResult:
        raw = (keyword or "").strip()
        if not raw:
            return ExecutionResult(
                success=False,
                message="请提供进程名、PID 或关键词",
                verified=False,
                action="find_process",
                target="",
            )
        ok, msg = find_process_impl(raw, limit=limit)
        return ExecutionResult(
            success=ok,
            message=msg,
            verified=ok,
            action="find_process",
            target=raw,
        )
