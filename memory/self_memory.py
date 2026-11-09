"""自我记忆层（Self Memory 1.0）。

把各插件的第一人称经历（目前只有 douyin_browse 刷到的见闻）清洗进 kekesi.db 的
self_memory 表，分三层供给：

- L0 常驻见闻：每轮无条件注入，最多 2 条、合计 ≤100 字，带冷却与轮换。
- L1 经历明细：只在被问/被提及时注入，本地关键词与实体相关性检索，保留来源。
- L2 后台清洗：10 分钟一轮的增量摄取 + 向量化 + 聚类去重 + 热度累计。

铁律：每轮路径只做本地 SQLite 读；embedding 是远程 API，只允许出现在 L2。
tier=3 预留给 L3 沉淀认知，本模块不实现读写。
"""

from __future__ import annotations

import array
import hashlib
import logging
import math
import re
import sqlite3
import threading
import time

logger = logging.getLogger(__name__)

_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
_WORD_RE = re.compile(r"[A-Za-z0-9]{2,}")
_STOP = {
    "什么", "怎么", "为什么", "这个", "那个", "一个", "我们", "你们", "他们",
    "就是", "还是", "不是", "有的", "时候", "自己", "真的", "感觉", "觉得",
    "然后", "可以", "没有", "一下",
}

# L0 排序权重：相关性压倒性主导（用户定的口径——"围绕讨论的、相关的为主"）
W_RELEVANCE = 1.0
W_HEAT = 0.4
W_FRESH = 0.3
W_VALUE = 0.2
_FRESH_HALFLIFE_HOURS = 72.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS self_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tier INTEGER DEFAULT 0,
    topic TEXT DEFAULT '',
    text TEXT NOT NULL,
    kind TEXT DEFAULT 'viewpoint',
    source_plugin TEXT DEFAULT '',
    source_key TEXT DEFAULT '',
    tokens TEXT DEFAULT '',
    heat REAL DEFAULT 1.0,
    value REAL DEFAULT 0.5,
    embedding BLOB,
    first_ts REAL DEFAULT 0,
    last_ts REAL DEFAULT 0,
    injected_ts REAL DEFAULT 0,
    inject_count INTEGER DEFAULT 0,
    status TEXT DEFAULT 'active',
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_self_memory_active
    ON self_memory(status, tier, injected_ts);
CREATE UNIQUE INDEX IF NOT EXISTS idx_self_memory_source
    ON self_memory(source_plugin, source_key) WHERE source_key != '';
"""

# 每条视频最多拆几行，避免一次刷 12 条就把见闻塞满
_KIND_VALUE = {"viewpoint": 0.7, "fact": 0.6, "feeling": 0.5}
_MAX_ROWS_PER_ITEM = 3
_MAX_SERIES = 2
# embedding 拿不到时的关键词同题阈值（双向覆盖率，要求几乎全等）
_FALLBACK_SIM = 0.90
# 插件侧 recent() 自己就夹到 50，这里按同一口径取，别假装能一次读 200 条
_LOAD_LIMIT = 50


# 向量补齐最多重试几轮；超过后放弃该行（仍可走关键词去重/检索）
_MAX_EMBED_ATTEMPTS = 5
_EMBED_RETRIES = 3
_EMBED_RETRY_DELAY = 2.0
_BACKFILL_BATCH = 32


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(self_memory)").fetchall()}
    if "embed_attempts" not in cols:
        conn.execute("ALTER TABLE self_memory ADD COLUMN embed_attempts INTEGER DEFAULT 0")
        conn.commit()


def _release(conn: sqlite3.Connection) -> None:
    """进入网络 I/O 前确保本连接没有挂着事务（读快照/写锁都放掉）。"""
    if conn.in_transaction:
        conn.commit()


def _embed_with_retry(
    embedder,
    texts: list[str],
    *,
    retries: int = _EMBED_RETRIES,
    delay: float = _EMBED_RETRY_DELAY,
    sleep=time.sleep,
) -> list[list[float] | None] | None:
    """远程 embedding（调用方保证此时不持有任何 SQLite 事务）。

    失败按指数退避重试；全部失败返回 None，由调用方降级为关键词去重，
    未拿到向量的行留 embedding=NULL，后续由 backfill_embeddings 补齐。
    """
    if embedder is None or not texts:
        return None
    wait = max(0.0, float(delay))
    for attempt in range(1, max(1, retries) + 1):
        try:
            got = embedder(list(texts))
            if got and len(got) == len(texts):
                return [list(v) if v else None for v in got]
        except Exception:
            logger.debug("自我记忆向量化失败（第 %d 次）", attempt, exc_info=True)
        if attempt < retries and wait > 0:
            sleep(wait)
            wait *= 2
    return None


def _forgotten(conn: sqlite3.Connection) -> list[str]:
    try:
        from .forget import load_tombstones

        return load_tombstones(conn)
    except Exception:
        return []


def _tokens(text: str) -> list[str]:
    out: list[str] = []
    for word in _WORD_RE.findall(text or ""):
        out.append(word.lower())
    for chunk in _CJK_RE.findall(text or ""):
        if len(chunk) <= 2:
            out.append(chunk)
        else:
            out.extend(chunk[i : i + 2] for i in range(len(chunk) - 1))
    seen: set[str] = set()
    kept = []
    for token in out:
        if token in _STOP or token in seen:
            continue
        seen.add(token)
        kept.append(token)
    return kept[:48]


def _token_blob(text: str) -> str:
    return "|".join(_tokens(text))


def _relevance(query_tokens: list[str], blob: str) -> float:
    """当前话与条目的关键词重合度（0~1，按查询侧覆盖率算）。"""
    if not query_tokens or not blob:
        return 0.0
    row = set(blob.split("|"))
    if not row:
        return 0.0
    hits = sum(1 for t in query_tokens if t in row)
    if hits == 0:
        return 0.0
    return hits / len(query_tokens)


def _overlap(tokens: list[str], blob: str) -> float:
    """双向覆盖率取小，判「是不是同一条见闻」；只看查询侧会让短句吞掉长条目、把不同视频并成一条。"""
    if not tokens or not blob:
        return 0.0
    row = set(blob.split("|"))
    picked = set(tokens)
    if not row or not picked:
        return 0.0
    forward = sum(1 for t in picked if t in row) / len(picked)
    backward = sum(1 for t in row if t in picked) / len(row)
    return min(forward, backward)


def _pack(vector: list[float]) -> bytes:
    return array.array("f", vector).tobytes()


def _unpack(blob) -> list[float] | None:
    if not blob:
        return None
    arr = array.array("f")
    try:
        arr.frombytes(bytes(blob))
    except Exception:
        return None
    return list(arr)


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if not na or not nb:
        return 0.0
    return dot / (na * nb)


def _entries_from_item(item: dict) -> list[tuple[str, str]]:
    """一条插件经历拆成 (kind, text)；来源原句保留，不做二次改写。

    感受与事实各占固定名额，剩下的名额才给梗标签——别让一次刷视频只剩标签。
    """
    title = str(item.get("title") or "")[:40]
    body = str(item.get("text") or "").strip()
    fact = str(item.get("fact") or "").strip()
    out: list[tuple[str, str]] = []
    if body:
        out.append(("feeling", body))
    tags = item.get("tags") or []
    if isinstance(tags, str):
        tags = [tags]
    budget = _MAX_ROWS_PER_ITEM - len(out) - (1 if fact else 0)
    series = [str(t).strip() for t in tags if str(t).strip()]
    for tag in series[: max(0, min(_MAX_SERIES, budget))]:
        out.append(("viewpoint", tag))
    if fact:
        out.append(("fact", f"{fact}（来自《{title}》）" if title else fact))
    return out[:_MAX_ROWS_PER_ITEM]


def _source_key(item: dict, index: int, text: str) -> str:
    video = str(item.get("video_id") or item.get("ts") or "")
    digest = hashlib.md5(text.encode("utf-8")).hexdigest()[:8]
    return f"{video}:{index}:{digest}"


def upsert_experiences(
    conn: sqlite3.Connection,
    items: list[dict],
    *,
    plugin: str,
    embedder=None,
    sim_threshold: float = 0.86,
    now: float | None = None,
    embed_retries: int = _EMBED_RETRIES,
    embed_retry_delay: float = _EMBED_RETRY_DELAY,
) -> dict:
    """L2 摄取：精确去重 → 向量聚类合并热度 → 落库。

    三段式，远程 I/O 绝不占用 SQLite 事务：
    1. 读：取已知来源键，剔除见过的候选（不开写事务）；
    2. 事务外：对新候选做 embedding（带重试）；
    3. 短写事务（BEGIN IMMEDIATE）：重新读取聚类池与来源键后合并/插入，一次提交。
    """
    stats = {"seen": 0, "added": 0, "merged": 0, "dup": 0, "failed": 0}
    if not items:
        return stats
    now = float(now if now is not None else time.time())

    candidates: list[tuple[str, dict, str, str]] = []  # (source_key, item, kind, text)
    for item in items:
        index = -1
        for kind, text in _entries_from_item(item):
            index += 1
            candidates.append((_source_key(item, index, text), item, kind, text))
    stats["seen"] = len(candidates)
    if not candidates:
        return stats

    # ── 阶段 1：只读 ──
    known_keys = {
        str(r[0])
        for r in conn.execute(
            "SELECT source_key FROM self_memory WHERE source_plugin = ?", (plugin,)
        ).fetchall()
    }
    tombstones = _forgotten(conn)
    _release(conn)

    # 先按来源剔掉见过的，再向量化——embedding 是远程调用，重复摄取不该花钱
    fresh = []
    for cand in candidates:
        if cand[0] in known_keys:
            stats["dup"] += 1
        elif tombstones and hits_forgotten(cand, tombstones):
            stats["dup"] += 1  # 用户要求遗忘过的内容不再写回
        else:
            fresh.append(cand)
    if not fresh:
        return stats

    # ── 阶段 2：事务外远程 embedding ──
    vectors: list[list[float] | None] = [None] * len(fresh)
    if embedder is not None:
        got = _embed_with_retry(
            embedder,
            [c[3] for c in fresh],
            retries=embed_retries,
            delay=embed_retry_delay,
        )
        if got is not None:
            vectors = got
        else:
            logger.debug("自我记忆向量化失败，退回关键词去重，待后台补齐")

    # ── 阶段 3：短写事务 ──
    _release(conn)
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = conn.execute(
            """SELECT id, tokens, embedding, heat FROM self_memory
               WHERE source_plugin = ? AND tier = 0 AND status = 'active'""",
            (plugin,),
        ).fetchall()
        known_keys = {
            str(r[0])
            for r in conn.execute(
                "SELECT source_key FROM self_memory WHERE source_plugin = ?", (plugin,)
            ).fetchall()
        }
        pool = [
            {
                "id": r[0],
                "tokens": str(r[1] or ""),
                "vector": _unpack(r[2]),
                "heat": float(r[3] or 1.0),
            }
            for r in existing
        ]

        for pos, (key, item, kind, text) in enumerate(fresh):
            if key in known_keys:  # 同批重复，或 embedding 期间别处已写入
                stats["dup"] += 1
                continue
            vec = vectors[pos]
            text_tokens = _tokens(text)
            best_id, best_margin = None, -1.0
            for row in pool:
                if row["vector"] and vec:
                    score, floor = _cosine(vec, row["vector"]), sim_threshold
                else:
                    score, floor = _overlap(text_tokens, row["tokens"]), _FALLBACK_SIM
                margin = score - floor
                if margin > best_margin:
                    best_id, best_margin = row["id"], margin
            ts = float(item.get("ts") or now)
            if best_id is not None and best_margin >= 0.0:
                conn.execute(
                    """UPDATE self_memory SET heat = heat + 1, last_ts = ?
                       WHERE id = ?""",
                    (ts, best_id),
                )
                stats["merged"] += 1
                known_keys.add(key)
                continue
            if vec is None and embedder is not None:
                # 向量拿不到但条目确实新：照常入库，embedding 留空等后台补齐
                stats["failed"] += 1
            title = str(item.get("title") or "")[:40]
            conn.execute(
                """INSERT INTO self_memory
                   (tier, topic, text, kind, source_plugin, source_key, tokens, heat, value,
                    embedding, first_ts, last_ts)
                   VALUES (0, ?, ?, ?, ?, ?, ?, 1.0, ?, ?, ?, ?)""",
                (
                    title,
                    text[:200],
                    kind,
                    plugin,
                    key,
                    _token_blob(f"{title} {text}"),
                    _KIND_VALUE.get(kind, 0.5),
                    _pack(vec) if vec else None,
                    ts,
                    ts,
                ),
            )
            stats["added"] += 1
            known_keys.add(key)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return stats


def hits_forgotten(cand: tuple[str, dict, str, str], tombstones: list[str]) -> bool:
    _key, item, _kind, text = cand
    blob = f"{item.get('title') or ''} {text}".lower()
    return any(t.lower() in blob for t in tombstones if t)


def backfill_embeddings(
    connect,
    embedder,
    *,
    batch: int = _BACKFILL_BATCH,
    max_attempts: int = _MAX_EMBED_ATTEMPTS,
    embed_retries: int = _EMBED_RETRIES,
    embed_retry_delay: float = _EMBED_RETRY_DELAY,
) -> dict:
    """任务恢复：给 embedding 仍为空的行补向量（读 → 事务外 embedding → 短写回）。

    embedding 为空的行本身就是持久化的待办队列，进程重启不丢；
    每轮失败 embed_attempts+1，超过 max_attempts 不再重试。
    """
    stats = {"pending": 0, "filled": 0, "failed": 0}
    if embedder is None:
        return stats
    conn = connect()
    try:
        ensure_schema(conn)
        rows = conn.execute(
            """SELECT id, text FROM self_memory
               WHERE embedding IS NULL AND status = 'active'
                 AND COALESCE(embed_attempts, 0) < ?
               ORDER BY id DESC LIMIT ?""",
            (int(max_attempts), max(1, int(batch))),
        ).fetchall()
        _release(conn)
    finally:
        conn.close()
    if not rows:
        return stats
    ids = [int(r[0]) for r in rows]
    texts = [str(r[1] or "") for r in rows]
    stats["pending"] = len(ids)

    vectors = _embed_with_retry(
        embedder, texts, retries=embed_retries, delay=embed_retry_delay
    )

    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        for pos, row_id in enumerate(ids):
            vec = vectors[pos] if vectors else None
            if vec:
                # 只填仍为空的行：期间被删/被别的线程填过都不覆盖
                cur = conn.execute(
                    "UPDATE self_memory SET embedding = ? WHERE id = ? AND embedding IS NULL",
                    (_pack(vec), row_id),
                )
                stats["filled"] += max(0, cur.rowcount)
            else:
                conn.execute(
                    """UPDATE self_memory SET embed_attempts = COALESCE(embed_attempts, 0) + 1
                       WHERE id = ? AND embedding IS NULL""",
                    (row_id,),
                )
                stats["failed"] += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return stats


def trim(conn: sqlite3.Connection, max_rows: int = 800) -> int:
    """按热度+新鲜度保留上限，多余的先淘汰已注入过的老条目。"""
    total = int(conn.execute("SELECT COUNT(*) FROM self_memory").fetchone()[0] or 0)
    if total <= max_rows:
        return 0
    cur = conn.execute(
        """DELETE FROM self_memory WHERE id IN (
               SELECT id FROM self_memory
               ORDER BY (injected_ts > 0) DESC, heat ASC, last_ts ASC
               LIMIT ?
           )""",
        (total - max_rows,),
    )
    conn.commit()
    return cur.rowcount


def pick_injections(
    conn: sqlite3.Connection,
    user_text: str,
    *,
    limit: int = 2,
    max_chars: int = 100,
    cooldown_sec: float = 1800.0,
    now: float | None = None,
) -> list[dict]:
    """L0 选条：相关性优先，其次热度/新鲜/价值；冷却期内注入过的先靠边。"""
    now = float(now if now is not None else time.time())
    # tokens 列在写入时就算好，每轮只做交集，不做分词或远程调用
    rows = conn.execute(
        """SELECT id, text, topic, kind, tokens, heat, value, last_ts, injected_ts
           FROM self_memory WHERE tier = 0 AND status = 'active'
           ORDER BY last_ts DESC LIMIT 300"""
    ).fetchall()
    if not rows:
        return []
    q_tokens = _tokens(user_text)
    scored = []
    for r in rows:
        rel = _relevance(q_tokens, str(r["tokens"] or ""))
        heat = min(1.0, math.log1p(float(r["heat"] or 1.0)) / math.log(10))
        age_hours = max(0.0, (now - float(r["last_ts"] or now)) / 3600.0)
        fresh = math.exp(-age_hours / _FRESH_HALFLIFE_HOURS)
        score = W_RELEVANCE * rel + W_HEAT * heat + W_FRESH * fresh + W_VALUE * float(
            r["value"] or 0.5
        )
        if now - float(r["injected_ts"] or 0.0) < cooldown_sec:
            score -= 0.8  # 冷却期内轮换到别的见闻
        scored.append((score, rel, r))
    scored.sort(key=lambda x: x[0], reverse=True)

    chosen = []
    used = 0
    for score, rel, row in scored:
        if len(chosen) >= max(1, limit):
            break
        text = str(row["text"] or "").strip()
        if not text:
            continue
        cost = len(text) + 4
        if used and used + cost > max_chars:
            break
        used += cost
        chosen.append(dict(row, score=score, rel=rel))
    if chosen:
        marks = ",".join("?" * len(chosen))
        conn.execute(
            f"""UPDATE self_memory SET injected_ts = ?, inject_count = inject_count + 1
                WHERE id IN ({marks})""",
            [now] + [c["id"] for c in chosen],
        )
        conn.commit()
    return chosen


def search_details(
    conn: sqlite3.Connection,
    text: str,
    *,
    limit: int = 4,
    days: float = 30.0,
    now: float | None = None,
) -> list[dict]:
    """L1 明细：本地关键词相关性，带来源；不做任何远程调用。"""
    now = float(now if now is not None else time.time())
    q_tokens = _tokens(text)
    if not q_tokens:
        return []
    rows = conn.execute(
        """SELECT text, topic, kind, tokens, source_plugin, last_ts, heat FROM self_memory
           WHERE status = 'active' AND last_ts >= ?
           ORDER BY last_ts DESC LIMIT 400""",
        (now - max(1.0, days) * 86400.0,),
    ).fetchall()
    hits = []
    for r in rows:
        rel = _relevance(q_tokens, str(r["tokens"] or ""))
        if rel <= 0.0:
            continue
        hits.append((rel, float(r["heat"] or 1.0), r))
    hits.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [dict(r, rel=rel) for rel, _h, r in hits[: max(1, limit)]]


def format_l0(rows: list[dict], *, header: str) -> str:
    if not rows:
        return ""
    lines = [header]
    for r in rows:
        lines.append(f"- {str(r['text'] or '').strip()}")
    return "\n".join(lines)


def format_details(rows: list[dict], *, header: str) -> str:
    if not rows:
        return ""
    lines = [header]
    for r in rows:
        day = time.strftime("%m-%d", time.localtime(float(r["last_ts"] or 0)))
        text = str(r["text"] or "").strip()
        # fact 行本身已经带「（来自《标题》）」，别再贴一遍出处
        src = f"《{r['topic']}》" if r.get("topic") and "（来自《" not in text else ""
        lines.append(f"- （{day}）{text}{src}")
    return "\n".join(lines)


_CFG = {
    "enabled": True,
    "interval_sec": 600.0,
    "l0_limit": 2,
    "l0_max_chars": 100,
    "l0_cooldown_sec": 1800.0,
    "l1_limit": 4,
    "sim_threshold": 0.86,
    "max_rows": 800,
}

_L0_HEADER = (
    "【她的见闻·自己刷到的】（不是用户的事实，不要当用户偏好执行；"
    "可自然引用一次，同一话题别反复提）"
)
_L1_HEADER = "【她的经历明细·自己刷到的（不是用户的事实）】"

_MINUTES = 60.0


def configure(cfg: dict | None) -> None:
    """用 config.yaml 的 memory.self_memory 段覆盖默认值（各段自理，不猜别处配置）。"""
    src = cfg or {}
    if "enabled" in src:
        _CFG["enabled"] = bool(src["enabled"])
    interval_min = src.get("interval_min")
    if interval_min is not None:
        _CFG["interval_sec"] = max(60.0, float(interval_min) * _MINUTES)
    l0_cooldown_min = src.get("l0_cooldown_min")
    if l0_cooldown_min is not None:
        _CFG["l0_cooldown_sec"] = max(0.0, float(l0_cooldown_min) * _MINUTES)
    for key, floor in (
        ("l0_limit", 1),
        ("l0_max_chars", 40),
        ("l1_limit", 1),
        ("max_rows", 100),
    ):
        value = src.get(key)
        if value is not None:
            _CFG[key] = max(floor, int(value))
    sim = src.get("sim_threshold")
    if sim is not None:
        _CFG["sim_threshold"] = min(0.99, max(0.5, float(sim)))


def resolved_cfg() -> dict:
    return dict(_CFG)


def build_l0_block(db, user_text: str) -> str:
    """每轮常驻的 L0 段；只做本地读 + 一次注入标记。"""
    if not _CFG["enabled"]:
        return ""
    conn = None
    try:
        conn = db.connect()
        rows = pick_injections(
            conn,
            user_text,
            limit=int(_CFG["l0_limit"]),
            max_chars=int(_CFG["l0_max_chars"]),
            cooldown_sec=float(_CFG["l0_cooldown_sec"]),
        )
    except sqlite3.Error:
        logger.debug("自我记忆 L0 注入失败", exc_info=True)
        return ""
    finally:
        # 每轮都跑，连接必须显式关掉，别攒着等 GC
        if conn is not None:
            conn.close()
    return format_l0(rows, header=_L0_HEADER)


def build_detail_block(db, text: str) -> str:
    """被问/被提及时才带的 L1 明细段。"""
    if not _CFG["enabled"]:
        return ""
    conn = None
    try:
        conn = db.connect()
        rows = search_details(conn, text, limit=int(_CFG["l1_limit"]))
    except sqlite3.Error:
        logger.debug("自我记忆 L1 检索失败", exc_info=True)
        return ""
    finally:
        if conn is not None:
            conn.close()
    return format_details(rows, header=_L1_HEADER)


def make_plugin_loader(base_dir, *, pkg_name: str, sub_path: str, since_days: float = 120.0):
    """把某个插件的经历读取函数包成 L2 loader（沿用插件的合成包名约定）。"""

    def _load(limit: int = _LOAD_LIMIT) -> list[dict]:
        import importlib
        import sys
        import types
        from pathlib import Path

        if f"{pkg_name}.memory" not in sys.modules:
            if pkg_name not in sys.modules:
                pkg = types.ModuleType(pkg_name)
                pkg.__path__ = [str(Path(base_dir) / sub_path)]
                pkg.__package__ = pkg_name
                sys.modules[pkg_name] = pkg
            importlib.import_module(f"{pkg_name}.memory")
        module = sys.modules[f"{pkg_name}.memory"]
        return list(module.recent(limit=limit, since_days=since_days) or [])

    return _load


def start_refresh_thread(
    connect,
    loaders: list[tuple[str, callable]],
    *,
    interval_sec: float = 600.0,
    embedder=None,
    sim_threshold: float = 0.86,
    max_rows: int = 800,
    stop_event: threading.Event | None = None,
    name: str = "self-memory",
) -> threading.Thread:
    """L2 后台线程：每 interval 一轮增量摄取 + 向量化 + 聚类。

    loaders 是 [(插件名, fn(limit) -> 经历 dict 列表)]，由 router 注入——本模块不 import
    插件，也不在这里放每轮路径。
    """

    stop = stop_event or threading.Event()

    def _round(cursor: float) -> float:
        # schema 用独立短连接准备好
        conn = connect()
        try:
            ensure_schema(conn)
            _release(conn)
        finally:
            conn.close()
        for plugin, loader in loaders:
            # 插件 loader 可能读文件/慢 I/O，不占用数据库连接
            items = loader(limit=_LOAD_LIMIT) or []
            fresh = [it for it in items if float(it.get("ts") or 0) > cursor]
            if not fresh:
                continue
            conn = connect()
            try:
                # 内部三段式：读 → 事务外 embedding → 短写事务
                stats = upsert_experiences(
                    conn,
                    fresh,
                    plugin=plugin,
                    embedder=embedder,
                    sim_threshold=sim_threshold,
                )
            finally:
                conn.close()
            # 只有写入成功才推进游标；失败下一轮整批重来（来源键去重保证幂等）
            cursor = max(cursor, max(float(it.get("ts") or 0) for it in fresh))
            if len(items) >= _LOAD_LIMIT:
                # 一次拿满说明插件那边还有更老的没看见，只能等下一轮
                logger.warning(
                    "[SelfMemory] %s 一次返回 %d 条，可能有未摄取的历史",
                    plugin, len(items),
                )
            logger.info("[SelfMemory] %s 摄取 %s", plugin, stats)
        if embedder is not None:
            filled = backfill_embeddings(connect, embedder)
            if filled["pending"]:
                logger.info("[SelfMemory] 向量补齐 %s", filled)
        conn = connect()
        try:
            trim(conn, max_rows=max_rows)
        finally:
            conn.close()
        return cursor

    def _run():
        cursor = 0.0
        while not stop.wait(max(60.0, interval_sec)):
            try:
                cursor = _round(cursor)
            except Exception:
                logger.debug("自我记忆刷新失败（下一轮重试）", exc_info=True)

    thread = threading.Thread(target=_run, name=name, daemon=True)
    thread.start()
    return thread
