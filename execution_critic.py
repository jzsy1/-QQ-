"""执行批评层 — 向后兼容薄封装，委托 core 统一管道。"""

from __future__ import annotations

from typing import Any, Callable

from .core.executor import execute_pipeline
from .core.idempotency import get_default_manager
from .core.verifier import Verifier, needs_verification
from .r1_executor import R1Response
from .tools.executor import ToolContext

# 向后兼容别名
ExecutionCritic = Verifier

# 省略该参数 = 用进程共享的默认幂等注册表；显式传 None = 关闭幂等门
_UNSET = object()


def execute_with_critic(
    func: str,
    args: dict,
    ctx: ToolContext,
    *,
    execute_call: Callable[..., R1Response],
    mcp_bridge: Any | None = None,
    cancel_check: Callable[[], bool] | None = None,
    mcp_call: Callable | None = None,
    idempotency: Any = _UNSET,
    source: str = "",
) -> tuple[R1Response, list[dict], bool, str]:
    """R1 执行 + 验证的统一入口（Agent Loop / Task Engine / Sentinel 共用）。"""
    return execute_pipeline(
        func,
        args,
        ctx,
        execute_call=execute_call,
        mcp_bridge=mcp_bridge,
        cancel_check=cancel_check,
        mcp_call=mcp_call,
        idempotency=get_default_manager() if idempotency is _UNSET else idempotency,
        source=source,
    )


__all__ = [
    "ExecutionCritic",
    "Verifier",
    "execute_with_critic",
    "needs_verification",
]
