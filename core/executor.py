"""统一执行器 — 调用工具，不判断任务是否完成。"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

from ..models.action import Action
from ..models.result import RuntimeExecutionResult
from ..r1_executor import (
    R1Response,
    STATUS_PHYSICAL_BLOCKED,
    STATUS_SUCCESS,
    execute_call,
)
from ..tools.execution_result import ExecutionResult, from_tuple
from ..tools.executor import ToolContext
from .execution_result import from_r1_response
from .idempotency import should_guard

logger = logging.getLogger(__name__)


class Executor:
  """执行 Action，返回 RuntimeExecutionResult。"""

  def __init__(
    self,
    ctx: ToolContext,
    *,
    execute_call_fn: Callable[..., R1Response] | None = None,
    mcp_bridge: Any | None = None,
    mcp_call: Callable | None = None,
    agent_runner: Any | None = None,
    cancel_check: Callable[[], bool] | None = None,
  ) -> None:
    self.ctx = ctx
    self._execute_call = execute_call_fn or execute_call
    self.mcp_bridge = mcp_bridge
    self.mcp_call = mcp_call
    self.agent_runner = agent_runner
    self.cancel_check = cancel_check
    self.on_chunk: Callable[[str], None] | None = None
    self.on_start: Callable[[], None] | None = None
    # WATCHDOG_V1: on_tool_event passthrough
    self.on_tool_event: Callable[[str, bool, str], None] | None = None
    self.goal_controller: Any | None = None  # GOAL_CTRL_V1

  def set_cancel_check(self, fn: Callable[[], bool] | None) -> None:
    self.cancel_check = fn

  def set_stream_callbacks(
    self,
    on_chunk: Callable[[str], None] | None = None,
    on_start: Callable[[], None] | None = None,
  ) -> None:
    self.on_chunk = on_chunk
    self.on_start = on_start

  def set_on_tool_event(
    self,
    fn: Callable[[str, bool, str], None] | None,
  ) -> None:
    self.on_tool_event = fn

  def set_goal_controller(self, controller: Any | None) -> None:  # GOAL_CTRL_V1
    self.goal_controller = controller

  def execute(self, action: Action) -> RuntimeExecutionResult:
    started = time.monotonic()
    if self.cancel_check and self.cancel_check():
      return RuntimeExecutionResult(
        success=False,
        status="cancelled",
        message="cancelled before execution",
        error="cancelled",
        verification_required=False,
        evidence={"verified": False, "cancelled": True},
      )

    try:
      if action.tool == "agent_loop":
        result = self._execute_agent_loop(action)
      else:
        result = self._execute_tool(action)
    except TimeoutError:
      return RuntimeExecutionResult.timeout()
    except Exception as exc:  # noqa: BLE001
      logger.exception("Executor 异常: %s", action.tool)
      return RuntimeExecutionResult.from_error(str(exc))

    elapsed = int((time.monotonic() - started) * 1000)
    result.duration_ms = elapsed
    return result

  def _execute_tool(self, action: Action) -> RuntimeExecutionResult:
    # 快路径直接工具也需起始反馈（agent_loop 的 on_start 由 runner 自行触发）
    if self.on_start:
      try:
        self.on_start()
      except Exception:  # noqa: BLE001
        logger.debug("on_start 回调异常", exc_info=True)
    r1 = self._execute_call(
      {"func": action.tool, "args": action.parameters or {}},
      self.ctx,
      mcp_bridge=self.mcp_bridge,
      mcp_call=self.mcp_call,
      cancel_check=self.cancel_check,
    )
    tool_result = from_r1_response(r1)
    return RuntimeExecutionResult(
      success=tool_result.success,
      status=r1.status,
      message=tool_result.message,
      error=tool_result.error or None,
      verification_required=True,
      evidence={
        "verified": tool_result.verified,
        "r1_status": r1.status,
        "func": action.tool,
        "args": dict(action.parameters or {}),
        **tool_result.extra,
      },
    )

  def _execute_agent_loop(self, action: Action) -> RuntimeExecutionResult:
    if not self.agent_runner:
      return RuntimeExecutionResult.from_error("agent_runner not configured")

    params = action.parameters or {}
    text = str(params.get("text") or "").strip()
    if not text:
      return RuntimeExecutionResult.from_error("empty agent_loop text")

    reply, layer, tool_results = self.agent_runner.run(
      text,
      agent_id=str(params.get("agent_id") or "keks"),
      session_id=str(params.get("session_id") or "default"),
      on_chunk=self.on_chunk,
      on_start=self.on_start,
      on_tool_event=self.on_tool_event,
      cancel_check=self.cancel_check,
      memory_text=str(params.get("memory_text") or text),
      execution_policy_context=str(params.get("execution_policy_context") or ""),
      goal_controller=self.goal_controller,  # GOAL_CTRL_V1
      max_tool_calls=(int(params["max_tool_calls"]) if params.get("max_tool_calls") is not None else None),
      tool_call_budget_state=(params.get("_tool_call_budget_state") if isinstance(params.get("_tool_call_budget_state"), dict) else None),
    )

    # agent_loop 整体成功判定：
    # - 调了工具：reply 非空 且 至少一个工具成功（辅助操作如看剧前静音 control_music、
    #   selfcheck 探测失败不应拖垮主任务；LLM 多轮重试只要最终有工具成功即算完成）
    # - 没调工具（纯对话）：reply 非空即成功
    any_ok = any(bool(t.get("ok")) for t in (tool_results or []))
    # CANCEL_TOKEN_V1: cancelled loop must not be read as success.
    _cancelled_loop = layer == "cancelled" or (self.cancel_check and self.cancel_check())
    # Q2_TERMINAL_V1: degraded / exhausted loop must not be read as success,
    # even if reply is non-empty or some tool succeeded.
    _degraded_loop = layer in ("loop_degraded", "loop_exhausted", "loop_goal_locked", "loop_stalled")  # GOAL_CTRL_V1
    ok = (not _cancelled_loop) and (not _degraded_loop) and bool(reply) and (any_ok or not tool_results)
    return RuntimeExecutionResult(
      success=ok,
      status="cancelled" if _cancelled_loop else (STATUS_SUCCESS if ok else "error"),
      message=reply or "",
      data={"layer": layer, "tool_results": tool_results or [], "cancelled": _cancelled_loop},
      verification_required=False,
      evidence={
        "verified": ok,
        "layer": layer,
        "tool_results": tool_results or [],
        "cancelled": _cancelled_loop,
      },
    )


def execute_pipeline(
  func: str,
  args: dict,
  ctx: ToolContext,
  *,
  execute_call: Callable[..., R1Response] | None = None,
  execute_call_fn: Callable[..., R1Response] | None = None,
  mcp_bridge: Any | None = None,
  cancel_check: Callable[[], bool] | None = None,
  mcp_call: Callable | None = None,
  verifier: Any | None = None,
  idempotency: Any | None = None,
  source: str = "",
) -> tuple[R1Response, list[dict], bool, str]:
  """R1 执行 + Verifier 验证的统一入口（向后兼容）。

  idempotency 传入 IdempotencyManager 时，只对 GUARDED_FUNCS 白名单内的不可逆动作上锁；
  不传则行为与改造前完全一致。
  """
  import time

  from .verifier import Verifier

  fn = execute_call_fn or execute_call
  if fn is None:
    fn = execute_call  # 使用顶部导入的 execute_call
  v = verifier or Verifier()

  idem_key: str | None = None
  if idempotency is not None and should_guard(func):
    allowed, reason, record = idempotency.check_and_acquire(func, args or {})
    if not allowed:
      key = record.key if record else ""
      if reason == "already_completed":
        cached = record.result if isinstance(record.result, dict) else {}
        resp = R1Response(
          status=STATUS_SUCCESS,
          func=func,
          args=args or {},
          data={
            **cached,
            "ok": True,
            "cached": True,
            "verified": True,
            "idempotency_key": key,
            "text": cached.get("text") or "相同动作在有效期内已完成，未重复下发",
          },
        )
        return resp, [{"layer": "idempotency", "reason": reason}], True, "幂等命中"
      resp = R1Response(
        status=STATUS_PHYSICAL_BLOCKED,
        func=func,
        args=args or {},
        data={
          "ok": False,
          "reason": f"idempotency_{reason}",
          "idempotency_key": key,
          "text": (
            "上一次相同动作结果未知，禁止盲目重发，请先查外部状态"
            if reason == "unknown"
            else "相同动作正在执行中，已拦截重复下发"
          ),
        },
      )
      return resp, [{"layer": "idempotency", "reason": reason}], False, "幂等拦截"
    idem_key = record.key if record else None

  started = time.monotonic()
  r1 = fn(
    {"func": func, "args": args or {}},
    ctx,
    mcp_bridge=mcp_bridge,
    mcp_call=mcp_call,
    cancel_check=cancel_check,
  )
  resp, extra, ok, summary = v.verify(func, args or {}, r1, ctx, mcp_bridge=mcp_bridge)
  if idem_key is not None:
    if ok:
      idempotency.mark_completed(idem_key, dict(resp.data or {}))
    else:
      idempotency.mark_failed(idem_key, summary or "")
  hist = getattr(ctx, "execution_history", None)
  if hist is not None:
    verified = resp.data.get("verified")
    hist.record(
      func,
      task_id=str(getattr(ctx, "task_id", None) or ""),
      success=ok,
      status=resp.status,
      verified=bool(verified) if verified is not None else ok,
      error="" if ok else (summary or "")[:300],
      duration_ms=int((time.monotonic() - started) * 1000),
      source=source or "pipeline",
    )
  return resp, extra, ok, summary
