"""EnvManager — 环境变量读写。"""

from __future__ import annotations

from ...tools import env_ops
from ...tools.execution_result import ExecutionResult, from_tuple


class EnvManager:
    """用户/进程/机器级环境变量。"""

    def read(self, name: str) -> ExecutionResult:
        raw = (name or "").strip()
        if not raw:
            return ExecutionResult(
                success=False,
                message="请提供环境变量名",
                verified=False,
                action="read_env_var",
                target="",
            )
        ok, msg = env_ops.read_env_var(raw)
        return from_tuple(ok, msg, verified=ok, action="read_env_var", target=raw)

    def set(self, name: str, value: str) -> ExecutionResult:
        raw = (name or "").strip()
        if not raw:
            return ExecutionResult(
                success=False,
                message="请提供环境变量名",
                verified=False,
                action="set_env_var",
                target="",
            )
        ok, msg = env_ops.set_env_var(raw, str(value or ""))
        return from_tuple(ok, msg, verified=ok, action="set_env_var", target=raw)

    def list_vars(self, *, prefix: str = "", limit: int = 40) -> ExecutionResult:
        ok, msg = env_ops.list_env_vars(prefix=prefix, limit=limit)
        return from_tuple(ok, msg, verified=ok, action="list_env_vars", target=prefix or "all")
