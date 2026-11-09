"""Supervisor — 总控制中心：Plan → Execute → Verify → Recover。"""

from __future__ import annotations

# WATCHDOG_V1: independent watchdog inside Supervisor
import logging
import threading
from datetime import datetime, timedelta
from typing import Any, Callable

from ..models.action import Action
from ..models.evidence import Evidence
from ..models.plan import Plan
from ..models.result import RuntimeExecutionResult
from ..models.task import Task, TaskStatus
from ..trace import current_trace, span as trace_span
from ..r1_executor import (
  autonomy_policy_violated,
  reset_autonomy_execution,
  reset_tool_call_budget,
  set_autonomy_execution,
  set_tool_call_budget,
)
from .cancellation import CancellationToken, cancel_with_processes, merge_cancel_checks
from .event_bus import EventBus
from .executor import Executor
from .goal_controller import GoalController  # GOAL_CTRL_V1
from .metrics import get_runtime_metrics
from .planner import Planner
from .process_registry import set_current_task
from .recovery import Recovery
from .task_store import TaskStore
from .verifier import Verifier

logger = logging.getLogger(__name__)


class ResponsePolicy:
  """根据 Task 最终状态生成用户可见回复。"""

  @staticmethod
  def format(task: Task) -> str:
    if task.reply:
      return task.reply
    if task.status == TaskStatus.SUCCESS:
      return task.result if isinstance(task.result, str) else "已完成。"
    if task.status == TaskStatus.CANCELLED:
      return "任务已取消。"
    if task.status == TaskStatus.TIMEOUT:
      return "任务超时，未能完成。"
    if task.status == TaskStatus.UNKNOWN:
      return "操作已发出，但目前无法确认是否完成。"
    if task.error:
      return f"未能完成：{task.error}"
    return "任务失败。"


def _safe_reason(evidence: Any) -> str:
    """从 Evidence 安全提取 reason 字符串（防御性：reason 偶发为 method 时不崩溃）。"""
    r = getattr(evidence, "reason", "")
    if callable(r):
        logger.warning("evidence.reason 类型异常: %r (Evidence=%r)", type(r), type(evidence))
        return ""
    return str(r or "")


def _safe_str(value: Any) -> str:
    """任意值安全转 str；method/None 返回空串。

    Evidence.reason / Result.error / Result.message / Task.error 均偶发为 method
    类型（疑似 dataclass 字段被赋值时拿到的 bound method），直接写 SQLite 会抛
    "type 'method' is not supported"。统一在此处兜底。
    """
    if value is None:
        return ""
    if callable(value):
        logger.warning("_safe_str: 值为 method 类型，已丢弃: %r", type(value))
        return ""
    try:
        return str(value)
    except Exception:
        return ""


class Supervisor:
  """v2.0 总调度：Task → Planner → Executor → Verifier → Result。"""

  def __init__(
    self,
    *,
    planner: Planner,
    executor: Executor,
    verifier: Verifier,
    recovery: Recovery,
    task_store: TaskStore | None = None,
    event_bus: EventBus | None = None,
    task_timeout_sec: float = 300.0,
    windows_agent: Any | None = None,
    metrics: Any | None = None,
  ) -> None:
    self.planner = planner
    self.executor = executor
    self.verifier = verifier
    self.recovery = recovery
    self.task_store = task_store
    self.event_bus = event_bus or EventBus()
    self.task_timeout_sec = max(30.0, float(task_timeout_sec))
    self.windows_agent = windows_agent
    self.metrics = metrics or get_runtime_metrics()
    self._cancel_tokens: dict[str, CancellationToken] = {}
    # CANCEL_REPORT_V1_4_2: last cancel report per task_id, for observability.
    self._cancel_reports: dict[str, dict] = {}
    # WATCHDOG_V1: in-memory watchdog state (no TaskStore scan)
    self._watchdog_poll_sec = 2.0
    self._watchdog_first_grace_sec = 10.0
    self._watchdog_refresh_sec = 30.0
    self._watchdog_max_refreshes = 2
    self._watchdog_tasks: dict[str, dict] = {}
    self._watchdog_lock = threading.Lock()
    self._watchdog_local = threading.local()
    self._watchdog_stop = threading.Event()
    self._watchdog_thread: threading.Thread | None = None
    self._ensure_watchdog()

  def run(
    self,
    task: Task,
    *,
    on_chunk: Callable[[str], None] | None = None,
    on_start: Callable[[], None] | None = None,
    on_tool_event: Callable[[str, bool, str], None] | None = None,
    on_tool_start: Callable[[str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    plan_override: Plan | None = None,  # Q3_TASK_RESUME_V1
    max_tool_calls: int | None = None,
    autonomous: bool = False,
    action_authorizer: Callable[[str, dict, bool], bool] | None = None,
  ) -> Task:
    token = CancellationToken()
    tool_call_budget_state = None
    self._cancel_tokens[task.id] = token
    merged_cancel = merge_cancel_checks(cancel_check, token.as_check())
    self.executor.set_cancel_check(merged_cancel)
    self.executor.set_stream_callbacks(on_chunk=on_chunk, on_start=on_start)
    # WATCHDOG_V1: internal tool-count callback only; user on_tool_event
    # stays as the per-action callback after each action, untouched.
    _watchdog_setter = getattr(self.executor, "set_on_tool_event", None)
    if callable(_watchdog_setter):
      _watchdog_setter(self._watchdog_tool_event)
    self._watchdog_local.task_id = task.id
    set_current_task(task.id)
    if hasattr(self.executor, "ctx"):
      self.executor.ctx.cancel_check = merged_cancel
      self.executor.ctx.task_id = task.id

    # === 关联 task_id 到当前 trace（Global Trace 系统）===
    cur_trace = current_trace()
    if cur_trace is not None and not cur_trace.task_id:
      cur_trace.task_id = task.id
      # 立即落库，避免请求进行中 task_id 一直为空（进程崩溃则永远丢失关联）
      if cur_trace._store is not None:
        cur_trace._store.link_task_id(cur_trace.trace_id, task.id)

    try:
      if not task.deadline_at:
        task.deadline_at = (
          datetime.now() + timedelta(seconds=self.task_timeout_sec)
        ).isoformat()
      # WATCHDOG_V1: register in-memory task for watchdog polling.
      self._watchdog_register(task)

      if self._cancelled(merged_cancel):
        return self._finish(task, TaskStatus.CANCELLED)

      # Q3_TASK_RESUME_V1: resume with remaining plan, do NOT re-plan.
      # plan_override comes from recover_incomplete_tasks (remaining
      # actions only); re-planning here would discard the remaining
      # filter and re-run completed actions.
      if plan_override is not None:
        plan = plan_override
      else:
        self._set_status(task, TaskStatus.PLANNING)
        with trace_span("planner.create", task_id=task.id) as s:
          plan = self.planner.create(task)
          s.set_attribute("actions_count", len(plan.actions))
          s.set_attribute("plan_source", plan.source)
      if max_tool_calls is not None:
        limit = max(0, int(max_tool_calls))
        direct_actions = [action for action in plan.actions if action.tool != "agent_loop"]
        if len(direct_actions) > limit:
          return self._finish(
            task, TaskStatus.FAILED,
            error=f"autonomy tool-call budget exceeded: plan has {len(direct_actions)} direct actions, limit {limit}",
          )
        shared_budget = {
          "limit": limit,
          "used": 0,
          "exceeded": False,
        }
        tool_call_budget_state = shared_budget
        for action in plan.actions:
          if action.tool == "agent_loop":
            action.parameters = dict(action.parameters or {})
            action.parameters["max_tool_calls"] = shared_budget["limit"]
      self._persist(task, plan)

      if self._cancelled(merged_cancel):
        return self._finish(task, TaskStatus.CANCELLED)

      self._set_status(task, TaskStatus.READY)
      self.event_bus.publish("task.ready", {"task_id": task.id, "plan_id": plan.id})

      # GOAL_CTRL_V1: Supervisor owns goal control (achieved/deviation/
      # runaway). Threaded to the loop via the executor setter, following
      # the watchdog setter pattern; cleared in finally.
      _goal_controller = GoalController(task, plan, self.verifier, self.executor.ctx)
      _goal_setter = getattr(self.executor, "set_goal_controller", None)
      if callable(_goal_setter):
        _goal_setter(_goal_controller)

      for action in plan.actions:
        if self._cancelled(merged_cancel):
          return self._finish(task, TaskStatus.CANCELLED)
        if self._timed_out(task):
          return self._finish(task, TaskStatus.TIMEOUT, error="task deadline exceeded")

        self._set_status(task, TaskStatus.EXECUTING)
        if self.task_store:
          self.task_store.add_step(task.id, action, status="executing")

        if action.tool == "agent_loop" and tool_call_budget_state is not None:
          action.parameters = dict(action.parameters or {})
          action.parameters["_tool_call_budget_state"] = tool_call_budget_state

        if on_tool_start:
          on_tool_start(action.tool)

        with trace_span(
          f"executor.execute:{action.tool}",
          tool=action.tool,
          task_id=task.id,
        ) as s:
          if tool_call_budget_state is not None:
            action.parameters = dict(action.parameters or {})
            action.parameters["_tool_call_budget_state"] = tool_call_budget_state
            _budget_token = set_tool_call_budget(
              tool_call_budget_state["limit"], tool_call_budget_state
            )
          else:
            _budget_token = None
          _autonomy_token = set_autonomy_execution(
            autonomous, action_authorizer=action_authorizer, goal_text=task.goal
          )
          _autonomy_violation = None
          try:
            result = self.executor.execute(action)
            _autonomy_violation = autonomy_policy_violated()
          finally:
            reset_autonomy_execution(_autonomy_token)
            if _budget_token is not None:
              reset_tool_call_budget(_budget_token)
          if _autonomy_violation:
            _autonomy_reason = str(
              _autonomy_violation.get("reason") or "autonomy action blocked by risk policy"
            )
            _blocked_tool = str(_autonomy_violation.get("blocked_tool") or action.tool)
            _blocked_args = _autonomy_violation.get("args")
            if not isinstance(_blocked_args, dict):
              _blocked_args = {}
            task.tool_results.append({
              "name": _blocked_tool,
              "func": _blocked_tool,
              "args": _blocked_args,
              "ok": False,
              "failure_class": "authorization",
              "autonomy_risk": str(_autonomy_violation.get("risk") or "R2"),
              "summary": _autonomy_reason[:200],
            })
            if self.task_store:
              self.task_store.update_step(
                action.id,
                status="failed",
                result={
                  "autonomy_risk": str(_autonomy_violation.get("risk") or "R2"),
                  "blocked_tool": _blocked_tool,
                },
                error=_autonomy_reason,
              )
            return self._finish(
              task, TaskStatus.FAILED,
              error=_autonomy_reason,
            )
          s.set_attribute("success", bool(result.success))
          s.set_attribute("duration_ms", result.duration_ms)
          s.set_attribute("status", result.status)
          if not result.success:
            s.set_error(_safe_str(result.error) or "execution failed")

        # CANCEL_TOKEN_V1: after-execute recheck — cancelled during execute
        # must map to CANCELLED, not SUCCESS via verifier or FAILED via recovery.
        _cancelled_now = self._cancelled(merged_cancel)
        _cancel_layer = None
        if isinstance(getattr(result, "data", None), dict):
          _cancel_layer = result.data.get("layer")
        if _cancelled_now or _cancel_layer == "cancelled":
          if self.task_store:
            self.task_store.update_step(
              action.id,
              status="cancelled",
              error="cancelled during execution",
            )
          if action.tool == "agent_loop":
            self._merge_agent_loop_result(task, action, result)
          return self._finish(task, TaskStatus.CANCELLED, error="cancelled during execution")
        # GOAL_CTRL_V1: controller verdicts bypass per-action verify/recovery.
        # Achieved breaks to final verify_goal; locked/stalled finish FAILED.
        _ctrl_layer = result.data.get("layer") if isinstance(getattr(result, "data", None), dict) else None
        if action.tool == "agent_loop" and _ctrl_layer == "loop_goal_achieved":
          self._merge_agent_loop_result(task, action, result)
          self._persist(task, plan)
          break
        if action.tool == "agent_loop" and _ctrl_layer in ("loop_goal_locked", "loop_stalled"):
          self._merge_agent_loop_result(task, action, result)
          self._persist(task, plan)
          return self._finish(
            task,
            TaskStatus.FAILED,
            error=f"{_ctrl_layer}: {_safe_str(result.message)}",
          )
        hist = getattr(self.executor.ctx, "execution_history", None)
        if hist is not None:
            hist.record(
                action.tool,
                task_id=task.id,
                success=result.success,
                status=result.status,
                verified=bool(result.evidence.get("verified")),
                error=_safe_str(result.error),
                duration_ms=result.duration_ms,
                source="supervisor",
            )

        self._set_status(task, TaskStatus.VERIFYING)
        with trace_span(
          f"verifier.verify:{action.tool}",
          tool=action.tool,
        ) as s:
          evidence = self.verifier.verify_action(
            action,
            result,
            self.executor.ctx,
            mcp_bridge=self.executor.mcp_bridge,
          )
          s.set_attribute("verified", bool(evidence.verified))
          s.set_attribute("reason", _safe_reason(evidence)[:200])
          if not evidence.verified:
            s.set_error(_safe_reason(evidence))

        self.metrics.record_tool(
          tool=action.tool,
          success=bool(result.success and evidence.verified),
          verified=bool(evidence.verified),
          claimed_success=bool(result.success),
        )
        task.tool_results.append({
          "name": action.tool,
          "func": action.tool,
          "args": dict(action.parameters or {}),
          "ok": bool(result.success and evidence.verified),
          "execution_ok": bool(result.success),
          "verified": bool(evidence.verified),
          "summary": _safe_str(_safe_reason(evidence) or result.message)[:240],
        })
        logger.info(
          "task.action task_id=%s tool=%s success=%s verified=%s "
          "duration_ms=%s reason=%s",
          task.id,
          action.tool,
          result.success,
          evidence.verified,
          result.duration_ms,
          _safe_reason(evidence)[:80],
        )

        if on_tool_event:
          on_tool_event(action.tool, evidence.verified, _safe_reason(evidence) or result.message)

        if evidence.verified and result.can_report_success():
          if self.task_store:
            self.task_store.update_step(
              action.id,
              status="success",
              result={"message": result.message},
            )
          self._merge_agent_loop_result(task, action, result)
          # Q3_TASK_RESUME_V1: per-action persist of task aggregation
          # (reply/layer/tool_results/result), not only at _finish.
          # Without this, a crash after a successful action loses
          # the task-level output even though the step is marked
          # success, and resume skipping that step would lack its output.
          self._persist(task, plan)
          continue

        self._set_status(task, TaskStatus.RECOVERING)
        with trace_span(
          f"recovery.try_recover:{action.tool}",
          tool=action.tool,
        ) as s:
          # UNKNOWN_GATE_V2_4_3: read-only check callback for unknown gate in recovery.
          # Only returns "no_effect" when current state positively shows no effect;
          # otherwise "effect" or "unavailable", in which case recovery will not retry.
          def _read_only_check(_action) -> str:
            try:
              _verifier = None
              if getattr(self, "windows_agent", None) is not None:
                _verifier = getattr(self.windows_agent, "verifier", None)
              if _verifier is None and getattr(self.executor, "ctx", None) is not None:
                _wa = getattr(self.executor.ctx, "windows_agent", None)
                if _wa is not None:
                  _verifier = getattr(_wa, "verifier", None)
              if _verifier is None or not hasattr(_verifier, "check_no_side_effect"):
                return "unavailable"
              return str(_verifier.check_no_side_effect(_action.tool, dict(_action.parameters or {})))
            except Exception:
              logger.debug("Supervisor read-only check failed", exc_info=True)
              return "unavailable"

          recovered = self.recovery.try_recover(
            task,
            action,
            result,
            evidence,
            retry_fn=self.executor.execute,
            read_only_check=_read_only_check,
          )
          s.set_attribute("recovered", bool(recovered))
          if not recovered:
            s.set_error(_safe_str(task.error) or _safe_reason(evidence))
        self.metrics.record_recovery(success=recovered)
        if recovered:
          if self.task_store:
            self.task_store.update_step(
              action.id,
              status="success",
              result={"message": "recovered"},
            )
          self._persist(task, plan)
        if not recovered:
          # agent_loop 已给出含真实原因的回复时（如「点歌没成功：页面脚本执行超时」），
          # 优先用它作为用户可见回复，不套「未能完成：」前缀；task.error 仍记录内部原因
          if action.tool == "agent_loop" and result.message:
            task.reply = _safe_str(result.message)
          # Q2_TERMINAL_V1: 失败路径也要把 layer/tool_results 抄到 Task，
          # 否则降级/耗尽落 FAILED 后 task.layer 还是上一个动作的，查不到原因。
          # error 前缀带 layer 码，便于按码区分 degraded / exhausted。
          if action.tool == "agent_loop":
            self._merge_agent_loop_result(task, action, result)
            _q2_layer = str(task.layer or "")
            if _q2_layer in ("loop_degraded", "loop_exhausted"):
              _q2_err = _safe_str(task.error) or _safe_reason(evidence)
              task.error = f"{_q2_layer}: {_q2_err}" if _q2_err else _q2_layer
          # UNKNOWN_GATE_V2_4_3: timeout/unknown result status also maps to UNKNOWN,
          # not just evidence.observed_state, so a timeout after the recovery gate
          # is reported as unknown, not generic failure.
          _is_unknown_result = (
            evidence.observed_state.get("state") == "unknown"
            or getattr(result, "status", "") in ("timeout", "unknown")
            or (isinstance(getattr(result, "evidence", None), dict) and result.evidence.get("unknown"))
          )
          status = TaskStatus.UNKNOWN if _is_unknown_result else TaskStatus.FAILED
          if self.task_store:
            self.task_store.update_step(
              action.id,
              status=status.value,
              error=_safe_str(task.error) or _safe_reason(evidence),
            )
          return self._finish(task, status, error=_safe_str(task.error) or _safe_reason(evidence), evidence=evidence)

      # SUPERVISOR_PASSIVE_TIMEOUT_V1: final timeout check before SUCCESS, a last action overrun must not become SUCCESS
      if self._timed_out(task):
        return self._finish(task, TaskStatus.TIMEOUT, error="task deadline exceeded")
      # Q4_TASK_GOAL_VERIFY_V1: final SUCCESS must pass independent goal verification.
      # Per-action verified results are evidence only, not the final verdict.
      # verify_goal checks plan criteria, rule mapping fallback, and loop claims
      # independently; unverifiable -> UNKNOWN, never old SUCCESS.
      _goal_verify = getattr(self.verifier, "verify_goal", None)
      if _goal_verify is None:
        return self._finish(task, TaskStatus.UNKNOWN, error="verifier has no verify_goal")
      _goal_evidence = _goal_verify(task, plan, self.executor.ctx, mcp_bridge=self.executor.mcp_bridge)
      if _goal_evidence.verified:
        return self._finish(task, TaskStatus.SUCCESS, evidence=_goal_evidence)
      if _goal_evidence.observed_state.get("state") == "unknown":
        return self._finish(task, TaskStatus.UNKNOWN, error=_safe_str(_goal_evidence.reason) or "goal verification unknown", evidence=_goal_evidence)
      return self._finish(task, TaskStatus.FAILED, error=_safe_str(_goal_evidence.reason) or "goal verification failed", evidence=_goal_evidence)
    finally:
      set_current_task(None)
      self.executor.set_stream_callbacks(None, None)
      if hasattr(self.executor, "set_goal_controller"):  # GOAL_CTRL_V1
        self.executor.set_goal_controller(None)
      # WATCHDOG_V1: unregister is idempotent; _finish already did it
      # for the normal path, this only covers exits without _finish.
      self._watchdog_unregister(task.id)
      if getattr(self._watchdog_local, "task_id", None) == task.id:
        self._watchdog_local.task_id = None
      if hasattr(self.executor, "ctx"):
        self.executor.ctx.cancel_check = None
        self.executor.ctx.task_id = None

  def handle_legacy(
    self,
    text: str,
    *,
    agent_id: str = "keks",
    session_id: str = "default",
    on_chunk: Callable[[str], None] | None = None,
    on_start: Callable[[], None] | None = None,
    on_tool_event: Callable[[str, bool, str], None] | None = None,
    on_tool_start: Callable[[str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    memory_text: str | None = None,
  ) -> tuple[str | None, str, list[dict], Task | None]:
    """兼容旧 supervisor.handle 签名。"""
    goal = (text or "").strip()
    if not goal:
      return None, "empty", [], None

    task = Task(
      goal=goal,
      agent_id=agent_id,
      session_id=session_id,
      memory_text=memory_text,
    )
    if self.task_store:
      self.task_store.save(task)

    # 统计执行期是否流式播报过：agent_loop / fastpath 等路径在执行期通过
    # executor 回调流式输出；普通工具步骤（open_app/close_app 等）从不流式。
    streamed = {"count": 0}
    if on_chunk is not None:
      _orig_on_chunk = on_chunk

      def on_chunk(chunk: str) -> None:
        streamed["count"] += 1
        _orig_on_chunk(chunk)

    task = self.run(
      task,
      on_chunk=on_chunk,
      on_start=on_start,
      on_tool_event=on_tool_event,
      on_tool_start=on_tool_start,
      cancel_check=cancel_check,
    )

    layer = task.layer or "runtime"
    reply = ResponsePolicy.format(task)

    # 语音入口只播报 on_chunk 流式内容。runtime 层普通工具步骤全程无流式
    # 文本，最终回复（含成功默认「已完成」）若不在此补发，收工就静默——
    # 「关闭记事本完成后没有播报」的根因（2026-09-12）。
    # 执行期已流式过（计数 > 0）则不重复播报。
    if reply and on_chunk is not None and streamed["count"] == 0:
      try:
        on_chunk(reply)
      except Exception:
        logger.warning("补发最终回复 on_chunk 失败", exc_info=True)

    return reply, layer, list(task.tool_results or []), task

  def cancel(self, task_id: str) -> bool:
    # CANCEL_REPORT_V1_4_2: keep bool return for existing callers; full report
    # is available via cancel_with_report(). Running tasks are NOT written
    # to CANCELLED here — _finish (with its cancel guard) is the only writer
    # of final status once run() converges.
    report = self.cancel_with_report(task_id)
    return bool(report["cancelled"])

  def cancel_with_report(self, task_id: str) -> dict:
    token = self._cancel_tokens.get(task_id)
    killed = cancel_with_processes(task_id, token)
    running = token is not None
    report: dict = {
      "task_id": task_id,
      "killed": int(killed or 0),
      "token_present": running,
      "running": running,
      "cancelled": False,
      "final_status_written": False,
    }
    if running:
      # Task is still in run(): do NOT write CANCELLED to the store here.
      # run() will converge to CANCELLED via its cancel checks / _finish guard.
      report["cancelled"] = True
    elif self.task_store:
      task = self.task_store.get_task(task_id)
      if task and not task.is_terminal():
        self._finish(task, TaskStatus.CANCELLED, error="cancelled")
        report["cancelled"] = True
        report["final_status_written"] = True
      elif task and task.is_terminal():
        report["cancelled"] = task.status == TaskStatus.CANCELLED
    else:
      report["cancelled"] = report["killed"] > 0
    self._cancel_reports[task_id] = report
    logger.info(
      "task.cancel task_id=%s killed=%s running=%s cancelled=%s",
      task_id,
      report["killed"],
      running,
      report["cancelled"],
    )
    self.event_bus.publish(
      "task.cancel_requested",
      {"task_id": task_id, "killed": report["killed"], "running": running},
    )
    return report

  def recover_incomplete_tasks(self) -> int:
    if not self.task_store:
      return 0
    incomplete = self.task_store.list_incomplete()
    resumed = 0
    for task in incomplete:
      # SUPERVISOR_PASSIVE_TIMEOUT_V1: recover timeout-first, timeout authority belongs to Supervisor
      if self._timed_out(task):
        self._finish(task, TaskStatus.TIMEOUT, error="task deadline exceeded")
        continue
      plan = self.task_store.load_plan(task.id)
      if not plan or not plan.actions:
        self._finish(task, TaskStatus.FAILED, error="interrupted: no plan to resume")
        continue
      # Q3_TASK_RESUME_V1: agent_loop is ONE action under task-level
      # granularity. Do NOT mark the whole task failed just because
      # the plan contains agent_loop. If it is already done, it is
      # in done and not in remaining, so resume proceeds with the
      # remaining non-loop actions. If it is in remaining (not
      # completed / interrupted mid-Loop), re-run the WHOLE Loop
      # action via run(plan_override=remaining), do NOT resume
      # mid-Loop. Loop-internal side effects may repeat on re-run;
      # that is the accepted task-level trade-off, documented here.
      done = self.task_store.completed_step_ids(task.id)
      remaining = [a for a in plan.actions if a.id not in done]
      if not remaining:
        # Q3_TASK_RESUME_V1: all steps done but task not finalized.
        # Previously marked FAILED; now finalize SUCCESS, because
        # every step was verified success. Do NOT re-run, do NOT
        # mark failed. task.reply/layer/tool_results are restored
        # from runtime_tasks (per-action persist in run()); if reply
        # is missing, _finish fills result with "已完成".
        task.status = TaskStatus.READY
        task.error = ""
        logger.info("恢复任务 %s：全部步骤已完成，落终态 SUCCESS", task.id)
        try:
          self._finish(task, TaskStatus.SUCCESS)
          resumed += 1
        except Exception:
          logger.exception("恢复任务 %s 落终态失败", task.id)
          self._finish(task, TaskStatus.FAILED, error="recovery finalize failed")
        continue
      plan.actions = remaining
      task.status = TaskStatus.READY
      task.error = ""
      logger.info("恢复任务 %s，剩余 %s 步", task.id, len(remaining))
      try:
        self.run(task, plan_override=plan)
        resumed += 1
      except Exception:
        logger.exception("恢复任务 %s 失败", task.id)
        self._finish(task, TaskStatus.FAILED, error="resume failed")
    if resumed:
      logger.info("启动恢复：续跑 %s 个任务", resumed)
    return resumed

  def _merge_agent_loop_result(self, task: Task, action: Action, result: RuntimeExecutionResult) -> None:
    if action.tool.startswith("skill__"):
      # 规划层直连技能：把技能返回的短句原样作为播报内容，避免只播「已完成」
      task.reply = result.message
      task.layer = "skill"
      task.result = task.reply
      return
    if action.tool != "agent_loop":
      return
    data = result.data if isinstance(result.data, dict) else {}
    task.reply = result.message
    task.layer = str(data.get("layer") or "agent_loop")
    task.tool_results = list(data.get("tool_results") or [])
    task.result = task.reply

  def _set_status(self, task: Task, status: TaskStatus) -> None:
    task.status = status
    task.touch()
    if self.task_store:
      self.task_store.set_status(task, status)
    self.event_bus.publish(
      "task.status",
      {"task_id": task.id, "status": status.value},
    )

  def _persist(self, task: Task, plan: Plan) -> None:
    if self.task_store:
      self.task_store.save(task, plan)

  def _finish(self, task: Task, status: TaskStatus, *, error: str = "", evidence: Any | None = None) -> Task:
    # CANCEL_REPORT_V1_4_2: cancel guard — a cancelled token must not be
    # overwritten by SUCCESS/FAILED/UNKNOWN from verifier/recovery. TIMEOUT
    # and CANCELLED themselves are kept as requested.
    _cancel_token = self._cancel_tokens.get(task.id)
    if _cancel_token is not None and _cancel_token.is_cancelled() and status in (TaskStatus.SUCCESS, TaskStatus.FAILED, TaskStatus.UNKNOWN):
      status = TaskStatus.CANCELLED
      error = error or "cancelled during execution"
    if error:
      task.error = _safe_str(error)
    if evidence is not None:
      task.completion_evidence = {
        "verified": bool(getattr(evidence, "verified", False)),
        "checks": getattr(evidence, "checks", []) or [],
        "observed_state": getattr(evidence, "observed_state", {}) or {},
        "reason": _safe_reason(evidence),
      }
      # Evidence objects may contain provider-specific values; persist only JSON-safe data.
      import json
      task.completion_evidence = json.loads(json.dumps(task.completion_evidence, ensure_ascii=False, default=str))
    elif not task.completion_evidence:
      task.completion_evidence = {
        "verified": status == TaskStatus.SUCCESS,
        "checks": [],
        "observed_state": {"status": status.value},
        "reason": _safe_str(error) or status.value,
      }
    task.status = status
    task.touch()
    if status == TaskStatus.SUCCESS and not task.reply:
      task.result = task.result or "已完成"
    self._cancel_tokens.pop(task.id, None)
    self._watchdog_unregister(task.id)
    self.metrics.record_task(status.value)
    logger.info(
      "task.finished task_id=%s status=%s error=%s",
      task.id,
      status.value,
      _safe_str(task.error)[:120],
    )
    payload = {
      "task_id": task.id,
      "goal": task.goal,
      "status": status.value,
      "error": _safe_str(task.error),
      "result": task.result if isinstance(task.result, (str, int, float, bool, type(None), dict, list)) else _safe_str(task.result),
      "reply": _safe_str(task.reply),
      "layer": _safe_str(task.layer),
      "tool_results": task.tool_results or [],
      "evidence": task.completion_evidence or {},
      "session_id": task.session_id,
      "agent_id": task.agent_id,
    }
    if self.task_store:
      try:
        # Persist the terminal task and feedback row atomically so a crash
        # cannot leave a finished task without a replayable feedback record.
        self.task_store.save(task, feedback_payload=payload)
      except Exception:
        logger.warning("任务反馈原子写入失败 task_id=%s", task.id, exc_info=True)
        # Preserve the primary task result if the auxiliary outbox is unavailable.
        self.task_store.save(task)
    self.event_bus.publish("task.finished", payload)
    # === Global Trace：记录 task.finish span ===
    with trace_span(
      "task.finish",
      task_id=task.id,
      status=status.value,
    ) as s:
      s.set_attribute("error", _safe_str(task.error)[:200])
      s.set_attribute("layer", _safe_str(task.layer))
    return task

  # ---------------- WATCHDOG_V1: methods below ----------------
  def _ensure_watchdog(self) -> None:
    """Start the daemon watchdog thread once; daemon so it never blocks exit."""
    with self._watchdog_lock:
      if self._watchdog_thread is not None and self._watchdog_thread.is_alive():
        return
      self._watchdog_stop.clear()
      thread = threading.Thread(
        target=self._watchdog_loop, name="kekesi-watchdog", daemon=True
      )
      self._watchdog_thread = thread
    thread.start()

  def stop_watchdog(self) -> None:
    """Stop the watchdog thread (clean shutdown / tests); safe if never started."""
    self._watchdog_stop.set()
    thread = self._watchdog_thread
    if thread is not None and thread.is_alive():
      thread.join(timeout=5.0)
    self._watchdog_thread = None

  def _watchdog_register(self, task: Task) -> None:
    self._ensure_watchdog()
    with self._watchdog_lock:
      self._watchdog_tasks[task.id] = {
        "task": task,
        "tool_count": 0,
        "stage": 0,  # 0 not overdue, 1 first grace 10s, 2 refresh1 30s, 3 refresh2 30s, 4 triggered
        "baseline": 0,
        "next_check_at": None,
        "refresh_count": 0,
        "triggered": False,
      }

  def _watchdog_unregister(self, task_id: str) -> None:
    with self._watchdog_lock:
      self._watchdog_tasks.pop(task_id, None)

  def _watchdog_tool_event(self, *args: Any, **kwargs: Any) -> None:
    """Internal tool-count callback from AgentLoop (func, ok, summary).

    Called in the same thread that runs AgentLoop, so a thread-local
    task_id set in run() identifies the task. Fallback is the single
    registered task only; with two concurrent tasks this must not be
    relied on (single active task is the assumed shape here).
    """
    task_id = getattr(self._watchdog_local, "task_id", None)
    with self._watchdog_lock:
      if task_id is not None:
        info = self._watchdog_tasks.get(task_id)
        if info is not None:
          info["tool_count"] += 1
          return
      if len(self._watchdog_tasks) == 1:
        next(iter(self._watchdog_tasks.values()))["tool_count"] += 1

  def _watchdog_loop(self) -> None:
    while not self._watchdog_stop.is_set():
      try:
        self._watchdog_check(datetime.now())
      except Exception:  # noqa: BLE001 - watchdog must never kill the main task
        logger.exception("watchdog loop failed")
      self._watchdog_stop.wait(self._watchdog_poll_sec)

  def _watchdog_check(self, now: datetime) -> None:
    with self._watchdog_lock:
      infos = list(self._watchdog_tasks.values())
    for info in infos:
      try:
        self._watchdog_advance(info, now)
      except Exception:  # noqa: BLE001 - one task must not stop the others
        logger.exception("watchdog check failed")

  def _watchdog_advance(self, info: dict, now: datetime) -> None:
    task = info["task"]
    if task.is_terminal():
      self._watchdog_unregister(task.id)
      return
    # WATCHDOG_V2: already triggered — never cancel/publish twice.
    if info.get("triggered") or info.get("stage") == 4:
      return
    # Only EXECUTING tasks are watched. PLANNING/READY/VERIFYING/RECOVERING
    # are short phases; a deadline passing there is handled by the
    # passive checks in run(), not by the watchdog.
    if task.status != TaskStatus.EXECUTING:
      with self._watchdog_lock:
        info["stage"] = 0
        info["baseline"] = info["tool_count"]
        info["next_check_at"] = None
      return
    if not task.deadline_at:
      return
    try:
      deadline = datetime.fromisoformat(task.deadline_at)
    except ValueError:
      return
    with self._watchdog_lock:
      tool_count = info["tool_count"]
      stage = info["stage"]
      next_check_at = info["next_check_at"]
      baseline = info["baseline"]
    if now <= deadline:
      # WATCHDOG_V2: not overdue yet — do NOT reset stage/baseline here.
      # Grace timing (next_check_at) is separate from the authoritative
      # deadline. Resetting here is what turned a no-progress refresh
      # into a 0->1->2->0 loop in v1. Reset happens only in the
      # true-progress branch below.
      return
    if stage == 0:
      # First overdue detection: enter first grace, do not cancel yet.
      with self._watchdog_lock:
        info["stage"] = 1
        info["baseline"] = tool_count
        info["next_check_at"] = now + timedelta(seconds=self._watchdog_first_grace_sec)
      return
    if next_check_at is None or now < next_check_at:
      return
    if tool_count > baseline:
      # Progress: slow but moving. Give a fresh timeout window and reset grace.
      # This is a refresh of the delay, not a new task; refresh_count resets.
      new_deadline = now + timedelta(seconds=self.task_timeout_sec)
      task.deadline_at = new_deadline.isoformat()
      with self._watchdog_lock:
        info["stage"] = 0
        info["baseline"] = tool_count
        info["next_check_at"] = None
        info["refresh_count"] = 0
      if self.task_store:
        try:
          self.task_store.save(task)
        except Exception:  # noqa: BLE001
          logger.warning("watchdog deadline refresh save failed", exc_info=True)
      return
    # No progress in this grace window.
    if stage == 1:
      # WATCHDOG_V2: only move next_check_at (grace timing).
      # Never move task.deadline_at here: that field is also read by
      # the step-1 final timeout check, and moving it would let an
      # overdue task become SUCCESS.
      with self._watchdog_lock:
        info["stage"] = 2
        info["refresh_count"] = 1
        info["next_check_at"] = now + timedelta(seconds=self._watchdog_refresh_sec)
      return
    if stage == 2:
      # WATCHDOG_V2: same as stage 1 — grace timing only, deadline untouched.
      with self._watchdog_lock:
        info["stage"] = 3
        info["refresh_count"] = 2
        info["next_check_at"] = now + timedelta(seconds=self._watchdog_refresh_sec)
      return
    if stage == 3:
      # WATCHDOG_V2: _watchdog_max_refreshes now drives the decision,
      # not just the reason text. refresh_count is 2 here (two
      # refreshes used); if it were lower, this would be a miscount
      # and must not cancel yet.
      with self._watchdog_lock:
        _refreshes = info["refresh_count"]
      if _refreshes < self._watchdog_max_refreshes:
        with self._watchdog_lock:
          info["refresh_count"] = _refreshes + 1
          info["next_check_at"] = now + timedelta(seconds=self._watchdog_refresh_sec)
        return
      with self._watchdog_lock:
        info["stage"] = 4
        info["triggered"] = True
      self._watchdog_cancel_only(task, baseline, tool_count)
      return

  def _watchdog_cancel_only(self, task: Task, baseline: int, tool_count: int) -> None:
    """Step 2 cancel signal ONLY: token.cancel(), no processes, no status change.

    Do NOT call the Supervisor cancel method here: it also does process
    termination (step 4) and immediately sets CANCELLED in the store. The final task status is
    decided by run() after AgentLoop returns on the cancel check (Q2 scope,
    deliberately not changed here). The reason is recorded in task.error
    and published on the event bus so the user-facing feedback can use it.
    """
    token = self._cancel_tokens.get(task.id)
    if token is not None:
      try:
        token.cancel()
      except Exception:  # noqa: BLE001
        logger.warning("watchdog token cancel failed", exc_info=True)
    reason = (
      "watchdog timeout: deadline exceeded, tool_count "
      f"{baseline}->{tool_count} unchanged after first grace "
      f"{self._watchdog_first_grace_sec:.0f}s and "
      f"{self._watchdog_max_refreshes} refreshes of "
      f"{self._watchdog_refresh_sec:.0f}s each"
    )
    task.error = reason
    if self.task_store:
      try:
        self.task_store.save(task)
      except Exception:  # noqa: BLE001
        logger.warning("watchdog cancel save failed", exc_info=True)
    try:
      self.event_bus.publish(
        "task.watchdog_cancel",
        {
          "task_id": task.id,
          "reason": reason,
          "tool_count_before": baseline,
          "tool_count_after": tool_count,
          "refresh_count": self._watchdog_max_refreshes,
        },
      )
    except Exception:  # noqa: BLE001
      logger.warning("watchdog cancel publish failed", exc_info=True)
    logger.warning("watchdog cancel task_id=%s %s", task.id, reason)

  @staticmethod
  def _cancelled(cancel_check: Callable[[], bool] | None) -> bool:
    return bool(cancel_check and cancel_check())

  @staticmethod
  def _timed_out(task: Task) -> bool:
    if not task.deadline_at:
      return False
    try:
      deadline = datetime.fromisoformat(task.deadline_at)
    except ValueError:
      return False
    return datetime.now() > deadline
