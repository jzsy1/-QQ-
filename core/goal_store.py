"""Goal 持久化 — goals 表。

v1（KEKESI_GOAL_STORE_V1）：照 kekesi/core/task_store.py 写。
运行时只依赖标准库（Database 仅作类型注解，TYPE_CHECKING 内导入），
以便探针按路径加载；生产调用方传 kekesi.memory.db.Database 实例。
"""

from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
import logging
from typing import Any, TYPE_CHECKING

from ..models.goal import Goal, GoalStatus

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ..memory.db import Database

_GOALS_SCHEMA = """
CREATE TABLE IF NOT EXISTS goals (
    id TEXT PRIMARY KEY,
    goal TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'user',
    status TEXT NOT NULL DEFAULT 'active',
    parent_id TEXT,
    priority INTEGER DEFAULT 0,
    deadline_at TEXT,
    acceptance_json TEXT DEFAULT '',
    last_task_id TEXT,
    last_task_status TEXT DEFAULT '',
    next_attempt_at TEXT,
    failure_count INTEGER DEFAULT 0,
    blocked_reason TEXT DEFAULT '',
    discovery_key TEXT DEFAULT '',
    discovery_evidence_json TEXT DEFAULT '',
    risk_level TEXT DEFAULT 'medium',
    dispatch_state TEXT DEFAULT 'approval_required',
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_goals_status
    ON goals(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_goals_parent
    ON goals(parent_id);
CREATE INDEX IF NOT EXISTS idx_goals_deadline
    ON goals(deadline_at);
CREATE TABLE IF NOT EXISTS autonomy_controls (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT DEFAULT (datetime('now'))
);
INSERT OR IGNORE INTO autonomy_controls(key,value) VALUES ('global_paused','1');
CREATE TABLE IF NOT EXISTS autonomy_dispatch_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id TEXT NOT NULL,
    task_id TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'reserved',
    error TEXT DEFAULT '',
    dispatched_at TEXT DEFAULT (datetime('now')),
    finished_at TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_autonomy_dispatch_time
    ON autonomy_dispatch_log(dispatched_at);
CREATE TABLE IF NOT EXISTS goal_action_authorizations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    action_hash TEXT NOT NULL,
    args_json TEXT NOT NULL DEFAULT '{}',
    authorization_note TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    authorized_at TEXT DEFAULT (datetime('now')),
    expires_at TEXT DEFAULT '',
    used_at TEXT DEFAULT '',
    used_by_task_id TEXT DEFAULT '',
    risk_level TEXT NOT NULL DEFAULT 'R2',
    rollback_plan TEXT DEFAULT '',
    execution_status TEXT NOT NULL DEFAULT 'pending',
    execution_success INTEGER,
    task_status TEXT DEFAULT '',
    verifier_confirmed INTEGER,
    completed_at TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_goal_action_auth_lookup
    ON goal_action_authorizations(goal_id, action_hash, status);
"""

# 失败计满即 BLOCKED（方案 §四）。
_BLOCK_THRESHOLD = 3

# save_topics 默认优先级约定（方案 §二）：user=10，sentinel/proactor=5，其余 0。
_PRIORITY_BY_SOURCE = {"user": 10, "sentinel": 5, "proactor": 5}


def _status_value(status: Any) -> str:
    v = getattr(status, "value", None)
    return str(v) if v is not None else str(status)


class GoalStore:
    """长期目标持久化与状态机。"""

    def __init__(self, db: Database) -> None:
        self.db = db
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with self.db.connect() as conn:
            conn.executescript(_GOALS_SCHEMA)
            columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(goals)")}
            for name, declaration in {
                "discovery_key": "TEXT DEFAULT ''",
                "discovery_evidence_json": "TEXT DEFAULT ''",
                "risk_level": "TEXT DEFAULT 'medium'",
                "dispatch_state": "TEXT DEFAULT 'approval_required'",
                "last_task_status": "TEXT DEFAULT ''",
                "next_attempt_at": "TEXT",
            }.items():
                if name not in columns:
                    conn.execute(f"ALTER TABLE goals ADD COLUMN {name} {declaration}")
            auth_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(goal_action_authorizations)")}
            for name, declaration in {
                "expires_at": "TEXT DEFAULT ''",
                "risk_level": "TEXT NOT NULL DEFAULT 'R2'",
                "rollback_plan": "TEXT DEFAULT ''",
                "execution_status": "TEXT NOT NULL DEFAULT 'pending'",
                "execution_success": "INTEGER",
                "task_status": "TEXT DEFAULT ''",
                "verifier_confirmed": "INTEGER",
                "completed_at": "TEXT DEFAULT ''",
            }.items():
                if name not in auth_columns:
                    conn.execute(f"ALTER TABLE goal_action_authorizations ADD COLUMN {name} {declaration}")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_goals_discovery_key ON goals(discovery_key) WHERE discovery_key != ''")
            conn.commit()

    def set_autonomy_paused(self, paused: bool) -> None:
        """Persist the global autonomy stop switch in the existing primary DB."""
        with self.db.connect() as conn:
            conn.execute(
                "INSERT INTO autonomy_controls(key,value,updated_at) VALUES('global_paused',?,datetime('now')) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                ("1" if paused else "0",),
            )
            conn.commit()

    def is_autonomy_paused(self) -> bool:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT value FROM autonomy_controls WHERE key='global_paused'"
            ).fetchone()
        return row is None or str(row[0]).strip().lower() not in {"0", "false", "off"}

    def reserve_autonomy_dispatch(self, goal_id: str, daily_limit: int = 5) -> bool:
        """Atomically claim one approved goal if the global daily budget allows it."""
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            paused = conn.execute(
                "SELECT value FROM autonomy_controls WHERE key='global_paused'"
            ).fetchone()
            if paused is None or str(paused[0]).strip().lower() not in {"0", "false", "off"}:
                conn.rollback()
                return False
            count = conn.execute(
                "SELECT COUNT(*) FROM autonomy_dispatch_log WHERE date(dispatched_at)=date('now')"
            ).fetchone()[0]
            if int(count) >= max(0, int(daily_limit)):
                conn.rollback()
                return False
            row = conn.execute(
                "SELECT status,dispatch_state,next_attempt_at FROM goals WHERE id=?",
                (goal_id,),
            ).fetchone()
            if (
                row is None or row[0] != "active" or row[1] != "approved"
                or (row[2] and str(row[2]) > datetime.now().isoformat(timespec="seconds"))
            ):
                conn.rollback()
                return False
            conn.execute("UPDATE goals SET dispatch_state='dispatching',updated_at=datetime('now') WHERE id=?", (goal_id,))
            conn.execute(
                "INSERT INTO autonomy_dispatch_log(goal_id,status) VALUES(?,'reserved')",
                (goal_id,),
            )
            conn.commit()
            return True

    def finish_autonomy_dispatch(self, goal_id: str, task_id: str = "", status: str = "", error: str = "") -> None:
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE autonomy_dispatch_log SET task_id=?,status=?,error=?,finished_at=datetime('now') "
                "WHERE id=(SELECT MAX(id) FROM autonomy_dispatch_log WHERE goal_id=?)",
                (str(task_id or ""), str(status or "unknown"), str(error or "")[:500], goal_id),
            )
            conn.commit()

    def link_autonomy_task(self, goal_id: str, task_id: str) -> bool:
        """Durably link a spawned runtime task before its supervisor can act."""
        if not goal_id or not task_id:
            return False
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "UPDATE goals SET last_task_id=?,last_task_status='running',updated_at=datetime('now') "
                "WHERE id=? AND status='active' AND dispatch_state='dispatching'",
                (str(task_id), str(goal_id)),
            )
            if cur.rowcount != 1:
                conn.rollback()
                return False
            conn.execute(
                "UPDATE autonomy_dispatch_log SET task_id=? WHERE id=("
                "SELECT MAX(id) FROM autonomy_dispatch_log WHERE goal_id=? AND status='reserved')",
                (str(task_id), str(goal_id)),
            )
            conn.commit()
            return True

    def reconcile_autonomy_dispatches(self, task_store: Any) -> int:
        """Resolve interrupted dispatch claims without blindly replaying them."""
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT g.id,g.last_task_id,l.id,l.task_id FROM goals g "
                "LEFT JOIN autonomy_dispatch_log l ON l.id=("
                "SELECT MAX(id) FROM autonomy_dispatch_log WHERE goal_id=g.id) "
                "WHERE g.status='active' AND g.dispatch_state='dispatching'"
            ).fetchall()
        fixed = 0
        for goal_id, linked_task_id, log_id, logged_task_id in rows:
            task_id = str(linked_task_id or logged_task_id or "")
            task = None
            if task_id:
                try:
                    task = task_store.get_task(task_id)
                except Exception:
                    logger.warning("自主目标恢复读取任务失败 goal_id=%s", goal_id, exc_info=True)
            if task is None:
                reason = "派发中断且无法确认任务状态，需人工复核；为避免重复执行，不自动重派"
                self.finish_autonomy_dispatch(str(goal_id), task_id, "unknown", reason)
                goal = self.get(str(goal_id))
                if goal is not None and goal.status == GoalStatus.ACTIVE:
                    goal.dispatch_state = "interrupted"
                    goal.blocked_reason = reason[:200]
                    self.save(goal)
                fixed += 1
                continue
            status = _status_value(getattr(task, "status", "unknown"))
            if status not in {"success", "failed", "timeout", "unknown", "cancelled"}:
                continue
            self.record_task(str(goal_id), task_id, status, error=getattr(task, "error", "") or "")
            self.finish_autonomy_dispatch(str(goal_id), task_id, status, getattr(task, "error", "") or "")
            fixed += 1
        return fixed

    @staticmethod
    def _action_hash(tool_name: str, args: dict) -> tuple[str, str]:
        args_json = json.dumps(args, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        payload = json.dumps([str(tool_name).strip(), args_json], ensure_ascii=False, separators=(",", ":"))
        fingerprints = {
            str(key): hashlib.sha256(
                json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
            ).hexdigest()
            for key, value in sorted(args.items(), key=lambda item: str(item[0]))
        }
        audit_json = json.dumps(fingerprints, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest(), audit_json

    def authorize_action(
        self, goal_id: str, tool_name: str, args: dict, note: str = "",
        rollback_plan: str = "",
    ) -> int | None:
        """Create exact one-use approval; R1 requires scope and recovery notes."""
        goal = self.get(goal_id)
        name = str(tool_name or "").strip()
        if goal is None or goal.is_terminal() or not name or not isinstance(args, dict):
            return None
        from .autonomy_policy import classify_autonomy_action

        risk = classify_autonomy_action(name, args)
        if risk == "R1" and (not str(note or "").strip() or not str(rollback_plan or "").strip()):
            return None
        action_hash, args_json = self._action_hash(name, args)
        with self.db.connect() as conn:
            cur = conn.execute(
                "INSERT INTO goal_action_authorizations("
                "goal_id,tool_name,action_hash,args_json,authorization_note,expires_at,risk_level,rollback_plan) "
                "VALUES(?,?,?,?,?,datetime('now','+10 minutes'),?,?)",
                (goal_id, name, action_hash, args_json, str(note or "")[:300], risk,
                 str(rollback_plan or "")[:500]),
            )
            conn.commit()
            return int(cur.lastrowid)

    def check_action_authorization(
        self,
        goal_id: str,
        tool_name: str,
        args: dict,
        *,
        task_id: str = "",
        consume: bool = False,
    ) -> bool:
        if not goal_id or not str(tool_name or "").strip() or not isinstance(args, dict):
            return False
        action_hash, _args_json = self._action_hash(tool_name, args)
        with self.db.connect() as conn:
            if consume:
                conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT id FROM goal_action_authorizations WHERE goal_id=? AND tool_name=? "
                "AND action_hash=? AND status='pending' "
                "AND (expires_at='' OR expires_at>datetime('now')) ORDER BY id LIMIT 1",
                (goal_id, str(tool_name).strip(), action_hash),
            ).fetchone()
            if row is None:
                if consume:
                    conn.rollback()
                return False
            if not consume:
                return True
            cur = conn.execute(
                "UPDATE goal_action_authorizations SET status='consumed',used_at=datetime('now'),"
                "used_by_task_id=?,execution_status='started' "
                "WHERE id=? AND status='pending'",
                (str(task_id or ""), int(row[0])),
            )
            conn.commit()
            return cur.rowcount == 1

    def record_action_results(
        self, task_id: str, tool_results: list[dict], evidence: dict, task_status: str,
    ) -> int:
        """Attach tool outcome and task verifier conclusion to consumed approvals."""
        if not task_id:
            return 0
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT id,tool_name,action_hash FROM goal_action_authorizations "
                "WHERE used_by_task_id=? AND execution_status='started'",
                (str(task_id),),
            ).fetchall()
            updated = 0
            for auth_id, tool_name, action_hash in rows:
                matching = None
                for item in tool_results or []:
                    if not isinstance(item, dict) or str(item.get("name") or item.get("func") or "") != str(tool_name):
                        continue
                    args = item.get("args") if isinstance(item.get("args"), dict) else {}
                    candidate_hash, _ = self._action_hash(str(tool_name), args)
                    if candidate_hash == action_hash:
                        matching = item
                        break
                success = matching.get("ok") if matching is not None else None
                action_status = "success" if success is True else "failed" if success is False else "unknown"
                verified = bool(isinstance(evidence, dict) and evidence.get("verified"))
                conn.execute(
                    "UPDATE goal_action_authorizations SET execution_status=?,execution_success=?,"
                    "task_status=?,verifier_confirmed=?,completed_at=datetime('now') WHERE id=?",
                    (action_status, None if success is None else int(bool(success)),
                     str(task_status or "unknown"), int(verified), int(auth_id)),
                )
                updated += 1
            conn.commit()
        return updated

    # -- 写入 --
    def save(self, goal: Goal) -> None:
        goal.touch()
        with self.db.connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO goals
                   (id, goal, source, status, parent_id, priority, deadline_at,
                    acceptance_json, last_task_id, last_task_status, next_attempt_at, failure_count, blocked_reason,
                    discovery_key, discovery_evidence_json, risk_level, dispatch_state,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (goal.id, goal.goal, goal.source, _status_value(goal.status),
                 goal.parent_id, goal.priority, goal.deadline_at,
                 goal.acceptance_json, goal.last_task_id, goal.last_task_status,
                 goal.next_attempt_at,
                 goal.failure_count,
                 goal.blocked_reason, goal.discovery_key, goal.discovery_evidence_json, goal.risk_level,
                 goal.dispatch_state, goal.created_at, goal.updated_at),
            )
            conn.commit()

    def save_topics(self, topics: list[str], source: str = "evolution") -> dict:
        """批量保存目标文本（Evolver goal_sink 入口）。去重：同文本 + 同 source
        已有 ACTIVE 或 BLOCKED 行 → 跳过；仅当全部同文行均为 DONE/CANCELLED 时新建。"""
        saved: list[str] = []
        skipped: list[str] = []
        for raw in topics or []:
            text = str(raw or "").strip()
            if not text:
                continue
            low = text.lower()
            if any(tag in low for tag in ("self_study:", "internal:", "task_history")):
                skipped.append(text)
                continue
            if source == "evolution" and any(bad in text for bad in ("失败原因与改进", "能力短板补齐")):
                skipped.append(text)
                continue
            if self._has_open(text, source):
                skipped.append(text)
                continue
            goal = Goal(goal=text, source=source,
                        priority=_PRIORITY_BY_SOURCE.get(source, 0))
            self.save(goal)
            saved.append(goal.id)
        return {"saved": saved, "skipped": skipped}

    def save_discovered(
        self, goal: Goal, discovery_key: str, *, cooldown_days: int = 1,
        allow_r0_auto_dispatch: bool = False,
    ) -> bool:
        """Persist one evidence-backed proposal per cooldown window.

        Active/blocked matches suppress rediscovery indefinitely; closed matches
        suppress it for the configured cooldown. A later recurrence gets a new
        discovery key so the audit history remains intact.
        """
        base_key = str(discovery_key or "")
        if not base_key:
            return False
        cooldown_days = max(0, int(cooldown_days))
        now = datetime.now()
        cutoff = (now - timedelta(days=cooldown_days)).isoformat(timespec="seconds")
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT status,created_at FROM goals WHERE substr(discovery_key,1,?)=? ORDER BY created_at DESC",
                (len(base_key) + 1, base_key + "#"),
            ).fetchall()
            if any(str(row[0]) in {"active", "blocked"} for row in rows):
                conn.rollback()
                return False
            if cooldown_days and any(str(row[1] or "") >= cutoff for row in rows):
                conn.rollback()
                return False
            # Cooldown query provides deduplication; a timestamp suffix permits a
            # new audited proposal as soon as the cooldown expires.
            bucket = int(now.timestamp())
            goal.discovery_key = f"{base_key}#{bucket}:{goal.id}"
            r0_evidence_valid = False
            if allow_r0_auto_dispatch and goal.risk_level == "R0":
                try:
                    evidence = json.loads(goal.discovery_evidence_json or "{}")
                    r0_evidence_valid = (
                        evidence.get("kind") == "environment_anomaly"
                        and "winops.check_system_health" in str(evidence.get("evidence") or "")
                    )
                except (TypeError, ValueError, AttributeError):
                    r0_evidence_valid = False
            goal.dispatch_state = (
                "approved" if r0_evidence_valid
                else "approval_required"
            )
            goal.touch()
            conn.execute(
                """INSERT INTO goals
                   (id,goal,source,status,parent_id,priority,deadline_at,acceptance_json,
                    last_task_id,last_task_status,next_attempt_at,failure_count,blocked_reason,
                    discovery_key,discovery_evidence_json,risk_level,dispatch_state,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (goal.id, goal.goal, goal.source, _status_value(goal.status), goal.parent_id,
                 goal.priority, goal.deadline_at, goal.acceptance_json, goal.last_task_id,
                 goal.last_task_status, goal.next_attempt_at, goal.failure_count,
                 goal.blocked_reason, goal.discovery_key, goal.discovery_evidence_json,
                 goal.risk_level, goal.dispatch_state,
                 goal.created_at, goal.updated_at),
            )
            conn.commit()
        return True

    def _has_open(self, text: str, source: str) -> bool:
        with self.db.connect() as conn:
            row = conn.execute(
                """SELECT id FROM goals
                   WHERE goal = ? AND source = ? AND status IN ('active', 'blocked')
                   LIMIT 1""",
                (text, source),
            ).fetchone()
        return row is not None

    # -- 状态 --
    def set_status(self, goal: Goal, status: GoalStatus) -> bool:
        """通用状态转移。终态（DONE/CANCELLED）不回迁；BLOCKED → ACTIVE 时清零失败计数。"""
        if goal.is_terminal():
            return False
        old = goal.status
        goal.status = status
        if old == GoalStatus.BLOCKED and status == GoalStatus.ACTIVE:
            goal.failure_count = 0
            goal.blocked_reason = ""
        self.save(goal)
        return True

    def set_blocked(self, goal: Goal, reason: str) -> bool:
        if goal.is_terminal():
            return False
        goal.status = GoalStatus.BLOCKED
        goal.blocked_reason = reason or ""
        self.save(goal)
        return True

    def cancel(self, goal: Goal) -> bool:
        if goal.status == GoalStatus.DONE or goal.status == GoalStatus.CANCELLED:
            return False
        goal.status = GoalStatus.CANCELLED
        self.save(goal)
        return True

    def record_task(self, goal_id: str, task_id: str, task_status: Any, error: str = "") -> bool:
        """任务终态 → 目标状态（方案 §四转移表）。CANCELLED 的任务不改动目标；
        非终态调用只记 last_task_id，不转移。"""
        goal = self.get(goal_id)
        if goal is None or goal.is_terminal():
            return False
        s = _status_value(task_status)
        if goal.last_task_id == task_id and goal.last_task_status == s:
            if goal.dispatch_state == "dispatching":
                if s == "success":
                    goal.dispatch_state = "dispatched"
                elif s == "cancelled":
                    goal.dispatch_state = "interrupted"
                elif goal.status == GoalStatus.BLOCKED:
                    goal.dispatch_state = "blocked"
                else:
                    goal.dispatch_state = "approved"
                self.save(goal)
            return False
        goal.last_task_id = task_id
        goal.last_task_status = s
        if s == "success":
            goal.status = GoalStatus.DONE
            goal.next_attempt_at = None
            goal.dispatch_state = "dispatched"
        elif s in ("failed", "timeout", "unknown"):
            goal.failure_count += 1
            if goal.failure_count >= _BLOCK_THRESHOLD:
                goal.status = GoalStatus.BLOCKED
                goal.dispatch_state = "blocked"
                goal.blocked_reason = (error or "").strip()[:200] or f"连续失败 {goal.failure_count} 次"
                goal.next_attempt_at = None
            else:
                goal.dispatch_state = "approved"
                delay = 10 if goal.failure_count == 1 else 60
                goal.next_attempt_at = (datetime.now() + timedelta(minutes=delay)).isoformat(timespec="seconds")
        elif s == "cancelled":
            goal.dispatch_state = "interrupted"
        self.save(goal)
        return True

    def record_task_result(self, task_id: str, task_status: Any, error: str = "") -> int:
        """Idempotently update goals linked to a completed runtime task."""
        if not task_id:
            return 0
        with self.db.connect() as conn:
            rows = conn.execute("SELECT id FROM goals WHERE last_task_id=?", (task_id,)).fetchall()
        return sum(bool(self.record_task(str(row[0]), task_id, task_status, error)) for row in rows)

    def reconcile(self, task_store: Any) -> int:
        """启动纠正：ACTIVE 目标的 last_task 已终态、但 goal 未更新时补一次转移。
        只碰 ACTIVE 行；BLOCKED/DONE/CANCELLED 不动。"""
        fixed = 0
        for goal in self.list_active(limit=1000):
            if not goal.last_task_id:
                continue
            try:
                task = task_store.get_task(goal.last_task_id)
            except Exception:
                continue
            if task is None:
                continue
            ts = _status_value(getattr(task, "status", ""))
            if ts in ("success", "failed", "timeout", "unknown", "cancelled"):
                if self.record_task(goal.id, goal.last_task_id, ts,
                                   error=getattr(task, "error", "") or ""):
                    fixed += 1
        return fixed

    # -- 读取 --
    def get(self, goal_id: str) -> Goal | None:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone()
        return self._row_to_goal(row) if row is not None else None

    def list_active(self, limit: int = 50, source: str | None = None) -> list[Goal]:
        sql = "SELECT * FROM goals WHERE status = 'active'"
        args: list[Any] = []
        if source is not None:
            sql += " AND source = ?"
            args.append(source)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        args.append(limit)
        with self.db.connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [self._row_to_goal(r) for r in rows]

    def list_due(self, now: str, limit: int = 50) -> list[Goal]:
        """到期目标：ACTIVE 且（无 deadline 或 deadline <= now）。
        排序：deadline ASC NULLS LAST、priority DESC、created_at ASC。"""
        with self.db.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM goals WHERE status = 'active'
                   AND (deadline_at IS NULL OR deadline_at <= ?)
                   ORDER BY (deadline_at IS NULL), deadline_at ASC,
                            priority DESC, created_at ASC
                   LIMIT ?""",
                (now, limit)).fetchall()
        return [self._row_to_goal(r) for r in rows]

    def children(self, parent_id: str) -> list[Goal]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM goals WHERE parent_id = ? ORDER BY created_at ASC",
                (parent_id,)).fetchall()
        return [self._row_to_goal(r) for r in rows]

    @staticmethod
    def _row_to_goal(row: Any) -> Goal:
        try:
            status = GoalStatus(row["status"])
        except ValueError:
            status = row["status"]  # 未知状态值不丢行；is_terminal 仍可用 str 比较
        return Goal(
            goal=row["goal"], id=row["id"], source=row["source"],
            status=status, parent_id=row["parent_id"], priority=row["priority"],
            deadline_at=row["deadline_at"], acceptance_json=row["acceptance_json"],
            last_task_id=row["last_task_id"],
            last_task_status=str(row["last_task_status"] or ""),
            next_attempt_at=row["next_attempt_at"],
            failure_count=row["failure_count"],
            blocked_reason=row["blocked_reason"], created_at=row["created_at"],
            discovery_key=str(row["discovery_key"] or ""),
            discovery_evidence_json=str(row["discovery_evidence_json"] or ""),
            risk_level=str(row["risk_level"] or "medium"),
            dispatch_state=str(row["dispatch_state"] or "approval_required"),
            updated_at=row["updated_at"],
        )
