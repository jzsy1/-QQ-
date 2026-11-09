"""TraceStore — SQLite 持久化 traces / spans 两张表。

设计要点：
- 复用 kekesi.memory.db.Database 的连接管理（与 runtime_tasks / execution_history 同库）
- fail-soft：所有写操作 try/except，失败只 log.debug，不影响主流程
- prune：按 keep_days 和 keep_max 双策略清理
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from ..memory.db import Database

if TYPE_CHECKING:
    from .context import Span, Trace

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS traces (
    trace_id TEXT PRIMARY KEY,
    task_id TEXT DEFAULT '',
    user_text TEXT DEFAULT '',
    agent_id TEXT DEFAULT 'keks',
    session_id TEXT DEFAULT 'default',
    started_at TEXT DEFAULT (datetime('now')),
    finished_at TEXT,
    duration_ms INTEGER DEFAULT 0,
    status TEXT DEFAULT 'running',
    reply TEXT DEFAULT '',
    error TEXT DEFAULT '',
    layer TEXT DEFAULT '',
    span_count INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_traces_started
    ON traces(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_traces_status
    ON traces(status, started_at DESC);

CREATE TABLE IF NOT EXISTS spans (
    span_id TEXT PRIMARY KEY,
    trace_id TEXT NOT NULL,
    parent_span_id TEXT DEFAULT '',
    name TEXT NOT NULL,
    started_at TEXT DEFAULT (datetime('now')),
    finished_at TEXT,
    duration_ms INTEGER DEFAULT 0,
    status TEXT DEFAULT 'success',
    attributes_json TEXT DEFAULT '{}',
    error TEXT DEFAULT '',
    events_json TEXT DEFAULT '[]',
    seq INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_spans_trace
    ON spans(trace_id, seq);
"""


def _safe_str(value: Any, max_len: int = 500) -> str:
    """任意值安全转 str；method/None 返回空串（防 dataclass 字段被赋值为 method）。

    与 supervisor._safe_str 思路一致，避免 'method' 类型污染 SQLite。
    """
    if value is None or callable(value):
        return ""
    try:
        s = str(value)
    except Exception:
        return ""
    return s[:max_len] if max_len > 0 else s


class TraceStore:
    """traces / spans 持久化与查询。"""

    def __init__(self, db: Database) -> None:
        self.db = db
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        try:
            with self.db.connect() as conn:
                conn.executescript(_SCHEMA)
                conn.commit()
        except Exception:
            logger.debug("TraceStore schema 初始化失败", exc_info=True)

    def save_trace(self, trace: "Trace") -> None:
        """upsert 一条 trace 记录。"""
        try:
            with self.db.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO traces (
                        trace_id, task_id, user_text, agent_id, session_id,
                        started_at, finished_at, duration_ms, status,
                        reply, error, layer, span_count
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(trace_id) DO UPDATE SET
                        task_id=excluded.task_id,
                        finished_at=excluded.finished_at,
                        duration_ms=excluded.duration_ms,
                        status=excluded.status,
                        reply=excluded.reply,
                        error=excluded.error,
                        layer=excluded.layer,
                        span_count=excluded.span_count
                    """,
                    (
                        trace.trace_id,
                        _safe_str(trace.task_id, 64),
                        _safe_str(trace.user_text, getattr(trace, "user_text_max", 500)),
                        _safe_str(trace.agent_id, 32),
                        _safe_str(trace.session_id, 64),
                        trace.started_at_iso or "",
                        trace.finished_at_iso or "",
                        max(0, int(trace.duration_ms)),
                        _safe_str(trace.status, 32),
                        _safe_str(trace.reply, getattr(trace, "reply_max", 500)),
                        _safe_str(trace.error, getattr(trace, "reply_max", 500)),
                        _safe_str(trace.layer, 64),
                        max(0, int(trace.span_count)),
                    ),
                )
                conn.commit()
        except Exception:
            logger.debug("save_trace failed: %s", trace.trace_id, exc_info=True)

    def link_task_id(self, trace_id: str, task_id: str) -> None:
        """轻量更新：只把 task_id 关联到 trace（请求进行中即可查到关联）。

        场景：Supervisor.run 开头创建 Task 后立即调用，避免 trace 一直停在
        task_id='' 直到 finish 才落库——那样进程崩溃就永远丢失关联。
        """
        if not trace_id or not task_id:
            return
        try:
            with self.db.connect() as conn:
                conn.execute(
                    "UPDATE traces SET task_id = ? WHERE trace_id = ?",
                    (_safe_str(task_id, 64), trace_id),
                )
                conn.commit()
        except Exception:
            logger.debug("link_task_id failed: %s", trace_id, exc_info=True)

    def save_span(self, span: "Span") -> None:
        """insert 一条 span 记录（含递增 seq，便于按插入顺序排序）。"""
        try:
            attrs_json = json.dumps(
                span.attributes or {}, ensure_ascii=False, default=str
            )
            events_json = json.dumps(
                span.events or [], ensure_ascii=False, default=str
            )
            with self.db.connect() as conn:
                # seq 取当前 trace 下最大 seq + 1，保证同 trace 内 span 有序
                row = conn.execute(
                    "SELECT COALESCE(MAX(seq), 0) AS m FROM spans WHERE trace_id=?",
                    (span.trace_id,),
                ).fetchone()
                seq = int(row["m"] if row else 0) + 1
                conn.execute(
                    """
                    INSERT OR REPLACE INTO spans (
                        span_id, trace_id, parent_span_id, name,
                        started_at, finished_at, duration_ms, status,
                        attributes_json, error, events_json, seq
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        span.span_id,
                        span.trace_id,
                        _safe_str(span.parent_span_id, 64),
                        _safe_str(span.name, 128),
                        span.started_at_iso or "",
                        span.finished_at_iso or "",
                        max(0, int(span.duration_ms)),
                        _safe_str(span.status, 32),
                        attrs_json,
                        _safe_str(span.error, 500),
                        events_json,
                        seq,
                    ),
                )
                # 同步 traces.span_count
                conn.execute(
                    """
                    UPDATE traces SET span_count = (
                        SELECT COUNT(*) FROM spans WHERE trace_id = ?
                    ) WHERE trace_id = ?
                    """,
                    (span.trace_id, span.trace_id),
                )
                conn.commit()
        except Exception:
            logger.debug("save_span failed: %s", span.span_id, exc_info=True)

    def get_trace(self, trace_id: str) -> dict | None:
        """返回单条 trace 及其所有 spans（按 seq 排序）。"""
        try:
            with self.db.connect() as conn:
                row = conn.execute(
                    "SELECT * FROM traces WHERE trace_id=?",
                    (trace_id,),
                ).fetchone()
                if not row:
                    return None
                spans = conn.execute(
                    """
                    SELECT * FROM spans WHERE trace_id=?
                    ORDER BY seq ASC
                    """,
                    (trace_id,),
                ).fetchall()
            return {
                "trace": _row_to_trace(dict(row)),
                "spans": [_row_to_span(dict(r)) for r in spans],
            }
        except Exception:
            logger.debug("get_trace failed: %s", trace_id, exc_info=True)
            return None

    def recent_traces(
        self, limit: int = 20, status: str = ""
    ) -> list[dict]:
        """最近 trace 列表（不含 spans，仅元信息）。"""
        limit = max(1, min(int(limit), 200))
        try:
            with self.db.connect() as conn:
                if status:
                    rows = conn.execute(
                        """
                        SELECT * FROM traces
                        WHERE status = ?
                        ORDER BY started_at DESC
                        LIMIT ?
                        """,
                        (status, limit),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """
                        SELECT * FROM traces
                        ORDER BY started_at DESC
                        LIMIT ?
                        """,
                        (limit,),
                    ).fetchall()
            return [_row_to_trace(dict(r)) for r in rows]
        except Exception:
            logger.debug("recent_traces failed", exc_info=True)
            return []

    def recent_errors(self, limit: int = 20) -> list[dict]:
        """最近失败 trace（status in failed/timeout/error/cancelled）。"""
        limit = max(1, min(int(limit), 200))
        failed_states = ("failed", "timeout", "error", "cancelled")
        placeholders = ",".join("?" for _ in failed_states)
        try:
            with self.db.connect() as conn:
                rows = conn.execute(
                    f"""
                    SELECT * FROM traces
                    WHERE status IN ({placeholders})
                    ORDER BY started_at DESC
                    LIMIT ?
                    """,
                    (*failed_states, limit),
                ).fetchall()
            return [_row_to_trace(dict(r)) for r in rows]
        except Exception:
            logger.debug("recent_errors failed", exc_info=True)
            return []

    def prune(self, keep_days: int = 7, keep_max: int = 5000) -> int:
        """清理旧 trace：先按天数，再按上限数量；返回删除条数。"""
        keep_days = max(0, int(keep_days))
        keep_max = max(0, int(keep_max))
        deleted = 0
        try:
            with self.db.connect() as conn:
                # 1. 按天数清理
                if keep_days > 0:
                    cutoff = (
                        datetime.now() - timedelta(days=keep_days)
                    ).isoformat()
                    old_ids = [
                        str(r["trace_id"])
                        for r in conn.execute(
                            "SELECT trace_id FROM traces WHERE started_at < ?",
                            (cutoff,),
                        ).fetchall()
                    ]
                    if old_ids:
                        placeholders = ",".join("?" for _ in old_ids)
                        conn.execute(
                            f"DELETE FROM spans WHERE trace_id IN ({placeholders})",
                            tuple(old_ids),
                        )
                        cur = conn.execute(
                            f"DELETE FROM traces WHERE trace_id IN ({placeholders})",
                            tuple(old_ids),
                        )
                        deleted += cur.rowcount

                # 2. 按数量上限清理（保留最新 keep_max 条）
                if keep_max > 0:
                    keep_ids = [
                        str(r["trace_id"])
                        for r in conn.execute(
                            "SELECT trace_id FROM traces ORDER BY started_at DESC LIMIT ?",
                            (keep_max,),
                        ).fetchall()
                    ]
                    if keep_ids:
                        placeholders = ",".join("?" for _ in keep_ids)
                        # 子查询 NOT IN 删除多余
                        excess = [
                            str(r["trace_id"])
                            for r in conn.execute(
                                f"SELECT trace_id FROM traces WHERE trace_id NOT IN ({placeholders})",
                                tuple(keep_ids),
                            ).fetchall()
                        ]
                        if excess:
                            ex_ph = ",".join("?" for _ in excess)
                            conn.execute(
                                f"DELETE FROM spans WHERE trace_id IN ({ex_ph})",
                                tuple(excess),
                            )
                            cur = conn.execute(
                                f"DELETE FROM traces WHERE trace_id IN ({ex_ph})",
                                tuple(excess),
                            )
                            deleted += cur.rowcount

                conn.commit()
        except Exception:
            logger.debug("prune failed", exc_info=True)
        return deleted


def _row_to_trace(row: dict) -> dict:
    return {
        "trace_id": row.get("trace_id", ""),
        "task_id": row.get("task_id", ""),
        "user_text": row.get("user_text", ""),
        "agent_id": row.get("agent_id", ""),
        "session_id": row.get("session_id", ""),
        "started_at": row.get("started_at", ""),
        "finished_at": row.get("finished_at", ""),
        "duration_ms": int(row.get("duration_ms", 0) or 0),
        "status": row.get("status", ""),
        "reply": row.get("reply", ""),
        "error": row.get("error", ""),
        "layer": row.get("layer", ""),
        "span_count": int(row.get("span_count", 0) or 0),
    }


def _row_to_span(row: dict) -> dict:
    try:
        attrs = json.loads(row.get("attributes_json") or "{}")
    except (json.JSONDecodeError, TypeError):
        attrs = {}
    try:
        events = json.loads(row.get("events_json") or "[]")
    except (json.JSONDecodeError, TypeError):
        events = []
    return {
        "span_id": row.get("span_id", ""),
        "trace_id": row.get("trace_id", ""),
        "parent_span_id": row.get("parent_span_id", ""),
        "name": row.get("name", ""),
        "started_at": row.get("started_at", ""),
        "finished_at": row.get("finished_at", ""),
        "duration_ms": int(row.get("duration_ms", 0) or 0),
        "status": row.get("status", ""),
        "attributes": attrs,
        "error": row.get("error", ""),
        "events": events,
        "seq": int(row.get("seq", 0) or 0),
    }
