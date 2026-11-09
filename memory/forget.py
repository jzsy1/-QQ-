"""跨库遗忘协调器（Forget Coordinator）。

四个记忆库保持独立（不合并、不 ATTACH）：

- kekesi.db            主库：knowledge_entries / user_profile / auto_facts /
                       consolidated_memory / conversation_digest / memory_chunks /
                       memory_chunk_vec / self_memory / entity_edges / qs_events /
                       inferred_preferences / behavior_patterns / habit_patterns /
                       task_history / memory_audit，以及 memory_fts / qs_events_fts
- cognition.db         world_entities / user_model / self_model / p2_seen_user_prefixes
- perception_events.db perception_events
- episodes_cases.db    staging_cases / cases / episodes(event_ids 关联)

协议（可重入的 saga + 持久化作业日志）：

1. 先在主库 ``memory_forget_jobs`` 落一条作业（崩溃后可恢复）。
2. 逐库执行清除：每个库内部「一个连接 + 一个 BEGIN IMMEDIATE 事务」，
   主表、衍生表、FTS、向量切片在同一事务内原子删除；任一步失败整库回滚。
   遇到 ``database is locked`` 按退避重试。
3. 每个库的清除都是幂等的（按关键词 + 孤儿索引清扫），作业可以整体重跑。
4. 全部清除后用新连接逐库核验：主表无命中、FTS 无命中、无孤儿索引、
   无孤儿向量。只有全部核验通过，作业才标记 verified，才对外报告成功。
5. 未通过的作业保留为 failed / pending_vec，启动时 ``resume_pending`` 自动重试。

被遗忘的关键词同时作为「墓碑」，供后台摄取链路（如 self_memory）过滤，
防止已遗忘的内容被重新写回。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

JOBS_DDL = """
CREATE TABLE IF NOT EXISTS memory_forget_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER DEFAULT 0,
    removed INTEGER DEFAULT 0,
    report TEXT DEFAULT '{}',
    last_error TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_memory_forget_jobs_status
    ON memory_forget_jobs(status);
CREATE TABLE IF NOT EXISTS memory_chunk_vec_gc (
    chunk_id INTEGER PRIMARY KEY,
    created_at TEXT DEFAULT (datetime('now'))
);
"""

# 作业状态
PENDING = "pending"
RUNNING = "running"
VERIFIED = "verified"
FAILED = "failed"
PENDING_VEC = "pending_vec"  # 主体已删除，向量索引因 sqlite-vec 不可用而待清理
NOOP = "noop"  # 四库都没有命中
_RESUMABLE = (PENDING, RUNNING, FAILED, PENDING_VEC)
# 作为墓碑生效的状态（noop 不算：什么都没删就不该拦截以后的写入）
_TOMBSTONE = (PENDING, RUNNING, VERIFIED, FAILED, PENDING_VEC)

_MAX_STEP_RETRIES = 3
_RETRY_BASE_DELAY = 0.5
_MAX_JOB_ATTEMPTS = 6

_RUN_LOCK = threading.Lock()


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(JOBS_DDL)


# ─── 工具函数 ────────────────────────────────────────────────────


def _escape_like(keyword: str) -> str:
    return (
        keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )


def _pattern(keyword: str) -> str:
    return f"%{_escape_like(keyword)}%"


def _match(cols: tuple[str, ...]) -> str:
    """多列 LIKE 条件（大小写不敏感，%/_ 已转义）。"""
    return "(" + " OR ".join(f"COALESCE({c}, '') LIKE ? ESCAPE '\\'" for c in cols) + ")"


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = ? AND type IN ('table', 'view')",
            (name,),
        ).fetchone()
        is not None
    )


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _is_locked(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "locked" in text or "busy" in text


def _connect_plain(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=15000")
    except sqlite3.Error:
        pass
    return conn


def _try_load_vec(conn: sqlite3.Connection) -> bool:
    try:
        import sqlite_vec  # type: ignore

        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        return True
    except Exception:
        return False


def _delete_ids(conn: sqlite3.Connection, table: str, col: str, ids) -> int:
    ids = list(ids)
    if not ids:
        return 0
    conn.executemany(f"DELETE FROM {table} WHERE {col} = ?", [(i,) for i in ids])
    return len(ids)


def load_tombstones(conn: sqlite3.Connection) -> list[str]:
    """已遗忘的关键词（供摄取链路过滤）。表不存在时返回空列表。"""
    try:
        if not _table_exists(conn, "memory_forget_jobs"):
            return []
        marks = ",".join("?" * len(_TOMBSTONE))
        rows = conn.execute(
            f"SELECT DISTINCT keyword FROM memory_forget_jobs WHERE status IN ({marks})",
            _TOMBSTONE,
        ).fetchall()
        return [str(r[0]) for r in rows if str(r[0] or "").strip()]
    except sqlite3.Error:
        return []


def hits_tombstone(text: str, tombstones: list[str]) -> bool:
    low = (text or "").lower()
    return any(t.lower() in low for t in tombstones if t)


# ─── 各库清除 / 核验 ─────────────────────────────────────────────

# 主库内「整行删除」的普通表：(表, 匹配列)
_MAIN_PLAIN_TABLES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("user_profile", ("key", "value")),
    ("inferred_preferences", ("preference_key", "preference_value", "evidence")),
    ("behavior_patterns", ("trigger_condition", "expected_action")),
    ("habit_patterns", ("description", "suggested_action", "trigger_condition")),
    ("task_history", ("action", "result")),
    ("memory_audit", ("query",)),
    ("self_memory", ("text", "topic")),
)

# memory_fts 的 source_type → (主表, 匹配列)
_FTS_SOURCES: dict[str, tuple[str, tuple[str, ...]]] = {
    "knowledge": ("knowledge_entries", ("payload", "trigger")),
    "auto_fact": ("auto_facts", ("fact",)),
    "consolidated": ("consolidated_memory", ("summary",)),
    "entity_edge": ("entity_edges", ("subject", "relation", "object")),
}


class MainStore:
    """kekesi.db：一个连接、一个事务完成主表 + FTS + 切片 + 向量的删除。"""

    name = "kekesi.db"

    def __init__(self, path: Path):
        self.path = Path(path)

    def available(self) -> bool:
        return self.path.exists()

    def purge(self, keyword: str, ctx: dict) -> dict:
        pat = _pattern(keyword)
        counts: dict[str, int] = {}
        conn = _connect_plain(self.path)
        vec_loaded = _try_load_vec(conn)
        try:
            conn.execute("BEGIN IMMEDIATE")
            ensure_schema_inline(conn)

            # 1) 带 FTS 的主表：先取 id
            fts_ids: dict[str, list[int]] = {}
            for stype, (table, cols) in _FTS_SOURCES.items():
                if not _table_exists(conn, table):
                    continue
                cols = tuple(c for c in cols if c in _columns(conn, table))
                if not cols:
                    continue
                rows = conn.execute(
                    f"SELECT id FROM {table} WHERE {_match(cols)}", (pat,) * len(cols)
                ).fetchall()
                fts_ids[stype] = [int(r[0]) for r in rows]

            # 2) 对话原文（向量切片/摘要的源头）
            conv_ids: list[int] = []
            if _table_exists(conn, "conversation_digest"):
                conv_ids = [
                    int(r[0])
                    for r in conn.execute(
                        f"SELECT id FROM conversation_digest "
                        f"WHERE {_match(('user_text', 'reply_text'))}",
                        (pat, pat),
                    ).fetchall()
                ]

            # 3) 向量切片：源记录被删的 + 自身文本命中的
            chunk_ids: list[int] = []
            if _table_exists(conn, "memory_chunks"):
                found: set[int] = set()
                for stype, src_ids in (
                    ("conversation", conv_ids),
                    ("summary", fts_ids.get("consolidated", [])),
                ):
                    for sid in src_ids:
                        for r in conn.execute(
                            "SELECT id FROM memory_chunks WHERE source_type = ? AND source_id = ?",
                            (stype, sid),
                        ).fetchall():
                            found.add(int(r[0]))
                for r in conn.execute(
                    f"SELECT id FROM memory_chunks WHERE {_match(('text',))}", (pat,)
                ).fetchall():
                    found.add(int(r[0]))
                chunk_ids = sorted(found)

            # 4) 向量索引：能加载 sqlite-vec 就在同一事务里删；否则记入 gc 表待清理
            vec_exists = _table_exists(conn, "memory_chunk_vec")
            counts["memory_chunk_vec"] = 0
            if vec_exists and chunk_ids:
                if vec_loaded:
                    counts["memory_chunk_vec"] = _delete_ids(
                        conn, "memory_chunk_vec", "chunk_id", chunk_ids
                    )
                else:
                    conn.executemany(
                        "INSERT OR IGNORE INTO memory_chunk_vec_gc(chunk_id) VALUES (?)",
                        [(i,) for i in chunk_ids],
                    )
            counts["memory_chunks"] = _delete_ids(conn, "memory_chunks", "id", chunk_ids)

            # 5) memory_fts：按源 id 删 + 文本命中兜底
            fts_removed = 0
            if _table_exists(conn, "memory_fts"):
                for stype, ids in fts_ids.items():
                    for i in ids:
                        fts_removed += conn.execute(
                            "DELETE FROM memory_fts WHERE source_type = ? AND source_id = ?",
                            (stype, i),
                        ).rowcount
                fts_removed += conn.execute(
                    f"DELETE FROM memory_fts WHERE {_match(('content',))}", (pat,)
                ).rowcount
            counts["memory_fts"] = max(0, fts_removed)

            # 6) 主表行
            for stype, ids in fts_ids.items():
                table = _FTS_SOURCES[stype][0]
                counts[table] = _delete_ids(conn, table, "id", ids)
            counts["conversation_digest"] = _delete_ids(
                conn, "conversation_digest", "id", conv_ids
            )

            # 7) qs_events + qs_events_fts
            if _table_exists(conn, "qs_events"):
                qs_ids = [
                    int(r[0])
                    for r in conn.execute(
                        f"SELECT id FROM qs_events WHERE {_match(('summary', 'payload'))}",
                        (pat, pat),
                    ).fetchall()
                ]
                if _table_exists(conn, "qs_events_fts"):
                    _delete_ids(conn, "qs_events_fts", "source_id", qs_ids)
                    conn.execute(
                        f"DELETE FROM qs_events_fts WHERE {_match(('summary', 'payload'))}",
                        (pat, pat),
                    )
                counts["qs_events"] = _delete_ids(conn, "qs_events", "id", qs_ids)

            # 8) 其余普通表
            for table, cols in _MAIN_PLAIN_TABLES:
                if not _table_exists(conn, table):
                    continue
                cols = tuple(c for c in cols if c in _columns(conn, table))
                if not cols:
                    continue
                counts[table] = conn.execute(
                    f"DELETE FROM {table} WHERE {_match(cols)}", (pat,) * len(cols)
                ).rowcount

            # 9) 孤儿清扫（幂等：上次中断留下的残余索引在这里收干净）
            self._sweep_orphans(conn, vec_loaded)

            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()
        # removed 只计记忆条目本身；FTS / 切片 / 向量属于衍生索引，单列在 counts 里
        return {
            "counts": counts,
            "removed": sum(
                v
                for k, v in counts.items()
                if k not in ("memory_fts", "memory_chunk_vec", "memory_chunks")
            ),
        }

    @staticmethod
    def _sweep_orphans(conn: sqlite3.Connection, vec_loaded: bool) -> None:
        if _table_exists(conn, "memory_fts"):
            for stype, (table, _cols) in _FTS_SOURCES.items():
                if not _table_exists(conn, table):
                    continue
                conn.execute(
                    f"""DELETE FROM memory_fts WHERE source_type = ?
                        AND CAST(source_id AS INTEGER) NOT IN (SELECT id FROM {table})""",
                    (stype,),
                )
        if _table_exists(conn, "qs_events_fts") and _table_exists(conn, "qs_events"):
            conn.execute(
                """DELETE FROM qs_events_fts
                   WHERE CAST(source_id AS INTEGER) NOT IN (SELECT id FROM qs_events)"""
            )
        if vec_loaded and _table_exists(conn, "memory_chunk_vec"):
            gc_ids = [
                int(r[0])
                for r in conn.execute("SELECT chunk_id FROM memory_chunk_vec_gc").fetchall()
            ]
            _delete_ids(conn, "memory_chunk_vec", "chunk_id", gc_ids)
            conn.execute("DELETE FROM memory_chunk_vec_gc")
            conn.execute(
                """DELETE FROM memory_chunk_vec
                   WHERE chunk_id NOT IN (SELECT id FROM memory_chunks)"""
            )

    def verify(self, keyword: str, ctx: dict) -> tuple[list[str], list[str]]:
        """返回 (残留, 延迟项)。延迟项只出现在向量索引无法加载时。"""
        pat = _pattern(keyword)
        residue: list[str] = []
        deferred: list[str] = []
        conn = _connect_plain(self.path)
        vec_loaded = _try_load_vec(conn)
        try:
            checks: list[tuple[str, tuple[str, ...]]] = [
                (t, c) for t, c in _FTS_SOURCES.values()
            ]
            checks += [
                ("conversation_digest", ("user_text", "reply_text")),
                ("memory_chunks", ("text",)),
                ("qs_events", ("summary", "payload")),
                ("memory_fts", ("content",)),
                ("qs_events_fts", ("summary", "payload")),
            ]
            checks += list(_MAIN_PLAIN_TABLES)
            for table, cols in checks:
                if not _table_exists(conn, table):
                    continue
                cols = tuple(c for c in cols if c in _columns(conn, table))
                if not cols:
                    continue
                n = conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE {_match(cols)}",
                    (pat,) * len(cols),
                ).fetchone()[0]
                if n:
                    residue.append(f"{self.name}:{table}={n}")
            # 孤儿 FTS
            if _table_exists(conn, "memory_fts"):
                for stype, (table, _c) in _FTS_SOURCES.items():
                    if not _table_exists(conn, table):
                        continue
                    n = conn.execute(
                        f"""SELECT COUNT(*) FROM memory_fts WHERE source_type = ?
                            AND CAST(source_id AS INTEGER) NOT IN (SELECT id FROM {table})""",
                        (stype,),
                    ).fetchone()[0]
                    if n:
                        residue.append(f"{self.name}:memory_fts[{stype}] 孤儿={n}")
            # 切片：源记录已不存在却仍留着的
            if _table_exists(conn, "memory_chunks"):
                for stype, table in (
                    ("conversation", "conversation_digest"),
                    ("summary", "consolidated_memory"),
                ):
                    if not _table_exists(conn, table):
                        continue
                    n = conn.execute(
                        f"""SELECT COUNT(*) FROM memory_chunks WHERE source_type = ?
                            AND source_id NOT IN (SELECT id FROM {table})""",
                        (stype,),
                    ).fetchone()[0]
                    if n:
                        residue.append(f"{self.name}:memory_chunks[{stype}] 孤儿={n}")
            # 向量
            if _table_exists(conn, "memory_chunk_vec"):
                if vec_loaded:
                    n = conn.execute(
                        """SELECT COUNT(*) FROM memory_chunk_vec
                           WHERE chunk_id NOT IN (SELECT id FROM memory_chunks)"""
                    ).fetchone()[0]
                    if n:
                        residue.append(f"{self.name}:memory_chunk_vec 孤儿={n}")
                else:
                    pending = 0
                    if _table_exists(conn, "memory_chunk_vec_gc"):
                        pending = conn.execute(
                            "SELECT COUNT(*) FROM memory_chunk_vec_gc"
                        ).fetchone()[0]
                    if pending:
                        deferred.append(
                            f"{self.name}:memory_chunk_vec 待清理={pending}（sqlite-vec 不可用）"
                        )
        finally:
            conn.close()
        return residue, deferred


def ensure_schema_inline(conn: sqlite3.Connection) -> None:
    """事务内建表（不能用 executescript，它会先隐式 COMMIT）。"""
    for stmt in JOBS_DDL.split(";"):
        if stmt.strip():
            conn.execute(stmt)


class CognitionStore:
    name = "cognition.db"
    _TABLES = (
        ("world_entities", ("key", "value")),
        ("user_model", ("key", "value")),
        ("self_model", ("key", "value")),
        ("p2_seen_user_prefixes", ("prefix",)),
    )

    def __init__(self, path: Path):
        self.path = Path(path)

    def available(self) -> bool:
        return self.path.exists()

    def purge(self, keyword: str, ctx: dict) -> dict:
        return _purge_plain(self.path, self._TABLES, keyword)

    def verify(self, keyword: str, ctx: dict) -> tuple[list[str], list[str]]:
        return _verify_plain(self.path, self.name, self._TABLES, keyword), []


class PerceptionDbStore:
    name = "perception_events.db"
    _TABLES = (("perception_events", ("summary", "detail", "source")),)

    def __init__(self, path: Path):
        self.path = Path(path)

    def available(self) -> bool:
        return self.path.exists()

    def purge(self, keyword: str, ctx: dict) -> dict:
        pat = _pattern(keyword)
        conn = _connect_plain(self.path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            ids: list[int] = []
            if _table_exists(conn, "perception_events"):
                cols = tuple(
                    c for c in self._TABLES[0][1] if c in _columns(conn, "perception_events")
                )
                if cols:
                    ids = [
                        int(r[0])
                        for r in conn.execute(
                            f"SELECT id FROM perception_events WHERE {_match(cols)}",
                            (pat,) * len(cols),
                        ).fetchall()
                    ]
                    _delete_ids(conn, "perception_events", "id", ids)
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()
        # 供 episodes 剥离关联 id（跨库无外键，只能由协调器转交）
        known = set(ctx.get("perception_deleted_ids") or [])
        ctx["perception_deleted_ids"] = sorted(known | set(ids))
        return {"counts": {"perception_events": len(ids)}, "removed": len(ids)}

    def verify(self, keyword: str, ctx: dict) -> tuple[list[str], list[str]]:
        return _verify_plain(self.path, self.name, self._TABLES, keyword), []


class EpisodesCasesStore:
    name = "episodes_cases.db"
    _TABLES = (
        ("staging_cases", ("trajectory_summary", "lesson", "steps", "lesson_key")),
        ("cases", ("trajectory_summary", "lesson", "steps", "lesson_key")),
        ("episodes", ("goal", "reflection", "summary")),
    )

    def __init__(self, path: Path):
        self.path = Path(path)

    def available(self) -> bool:
        return self.path.exists()

    def purge(self, keyword: str, ctx: dict) -> dict:
        pat = _pattern(keyword)
        dead = {int(i) for i in (ctx.get("perception_deleted_ids") or [])}
        counts: dict[str, int] = {}
        conn = _connect_plain(self.path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            for table, cols in self._TABLES:
                if not _table_exists(conn, table):
                    continue
                cols = tuple(c for c in cols if c in _columns(conn, table))
                if cols:
                    counts[table] = conn.execute(
                        f"DELETE FROM {table} WHERE {_match(cols)}", (pat,) * len(cols)
                    ).rowcount
            # episodes 只存感知事件 id：剥掉被遗忘事件的 id，空了就整段删掉
            ep_touched = 0
            ep_cols = _columns(conn, "episodes") if _table_exists(conn, "episodes") else set()
            if dead and {"episode_id", "event_ids"}.issubset(ep_cols):
                for row in conn.execute(
                    "SELECT episode_id, event_ids FROM episodes"
                ).fetchall():
                    try:
                        ids = json.loads(row["event_ids"] or "[]")
                    except (TypeError, ValueError):
                        continue
                    kept = [i for i in ids if not (isinstance(i, int) and i in dead)]
                    if len(kept) == len(ids):
                        continue
                    ep_touched += 1
                    if kept:
                        conn.execute(
                            "UPDATE episodes SET event_ids = ?, event_count = ? WHERE episode_id = ?",
                            (json.dumps(kept, ensure_ascii=False), len(kept), row["episode_id"]),
                        )
                    else:
                        conn.execute(
                            "DELETE FROM episodes WHERE episode_id = ?", (row["episode_id"],)
                        )
            counts["episodes(关联)"] = ep_touched
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()
        return {
            "counts": counts,
            "removed": sum(v for k, v in counts.items() if k != "episodes(关联)"),
        }

    def verify(self, keyword: str, ctx: dict) -> tuple[list[str], list[str]]:
        residue = _verify_plain(self.path, self.name, self._TABLES, keyword)
        dead = {int(i) for i in (ctx.get("perception_deleted_ids") or [])}
        if dead and self.path.exists():
            conn = _connect_plain(self.path)
            try:
                if _table_exists(conn, "episodes") and "event_ids" in _columns(conn, "episodes"):
                    left = 0
                    for row in conn.execute("SELECT event_ids FROM episodes").fetchall():
                        try:
                            ids = json.loads(row[0] or "[]")
                        except (TypeError, ValueError):
                            continue
                        if any(isinstance(i, int) and i in dead for i in ids):
                            left += 1
                    if left:
                        residue.append(f"{self.name}:episodes 仍引用已遗忘事件={left}")
            finally:
                conn.close()
        return residue, []


def _purge_plain(path: Path, tables, keyword: str) -> dict:
    pat = _pattern(keyword)
    counts: dict[str, int] = {}
    conn = _connect_plain(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        for table, cols in tables:
            if not _table_exists(conn, table):
                continue
            cols = tuple(c for c in cols if c in _columns(conn, table))
            if cols:
                counts[table] = conn.execute(
                    f"DELETE FROM {table} WHERE {_match(cols)}", (pat,) * len(cols)
                ).rowcount
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()
    return {"counts": counts, "removed": sum(counts.values())}


def _verify_plain(path: Path, name: str, tables, keyword: str) -> list[str]:
    if not path.exists():
        return []
    pat = _pattern(keyword)
    residue: list[str] = []
    conn = _connect_plain(path)
    try:
        for table, cols in tables:
            if not _table_exists(conn, table):
                continue
            cols = tuple(c for c in cols if c in _columns(conn, table))
            if not cols:
                continue
            n = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {_match(cols)}", (pat,) * len(cols)
            ).fetchone()[0]
            if n:
                residue.append(f"{name}:{table}={n}")
    finally:
        conn.close()
    return residue


# ─── 协调器 ──────────────────────────────────────────────────────


class ForgetResult:
    __slots__ = ("ok", "status", "removed", "job_id", "residue", "deferred", "errors")

    def __init__(self, ok, status, removed, job_id, residue, deferred, errors):
        self.ok = ok
        self.status = status
        self.removed = removed
        self.job_id = job_id
        self.residue = residue
        self.deferred = deferred
        self.errors = errors

    def message(self) -> str:
        if self.status == VERIFIED:
            return f"已经忘掉 {self.removed} 条相关记忆"
        if self.status == NOOP:
            return "没找到相关的记忆"
        if self.status == PENDING_VEC:
            return (
                f"已删除 {self.removed} 条相关记忆，但向量索引暂时无法清理"
                "（sqlite-vec 不可用），已记录并会自动重试，暂不算完全遗忘"
            )
        detail = "；".join((self.errors + self.residue)[:3])
        return f"遗忘没有完全完成（{detail}），已记录，稍后会自动重试"


class ForgetCoordinator:
    def __init__(
        self,
        main_db_path: str | Path,
        *,
        cognition_path: str | Path | None = None,
        perception_path: str | Path | None = None,
        episodes_cases_path: str | Path | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        main = Path(main_db_path)
        data_dir = main.parent
        # 四库默认同目录（kekesi/data），测试用临时主库时也只会碰到临时目录的兄弟文件
        self.stores = [
            MainStore(main),
            CognitionStore(Path(cognition_path or data_dir / "cognition.db")),
            PerceptionDbStore(Path(perception_path or data_dir / "perception_events.db")),
            EpisodesCasesStore(Path(episodes_cases_path or data_dir / "episodes_cases.db")),
        ]
        self.main_path = main
        self._sleep = sleep

    # 作业日志 ------------------------------------------------------

    def _jobs_conn(self) -> sqlite3.Connection:
        conn = _connect_plain(self.main_path)
        ensure_schema(conn)
        return conn

    def _create_job(self, keyword: str) -> int:
        conn = self._jobs_conn()
        try:
            cur = conn.execute(
                "INSERT INTO memory_forget_jobs(keyword, status) VALUES (?, ?)",
                (keyword, PENDING),
            )
            conn.commit()
            return int(cur.lastrowid)
        finally:
            conn.close()

    def _update_job(self, job_id: int, **fields) -> None:
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields) + ", updated_at = datetime('now')"
        conn = self._jobs_conn()
        try:
            conn.execute(
                f"UPDATE memory_forget_jobs SET {sets} WHERE id = ?",
                (*fields.values(), job_id),
            )
            conn.commit()
        finally:
            conn.close()

    def _load_job(self, job_id: int) -> sqlite3.Row | None:
        conn = self._jobs_conn()
        try:
            return conn.execute(
                "SELECT * FROM memory_forget_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        finally:
            conn.close()

    # 执行 ---------------------------------------------------------

    def _with_retry(self, fn, *args):
        delay = _RETRY_BASE_DELAY
        for attempt in range(1, _MAX_STEP_RETRIES + 1):
            try:
                return fn(*args)
            except sqlite3.OperationalError as exc:
                if not _is_locked(exc) or attempt == _MAX_STEP_RETRIES:
                    raise
                logger.info("[forget] 数据库忙，%.1fs 后重试（第 %d 次）", delay, attempt)
                self._sleep(delay)
                delay *= 2

    def forget(self, keyword: str) -> ForgetResult:
        keyword = (keyword or "").strip()
        job_id = self._create_job(keyword)
        return self.run_job(job_id)

    def run_job(self, job_id: int) -> ForgetResult:
        with _RUN_LOCK:
            row = self._load_job(job_id)
            if row is None:
                return ForgetResult(False, FAILED, 0, job_id, [], [], ["作业不存在"])
            keyword = str(row["keyword"])
            try:
                report = json.loads(row["report"] or "{}")
            except ValueError:
                report = {}
            ctx = report.setdefault("ctx", {})
            stores_report = report.setdefault("stores", {})
            prev_removed = int(row["removed"] or 0)
            self._update_job(
                job_id, status=RUNNING, attempts=int(row["attempts"] or 0) + 1
            )

            errors: list[str] = []
            removed = 0
            for store in self.stores:
                if not store.available():
                    stores_report[store.name] = {"status": "absent"}
                    continue
                try:
                    out = self._with_retry(store.purge, keyword, ctx)
                    removed += int(out.get("removed") or 0)
                    stores_report[store.name] = {"status": "purged", "counts": out["counts"]}
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[forget] %s 清除失败: %s", store.name, exc)
                    errors.append(f"{store.name}: {exc}")
                    stores_report[store.name] = {"status": "error", "error": str(exc)[:200]}
                # 每库落一次进度（尤其是 perception 删掉的 id，崩溃后 episodes 还要用）
                self._update_job(job_id, report=json.dumps(report, ensure_ascii=False))

            residue: list[str] = []
            deferred: list[str] = []
            for store in self.stores:
                if not store.available():
                    continue
                try:
                    r, d = store.verify(keyword, ctx)
                    residue += r
                    deferred += d
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{store.name} 核验失败: {exc}")

            total = prev_removed + removed
            touched = total + sum(
                int(v or 0)
                for s in stores_report.values()
                for v in (s.get("counts") or {}).values()
            )
            if errors or residue:
                status = FAILED
            elif deferred:
                status = PENDING_VEC
            elif touched == 0:
                status = NOOP
            else:
                status = VERIFIED
            report["verify"] = {"residue": residue, "deferred": deferred}
            self._update_job(
                job_id,
                status=status,
                removed=total,
                report=json.dumps(report, ensure_ascii=False),
                last_error="；".join(errors)[:500],
            )
            if status in (FAILED, PENDING_VEC):
                logger.warning(
                    "[forget] 作业 %s 未通过核验: errors=%s residue=%s deferred=%s",
                    job_id, errors, residue, deferred,
                )
            return ForgetResult(
                status == VERIFIED, status, total, job_id, residue, deferred, errors
            )

    def resume_pending(self, max_attempts: int = _MAX_JOB_ATTEMPTS) -> list[ForgetResult]:
        """重跑未完成的作业（启动时调用）。清除是幂等的，整体重跑安全。"""
        if not self.main_path.exists():
            return []
        conn = self._jobs_conn()
        try:
            marks = ",".join("?" * len(_RESUMABLE))
            ids = [
                int(r[0])
                for r in conn.execute(
                    f"""SELECT id FROM memory_forget_jobs
                        WHERE status IN ({marks}) AND attempts < ? ORDER BY id""",
                    (*_RESUMABLE, int(max_attempts)),
                ).fetchall()
            ]
        finally:
            conn.close()
        results = []
        for job_id in ids:
            try:
                results.append(self.run_job(job_id))
            except Exception:  # noqa: BLE001
                logger.warning("[forget] 恢复作业 %s 失败", job_id, exc_info=True)
        return results
