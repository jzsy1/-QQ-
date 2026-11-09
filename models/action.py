"""Action 模型 — 任务执行单元。"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import uuid4


@dataclass
class Action:
  """单个可执行动作。"""

  tool: str
  parameters: dict = field(default_factory=dict)
  id: str = field(default_factory=lambda: str(uuid4()))
  timeout: float = 30.0
  risk_level: int = 0
  verification: dict | None = None
  retry_policy: dict | None = None
