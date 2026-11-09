"""R1 物理执行层 — 仅接受结构化函数调用，不对用户说话。

输入格式：{"func": "...", "args": {...}}
输出格式：{"status": "...", "func": "...", "args": {...}, "data": {...}}

状态码仅五种：success / invalid_format / func_not_found / physical_blocked / executing
解读状态、生成用户回复由 Agent Loop 负责。
"""

from __future__ import annotations

import logging
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any, Callable

from .core.exec_locks import domain_lock
from .tools.executor import ToolContext, ToolOutput, execute_tool_guarded
from .tools_policy import is_tool_enabled
from .utils import parse_json_dict

logger = logging.getLogger(__name__)

_TOOL_CALL_BUDGET: ContextVar[dict | None] = ContextVar("tool_call_budget", default=None)
_AUTONOMY_EXECUTION: ContextVar[dict | None] = ContextVar("autonomy_execution", default=None)


def set_tool_call_budget(limit: int | None, state: dict | None = None) -> Token:
    if limit is None:
        return _TOOL_CALL_BUDGET.set(None)
    if isinstance(state, dict) and {"limit", "used", "exceeded"}.issubset(state):
        return _TOOL_CALL_BUDGET.set(state)
    return _TOOL_CALL_BUDGET.set({"limit": max(0, int(limit)), "used": 0, "exceeded": False})


def reset_tool_call_budget(token: Token) -> None:
    _TOOL_CALL_BUDGET.reset(token)


def tool_call_budget_exceeded() -> bool:
    budget = _TOOL_CALL_BUDGET.get()
    return bool(budget and budget.get("exceeded"))


def _reserve_tool_call() -> bool:
    budget = _TOOL_CALL_BUDGET.get()
    if budget is None:
        return True
    if int(budget["used"]) >= int(budget["limit"]):
        budget["exceeded"] = True
        return False
    budget["used"] += 1
    return True


def set_autonomy_execution(
    enabled: bool,
    *,
    action_authorizer: Callable[[str, dict, bool], bool] | None = None,
    goal_text: str = "",
) -> Token:
    state = None
    if enabled:
        state = {
            "enabled": True,
            "violated": False,
            "action_authorizer": action_authorizer,
            "goal_text": str(goal_text or ""),
            "authorized_actions": [],
        }
    return _AUTONOMY_EXECUTION.set(state)


def reset_autonomy_execution(token: Token) -> None:
    _AUTONOMY_EXECUTION.reset(token)


def autonomous_tool_decision(
    func: str,
    args: dict | None = None,
    *,
    consume: bool = False,
) -> tuple[bool, str, str]:
    state = _AUTONOMY_EXECUTION.get()
    if state is None:
        return True, "", ""
    from .core.autonomy_policy import autonomy_action_decision

    allowed, risk, reason = autonomy_action_decision(func, args)
    authorized = risk == "R0"
    if not authorized and callable(state.get("action_authorizer")):
        try:
            authorized = bool(state["action_authorizer"](func, args or {}, consume))
        except Exception:
            logger.warning("自主动作授权检查失败 tool=%s", func, exc_info=True)
            authorized = False
    if authorized:
        if consume and risk != "R0":
            state["authorized_actions"].append({"tool": str(func), "risk": risk, "args": dict(args or {})})
        return True, risk, ""
    if not allowed:
        if risk != "R0":
            reason = (
                "该单次动作授权已失效或被并发消费，操作未执行。"
                if consume
                else f"{func} 属于 {risk}；需要与工具名及参数完全匹配的一次性授权，目标批准不包含此动作授权。"
            )
        state["violated"] = True
        state["blocked_tool"] = str(func or "")
        state["args"] = dict(args or {}) if isinstance(args, dict) else {}
        state["risk"] = risk
        state["reason"] = reason
    return allowed, risk, reason


def autonomy_policy_violated() -> dict | None:
    state = _AUTONOMY_EXECUTION.get()
    return state if state and state.get("violated") else None


def mark_autonomy_policy_violation(func: str, args: dict | None, risk: str, reason: str) -> None:
    state = _AUTONOMY_EXECUTION.get()
    if state is None:
        return
    state.update({
        "violated": True,
        "blocked_tool": str(func or ""),
        "args": dict(args or {}) if isinstance(args, dict) else {},
        "risk": str(risk or "R2"),
        "reason": str(reason or "autonomy action blocked"),
    })
# TOOL_WORKER_V2_1_4_5
# Live whitelist is empty by design (2026-11-02 probe): the five
# read-only tools never hung in-process, worker costs ~1s and
# skips task_history logging, so no tool goes through worker live.
# The gate below is kept for a future truly-hanging tool only.
_WORKER_WHITELIST = frozenset()


STATUS_SUCCESS = "success"
STATUS_INVALID_FORMAT = "invalid_format"
STATUS_FUNC_NOT_FOUND = "func_not_found"
STATUS_PHYSICAL_BLOCKED = "physical_blocked"
STATUS_EXECUTING = "executing"


@dataclass
class R1Response:
    status: str
    func: str = ""
    args: dict = field(default_factory=dict)
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "func": self.func,
            "args": self.args,
            "data": self.data,
        }


def _invalid(func: str = "", args: dict | None = None) -> R1Response:
    return R1Response(
        status=STATUS_INVALID_FORMAT,
        func=func,
        args=args or {},
    )


def parse_call(payload: dict | str | None) -> tuple[str, dict] | None:
    """解析结构化调用；格式不对返回 None。"""
    data = parse_json_dict(payload) if not isinstance(payload, dict) else payload
    if not isinstance(data, dict):
        return None
    func = data.get("func")
    if not isinstance(func, str) or not func.strip():
        return None
    args = data.get("args", {})
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return None
    return func.strip(), dict(args)


def execute_call(
    payload: dict | str | None,
    ctx: ToolContext,
    *,
    mcp_bridge: Any | None = None,
    mcp_call: Callable[[str, dict, float], tuple[bool, str]] | None = None,
    mcp_timeout: float = 90.0,
    cancel_check: Callable[[], bool] | None = None,
) -> R1Response:
    """执行结构化函数调用，永不抛异常、不生成用户可见文案。"""
    parsed = parse_call(payload)
    if parsed is None:
        return _invalid()

    func, args = parsed

    autonomy_allowed, autonomy_risk, autonomy_reason = autonomous_tool_decision(func, args, consume=False)
    if not autonomy_allowed:
        return R1Response(
            status=STATUS_PHYSICAL_BLOCKED,
            func=func,
            args=args,
            data={"reason": "autonomy_risk_blocked", "risk": autonomy_risk, "text": autonomy_reason},
        )

    autonomy_state = _AUTONOMY_EXECUTION.get()
    if autonomy_state is not None:
        from .safety.guard import check_tool_call

        autonomy_pre_authorized = autonomy_risk != "R0"
        safe, safety_reason = check_tool_call(
            func,
            args,
            user_text=str(autonomy_state.get("goal_text") or ""),
            pre_authorized=autonomy_pre_authorized,
            pre_authorized_reason="exact one-use goal action authorization" if autonomy_pre_authorized else "",
        )
        if not safe:
            mark_autonomy_policy_violation(func, args, autonomy_risk, safety_reason)
            return R1Response(
                status=STATUS_PHYSICAL_BLOCKED,
                func=func,
                args=args,
                data={"reason": "autonomy_safety_guard", "risk": autonomy_risk, "text": safety_reason},
            )

    if cancel_check and cancel_check():
        return R1Response(
            status=STATUS_PHYSICAL_BLOCKED,
            func=func,
            args=args,
            data={"reason": "cancelled"},
        )

    with domain_lock(func) as (domain, acquired):
        if not acquired:
            return R1Response(
                status=STATUS_EXECUTING,
                func=func,
                args=args,
                data={"domain": domain},
            )

        if cancel_check and cancel_check():
            return R1Response(
                status=STATUS_PHYSICAL_BLOCKED,
                func=func,
                args=args,
                data={"reason": "cancelled"},
            )

        if not is_tool_enabled(func, ctx.tools_cfg) and not func.startswith("mcp__"):
            return R1Response(
                status=STATUS_FUNC_NOT_FOUND,
                func=func,
                args=args,
                data={"reason": "disabled"},
            )

        if not func.startswith("mcp__") and not _reserve_tool_call():
            return R1Response(
                status=STATUS_PHYSICAL_BLOCKED,
                func=func,
                args=args,
                data={"reason": "autonomy_tool_call_budget_exhausted"},
            )

        if func.startswith("mcp__"):
            if not mcp_bridge or not mcp_bridge.has(func):
                return R1Response(
                    status=STATUS_FUNC_NOT_FOUND,
                    func=func,
                    args=args,
                )
            if not _reserve_tool_call():
                return R1Response(
                    status=STATUS_PHYSICAL_BLOCKED,
                    func=func,
                    args=args,
                    data={"reason": "autonomy_tool_call_budget_exhausted"},
                )
            if autonomy_state is not None and autonomy_pre_authorized:
                if not autonomous_tool_decision(func, args, consume=True)[0]:
                    blocked = autonomy_policy_violated() or {}
                    reason = str(blocked.get("reason") or "one-use action authorization was already consumed")
                    return R1Response(status=STATUS_PHYSICAL_BLOCKED, func=func, args=args,
                                      data={"reason": "autonomy_action_authorization_consumed", "risk": autonomy_risk, "text": reason})
            try:
                detail = None
                if mcp_call is not None:
                    ok, text = mcp_call(func, args, mcp_timeout)
                else:
                    call_detail = getattr(mcp_bridge, "call_detail", None)
                    if call_detail is not None:
                        detail = call_detail(func, args, timeout=mcp_timeout)
                        ok, text = detail.ok, detail.text
                    else:
                        ok, text = mcp_bridge.call(func, args)
            except Exception as e:  # noqa: BLE001
                logger.exception("R1 MCP 执行异常 %s: %s", func, e)
                return R1Response(
                    status=STATUS_PHYSICAL_BLOCKED,
                    func=func,
                    args=args,
                    data={"text": str(e), "reason": "mcp_error"},
                )
            status = STATUS_SUCCESS if ok else STATUS_PHYSICAL_BLOCKED
            data: dict = {"ok": ok, "text": text or ""}
            if detail is not None:
                # isError / structuredContent 只进结构化数据，不进 LLM 文本
                if detail.error_code:
                    data["reason"] = detail.error_code
                if detail.structured_content is not None:
                    data["structured_content"] = detail.structured_content
            return R1Response(
                status=status,
                func=func,
                args=args,
                data=data,
            )

        if autonomy_state is not None and autonomy_pre_authorized:
            if not autonomous_tool_decision(func, args, consume=True)[0]:
                blocked = autonomy_policy_violated() or {}
                reason = str(blocked.get("reason") or "one-use action authorization was already consumed")
                return R1Response(
                    status=STATUS_PHYSICAL_BLOCKED, func=func, args=args,
                    data={"reason": "autonomy_action_authorization_consumed", "risk": autonomy_risk, "text": reason},
                )

        # TOOL_WORKER_V2_1_4_5: whitelist tools run in a per-call worker.
        # domain_lock is held by the enclosing with-block until it exits,
        # so the lock is released only after the worker is confirmed dead.
        # Timeout/cancel returns a ToolOutput (no fallback); worker
        # infrastructure failure returns None and falls back to guarded.
        _worker_output = None
        if func in _WORKER_WHITELIST:
            try:
                from .tool_worker import run_tool_in_worker

                _worker_output = run_tool_in_worker(
                    func, args, ctx, cancel_check=cancel_check,
                    ctx_module=type(ctx).__module__,
                    tool_module=execute_tool_guarded.__module__,
                )
            except Exception:
                logger.exception("4-5 worker 分流异常 %s，fallback 到进程内执行", func)
                _worker_output = None
        if _worker_output is not None:
            output = _worker_output
        else:
            try:
                output: ToolOutput = execute_tool_guarded(
                    func, args, ctx, cancel_check=cancel_check
                )
            except Exception as e:  # noqa: BLE001
                logger.exception("R1 工具执行异常 %s: %s", func, e)
                return R1Response(
                    status=STATUS_PHYSICAL_BLOCKED,
                    func=func,
                    args=args,
                    data={"text": str(e), "reason": "exception"},
                )

        if cancel_check and cancel_check():
            return R1Response(
                status=STATUS_PHYSICAL_BLOCKED,
                func=func,
                args=args,
                data={"text": "操作已取消。", "reason": "cancelled"},
            )

        text = (output.text or "").strip()
        if (
            not output.ok
            and ("未知工具" in text or "尚未实现" in text)
        ):
            return R1Response(
                status=STATUS_FUNC_NOT_FOUND,
                func=func,
                args=args,
                data={"text": text},
            )

        status = STATUS_SUCCESS if output.ok else STATUS_PHYSICAL_BLOCKED
        data: dict[str, Any] = {"ok": output.ok, "text": text}
        if output.inject_messages:
            data["inject_messages"] = output.inject_messages

        return R1Response(
            status=status,
            func=func,
            args=args,
            data=data,
        )


from .agents.windows.verifier import VERIFY_FUNCS as _VERIFY_FUNCS


def r1_to_tool_output(resp: R1Response) -> ToolOutput:
    """供 Agent Loop 将 R1 响应转为 ToolOutput 兼容结构。"""
    ok = resp.status == STATUS_SUCCESS
    if ok and resp.func in _VERIFY_FUNCS:
        # 仅显式 verified=False（物理复核失败）才降级；
        # 键缺失(未复核的直连执行)不视为失败，避免对 execute_call 直连路径误杀成功操作
        if resp.data.get("verified") is False:
            ok = False
    text = str(resp.data.get("text") or "")
    inject = resp.data.get("inject_messages") or []
    if isinstance(inject, list):
        return ToolOutput(ok, text, inject_messages=inject)
    return ToolOutput(ok, text)
