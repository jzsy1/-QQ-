"""任务持久化 — runtime_tasks / task_steps。"""

from __future__ import annotations

import json
import logging
from typing import Any

from ..memory.db import Database
from ..models.action import Action
from ..models.plan import Plan
from ..models.task import Task, TaskStatus

logger = logging.getLogger(__name__)

_RUNTIME_TASKS_SCHEMA = """
CREATE TABLE IF NOT EXISTS runtime_tasks (
    id TEXT PRIMARY KEY,
    goal TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'created',
    session_id TEXT DEFAULT 'default',
    agent_id TEXT DEFAULT 'keks',
    result TEXT DEFAULT '',
    error TEXT DEFAULT '',
    layer TEXT DEFAULT '',
    reply TEXT DEFAULT '',
    plan_json TEXT DEFAULT '',
    tool_results_json TEXT DEFAULT '[]',
    completion_evidence_json TEXT DEFAULT '{}',
    deadline_at TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS task_steps (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    action_tool TEXT NOT NULL,
    action_params TEXT DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'created',
    attempt INTEGER DEFAULT 0,
    started_at TEXT,
    finished_at TEXT,
    result_json TEXT DEFAULT '',
    error TEXT DEFAULT '',
    FOREIGN KEY (task_id) REFERENCES runtime_tasks(id)
);

CREATE INDEX IF NOT EXISTS idx_runtime_tasks_status
    ON runtime_tasks(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_task_steps_task
    ON task_steps(task_id, started_at);
"""


class TaskStore:
  """运行时任务持久化与恢复。"""

  _INCOMPLETE = (
    TaskStatus.CREATED.value,
    TaskStatus.PLANNING.value,
    TaskStatus.READY.value,
    TaskStatus.EXECUTING.value,
    TaskStatus.VERIFYING.value,
    TaskStatus.RECOVERING.value,
  )

  def __init__(self, db: Database) -> None:
    self.db = db
    self._ensure_schema()

  def _ensure_schema(self) -> None:
    with self.db.connect() as conn:
      conn.executescript(_RUNTIME_TASKS_SCHEMA)
      cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(runtime_tasks)").fetchall()}
      if "completion_evidence_json" not in cols:
        conn.execute("ALTER TABLE runtime_tasks ADD COLUMN completion_evidence_json TEXT DEFAULT '{}' ")
      conn.executescript("""
      CREATE TABLE IF NOT EXISTS task_feedback_outbox (
        task_id TEXT PRIMARY KEY,
        payload_json TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now')),
        updated_at TEXT DEFAULT (datetime('now'))
      );
      CREATE INDEX IF NOT EXISTS idx_task_feedback_status ON task_feedback_outbox(status, created_at);
      """)
      conn.execute("PRAGMA journal_mode=WAL")
      conn.execute("PRAGMA busy_timeout=5000")
      conn.commit()

  def save(self, task: Task, plan: Plan | None = None, *, feedback_payload: dict[str, Any] | None = None) -> None:
    plan_json = ""
    if plan is not None:
      plan_json = json.dumps(
        {
          "id": plan.id,
          "summary": plan.summary,
          "source": plan.source,
          "acceptance": plan.acceptance,
          "actions": [
            {
              "id": a.id,
              "tool": a.tool,
              "parameters": a.parameters,
              "timeout": a.timeout,
              "risk_level": a.risk_level,
              "verification": a.verification,  # Q4_TASK_GOAL_VERIFY_V1
            }
            for a in plan.actions
          ],
        },
        ensure_ascii=False,
      )
    with self.db.connect() as conn:
      conn.execute(
        """
        INSERT INTO runtime_tasks (
            id, goal, status, session_id, agent_id, result, error, layer,
            reply, plan_json, tool_results_json, completion_evidence_json, deadline_at, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            status=excluded.status,
            result=excluded.result,
            error=excluded.error,
            layer=excluded.layer,
            reply=excluded.reply,
            plan_json=excluded.plan_json,
            tool_results_json=excluded.tool_results_json,
            completion_evidence_json=excluded.completion_evidence_json,
            deadline_at=excluded.deadline_at,
            updated_at=excluded.updated_at
        """,
        (
          task.id,
          task.goal,
          task.status.value,
          task.session_id,
          task.agent_id,
          json.dumps(task.result, ensure_ascii=False) if task.result is not None else "",
          task.error or "",
          task.layer or "",
          task.reply or "",
          plan_json,
          json.dumps(task.tool_results or [], ensure_ascii=False),
          json.dumps(task.completion_evidence or {}, ensure_ascii=False),
          task.deadline_at,
          task.created_at,
          task.updated_at,
        ),
      )
      if feedback_payload is not None:
        conn.execute(
          "INSERT OR IGNORE INTO task_feedback_outbox(task_id, payload_json) VALUES (?, ?)",
          (str(feedback_payload["task_id"]), json.dumps(feedback_payload, ensure_ascii=False, default=str)),
        )
      conn.commit()

  def enqueue_feedback(self, payload: dict[str, Any]) -> None:
    """Durably enqueue a terminal task feedback event; one row per task."""
    with self.db.connect() as conn:
      conn.execute(
        "INSERT OR IGNORE INTO task_feedback_outbox(task_id, payload_json) VALUES (?, ?)",
        (str(payload["task_id"]), json.dumps(payload, ensure_ascii=False, default=str)),
      )
      conn.commit()

  def pending_feedback(self, limit: int = 100) -> list[dict[str, Any]]:
    with self.db.connect() as conn:
      rows = conn.execute(
        "SELECT task_id, payload_json FROM task_feedback_outbox WHERE status IN ('pending','processing','failed') ORDER BY created_at LIMIT ?",
        (int(limit),),
      ).fetchall()
      conn.execute("UPDATE task_feedback_outbox SET status='pending' WHERE status='processing'")
      conn.commit()
    result = []
    for row in rows:
      try:
        result.append(json.loads(row["payload_json"]))
      except (TypeError, json.JSONDecodeError):
        logger.warning("忽略无法解析的任务反馈载荷 task_id=%s", row["task_id"])
    return result

  def set_feedback_status(self, task_id: str, status: str, error: str = "") -> None:
    with self.db.connect() as conn:
      conn.execute(
        "UPDATE task_feedback_outbox SET status=?, attempts=attempts+CASE WHEN ?='failed' THEN 1 ELSE 0 END, last_error=?, updated_at=datetime('now') WHERE task_id=?",
        (status, status, error[:1000], task_id),
      )
      conn.commit()

  def set_status(self, task: Task, status: TaskStatus) -> None:
    task.status = status
    task.touch()
    with self.db.connect() as conn:
      conn.execute(
        "UPDATE runtime_tasks SET status=?, updated_at=?, error=? WHERE id=?",
        (status.value, task.updated_at, task.error or "", task.id),
      )
      conn.commit()

  def add_step(self, task_id: str, action: Action, *, status: str = "created") -> None:
    with self.db.connect() as conn:
      conn.execute(
        """
        INSERT OR REPLACE INTO task_steps
            (id, task_id, action_tool, action_params, status, attempt)
        VALUES (?, ?, ?, ?, ?, 0)
        """,
        (
          action.id,
          task_id,
          action.tool,
          json.dumps(action.parameters or {}, ensure_ascii=False),
          status,
        ),
      )
      conn.commit()

  def update_step(
    self,
    action_id: str,
    *,
    status: str,
    result: dict | None = None,
    error: str = "",
    attempt: int | None = None,
  ) -> None:
    from datetime import datetime

    now = datetime.now().isoformat()
    fields = ["status=?", "finished_at=?"]
    values: list[Any] = [status, now]
    if result is not None:
      fields.append("result_json=?")
      values.append(json.dumps(result, ensure_ascii=False))
    if error:
      fields.append("error=?")
      values.append(error)
    if attempt is not None:
      fields.append("attempt=?")
      values.append(attempt)
    values.append(action_id)
    with self.db.connect() as conn:
      conn.execute(
        f"UPDATE task_steps SET {', '.join(fields)} WHERE id=?",
        tuple(values),
      )
      conn.commit()

  def get_task(self, task_id: str) -> Task | None:
    with self.db.connect() as conn:
      row = conn.execute(
        "SELECT * FROM runtime_tasks WHERE id=?",
        (task_id,),
      ).fetchone()
    if not row:
      return None
    return self._row_to_task(row)

  def list_incomplete(self, limit: int = 50) -> list[Task]:
    placeholders = ",".join("?" for _ in self._INCOMPLETE)
    with self.db.connect() as conn:
      rows = conn.execute(
        f"""
        SELECT * FROM runtime_tasks
        WHERE status IN ({placeholders})
        ORDER BY updated_at ASC
        LIMIT ?
        """,
        (*self._INCOMPLETE, limit),
      ).fetchall()
    return [self._row_to_task(r) for r in rows]

  def completed_step_ids(self, task_id: str) -> set[str]:
    with self.db.connect() as conn:
      rows = conn.execute(
        """
        SELECT id FROM task_steps
        WHERE task_id=? AND status IN ('success', 'completed')
        """,
        (task_id,),
      ).fetchall()
    return {str(r["id"]) for r in rows}

  def mark_recovered_failed(self, task_id: str, reason: str) -> None:
    # Q3_TASK_RESUME_V1: only fail incomplete tasks; never overwrite
    # a terminal status (race: task finished between list_incomplete
    # and mark_recovered_failed). _INCOMPLETE = created/planning/
    # ready/executing/verifying/recovering.
    with self.db.connect() as conn:
      conn.execute(
        """
        UPDATE runtime_tasks
        SET status=?, error=?, updated_at=datetime('now')
        WHERE id=? AND status IN (?,?,?,?,?,?)
        """,
        (TaskStatus.FAILED.value, reason, task_id, *self._INCOMPLETE),
      )
      conn.commit()

  def _row_to_task(self, row) -> Task:
    tool_results = []
    try:
      tool_results = json.loads(row["tool_results_json"] or "[]")
    except json.JSONDecodeError:
      pass
    try:
      completion_evidence = json.loads(row["completion_evidence_json"] or "{}")
    except (KeyError, TypeError, json.JSONDecodeError):
      completion_evidence = {}
    status_raw = row["status"] or TaskStatus.CREATED.value
    try:
      status = TaskStatus(status_raw)
    except ValueError:
      status = TaskStatus.UNKNOWN
    return Task(
      id=row["id"],
      goal=row["goal"] or "",
      status=status,
      session_id=row["session_id"] or "default",
      agent_id=row["agent_id"] or "keks",
      result=row["result"] or None,
      error=row["error"] or "",
      layer=row["layer"] or "",
      reply=row["reply"] or None,
      tool_results=tool_results,
      completion_evidence=completion_evidence,
      deadline_at=row["deadline_at"],
      created_at=row["created_at"] or "",
      updated_at=row["updated_at"] or "",
    )

  def load_plan(self, task_id: str) -> Plan | None:
    with self.db.connect() as conn:
      row = conn.execute(
        "SELECT plan_json FROM runtime_tasks WHERE id=?",
        (task_id,),
      ).fetchone()
    if not row or not row["plan_json"]:
      return None
    try:
      data = json.loads(row["plan_json"])
    except json.JSONDecodeError:
      return None
    actions = [
      Action(
        id=a.get("id") or "",
        tool=a.get("tool") or "",
        parameters=dict(a.get("parameters") or {}),
        timeout=float(a.get("timeout") or 30),
        risk_level=int(a.get("risk_level") or 0),
        verification=a.get("verification"),
      )
      for a in data.get("actions") or []
    ]
    return Plan(
      id=data.get("id") or "",
      summary=data.get("summary") or "",
      source=data.get("source") or "planner",
      acceptance=[a for a in (data.get("acceptance") or []) if isinstance(a, dict)],
      actions=actions,
    )
