"""Core Memory — 第二操作者的常驻「核心工作台画像」（MemGPT 式 Core RAM）。

与按关键词召回的长期记忆不同，Core RAM 每轮无条件注入，体积严格受控：
    - 用户画像（user_profile）里的高价值键值
    - 实体关系三元组（entity_edges）：人脉别名 / 常用路径 / 默认方式 …
      仅注入与当前用户话中出现的实体相关的 1~2 跳关系
    - 历史踩坑教训（error_corrections）：涉及的应用/工具曾失败过时提前警告

纪律：只作参数解析依据（老张是谁、周报在哪），不据此主动执行未请求的动作。
"""

from __future__ import annotations

import logging
import re
import sqlite3

from .db import Database

logger = logging.getLogger(__name__)

_DEFAULTS = {
    "max_chars": 420,
    "profile_max_items": 8,
    "profile_value_max_chars": 60,
    "edge_max_items": 10,
    "lesson_max_items": 2,
    "lesson_min_confidence": 0.5,
}

# user_profile 里属于系统内部状态、不该给模型看的键前缀
_INTERNAL_PROFILE_PREFIXES = ("_", "persona", "theme", "ilink", "wecom", "token", "last_")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS entity_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject TEXT NOT NULL,
    relation TEXT NOT NULL,
    object TEXT NOT NULL,
    confidence REAL DEFAULT 0.8,
    hit_count INTEGER DEFAULT 0,
    source TEXT DEFAULT 'agent',
    status TEXT DEFAULT 'active',
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    UNIQUE(subject, relation)
);
CREATE INDEX IF NOT EXISTS idx_entity_edges_subject ON entity_edges(subject, status);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", str(text or "")).strip()


# ----------------------------------------------------------------------
# 写入
# ----------------------------------------------------------------------


def upsert_edge(
    db: Database,
    subject: str,
    relation: str,
    obj: str,
    *,
    confidence: float = 0.9,
    source: str = "agent",
) -> tuple[bool, str]:
    """写入/覆盖一条实体关系。同 (subject, relation) 视为同一槽位，新值直接取代旧值。"""
    s, r, o = _norm(subject), _norm(relation), str(obj or "").strip()
    if not s or not r or not o:
        return False, "需要提供 subject、relation、object 三项"
    if len(s) > 40 or len(r) > 30 or len(o) > 300:
        return False, "内容过长，请精简后再记"
    with db.connect() as conn:
        ensure_schema(conn)
        row = conn.execute(
            "SELECT object FROM entity_edges WHERE subject = ? AND relation = ?",
            (s, r),
        ).fetchone()
        conn.execute(
            """INSERT INTO entity_edges (subject, relation, object, confidence, source, status)
               VALUES (?, ?, ?, ?, ?, 'active')
               ON CONFLICT(subject, relation) DO UPDATE SET
                   object = excluded.object,
                   confidence = excluded.confidence,
                   source = excluded.source,
                   status = 'active',
                   updated_at = datetime('now')""",
            (s, r, o, float(confidence), source),
        )
        edge_id = int(
            conn.execute(
                "SELECT id FROM entity_edges WHERE subject = ? AND relation = ?", (s, r)
            ).fetchone()["id"]
        )
        try:
            db._fts_insert(conn, f"{s} 的{r}：{o}", "entity_edge", edge_id)
        except Exception:  # noqa: BLE001
            logger.debug("entity_edge FTS 索引失败", exc_info=True)
        conn.commit()
    old = str(row["object"]) if row else ""
    if old and old != o:
        return True, f"已更新：{s} 的{r} 从「{old}」改为「{o}」"
    if old:
        return True, f"{s} 的{r} 仍是「{o}」"
    return True, f"记住了：{s} 的{r} 是「{o}」"


def forget_edge(db: Database, subject: str, relation: str = "") -> int:
    s, r = _norm(subject), _norm(relation)
    if not s:
        return 0
    with db.connect() as conn:
        ensure_schema(conn)
        where = "subject = ? AND status = 'active'" + (" AND relation = ?" if r else "")
        args = (s, r) if r else (s,)
        ids = [int(x["id"]) for x in conn.execute(
            f"SELECT id FROM entity_edges WHERE {where}", args
        ).fetchall()]
        conn.execute(
            f"UPDATE entity_edges SET status = 'deleted', updated_at = datetime('now') "
            f"WHERE {where}",
            args,
        )
        for i in ids:
            try:
                db._fts_delete(conn, "entity_edge", i)
            except Exception:  # noqa: BLE001
                pass
        conn.commit()
    return len(ids)


# ----------------------------------------------------------------------
# 读取
# ----------------------------------------------------------------------


def related_edges(db: Database, text: str, *, limit: int = 10) -> list[dict]:
    """找出用户话中出现的实体，返回其 1 跳关系 + object 再作 subject 的 2 跳关系。"""
    t = _norm(text)
    if not t:
        return []
    try:
        with db.connect() as conn:
            ensure_schema(conn)
            subjects = [
                str(r["subject"])
                for r in conn.execute(
                    "SELECT DISTINCT subject FROM entity_edges WHERE status = 'active'"
                ).fetchall()
            ]
            # 长实体优先，避免「张」抢先匹配「老张」
            hit = sorted({s for s in subjects if s and s in t}, key=len, reverse=True)
            if not hit:
                return []
            out: list[dict] = []
            seen: set[tuple[str, str]] = set()

            def _collect(subj_list: list[str]) -> list[str]:
                nxt: list[str] = []
                if not subj_list:
                    return nxt
                marks = ",".join("?" * len(subj_list))
                rows = conn.execute(
                    f"""SELECT id, subject, relation, object FROM entity_edges
                        WHERE status = 'active' AND subject IN ({marks})
                        ORDER BY confidence DESC, updated_at DESC""",
                    subj_list,
                ).fetchall()
                for r in rows:
                    key = (r["subject"], r["relation"])
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append(dict(r))
                    obj = _norm(r["object"])
                    if obj in subjects and obj not in subj_list:
                        nxt.append(obj)
                return nxt

            second = _collect(hit)
            _collect(second[:4])
            from .tombstones import filter_rows, main_db_path_for_store
            out = filter_rows(out, main_db_path_for_store(db.path), lambda r: " ".join(str(r.get(k) or "") for k in ("subject", "relation", "object")))[:limit]
            if out:
                ids = [int(e["id"]) for e in out]
                conn.execute(
                    f"UPDATE entity_edges SET hit_count = hit_count + 1 "
                    f"WHERE id IN ({','.join('?' * len(ids))})",
                    ids,
                )
                conn.commit()
            return out
    except Exception:  # noqa: BLE001
        logger.debug("related_edges 失败", exc_info=True)
        return []


def _profile_lines(db: Database, max_items: int, max_val: int) -> list[str]:
    try:
        with db.connect() as conn:
            rows = conn.execute(
                "SELECT key, value FROM user_profile ORDER BY updated_at DESC"
            ).fetchall()
    except Exception:  # noqa: BLE001
        return []
    lines: list[str] = []
    for r in rows:
        k = str(r["key"] or "").strip()
        v = str(r["value"] or "").strip()
        if not k or not v or k.lower().startswith(_INTERNAL_PROFILE_PREFIXES):
            continue
        if v.startswith(("{", "[")):  # 结构化内部状态
            continue
        lines.append(f"{k}={v[:max_val]}")
        if len(lines) >= max_items:
            break
    return lines


def _lesson_lines(db: Database, text: str, max_items: int, min_conf: float) -> list[str]:
    """用户话涉及的应用/工具若有历史失败教训，提前给出。"""
    t = (text or "").strip()
    if not t:
        return []
    try:
        with db.connect() as conn:
            aliases = [
                str(r["alias"])
                for r in conn.execute("SELECT alias FROM apps_registry").fetchall()
                if r["alias"]
            ]
            terms = [a for a in aliases if len(a) >= 2 and a in t][:3]
            if not terms:
                return []
            clauses = " OR ".join(["error_pattern LIKE ?"] * len(terms))
            rows = conn.execute(
                f"""SELECT error_pattern, correction_strategy FROM error_corrections
                    WHERE confidence >= ? AND ({clauses})
                    ORDER BY confidence DESC, updated_at DESC LIMIT ?""",
                [min_conf, *[f"%{x}%" for x in terms], max_items],
            ).fetchall()
    except Exception:  # noqa: BLE001
        return []
    return [
        f"{str(r['error_pattern'])[:40]} → {str(r['correction_strategy'])[:60]}"
        for r in rows
    ]


def build_core_ram(db: Database, user_text: str, cfg: dict | None = None) -> str:
    """组装每轮常驻的核心记忆块；无内容时返回空串。

    接入 core_budgets.split_core_ram：将「用户画像 + 相关实体」与「历史踩坑/任务教训」
    按独立预算（250 / 170 字符，整行/整条裁剪）隔离，防止画像过长挤占任务踩坑提示。
    """
    from .core_budgets import split_core_ram

    c = dict(_DEFAULTS)
    c.update({k: v for k, v in (cfg or {}).items() if v is not None})

    profile_lines: list[str] = []
    prof = _profile_lines(db, int(c["profile_max_items"]), int(c["profile_value_max_chars"]))
    if prof:
        profile_lines.append("- 用户画像：" + "；".join(prof))

    edges = related_edges(db, user_text, limit=int(c["edge_max_items"]))
    if edges:
        profile_lines.append(
            "- 相关实体：" + "；".join(
                f"{e['subject']}的{e['relation']}={str(e['object'])[:60]}" for e in edges
            )
        )

    task_lines: list[str] = []
    lessons = _lesson_lines(
        db, user_text, int(c["lesson_max_items"]), float(c["lesson_min_confidence"])
    )
    if lessons:
        task_lines.append("- 历史踩坑（避免重犯）：" + "；".join(lessons))

    if not profile_lines and not task_lines:
        return ""

    prof_budgeted, task_budgeted = split_core_ram(
        "\n".join(profile_lines), "\n".join(task_lines)
    )
    # 若单行本身超过子预算导致被整行裁空，回退为单行截断，保证至少有核心内容输出
    if profile_lines and not prof_budgeted:
        prof_budgeted = "\n".join(profile_lines)[:249] + "…"
    if task_lines and not task_budgeted:
        task_budgeted = "\n".join(task_lines)[:169] + "…"

    sections = [s for s in (prof_budgeted, task_budgeted) if s]
    if not sections:
        return ""
    body = (
        "【核心记忆·常驻】用于解析人名/路径/默认方式等参数，"
        "不要据此执行用户没要求的动作；用户交代新的人物关系、别名、常用路径时，"
        "调用 upsert_core_memory 记下。\n" + "\n".join(sections)
    )
    limit = int(c["max_chars"])
    if len(body) > limit:
        body = body[: limit - 1] + "…"
    return body
