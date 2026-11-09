"""Background perception events (P1 of perception -> cognition -> evolution).

This module stores ONLY background sensory events: perception that happens
with no user instruction and no tool call (window focus/create/destroy/
rename, process start/exit, clipboard_change, power/network/file change,
mouse idle/active, ...).

It deliberately never records tool results. Tool-triggered perception already
lands in qs_events / task_history / workflow_candidates / error_corrections,
so recording it here would duplicate those tables. The wiring hook lives in
SensorySystem's publish path (kekesi/sensory.py) and calls
record_sensory_event(); nothing in this module reads config.yaml and nothing
here touches the main memory database: perception_events lives in its own
SQLite file (default kekesi/data/perception_events.db, overridable via
PerceptionStore(db_path) or configure_perception_store(db_path)).

Read entry for the cognition layer (P2 imports it):
    recent_perception_context(limit) -> str
It returns recent background events plus, on a best-effort basis, recent
qs_events rows when QsStore default-constructs (or when a reader was
registered via register_qs_reader). It does NOT change
learning_context_for_llm output; P2 wires that.

Retention (code defaults, never read from config.yaml):
  sampling: same (event_type, source) at most once per SAMPLE_MIN_INTERVAL_SEC
  dedup:    same dedup key within DEDUP_WINDOW_SEC is skipped -- repeats are
            not stored, only a new/distinct event is kept
  pruning:  table capped at PRUNE_KEEP newest rows (mirrors qs_keep)

Marker: KEKESI_PCE_P1_20261101
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path

MARKER = "KEKESI_PCE_P1_20261101"

# Code defaults only (never read from config.yaml; P2 may wire config later).
DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "perception_events.db"
SAMPLE_MIN_INTERVAL_SEC = 5.0  # same (event_type, source): at most one stored event per 5 s
DEDUP_WINDOW_SEC = 300.0  # same dedup key within 5 min: skip (repeats are not stored)
PRUNE_KEEP = 2000  # mirrors qs_keep
MAX_SUMMARY_CHARS = 200
MAX_DETAIL_CHARS = 2000

_TOOL_SOURCES = {"tool", "tool_result", "executor", "tool_call"}
_TOOL_EVENT_TYPES = {"tool_result", "tool_call", "tool_output"}


def _field(event, *names, default=""):
    """Read a field from a dict event or an object event, trying each name."""
    if isinstance(event, dict):
        for name in names:
            value = event.get(name)
            if value is not None:
                return value
    else:
        for name in names:
            value = getattr(event, name, None)
            if value is not None:
                return value
    return default


def _dedup_key(event_type, source, summary):
    raw = f"{event_type}|{source}|{summary}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()


class PerceptionStore:
    """SQLite store for background perception events only."""

    def __init__(self, db_path=None):
        self.db_path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
        self._lock = threading.Lock()
        self._last_sample = {}
        self._ensure_schema()

    def _connect(self):
        return sqlite3.connect(str(self.db_path))

    def _ensure_schema(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS perception_events ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "ts REAL NOT NULL, "
                "event_type TEXT NOT NULL, "
                "source TEXT NOT NULL, "
                "summary TEXT NOT NULL, "
                "detail TEXT NOT NULL, "
                "dedup_key TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_perception_ts "
                "ON perception_events(ts)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_perception_dedup "
                "ON perception_events(dedup_key)"
            )
            conn.commit()

    def record_sensory_event(self, event):
        """Store one background sensory event. True if stored, False if skipped.

        Skipped when: the event looks tool-originated, the same
        (event_type, source) was stored within SAMPLE_MIN_INTERVAL_SEC
        (sampling), or the same dedup key was stored within DEDUP_WINDOW_SEC
        (repeats are not stored; only a new/distinct event is kept).
        """
        event_type = str(_field(event, "event_type", "type", "kind", default="unknown"))
        source = str(_field(event, "source", "origin", default="sensory"))
        if source in _TOOL_SOURCES or event_type in _TOOL_EVENT_TYPES:
            return False
        if _field(event, "origin", default="") == "tool":
            return False
        if _field(event, "from_tool", default=False):
            return False
        summary = str(_field(event, "summary", "text", "content", "title", default=""))
        summary = summary[:MAX_SUMMARY_CHARS]
        detail_raw = _field(event, "detail", "payload", "data", default="")
        if isinstance(detail_raw, (dict, list)):
            detail = json.dumps(detail_raw, ensure_ascii=False)[:MAX_DETAIL_CHARS]
        else:
            detail = str(detail_raw)[:MAX_DETAIL_CHARS]
        key = _dedup_key(event_type, source, summary)
        now = time.time()
        with self._lock:
            last = self._last_sample.get((event_type, source))
            if last is not None and now - last < SAMPLE_MIN_INTERVAL_SEC:
                return False
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT 1 FROM perception_events "
                    "WHERE dedup_key = ? AND ts > ? LIMIT 1",
                    (key, now - DEDUP_WINDOW_SEC),
                ).fetchone()
                if row is not None:
                    return False
                conn.execute(
                    "INSERT INTO perception_events "
                    "(ts, event_type, source, summary, detail, dedup_key) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (now, event_type, source, summary, detail, key),
                )
                conn.execute(
                    "DELETE FROM perception_events WHERE id NOT IN "
                    "(SELECT id FROM perception_events ORDER BY id DESC LIMIT ?)",
                    (PRUNE_KEEP,),
                )
                conn.commit()
            self._last_sample[(event_type, source)] = now
            return True

    def recent(self, limit=20):
        """Newest-first list of {ts, event_type, source, summary} dicts."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT ts, event_type, source, summary FROM perception_events "
                "ORDER BY id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [
            {"ts": r[0], "event_type": r[1], "source": r[2], "summary": r[3]}
            for r in rows
        ]

    def recent_structured(self, limit=20):
        """Read background sensory events with their evidence payloads."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id,ts,event_type,source,summary,detail FROM perception_events "
                "ORDER BY id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [
            {"id": r[0], "ts": r[1], "event_type": r[2], "source": r[3], "summary": r[4], "detail": r[5]}
            for r in rows
        ]


_DEFAULT_STORE = None
_QS_READER = None


def _default_store():
    global _DEFAULT_STORE
    if _DEFAULT_STORE is None:
        _DEFAULT_STORE = PerceptionStore()
    return _DEFAULT_STORE


def configure_perception_store(db_path):
    """Point the module-level store at another SQLite file (tests, P2)."""
    global _DEFAULT_STORE
    _DEFAULT_STORE = PerceptionStore(db_path)


def register_qs_reader(reader):
    """Register an explicit qs reader: object with .recent(limit) or callable(limit).

    When set, recent_perception_context() merges its rows. The wiring may call
    this with the live QsStore; without it, qs merging falls back to a
    best-effort QsStore default construction (see _qs_rows), else background
    events only.
    """
    global _QS_READER
    _QS_READER = reader


def record_sensory_event(event):
    """Module-level hook for SensorySystem's publish path. Never raises."""
    try:
        return _default_store().record_sensory_event(event)
    except Exception:
        return False


def _qs_rows(limit):
    """Best-effort recent qs rows as short strings (empty list on any failure)."""
    reader = _QS_READER
    rows = None
    try:
        if reader is not None:
            if callable(reader):
                rows = reader(limit)
            elif hasattr(reader, "recent"):
                rows = reader.recent(limit)
        else:
            try:
                from kekesi.memory.qs_store import QsStore
            except Exception:
                try:
                    from .qs_store import QsStore
                except Exception:
                    return []
            qs = QsStore()
            try:
                rows = qs.recent(limit)
            except TypeError:
                try:
                    rows = qs.recent(limit=limit)
                except TypeError:
                    rows = qs.recent()
                    rows = list(rows or [])[:limit]
    except Exception:
        return []
    out = []
    for row in rows or []:
        if isinstance(row, dict):
            text = (
                row.get("summary") or row.get("text") or row.get("content")
                or row.get("func") or ""
            )
        elif isinstance(row, (list, tuple)):
            text = " ".join(str(part) for part in row if part not in (None, ""))
        else:
            text = (
                getattr(row, "summary", None) or getattr(row, "text", None)
                or getattr(row, "func", None) or str(row)
            )
        text = str(text).strip()
        if text:
            out.append(text[:MAX_SUMMARY_CHARS])
    return out


def recent_perception_context(limit):
    """Single clean read entry for the cognition layer (P2 imports this).

    Returns a short text summary: recent background sensory events from
    perception_events, plus recent qs_events rows when a qs reader is
    available. Empty string when there is nothing to show. This function
    does not change learning_context_for_llm output; P2 wires that.
    """
    limit = max(1, int(limit))
    lines = []
    try:
        events = _default_store().recent(limit)
    except Exception:
        events = []
    for event in events:
        lines.append(f"[{event['event_type']}] {event['summary']}")
    for text in _qs_rows(limit):
        lines.append(f"[qs] {text}")
    if not lines:
        return ""
    return "\n".join(lines[: limit * 2])


def recent_environment_summary(limit=20):
    """Standard read-only summary of background OS state changes.

    Tool-originated events and conversational/QS rows are excluded. The
    perception database remains independent from cognition.db.
    """
    allowed = {
        "window_focus", "window_create", "window_destroy", "window_rename",
        "process_start", "process_exit", "service_change", "network_change",
        "file_change", "power_change", "device_change",
    }
    try:
        events = _default_store().recent_structured(limit)
    except Exception:
        return []
    return [
        event for event in events
        if event.get("event_type") in allowed
        and event.get("source") not in _TOOL_SOURCES
    ]
