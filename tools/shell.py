"""系统底层 Shell 执行 — PowerShell / cmd，权限感知，支持取消。"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from typing import Callable

logger = logging.getLogger(__name__)

_NO_WINDOW = 0x08000000
_MAX_OUTPUT = 6000

_DENIED_MARKERS = (
    "access is denied",
    "拒绝访问",
    "requires elevation",
    "需要提升",
    "run as administrator",
    "以管理员身份",
    "unauthorizedaccess",
    "permission denied",
    "not have permission",
    "privilege not held",
    "拒绝了对",
    "无法打开计算机",
    "无法停止服务",
    "无法启动服务",
    "无法控制服务",
    "open service failed",
    "failed to open service",
    "无法写入注册表",
    "无法访问路径",
)


def _looks_denied(text: str) -> bool:
    low = (text or "").lower()
    return any(m in low or m in (text or "") for m in _DENIED_MARKERS)


def _clip(text: str, limit: int = _MAX_OUTPUT) -> str:
    if len(text) > limit:
        return text[:limit] + "\n…(输出已截断)"
    return text


# SHELL_BUILD_ARGV_EXITCODE_V1: propagate native exit codes to the outer
# powershell process. Without this, `powershell -Command` swallows the inner
# exit code (a failed native command reports outer rc 1 regardless of the
# real code) and run_shell can only report "退出码 1", never 3 or 7.
# Null guard: $LASTEXITCODE is $null when no external program ran; a bare
# `exit $LASTEXITCODE` would then itself produce a non-zero exit, so the
# guard skips in that case. A native `exit N` inside the command exits the
# process before this guard runs, so it still returns N. Cmdlet-only errors
# do not set $LASTEXITCODE and keep the previous success-with-output behavior.
# Cancel/timeout return before the returncode logic below (branch order),
# so a terminated process never yields "执行失败（退出码 N）".
def _build_argv(command: str, shell: str) -> list[str]:
    shell = (shell or "powershell").strip().lower()
    if shell == "cmd":
        full = "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; cmd /c " + command + "; if ($null -ne $LASTEXITCODE) { exit $LASTEXITCODE }"
        return ["powershell", "-NoProfile", "-NonInteractive", "-Command", full]
    full = "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;" + command + "; if ($null -ne $LASTEXITCODE) { exit $LASTEXITCODE }"
    return ["powershell", "-NoProfile", "-NonInteractive", "-Command", full]


def run_shell(
    command: str,
    timeout: int = 90,
    shell: str = "powershell",
    admin: bool = False,
    max_output: int = _MAX_OUTPUT,
    *,
    cancel_check: Callable[[], bool] | None = None,
    task_id: str | None = None,
) -> tuple[bool, str]:
    """执行系统命令，返回 (success, message)。

    cancel_check 为真时终止子进程并返回取消消息。
    """
    command = (command or "").strip()
    if not command:
        return False, "命令不能为空"
    if task_id is None:
        logger.warning("[Shell] run_shell 未传 task_id，子进程将记为 unowned，无法按任务取消")

    if admin:
        # SHELL_ADMIN_CALLER_V1_1: admin path now passes task_id/cancel_check
        # through to privilege.py (v1.2.2). Fast/launcher/runner paths register
        # and are cleaned up on cancel; pre-execution and polling cancel work.
        # RunAs-elevated / SYSTEM targets themselves are outside the process
        # tree and still cannot be tree-killed; cancel stops waiting, not a guarantee.
        logger.warning("[Shell] admin 命令已传 task_id/cancel_check（privilege v1.2.2 注册快路径/launcher/runner），取消可停等待并清理已注册进程，但 RunAs/SYSTEM 目标本身在进程树外，仍不可树杀")
        from .privilege import run_shell_admin

        return run_shell_admin(command, timeout=timeout, task_id=task_id, cancel_check=cancel_check)

    argv = _build_argv(command, shell)
    proc: subprocess.Popen[str] | None = None
    stdout = ""
    stderr = ""
    returncode = 1

    try:
        from ..core.process_registry import register, unregister

        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=_NO_WINDOW,
        )
        register(proc, task_id=task_id)
        deadline = time.monotonic() + max(1, int(timeout))
        comm_result: dict[str, str | int] = {}

        def _communicate():
            try:
                out, err = proc.communicate()
                comm_result["stdout"] = out or ""
                comm_result["stderr"] = err or ""
                comm_result["returncode"] = proc.returncode or 0
            except Exception as exc:
                comm_result["error"] = str(exc)

        comm_th = threading.Thread(target=_communicate, daemon=True)
        comm_th.start()

        while comm_th.is_alive():
            if cancel_check and cancel_check():
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                comm_th.join(timeout=2)
                return False, "命令执行已取消。"
            if time.monotonic() >= deadline:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                comm_th.join(timeout=2)
                return False, f"命令执行超时（超过 {timeout} 秒），已中断。可能命令在等待输入或目标无响应。"
            time.sleep(0.1)

        comm_th.join(timeout=1)
        if comm_result.get("error"):
            return False, f"执行异常: {comm_result['error']}"
        stdout = str(comm_result.get("stdout") or "")
        stderr = str(comm_result.get("stderr") or "")
        returncode = int(comm_result.get("returncode") or 0)
    except FileNotFoundError:
        return False, f"找不到 {shell} 解释器"
    except Exception as e:
        return False, f"执行异常: {e}"
    finally:
        if proc is not None:
            try:
                from ..core.process_registry import unregister

                unregister(proc, task_id=task_id)
            except Exception as e:
                logger.warning("[Shell] 进程注销失败: %s", e)

    # PROCESS_REGISTRY_V1_4_1: external registry kill or cancel during communicate
    # should be reported as cancelled, not generic failure.
    if cancel_check and cancel_check():
        return False, "命令执行已取消。"
    stdout = stdout.strip()
    stderr = stderr.strip()
    success = returncode == 0

    if not success and (_looks_denied(stderr) or _looks_denied(stdout)):
        detail = (stderr or stdout).strip()
        return False, (
            "权限不足（拒绝访问）：该命令需要管理员权限。"
            "可改用管理员执行（run_shell_admin / run_shell 设 admin=true），"
            "或先 elevate_self 提权。\n原始信息：" + _clip(detail, 800)
        )

    if success:
        body = stdout or stderr or "命令已执行（无输出）"
        return True, _clip(body, max_output)

    parts = []
    if stderr:
        parts.append(stderr)
    if stdout:
        parts.append(stdout)
    detail = "\n".join(parts).strip() or "无输出"
    return False, _clip(f"执行失败（退出码 {returncode}）：\n{detail}", max_output)
