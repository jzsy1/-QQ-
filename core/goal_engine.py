"""Evidence-backed goal discovery and explicitly gated dispatch."""

from __future__ import annotations

import logging
import json
import re
import threading
import time
from collections import deque
from datetime import date, datetime, timedelta
from typing import Any, Callable

from ..models.goal import Goal

logger = logging.getLogger(__name__)


class GoalEvaluator:
    """Classify opportunity value and risk; discovery never grants permission."""

    @staticmethod
    def evaluate(kind: str, *, confidence: float = 1.0, evidence: str = "") -> dict[str, Any]:
        if confidence < 0.6 or not evidence.strip():
            return {"value": "low", "risk_level": "high", "dispatch_state": "rejected"}
        if kind == "due_commitment":
            return {"value": "high", "risk_level": "high", "dispatch_state": "approval_required"}
        if kind == "capability_degraded":
            state = "BROKEN" if "BROKEN" in evidence.upper() else "DEGRADED"
            return {"value": "high" if state == "BROKEN" else "medium", "risk_level": "medium", "dispatch_state": "approval_required"}
        if kind == "interrupted_task":
            return {"value": "medium", "risk_level": "high", "dispatch_state": "approval_required"}
        if kind == "environment_anomaly":
            # The candidate is scoped to read-only diagnosis. Tool-level risk
            # checks still gate every action when Supervisor executes it.
            return {"value": "medium", "risk_level": "R0", "dispatch_state": "approved"}
        return {"value": "low", "risk_level": "high", "dispatch_state": "approval_required"}


class GoalDriver:
    """Periodic opportunity scanner; execution requires a separate explicit gate.

    Discovery is disabled unless configured. Dispatch additionally requires both
    ``enabled`` and ``dispatch_enabled`` and an explicitly approved goal.
    """

    def __init__(
        self,
        db: Any,
        goal_store: Any,
        task_store: Any,
        runtime: Any,
        *,
        enabled: bool = False,
        dispatch_enabled: bool = False,
        interval_sec: int = 300,
        daily_budget: int = 5,
        task_timeout_sec: int = 120,
        max_tool_calls: int = 5,
        capability_provider: Callable[[], list[dict]] | None = None,
        cognition_db_path: str | None = None,
        load_provider: Callable[[], dict[str, Any]] | None = None,
        environment_provider: Callable[[], dict[str, Any]] | None = None,
        busy_request_threshold: int = 5,
        busy_window_sec: int = 60,
    ) -> None:
        self.db = db
        self.goal_store = goal_store
        self.task_store = task_store
        self.runtime = runtime
        self.enabled = bool(enabled)
        self.dispatch_enabled = bool(dispatch_enabled)
        self.interval_sec = max(60, int(interval_sec))
        self.daily_budget = max(0, int(daily_budget))
        self.task_timeout_sec = max(30, int(task_timeout_sec))
        self.max_tool_calls = max(0, int(max_tool_calls))
        self.capability_provider = capability_provider or self._broken_capabilities
        self.cognition_db_path = cognition_db_path
        self._shared_health_probe = load_provider is None and environment_provider is None
        self.load_provider = load_provider or self._system_load
        self.environment_provider = environment_provider or self._system_load
        self.busy_request_threshold = max(1, int(busy_request_threshold))
        self.busy_window_sec = max(10, int(busy_window_sec))
        self._stop = threading.Event()
        self._state_lock = threading.RLock()
        self._active_user_requests = 0
        self._user_request_times: deque[float] = deque()
        self._last_admission_reason = "clear"
        self._last_load_snapshot: dict[str, Any] = {}
        self._active_autonomous_tasks: dict[str, threading.Event] = {}
        self._thread: threading.Thread | None = None
        self._external_scheduler = False
        self._last_external_scan = 0.0
        if self.enabled:
            self._thread = threading.Thread(target=self._run, name="goal-driver", daemon=True)
            self._thread.start()

    def _broken_capabilities(self) -> list[dict]:
        from ..memory.cognition import connect, default_db_path

        conn = connect(self.cognition_db_path or default_db_path())
        try:
            rows = conn.execute(
                "SELECT key,health_state,evidence_json FROM self_model "
                "WHERE key LIKE 'capability_health:%' AND health_state IN ('BROKEN','DEGRADED')"
            ).fetchall()
            return [
                {"tool_name": str(row[0]).split(":", 1)[-1], "state": str(row[1]), "evidence": row[2]}
                for row in rows
            ]
        finally:
            conn.close()

    def discover(self, *, today: str | None = None) -> list[Goal]:
        """Persist deterministic proposals from commitments, health and stale tasks."""
        if self.goal_store.is_autonomy_paused() or self._admission_throttled():
            return []
        current_day = today or date.today().isoformat()
        found: list[Goal] = []
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT id,fact,due_date,confidence FROM auto_facts "
                "WHERE category='promise' AND status='open' AND due_date IS NOT NULL "
                "AND date(due_date)<=date(?) AND COALESCE(confidence,0)>=0.6 "
                "ORDER BY due_date ASC LIMIT 100",
                (current_day,),
            ).fetchall()
        for row in rows:
            fact_id, fact, due_date, _confidence = row
            text = f"跟进已到期承诺：{str(fact).strip()}"
            found.extend(self._persist(text, "due_commitment", f"promise:{fact_id}", str(due_date), confidence=float(_confidence or 0), evidence=f"fact_id={fact_id}; due={due_date}"))

        for item in self.capability_provider() or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("tool_name") or item.get("key") or "").replace("capability_health:", "").strip()
            state = str(item.get("state") or item.get("health_state") or "").upper()
            if not name or state not in {"BROKEN", "DEGRADED"}:
                continue
            found.extend(self._persist(
                f"诊断能力 {name} 当前状态 {state}，先收集只读诊断信息并提出修复建议",
                "capability_degraded", f"capability:{name}:{state}", None,
                evidence=f"capability={name}; state={state}; source=self_model",
            ))

        try:
            environment = (
                dict(self._last_load_snapshot) if self._shared_health_probe
                else (self.environment_provider() or {})
            )
        except Exception:
            logger.debug("自主目标环境探针失败", exc_info=True)
            environment = {}
        if isinstance(environment, dict):
            for issue in environment.get("issues") or []:
                detail = str(issue or "").strip()
                if not detail:
                    continue
                normalized = detail.split("(", 1)[0].strip().lower()
                category = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "_", normalized).strip("_")[:80]
                if not category:
                    continue
                evidence = json.dumps({
                    "issue": detail,
                    "cpu_pct": environment.get("cpu_pct"),
                    "memory_pct": environment.get("memory_pct"),
                    "disk_pct": environment.get("disk_pct"),
                    "source": "winops.check_system_health",
                }, ensure_ascii=False, sort_keys=True)
                found.extend(self._persist(
                    f"系统健康异常待诊断：{detail}；先收集只读诊断信息并提出处理建议",
                    "environment_anomaly", f"environment:{category}", None,
                    evidence=evidence,
                ))

        for task in self.task_store.list_incomplete(limit=100):
            updated = str(getattr(task, "updated_at", "") or "")
            if not self._is_stale(updated, current_day):
                continue
            task_id = str(getattr(task, "id", "") or "")
            if not task_id:
                continue
            found.extend(self._persist(
                f"检查中断任务 {task_id} 的状态并确定后续处理方式",
                "interrupted_task", f"task:{task_id}", None,
                evidence=f"task_id={task_id}; updated_at={updated}; source=task_store",
            ))
        return found

    @staticmethod
    def _is_stale(updated_at: str, today: str) -> bool:
        if not updated_at:
            return True
        try:
            from datetime import datetime, timedelta

            value = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
            now = datetime.now(value.tzinfo) if value.tzinfo else datetime.now()
            return value <= now - timedelta(minutes=15)
        except (TypeError, ValueError):
            return updated_at[:10] < today

    def _persist(self, text: str, kind: str, key: str, deadline: str | None,
                 *, confidence: float = 1.0, evidence: str = "") -> list[Goal]:
        evaluation = GoalEvaluator.evaluate(kind, confidence=confidence, evidence=evidence)
        if evaluation["dispatch_state"] == "rejected":
            return []
        goal = Goal(
            goal=text, source="proactor", priority=10 if evaluation["value"] == "high" else 5,
            deadline_at=deadline, discovery_key=key, risk_level=evaluation["risk_level"],
            dispatch_state=evaluation["dispatch_state"],
            discovery_evidence_json=json.dumps({
                "kind": kind, "source_key": key, "confidence": float(confidence),
                "evidence": evidence, "value": evaluation["value"],
                "recorded_at": datetime.now().isoformat(timespec="seconds"),
            }, ensure_ascii=False, sort_keys=True),
        )
        try:
            return [goal] if self.goal_store.save_discovered(
                goal, key,
                allow_r0_auto_dispatch=(
                    evaluation["dispatch_state"] == "approved" and evaluation["risk_level"] == "R0"
                ),
            ) else []
        except Exception:
            logger.warning("自主目标候选保存失败 key=%s", key, exc_info=True)
            return []

    def scan_once(self) -> dict[str, int]:
        if self.goal_store.is_autonomy_paused():
            return {"discovered": 0, "dispatched": 0}
        discovered = self.discover()
        dispatched = self.dispatch_approved() if self.dispatch_enabled else 0
        return {"discovered": len(discovered), "dispatched": dispatched}

    def use_external_scheduler(self) -> None:
        """Hand periodic polling to Proactor without running two scan loops."""
        self._external_scheduler = True
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)

    def scan_if_due(self) -> dict[str, int] | None:
        """Run one scan on an external host, respecting the configured interval."""
        if not self.enabled or not self._external_scheduler:
            return None
        now = time.monotonic()
        with self._state_lock:
            if now - self._last_external_scan < self.interval_sec:
                return None
            self._last_external_scan = now
        return self.scan_once()

    def dispatch_approved(self) -> int:
        if (
            not (self.enabled and self.dispatch_enabled)
            or self.goal_store.is_autonomy_paused()
            or self._admission_throttled()
        ):
            return 0
        # Explicit approval is required even when autonomous dispatch is enabled.
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT id FROM goals WHERE status='active' AND dispatch_state='approved' "
                "AND (next_attempt_at IS NULL OR julianday(next_attempt_at)<=julianday('now')) "
                "ORDER BY priority DESC,created_at ASC LIMIT 10"
            ).fetchall()
        dispatched = 0
        from .goal_spawn import spawn_goal_task

        for row in rows:
            if self.user_request_active:
                break
            goal_id = str(row[0])
            if not self.goal_store.reserve_autonomy_dispatch(goal_id, self.daily_budget):
                continue
            goal = self.goal_store.get(goal_id)
            if goal is None:
                continue
            # Persist the claim before invoking Runtime: a crash after task creation
            # must not cause this goal to spawn the same side effect again.
            goal.dispatch_state = "dispatching"
            self.goal_store.save(goal)
            deadline_at = (datetime.now() + timedelta(seconds=self.task_timeout_sec)).isoformat()
            try:
                task = spawn_goal_task(
                    self.runtime, self.goal_store, goal,
                    deadline_at=deadline_at, max_tool_calls=self.max_tool_calls,
                    autonomous=True, goal_id=goal.id,
                )
                task_status = str(getattr(getattr(task, "status", ""), "value", getattr(task, "status", "unknown")))
                self.goal_store.finish_autonomy_dispatch(
                    goal.id, str(getattr(task, "id", "") or ""),
                    task_status,
                    str(getattr(task, "error", "") or ""),
                )
                goal = self.goal_store.get(goal.id) or goal
                if task_status == "success":
                    goal.dispatch_state = "dispatched"
                elif task_status in {"failed", "timeout", "unknown"}:
                    goal.dispatch_state = "approved"
                elif task_status == "cancelled":
                    goal.dispatch_state = "interrupted"
                else:
                    goal.dispatch_state = "dispatch_error"
                self.goal_store.save(goal)
                if task_status in {"success", "failed", "timeout", "unknown", "cancelled"}:
                    dispatched += 1
            except Exception:
                self.goal_store.finish_autonomy_dispatch(goal.id, status="dispatch_error", error="runtime dispatch raised")
                goal = self.goal_store.get(goal.id) or goal
                goal.dispatch_state = "dispatch_error"
                self.goal_store.save(goal)
                logger.warning("自主目标派发失败 goal_id=%s", goal.id, exc_info=True)
        return dispatched

    def approve(self, goal_id: str) -> bool:
        goal = self.goal_store.get(goal_id)
        if goal is None or goal.status.value != "active":
            return False
        goal.dispatch_state = "approved"
        self.goal_store.save(goal)
        return True

    def authorize_action(
        self, goal_id: str, tool_name: str, args: dict, note: str = "",
        rollback_plan: str = "",
    ) -> int | None:
        """Record explicit, exact and one-use approval for a concrete action."""
        return self.goal_store.authorize_action(goal_id, tool_name, args, note, rollback_plan)

    def pause(self) -> None:
        self.goal_store.set_autonomy_paused(True)

    def resume(self) -> None:
        self.goal_store.set_autonomy_paused(False)

    @property
    def user_request_active(self) -> bool:
        with self._state_lock:
            return self._active_user_requests > 0

    def begin_user_request(self) -> None:
        """Give foreground work priority and request cooperative cancellation."""
        with self._state_lock:
            self._active_user_requests += 1
            now = time.monotonic()
            self._user_request_times.append(now)
            self._prune_user_request_times(now)
            for event in self._active_autonomous_tasks.values():
                event.set()

    def end_user_request(self) -> None:
        with self._state_lock:
            self._active_user_requests = max(0, self._active_user_requests - 1)

    def _prune_user_request_times(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        cutoff = now - self.busy_window_sec
        while self._user_request_times and self._user_request_times[0] < cutoff:
            self._user_request_times.popleft()

    def _system_load(self) -> dict[str, Any]:
        """Reuse the existing read-only Windows health probe; unknown load is neutral."""
        try:
            from ..tools import winops

            ok, raw = winops.check_system_health()
            return json.loads(raw) if ok else {}
        except Exception:
            logger.debug("自主目标负载探针不可用", exc_info=True)
            return {}

    def _admission_throttled(self) -> bool:
        with self._state_lock:
            now = time.monotonic()
            self._prune_user_request_times(now)
            user_busy = (
                self._active_user_requests > 0
                or len(self._user_request_times) >= self.busy_request_threshold
            )
            user_busy_reason = (
                "user_request_active" if self._active_user_requests > 0 else "user_request_burst"
            )
        if user_busy:
            with self._state_lock:
                self._last_admission_reason = user_busy_reason
            return True
        try:
            load = self.load_provider() or {}
            if not isinstance(load, dict):
                with self._state_lock:
                    self._last_load_snapshot = {}
                    self._last_admission_reason = "clear"
                return False
            cpu = float(load.get("cpu_pct", load.get("cpu_percent", 0)) or 0)
            memory = float(load.get("memory_pct", load.get("memory_percent", 0)) or 0)
            throttled = cpu >= 90 or memory >= 90
            with self._state_lock:
                self._last_load_snapshot = dict(load)
                self._last_admission_reason = "high_system_load" if throttled else "clear"
            return throttled
        except (TypeError, ValueError, Exception):
            # A failed/unknown probe must not fabricate a high-load condition.
            logger.debug("自主目标负载读取失败", exc_info=True)
            with self._state_lock:
                self._last_load_snapshot = {}
                self._last_admission_reason = "clear"
            return False

    def register_autonomous_task(self, task_id: str) -> threading.Event:
        event = threading.Event()
        with self._state_lock:
            if self._active_user_requests:
                event.set()
            self._active_autonomous_tasks[str(task_id)] = event
        return event

    def unregister_autonomous_task(self, task_id: str) -> None:
        with self._state_lock:
            self._active_autonomous_tasks.pop(str(task_id), None)

    def status(self) -> dict[str, Any]:
        with self.db.connect() as conn:
            counts = {
                str(row[0]): int(row[1])
                for row in conn.execute(
                    "SELECT dispatch_state,COUNT(*) FROM goals WHERE status='active' GROUP BY dispatch_state"
                ).fetchall()
            }
            by_status = {
                str(row[0]): int(row[1])
                for row in conn.execute(
                    "SELECT status,COUNT(*) FROM goals GROUP BY status"
                ).fetchall()
            }
            by_risk = {
                str(row[0]): int(row[1])
                for row in conn.execute(
                    "SELECT risk_level,COUNT(*) FROM goals WHERE status='active' GROUP BY risk_level"
                ).fetchall()
            }
            dispatches_today = int(conn.execute(
                "SELECT COUNT(*) FROM autonomy_dispatch_log WHERE date(dispatched_at)=date('now')"
            ).fetchone()[0])
            recent_failures = [
                {"goal_id": str(row[0]), "status": str(row[1]), "reason": str(row[2] or "")[:240],
                 "at": str(row[3] or "")}
                for row in conn.execute(
                    "SELECT goal_id,status,error,dispatched_at FROM autonomy_dispatch_log "
                    "WHERE status IN ('failed','timeout','unknown','dispatch_error') "
                    "ORDER BY id DESC LIMIT 5"
                ).fetchall()
            ]
            pending_action_authorizations = int(conn.execute(
                "SELECT COUNT(*) FROM goal_action_authorizations WHERE status='pending' "
                "AND (expires_at='' OR expires_at>datetime('now'))"
            ).fetchone()[0])
            recent_action_audits = [
                {
                    "goal_id": str(row[0]), "task_id": str(row[1] or ""),
                    "tool_name": str(row[2]), "risk_level": str(row[3]),
                    "execution_status": str(row[4]), "execution_success": row[5],
                    "task_status": str(row[6] or ""),
                    "verifier_confirmed": row[7],
                    "rollback_plan": str(row[8] or ""),
                    "completed_at": str(row[9] or ""),
                }
                for row in conn.execute(
                    "SELECT goal_id,used_by_task_id,tool_name,risk_level,execution_status,"
                    "execution_success,task_status,verifier_confirmed,rollback_plan,completed_at "
                    "FROM goal_action_authorizations WHERE status='consumed' "
                    "ORDER BY id DESC LIMIT 10"
                ).fetchall()
            ]
        with self._state_lock:
            active_autonomous_tasks = len(self._active_autonomous_tasks)
            active_user_requests = self._active_user_requests
            self._prune_user_request_times()
            recent_user_requests = len(self._user_request_times)
            last_admission_reason = self._last_admission_reason
            last_load_snapshot = dict(self._last_load_snapshot)
        return {
            "enabled": self.enabled,
            "dispatch_enabled": self.dispatch_enabled,
            "paused": self.goal_store.is_autonomy_paused(),
            "active_goals_by_dispatch_state": counts,
            "goals_by_status": by_status,
            "active_goals_by_risk": by_risk,
            "autonomous_dispatches_today": dispatches_today,
            "recent_dispatch_failures": recent_failures,
            "active_autonomous_tasks": active_autonomous_tasks,
            "active_user_requests": active_user_requests,
            "user_requests_in_window": recent_user_requests,
            "busy_request_threshold": self.busy_request_threshold,
            "last_admission_reason": last_admission_reason,
            "last_load_snapshot": last_load_snapshot,
            "pending_action_authorizations": pending_action_authorizations,
            "recent_action_audits": recent_action_audits,
        }

    def _run(self) -> None:
        while not self._stop.wait(self.interval_sec):
            try:
                self.scan_once()
            except Exception:
                logger.warning("自主目标巡检失败", exc_info=True)

    def close(self) -> None:
        self._stop.set()
        with self._state_lock:
            for event in self._active_autonomous_tasks.values():
                event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
