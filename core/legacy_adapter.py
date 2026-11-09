"""LegacyExecutorAdapter — r1_executor 兼容层，对接 v2 Executor。"""

from __future__ import annotations

from typing import Any, Callable

from ..models.action import Action
from ..models.result import RuntimeExecutionResult
from ..r1_executor import R1Response, execute_call
from ..tools.executor import ToolContext
from .execution_result import from_r1_response
from .executor import Executor


class LegacyExecutorAdapter:
    """将旧 execute_call 包装为 v2 Executor.execute(Action)。"""

    def __init__(
        self,
        ctx: ToolContext,
        *,
        mcp_bridge: Any | None = None,
        mcp_call: Callable | None = None,
        agent_runner: Any | None = None,
    ) -> None:
        self._inner = Executor(
            ctx,
            execute_call_fn=execute_call,
            mcp_bridge=mcp_bridge,
            mcp_call=mcp_call,
            agent_runner=agent_runner,
        )

    @property
    def ctx(self) -> ToolContext:
        return self._inner.ctx

    @property
    def mcp_bridge(self):
        return self._inner.mcp_bridge

    @mcp_bridge.setter
    def mcp_bridge(self, value) -> None:
        self._inner.mcp_bridge = value

    def execute(self, action: Action) -> RuntimeExecutionResult:
        """与 core.Executor 相同签名，供 Supervisor 使用。"""
        return self._inner.execute(action)

    def execute_action(self, action: Action) -> RuntimeExecutionResult:
        return self.execute(action)

    def set_cancel_check(self, fn: Callable[[], bool] | None) -> None:
        self._inner.set_cancel_check(fn)

    def set_stream_callbacks(
        self,
        on_chunk: Callable[[str], None] | None = None,
        on_start: Callable[[], None] | None = None,
    ) -> None:
        self._inner.set_stream_callbacks(on_chunk=on_chunk, on_start=on_start)

    def set_on_tool_event(
        self,
        fn: Callable[[str, bool, str], None] | None,
    ) -> None:
        # WATCHDOG_V1: on_tool_event passthrough
        self._inner.set_on_tool_event(fn)

    def set_goal_controller(self, controller: Any | None) -> None:  # GOAL_CTRL_V1_2
        self._inner.set_goal_controller(controller)

    def execute_call(
        self,
        func: str,
        args: dict | None = None,
        *,
        cancel_check: Callable[[], bool] | None = None,
    ) -> R1Response:
        """兼容 r1_executor.execute_call 签名。"""
        if cancel_check:
            self._inner.set_cancel_check(cancel_check)
        action = Action(tool=func, parameters=dict(args or {}))
        result = self._inner.execute(action)
        if action.tool == "agent_loop":
            data = result.data if isinstance(result.data, dict) else {}
            return R1Response(
                status="success" if result.success else "error",
                func=func,
                args=args or {},
                data={
                    "ok": result.success,
                    "text": result.message,
                    "verified": result.evidence.get("verified", result.success),
                    **data,
                },
            )
        tool_result = from_r1_response(
            R1Response(
                status="success" if result.success else "error",
                func=func,
                args=args or {},
                data={
                    "ok": result.success,
                    "text": result.message,
                    "verified": result.evidence.get("verified", False),
                    **result.evidence,
                },
            )
        )
        return R1Response(
            status="success" if tool_result.success else "error",
            func=func,
            args=args or {},
            data=tool_result.to_dict(),
        )
