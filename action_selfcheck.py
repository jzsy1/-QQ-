"""Agent Loop 辅助 — R1 执行后物理自检（委托 WindowsVerifier）。"""

from __future__ import annotations

import logging
from typing import Any

from .agents.windows.verifier import VERIFY_FUNCS, verification_enabled
from .r1_executor import R1Response
from .tools.executor import ToolContext

logger = logging.getLogger(__name__)

__all__ = ["verification_enabled", "run_post_check", "VERIFY_FUNCS"]


def run_post_check(
    func: str,
    args: dict,
    r1: R1Response,
    ctx: ToolContext,
    *,
    learning: Any | None = None,
    mcp_bridge: Any | None = None,
) -> tuple[R1Response, list[dict]]:
    """R1 返回后验证物理结果。"""
    _ = learning
    verifier = ctx.windows_agent.verifier
    return verifier.run_post_check(
        func, args, r1, ctx, mcp_bridge=mcp_bridge
    )
