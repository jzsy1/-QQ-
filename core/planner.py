"""Planner — 将用户目标拆解为 Action 计划。"""

from __future__ import annotations

import logging
import re
from typing import Any

from ..agents.windows_agent import WindowsAgent
from ..models.action import Action
from ..models.plan import Plan, goal_acceptance_to_entries
from ..models.task import Task

logger = logging.getLogger(__name__)

_COMPLEX_PATTERNS = (
  re.compile(r"先.+再.+"),
  re.compile(r"然后|接着|之后|最后"),
  re.compile(r"一步步|分步|多步"),
  re.compile(r"全部|所有|批量"),
  re.compile(r"工作流|自动化"),
)

_OPEN_RE = re.compile(
  r"^(?:请|帮我|给我)?(?:打开|启动|运行)(?:一下|下)?\s*(.+?)[。！？!?\s]*$",
)
_CLOSE_RE = re.compile(
  r"^(?:请|帮我)?(?:关闭|关掉|退出)(?:一下|下)?\s*(.+?)[。！？!?\s]*$",
)


class Planner:
  """规划层：不直接操作系统，只产出 Plan。"""

  def __init__(
    self,
    *,
    windows_agent: WindowsAgent | None = None,
    default_agent_timeout: float = 300.0,
    shell_timeout: float = 30.0,
  ) -> None:
    self.windows_agent = windows_agent
    self.default_agent_timeout = default_agent_timeout
    self.shell_timeout = shell_timeout

  def is_complex_task(self, text: str) -> bool:
    raw = (text or "").strip()
    if len(raw) < 8:
      return False
    return any(p.search(raw) for p in _COMPLEX_PATTERNS)

  def create(self, task: Task, acceptance_json: str | None = None) -> Plan:
    goal = (task.goal or "").strip()
    if not goal:
      return Plan(actions=[], summary="empty goal")

    policy_context = ""
    policies = []
    learning = getattr(self, "learning", None)
    if learning is not None:
      try:
        policies = learning.get_execution_policies(goal, None, limit=2)
        policy_context = learning.format_execution_policy_context(policies)
      except Exception:
        logger.debug("Planner 执行策略检索失败", exc_info=True)

    concrete = self._try_concrete_plan(goal)
    if concrete is not None:
      concrete_tool = str(concrete.actions[0].tool) if concrete.actions else ""
      if any(
        not p.get("is_candidate_reference")
        and str(p.get("policy_state", "active")) == "active"
        and str(p.get("original_func") or "") == concrete_tool
        and str(p.get("correction_func") or "") != concrete_tool
        for p in policies
      ):
        return self._agent_loop_plan(task, goal, policy_context)
      concrete.acceptance = self._acceptance_for_actions(concrete.actions)
      if acceptance_json:
        concrete.acceptance = goal_acceptance_to_entries(acceptance_json) + concrete.acceptance
      return concrete

    return self._agent_loop_plan(task, goal, policy_context)

  def _agent_loop_plan(self, task: Task, goal: str, policy_context: str = "") -> Plan:
    parameters = {
      "text": goal,
      "agent_id": task.agent_id,
      "session_id": task.session_id,
      "complex": self.is_complex_task(goal),
      "memory_text": task.memory_text or goal,
    }
    if policy_context:
      parameters["execution_policy_context"] = policy_context
    return Plan(
      actions=[
        Action(
          tool="agent_loop",
          parameters=parameters,
          timeout=self.default_agent_timeout,
          risk_level=0,
        )
      ],
      summary="agent_loop",
      source="planner",
      acceptance=[],
    )

  def _try_concrete_plan(self, goal: str) -> Plan | None:
    if not self.windows_agent:
      return None

    m = _OPEN_RE.match(goal)
    if m:
      app = m.group(1).strip()
      if app and len(app) <= 24:
        return Plan(
          actions=[
            Action(
              tool="open_app",
              parameters={"app_name": app},
              timeout=30.0,
              risk_level=1,
              verification={"type": "process_and_window"},
            )
          ],
          summary=f"open {app}",
          source="windows_planner",
        )

    m = _CLOSE_RE.match(goal)
    if m:
      app = m.group(1).strip()
      if app and len(app) <= 24:
        return Plan(
          actions=[
            Action(
              tool="close_app",
              parameters={"app_name": app, "force": True},
              timeout=30.0,
              risk_level=1,
              verification={"type": "process_gone"},
              retry_policy={
                "max_retries": 1,
                "fallback_tool": "kill_process",
                "fallback_params": {"process": app},
              },
            )
          ],
          summary=f"close {app}",
          source="windows_planner",
        )

    return None

  # KEKESI_CONTRACT_V0: acceptance 派生（verification 优先，tool 查表次之，皆无则空）。
  def _acceptance_target(self, kind: str, params: dict) -> str:
    params = params or {}
    if kind in ("process_running", "process_not_running"):
      return str(params.get("app_name") or params.get("target") or params.get("process") or "").strip()
    if kind in ("file_exists", "file_not_exists"):
      return str(params.get("path") or params.get("dest") or params.get("destination") or "").strip()
    if kind in ("window_visible", "window_not_visible"):
      return str(params.get("title") or params.get("window_title") or "").strip()
    if kind == "pid_not_alive":
      return str(params.get("pid") or "").strip()
    if kind == "package_not_installed":
      return str(params.get("package") or params.get("name") or params.get("app_name") or "").strip()
    return ""

  def _checkable_map(self) -> dict:
    # 唯一来源：Verifier._GOAL_CHECKABLE_TOOLS；懒导入，取不到则该分支无 acceptance（不中断规划）。
    try:
      from .verifier import Verifier
      m = getattr(Verifier, "_GOAL_CHECKABLE_TOOLS", None)
      return dict(m) if isinstance(m, dict) else {}
    except Exception:
      return {}

  def _acceptance_for_actions(self, actions: list[Action]) -> list[dict]:
    checkable = self._checkable_map()
    out: list[dict] = []
    for action in actions or []:
      verification = getattr(action, "verification", None)
      params = getattr(action, "parameters", None) or {}
      tool = str(getattr(action, "tool", "") or "").strip()
      if isinstance(verification, dict):
        vtype = str(verification.get("type") or "").strip()
        if vtype == "process_and_window":
          target = str(params.get("app_name") or "").strip()
          if target:
            out.append({"type": "state", "kind": "process_running", "target": target, "source": "plan", "window_unchecked": True})
        elif vtype == "process_gone":
          target = str(params.get("app_name") or "").strip()
          if target:
            out.append({"type": "state", "kind": "process_not_running", "target": target, "source": "plan"})
        elif vtype in ("process_running", "process_not_running", "file_exists", "file_not_exists", "window_visible", "window_not_visible", "pid_not_alive", "package_not_installed"):
          target = self._acceptance_target(vtype, params)
          if target:
            out.append({"type": "state", "kind": vtype, "target": target, "source": "plan"})
        # 未知 verification 类型 → 无条目，不回落到 tool 查表
        continue
      kind = checkable.get(tool)
      if kind is None:
        continue
      if tool == "kill_process" and params.get("pid"):
        kind = "pid_not_alive"
      target = self._acceptance_target(kind, params)
      if target:
        out.append({"type": "state", "kind": kind, "target": target, "source": "plan"})
    return out
