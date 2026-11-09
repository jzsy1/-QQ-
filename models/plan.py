"""Plan 模型 — Planner 输出。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from uuid import uuid4

from .action import Action


@dataclass
class Plan:
  """执行计划：由多个 Action 组成。"""

  actions: list[Action] = field(default_factory=list)
  id: str = field(default_factory=lambda: str(uuid4()))
  summary: str = ""
  source: str = "planner"
  acceptance: list[dict] = field(default_factory=list)  # KEKESI_CONTRACT_V0


# KEKESI_CONTRACT_V0: Goal.acceptance_json → Plan.acceptance 翻译（只收 state 8 终态 kinds，非法条目丢弃）。
_GOAL_STATE_KINDS = frozenset({
    "process_running", "process_not_running", "file_exists", "file_not_exists",
    "window_visible", "window_not_visible", "pid_not_alive", "package_not_installed",
})


def goal_acceptance_to_entries(acceptance_json: str | None) -> list[dict]:
    if not acceptance_json:
        return []
    try:
        data = json.loads(acceptance_json)
    except (ValueError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    out: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "").strip()
        target = str(item.get("target") or "").strip()
        if kind not in _GOAL_STATE_KINDS or not target:
            continue
        out.append({"type": "state", "kind": kind, "target": target, "source": "plan"})
    return out
