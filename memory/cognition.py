# KEKESI_PCE_P2_20261101_V2
# P2 cognition models for Kekesi main memory (SQLite, stdlib only).
# Three model tables: world_entities, user_model, self_model.
# Extraction is deterministic keyword rules only; no LLM is called here,
# so this module is safe to run on the learning path and the hot path.
# Conflict policy for these NEW tables: keyed upsert (latest value wins,
# occurrences count grows). The existing _store_or_reconcile_fact mechanism
# in learning.py keeps governing auto_facts untouched; its private signature
# is deliberately not guessed or called from here.
import functools
import json
import sqlite3
import time
from pathlib import Path

MARKER = "KEKESI_PCE_P2_20261101_V2"

TABLES = {
    "world": "world_entities",
    "user": "user_model",
    "self": "self_model",
}

_WORLD_SCHEMA = (
    "key TEXT PRIMARY KEY, value TEXT NOT NULL, relation TEXT DEFAULT 'mentioned', "
    "status TEXT DEFAULT 'mentioned', source TEXT DEFAULT '', "
    "occurrences INTEGER DEFAULT 1, updated_at REAL NOT NULL, "
    "validity TEXT DEFAULT 'INFERRED_HYPOTHESIS', confidence REAL DEFAULT 0.5, "
    "expires_at REAL, health_state TEXT DEFAULT '', evidence_json TEXT DEFAULT '{}', "
    "success_count INTEGER DEFAULT 0, failure_count INTEGER DEFAULT 0"
)
_FLAT_SCHEMA = (
    "key TEXT PRIMARY KEY, value TEXT NOT NULL, source TEXT DEFAULT '', "
    "occurrences INTEGER DEFAULT 1, updated_at REAL NOT NULL, "
    "validity TEXT DEFAULT 'INFERRED_HYPOTHESIS', confidence REAL DEFAULT 0.5, "
    "expires_at REAL, health_state TEXT DEFAULT '', evidence_json TEXT DEFAULT '{}', "
    "success_count INTEGER DEFAULT 0, failure_count INTEGER DEFAULT 0"
)


def default_db_path():
    try:
        root = Path(__file__).resolve().parents[2]
    except Exception:
        root = Path.cwd()
    return root / "kekesi" / "data" / "cognition.db"


def connect(db_path=None):
    path = Path(db_path) if db_path else default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS world_entities (%s)" % _WORLD_SCHEMA
    )
    conn.execute("CREATE TABLE IF NOT EXISTS user_model (%s)" % _FLAT_SCHEMA)
    conn.execute("CREATE TABLE IF NOT EXISTS self_model (%s)" % _FLAT_SCHEMA)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS capability_health_events ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, tool_name TEXT NOT NULL, "
        "previous_state TEXT NOT NULL, new_state TEXT NOT NULL, source TEXT DEFAULT '', "
        "failure_class TEXT DEFAULT '', task_id TEXT DEFAULT '', confidence REAL DEFAULT 0, "
        "evidence_json TEXT DEFAULT '{}', created_at REAL NOT NULL)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_capability_health_tool_time "
        "ON capability_health_events(tool_name, created_at DESC)"
    )
    for table in TABLES.values():
        cols = {str(row[1]) for row in conn.execute("PRAGMA table_info(%s)" % table)}
        migrations = {
            "validity": "TEXT DEFAULT 'INFERRED_HYPOTHESIS'",
            "confidence": "REAL DEFAULT 0.5",
            "expires_at": "REAL",
            "health_state": "TEXT DEFAULT ''",
            "evidence_json": "TEXT DEFAULT '{}'",
            "success_count": "INTEGER DEFAULT 0",
            "failure_count": "INTEGER DEFAULT 0",
        }
        for name, declaration in migrations.items():
            if name not in cols:
                conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, name, declaration))
    conn.commit()
    return conn


def _upsert(conn, table, key, value, source, relation="mentioned", status="mentioned"):
    now = time.time()
    validity = "CONFIRMED_FACT" if table == "user_model" and source in {"user_turn", "perception:user_turn"} else "INFERRED_HYPOTHESIS"
    confidence = 0.8 if validity == "CONFIRMED_FACT" else 0.55
    if table == "world_entities":
        conn.execute(
            "INSERT INTO world_entities(key, value, relation, status, source, occurrences, updated_at, validity, confidence) "
            "VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, relation=excluded.relation, "
            "status=excluded.status, source=excluded.source, "
            "occurrences=occurrences+1, updated_at=excluded.updated_at, "
            "validity=excluded.validity, confidence=excluded.confidence, expires_at=NULL",
            (key, value, relation, status, source, now, validity, confidence),
        )
    else:
        conn.execute(
            "INSERT INTO %s(key, value, source, occurrences, updated_at, validity, confidence) "
            "VALUES (?, ?, ?, 1, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, source=excluded.source, "
            "occurrences=occurrences+1, updated_at=excluded.updated_at, "
            "validity=excluded.validity, confidence=excluded.confidence, expires_at=NULL" % table,
            (key, value, source, now, validity, confidence),
        )

_SPLIT_CHARS = ["\n", "。", "！", "？", "；", "!", "?", ";"]

_USER_PREF = ["喜欢", "偏好", "习惯", "经常", "总是", "最爱", "讨厌", "不喜欢"]
_USER_GOAL = ["目标", "计划", "打算", "想要", "想做"]
_SELF_BAD = ["失败", "错误", "不会", "做不到", "不行", "没学会"]
_SELF_GOOD = ["擅长", "会做", "能做", "学会"]
_WORLD_WORDS = ["窗口", "应用", "程序", "文件", "项目", "服务", "进程", "软件"]
_WORLD_ACTIVE = ["打开", "运行", "正在", "在用"]


# KEKESI_PCE_P2_20261101_V3: polarity helpers. _USER_PREF mixes
# positive words with negative ones (讨厌/不喜欢); V2 recorded both
# as preferences. Negation is checked per occurrence in the 2 chars
# before it: covers direct negation (不喜欢/没习惯/不擅长) and 不太;
# longer hedges (不是很/不怎么) stay positive by design — a false
# aversion is worse than a weak preference.
_USER_PREF_NEG = ("讨厌", "不喜欢")
_USER_PREF_POS = tuple(w for w in _USER_PREF if w not in _USER_PREF_NEG)
_NEG_MARKERS = ("不要", "从不", "不", "没", "别", "拒绝", "反感")


def _find_all(seg, word):
    out = []
    start = 0
    while True:
        i = seg.find(word, start)
        if i < 0:
            return out
        out.append(i)
        start = i + 1


def _negated_before(seg, pos):
    window = seg[max(0, pos - 2):pos]
    base = pos - len(window)
    for m in _NEG_MARKERS:
        start = 0
        while True:
            i = window.find(m, start)
            if i < 0:
                break
            # KEKESI_PCE_P2_20261104_TAILS: 别 inside 特别 is an
            # intensifier, not a negation ("特别喜欢" must stay
            # positive). Skip 别 when the char before it in the
            # segment is 特. Other markers (including 不 inside
            # 特别不喜欢) still count.
            if m == "别" and base + i > 0 and seg[base + i - 1] == "特":
                start = i + 1
                continue
            return True
    return False


# KEKESI_PCE_P2_20261103_NOISE: system-event perception lines are not user
# signals. recent_perception_context renders events as "[type] summary";
# tool_event / turn_end lines previously entered user_model as bogus rows.
# Matched on the exact bracket type; [qs] and other types pass through.
# KEKESI_PCE_P2_20261103_NOISE_V2: denylist extended from live evidence
# (world_entities 121/143 system-event rows, 2026-11-03). window_rename is
# included on mechanism evidence (its lines reached _extract and bumped
# occurrences); the other four are row-count evidenced. No speculative
# types are added; filter semantics are unchanged.
# KEKESI_PCE_P2_20261103_NOISE_V3: window_destroy (8 rows) and
# window_focus (6 rows) added -- same window-monitor mechanism as the v2
# types, row-count evidenced 2026-11-04, still growing after restart.
_PERCEPTION_NOISE_TYPES = ("tool_event", "turn_end",
                           "process_start", "process_exit",
                           "window_create", "window_rename", "file_change",
                           "window_destroy", "window_focus")


_SEEN_TABLE = "p2_seen_user_prefixes"
_SEEN_KEEP = 64


def _user_turn_content(line):
    """Content C of a [user_turn] perception line, or "".

    Strips the "[user_turn] " prefix and the "用户发言：" label, leaving
    the user text (at most its first 60 chars, as recorded by sensory).
    """
    s = line.strip()
    if not s.startswith("["):
        return ""
    end = s.find("]")
    if 0 >= end or s[1:end] != "user_turn":
        return ""
    rest = s[end + 1:].strip()
    if rest.startswith("用户发言："):
        rest = rest[len("用户发言："):].strip()
    return rest


def _load_seen_prefixes(db_path):
    """Prefixes recorded as digested in earlier rounds (empty on error).

    The table is created on first use so the SELECT never fails. The
    load is skipped by update_models when perception has no [user_turn]
    line, so rounds without one never touch the DB for this.
    """
    try:
        conn = connect(db_path)
    except Exception:
        return set()
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS %s ("
            "prefix TEXT PRIMARY KEY, ts REAL NOT NULL)" % _SEEN_TABLE
        )
        conn.commit()
        cur = conn.execute("SELECT prefix FROM %s" % _SEEN_TABLE)
        return {r[0] for r in cur.fetchall()}
    except Exception:
        return set()
    finally:
        conn.close()


def _record_seen_prefixes(conn, digest_text, perception_text):
    """Record current-round [user_turn] contents covered by digest.

    Only lines whose content is a prefix of digest_text are recorded:
    the utterance was digested this round, so later rounds must drop
    its perception copy. Lines kept as the only record (fast-path
    rounds, past-turn lines) are never recorded here. Pruned to the
    newest _SEEN_KEEP prefixes.
    """
    import time

    now = time.time()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS %s ("
        "prefix TEXT PRIMARY KEY, ts REAL NOT NULL)" % _SEEN_TABLE
    )
    for line in (perception_text or "").split("\n"):
        content = _user_turn_content(line)
        if content and digest_text and digest_text.startswith(content):
            conn.execute(
                "INSERT OR IGNORE INTO %s (prefix, ts) VALUES (?, ?)"
                % _SEEN_TABLE,
                (content, now),
            )
    conn.execute(
        "DELETE FROM %s WHERE prefix NOT IN ("
        "SELECT prefix FROM %s ORDER BY ts DESC LIMIT ?)"
        % (_SEEN_TABLE, _SEEN_TABLE),
        (_SEEN_KEEP,),
    )


def _filter_perception_lines(text, digest_text=None, seen_prefixes=None):
    out = []
    for line in (text or "").split("\n"):
        s = line.strip()
        if s.startswith("["):
            end = s.find("]")
            if 0 < end and s[1:end] in _PERCEPTION_NOISE_TYPES:
                continue
            # KEKESI_PCE_P2_20261104_DEDUP: a [user_turn] line restates the
            # current turn's user text (sensory summary is "用户发言：" +
            # text[:60], rendered as "[user_turn] ..."). If its content is
            # a prefix of digest_text, the utterance is already recorded
            # via digest -- drop the perception copy so one utterance
            # yields one row. Prefix, not equality: the perception side
            # only keeps 60 chars. If digest does not start with it (a
            # past-turn line, or a turn that skipped digest), the line is
            # kept as the only record. digest_text starts with user_text,
            # so no separate user_text argument is needed.
            # KEKESI_PCE_P2_20261104_TAILS: also drop a [user_turn] line
            # whose content was recorded as digested in an earlier round
            # (seen_prefixes). The current-round rule alone misses
            # adjacent loop rounds: round N's line stays in the
            # perception window and does not match round N+1's digest,
            # so it was kept as a second row. Lines never recorded
            # (fast-path rounds) are still kept as the only record.
            if 0 < end and s[1:end] == "user_turn":
                rest = _user_turn_content(line)
                if rest and (
                    (digest_text and digest_text.startswith(rest))
                    or (seen_prefixes and rest in seen_prefixes)
                ):
                    continue
        out.append(line)
    return "\n".join(out)


def _segments(text):
    parts = [text or ""]
    for sep in _SPLIT_CHARS:
        nxt = []
        for p in parts:
            nxt.extend(p.split(sep))
        parts = nxt
    out = []
    for p in parts:
        s = " ".join(p.split())
        if len(s) >= 2:
            out.append(s)
    return out


def _title_spans(seg):
    """Spans (open, close) of closed 《...》 title pairs in a segment.

    Unmatched 《 (no later 》) yields no span, so its content is treated
    as ordinary text. Nested 《》 pairs are not specially handled: the
    first 》 closes the span, which is enough for song/book/film titles.
    """
    spans = []
    start = 0
    while True:
        open_i = seg.find("《", start)
        if open_i < 0:
            break
        close_i = seg.find("》", open_i + 1)
        if close_i < 0:
            break
        spans.append((open_i, close_i))
        start = close_i + 1
    return spans


def _in_title(spans, i, n):
    """True when a word occurrence at i with length n lies inside a title."""
    return any(s < i and i + n <= e for s, e in spans)


# KEKESI_PCE_P2_20261104_TITLEGUARD: positive-word occurrences inside
# 《...》 titles are title content, not the speaker's own claim
# ("《我喜欢》" must not create a preference row by itself). The guard
# applies to the _find_all-based checks below; negative-word substring
# checks are untouched (no title sample for them).
_QUESTION_CUES = (
    "吗", "呢", "？", "?", "什么", "哪个", "哪儿", "哪里", "怎么", "怎样",
    "为什么", "为何", "是否", "是不是", "好不好", "能不能", "可不可以",
    "要不要", "对不对", "行不行", "多少", "几",
)
_HYPOTHESIS_CUES = (
    "如果", "假如", "假设", "万一", "要是", "倘若", "设想", "按理说", "比如", "例如",
)
_QUOTE_CUES = (
    "他说", "她说", "别人说", "听说", "据说", "有人说", "网上说", "歌词里", "书上说",
)
_ASSISTANT_ADDR_CUES = (
    "你喜欢", "你讨厌", "你不喜欢", "你习惯", "你偏好", "你的目标", "你打算", "你想要",
    "你会不会", "你能", "你擅长",
)
_REPLY_SELF_PRONOUNS = ("我", "本助手", "作为助手", "科科斯")


def _is_non_declarative_or_other_subject(seg):
    """判断句子是否为疑问、假设、引用或询问对方（非用户陈述句）。"""
    s = (seg or "").strip()
    if not s:
        return True
    if s.endswith(("？", "?", "吗", "呢")):
        return True
    if any(q in s for q in ("？", "?")):
        return True
    if any(s.startswith(h) or (h in s[:8]) for h in _HYPOTHESIS_CUES):
        return True
    if any(q in s for q in _QUOTE_CUES):
        return True
    if any(a in s for a in _ASSISTANT_ADDR_CUES):
        return True
    # 含典型疑问词且无明确第一人称肯定主语
    if any(w in s for w in ("什么", "哪个", "哪里", "怎么", "为什么", "是否", "是不是", "能不能", "要不要")):
        return True
    return False


def _split_user_and_reply(digest_text, user_text=None, reply_text=None):
    """拆分用户原文与助手回复，保证来源隔离。

    - 若显式传入 user_text / reply_text，直接使用；
    - 否则解析 digest_text：识别 ``用户：...`` / ``助手：...`` 标签；
    - 兼容旧调用 ``update_models(user_text + '\n' + reply_text, ...)``：
      若无标签但仅由单换行拼接且第二段像助手回复，仅当显式传入 reply_text 时才拆，
      纯字符串无标签时默认整段视为 user（保持旧单测向后兼容），但若含明确
      ``助手：`` / ``Assistant:`` 分隔则严格拆开。
    """
    if user_text is not None or reply_text is not None:
        return str(user_text or ""), str(reply_text or "")
    raw = str(digest_text or "")
    if not raw:
        return "", ""
    u_lines = []
    r_lines = []
    mode = "user"
    has_label = False
    for line in raw.splitlines():
        s = line.strip()
        if s.startswith(("用户：", "用户:", "User:", "user:")):
            has_label = True
            mode = "user"
            u_lines.append(s.split("：", 1)[-1].split(":", 1)[-1].strip())
        elif s.startswith(("助手：", "助手:", "Assistant:", "assistant:")):
            has_label = True
            mode = "reply"
            r_lines.append(s.split("：", 1)[-1].split(":", 1)[-1].strip())
        else:
            if mode == "reply":
                r_lines.append(line)
            else:
                u_lines.append(line)
    if has_label:
        return "\n".join(u_lines), "\n".join(r_lines)
    return raw, ""


def _extract(text, source, *, allow_user=True, allow_self=True, allow_world=True):
    # KEKESI_PCE_P2_20261101_V3: polarity-aware, multi-label extraction.
    # (1) Dislikes are "aversion:" rows, not "preference:" rows. (2) User
    # preference, user goal and world are independent: one segment may
    # yield several rows. (3) Self rows stay exclusive with BAD winning;
    # a negated GOOD word flips to a limitation. Key derivation (seg[:80])
    # is unchanged, so existing rows keep upserting.
    # P1-6 source isolation:
    # - allow_user=False when extracting from assistant reply_text so assistant
    #   utterances never pollute user_model.
    # - non-declarative / question / hypothesis / second-person queries are
    #   skipped for user_model.
    rows = []
    for seg in _segments(text):
        key = seg[:80]
        val = seg[:500]
        spans = _title_spans(seg)
        if allow_user and not _is_non_declarative_or_other_subject(seg):
            pos_hit = any(
                not _negated_before(seg, i)
                for w in _USER_PREF_POS for i in _find_all(seg, w)
                if not _in_title(spans, i, len(w))
            )
            neg_hit = any(w in seg for w in _USER_PREF_NEG) or any(
                _negated_before(seg, i)
                for w in _USER_PREF_POS for i in _find_all(seg, w)
                if not _in_title(spans, i, len(w))
            )
            if pos_hit:
                rows.append(("user", "preference:" + key, val, source, "mentioned", "mentioned"))
            if neg_hit:
                rows.append(("user", "aversion:" + key, val, source, "mentioned", "mentioned"))
            goal_hit = any(
                not _negated_before(seg, i)
                for w in _USER_GOAL for i in _find_all(seg, w)
                if not _in_title(spans, i, len(w))
            )
            if goal_hit:
                rows.append(("user", "goal:" + key, val, source, "mentioned", "mentioned"))
        if allow_self:
            if any(w in seg for w in _SELF_BAD):
                rows.append(("self", "limitation:" + key, val, source, "mentioned", "mentioned"))
            else:
                good_pos = any(
                    not _negated_before(seg, i)
                    for w in _SELF_GOOD for i in _find_all(seg, w)
                    if not _in_title(spans, i, len(w))
                )
                good_neg = any(
                    _negated_before(seg, i)
                    for w in _SELF_GOOD for i in _find_all(seg, w)
                    if not _in_title(spans, i, len(w))
                )
                if good_pos:
                    rows.append(("self", "capability:" + key, val, source, "mentioned", "mentioned"))
                elif good_neg:
                    rows.append(("self", "limitation:" + key, val, source, "mentioned", "mentioned"))
        if allow_world and any(w in seg for w in _WORLD_WORDS):
            rel = "mentioned"
            for w in _WORLD_WORDS:
                if w in seg:
                    rel = w
                    break
            status = "active" if any(w in seg for w in _WORLD_ACTIVE) else "mentioned"
            rows.append(("world", "entity:" + key, val, source, rel, status))
    return rows


def update_models(
    digest_text,
    perception_text,
    db_path=None,
    *,
    user_text=None,
    reply_text=None,
):
    # KEKESI_PCE_P2_20261103_NOISE: filter system-event lines out of
    # perception only; user_text and reply_text are separated so assistant
    # expressions never enter user_model (only self_model / world_entities).
    u_text, r_text = _split_user_and_reply(
        digest_text, user_text=user_text, reply_text=reply_text
    )
    _seen = (
        _load_seen_prefixes(db_path)
        if "[user_turn]" in (perception_text or "")
        else set()
    )
    user_source = "user_turn" if (user_text is not None or reply_text is not None) else "digest"
    rows = []
    if u_text:
        rows.extend(
            _extract(
                u_text,
                user_source,
                allow_user=True,
                allow_self=True,
                allow_world=True,
            )
        )
    if r_text:
        # 助手回复绝不写入 user_model；仅提取自我模型（能力/局限）与世界实体
        rows.extend(
            _extract(
                r_text,
                "assistant_reply",
                allow_user=False,
                allow_self=True,
                allow_world=True,
            )
        )
    filtered_perc = _filter_perception_lines(perception_text, u_text or digest_text, _seen)
    if filtered_perc:
        # 感知流里仅 [user_turn] 行允许提取 user_model，其余感知事件只进 world/self
        perc_user_lines = []
        perc_other_lines = []
        for line in filtered_perc.splitlines():
            s = line.strip()
            if s.startswith("[user_turn]"):
                perc_user_lines.append(_user_turn_content(line) or line)
            else:
                perc_other_lines.append(line)
        if perc_user_lines:
            rows.extend(
                _extract(
                    "\n".join(perc_user_lines),
                    "perception:user_turn",
                    allow_user=True,
                    allow_self=True,
                    allow_world=True,
                )
            )
        if perc_other_lines:
            rows.extend(
                _extract(
                    "\n".join(perc_other_lines),
                    "perception",
                    allow_user=False,
                    allow_self=True,
                    allow_world=True,
                )
            )
    try:
        from .perception import recent_environment_summary
        sync_environment_state(recent_environment_summary(20), db_path=db_path)
    except Exception:
        pass
    if not rows:
        return 0
    conn = connect(db_path)
    try:
        for kind, key, val, source, rel, status in rows:
            _upsert(conn, TABLES[kind], key, val, source, rel, status)
        _record_seen_prefixes(conn, u_text or digest_text, perception_text)
        conn.commit()
    finally:
        conn.close()
    return len(rows)


def sync_environment_state(events, db_path=None):
    """Copy verified background state changes into world_entities with TTL/evidence."""
    status_by_type = {
        "window_focus": "focused", "window_create": "visible",
        "window_destroy": "gone", "window_rename": "renamed",
        "process_start": "running", "process_exit": "stopped",
        "service_change": "changed", "network_change": "changed",
        "file_change": "changed", "power_change": "changed", "device_change": "changed",
    }
    processed = 0
    conn = connect(db_path)
    try:
        now = time.time()
        # The sensor API returns newest-first; apply oldest-first so the
        # latest physical state wins when several events describe one entity.
        for event in reversed(list(events or [])):
            kind = str(event.get("event_type") or "")
            source = str(event.get("source") or "")
            if kind not in status_by_type or source in {"tool", "tool_result", "executor", "tool_call"}:
                continue
            summary = str(event.get("summary") or "").strip()
            if not summary:
                continue
            detail = event.get("detail") or ""
            parsed = {}
            if isinstance(detail, dict):
                parsed = detail
            elif isinstance(detail, str):
                try:
                    maybe = json.loads(detail)
                    parsed = maybe if isinstance(maybe, dict) else {}
                except (TypeError, json.JSONDecodeError):
                    parsed = {}
            identity = next((str(parsed[k]).strip() for k in ("process", "process_name", "name", "title", "window_title", "path", "pid") if parsed.get(k) not in (None, "")), summary)
            domain = "process" if kind.startswith("process_") else "window" if kind.startswith("window_") else kind.split("_", 1)[0]
            key = (
                "environment:window:foreground"
                if kind == "window_focus"
                else f"environment:{domain}:{identity[:100]}"
            )
            ttl = 3600 if domain == "process" else 900 if domain == "window" else 1800
            evidence = json.dumps({"event_id": event.get("id"), "event_type": kind, "source": source, "summary": summary, "detail": parsed, "observed_at": event.get("ts")}, ensure_ascii=False, default=str)
            previous = conn.execute(
                "SELECT evidence_json FROM world_entities WHERE key=?", (key,)
            ).fetchone()
            if previous:
                try:
                    previous_id = json.loads(previous[0] or "{}").get("event_id")
                    current_id = event.get("id")
                    if previous_id == current_id or (
                        isinstance(previous_id, (int, float))
                        and isinstance(current_id, (int, float))
                        and current_id <= previous_id
                    ):
                        continue
                except (TypeError, json.JSONDecodeError):
                    pass
            conn.execute(
                "INSERT INTO world_entities(key,value,relation,status,source,occurrences,updated_at,validity,confidence,expires_at,evidence_json) "
                "VALUES(?,?,?,?,?,1,?,'CONFIRMED_FACT',0.9,?,?) ON CONFLICT(key) DO UPDATE SET "
                "value=excluded.value,relation=excluded.relation,status=excluded.status,source=excluded.source, "
                "occurrences=world_entities.occurrences+1,updated_at=excluded.updated_at,validity='CONFIRMED_FACT', "
                "confidence=0.9,expires_at=excluded.expires_at,evidence_json=excluded.evidence_json",
                (key, summary, domain, status_by_type[kind], f"perception:{kind}", now, now + ttl, evidence),
            )
            processed += 1
        conn.commit()
    finally:
        conn.close()
    return processed


def sync_episodes_to_cognition(episodes=None, db_path=None):
    """Sync multi-event verified episodes into world_entities without polluting user/self models.

    Requirements enforced:
    - Single-event or unverified episodes (`event_count < 2` or empty `event_ids`) NEVER modify global cognition.
    - Distinguishes current episodes (`temporal_tag='当前事件片段'`) from historical episodes (`temporal_tag='历史情景片段'`).
    - On conflicting value for the same scene entity, preserves both sources and timestamps in `evidence_json['history']`
      and marks `validity='CONFLICTED'` instead of blindly overwriting.
    """
    if episodes is None:
        from .episodes import EpisodeStore

        episodes = EpisodeStore().query_relevant_episodes("", limit=5)
    conn = connect(db_path)
    now = time.time()
    updated = 0
    try:
        for ep in episodes or []:
            if not isinstance(ep, dict):
                continue
            ev_ids = ep.get("event_ids") or []
            ev_cnt = int(ep.get("event_count") or len(ev_ids) or 0)
            # Gate: unverified or single-event episodes must not modify global cognition
            if ev_cnt < 2 or len(ev_ids) < 2:
                continue
            summary = str(ep.get("scene_summary") or "").strip()
            if not summary:
                continue
            entities = ep.get("scene_entities") or []
            entity_id = str(entities[0] if entities else (ep.get("split_reason") or ep.get("episode_id") or "scene")).strip()[:80]
            key = f"episode_pattern:{entity_id}"
            ep_id = str(ep.get("episode_id") or "")
            start_t = str(ep.get("start_time") or "")
            end_t = str(ep.get("end_time") or "")
            is_cur = bool(ep.get("is_current"))
            temporal = "current_episode" if is_cur else "historical_episode"
            src_str = f"episodes:{ep_id}"

            prev_row = conn.execute(
                "SELECT value, source, occurrences, validity, confidence, evidence_json FROM world_entities WHERE key=?",
                (key,),
            ).fetchone()

            new_ev_entry = {
                "episode_id": ep_id,
                "event_ids": ev_ids[:20],
                "event_count": ev_cnt,
                "start_time": start_t,
                "end_time": end_t,
                "temporal": temporal,
                "summary": summary[:160],
                "sources": ep.get("sources") or [],
            }

            if prev_row is None:
                # First observation of this episode pattern -> INFERRED_HYPOTHESIS until multi-episode evidence
                ev_json = json.dumps({"latest": new_ev_entry, "history": [new_ev_entry]}, ensure_ascii=False)
                conn.execute(
                    "INSERT INTO world_entities(key,value,relation,status,source,occurrences,updated_at,validity,confidence,expires_at,evidence_json) "
                    "VALUES(?,?,?,?,?,1,?,'INFERRED_HYPOTHESIS',0.6,?,?)",
                    (key, summary[:200], "episode_scene", temporal, src_str, now, now + 7 * 86400, ev_json),
                )
                updated += 1
            else:
                prev_val, prev_src, prev_occ, prev_val_state, prev_conf, prev_ev_raw = prev_row
                try:
                    prev_ev = json.loads(prev_ev_raw or "{}")
                except Exception:
                    prev_ev = {}
                hist = prev_ev.get("history") if isinstance(prev_ev.get("history"), list) else []
                if any(isinstance(h, dict) and h.get("episode_id") == ep_id for h in hist):
                    continue
                hist.append(new_ev_entry)
                hist = hist[-10:]
                new_occ = int(prev_occ or 1) + 1

                # Conflict detection: if previous summary & new summary describe opposite actions on same entity
                is_conflict = (
                    prev_val
                    and prev_val != summary[:200]
                    and (
                        ("关闭" in prev_val and "打开" in summary)
                        or ("打开" in prev_val and "关闭" in summary)
                        or ("失败" in prev_val and "成功" in summary)
                        or ("成功" in prev_val and "失败" in summary)
                    )
                )
                if is_conflict:
                    merged_val = f"[当前/最新({end_t[:16]}): {summary[:90]}] vs [历史({prev_src}): {str(prev_val)[:90]}]"
                    validity = "CONFLICTED"
                    conf = 0.55
                else:
                    merged_val = summary[:200]
                    validity = "CONFIRMED_FACT" if new_occ >= 2 else "INFERRED_HYPOTHESIS"
                    conf = min(0.9, 0.6 + 0.15 * (new_occ - 1))

                ev_json = json.dumps(
                    {
                        "latest": new_ev_entry,
                        "previous_source": prev_src,
                        "conflict_preserved": bool(is_conflict),
                        "history": hist,
                    },
                    ensure_ascii=False,
                )
                conn.execute(
                    "UPDATE world_entities SET value=?, relation='episode_scene', status=?, source=?, "
                    "occurrences=?, updated_at=?, validity=?, confidence=?, expires_at=?, evidence_json=? WHERE key=?",
                    (merged_val, temporal, src_str, new_occ, now, validity, conf, now + 7 * 86400, ev_json, key),
                )
                updated += 1
        conn.commit()
    finally:
        conn.close()
    return updated


def episode_context_for_llm(
    query="",
    *,
    entity_hints=None,
    limit=2,
    max_chars=240,
    episodes_db_path=None,
    perception_db_path=None,
    cognition_db_path=None,
):
    """Retrieve task/scene-relevant episodes, sync multi-event evidence to cognition, and render context."""
    from .episodes import EpisodeStore

    store = EpisodeStore(episodes_db_path)
    eps = store.query_relevant_episodes(
        query,
        entity_hints=entity_hints,
        limit=limit,
        perception_db_path=perception_db_path,
    )
    if not eps:
        return "", []
    try:
        sync_episodes_to_cognition(eps, db_path=cognition_db_path)
    except Exception:
        pass
    lines = []
    audit_ids = []
    for ep in eps[: int(limit)]:
        tag = ep.get("temporal_tag") or ("当前事件片段" if ep.get("is_current") else "历史情景片段")
        t_str = str(ep.get("end_time") or ep.get("start_time") or "")[:16]
        s_text = str(ep.get("scene_summary") or "")[:90]
        lines.append(f"- [{tag}·{t_str}] {s_text}")
        if ep.get("episode_id"):
            audit_ids.append(f"p5:episode:{ep['episode_id']}")
    body = "\n".join(lines)
    if len(body) > max_chars:
        body = body[: max_chars - 1] + "…"
    return body, audit_ids


def query_model(kind, key=None, db_path=None):
    table = TABLES.get(kind)
    if table is None:
        raise ValueError("unknown model kind: %r (want world/user/self)" % (kind,))
    conn = connect(db_path)
    try:
        conn.execute(
            "UPDATE %s SET validity='EXPIRED' WHERE expires_at IS NOT NULL AND expires_at<=? AND validity!='EXPIRED'" % table,
            (time.time(),),
        )
        conn.commit()
        if key:
            cur = conn.execute("SELECT * FROM %s WHERE key = ?" % table, (key,))
            row = cur.fetchone()
            if row is None:
                return None
            cols = [d[0] for d in cur.description]
            return dict(zip(cols, row))
        cur = conn.execute(
            "SELECT * FROM %s ORDER BY occurrences DESC, updated_at DESC" % table
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()


_CAPABILITY_STATES = {"UNKNOWN", "UNVERIFIED", "HEALTHY", "DEGRADED", "BROKEN"}
_COGNITION_VALIDITY = {"CONFIRMED_FACT", "INFERRED_HYPOTHESIS", "EXPIRED", "CONFLICTED"}


def update_capability_health(
    tool_name,
    *,
    state=None,
    source="execution",
    evidence=None,
    success=None,
    failure_class="tool",
    ttl_sec=None,
    db_path=None,
):
    """Persist evidence-backed capability health without overreacting to one failure."""
    name = str(tool_name or "").strip()
    if not name:
        raise ValueError("tool_name is required")
    key = "capability_health:" + name
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT success_count, failure_count, health_state, expires_at FROM self_model WHERE key=?",
            (key,),
        ).fetchone()
        successes = int(row[0] or 0) if row else 0
        failures = int(row[1] or 0) if row else 0
        previous = str(row[2] or "UNKNOWN") if row else "UNKNOWN"
        old_expires = row[3] if row else None
        if row and row[3] is not None and float(row[3]) <= time.time() and previous in {"DEGRADED", "HEALTHY", "UNVERIFIED"}:
            previous = "UNKNOWN"
        if success is True:
            successes += 1
            score = successes / max(1, successes + failures)
            if successes >= 3 and score >= 0.8:
                resolved = "HEALTHY"
            elif previous == "BROKEN":
                resolved = "BROKEN"
            elif previous == "DEGRADED" or failures:
                resolved = "DEGRADED"
            else:
                resolved = "UNVERIFIED"
        elif success is False and str(failure_class or "tool") == "tool":
            failures += 1
            score = successes / max(1, successes + failures)
            resolved = "BROKEN" if failures >= 3 and score < 0.4 else "DEGRADED"
        else:
            requested = str(state or previous)
            if requested == "BROKEN":
                resolved = "BROKEN"
            elif previous == "BROKEN" and requested != "HEALTHY":
                resolved = "BROKEN"
            elif previous == "DEGRADED" and requested in {"UNKNOWN", "UNVERIFIED"}:
                resolved = "DEGRADED"
            else:
                resolved = requested
            if resolved not in _CAPABILITY_STATES:
                resolved = "UNKNOWN"
            score = successes / max(1, successes + failures) if successes + failures else 0.0
        now = time.time()
        if ttl_sec is not None:
            expires = now + max(1, int(ttl_sec))
        elif source == "skill_registry.static_check" and resolved == previous and previous in {"BROKEN", "DEGRADED"}:
            expires = old_expires
        elif resolved == "DEGRADED":
            expires = now + 6 * 3600
        elif resolved == "HEALTHY":
            expires = now + 24 * 3600
        else:
            expires = None
        validity = "CONFIRMED_FACT" if resolved in {"HEALTHY", "BROKEN"} else "INFERRED_HYPOTHESIS"
        serialized = json.dumps(evidence or {}, ensure_ascii=False, default=str)
        conn.execute(
            "INSERT INTO self_model(key,value,source,occurrences,updated_at,validity,confidence,expires_at,health_state,evidence_json,success_count,failure_count) "
            "VALUES(?,?,?,1,?,?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
            "value=excluded.value, source=excluded.source, occurrences=self_model.occurrences+1, "
            "updated_at=excluded.updated_at, validity=excluded.validity, confidence=excluded.confidence, "
            "expires_at=excluded.expires_at, health_state=excluded.health_state, "
            "evidence_json=excluded.evidence_json, success_count=excluded.success_count, failure_count=excluded.failure_count",
            (key, resolved, str(source or ""), now, validity, max(0.0, min(1.0, score)), expires, resolved, serialized, successes, failures),
        )
        conn.execute(
            "INSERT INTO capability_health_events(tool_name,previous_state,new_state,source,failure_class,task_id,confidence,evidence_json,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (name, previous, resolved, str(source or ""), str(failure_class or ""), str((evidence or {}).get("task_id") or ""), max(0.0, min(1.0, score)), serialized, now),
        )
        conn.commit()
        return {
            "tool_name": name, "state": resolved, "previous_state": previous,
            "confidence": max(0.0, min(1.0, score)), "success_count": successes,
            "failure_count": failures, "source": source, "evidence": evidence or {},
            "expires_at": expires,
        }
    finally:
        conn.close()


def query_capability_health(tool_name, db_path=None):
    name = str(tool_name or "").strip()
    if not name:
        return None
    key = "capability_health:" + name
    conn = connect(db_path)
    try:
        row = conn.execute("SELECT * FROM self_model WHERE key=?", (key,)).fetchone()
        if row is None:
            return None
        cols = [d[0] for d in conn.execute("SELECT * FROM self_model LIMIT 0").description]
        result = dict(zip(cols, row))
        expires = result.get("expires_at")
        if expires is not None and float(expires) <= time.time() and result.get("health_state") in {"DEGRADED", "HEALTHY", "UNVERIFIED"}:
            conn.execute(
                "UPDATE self_model SET health_state='UNKNOWN',value='UNKNOWN',validity='EXPIRED',confidence=0,updated_at=? WHERE key=?",
                (time.time(), key),
            )
            conn.commit()
            result.update({"health_state": "UNKNOWN", "value": "UNKNOWN", "validity": "EXPIRED", "confidence": 0.0})
        try:
            result["evidence"] = json.loads(result.get("evidence_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            result["evidence"] = {}
        result["tool_name"] = name
        return result
    finally:
        conn.close()


def query_capability_health_history(tool_name, limit=20, db_path=None):
    name = str(tool_name or "").strip()
    if not name:
        return []
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT previous_state,new_state,source,failure_class,task_id,confidence,evidence_json,created_at "
            "FROM capability_health_events WHERE tool_name=? ORDER BY id DESC LIMIT ?",
            (name, max(1, int(limit))),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(zip(("previous_state", "new_state", "source", "failure_class", "task_id", "confidence", "evidence_json", "created_at"), row))
            try:
                item["evidence"] = json.loads(item.pop("evidence_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                item["evidence"] = {}
            result.append(item)
        return result
    finally:
        conn.close()


def capability_health_context(db_path=None, limit=20):
    conn = connect(db_path)
    try:
        now = time.time()
        conn.execute(
            "UPDATE self_model SET health_state='UNKNOWN',value='UNKNOWN',validity='EXPIRED',confidence=0,updated_at=? "
            "WHERE key LIKE 'capability_health:%' AND expires_at IS NOT NULL AND expires_at<=? "
            "AND health_state IN ('DEGRADED','HEALTHY','UNVERIFIED')",
            (now, now),
        )
        conn.commit()
        rows = conn.execute(
            "SELECT key,health_state,evidence_json FROM self_model WHERE key LIKE 'capability_health:%' "
            "ORDER BY updated_at DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    finally:
        conn.close()
    lines = []
    for key, state, evidence_json in rows:
        state = str(state or "UNKNOWN")
        if state not in _CAPABILITY_STATES:
            continue
        evidence = ""
        try:
            detail = json.loads(evidence_json or "{}")
            evidence = str(detail.get("reason") or detail.get("error") or detail.get("check") or "")[:80]
        except (TypeError, json.JSONDecodeError):
            pass
        lines.append(f"- {str(key).split(':', 1)[-1]}: {state}" + (f"（{evidence}）" if evidence else ""))
    if not lines:
        return ""
    return "【能力健康状态】\nBROKEN 工具禁止调用；DEGRADED 优先采用替代方案；UNKNOWN/UNVERIFIED 先验证再依赖。\n" + "\n".join(lines)


def query_capability_health_many(tool_names, db_path=None):
    names = [str(n).strip() for n in (tool_names or []) if str(n).strip()]
    if not names:
        return {}
    keys = ["capability_health:" + name for name in names]
    marks = ",".join("?" for _ in keys)
    conn = connect(db_path)
    try:
        rows = conn.execute(
            f"SELECT key,health_state,expires_at FROM self_model WHERE key IN ({marks})",
            keys,
        ).fetchall()
        now = time.time()
        return {
            str(key).split(":", 1)[-1]: str(state or "UNKNOWN")
            for key, state, expires in rows
            if not (expires is not None and float(expires) <= now)
        }
    finally:
        conn.close()


def model_rows_for_llm(db_path=None, per_kind=10):
    """Selected P2 model rows in render order (world, user, self).

    KEKESI_PCE_P4_20261103_WIRE: shared by model_context_for_llm and the
    context_builder audit wiring so the selection never drifts between
    the prompt text and the audit.
    """
    rows = []
    conn = connect(db_path)
    try:
        for kind in ("world", "user", "self"):
            table = TABLES[kind]
            cur = conn.execute(
                "SELECT key, value FROM %s WHERE validity != 'EXPIRED' AND (expires_at IS NULL OR expires_at > ?) ORDER BY occurrences DESC, updated_at DESC LIMIT?" % table,
                (time.time(), per_kind),
            )
            for k, v in cur.fetchall():
                rows.append((kind, k, v))
    finally:
        conn.close()
    return rows


def model_context_for_llm(db_path=None, per_kind=10, max_chars=1500):
    from .tombstones import filter_rows, main_db_path_for_store
    rows = filter_rows(
        model_rows_for_llm(db_path=db_path, per_kind=per_kind),
        main_db_path_for_store(db_path), lambda r: " ".join(str(x) for x in r),
    )
    titles = {"world": "世界模型", "user": "用户模型", "self": "自我模型"}
    blocks = []
    current = None
    current_lines = []
    for kind, k, v in rows:
        if kind != current:
            if current_lines:
                blocks.append("【%s】\n%s" % (titles[current], "\n".join(current_lines)))
            current = kind
            current_lines = []
        current_lines.append("- %s: %s" % (k, v))
    if current_lines:
        blocks.append("【%s】\n%s" % (titles[current], "\n".join(current_lines)))
    text = "\n".join(blocks)
    if len(text) > max_chars:
        cut = text[:max_chars].rsplit("\n", 1)[0]
        text = cut if cut else text[:max_chars]
    return text


def prepend_models(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            models = model_context_for_llm()
        except Exception:
            models = ""
        base = fn(*args, **kwargs)
        if isinstance(base, str) and base:
            return (models + "\n" + base) if models else base
        if models:
            return models
        return base

    return wrapper
