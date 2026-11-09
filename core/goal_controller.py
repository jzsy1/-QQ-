"""Supervisor 目标控制 — GoalController（GOAL_CTRL_V1）。

三态判定（Supervisor 拥有目标与验收标准，循环只提供事件与执行）：
  达成（achieved）：plan.acceptance 非空且全部 state 探针 pass → 提前停，
      只触发提前终态验证，不改写最终 verdict（终态验证仍说了算）。
  进行中（in-progress）：未达成也未偏离 → 继续。
  偏离·失控（locked/stalled/exhausted）：破坏性越界 veto、连续安全拒绝、
      连续无进展轮、时间预算 → 锁/停，终态 FAILED（locked 带原因前缀）。

注意：
  - 本文件只依赖标准库；task/plan/verifier/ctx 均为鸭子类型，便于单测。
  - 停锁绝不碰 cancel token；用户取消永远优先（循环侧保证）。
  - acceptance 为空 → 永不早停；state 探针任一非 pass → 不早停。
  - veto 锁后 fail-closed：后续一切调用一律 veto，零新增执行。
"""

from __future__ import annotations

import json
import time

_DESTRUCTIVE_TOOLS = {"delete_file", "move_to_trash", "kill_process", "uninstall_software"}
_DESTRUCTIVE_KINDS = {"file_not_exists", "pid_not_alive", "package_not_installed", "window_not_visible"}
# 破坏性动词表（从严：只有这些词或破坏性 acceptance 才授权破坏性工具；
# “整理”“收拾”等中性词不授权）。
_DESTRUCTIVE_VERBS = (
    "删除", "删掉", "清除", "清理", "卸载", "关闭", "结束", "杀掉",
    "delete", "uninstall", "kill", "trash",
)


class GoalController:
    """任务级目标控制器：由 Supervisor 构造，随任务生死（finally 清除）。"""

    def __init__(self, task, plan, verifier, ctx, *,
                 stagnation_rounds: int = 3,
                 time_budget_sec: float = 3600) -> None:
        self.task = task
        self.plan = plan
        self.verifier = verifier
        self.ctx = ctx
        self.stagnation_rounds = stagnation_rounds
        self.time_budget_sec = time_budget_sec
        self._start = time.monotonic()
        self._seen: set[str] = set()
        self._round_events: list[dict] = []
        self._no_progress_rounds = 0
        self._should_stop_calls = 0
        self._denial_streak = 0
        self._locked = False
        self._lock_reason = ""
        self._destructive_acceptance = self._compute_destructive_acceptance()

    # ---------- 覆盖判定 ----------

    def _compute_destructive_acceptance(self) -> bool:
        try:
            criteria = self.verifier._goal_acceptance_criteria(self.plan)
        except Exception:
            return False
        for c in (criteria or []):
            if not isinstance(c, dict):
                continue
            kind = c.get("kind") or c.get("type")
            if kind in _DESTRUCTIVE_KINDS:
                return True
        return False

    def _goal_text(self) -> str:
        return str(getattr(self.task, "goal", "") or "")

    def _destructive_covered(self) -> bool:
        text = self._goal_text()
        low = text.lower()
        if any(v in text or v in low for v in _DESTRUCTIVE_VERBS):
            return True
        return self._destructive_acceptance

    # ---------- veto（执行前） ----------

    def veto(self, func: str, args: dict) -> tuple[bool, str]:
        """返回 (vetoed, reason)。锁后 fail-closed：一切调用一律 veto。"""
        if self._locked:
            return True, self._lock_reason or "目标已锁定。"
        if func not in _DESTRUCTIVE_TOOLS:
            return False, ""
        if self._destructive_covered():
            return False, ""
        self._locked = True
        self._lock_reason = (
            f"目标偏离，已锁定：{func} 不在目标授权内"
            "（目标未含破坏性动词且验收标准无对应破坏性项）。"
        )
        return True, self._lock_reason

    # ---------- 事件（执行后 / 拒绝后） ----------

    def on_executed(self, func: str, args: dict, ok: bool) -> None:
        try:
            sig = json.dumps([func, args or {}], ensure_ascii=False,
                             sort_keys=True, default=str)
        except Exception:
            sig = f"{func}:{args}"
        self._round_events.append({"func": func, "ok": bool(ok), "sig": sig})
        # 任何一次真实执行都打断“连续拒绝”（失败执行亦然：不再是连续拒绝）。
        self._denial_streak = 0

    def on_denied(self, func: str, args: dict, reason: str) -> None:
        self._denial_streak += 1
        self._round_events.append({"func": func, "ok": False, "sig": ""})

    # ---------- 早停探针 ----------

    def _evaluate_achieved(self) -> bool:
        try:
            criteria = self.verifier._goal_acceptance_criteria(self.plan)
        except Exception:
            return False
        state = [c for c in (criteria or [])
                 if isinstance(c, dict) and c.get("type") not in ("readonly", "query")]
        if not state:
            return False
        for c in state:
            try:
                status, _detail = self.verifier._goal_check(c, self.ctx)
            except Exception:
                return False
            if status != "pass":
                return False
        return True

    # ---------- 每轮顶判定 ----------

    def should_stop(self) -> tuple[bool, str, str]:
        """返回 (stop, fate, reason)。每轮顶恰好调用一次。"""
        self._should_stop_calls += 1
        if self._should_stop_calls > 1:
            # 上一轮事件：任一 ok 且 (func,args) 首次出现即算进展。
            progress = False
            for e in self._round_events:
                if e["ok"] and e["sig"] and e["sig"] not in self._seen:
                    progress = True
                    self._seen.add(e["sig"])
            if progress:
                self._no_progress_rounds = 0
            else:
                self._no_progress_rounds += 1
        self._round_events = []
        # 1. 锁（veto 或连续 3 次安全拒绝）最优先。
        if self._locked or self._denial_streak >= 3:
            if self._denial_streak >= 3 and not self._locked:
                self._locked = True
                self._lock_reason = "目标偏离，已锁定：连续 3 次安全拒绝。"
            return True, "loop_goal_locked", self._lock_reason
        # 2. 达成（仅 state 探针全 pass；acceptance 为空永不触发）。
        if self._evaluate_achieved():
            return True, "loop_goal_achieved", "目标已达成，提前结束。"
        # 3. 停滞。
        if self._no_progress_rounds >= self.stagnation_rounds:
            return True, "loop_stalled", (
                f"连续 {self._no_progress_rounds} 轮无进展，已停止。"
            )
        # 4. 时间预算（熔断兜底，fate 复用 loop_exhausted，原因区分）。
        if self.time_budget_sec and (time.monotonic() - self._start) > self.time_budget_sec:
            return True, "loop_exhausted", (
                f"超出任务时间预算（{self.time_budget_sec} 秒），已停止。"
            )
        return False, "", ""
