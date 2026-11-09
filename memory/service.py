"""记忆服务 — 偏好、应用注册、对话摘要"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime, timedelta

from ..app_aliases import app_names_match
from .db import Database

logger = logging.getLogger(__name__)

# 应用路径信任阈值
_MIN_CONFIDENCE = 0.25
_UNVERIFY_FAIL_COUNT = 2

# 人脑式记忆筛选：只把承诺、偏好、未完成、个人信息、纠正这类高价值内容
# 标记为重要。重要对话不参与常规配额清理，原文切片保留以便日后追问细节。
# 否定/争论是行为修正信号，优先级高于普通偏好，排在最前。
_PROMISE_SALIENCE_RE = re.compile(
    r"答应|承诺|保证|说好|一定(会|要)|务必|记得提醒|提醒我|别忘了|"
    r"下次|待办|我(明天|后天|今天|今晚).{0,8}(要|得|需要|准备)"
)
_SALIENCE_RULES = (
    (
        "否定/争论",
        re.compile(r"不对|错了|你上次|我不是说|别再这样"),
    ),
    (
        "承诺",
        _PROMISE_SALIENCE_RE,
    ),
    (
        "偏好",
        re.compile(
            r"我喜欢|我不喜欢|我讨厌|我习惯|我偏好|最喜欢|最讨厌|受不了|"
            r"以后都|别再"
        ),
    ),
    (
        "未完成",
        re.compile(r"还没|没做完|没结束|稍后再|等会儿|待会儿|回头再|先放一放|继续做"),
    ),
    (
        "个人信息",
        re.compile(
            r"我叫|我的名字|我住|我家|我在.{0,6}(工作|上班)|我养|我老婆|"
            r"我老公|我女朋友|我男朋友|我孩子|我的项目"
        ),
    ),
    (
        "纠正",
        re.compile(r"不是这样|不对|错了|你理解错|我说的是|不是这个意思"),
    ),
)


def _detect_salience(user_text: str, reply_text: str) -> tuple[bool, str]:
    """按人脑记忆偏好判定这轮对话是否值得长期保留细节。"""
    user = (user_text or "").strip()
    reply = (reply_text or "").strip()
    if not user and not reply:
        return False, ""
    reasons: list[str] = []
    for label, pattern in _SALIENCE_RULES:
        if user and pattern.search(user):
            reasons.append(label)
    # 助手自己许下的承诺也要记，否则"我明天提醒你"会随原文一起被清掉
    if reply and _PROMISE_SALIENCE_RE.search(reply):
        if "承诺" not in reasons:
            reasons.append("承诺")
    return bool(reasons), "+".join(reasons)


class MemoryService:
    def __init__(
        self,
        db: Database,
        learning: dict | None = None,
        context: dict | None = None,
        vector=None,
    ):
        self.db = db
        self.vector = vector
        cfg = learning or {}
        self._min_confidence = float(cfg.get("min_confidence", _MIN_CONFIDENCE))
        self._unverify_fail_count = int(cfg.get("unverify_after_fails", _UNVERIFY_FAIL_COUNT))
        ctx = context or {}
        self._digest_limit = max(1, int(ctx.get("digest_limit", 8)))
        # 长期记忆：跨会话保留最近几条历史摘要，供每轮注入
        self._lt_summary_limit = max(0, int(ctx.get("long_term_summary_limit", 2)))
        self._lt_summary_chars = max(40, int(ctx.get("long_term_summary_chars", 180)))
        self._cross_digest_limit = max(0, int(ctx.get("cross_session_digest_limit", 2)))
        self._retrieval = None

    @property
    def retrieval(self):
        """统一分层检索与治理入口（MemoryRetrieval）。"""
        if self._retrieval is None:
            from .retrieval import MemoryRetrieval

            self._retrieval = MemoryRetrieval(self)
        return self._retrieval

    def recall(
        self,
        query: str = "",
        *,
        layers: list[str] | None = None,
        limit: int = 10,
        session_id: str = "default",
    ) -> dict[str, list[dict]]:
        """跨层统一召回入口，委托给 MemoryRetrieval.recall。"""
        return self.retrieval.recall(
            query, layers=layers, limit=limit, session_id=session_id
        )

    def get_pref(self, key: str) -> str | None:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT value FROM user_profile WHERE key = ?", (key,)
            ).fetchone()
            return row["value"] if row else None

    def set_pref(self, key: str, value: str):
        with self.db.connect() as conn:
            conn.execute(
                """INSERT INTO user_profile (key, value, updated_at)
                   VALUES (?, ?, datetime('now'))
                   ON CONFLICT(key) DO UPDATE SET
                   value = excluded.value, updated_at = datetime('now')""",
                (key, value),
            )
            conn.commit()

    def recent_app_aliases(self, limit: int = 8) -> list[str]:
        """最近成功打开过的应用名（供模糊匹配上下文加权）。"""
        limit = max(1, min(int(limit), 20))
        with self.db.connect() as conn:
            rows = conn.execute(
                """SELECT alias FROM apps_registry
                   WHERE success_count > 0
                   ORDER BY updated_at DESC
                   LIMIT ?""",
                (limit,),
            ).fetchall()
        return [str(r["alias"]) for r in rows if r["alias"]]

    def find_app_row(
        self,
        name: str,
        *,
        context_apps: list[str] | None = None,
    ) -> dict | None:
        name = (name or "").strip()
        if not name:
            return None
        from ..app_match import best_app_match, query_variants
        from ..intent_utils import extract_action_core

        ctx: list[str] = list(context_apps or [])
        for a in self.recent_app_aliases(6):
            if a not in ctx:
                ctx.append(a)

        _, core = extract_action_core(name)
        candidates: list[str] = []
        if core and len(core) >= 2:
            candidates.append(core)
        if name not in candidates:
            candidates.append(name)

        with self.db.connect() as conn:
            rows = [dict(r) for r in conn.execute("SELECT * FROM apps_registry").fetchall()]

        for cand in candidates:
            for row in rows:
                if app_names_match(cand, row["alias"]):
                    return row

        aliases = [r["alias"] for r in rows if r.get("alias")]
        for variant in query_variants(name):
            hit = best_app_match(variant, aliases, context=ctx)
            if not hit:
                continue
            matched, score = hit
            for row in rows:
                if row["alias"] == matched:
                    logger.info(
                        "模糊匹配记忆应用 %r → %r score=%.2f",
                        name,
                        matched,
                        score,
                    )
                    return row
        return None

    def find_app(self, name: str) -> str | None:
        row = self.find_app_row(name)
        if not row:
            return None
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE apps_registry SET hit_count = hit_count + 1 WHERE alias = ?",
                (row["alias"],),
            )
            conn.commit()
        return row["command"]

    def recent_app_failures(self, name: str, lookback: int = 12) -> int:
        """从 task_history 统计该应用最近一次打开以来的连续失败次数。"""
        name = (name or "").strip()
        if not name:
            return 0
        with self.db.connect() as conn:
            rows = conn.execute(
                """SELECT action, success FROM task_history
                   ORDER BY id DESC LIMIT ?""",
                (lookback,),
            ).fetchall()
        consecutive = 0
        for row in rows:
            if not self._task_action_matches_app(row["action"], name):
                continue
            if row["success"]:
                break
            consecutive += 1
        return consecutive

    @staticmethod
    def _task_action_matches_app(action: str, name: str) -> bool:
        action = action or ""
        for prefix in ("open:", "open_type:", "open_running:", "open_type_running:"):
            if action.startswith(prefix):
                return app_names_match(name, action[len(prefix):])
        if action.startswith("open_app"):
            import re

            m = re.search(r'"app_name"\s*:\s*"([^"]+)"', action)
            if m:
                return app_names_match(name, m.group(1))
        return False

    def is_app_trusted(self, row: dict | None, name: str = "") -> bool:
        """判断应用记忆是否可信（可跳过重新扫描）。"""
        if not row:
            return False
        check_name = name or row.get("alias", "")
        if self.recent_app_failures(check_name) >= self._unverify_fail_count:
            return False
        fail = int(row.get("fail_count") or 0)
        succ = int(row.get("success_count") or 0)
        conf = float(row.get("confidence") or 0.5)
        if fail >= self._unverify_fail_count and fail > succ:
            return False
        if conf < self._min_confidence:
            return False
        if row.get("verified"):
            return True
        return succ >= 3 and conf >= 0.65

    def record_app_outcome(self, name: str, success: bool) -> bool:
        """记录应用启动成败，动态调整 confidence / verified。"""
        row = self.find_app_row(name)
        if not row:
            return False
        alias = row["alias"]
        with self.db.connect() as conn:
            if success:
                conn.execute(
                    """UPDATE apps_registry SET
                       success_count = success_count + 1,
                       verified = 1,
                       confidence = MIN(1.0, confidence + 0.08),
                       updated_at = datetime('now')
                       WHERE alias = ?""",
                    (alias,),
                )
            else:
                conn.execute(
                    """UPDATE apps_registry SET
                       fail_count = fail_count + 1,
                       confidence = MAX(0.1, confidence - 0.15),
                       updated_at = datetime('now')
                       WHERE alias = ?""",
                    (alias,),
                )
                cur = conn.execute(
                    """SELECT fail_count, success_count FROM apps_registry
                       WHERE alias = ?""",
                    (alias,),
                ).fetchone()
                if (
                    cur
                    and cur["fail_count"] >= self._unverify_fail_count
                    and cur["fail_count"] > cur["success_count"]
                ):
                    conn.execute(
                        "UPDATE apps_registry SET verified = 0 WHERE alias = ?",
                        (alias,),
                    )
                    logger.info("应用 %s 多次启动失败，已取消验证标记", alias)
            conn.commit()
        return True

    def get_app_process_names(self, name: str) -> list[str]:
        """读取已学会的进程名（成功打开/关闭后自动写入）。"""
        row = self.find_app_row(name)
        if not row:
            return []
        raw = row.get("process_names") or ""
        if not raw:
            return []
        try:
            from ..tools.process_ops import _HELPER_PROCESSES, _PROTECTED
            _blocked = _PROTECTED | _HELPER_PROCESSES
        except Exception:
            _blocked = frozenset()
        try:
            data = json.loads(raw)
            if not isinstance(data, list):
                return []
            out: list[str] = []
            for x in data:
                stem = str(x or "").replace(".exe", "", 1).strip()
                if stem and stem.lower() not in _blocked:
                    out.append(stem)
            return out
        except (json.JSONDecodeError, TypeError):
            return []

    def learn_app_process_names(
        self,
        name: str,
        process_names: list[str],
        command: str = "",
    ) -> None:
        """合并保存应用对应的进程名，供下次关闭/检测使用。"""
        name = (name or "").strip()
        try:
            from ..tools.process_ops import _HELPER_PROCESSES, _PROTECTED
            _blocked = _PROTECTED | _HELPER_PROCESSES
        except Exception:
            _blocked = frozenset()
        clean = []
        seen: set[str] = set()
        for n in process_names or []:
            stem = str(n).replace(".exe", "", 1).strip()
            if not stem:
                continue
            k = stem.lower()
            if k in _blocked:
                continue
            if k not in seen:
                seen.add(k)
                clean.append(stem)
        if not name or not clean:
            return

        existing = self.find_app_row(name)
        merged = clean[:]
        if existing:
            for n in self.get_app_process_names(existing["alias"]):
                k = n.lower()
                if k not in seen:
                    seen.add(k)
                    merged.append(n)

        payload = json.dumps(merged, ensure_ascii=False)
        canonical = existing["alias"] if existing else name
        cmd = (command or (existing or {}).get("command") or "").strip()

        with self.db.connect() as conn:
            if existing:
                conn.execute(
                    """UPDATE apps_registry SET
                       process_names = ?,
                       command = CASE WHEN ? != '' THEN ? ELSE command END,
                       updated_at = datetime('now')
                       WHERE alias = ?""",
                    (payload, cmd, cmd, canonical),
                )
            else:
                conn.execute(
                    """INSERT INTO apps_registry
                       (alias, command, process_names, confidence, verified, updated_at)
                       VALUES (?, ?, ?, 0.8, 1, datetime('now'))""",
                    (canonical, cmd or " ", payload),
                )
            conn.commit()
        logger.info("已学会 %s 的进程名: %s", canonical, merged)

    def register_app(self, alias: str, command: str, verified: bool | None = None):
        """注册应用启动方式。

        - verified=True：用户明确教学（remember_app），写入并标记已验证
        - verified=False/None：自动扫描学习；若已有「可信」记录则不覆盖
        - 相近但不可信：直接用新 command 覆盖
        """
        alias = (alias or "").strip()
        command = (command or "").strip()
        if not alias or not command:
            return

        existing = self.find_app_row(alias)
        with self.db.connect() as conn:
            if existing:
                canonical = existing["alias"]
                user_taught = verified is True
                if not user_taught and self.is_app_trusted(existing, name=alias):
                    conn.execute(
                        """UPDATE apps_registry
                           SET hit_count = hit_count + 1, updated_at = datetime('now')
                           WHERE alias = ?""",
                        (canonical,),
                    )
                else:
                    new_verified = 1 if verified else 0
                    conn.execute(
                        """UPDATE apps_registry SET
                           command = ?,
                           verified = ?,
                           confidence = MIN(1.0, confidence + 0.1),
                           hit_count = hit_count + 1,
                           updated_at = datetime('now')
                           WHERE alias = ?""",
                        (command, new_verified, canonical),
                    )
            else:
                conn.execute(
                    """INSERT INTO apps_registry
                       (alias, command, confidence, hit_count, verified, updated_at)
                       VALUES (?, ?, 0.7, 1, ?, datetime('now'))""",
                    (alias, command, 1 if verified else 0),
                )
            conn.commit()

    def cleanup(
        self,
        digest_keep: int = 50,
        task_keep: int = 200,
        prune_stale_prefs: bool = True,
        qs_keep: int = 2000,
        digest_per_session: int = 40,
        execution_history_keep: int = 2000,
        working_retention_days: int = 7,
        salient_keep_days: int = 365,
        salient_keep_max: int = 500,
    ) -> dict[str, int]:
        """清理工作记忆与历史对话。

        工作记忆区（task_history / execution_history / qs_events）按天滚动；
        上下文记忆区（对话摘要）只删除已总结的原始记录，摘要长期保留。
        """
        stats = {
            "digest_deleted": 0,
            "task_deleted": 0,
            "prefs_deleted": 0,
            "qs_deleted": 0,
            "digest_session_trimmed": 0,
            "execution_history_deleted": 0,
            "working_expired": 0,
            "salient_deleted": 0,
        }

        with self.db.connect() as conn:
            # 工作记忆区：按保留天数滚动（支持 'YYYY-MM-DD HH:MM:SS' 与 ISO 两种格式）
            if working_retention_days > 0:
                cutoff = (
                    datetime.now() - timedelta(days=int(working_retention_days))
                ).date().isoformat()
                for table in ("task_history", "execution_history", "qs_events"):
                    try:
                        cur = conn.execute(
                            f"DELETE FROM {table} WHERE substr(created_at, 1, 10) < ?",
                            (cutoff,),
                        )
                        stats["working_expired"] += cur.rowcount
                    except Exception:
                        logger.debug("工作记忆清理失败: %s", table, exc_info=True)

            if digest_keep >= 0:
                keep_ids = conn.execute(
                    """SELECT id FROM conversation_digest
                       ORDER BY id DESC LIMIT ?""",
                    (max(digest_keep, 0),),
                ).fetchall()
                if keep_ids:
                    min_id = min(r["id"] for r in keep_ids)
                    cur = conn.execute(
                        "DELETE FROM conversation_digest "
                        "WHERE id < ? AND summarized = 1 AND important = 0",
                        (min_id,),
                    )
                    stats["digest_deleted"] = cur.rowcount
                elif digest_keep == 0:
                    cur = conn.execute(
                        "DELETE FROM conversation_digest "
                        "WHERE summarized = 1 AND important = 0"
                    )
                    stats["digest_deleted"] = cur.rowcount

            if task_keep >= 0:
                keep_ids = conn.execute(
                    "SELECT id FROM task_history ORDER BY id DESC LIMIT ?",
                    (max(task_keep, 0),),
                ).fetchall()
                if keep_ids:
                    min_id = min(r["id"] for r in keep_ids)
                    cur = conn.execute(
                        "DELETE FROM task_history WHERE id < ?", (min_id,)
                    )
                    stats["task_deleted"] = cur.rowcount
                elif task_keep == 0:
                    cur = conn.execute("DELETE FROM task_history")
                    stats["task_deleted"] = cur.rowcount

            if prune_stale_prefs:
                cur = conn.execute(
                    """DELETE FROM user_profile
                       WHERE value IN ('已遗忘', '已删除', '')"""
                )
                stats["prefs_deleted"] = cur.rowcount

            if qs_keep >= 0:
                keep_ids = conn.execute(
                    "SELECT id FROM qs_events ORDER BY id DESC LIMIT ?",
                    (max(qs_keep, 0),),
                ).fetchall()
                if keep_ids:
                    min_id = min(r["id"] for r in keep_ids)
                    cur = conn.execute(
                        "DELETE FROM qs_events WHERE id < ?", (min_id,)
                    )
                    stats["qs_deleted"] = cur.rowcount
                elif qs_keep == 0:
                    cur = conn.execute("DELETE FROM qs_events")
                    stats["qs_deleted"] = cur.rowcount

            if digest_per_session > 0:
                sessions = conn.execute(
                    "SELECT DISTINCT session_id FROM conversation_digest"
                ).fetchall()
                for row in sessions:
                    sid = row["session_id"]
                    cur = conn.execute(
                        """DELETE FROM conversation_digest
                           WHERE session_id = ? AND summarized = 1 AND important = 0
                             AND id NOT IN (
                               SELECT id FROM conversation_digest
                               WHERE session_id = ?
                               ORDER BY id DESC LIMIT ?
                           )""",
                        (sid, sid, digest_per_session),
                    )
                    stats["digest_session_trimmed"] += cur.rowcount

            # 重要对话（承诺/偏好/未完成/个人信息/纠正）不参与上面的配额清理，
            # 单独按「最多保留条数 + 保留天数」滚动，相当于人的长期记忆。
            if salient_keep_max >= 0:
                keep_ids = conn.execute(
                    """SELECT id FROM conversation_digest
                       WHERE important = 1
                       ORDER BY id DESC LIMIT ?""",
                    (max(salient_keep_max, 0),),
                ).fetchall()
                if keep_ids:
                    min_id = min(r["id"] for r in keep_ids)
                    cur = conn.execute(
                        """DELETE FROM conversation_digest
                           WHERE important = 1 AND summarized = 1 AND id < ?""",
                        (min_id,),
                    )
                    stats["salient_deleted"] += cur.rowcount
                elif salient_keep_max == 0:
                    cur = conn.execute(
                        """DELETE FROM conversation_digest
                           WHERE important = 1 AND summarized = 1"""
                    )
                    stats["salient_deleted"] += cur.rowcount
            if salient_keep_days > 0:
                cutoff = (
                    datetime.now() - timedelta(days=int(salient_keep_days))
                ).date().isoformat()
                cur = conn.execute(
                    """DELETE FROM conversation_digest
                       WHERE important = 1 AND summarized = 1
                         AND substr(created_at, 1, 10) < ?""",
                    (cutoff,),
                )
                stats["salient_deleted"] += cur.rowcount

            if execution_history_keep >= 0:
                try:
                    keep_ids = conn.execute(
                        "SELECT id FROM execution_history ORDER BY id DESC LIMIT ?",
                        (max(execution_history_keep, 0),),
                    ).fetchall()
                    if keep_ids:
                        min_id = min(r["id"] for r in keep_ids)
                        cur = conn.execute(
                            "DELETE FROM execution_history WHERE id < ?", (min_id,)
                        )
                        stats["execution_history_deleted"] = cur.rowcount
                    elif execution_history_keep == 0:
                        cur = conn.execute("DELETE FROM execution_history")
                        stats["execution_history_deleted"] = cur.rowcount
                except sqlite3.OperationalError:
                    pass

            from .vector_chunks import VectorChunkStore
            from .policies import LAYER_PREFERENCE, apply_layer_policy

            try:
                pref_policy = self.retrieval.policies.get(LAYER_PREFERENCE)
                if pref_policy and pref_policy.cleanup_table:
                    stats["inferred_prefs_trimmed"] = apply_layer_policy(
                        conn,
                        pref_policy,
                        table="inferred_preferences",
                        timestamp_col="created_at",
                        importance_col="confidence",
                    )
            except Exception:
                stats["inferred_prefs_trimmed"] = 0

            orphan_rep = VectorChunkStore.sweep_orphan_chunks_in_conn(conn)
            stats["orphan_chunks_deleted"] = orphan_rep.get("orphan_chunks", 0)
            stats["orphan_vectors_deleted"] = orphan_rep.get("orphan_vectors", 0)
            stats["orphan_fts_deleted"] = self.db.sweep_orphan_fts_in_conn(conn)
            conn.commit()
        return stats

    def save_digest(
        self,
        user_text: str,
        reply_text: str,
        route_layer: str,
        session_id: str = "default",
    ) -> int:
        try:  # KEKESI_PCE_P4_20261101_V2
            from kekesi.memory import u1_migration as _p4_u1
            _p4_fn = getattr(_p4_u1, 'ensure_u1_columns', None) or getattr(_p4_u1, 'ensure', None) or getattr(_p4_u1, 'migrate', None)
            if _p4_fn is not None:
                _p4_fn()
        except Exception:
            pass
        with self.db.connect() as conn:
            count = conn.execute(
                "SELECT COUNT(*) AS c FROM conversation_digest WHERE session_id = ?",
                (session_id,),
            ).fetchone()["c"]
            important, salience = _detect_salience(user_text, reply_text)
            cur = conn.execute(
                """INSERT INTO conversation_digest
                   (session_id, turn_index, user_text, reply_text, route_layer,
                    important, salience)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id,
                    count + 1,
                    user_text,
                    reply_text,
                    route_layer,
                    1 if important else 0,
                    salience,
                ),
            )
            digest_id = int(cur.lastrowid or 0)
            per_session = max(20, self._digest_limit * 5)
            cur_del = conn.execute(
                """DELETE FROM conversation_digest
                   WHERE session_id = ? AND summarized = 1 AND important = 0
                     AND id NOT IN (
                       SELECT id FROM conversation_digest
                       WHERE session_id = ?
                       ORDER BY id DESC LIMIT ?
                   )""",
                (session_id, session_id, per_session),
            )
            if cur_del.rowcount > 0:
                from .vector_chunks import VectorChunkStore

                VectorChunkStore.sweep_orphan_chunks_in_conn(conn)
            conn.commit()
        if self.vector is not None and digest_id:
            try:
                self.vector.enqueue_turn(
                    digest_id, session_id, user_text, reply_text
                )
            except Exception:
                logger.debug("切片入队失败", exc_info=True)
        return digest_id

    def search_chunks(
        self,
        query: str,
        limit: int | None = None,
        *,
        include_summarized: bool = False,
    ) -> list[dict]:
        """语义切片召回；未启用向量时返回空列表。"""
        if self.vector is None:
            return []
        try:
            return self.vector.search(
                query, limit=limit, include_summarized=include_summarized
            )
        except Exception:
            logger.debug("语义召回失败", exc_info=True)
            return []

    def recent_digests(
        self,
        session_id: str = "default",
        limit: int | None = None,
    ) -> list[dict]:
        fetch_limit = limit if limit is not None else self._digest_limit
        with self.db.connect() as conn:
            rows = conn.execute(
                """SELECT user_text, reply_text, route_layer
                   FROM conversation_digest WHERE session_id = ?
                   ORDER BY id DESC LIMIT ?""",
                (session_id, fetch_limit),
            ).fetchall()
            return [dict(r) for r in reversed(rows)]

    def recent_summaries(self, limit: int | None = None) -> list[str]:
        """最近的历史对话摘要（跨会话，按时间正序返回）。"""
        n = self._lt_summary_limit if limit is None else max(0, int(limit))
        if n <= 0:
            return []
        with self.db.connect() as conn:
            rows = conn.execute(
                """SELECT summary, created_at FROM consolidated_memory
                   ORDER BY id DESC LIMIT ?""",
                (n,),
            ).fetchall()
        out: list[str] = []
        for row in reversed(rows):
            text = " ".join(str(row["summary"] or "").split())
            if not text:
                continue
            if len(text) > self._lt_summary_chars:
                text = text[: self._lt_summary_chars - 1] + "…"
            day = str(row["created_at"] or "")[:10]
            out.append(f"（{day}）{text}" if day else text)
        return out

    def recent_digests_any_session(self, limit: int | None = None) -> list[dict]:
        """跨会话最近原始对话（避免唤醒轮换 session 后「上次聊了什么」查不到）。"""
        n = self._cross_digest_limit if limit is None else max(0, int(limit))
        if n <= 0:
            return []
        with self.db.connect() as conn:
            rows = conn.execute(
                """SELECT user_text, reply_text, created_at
                   FROM conversation_digest ORDER BY id DESC LIMIT ?""",
                (n,),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def log_task(self, action: str, result: str, success: bool = True):
        with self.db.connect() as conn:
            conn.execute(
                "INSERT INTO task_history (action, result, success) VALUES (?, ?, ?)",
                (action, result, int(success)),
            )
            conn.commit()

    def recent_tasks(self, limit: int = 5) -> list[dict]:
        limit = max(1, min(int(limit), 20))
        with self.db.connect() as conn:
            rows = conn.execute(
                """SELECT action, result, success
                   FROM task_history ORDER BY id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
            return [dict(r) for r in reversed(rows)]

    def remember(self, fact: str) -> tuple[bool, str]:
        fact = (fact or "").strip()
        if not fact:
            return False, "没有要记的内容"
        existing = self.list_facts(limit=100)
        if fact in existing:
            with self.db.connect() as conn:
                conn.execute(
                    """UPDATE knowledge_entries SET
                       confidence = MIN(1.0, confidence + 0.1),
                       hit_count = hit_count + 1,
                       last_used = datetime('now')
                       WHERE payload = ? AND status = 'confirmed'""",
                    (fact,),
                )
                conn.commit()
            return True, "这个我早就记下了"
        with self.db.connect() as conn:
            cur = conn.execute(
                """INSERT INTO knowledge_entries
                   (category, trigger, payload, confidence, status)
                   VALUES ('fact', ?, ?, 0.9, 'confirmed')""",
                (fact, fact),
            )
            entry_id = int(cur.lastrowid)
            self.db._fts_insert(conn, fact, "knowledge", entry_id)
            conn.commit()
        return True, f"好的，我记住了：{fact}"

    def _forget_coordinator(self):
        from .forget import ForgetCoordinator

        coord = getattr(self, "_forget_coord", None)
        if coord is None:
            coord = ForgetCoordinator(self.db.path)
            self._forget_coord = coord
        return coord

    def forget(self, keyword: str) -> tuple[bool, str]:
        """跨四库遗忘：各库内单事务原子删除（主表 + FTS + 切片 + 向量），
        全部核验通过才报告成功；未完成的作业持久化，启动时自动重试。"""
        keyword = (keyword or "").strip()
        if not keyword:
            return False, "请告诉我要忘掉什么"
        result = self._forget_coordinator().forget(keyword)
        return result.ok, result.message()

    def resume_forget_jobs(self) -> int:
        """重跑上次未完成/未核验通过的遗忘作业，返回本次核验通过的数量。"""
        results = self._forget_coordinator().resume_pending()
        return sum(1 for r in results if r.ok)

    def list_facts(self, limit: int = 20) -> list[str]:
        with self.db.connect() as conn:
            rows = conn.execute(
                """SELECT payload FROM knowledge_entries
                   WHERE category = 'fact' AND status = 'confirmed'
                   ORDER BY id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
            return [row["payload"] for row in rows]

    def remember_action(
        self,
        trigger: str,
        tool: str,
        args: dict | None = None,
        confidence: float = 0.85,
    ) -> None:
        """将「用户说法 → 工具调用」写入知识库；相近说法合并到同一 trigger。"""
        from ..intent_utils import (
            ACTION_TRIGGER_MERGE_SIM,
            parse_action_payload,
            trigger_similarity,
        )

        trigger = (trigger or "").strip()
        tool = (tool or "").strip()
        if not trigger or not tool:
            return

        # 与已有相近说法合并（不要求每次说法完全一致）
        with self.db.connect() as conn:
            rows = conn.execute(
                """SELECT id, trigger, payload, confidence FROM knowledge_entries
                   WHERE category = 'action' AND status = 'confirmed'"""
            ).fetchall()
        best_row = None
        best_sim = 0.0
        for row in rows:
            parsed = parse_action_payload(row["payload"])
            if not parsed or parsed[0] != tool:
                continue
            sim = trigger_similarity(trigger, row["trigger"])
            if sim >= ACTION_TRIGGER_MERGE_SIM and sim > best_sim:
                best_sim = sim
                best_row = row
        if best_row is not None:
            trigger = (best_row["trigger"] or "").strip() or trigger

        payload = json.dumps(
            {"tool": tool, "args": args or {}},
            ensure_ascii=False,
        )
        with self.db.connect() as conn:
            row = conn.execute(
                """SELECT id, confidence FROM knowledge_entries
                   WHERE category = 'action' AND trigger = ?
                   ORDER BY (status = 'confirmed') DESC, id DESC LIMIT 1""",
                (trigger,),
            ).fetchone()
            if row:
                entry_id = int(row["id"])
                new_conf = min(1.0, max(float(row["confidence"] or 0.5), 0.65) + 0.05)
                conn.execute(
                    """UPDATE knowledge_entries SET
                       payload = ?, confidence = ?, status = 'confirmed',
                       hit_count = hit_count + 1,
                       last_used = datetime('now')
                       WHERE id = ?""",
                    (payload, new_conf, entry_id),
                )
            else:
                cur = conn.execute(
                    """INSERT INTO knowledge_entries
                       (category, trigger, payload, confidence, status)
                       VALUES ('action', ?, ?, ?, 'confirmed')""",
                    (trigger, payload, confidence),
                )
                entry_id = int(cur.lastrowid)
            fts_text = self.db._format_knowledge_content(trigger, payload)
            self.db._fts_insert(conn, fts_text, "knowledge", entry_id)
            conn.commit()

    def update_knowledge(
        self,
        entry_id: int,
        *,
        trigger: str | None = None,
        payload: str | None = None,
        status: str | None = None,
        confidence: float | None = None,
    ) -> bool:
        """更新知识条目并同步维护 memory_fts 索引（trigger + payload）。"""
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT id, trigger, payload, status, confidence FROM knowledge_entries WHERE id = ?",
                (int(entry_id),),
            ).fetchone()
            if not row:
                return False
            new_trig = row["trigger"] if trigger is None else trigger
            new_pay = row["payload"] if payload is None else payload
            new_st = row["status"] if status is None else status
            new_conf = float(row["confidence"] or 0.5) if confidence is None else float(confidence)
            conn.execute(
                """UPDATE knowledge_entries
                   SET trigger = ?, payload = ?, status = ?, confidence = ?,
                       last_used = datetime('now')
                   WHERE id = ?""",
                (new_trig, new_pay, new_st, new_conf, int(entry_id)),
            )
            if new_st == "confirmed":
                fts_text = self.db._format_knowledge_content(new_trig, new_pay)
                self.db._fts_insert(conn, fts_text, "knowledge", int(entry_id))
            else:
                self.db._fts_delete(conn, "knowledge", int(entry_id))
            conn.commit()
            return True

    def delete_knowledge(self, entry_id: int) -> bool:
        """删除知识条目并同步清理 memory_fts 索引。"""
        with self.db.connect() as conn:
            self.db._fts_delete(conn, "knowledge", int(entry_id))
            cur = conn.execute(
                "DELETE FROM knowledge_entries WHERE id = ?", (int(entry_id),)
            )
            conn.commit()
            return bool(cur.rowcount)
