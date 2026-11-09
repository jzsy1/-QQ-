"""Supervisor 兼容层 — 委托 v2.0 KekesiRuntime。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from .models.task import Task, TaskStatus

# 向后兼容：旧代码使用 RuntimeTask / TaskState
RuntimeTask = Task
TaskState = TaskStatus


@dataclass
class LegacyRuntimeTask:
  """旧版内存任务记录（测试与历史引用）。"""

  id: str
  user_text: str
  state: TaskStatus = TaskStatus.CREATED
  created_at: str = field(default_factory=lambda: datetime.now().isoformat())
  tool_results: list[dict] = field(default_factory=list)
  layer: str = ""
  error: str = ""


class Supervisor:
  """顶层调度兼容包装：实际执行委托 KekesiRuntime。"""

  def __init__(self, agent, *, router: Any | None = None) -> None:
    self.agent = agent
    self.router = router
    self._runtime = None
    if router is not None and getattr(router, "runtime", None) is not None:
      self._runtime = router.runtime
    self._active: dict[str, Task] = {}

  @property
  def windows_agent(self):
    if self._runtime:
      return self._runtime.windows_agent
    from .agents.windows_agent import WindowsAgent

    return WindowsAgent(self.agent.tool_ctx)

  def is_complex_task(self, text: str) -> bool:
    if self._runtime:
      return self._runtime.planner.is_complex_task(text)
    from .core.planner import Planner

    return Planner(windows_agent=self.windows_agent).is_complex_task(text)

  def handle(
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
    if self._runtime is not None:
      reply, layer, tool_results, task = self._runtime.handle_legacy(
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
      if task:
        self._active[task.id] = task
      return reply, layer, tool_results, task

    # 无 runtime 时的最小降级（不应在正常运行中出现）
    reply, layer, tool_results = self.agent.run(
      text,
      agent_id=agent_id,
      session_id=session_id,
      on_chunk=on_chunk,
      on_start=on_start,
      on_tool_event=on_tool_event,
      on_tool_start=on_tool_start,
      cancel_check=cancel_check,
      memory_text=memory_text or text,
    )
    return reply, layer or "", list(tool_results or []), None

  def get_task(self, task_id: str) -> Task | None:
    if self._runtime:
      return self._runtime.get_task(task_id)
    return self._active.get(task_id)

  def cancel_active(self, task_id: str) -> bool:
    if self._runtime:
      return self._runtime.cancel_task(task_id)
