"""持久化向量切片 — 上下文记忆的语义检索层。

设计：
- 对话与摘要按句切分为固定长度的片段，长期存进 kekesi.db 的 memory_chunks 表
- 配置了 embedding 服务时，后台异步把片段向量写入 sqlite-vec 的 memory_chunk_vec 表
- 未配置或服务不可达时只存文本，检索自动退回 FTS，不影响使用
"""

from __future__ import annotations

import logging
import queue
import re
import sqlite3
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type TEXT NOT NULL,
    source_id INTEGER NOT NULL,
    chunk_index INTEGER NOT NULL,
    session_id TEXT DEFAULT '',
    text TEXT NOT NULL,
    embedded INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(source_type, source_id, chunk_index)
);
CREATE INDEX IF NOT EXISTS idx_memory_chunks_pending
    ON memory_chunks(embedded, id);
"""

_SENTENCE_RE = re.compile(r"(?<=[。！？!?；;\n])")


def split_text(text: str, chunk_chars: int, overlap: int = 0) -> list[str]:
    """按句子边界切分文本；块长与重叠均由 config.yaml memory.vectors 提供。"""
    text = (text or "").strip()
    if not text:
        return []
    chunk_chars = int(chunk_chars)
    if chunk_chars <= 0:
        return []
    overlap = max(0, min(int(overlap or 0), chunk_chars // 2))
    sentences = [s for s in _SENTENCE_RE.split(text) if s.strip()]
    chunks: list[str] = []
    buf = ""
    for sentence in sentences:
        if len(buf) + len(sentence) <= chunk_chars:
            buf += sentence
            continue
        if buf:
            chunks.append(buf.strip())
            tail = buf[-overlap:] if overlap else ""
            buf = tail + sentence
        else:
            buf = sentence
        while len(buf) > chunk_chars:
            chunks.append(buf[:chunk_chars].strip())
            buf = buf[chunk_chars - overlap:]
    if buf.strip():
        chunks.append(buf.strip())
    return [c for c in chunks if c]


class VectorChunkStore:
    """向量切片存储与检索（文本落盘始终可用，向量索引按配置启用）。"""

    def __init__(
        self,
        db_path: str | Path,
        vector_cfg: dict | None = None,
        embedding_cfg: dict | None = None,
    ):
        cfg = vector_cfg or {}
        self.db_path = Path(db_path)
        # 所有参数只认 config.yaml memory.vectors，缺失即不启用（不在代码里编造默认值）
        self.enabled = bool(cfg.get("enabled", False))
        self.chunk_chars = int(cfg.get("chunk_chars") or 0)
        self.overlap = max(0, int(cfg.get("chunk_overlap") or 0))
        self.top_k = int(cfg.get("top_k") or 0)
        self.min_score = float(cfg.get("min_score") or 0.0)
        if self.enabled and (self.chunk_chars <= 0 or self.top_k <= 0):
            logger.warning("[chunks] memory.vectors 配置不完整，切片功能已停用")
            self.enabled = False

        emb = embedding_cfg or {}
        # 地址/模型/密钥/超时全部来自 config.yaml embedding 段，地址按原样使用
        self._emb_enabled = bool(emb.get("enabled", False))
        self._emb_url = str(emb.get("base_url") or "").strip().rstrip("/")
        self._emb_model = str(emb.get("model") or "").strip()
        self._emb_key = str(emb.get("api_key") or "")
        self._emb_timeout = float(emb.get("timeout_sec") or 0)
        self._rerank_enabled = bool(emb.get("rerank_enabled", False))
        self._rerank_model = str(emb.get("rerank_model") or "").strip()
        self._rerank_min_score = float(emb.get("rerank_min_score") or 0.0)
        self._rerank_url = str(
            emb.get("rerank_base_url") or emb.get("base_url") or ""
        ).strip().rstrip("/")

        self._lock = threading.Lock()
        self._queue: queue.Queue = queue.Queue(maxsize=200)
        self._worker: threading.Thread | None = None
        self._embed_failed = False
        self._backfilled = False
        self.active_engine = "sqlite_vec"
        self.degrade_reason = ""
        self._force_numpy = False
        self._pending_numpy_vecs: dict[int, list[float]] = {}
        self._ensure_schema()

    # ─── 对外能力 ─────────────────────────────────────────────

    def engine_status(self) -> dict:
        return {
            "active_engine": self.active_engine,
            "degrade_reason": self.degrade_reason,
            "embed_available": self.embed_available,
        }

    def _mark_degraded(self, reason: str) -> None:
        prev = self.active_engine
        self.active_engine = "numpy"
        self.degrade_reason = str(reason or "sqlite-vec unavailable")
        if prev != "numpy":
            logger.warning(
                "[chunks] sqlite-vec 不可用 (%s)，已自动降级至 NumPy 备用计算引擎",
                self.degrade_reason,
            )

    def _mark_recovered(self) -> None:
        was_degraded = self.active_engine == "numpy" or bool(self.degrade_reason)
        self.active_engine = "sqlite_vec"
        self.degrade_reason = ""
        if was_degraded:
            logger.info("[chunks] sqlite-vec 已恢复正常，重新切回 sqlite-vec 主引擎")

    @property
    def embed_available(self) -> bool:
        if not self.enabled or self._embed_failed:
            return False
        if not (self._emb_enabled and self._emb_url and self._emb_model):
            return False
        try:
            import openai  # noqa: F401
        except ImportError:
            return False
        try:
            import sqlite_vec  # noqa: F401
            return True
        except ImportError:
            try:
                import numpy  # noqa: F401
                return True
            except ImportError:
                return False

    def enqueue_turn(
        self, digest_id: int, session_id: str, user_text: str, reply_text: str
    ) -> None:
        text = f"用户：{user_text}\n助手：{reply_text}"
        self._store_chunks("conversation", int(digest_id), session_id, text)
        self._signal()

    def enqueue_summary(self, summary_id: int, summary: str) -> None:
        self._store_chunks("summary", int(summary_id), "", summary)
        self._signal()

    def _query_vec_primary(
        self, qvec: list[float], candidates: int
    ) -> list[tuple[int, float]] | None:
        """主引擎：sqlite-vec vec0 MATCH 查询；不可用时返回 None 以触发 NumPy 降级。"""
        conn = self._open_vec()
        if conn is None:
            return None
        try:
            if self._pending_numpy_vecs:
                self._sync_pending_numpy_to_vec(conn)
            rows = conn.execute(
                """SELECT chunk_id, distance FROM memory_chunk_vec
                   WHERE embedding MATCH ? ORDER BY distance LIMIT ?""",
                (self._serialize(qvec), candidates),
            ).fetchall()
            return [(int(r[0]), float(r[1])) for r in rows]
        except Exception as e:
            self._mark_degraded(f"vec0 query failed: {e}")
            return None
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _query_vec_numpy(
        self, qvec: list[float], candidates: int
    ) -> list[tuple[int, float]]:
        """备用计算引擎：直接复用现有 memory_chunk_vec 底层表与内存缓冲用 NumPy 计算 L2 距离。"""
        try:
            import numpy as np

            q_arr = np.asarray(qvec, dtype=np.float32)
            with self._connect() as conn:
                has_meta = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='memory_chunks_meta'"
                ).fetchone()
                dim = len(qvec)
                if has_meta:
                    drow = conn.execute(
                        "SELECT value FROM memory_chunks_meta WHERE key='dim'"
                    ).fetchone()
                    if drow and int(drow[0]) > 0:
                        dim = int(drow[0])
                if dim != len(qvec):
                    return []

                gc_ids: set[int] = set()
                if conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='memory_chunk_vec_gc'"
                ).fetchone():
                    gc_ids = {
                        int(r[0])
                        for r in conn.execute("SELECT chunk_id FROM memory_chunk_vec_gc")
                    }

                alive_ids = {
                    int(r[0])
                    for r in conn.execute("SELECT id FROM memory_chunks")
                } - gc_ids

                vec_map: dict[int, "np.ndarray"] = {}
                has_shadow = (
                    conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE name='memory_chunk_vec_rowids'"
                    ).fetchone()
                    and conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE name='memory_chunk_vec_vector_chunks00'"
                    ).fetchone()
                )
                if has_shadow:
                    blocks = dict(
                        conn.execute(
                            "SELECT rowid, vectors FROM memory_chunk_vec_vector_chunks00"
                        ).fetchall()
                    )
                    rrows = conn.execute(
                        "SELECT rowid, id, chunk_id, chunk_offset FROM memory_chunk_vec_rowids"
                    ).fetchall()
                    for rowid, text_id, blk_id, offset in rrows:
                        cid = int(text_id if text_id is not None else rowid)
                        if cid not in alive_ids:
                            continue
                        blob = blocks.get(blk_id)
                        if blob is None:
                            continue
                        byte_off = int(offset) * dim * 4
                        if byte_off + dim * 4 > len(blob):
                            continue
                        vec_map[cid] = np.frombuffer(
                            blob, dtype="<f4", count=dim, offset=byte_off
                        )

                for cid, pvec in list(self._pending_numpy_vecs.items()):
                    if cid in alive_ids and len(pvec) == dim:
                        vec_map[cid] = np.asarray(pvec, dtype=np.float32)

                if not vec_map:
                    return []
                cids = list(vec_map.keys())
                mat = np.stack([vec_map[cid] for cid in cids]).astype(np.float32, copy=False)
                dists = np.linalg.norm(mat - q_arr, axis=1)
                order = np.argsort(dists)[:candidates]
                return [(int(cids[idx]), float(dists[idx])) for idx in order]
        except Exception as e:
            logger.warning("[chunks] NumPy 备用引擎查询异常: %s", e)
            return []

    def search(
        self,
        query: str,
        limit: int | None = None,
        min_score: float | None = None,
        *,
        include_summarized: bool = False,
    ) -> list[dict]:
        """语义召回（sqlite-vec 主引擎 + NumPy 自动降级兜底）。

        include_summarized=False 时，已压缩进摘要的对话原文切片不参与默认召回，
        避免摘要和原文重复注入；显式追问细节时传 True。
        """
        query = (query or "").strip()
        if not query or not self.embed_available:
            return []
        vec = self._embed([query])
        if not vec:
            return []
        if limit is None:
            n = self.top_k
            if n <= 0:
                return []
        else:
            n = max(1, int(limit))
        candidates = max(n * 4, 10)
        rows = self._query_vec_primary(vec[0], candidates)
        if rows is None:
            rows = self._query_vec_numpy(vec[0], candidates)
        if not rows:
            return []
        ids = [int(r[0]) for r in rows]

        texts = self._load_chunk_texts(ids, include_summarized=include_summarized)
        scored: list[dict] = []
        for chunk_id, distance in rows:
            # 向量已归一化：L2 距离 d 与余弦相似度满足 sim = 1 - d^2 / 2
            score = 1.0 - (float(distance) ** 2) / 2.0
            item = texts.get(int(chunk_id))
            if item:
                scored.append({**item, "score": round(score, 3)})

        reranked = self._rerank(query, scored)
        from .tombstones import filter_rows, main_db_path_for_store
        if reranked is not None:
            return filter_rows(reranked[:n], main_db_path_for_store(self.db_path), lambda r: r.get("text", ""))

        threshold = self.min_score if min_score is None else float(min_score)
        return filter_rows([h for h in scored if h["score"] >= threshold][:n], main_db_path_for_store(self.db_path), lambda r: r.get("text", ""))

    def _rerank(self, query: str, items: list[dict]) -> list[dict] | None:
        """调用 bge-reranker 重排候选；未启用或调用失败时返回 None（回退向量分数）。"""
        if not self._rerank_enabled or not self._rerank_model or len(items) < 2:
            return None
        url = self._rerank_url
        if not url:
            return None
        try:
            import requests

            resp = requests.post(
                url + "/rerank",
                headers={"Authorization": f"Bearer {self._emb_key or 'local-no-key'}"},
                json={
                    "model": self._rerank_model,
                    "query": query,
                    "documents": [i.get("text", "") for i in items],
                    "top_n": len(items),
                },
                timeout=self._emb_timeout or None,
            )
            if not resp.ok:
                logger.debug("重排失败 HTTP %s: %s", resp.status_code, resp.text[:150])
                return None
            results = resp.json().get("results") or []
            out = []
            for r in results:
                idx = int(r.get("index", -1))
                if not (0 <= idx < len(items)):
                    continue
                score = round(float(r.get("relevance_score", 0)), 3)
                if score < self._rerank_min_score:
                    continue
                out.append({**items[idx], "score": score})
            return out
        except Exception as e:
            logger.debug("重排调用失败: %s", e)
            return None

    def start_background(self) -> None:
        """启动后台工作线程，并把历史里未向量化的切片补上。"""
        if not self.enabled:
            return
        if self._worker and self._worker.is_alive():
            return
        self._worker = threading.Thread(
            target=self._worker_loop, name="memory-chunks", daemon=True
        )
        self._worker.start()
        threading.Thread(
            target=self._backfill_once, name="memory-chunks-backfill", daemon=True
        ).start()

    def _backfill_once(self, max_rows: int = 500) -> int:
        """把已有对话与摘要补成切片（文本始终落库，向量交给工作线程），并清理历史孤立切片。"""
        if self._backfilled:
            return 0
        self._backfilled = True
        self.cleanup_orphans()
        try:
            with self._connect() as conn:
                digests = conn.execute(
                    """SELECT id, session_id, user_text, reply_text
                       FROM conversation_digest ORDER BY id DESC LIMIT ?""",
                    (max_rows,),
                ).fetchall()
                summaries = conn.execute(
                    """SELECT id, summary FROM consolidated_memory
                       ORDER BY id DESC LIMIT ?""",
                    (max_rows,),
                ).fetchall()
        except Exception:
            return 0
        for row in digests:
            self._store_chunks(
                "conversation",
                int(row["id"]),
                str(row["session_id"] or ""),
                f"用户：{row['user_text']}\n助手：{row['reply_text']}",
            )
        for row in summaries:
            self._store_chunks("summary", int(row["id"]), "", str(row["summary"] or ""))
        if self.embed_available:
            self._signal()
        return len(digests) + len(summaries)

    # ─── 存储 ─────────────────────────────────────────────────

    def _ensure_schema(self) -> None:
        try:
            with self._connect() as conn:
                conn.executescript(_SCHEMA)
                conn.commit()
        except Exception as e:
            logger.warning("memory_chunks 初始化失败: %s", e)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=15000")
        return conn

    def _open_vec(self) -> sqlite3.Connection | None:
        if self._force_numpy:
            self._mark_degraded("forced numpy fallback")
            return None
        try:
            import sqlite_vec  # type: ignore

            conn = sqlite3.connect(str(self.db_path), timeout=15)
            conn.execute("PRAGMA busy_timeout=15000")
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            self._mark_recovered()
            return conn
        except Exception as e:
            self._mark_degraded(f"{type(e).__name__}: {e}")
            return None

    def _sync_pending_numpy_to_vec(self, vec_conn: sqlite3.Connection) -> None:
        """当 sqlite-vec 主引擎恢复时，将降级期间暂存的向量刷回 memory_chunk_vec。"""
        if not self._pending_numpy_vecs:
            return
        try:
            items = list(self._pending_numpy_vecs.items())
            if not items:
                return
            self._ensure_vec_table(vec_conn, len(items[0][1]))
            if vec_conn.in_transaction:
                vec_conn.commit()
            vec_conn.execute("BEGIN IMMEDIATE")
            self._apply_vec_gc(vec_conn)
            ids = [cid for cid, _ in items]
            marks = ",".join("?" * len(ids))
            alive = {
                int(r[0])
                for r in vec_conn.execute(
                    f"SELECT id FROM memory_chunks WHERE id IN ({marks})", ids
                ).fetchall()
            }
            for chunk_id, vec in items:
                if chunk_id in alive:
                    vec_conn.execute(
                        "INSERT OR REPLACE INTO memory_chunk_vec(chunk_id, embedding) VALUES (?, ?)",
                        (chunk_id, self._serialize(vec)),
                    )
            vec_conn.executemany(
                "UPDATE memory_chunks SET embedded = 1 WHERE id = ?",
                [(i,) for i in sorted(alive)],
            )
            vec_conn.commit()
            self._pending_numpy_vecs.clear()
        except Exception as e:
            try:
                vec_conn.rollback()
            except sqlite3.Error:
                pass
            logger.debug("同步暂存向量至 sqlite-vec 失败: %s", e)

    def _store_chunks(
        self, source_type: str, source_id: int, session_id: str, text: str
    ) -> None:
        if not self.enabled:
            return
        pieces = split_text(text, self.chunk_chars, self.overlap)
        if not pieces:
            return
        try:
            with self._connect() as conn:
                conn.executemany(
                    """INSERT OR IGNORE INTO memory_chunks
                       (source_type, source_id, chunk_index, session_id, text)
                       VALUES (?, ?, ?, ?, ?)""",
                    [
                        (source_type, source_id, i, session_id or "", piece)
                        for i, piece in enumerate(pieces)
                    ],
                )
                conn.commit()
        except Exception as e:
            logger.debug("切片写入失败: %s", e)

    def _load_chunk_texts(
        self, ids: list[int], *, include_summarized: bool = False
    ) -> dict[int, dict]:
        """读取切片文本并二次校验源记录仍然有效。

        修复点：
        - 之前 LEFT JOIN conversation_digest 在源记录已删除时 d.id / d.summarized
          均为 NULL，``COALESCE(d.summarized, 0) != 1`` 会把 NULL 变成 0，使幽灵切片
          绕过过滤继续召回；
        - 现在显式校验：
          * ``c.source_type = 'conversation'`` 必须 ``d.id IS NOT NULL``，且满足
            ``include_summarized`` 或 ``COALESCE(d.summarized, 0) != 1`` 或 ``COALESCE(d.important, 0) = 1``；
          * ``c.source_type = 'summary'`` 必须 ``m.id IS NOT NULL``（consolidated_memory 仍存在）；
          * 未知 source_type 一律不召回；
        - 若在候选集中发现源记录已消失的孤立切片，顺带异步/轻量清理。
        """
        if not ids:
            return {}
        marks = ",".join("?" for _ in ids)
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    f"""SELECT c.id, c.text, c.source_type, c.source_id, c.created_at
                        FROM memory_chunks c
                        LEFT JOIN conversation_digest d
                          ON c.source_type = 'conversation' AND d.id = c.source_id
                        LEFT JOIN consolidated_memory m
                          ON c.source_type = 'summary' AND m.id = c.source_id
                        WHERE c.id IN ({marks})
                          AND (
                              (c.source_type = 'conversation'
                               AND d.id IS NOT NULL
                               AND (? OR COALESCE(d.summarized, 0) != 1
                                    OR COALESCE(d.important, 0) = 1))
                              OR (c.source_type = 'summary' AND m.id IS NOT NULL)
                          )""",
                    [*ids, 1 if include_summarized else 0],
                ).fetchall()
                alive_ids = {int(r["id"]) for r in rows}
                # 检查未命中里是否有「源记录已删除」的孤立切片并顺手清掉
                missing = [i for i in ids if int(i) not in alive_ids]
                if missing:
                    m_marks = ",".join("?" for _ in missing)
                    orphans = [
                        int(r["id"])
                        for r in conn.execute(
                            f"""SELECT c.id FROM memory_chunks c
                                LEFT JOIN conversation_digest d
                                  ON c.source_type = 'conversation' AND d.id = c.source_id
                                LEFT JOIN consolidated_memory m
                                  ON c.source_type = 'summary' AND m.id = c.source_id
                                WHERE c.id IN ({m_marks})
                                  AND (
                                      (c.source_type = 'conversation' AND d.id IS NULL)
                                      OR (c.source_type = 'summary' AND m.id IS NULL)
                                      OR c.source_type NOT IN ('conversation', 'summary')
                                  )""",
                            missing,
                        ).fetchall()
                    ]
                    if orphans:
                        self._delete_chunk_ids_in_conn(conn, orphans)
                        conn.commit()
            return {int(r["id"]): dict(r) for r in rows}
        except Exception:
            return {}

    @staticmethod
    def _delete_chunk_ids_in_conn(
        conn: sqlite3.Connection, chunk_ids: list[int]
    ) -> int:
        """在调用方给定连接/事务内删除指定 chunk_ids 及其向量（或登记 GC）。"""
        ids = [int(i) for i in chunk_ids if i is not None]
        if not ids:
            return 0
        marks = ",".join("?" for _ in ids)
        vec_exists = (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'memory_chunk_vec'"
            ).fetchone()
            is not None
        )
        if vec_exists:
            vec_deleted = False
            try:
                conn.executemany(
                    "DELETE FROM memory_chunk_vec WHERE chunk_id = ?",
                    [(i,) for i in ids],
                )
                vec_deleted = True
            except sqlite3.Error:
                try:
                    import sqlite_vec  # type: ignore

                    conn.enable_load_extension(True)
                    sqlite_vec.load(conn)
                    conn.enable_load_extension(False)
                    conn.executemany(
                        "DELETE FROM memory_chunk_vec WHERE chunk_id = ?",
                        [(i,) for i in ids],
                    )
                    vec_deleted = True
                except Exception:
                    vec_deleted = False
            if not vec_deleted:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS memory_chunk_vec_gc("
                    "chunk_id INTEGER PRIMARY KEY, created_at TEXT DEFAULT (datetime('now')))"
                )
                conn.executemany(
                    "INSERT OR IGNORE INTO memory_chunk_vec_gc(chunk_id) VALUES (?)",
                    [(i,) for i in ids],
                )
        cur = conn.execute(
            f"DELETE FROM memory_chunks WHERE id IN ({marks})", ids
        )
        return max(0, cur.rowcount)

    @classmethod
    def purge_for_sources(
        cls,
        conn: sqlite3.Connection,
        source_type: str,
        source_ids: list[int],
    ) -> int:
        """在调用方事务内同步清理指定源记录关联的 memory_chunks 与向量。"""
        sids = [int(i) for i in source_ids if i is not None]
        if not sids:
            return 0
        has_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'memory_chunks'"
        ).fetchone()
        if not has_table:
            return 0
        marks = ",".join("?" for _ in sids)
        cids = [
            int(r[0])
            for r in conn.execute(
                f"SELECT id FROM memory_chunks WHERE source_type = ? AND source_id IN ({marks})",
                [source_type, *sids],
            ).fetchall()
        ]
        return cls._delete_chunk_ids_in_conn(conn, cids)

    @classmethod
    def sweep_orphan_chunks_in_conn(cls, conn: sqlite3.Connection) -> dict[str, int]:
        """在给定连接/事务内识别并清理历史孤立切片与孤立向量，保证主记录与切片一致。"""
        report = {"orphan_chunks": 0, "orphan_vectors": 0}
        has_chunks = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'memory_chunks'"
        ).fetchone()
        if not has_chunks:
            return report
        orphans: set[int] = set()
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'conversation_digest'"
        ).fetchone():
            for r in conn.execute(
                """SELECT c.id FROM memory_chunks c
                   LEFT JOIN conversation_digest d ON d.id = c.source_id
                   WHERE c.source_type = 'conversation' AND d.id IS NULL"""
            ).fetchall():
                orphans.add(int(r[0]))
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'consolidated_memory'"
        ).fetchone():
            for r in conn.execute(
                """SELECT c.id FROM memory_chunks c
                   LEFT JOIN consolidated_memory m ON m.id = c.source_id
                   WHERE c.source_type = 'summary' AND m.id IS NULL"""
            ).fetchall():
                orphans.add(int(r[0]))
        for r in conn.execute(
            "SELECT id FROM memory_chunks WHERE source_type NOT IN ('conversation', 'summary')"
        ).fetchall():
            orphans.add(int(r[0]))
        if orphans:
            report["orphan_chunks"] = cls._delete_chunk_ids_in_conn(
                conn, sorted(orphans)
            )
        # 向量表若有残留（chunk_id 已不在 memory_chunks 中），尝试直接清理
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'memory_chunk_vec'"
        ).fetchone():
            try:
                cls._apply_vec_gc(conn)
                cur = conn.execute(
                    """DELETE FROM memory_chunk_vec
                       WHERE chunk_id NOT IN (SELECT id FROM memory_chunks)"""
                )
                report["orphan_vectors"] = max(0, cur.rowcount)
            except Exception:
                try:
                    import sqlite_vec  # type: ignore

                    conn.enable_load_extension(True)
                    sqlite_vec.load(conn)
                    conn.enable_load_extension(False)
                    cls._apply_vec_gc(conn)
                    cur = conn.execute(
                        """DELETE FROM memory_chunk_vec
                           WHERE chunk_id NOT IN (SELECT id FROM memory_chunks)"""
                    )
                    report["orphan_vectors"] = max(0, cur.rowcount)
                except Exception:
                    pass
        return report

    def cleanup_orphans(self) -> dict[str, int]:
        """对外一致性检查与孤立切片清理入口。"""
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                rep = self.sweep_orphan_chunks_in_conn(conn)
                conn.commit()
                return rep
        except Exception as e:
            logger.debug("清理孤立切片失败: %s", e)
            return {"orphan_chunks": 0, "orphan_vectors": 0}

    def check_consistency(self) -> dict[str, int]:
        """检查主记录（conversation_digest / consolidated_memory）与向量切片的一致性。"""
        result = {
            "orphan_conversation_chunks": 0,
            "orphan_summary_chunks": 0,
            "orphan_unknown_chunks": 0,
            "orphan_vectors": 0,
        }
        try:
            with self._connect() as conn:
                if not conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE name = 'memory_chunks'"
                ).fetchone():
                    return result
                result["orphan_conversation_chunks"] = int(
                    conn.execute(
                        """SELECT COUNT(*) FROM memory_chunks c
                           LEFT JOIN conversation_digest d ON d.id = c.source_id
                           WHERE c.source_type = 'conversation' AND d.id IS NULL"""
                    ).fetchone()[0]
                )
                result["orphan_summary_chunks"] = int(
                    conn.execute(
                        """SELECT COUNT(*) FROM memory_chunks c
                           LEFT JOIN consolidated_memory m ON m.id = c.source_id
                           WHERE c.source_type = 'summary' AND m.id IS NULL"""
                    ).fetchone()[0]
                )
                result["orphan_unknown_chunks"] = int(
                    conn.execute(
                        """SELECT COUNT(*) FROM memory_chunks
                           WHERE source_type NOT IN ('conversation', 'summary')"""
                    ).fetchone()[0]
                )
                vec_conn = self._open_vec()
                if vec_conn is not None:
                    try:
                        if vec_conn.execute(
                            "SELECT 1 FROM sqlite_master WHERE name = 'memory_chunk_vec'"
                        ).fetchone():
                            result["orphan_vectors"] = int(
                                vec_conn.execute(
                                    """SELECT COUNT(*) FROM memory_chunk_vec
                                       WHERE chunk_id NOT IN (SELECT id FROM memory_chunks)"""
                                ).fetchone()[0]
                            )
                    finally:
                        vec_conn.close()
        except Exception as e:
            logger.debug("切片一致性检查失败: %s", e)
        return result

    # ─── 向量化 ───────────────────────────────────────────────

    def _signal(self) -> None:
        if not self.enabled:
            return
        try:
            self._queue.put_nowait(True)
        except queue.Full:
            pass
        self.start_background()

    def _worker_loop(self) -> None:
        while True:
            try:
                self._queue.get(timeout=30)
            except queue.Empty:
                pass
            if not self.embed_available:
                continue
            # 一次信号处理完所有待向量化切片；空闲时每 30 秒兜底补漏
            while self.embed_available:
                if self._flush_pending() <= 0:
                    break

    def _flush_pending(self, batch: int = 32) -> int:
        if not self.embed_available:
            return 0
        with self._lock:
            try:
                with self._connect() as conn:
                    rows = conn.execute(
                        """SELECT id, text FROM memory_chunks
                           WHERE embedded = 0 ORDER BY id ASC LIMIT ?""",
                        (batch,),
                    ).fetchall()
            except Exception:
                return 0
            if not rows:
                return 0
            ids = [int(r["id"]) for r in rows]
            texts = [str(r["text"]) for r in rows]
            # 远程 embedding 在任何事务之外完成
            vectors = self._embed(texts)
            if not vectors or len(vectors) != len(ids):
                return 0
            vec_conn = self._open_vec()
            if vec_conn is None:
                for chunk_id, vec in zip(ids, vectors):
                    self._pending_numpy_vecs[int(chunk_id)] = list(vec)
                return 0
            try:
                self._ensure_vec_table(vec_conn, len(vectors[0]))
                if vec_conn.in_transaction:
                    vec_conn.commit()
                # 短事务写回：向量与 embedded 标记同库同事务；
                # 只给仍存在的切片写向量——embedding 期间被遗忘/清理的切片不留幽灵向量
                vec_conn.execute("BEGIN IMMEDIATE")
                self._apply_vec_gc(vec_conn)
                marks = ",".join("?" * len(ids))
                alive = {
                    int(r[0])
                    for r in vec_conn.execute(
                        f"SELECT id FROM memory_chunks WHERE id IN ({marks})", ids
                    ).fetchall()
                }
                for chunk_id, vec in zip(ids, vectors):
                    if chunk_id not in alive:
                        continue
                    vec_conn.execute(
                        "INSERT OR REPLACE INTO memory_chunk_vec(chunk_id, embedding) VALUES (?, ?)",
                        (chunk_id, self._serialize(vec)),
                    )
                vec_conn.executemany(
                    "UPDATE memory_chunks SET embedded = 1 WHERE id = ?",
                    [(i,) for i in sorted(alive)],
                )
                vec_conn.commit()
            except Exception as e:
                try:
                    vec_conn.rollback()
                except sqlite3.Error:
                    pass
                logger.debug("向量写入失败: %s", e)
            finally:
                vec_conn.close()
            return len(ids)

    @staticmethod
    def _apply_vec_gc(conn: sqlite3.Connection) -> int:
        """清理遗忘时因 sqlite-vec 不可用而延迟的向量（memory_chunk_vec_gc）。"""
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'memory_chunk_vec_gc'"
        ).fetchone()
        if not exists:
            return 0
        ids = [int(r[0]) for r in conn.execute("SELECT chunk_id FROM memory_chunk_vec_gc")]
        if not ids:
            return 0
        conn.executemany(
            "DELETE FROM memory_chunk_vec WHERE chunk_id = ?", [(i,) for i in ids]
        )
        conn.execute("DELETE FROM memory_chunk_vec_gc")
        return len(ids)

    def _ensure_vec_table(self, conn: sqlite3.Connection, dim: int) -> None:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS memory_chunks_meta("
            "key TEXT PRIMARY KEY, value TEXT)"
        )
        row = conn.execute(
            "SELECT value FROM memory_chunks_meta WHERE key='dim'"
        ).fetchone()
        if row and int(row[0]) != int(dim):
            # 嵌入模型维度变化 → 重建索引并重新向量化
            conn.execute("DROP TABLE IF EXISTS memory_chunk_vec")
            conn.execute("UPDATE memory_chunks SET embedded = 0")
            conn.execute("DELETE FROM memory_chunks_meta")
        if not row or int(row[0]) != int(dim):
            conn.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS memory_chunk_vec USING vec0("
                f"chunk_id INTEGER PRIMARY KEY, embedding FLOAT[{int(dim)}])"
            )
            conn.execute(
                "INSERT OR REPLACE INTO memory_chunks_meta(key, value) VALUES('dim', ?)",
                (str(int(dim)),),
            )
            conn.commit()

    def embed_texts(self, texts: list[str]) -> list[list[float]] | None:
        """供后台清洗复用的公开向量化入口（远程调用，禁止进每轮关键路径）。"""
        return self._embed(list(texts or []))

    def _embed(self, texts: list[str]) -> list[list[float]] | None:
        if not texts:
            return []
        if not self.embed_available:
            return None
        try:
            from openai import OpenAI

            client = OpenAI(
                api_key=self._emb_key or "local-no-key",
                base_url=self._emb_url,
                timeout=self._emb_timeout or None,
            )
            resp = client.embeddings.create(model=self._emb_model, input=texts)
            data = [list(map(float, d.embedding)) for d in (resp.data or [])]
            if len(data) != len(texts) or not data:
                return None
            return [self._normalize(v) for v in data]
        except Exception as e:
            text = str(e)
            permanent = bool(
                re.search(r"(?:error code|status(?:_code)?)[:=\s]*(400|401|403)", text, re.I)
            ) or "invalid api key" in text.lower() or "model not found" in text.lower()
            if permanent:
                self._embed_failed = True
                logger.warning("[chunks] embedding 配置错误，暂停向量化: %s", text[:200])
            else:
                logger.warning("[chunks] 嵌入调用失败，稍后重试: %s", text[:200])
            return None

    @staticmethod
    def _normalize(vec: list[float]) -> list[float]:
        norm = sum(v * v for v in vec) ** 0.5
        return [v / norm for v in vec] if norm > 0 else list(vec)

    @staticmethod
    def _serialize(vec: list[float]) -> bytes:
        try:
            import sqlite_vec  # type: ignore

            fn = getattr(sqlite_vec, "serialize_float32", None) or getattr(
                sqlite_vec, "serialize_float", None
            )
            if fn is not None:
                return fn(list(map(float, vec)))
        except Exception:
            pass
        import struct

        return struct.pack(f"<{len(vec)}f", *vec)
