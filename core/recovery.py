"""失败恢复策略 — retry / fallback / abort。"""

from __future__ import annotations

import logging
from typing import Any, Callable

from ..models.action import Action
from ..models.evidence import Evidence
from ..models.result import RuntimeExecutionResult
from ..models.task import Task

logger = logging.getLogger(__name__)


def _safe_str(value: Any) -> str:
    """任意值安全转 str；method/None 返回空串（对齐 supervisor._safe_str）。"""
    if value is None or callable(value):
        return ""
    try:
        return str(value)
    except Exception:
        return ""


class Recovery:
  """验证失败或执行失败后的恢复策略。"""

  def __init__(
    self,
    *,
    max_retries: int = 1,
    on_ask_user: Callable[[Task, Action, str], bool] | None = None,
  ) -> None:
    self.max_retries = max(0, int(max_retries))
    self._on_ask_user = on_ask_user
    self._attempts: dict[str, int] = {}

  def _key(self, task: Task, action: Action) -> str:
    return f"{task.id}:{action.id}"

  def try_recover(
    self,
    task: Task,
    action: Action,
    result: RuntimeExecutionResult,
    evidence: Evidence,
    *,
    retry_fn: Callable[[Action], RuntimeExecutionResult] | None = None,
    read_only_check: Callable[[Action], str] | None = None,
  ) -> bool:
    """尝试恢复；返回 True 表示可继续任务。"""
    key = self._key(task, action)
    attempts = self._attempts.get(key, 0)

    policy = action.retry_policy or {}
    max_retries = int(policy.get("max_retries", self.max_retries))

    # UNKNOWN_GATE_V2_4_3: unknown gate -- retry/fallback only after a read-only
    # check positively confirms no side effect. Otherwise, no retry, no fallback.
    _is_unknown = (
      (getattr(evidence, "observed_state", {}) or {}).get("state") == "unknown"
      or getattr(result, "status", "") in ("timeout", "unknown")
      or (isinstance(getattr(result, "evidence", None), dict) and result.evidence.get("unknown"))
      or (result.status == "cancelled" and _safe_str(result.message) != "cancelled before execution")
    )
    if _is_unknown:
      _check_result = None
      if read_only_check is not None:
        try:
          _check_result = read_only_check(action)
        except Exception:
          logger.debug("Recovery read-only check failed", exc_info=True)
          _check_result = None
      if _check_result != "no_effect":
        task.error = (
          _safe_str(getattr(evidence, "reason", ""))
          or _safe_str(result.error)
          or _safe_str(result.message)
          or "tool result unknown, no retry without read-only confirmation"
        )
        logger.info("Recovery unknown gate: no retry for %s, check=%s", action.tool, _check_result)
        return False
      logger.info("Recovery unknown gate: read-only check confirmed no effect for %s, retry allowed", action.tool)

    if retry_fn and attempts < max_retries:
      self._attempts[key] = attempts + 1
      logger.info(
        "Recovery retry %s/%s for %s",
        attempts + 1,
        max_retries,
        action.tool,
      )
      retry_result = retry_fn(action)
      if retry_result.can_report_success():
        task.error = ""
        return True

    fallback_tool = policy.get("fallback_tool")
    if fallback_tool and retry_fn:
      logger.info("Recovery fallback → %s", fallback_tool)
      fallback_action = Action(
        tool=str(fallback_tool),
        parameters=dict(policy.get("fallback_params") or action.parameters),
        timeout=action.timeout,
        risk_level=action.risk_level,
      )
      fb_result = retry_fn(fallback_action)
      if fb_result.can_report_success():
        return True

    # evidence.reason / result.error / result.message 均偶发为 method 类型，
    # 直接拼字符串会短路污染 task.error → 写 SQLite 抛 "type 'method' is not supported"
    reason = (
        _safe_str(getattr(evidence, "reason", ""))
        or _safe_str(result.error)
        or _safe_str(result.message)
        or "verify failed"
    )
    task.error = reason

    if self._on_ask_user:
      try:
        return bool(self._on_ask_user(task, action, reason))
      except Exception:
        logger.debug("Recovery ask_user failed", exc_info=True)

    return False

  def reset(self, task_id: str | None = None) -> None:
    if task_id is None:
      self._attempts.clear()
      return
    prefix = f"{task_id}:"
    for key in list(self._attempts):
      if key.startswith(prefix):
        del self._attempts[key]
