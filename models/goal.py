"""Goal 模型 — 跨会话长期目标对象。

v1（KEKESI_GOAL_MODEL_V1）：照 kekesi/models/task.py 写。
Goal 是“要达成的状态”，Task 是“一次执行”；一个 Goal 可派生多个 Task。
本文件只依赖标准库。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from uuid import uuid4


class GoalStatus(str, Enum):
    """目标生命周期（v1 不设 FAILED：失败走失败计数，计满进 BLOCKED）。"""

    ACTIVE = "active"
    BLOCKED = "blocked"
    DONE = "done"
    CANCELLED = "cancelled"


GOAL_TERMINAL_STATUSES = frozenset({
    GoalStatus.DONE,
    GoalStatus.CANCELLED,
})


@dataclass
class Goal:
    """系统长期目标对象。"""

    goal: str
    id: str = field(default_factory=lambda: str(uuid4()))
    source: str = "user"
    status: GoalStatus = GoalStatus.ACTIVE
    parent_id: str | None = None
    priority: int = 0
    deadline_at: str | None = None
    acceptance_json: str = ""
    last_task_id: str | None = None
    last_task_status: str = ""
    next_attempt_at: str | None = None
    failure_count: int = 0
    blocked_reason: str = ""
    discovery_key: str = ""
    discovery_evidence_json: str = ""
    risk_level: str = "medium"
    dispatch_state: str = "approval_required"
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def is_terminal(self) -> bool:
        return self.status in GOAL_TERMINAL_STATUSES

    def touch(self) -> None:
        self.updated_at = datetime.now().isoformat()
