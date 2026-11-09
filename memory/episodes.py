"""P5-1: deterministic episode segmentation over background perception events.

Reads perception_events (written by P1) and writes episodes into the P5
store (episodes_cases.db). Boundary decisions use deterministic rules only,
never an LLM: foreground window/app switch, an idle gap of >= 180 seconds,
or an explicit task-switch marker. Segmentation is a pure function over
the append-only event stream, so rebuilds are idempotent: episode ids are
derived from the first event id (or start time), and already-stored
episodes are skipped.

Marker: KEKESI_PCE_P5_20261101
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

MARKER = "KEKESI_PCE_P5_20261101"
IDLE_GAP_SECONDS = 180
TASK_MARKERS = {"task_switch", "session_start", "goal_change", "task_start"}
WINDOW_HINTS = ("window", "focus", "app_switch", "foreground")


def _repo_root():
    return Path(__file__).resolve().parents[2]


def _default_store_path():
    return _repo_root() / "kekesi" / "data" / "episodes_cases.db"


def _default_perception_path():
    return _repo_root() / "kekesi" / "data" / "perception_events.db"


def _parse_time(value):
    """Best-effort epoch seconds from epoch numbers or ISO strings."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    try:
        return float(text)
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except ValueError:
        return None


def _to_iso(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _is_window_event(event):
    kind = event["type"].lower()
    return any(hint in kind for hint in WINDOW_HINTS)


def segment_events(events):
    """Pure function: split time-ordered events into episodes.

    Each input event is a dict with keys time (epoch float), type (str)
    and source (str). Returns a list of episode dicts with keys start,
    end, reason and events. Reasons: start, task_marker, idle_gap,
    window_switch (checked in that order).
    """
    episodes = []
    current = None
    prev = None
    last_window = None
    for event in events:
        reason = None
        if current is None:
            reason = "start"
        elif event["type"] in TASK_MARKERS:
            reason = "task_marker"
        elif prev is not None and (event["time"] - prev["time"]) >= IDLE_GAP_SECONDS:
            reason = "idle_gap"
        elif (
            _is_window_event(event)
            and last_window
            and event["source"]
            and event["source"] != last_window
        ):
            reason = "window_switch"
        if reason is not None:
            if current is not None:
                episodes.append(current)
            current = {
                "start": event["time"],
                "end": event["time"],
                "reason": reason,
                "events": [event],
            }
        else:
            current["events"].append(event)
            current["end"] = event["time"]
        if _is_window_event(event) and event["source"]:
            last_window = event["source"]
        prev = event
    if current is not None:
        episodes.append(current)
    return episodes


def _read_perception_events(perception_db):
    """Read perception rows defensively (P1 schema is not assumed).

    Returns (events, id_column_or_None). Column names are picked from
    candidate lists; a missing table or unusable shape yields ([], None)
    instead of raising, so the hook can never break the learning chain.
    """
    path = Path(perception_db)
    if not path.exists():
        return [], None
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(perception_events)")]
        if not columns:
            return [], None

        def pick(candidates):
            for name in candidates:
                if name in columns:
                    return name
            return None

        id_col = pick(["id"])
        time_col = pick(["created_at", "timestamp", "ts", "time"])
        type_col = pick(["event_type", "type"])
        src_col = pick(["source", "app", "window", "window_title"])
        if time_col is None and id_col is None:
            return [], None
        rows = conn.execute("SELECT * FROM perception_events").fetchall()
    finally:
        conn.close()
    events = []
    for row in rows:
        data = dict(row)
        stamp = _parse_time(data.get(time_col)) if time_col else None
        events.append(
            {
                "id": data.get(id_col) if id_col else None,
                "time": stamp,
                "type": str(data.get(type_col) or ""),
                "source": str(data.get(src_col) or ""),
            }
        )
    events.sort(
        key=lambda e: (
            e["time"] is None,
            e["time"] if e["time"] is not None else 0.0,
            e["id"] if isinstance(e["id"], int) else 0,
        )
    )
    last = None
    for event in events:
        if event["time"] is None:
            event["time"] = last if last is not None else 0.0
        last = event["time"]
    return events, id_col


class EpisodeStore:
    """episodes table inside the P5 store (never the main kekesi.db)."""

    def __init__(self, db_path=None):
        self.db_path = Path(db_path) if db_path else _default_store_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self):
        conn = self._connect()
        try:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS episodes (
                    episode_id TEXT PRIMARY KEY,
                    start_time TEXT,
                    end_time TEXT,
                    split_reason TEXT,
                    event_ids TEXT,
                    event_count INTEGER,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )"""
            )
            conn.commit()
        finally:
            conn.close()

    def _watermark(self, conn):
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'episodes_watermark'"
        ).fetchone()
        return row[0] if row else None

    def _set_watermark(self, conn, value):
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('episodes_watermark', ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(value),),
        )

    def recent_episodes(self, limit=10):
        """Most recent episodes, newest first, for cognition to read."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT episode_id, start_time, end_time, split_reason,"
                " event_ids, event_count FROM episodes"
                " ORDER BY start_time DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        finally:
            conn.close()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["event_ids"] = json.loads(item["event_ids"] or "[]")
            except (ValueError, TypeError):
                item["event_ids"] = []
            result.append(item)
        return result

    def query_relevant_episodes(
        self,
        query="",
        *,
        entity_hints=None,
        max_age_hours=168.0,
        limit=3,
        perception_db_path=None,
        now_ts=None,
    ):
        """Retrieve task/entity/time/scene-relevant episodes (hydrating perception summaries).

        - Never blindly returns 10 unrelated episodes when a query or entity_hints is given.
        - Distinguishes current episodes (`is_current=True`, within 15 min) from historical episodes (`is_current=False`).
        - Filters by `max_age_hours` and applies tombstone filtering.
        """
        import re as _re

        limit = max(1, int(limit))
        base_now = float(now_ts) if now_ts is not None else datetime.now(timezone.utc).timestamp()
        pdb = Path(perception_db_path) if perception_db_path else (self.db_path.parent / "perception_events.db")
        if not pdb.exists():
            pdb = _default_perception_path()

        raw_eps = self.recent_episodes(limit=80)
        if not raw_eps:
            return []

        # Collect all referenced integer event_ids to batch-hydrate from perception_events.db
        all_eids = set()
        for ep in raw_eps:
            for eid in ep.get("event_ids") or []:
                if isinstance(eid, int) or (isinstance(eid, str) and eid.isdigit()):
                    all_eids.add(int(eid))

        ev_map = {}
        if all_eids and pdb.exists():
            try:
                pconn = sqlite3.connect(pdb)
                pconn.row_factory = sqlite3.Row
                try:
                    id_list = sorted(all_eids)[:500]
                    placeholders = ",".join("?" for _ in id_list)
                    prows = pconn.execute(
                        f"SELECT id, event_type, source, summary, detail, ts FROM perception_events WHERE id IN ({placeholders})",
                        tuple(id_list),
                    ).fetchall()
                    for pr in prows:
                        ev_map[int(pr["id"])] = dict(pr)
                finally:
                    pconn.close()
            except Exception:
                ev_map = {}

        # Build query tokens + bigrams
        q_raw = f"{query or ''} {' '.join(str(x) for x in (entity_hints or []))}".strip().lower()
        q_tokens = set(_re.findall(r"[a-z0-9_]{2,}|[\u4e00-\u9fff]{2,}", q_raw))
        q_bigrams = set()
        for seq in _re.findall(r"[\u4e00-\u9fff]{2,}", q_raw):
            for i in range(len(seq) - 1):
                bg = seq[i : i + 2]
                if bg not in ("刚才", "之前", "上次", "什么", "怎么", "帮我", "一下", "历史", "最近"):
                    q_bigrams.add(bg)
        has_query_terms = bool(q_tokens or q_bigrams)

        scored = []
        for ep in raw_eps:
            end_ts = _parse_time(ep.get("end_time")) or _parse_time(ep.get("start_time")) or 0.0
            age_hours = max(0.0, (base_now - end_ts) / 3600.0) if end_ts > 0 else 0.0
            if max_age_hours and max_age_hours > 0 and end_ts > 0 and age_hours > float(max_age_hours):
                continue

            eids = [int(x) for x in (ep.get("event_ids") or []) if isinstance(x, int) or (isinstance(x, str) and x.isdigit())]
            ev_summaries = []
            ev_types = []
            ev_sources = []
            scene_entities = []
            for eid in eids:
                ev = ev_map.get(eid)
                if not ev:
                    continue
                s = str(ev.get("summary") or "").strip()
                if s and s not in ev_summaries:
                    ev_summaries.append(s)
                et = str(ev.get("event_type") or "").strip()
                if et and et not in ev_types:
                    ev_types.append(et)
                src = str(ev.get("source") or "").strip()
                if src and src not in ev_sources:
                    ev_sources.append(src)
                det = ev.get("detail")
                if isinstance(det, str) and det.startswith("{"):
                    try:
                        det_obj = json.loads(det)
                        for k in ("process", "process_name", "name", "title", "window_title", "app"):
                            val = str(det_obj.get(k) or "").strip()
                            if val and val not in scene_entities:
                                scene_entities.append(val)
                    except Exception:
                        pass

            scene_summary = " → ".join(ev_summaries[:3]) if ev_summaries else f"{ep.get('split_reason') or 'episode'}({ep.get('event_count', 0)} events)"
            haystack = f"{scene_summary} {' '.join(scene_entities)} {' '.join(ev_types)} {' '.join(ev_sources)} {ep.get('split_reason') or ''}".lower()

            rel_score = 0.0
            if has_query_terms:
                for tok in q_tokens:
                    if tok in haystack:
                        rel_score += 1.5
                for bg in q_bigrams:
                    if bg in haystack:
                        rel_score += 0.8
                if rel_score <= 0.0:
                    continue
            else:
                rel_score = 0.5

            is_current = bool(end_ts > 0 and age_hours <= 0.25)
            recency_bonus = max(0.05, 1.0 / (1.0 + age_hours / 12.0))
            total_score = round(rel_score + recency_bonus * 0.4 + (0.2 if is_current else 0.0), 4)

            enriched = dict(ep)
            enriched["scene_summary"] = scene_summary[:180]
            enriched["scene_entities"] = scene_entities[:5]
            enriched["event_types"] = ev_types[:5]
            enriched["sources"] = ev_sources[:5]
            enriched["is_current"] = is_current
            enriched["temporal_tag"] = "当前事件片段" if is_current else "历史情景片段"
            enriched["age_hours"] = round(age_hours, 2)
            enriched["relevance_score"] = total_score
            scored.append(enriched)

        scored.sort(key=lambda x: (x["relevance_score"], x.get("end_time") or ""), reverse=True)
        from .tombstones import filter_rows, main_db_path_for_store

        filtered = filter_rows(
            scored,
            main_db_path_for_store(self.db_path),
            lambda r: f"{r.get('scene_summary') or ''} {' '.join(r.get('scene_entities') or [])}",
        )
        return filtered[:limit]


def rebuild_episodes(perception_db=None, store_db=None):
    """Segment the perception stream and store only new episodes.

    The full stream is re-segmented each call (bounded: perception rows
    are sampled and capped upstream), but episodes already stored are
    skipped by primary key, so repeated calls only add new episodes.
    Returns the number of newly stored episodes.
    """
    store = EpisodeStore(store_db)
    pdb = Path(perception_db) if perception_db else _default_perception_path()
    events, id_col = _read_perception_events(pdb)
    if not events:
        return 0
    conn = store._connect()
    try:
        new_count = 0
        for index, episode in enumerate(segment_events(events)):
            first = episode["events"][0]
            if first["id"] is not None:
                episode_id = "ep-%s" % (first["id"],)
            else:
                episode_id = "ep-%d-%d" % (int(episode["start"]), index)
            exists = conn.execute(
                "SELECT 1 FROM episodes WHERE episode_id = ?", (episode_id,)
            ).fetchone()
            if exists:
                continue
            event_ids = [e["id"] for e in episode["events"] if e["id"] is not None]
            conn.execute(
                "INSERT INTO episodes(episode_id, start_time, end_time,"
                " split_reason, event_ids, event_count) VALUES(?, ?, ?, ?, ?, ?)",
                (
                    episode_id,
                    _to_iso(episode["start"]),
                    _to_iso(episode["end"]),
                    episode["reason"],
                    json.dumps(event_ids, ensure_ascii=False),
                    len(episode["events"]),
                ),
            )
            new_count += 1
        int_ids = [e["id"] for e in events if isinstance(e["id"], int)]
        if id_col and int_ids:
            store._set_watermark(conn, max(int_ids))
        else:
            store._set_watermark(conn, max(e["time"] for e in events))
        conn.commit()
        return new_count
    finally:
        conn.close()


def recent_episodes(limit=10):
    """Module-level shortcut for cognition: newest episodes first."""
    return EpisodeStore().recent_episodes(limit)
