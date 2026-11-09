"""执行历史 — 工具调用记录（方案 execution_history 表）。"""

from __future__ import annotations

import logging
from datetime import datetime

from .db import Database

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS execution_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT DEFAULT '',
    tool TEXT NOT NULL,
    success INTEGER DEFAULT 0,
    status TEXT DEFAULT '',
    verified INTEGER DEFAULT 0,
    error TEXT DEFAULT '',
    duration_ms INTEGER DEFAULT 0,
    source TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_exec_history_created
    ON execution_history(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_exec_history_task
    ON execution_history(task_id, id DESC);
"""


class ExecutionHistoryStore:
    """记录每次工具执行结果，供经验回溯与调试。"""

    def __init__(self, db: Database) -> None:
        self.db = db
        self._ensure()

    def _ensure(self) -> None:
        with self.db.connect() as conn:
            conn.executescript(_SCHEMA)
            conn.commit()

    def record(
        self,
        tool: str,
        *,
        task_id: str = "",
        success: bool = False,
        status: str = "",
        verified: bool | None = None,
        error: str = "",
        duration_ms: int = 0,
        source: str = "",
    ) -> None:
        try:
            ver = int(success if verified is None else bool(verified))
            with self.db.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO execution_history
                        (task_id, tool, success, status, verified, error,
                         duration_ms, source, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        (task_id or "")[:64],
                        (tool or "")[:80],
                        int(success),
                        (status or "")[:32],
                        ver,
                        (error or "")[:300],
                        max(0, int(duration_ms)),
                        (source or "")[:32],
                        datetime.now().isoformat(),
                    ),
                )
                conn.commit()
        except Exception:
            logger.debug("execution_history record failed", exc_info=True)

    def recent(self, *, limit: int = 20, task_id: str = "") -> list[dict]:
        limit = max(1, min(int(limit), 100))
        with self.db.connect() as conn:
            if task_id:
                rows = conn.execute(
                    """SELECT tool, success, status, verified, error,
                              duration_ms, source, created_at
                       FROM execution_history
                       WHERE task_id = ?
                       ORDER BY id DESC LIMIT ?""",
                    (task_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT tool, success, status, verified, error,
                              duration_ms, source, created_at, task_id
                       FROM execution_history
                       ORDER BY id DESC LIMIT ?""",
                    (limit,),
                ).fetchall()
        return [dict(r) for r in rows]

    def prune(self, keep: int = 2000) -> int:
        keep = max(0, int(keep))
        with self.db.connect() as conn:
            keep_ids = conn.execute(
                "SELECT id FROM execution_history ORDER BY id DESC LIMIT ?",
                (keep,),
            ).fetchall()
            if not keep_ids:
                if keep == 0:
                    cur = conn.execute("DELETE FROM execution_history")
                    conn.commit()
                    return cur.rowcount
                return 0
            min_id = min(r["id"] for r in keep_ids)
            cur = conn.execute(
                "DELETE FROM execution_history WHERE id < ?", (min_id,)
            )
            conn.commit()
            return cur.rowcount
