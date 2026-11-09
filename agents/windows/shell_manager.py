"""ShellManager — PowerShell/CMD 统一返回。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ...tools.shell import run_shell


@dataclass
class ShellResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int = 0
    timed_out: bool = False
    cancelled: bool = False
    success: bool = False
    message: str = ""


class ShellManager:
    """Shell 执行，禁止「无异常」即成功。"""

    def run(
        self,
        command: str,
        *,
        timeout: int = 30,
        shell: str = "powershell",
        admin: bool = False,
        cancel_check: Callable[[], bool] | None = None,
        task_id: str | None = None,
    ) -> ShellResult:
        ok, msg = run_shell(
            command,
            timeout=timeout,
            shell=shell,
            admin=admin,
            cancel_check=cancel_check,
            task_id=task_id,
        )
        cancelled = "已取消" in (msg or "")
        timed_out = "超时" in (msg or "")
        return ShellResult(
            exit_code=0 if ok else 1,
            stdout=msg if ok else "",
            stderr="" if ok else msg,
            timed_out=timed_out,
            cancelled=cancelled,
            success=ok,
            message=msg,
        )

    def run_system(self, command: str, *, timeout: int = 180, cancel_check=None, task_id: str | None = None) -> ShellResult:
        from ...tools.privilege import run_shell_system

        # SHELL_SYSTEM_CALLER_V1: pass task context through to privilege.
        ok, msg = run_shell_system(command, timeout=timeout, task_id=task_id, cancel_check=cancel_check)
        return ShellResult(
            exit_code=0 if ok else 1,
            stdout=msg if ok else "",
            stderr="" if ok else msg,
            success=ok,
            message=msg,
        )
