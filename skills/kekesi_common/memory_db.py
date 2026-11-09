# -*- coding: utf-8 -*-
"""通用陪伴记忆数据库 (SQLite)。

每个命名空间只在自己的 memory/ 目录内创建和运行，绝对不读写、不打开
主系统 SQLite，不触碰四库边界。仅使用 Python 标准库 sqlite3。
"""

from __future__ import annotations

import json
import hashlib
import logging
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["LiveMemoryDB"]


def _tokenize_for_fts(text: str) -> str:
    """提取中英文特征词用于 FTS 全文索引（英文单词 + 中文 2 字分词）。"""
    if not text:
        return ""
    words = re.findall(r"[A-Za-z0-9_]+", text.lower())
    cn = re.sub(r"[^\u4e00-\u9fff]", "", text)
    grams = [cn[i:i + 2] for i in range(len(cn) - 1)]
    return " ".join(words + grams)


class LiveMemoryDB:
    """命名空间独立记忆数据库（仅在各自 memory/ 目录下创建与运行）。"""

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    def _connect(self) -> sqlite3.Connection:
        """获取带 WAL 模式与外键约束的独立数据库连接。"""
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    def init_db(self) -> None:
        """初始化命名空间专属表结构与全文索引。"""
        conn = self._connect()
        try:
            # 1. 观众画像总表 (借鉴 ForgetMeNot viewers + Lumi_Nox viewer_profiles)
            conn.execute("""
            CREATE TABLE IF NOT EXISTS viewers (
                user_id TEXT PRIMARY KEY,
                user_name TEXT NOT NULL,
                first_seen_at REAL NOT NULL,
                last_seen_at REAL NOT NULL,
                interaction_count INTEGER DEFAULT 0,
                is_vip INTEGER DEFAULT 0,
                meta_json TEXT
            );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_viewers_last_seen ON viewers(last_seen_at DESC);")

            # 2. 结构化持久事实表 (借鉴 Lumi_Nox viewer_facts + ForgetMeNot semantic_notes)
            conn.execute("""
            CREATE TABLE IF NOT EXISTS facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                category TEXT NOT NULL,
                fact TEXT NOT NULL,
                confidence REAL DEFAULT 0.8,
                source_kind TEXT DEFAULT 'self_claim',
                stability_reason TEXT,
                is_pinned INTEGER DEFAULT 0,
                created_at REAL NOT NULL,
                last_confirmed_at REAL NOT NULL,
                status TEXT DEFAULT 'active',
                superseded_by INTEGER,
                FOREIGN KEY (user_id) REFERENCES viewers(user_id)
            );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_facts_user_status ON facts(user_id, status);")

            # 3. 实时事件流水表 (借鉴 ForgetMeNot events)
            conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                record_id TEXT UNIQUE,
                session_id TEXT,
                user_id TEXT,
                user_name TEXT,
                kind TEXT NOT NULL,
                content TEXT,
                gift_name TEXT,
                gift_count INTEGER,
                ts REAL NOT NULL
            );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_events_user_ts ON events(user_id, ts DESC);")

            # 4. 鱼鱼最近回复表 (借鉴 ForgetMeNot recentBotReplies)
            conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_replies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT,
                user_name TEXT,
                reply_text TEXT NOT NULL,
                reply_to_ts REAL,
                ts REAL NOT NULL
            );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_bot_replies_ts ON bot_replies(ts DESC);")

            # 5. FTS5 全文索引虚拟表 (用于弹幕关键词高速匹配，免向量模型)
            try:
                conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS fts_events USING fts5(
                    record_id UNINDEXED,
                    user_id UNINDEXED,
                    user_name UNINDEXED,
                    content,
                    tokens
                );
                """)
            except Exception:
                logger.debug("当前环境 SQLite 未开启 FTS5，降级为普通查询")

            conn.commit()
        finally:
            conn.close()

    # -- 观众操作 ------------------------------------------------------
    def upsert_viewer(
        self,
        user_id: str,
        user_name: str,
        ts: float | None = None,
        is_vip: bool | None = None,
    ) -> None:
        """更新或插入观众基本信息。"""
        user_id = str(user_id or "").strip()
        if not user_id:
            return
        now_ts = time.time() if ts is None else float(ts)
        user_name = str(user_name or "")
        conn = self._connect()
        try:
            row = conn.execute("SELECT first_seen_at, interaction_count, is_vip FROM viewers WHERE user_id = ?", (user_id,)).fetchone()
            if row is None:
                vip_val = 1 if is_vip else 0
                conn.execute("""
                INSERT INTO viewers (user_id, user_name, first_seen_at, last_seen_at, interaction_count, is_vip)
                VALUES (?, ?, ?, ?, 1, ?)
                """, (user_id, user_name, now_ts, now_ts, vip_val))
            else:
                vip_val = (1 if is_vip else 0) if is_vip is not None else row["is_vip"]
                name_clause = ", user_name = ?" if user_name else ""
                params = [now_ts, row["interaction_count"] + 1, vip_val]
                if user_name:
                    params.append(user_name)
                params.append(user_id)
                conn.execute(f"""
                UPDATE viewers
                SET last_seen_at = ?, interaction_count = ?, is_vip = ?{name_clause}
                WHERE user_id = ?
                """, params)
            conn.commit()
        finally:
            conn.close()

    def get_viewer(self, user_id: str) -> dict[str, Any] | None:
        """读取观众信息。"""
        user_id = str(user_id or "").strip()
        if not user_id:
            return None
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM viewers WHERE user_id = ?", (user_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    # -- 事实操作 ------------------------------------------------------
    def add_fact(
        self,
        user_id: str,
        fact: str,
        category: str = "preference",
        confidence: float = 0.8,
        source_kind: str = "self_claim",
        stability_reason: str = "",
        is_pinned: bool = False,
        ts: float | None = None,
        user_name: str = "",
    ) -> int:
        """插入或更新观众长期事实。"""
        user_id = str(user_id or "").strip()
        fact = str(fact or "").strip()
        if not user_id or not fact:
            return 0
        now_ts = time.time() if ts is None else float(ts)
        self.upsert_viewer(user_id, user_name=user_name, ts=now_ts)

        conn = self._connect()
        try:
            # 检查是否有完全相同的事实
            existing = conn.execute(
                "SELECT id, confidence, is_pinned FROM facts WHERE user_id = ? AND fact = ? AND status = 'active'",
                (user_id, fact)
            ).fetchone()
            if existing:
                fact_id = existing["id"]
                # 强化置信度
                new_conf = min(1.0, float(existing["confidence"]) + (1.0 - float(existing["confidence"])) * 0.1)
                pin_val = 1 if is_pinned or existing["is_pinned"] else 0
                conn.execute("""
                UPDATE facts
                SET confidence = ?, is_pinned = ?, last_confirmed_at = ?
                WHERE id = ?
                """, (new_conf, pin_val, now_ts, fact_id))
                conn.commit()
                return fact_id

            cursor = conn.execute("""
            INSERT INTO facts (user_id, category, fact, confidence, source_kind, stability_reason, is_pinned, created_at, last_confirmed_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')
            """, (user_id, category, fact, float(confidence), source_kind, stability_reason, 1 if is_pinned else 0, now_ts, now_ts))
            conn.commit()
            return int(cursor.lastrowid)
        finally:
            conn.close()

    def list_facts(self, user_id: str, active_only: bool = True) -> list[dict[str, Any]]:
        """获取观众的所有事实。"""
        user_id = str(user_id or "").strip()
        if not user_id:
            return []
        sql = "SELECT * FROM facts WHERE user_id = ?"
        if active_only:
            sql += " AND status = 'active'"
        sql += " ORDER BY is_pinned DESC, confidence DESC, created_at DESC"
        conn = self._connect()
        try:
            rows = conn.execute(sql, (user_id,)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # -- 事件流水 ------------------------------------------------------
    def record_event(
        self,
        record_id: str,
        session_id: str,
        user_id: str,
        user_name: str,
        kind: str,
        content: str,
        gift_name: str = "",
        gift_count: int = 0,
        ts: float | None = None,
    ) -> bool:
        """记录弹幕/礼物流水。"""
        now_ts = time.time() if ts is None else float(ts)
        user_id = str(user_id or "").strip()
        user_name = str(user_name or "")
        content = str(content or "")
        record_id = str(record_id or "")

        if user_id:
            is_vip = True if (kind == "gift" and gift_count and gift_count >= 10) else None
            self.upsert_viewer(user_id, user_name=user_name, ts=now_ts, is_vip=is_vip)

        tokens = _tokenize_for_fts(content)
        conn = self._connect()
        try:
            # FTS_DUP_FIX_20261101: events 靠 record_id UNIQUE 去重，fts_events 没有约束，
            # 无条件插会在重放同一批 raw 时堆双份，检索结果出现重复行。
            cur = conn.execute("""
            INSERT OR IGNORE INTO events (record_id, session_id, user_id, user_name, kind, content, gift_name, gift_count, ts)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (record_id, session_id, user_id, user_name, kind, content, gift_name or "", int(gift_count or 0), now_ts))

            # 同步写入 FTS5
            if cur and cur.rowcount > 0:
                try:
                    conn.execute("""
                    INSERT INTO fts_events (record_id, user_id, user_name, content, tokens)
                    VALUES (?, ?, ?, ?, ?)
                    """, (record_id, user_id, user_name, content, tokens))
                except Exception:
                    pass
            conn.commit()
            return True
        except Exception:
            logger.debug("数据库记录事件流水失败", exc_info=True)
            return False
        finally:
            conn.close()

    def get_recent_events_by_user(self, user_id: str, limit: int = 5) -> list[dict[str, Any]]:
        """获取目标观众的最近发言。"""
        user_id = str(user_id or "").strip()
        if not user_id:
            return []
        conn = self._connect()
        try:
            rows = conn.execute("""
            SELECT * FROM events
            WHERE user_id = ? AND kind = 'chat'
            ORDER BY ts DESC
            LIMIT ?
            """, (user_id, limit)).fetchall()
            res = [dict(r) for r in rows]
            res.reverse()
            return res
        finally:
            conn.close()

    def search_events_fts(self, query: str, user_id: str | None = None, limit: int = 5) -> list[dict[str, Any]]:
        """利用 FTS5 全文检索历史弹幕往事。"""
        query = str(query or "").strip()
        if not query:
            return []
        tokens = _tokenize_for_fts(query) or query
        conn = self._connect()
        try:
            # 1. 尝试 FTS5 MATCH
            try:
                if user_id:
                    rows = conn.execute("""
                    SELECT e.* FROM fts_events f
                    JOIN events e ON e.record_id = f.record_id
                    WHERE fts_events MATCH ? AND e.user_id = ?
                    ORDER BY e.ts DESC
                    LIMIT ?
                    """, (tokens, user_id, limit)).fetchall()
                else:
                    rows = conn.execute("""
                    SELECT e.* FROM fts_events f
                    JOIN events e ON e.record_id = f.record_id
                    WHERE fts_events MATCH ?
                    ORDER BY e.ts DESC
                    LIMIT ?
                    """, (tokens, limit)).fetchall()
                if rows:
                    return [dict(r) for r in rows]
            except Exception:
                pass

            # 2. 降级走普通 LIKE
            like_term = f"%{query}%"
            if user_id:
                rows = conn.execute("""
                SELECT * FROM events
                WHERE content LIKE ? AND user_id = ?
                ORDER BY ts DESC
                LIMIT ?
                """, (like_term, user_id, limit)).fetchall()
            else:
                rows = conn.execute("""
                SELECT * FROM events
                WHERE content LIKE ?
                ORDER BY ts DESC
                LIMIT ?
                """, (like_term, limit)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # -- 鱼鱼自身回复 --------------------------------------------------
    def record_bot_reply(
        self,
        user_id: str,
        user_name: str,
        reply_text: str,
        reply_to_ts: float | None = None,
        ts: float | None = None,
    ) -> bool:
        """记录鱼鱼输出给观众的回复。"""
        now_ts = time.time() if ts is None else float(ts)
        conn = self._connect()
        try:
            conn.execute("""
            INSERT INTO bot_replies (user_id, user_name, reply_text, reply_to_ts, ts)
            VALUES (?, ?, ?, ?, ?)
            """, (str(user_id or ""), str(user_name or ""), str(reply_text or ""), reply_to_ts, now_ts))
            conn.commit()
            return True
        finally:
            conn.close()

    def get_recent_bot_replies(self, limit: int = 3) -> list[dict[str, Any]]:
        """获取鱼鱼最近说过的几句话。"""
        conn = self._connect()
        try:
            rows = conn.execute("""
            SELECT * FROM bot_replies
            ORDER BY ts DESC
            LIMIT ?
            """, (limit,)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # -- 灾备/重建：从 raw/ 同步数据 ------------------------------------
    def sync_from_raw_jsonl(self, raw_dir: Path | str) -> int:
        """从 raw/ 目录导入历史事件重建数据库，无损还原。"""
        path = Path(raw_dir)
        if not path.exists():
            return 0
        total_synced = 0
        for f in sorted(path.glob("viewer-*.jsonl")):
            try:
                for line in f.read_text(encoding="utf-8", errors="ignore").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except Exception:
                        continue
                    if not isinstance(row, dict):
                        continue
                    # REBUILD_ALL_20261101: P0 之前的历史行没有 record_id，
                    # 原来直接跳过，3687 行只灌进 348 行。这里按“日期+行内容”
                    # 合成稳定 id，重复重建仍是同一条，不会灌出双份。
                    rid = str(row.get("record_id") or "")
                    if not rid:
                        digest = hashlib.sha1(
                            f"{f.name}|{row.get('ts')}|{row.get('user_id')}|"
                            f"{row.get('kind')}|{row.get('content')}".encode("utf-8")
                        ).hexdigest()[:24]
                        rid = f"legacy_{digest}"
                    if self.record_event(
                        record_id=rid,
                        session_id=row.get("session_id", ""),
                        user_id=row.get("user_id", ""),
                        user_name=row.get("user_name", ""),
                        kind=row.get("kind", ""),
                        content=row.get("content", ""),
                        gift_name=row.get("gift_name", ""),
                        gift_count=row.get("gift_count", 0),
                        ts=row.get("ts"),
                    ):
                        total_synced += 1
            except Exception:
                logger.debug("从文件同步至数据库失败: %s", f, exc_info=True)
        return total_synced

    # -- TTL 清理 ------------------------------------------------------
    def prune_user_ids(self, user_ids: list[str]) -> int:
        """TTL_PRUNE_ALL_20261101: 观众过期时把镜像里的行一起删掉。

        删的顺序是固定的：先删带外键的 facts / 事件流水 / FTS，最后才删
        viewers 主表，否则 foreign_keys=ON 直接报错。
        """
        ids = [str(uid or "").strip() for uid in (user_ids or []) if str(uid or "").strip()]
        if not ids:
            return 0
        marks = ",".join("?" * len(ids))
        conn = self._connect()
        changed = 0
        try:
            for table in ("facts", "events", "bot_replies"):
                cur = conn.execute(
                    f"DELETE FROM {table} WHERE user_id IN ({marks})", ids)
                changed += max(0, cur.rowcount)
            try:
                cur = conn.execute(
                    f"DELETE FROM fts_events WHERE user_id IN ({marks})", ids)
                changed += max(0, cur.rowcount)
            except sqlite3.Error:
                logger.debug("FTS 镜像清理跳过（当前环境无 fts_events）", exc_info=True)
            cur = conn.execute(
                f"DELETE FROM viewers WHERE user_id IN ({marks})", ids)
            changed += max(0, cur.rowcount)
            conn.commit()
            return changed
        except Exception:
            logger.debug("数据库镜像清理失败", exc_info=True)
            conn.rollback()
            return 0
        finally:
            conn.close()
