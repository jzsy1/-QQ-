"""审计日志 — 高风险操作记录（不含密钥）。"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_AUDIT_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT DEFAULT '',
    tool TEXT NOT NULL,
    action TEXT DEFAULT '',
    params_summary TEXT DEFAULT '',
    permission_level INTEGER DEFAULT 0,
    success INTEGER DEFAULT 0,
    verified INTEGER DEFAULT 0,
    error TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at DESC);
"""

_SENSITIVE_KEYS = frozenset({
    "password", "api_key", "token", "secret", "key_material",
})


def _sanitize_params(params: dict | None) -> str:
    if not params:
        return ""
    safe = {}
    for k, v in params.items():
        if str(k).lower() in _SENSITIVE_KEYS:
            safe[k] = "***"
        else:
            text = str(v)
            safe[k] = text[:120] + ("…" if len(text) > 120 else "")
    return json.dumps(safe, ensure_ascii=False)[:500]


class AuditLog:
    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure()

    def _ensure(self) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.executescript(_AUDIT_SCHEMA)
            conn.commit()

    def record(
        self,
        tool: str,
        *,
        task_id: str = "",
        action: str = "",
        params: dict | None = None,
        permission_level: int = 0,
        success: bool = False,
        verified: bool = False,
        error: str = "",
    ) -> None:
        try:
            with sqlite3.connect(self.path) as conn:
                conn.execute(
                    """
                    INSERT INTO audit_log
                        (task_id, tool, action, params_summary, permission_level,
                         success, verified, error, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task_id,
                        tool,
                        action or tool,
                        _sanitize_params(params),
                        permission_level,
                        int(success),
                        int(verified),
                        (error or "")[:300],
                        datetime.now().isoformat(),
                    ),
                )
                conn.commit()
        except Exception:
            logger.debug("audit record failed", exc_info=True)
