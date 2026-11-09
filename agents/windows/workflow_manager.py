"""WorkflowManager — 工作流存取与执行。"""

from __future__ import annotations

from typing import Any

from ...tools import workflow_ops
from ...tools.execution_result import ExecutionResult, from_tuple


class WorkflowManager:
    """学习/手动工作流的列表、保存与执行。"""

    def __init__(self, tool_ctx: Any = None) -> None:
        self._ctx = tool_ctx

    def _db(self):
        if self._ctx and getattr(self._ctx, "memory", None):
            return self._ctx.memory.db
        return None

    def _exec_step(self, func: str, args: dict) -> tuple[bool, str]:
        from ...r1_executor import STATUS_SUCCESS, execute_call, r1_to_tool_output
        from ...safety.guard import check_tool_call

        allowed, deny = check_tool_call(
            func, args or {}, user_text="", recent_assistant=""
        )
        if not allowed:
            return False, deny or "安全闸门拒绝"
        resp = execute_call({"func": func, "args": args or {}}, self._ctx)
        out = r1_to_tool_output(resp)
        ok = resp.status == STATUS_SUCCESS and out.ok
        return ok, out.text or ""

    def list_workflows(self, *, limit: int = 20) -> ExecutionResult:
        ok, msg = workflow_ops.list_workflows(self._db(), limit=limit)
        return from_tuple(ok, msg, verified=ok, action="list_workflows")

    def save_workflow(
        self,
        name: str,
        steps: list,
        *,
        title: str = "",
        trigger_hint: str = "",
    ) -> ExecutionResult:
        if not isinstance(steps, list):
            return ExecutionResult(
                success=False,
                message="steps 须为数组",
                verified=False,
                action="save_workflow",
                target=(name or "")[:40],
            )
        ok, msg = workflow_ops.save_workflow(
            self._db(),
            name,
            steps,
            title=title,
            trigger_hint=trigger_hint,
        )
        return from_tuple(
            ok,
            msg,
            verified=ok,
            action="save_workflow",
            target=(name or "")[:40],
        )

    def run_workflow(
        self,
        name: str,
        *,
        user_confirmed: bool = False,
        user_text: str = "",
    ) -> ExecutionResult:
        db = self._db()
        if not db:
            return ExecutionResult(
                success=False,
                message="记忆库不可用",
                verified=False,
                action="run_workflow",
                target=(name or "")[:40],
            )
        ok, msg = workflow_ops.run_workflow(
            db,
            name,
            self._exec_step,
            user_confirmed=user_confirmed,
            user_text=user_text,
        )
        return from_tuple(
            ok,
            msg,
            verified=ok,
            action="run_workflow",
            target=(name or "")[:40],
        )

    def run_sequence(
        self,
        steps: list,
        *,
        stop_on_error: bool = True,
    ) -> ExecutionResult:
        if not isinstance(steps, list):
            return ExecutionResult(
                success=False,
                message="steps 须为数组",
                verified=False,
                action="run_sequence",
            )
        ok, msg = workflow_ops.run_sequence(
            steps,
            self._exec_step,
            stop_on_error=stop_on_error,
        )
        return from_tuple(ok, msg, verified=ok, action="run_sequence")
