"""KekesiRuntime — v2.0 核心运行时入口。"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

from ..agents.windows_agent import WindowsAgent
from ..memory.db import Database
from ..models.task import Task, TaskStatus
from .event_bus import EventBus
from .metrics import RuntimeMetrics, bind_runtime_metrics, get_runtime_metrics
from .legacy_adapter import LegacyExecutorAdapter
from .planner import Planner
from .recovery import Recovery
from .supervisor import ResponsePolicy, Supervisor
from .task_store import TaskStore
from .task_completion_feedback import TaskCompletionFeedbackHook
from .goal_store import GoalStore
from .goal_engine import GoalDriver
from .verifier import Verifier

logger = logging.getLogger(__name__)


def _load_runtime_cfg(base_dir: Path) -> dict:
  yaml_path = base_dir / "config.yaml"
  if not yaml_path.exists():
    return {}
  try:
    import yaml

    with open(yaml_path, encoding="utf-8") as f:
      data = yaml.safe_load(f) or {}
    block = data.get("runtime", {})
    return block if isinstance(block, dict) else {}
  except Exception:
    return {}


class KekesiRuntime:
  """v2.0 主入口：Task 管理 + Agent 调度 + 工具执行 + 结果验证。"""

  def __init__(
    self,
    agent_runner: Any,
    db: Database,
    *,
    config_dir: Path | None = None,
    mcp_bridge: Any | None = None,
    event_bus: EventBus | None = None,
    learning: Any | None = None,
  ) -> None:
    base = config_dir or Path(__file__).resolve().parent.parent
    cfg = _load_runtime_cfg(base)

    self.agent_runner = agent_runner
    self.db = db
    self.mcp_bridge = mcp_bridge
    self.event_bus = event_bus or EventBus()
    self.metrics = RuntimeMetrics()
    bind_runtime_metrics(self.metrics)
    self.task_store = TaskStore(db)
    self.goal_store = GoalStore(db)
    from ..memory.learning import LearningService
    self.learning = learning or LearningService(db, cfg={"async": False})
    self.windows_agent = WindowsAgent(agent_runner.tool_ctx)

    task_timeout = float(cfg.get("task_timeout", 300))
    max_retries = int(cfg.get("max_retries", 1))

    self.planner = Planner(
      windows_agent=self.windows_agent,
      default_agent_timeout=task_timeout,
      shell_timeout=float(cfg.get("shell_timeout", 30)),
    )
    self.planner.learning = self.learning
    self.executor = LegacyExecutorAdapter(
      agent_runner.tool_ctx,
      mcp_bridge=mcp_bridge,
      agent_runner=agent_runner,
    )
    self.verifier = Verifier()
    self.recovery = Recovery(max_retries=max_retries)
    self.supervisor = Supervisor(
      planner=self.planner,
      executor=self.executor,
      verifier=self.verifier,
      recovery=self.recovery,
      task_store=self.task_store,
      event_bus=self.event_bus,
      task_timeout_sec=task_timeout,
      windows_agent=self.windows_agent,
      metrics=self.metrics,
    )
    self.feedback_hook = TaskCompletionFeedbackHook(
      self.event_bus, self.task_store, self._consume_task_feedback
    )
    self.goal_driver = GoalDriver(
      db, self.goal_store, self.task_store, self,
      enabled=bool(cfg.get("goal_discovery_enabled", False)),
      dispatch_enabled=bool(cfg.get("autonomous_goal_dispatch_enabled", False)),
      interval_sec=int(cfg.get("goal_scan_interval_sec", 300)),
      daily_budget=int(cfg.get("autonomy_daily_budget", 5)),
      task_timeout_sec=int(cfg.get("autonomy_task_timeout_sec", 120)),
      max_tool_calls=int(cfg.get("autonomy_max_tool_calls", 5)),
    )
    self.refresh_capability_health()

    recovered = self.supervisor.recover_incomplete_tasks()
    if recovered:
      logger.info("KekesiRuntime 启动恢复：%s 个任务", recovered)
    try:
      dispatches_reconciled = self.goal_store.reconcile_autonomy_dispatches(self.task_store)
      goals_reconciled = self.goal_store.reconcile(self.task_store)
      if dispatches_reconciled or goals_reconciled:
        logger.info(
          "自主目标启动对账：派发 %s，目标 %s",
          dispatches_reconciled, goals_reconciled,
        )
    except Exception:
      logger.warning("自主目标启动对账失败", exc_info=True)

  def _consume_task_feedback(self, payload: dict[str, Any]) -> None:
    """Route objective task results into the existing learning service."""
    task_id = str(payload.get("task_id") or "")
    status = str(payload.get("status") or "unknown")
    try:
      self.goal_store.record_action_results(
        task_id,
        list(payload.get("tool_results") or []),
        payload.get("evidence") if isinstance(payload.get("evidence"), dict) else {},
        status,
      )
    except Exception:
      logger.warning("自主动作执行审计写入失败 task_id=%s", task_id, exc_info=True)
    result = payload.get("reply") or payload.get("result") or ""
    text = (
      f"{result}\n任务终态：{status}。{payload.get('error') or ''}。"
      f"验证证据：{payload.get('evidence') or {}}"
    )
    self.learning.on_task_completion(
      user_text=str(payload.get("goal") or ""),
      reply_text=str(text),
      session_id=str(payload.get("session_id") or "default"),
      agent_id=str(payload.get("agent_id") or "keks"),
      tool_results=list(payload.get("tool_results") or []),
      task_id=task_id,
      status=status,
      evidence=payload.get("evidence") if isinstance(payload.get("evidence"), dict) else {},
    )
    try:
      self.goal_store.record_task_result(
        task_id, status, error=str(payload.get("error") or "")
      )
    except Exception:
      logger.warning("任务目标状态回写失败 task_id=%s", task_id, exc_info=True)
    try:
      from ..memory.cognition import update_capability_health

      for item in payload.get("tool_results") or []:
        if not isinstance(item, dict):
          continue
        name = str(item.get("name") or "")
        ok = item.get("ok")
        if name.startswith(("skill__", "mcp__")) and isinstance(ok, bool):
          summary = str(item.get("error") or item.get("summary") or item.get("text") or "")
          failure_class = str(item.get("failure_class") or self._classify_capability_failure(summary))
          update_capability_health(
            name,
            success=ok,
            failure_class=failure_class,
            source="task_completion",
            evidence={
              "task_id": task_id,
              "task_status": status,
              "error": summary[:300],
              "verifier": payload.get("evidence") or {},
            },
          )
    except Exception:
      logger.warning("任务能力证据更新失败 task_id=%s", task_id, exc_info=True)

  @staticmethod
  def _classify_capability_failure(message: str) -> str:
    text = (message or "").lower()
    if any(token in text for token in ("permission denied", "access denied", "权限不足", "拒绝访问")):
      return "environment"
    if any(token in text for token in ("not found", "no such", "未找到", "不存在", "未匹配")):
      return "target"
    if any(token in text for token in ("verification failed", "verifier", "验证失败", "未通过验证", "无法确认")):
      return "verification"
    if any(token in text for token in ("timeout", "timed out", "network", "连接中断", "网络")):
      return "environment"
    if any(token in text for token in ("importerror", "modulenotfound", "traceback", "入口非法", "无法装载")):
      return "tool"
    return "unknown"

  def refresh_capability_health(self) -> None:
    """Record static skill checks as UNVERIFIED, never as runtime HEALTHY."""
    try:
      tool_ctx = getattr(self.agent_runner, "tool_ctx", None)
      bridge = getattr(tool_ctx, "skill_bridge", None)
      if bridge is None:
        bridge = getattr(getattr(self.agent_runner, "_agent_loop", None), "skill_bridge", None)
      registry = getattr(bridge, "registry", None)
      from ..memory.cognition import update_capability_health

      if registry is not None and hasattr(registry, "health_report"):
        for report in registry.health_report().values():
          for name in report.get("tool_names") or []:
            update_capability_health(
              name,
              state=str(report.get("state") or "UNKNOWN"),
              source=str(report.get("source") or "skill_registry"),
              ttl_sec=86400,
              evidence={"error": report.get("error"), "static_check": report.get("static_check")},
            )
      if self.mcp_bridge is not None:
        mcp_health = self.mcp_bridge.health()
        names = self.mcp_bridge.tool_names()
        mcp_state = "UNVERIFIED" if mcp_health.get("running") and mcp_health.get("ready") else "DEGRADED"
        for name in names:
          update_capability_health(
            name,
            state=mcp_state,
            source="mcp_bridge.health",
            ttl_sec=900,
            evidence={
              "running": mcp_health.get("running"),
              "ready": mcp_health.get("ready"),
              "servers": mcp_health.get("servers") or [],
              "errors": mcp_health.get("errors") or [],
            },
          )
    except Exception:
      logger.warning("技能能力静态体检写入认知失败", exc_info=True)

  def close(self) -> None:
    self.goal_driver.close()
    self.feedback_hook.close()
    self.supervisor.stop_watchdog()

  def create_task(
    self,
    goal: str,
    *,
    session_id: str = "default",
    agent_id: str = "keks",
    memory_text: str | None = None,
    deadline_at: str | None = None,
  ) -> Task:
    task = Task(
      goal=goal.strip(),
      session_id=session_id,
      agent_id=agent_id,
      memory_text=memory_text,
      deadline_at=deadline_at,
    )
    self.task_store.save(task)
    return task

  def handle(
    self,
    request: str,
    *,
    session_id: str = "default",
    agent_id: str = "keks",
    on_chunk: Callable[[str], None] | None = None,
    on_start: Callable[[], None] | None = None,
    on_tool_event: Callable[[str, bool, str], None] | None = None,
    on_tool_start: Callable[[str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    memory_text: str | None = None,
    deadline_at: str | None = None,
    max_tool_calls: int | None = None,
    autonomous: bool = False,
    goal_id: str | None = None,
  ) -> Task:
    task = self.create_task(
      request,
      session_id=session_id,
      agent_id=agent_id,
      memory_text=memory_text,
      deadline_at=deadline_at,
    )
    if autonomous and goal_id:
      try:
        linked = self.goal_store.link_autonomy_task(str(goal_id), task.id)
      except Exception:
        logger.warning("自主目标任务关联失败 goal_id=%s task_id=%s", goal_id, task.id, exc_info=True)
        linked = False
      if not linked:
        task.status = TaskStatus.CANCELLED
        task.error = "goal dispatch claim is no longer valid"
        self.task_store.save(task)
        return task
    driver = getattr(self, "goal_driver", None)
    active_event = None
    if autonomous and driver is not None:
      active_event = driver.register_autonomous_task(task.id)
    elif driver is not None:
      driver.begin_user_request()

    def _foreground_cancelled() -> bool:
      return bool(
        (active_event is not None and active_event.is_set())
        or (cancel_check is not None and cancel_check())
      )

    action_authorizer = None
    if autonomous and goal_id:
      action_authorizer = lambda tool_name, args, consume: self.goal_store.check_action_authorization(
        str(goal_id), tool_name, args, task_id=task.id, consume=consume
      )

    try:
      return self.supervisor.run(
        task,
        on_chunk=on_chunk,
        on_start=on_start,
        on_tool_event=on_tool_event,
        on_tool_start=on_tool_start,
        cancel_check=_foreground_cancelled,
        max_tool_calls=max_tool_calls,
        autonomous=autonomous,
        action_authorizer=action_authorizer,
      )
    finally:
      if autonomous and driver is not None:
        driver.unregister_autonomous_task(task.id)
      elif driver is not None:
        driver.end_user_request()

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
    driver = getattr(self, "goal_driver", None)
    if driver is not None:
      driver.begin_user_request()
    try:
      return self.supervisor.handle_legacy(
        text,
        agent_id=agent_id,
        session_id=session_id,
        on_chunk=on_chunk,
        on_start=on_start,
        on_tool_event=on_tool_event,
        on_tool_start=on_tool_start,
        cancel_check=cancel_check,
        memory_text=memory_text,
      )
    finally:
      if driver is not None:
        driver.end_user_request()

  def format_response(self, task: Task) -> str:
    return ResponsePolicy.format(task)

  def cancel_task(self, task_id: str) -> bool:
    return self.supervisor.cancel(task_id)

  def approve_goal(self, goal_id: str) -> bool:
    return self.goal_driver.approve(goal_id)

  def authorize_goal_action(
    self, goal_id: str, tool_name: str, args: dict, note: str = "",
    rollback_plan: str = "",
  ) -> int | None:
    return self.goal_driver.authorize_action(goal_id, tool_name, args, note, rollback_plan)

  def autonomy_status(self) -> dict[str, Any]:
    return self.goal_driver.status()

  def get_task(self, task_id: str) -> Task | None:
    return self.task_store.get_task(task_id)
