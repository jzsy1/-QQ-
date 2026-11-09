"""Task 模型 — v2.0 统一任务对象。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any
from uuid import uuid4


class TaskStatus(str, Enum):
  """v2.0 任务生命周期。"""

  CREATED = "created"
  PLANNING = "planning"
  READY = "ready"
  EXECUTING = "executing"
  VERIFYING = "verifying"
  RECOVERING = "recovering"
  SUCCESS = "success"
  FAILED = "failed"
  CANCELLED = "cancelled"
  TIMEOUT = "timeout"
  UNKNOWN = "unknown"

  # 向后兼容别名
  PENDING = "pending"
  COMPLETED = "success"
  RUNNING = "executing"


TERMINAL_STATUSES = frozenset({
  TaskStatus.SUCCESS,
  TaskStatus.FAILED,
  TaskStatus.CANCELLED,
  TaskStatus.TIMEOUT,
  TaskStatus.UNKNOWN,
})


@dataclass
class Task:
  """系统核心任务对象。"""

  goal: str
  id: str = field(default_factory=lambda: str(uuid4()))
  status: TaskStatus = TaskStatus.CREATED
  steps: list = field(default_factory=list)
  result: Any | None = None
  session_id: str = "default"
  agent_id: str = "keks"
  created_at: str = field(default_factory=lambda: datetime.now().isoformat())
  updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
  error: str = ""
  layer: str = ""
  tool_results: list[dict] = field(default_factory=list)
  completion_evidence: dict[str, Any] = field(default_factory=dict)
  reply: str | None = None
  deadline_at: str | None = None
  memory_text: str | None = None

  def is_terminal(self) -> bool:
    return self.status in TERMINAL_STATUSES

  def touch(self) -> None:
    self.updated_at = datetime.now().isoformat()
