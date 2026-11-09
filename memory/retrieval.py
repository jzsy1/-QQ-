"""记忆检索 — 统一 recall API，按分层返回记忆。

规范 PART VII：
    recall(query, layers, limit) → {layer: [items]}
    每层独立检索，按重要度排序，应用保留策略过滤。

定位：
    retrieval.py 是 MemoryService 的上层封装：
    - 组合调用 MemoryService 已有 API（remember/forget/list_facts/...）
    - 添加分层视角（Working/Session/Profile/Preference/Task/Experience）
    - 提供 recall() 跨层统一检索
    - 提供 apply_policies() 应用保留策略
    - 不破坏现有 MemoryService API（向后兼容）

用法：
    retrieval = MemoryRetrieval(memory_service, policies)
    results = retrieval.recall("用户名字", layers=["profile", "experience"], limit=5)
    # results = {"profile": [...], "experience": [...]}
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from .db import Database
from .policies import (
    ALL_LAYERS,
    LAYER_EXPERIENCE,
    LAYER_PREFERENCE,
    LAYER_PROFILE,
    LAYER_SESSION,
    LAYER_TASK_HISTORY,
    LAYER_WORKING,
    LayerPolicy,
    apply_layer_policy,
    compute_importance,
    get_policy,
    policies_from_config,
)
from .service import MemoryService

logger = logging.getLogger(__name__)


class MemoryRetrieval:
    """统一记忆检索入口。

    Attributes:
        service: 底层 MemoryService（向后兼容）
        db: Database 实例
        policies: 层 → LayerPolicy 映射
    """

    def __init__(
        self,
        service: MemoryService,
        *,
        policies: dict[str, LayerPolicy] | None = None,
        config: dict | None = None,
    ) -> None:
        self.service = service
        self.db = service.db
        if policies is not None:
            self.policies = policies
        elif config is not None:
            self.policies = policies_from_config(config)
        else:
            from .policies import DEFAULT_POLICIES
            self.policies = dict(DEFAULT_POLICIES)

    # ------------------------------------------------------------------
    # recall — 跨层统一检索
    # ------------------------------------------------------------------

    def recall(
        self,
        query: str,
        *,
        layers: list[str] | None = None,
        limit: int = 10,
        session_id: str = "default",
    ) -> dict[str, list[dict]]:
        """跨层检索记忆。

        Args:
            query: 检索关键词
            layers: 要检索的层列表（None 表示全部）
            limit: 每层最大返回数
            session_id: 会话 ID（Working/Session 层用）

        Returns: {layer_name: [items]}
        """
        target_layers = list(layers) if layers else list(ALL_LAYERS)
        results: dict[str, list[dict]] = {}

        for layer in target_layers:
            layer = (layer or "").strip().lower()
            try:
                items = self._recall_layer(layer, query, limit, session_id)
                from .tombstones import filter_rows
                items = filter_rows(items, self.db.path, lambda r: " ".join(str(v) for v in r.values()))
                if items:
                    results[layer] = items
            except Exception:
                logger.debug("recall layer=%s failed", layer, exc_info=True)

        return results

    def _recall_layer(
        self,
        layer: str,
        query: str,
        limit: int,
        session_id: str,
    ) -> list[dict]:
        """单层检索。"""
        if layer == LAYER_WORKING:
            return self._recall_working(query, limit, session_id)
        if layer == LAYER_SESSION:
            return self._recall_session(query, limit, session_id)
        if layer == LAYER_PROFILE:
            return self._recall_profile(query, limit)
        if layer == LAYER_PREFERENCE:
            return self._recall_preference(query, limit)
        if layer == LAYER_TASK_HISTORY:
            return self._recall_task_history(query, limit)
        if layer == LAYER_EXPERIENCE:
            return self._recall_experience(query, limit)
        return []

    # ------------------------------------------------------------------
    # 各层检索实现
    # ------------------------------------------------------------------

    def _recall_working(self, query: str, limit: int, session_id: str) -> list[dict]:
        """Working Memory：当前对话最近 N 轮。"""
        try:
            digests = self.service.recent_digests(session_id=session_id, limit=limit)
        except Exception:
            return []
        q = (query or "").strip().lower()
        items: list[dict] = []
        for d in digests:
            user_text = str(d.get("user_text") or "")
            reply_text = str(d.get("reply_text") or "")
            content = f"{user_text}\n{reply_text}".strip()
            if not content:
                continue
            # 无 query 时返回全部；有 query 时做子串过滤
            if q and q not in content.lower():
                continue
            items.append({
                "layer": LAYER_WORKING,
                "content": content,
                "user_text": user_text,
                "reply_text": reply_text,
                "route": d.get("route_layer"),
                "importance": 0.8,  # Working 默认高重要度
            })
        return items[:limit]

    def _recall_session(self, query: str, limit: int, session_id: str) -> list[dict]:
        """Session Memory：整会话摘要（与 Working 同表，但 limit 更大）。"""
        try:
            session_limit = max(limit, 50)
            digests = self.service.recent_digests(session_id=session_id, limit=session_limit)
        except Exception:
            return []
        q = (query or "").strip().lower()
        items: list[dict] = []
        for d in digests:
            user_text = str(d.get("user_text") or "")
            reply_text = str(d.get("reply_text") or "")
            content = f"{user_text}\n{reply_text}".strip()
            if not content:
                continue
            if q and q not in content.lower():
                continue
            items.append({
                "layer": LAYER_SESSION,
                "content": content,
                "user_text": user_text,
                "reply_text": reply_text,
                "route": d.get("route_layer"),
                "importance": 0.6,
            })
        return items[:limit]

    def _recall_profile(self, query: str, limit: int) -> list[dict]:
        """User Profile：用户画像键值对。"""
        q = (query or "").strip().lower()
        items: list[dict] = []
        try:
            with self.db.connect() as conn:
                rows = conn.execute(
                    "SELECT key, value, updated_at FROM user_profile ORDER BY updated_at DESC"
                ).fetchall()
            for row in rows:
                key = str(row["key"] or "")
                value = str(row["value"] or "")
                content = f"{key}: {value}"
                if q and q not in content.lower():
                    continue
                items.append({
                    "layer": LAYER_PROFILE,
                    "content": content,
                    "key": key,
                    "value": value,
                    "updated_at": row["updated_at"],
                    "importance": 1.0,  # Profile 永久，重要度最高
                })
                if len(items) >= limit:
                    break
        except Exception:
            logger.debug("recall profile failed", exc_info=True)
        return items

    def _recall_preference(self, query: str, limit: int) -> list[dict]:
        """Preference：推断的用户偏好。"""
        q = (query or "").strip().lower()
        items: list[dict] = []
        try:
            with self.db.connect() as conn:
                rows = conn.execute(
                    """SELECT preference_key, preference_value, confidence, evidence
                       FROM inferred_preferences
                       ORDER BY confidence DESC, updated_at DESC
                       LIMIT ?""",
                    (max(limit * 3, 30),),
                ).fetchall()
            for row in rows:
                key = str(row["preference_key"] or "")
                value = str(row["preference_value"] or "")
                content = f"{key}: {value}"
                if q and q not in content.lower():
                    continue
                confidence = float(row["confidence"] or 0.5)
                items.append({
                    "layer": LAYER_PREFERENCE,
                    "content": content,
                    "key": key,
                    "value": value,
                    "confidence": confidence,
                    "evidence": row["evidence"],
                    "importance": confidence,
                })
                if len(items) >= limit:
                    break
        except Exception:
            logger.debug("recall preference failed", exc_info=True)
        return items

    def _recall_task_history(self, query: str, limit: int) -> list[dict]:
        """Task History：最近任务记录。"""
        q = (query or "").strip().lower()
        items: list[dict] = []
        try:
            tasks = self.service.recent_tasks(limit=max(limit * 3, 15))
            for t in tasks:
                action = str(t.get("action") or "")
                result = str(t.get("result") or "")
                content = f"{action} → {result}"
                if q and q not in content.lower():
                    continue
                success = bool(t.get("success"))
                items.append({
                    "layer": LAYER_TASK_HISTORY,
                    "content": content,
                    "action": action,
                    "result": result,
                    "success": success,
                    "importance": 0.7 if success else 0.4,
                })
                if len(items) >= limit:
                    break
        except Exception:
            logger.debug("recall task_history failed", exc_info=True)
        return items

    def _recall_experience(self, query: str, limit: int) -> list[dict]:
        """Experience：经验/错误修正/事实/行为模式。

        优先使用 Database.search_memory 的 FTS 跨表检索。
        """
        q = (query or "").strip()
        if not q or len(q) < 2:
            # 无 query 时返回最近的 confirmed 事实
            try:
                facts = self.service.list_facts(limit=limit)
                return [
                    {
                        "layer": LAYER_EXPERIENCE,
                        "content": f,
                        "source": "knowledge_entries",
                        "importance": 0.8,
                    }
                    for f in facts
                ]
            except Exception:
                return []

        items: list[dict] = []
        try:
            from .hybrid_retrieval import hybrid_search

            def _fts_fn(query_text: str, top_k: int) -> list[dict]:
                out: list[dict] = []
                for idx, r in enumerate(self.db.search_memory(query_text, limit=top_k)):
                    if r.get("match_channel") not in ("exact", "fts") and float(r.get("_search_score") or 0.0) < 1.5:
                        continue
                    st = str(r.get("source_type") or "unknown")
                    sid = r.get("source_id")
                    pool = str(r.get("pool") or ("fact" if st in ("auto_fact", "entity_edge") else "knowledge"))
                    out.append({
                        **r,
                        "id": r.get("id") or f"{st}:{sid if sid is not None else idx}",
                        "pool": pool,
                        "confidence": float(r.get("confidence") or 0.75),
                    })
                try:
                    terms = [query_text] + [t for t in self.db._extract_query_terms(query_text) if t != query_text][:5]
                    seen_ec: set[int] = set()
                    with self.db.connect() as conn:
                        for term in terms:
                            rows = conn.execute(
                                """SELECT id, error_pattern, correction_strategy, confidence, original_func, updated_at
                                   FROM error_corrections
                                   WHERE error_pattern LIKE ? OR original_func LIKE ? OR correction_strategy LIKE ?
                                   ORDER BY confidence DESC LIMIT ?""",
                                (f"%{term}%", f"%{term}%", f"%{term}%", top_k),
                            ).fetchall()
                            for row in rows:
                                rid = int(row["id"])
                                if rid in seen_ec:
                                    continue
                                seen_ec.add(rid)
                                out.append({
                                    "id": f"error_correction:{rid}",
                                    "content": f"错误: {row['error_pattern']} → 修正: {row['correction_strategy']}",
                                    "text": f"错误: {row['error_pattern']} → 修正: {row['correction_strategy']}",
                                    "source_type": "error_corrections",
                                    "source_id": rid,
                                    "original_func": row["original_func"],
                                    "confidence": float(row["confidence"] or 0.5),
                                    "updated_at": row["updated_at"],
                                    "pool": "correction",
                                    "rank": 0.0,
                                })
                                if len(seen_ec) >= top_k:
                                    break
                            if len(seen_ec) >= top_k:
                                break
                except Exception:
                    pass
                return out

            def _vec_fn(query_text: str, top_k: int) -> list[dict]:
                search_chunks = getattr(self.service, "search_chunks", None)
                if not callable(search_chunks):
                    return []
                out: list[dict] = []
                for idx, vh in enumerate(search_chunks(query_text, limit=min(top_k, 5))):
                    vtext = str(vh.get("text") or "").strip()
                    if not vtext:
                        continue
                    cid = vh.get("chunk_id") or vh.get("source_id") or idx
                    out.append({
                        **vh,
                        "id": f"chunk:{cid}",
                        "content": vtext,
                        "source_type": "chunk",
                        "source_id": cid,
                        "confidence": float(vh.get("score") or 0.7),
                        "updated_at": vh.get("created_at"),
                        "pool": "knowledge",
                    })
                return out

            fused = hybrid_search(q, _fts_fn, vec_search=_vec_fn, top_fts=max(limit * 2, 10), top_vec=5)
            seen_contents: set[str] = set()
            for r in fused:
                content = str(r.get("content") or "").strip()
                if not content or content in seen_contents:
                    continue
                seen_contents.add(content)
                source_type = str(r.get("source_type") or "unknown")
                rank = float(r.get("rank") or 0.0)
                importance = float(r.get("confidence") or 0.7)
                entry = {
                    "layer": LAYER_EXPERIENCE,
                    "content": content,
                    "source": source_type,
                    "source_id": r.get("source_id"),
                    "rank": rank,
                    "importance": importance if importance > 0 else 0.7,
                }
                if r.get("original_func") is not None:
                    entry["original_func"] = r.get("original_func")
                items.append(entry)
                if len(items) >= limit:
                    break
            try:
                self.db.reinforce_hits(
                    [{"source_type": it.get("source"), "source_id": it.get("source_id")} for it in items[:limit]]
                )
            except Exception:
                pass
        except Exception:
            logger.debug("recall experience search failed", exc_info=True)

        return items[:limit]

    def recall_context_hits(
        self,
        query: str,
        *,
        include_vec: bool = True,
        top_fts: int = 8,
        top_vec: int = 4,
        limit: int = 6,
    ) -> dict[str, Any]:
        """供 ContextBuilder 统一调用的混合记忆与语义召回入口。

        保持：
        - Database.search_memory 精确匹配 + FTS5 + 五级关键词分级 + _search_score >= 1.5 门禁
        - hybrid_search RRF 融合与时间衰减
        - vector_chunks 向量语义召回（按 include_vec 控制）
        - 跨通道内容去重、墓碑过滤与强化记数（reinforce_hits）
        """
        q = (query or "").strip()
        if not q:
            return {"mem_lines": [], "sem_lines": [], "injected": [], "audit_ids": []}

        from .hybrid_retrieval import hybrid_search

        def _fts_adapter(q_str: str, k: int) -> list[dict]:
            raw_hits = self.db.search_memory(q_str, limit=k)
            out: list[dict] = []
            for idx, h in enumerate(raw_hits):
                if h.get("match_channel") not in ("exact", "fts") and float(h.get("_search_score") or 0.0) < 1.5:
                    continue
                st = str(h.get("source_type") or "fact")
                sid = h.get("source_id")
                pool = str(h.get("pool") or ("fact" if st in ("auto_fact", "entity_edge") else "knowledge"))
                out.append({
                    **h,
                    "id": h.get("id") or f"{st}:{sid if sid is not None else idx}",
                    "pool": pool,
                    "confidence": float(h.get("confidence") or 0.75),
                })
            return out

        def _vec_adapter(q_str: str, k: int) -> list[dict]:
            if not include_vec:
                return []
            search_chunks = getattr(self.service, "search_chunks", None)
            if not callable(search_chunks):
                return []
            raw_vec = search_chunks(q_str, limit=min(k, 5))
            out: list[dict] = []
            for idx, vh in enumerate(raw_vec):
                vtext = str(vh.get("text") or "").strip()
                if not vtext:
                    continue
                cid = vh.get("chunk_id") or vh.get("source_id") or idx
                out.append({
                    **vh,
                    "id": f"chunk:{cid}",
                    "content": vtext[:140],
                    "pool": "knowledge",
                    "confidence": float(vh.get("score") or 0.7),
                    "updated_at": vh.get("created_at"),
                    "source_type": "chunk",
                    "source_id": cid,
                })
            return out

        hits = hybrid_search(
            q,
            _fts_adapter,
            vec_search=_vec_adapter if include_vec else None,
            top_fts=top_fts,
            top_vec=top_vec,
        )
        mem_lines: list[str] = []
        sem_lines: list[str] = []
        injected: list[dict] = []
        seen_norm: set[str] = set()
        for h in hits[:limit]:
            content = (h.get("content") or "").strip()
            if not content:
                continue
            norm = content[:120].strip()
            if norm in seen_norm:
                continue
            seen_norm.add(norm)
            if h.get("source_type") == "chunk":
                sem_lines.append(content[:120])
            else:
                mem_lines.append(content[:140])
                injected.append(h)

        audit_ids: list[str] = []
        if mem_lines:
            for _ih in injected[:4]:
                if _ih.get("source_id") is not None:
                    audit_ids.append(f"{_ih.get('source_type') or 'mem'}:{_ih.get('source_id')}")
            try:
                self.db.reinforce_hits(injected[:4])
            except Exception:
                pass
        return {
            "mem_lines": mem_lines[:4],
            "sem_lines": sem_lines[:3],
            "injected": injected[:4],
            "audit_ids": audit_ids,
        }

    # ------------------------------------------------------------------
    # 便捷访问 API
    # ------------------------------------------------------------------

    def get_working_memory(self, session_id: str = "default", limit: int = 10) -> list[dict]:
        """获取当前对话上下文。"""
        return self._recall_working("", limit, session_id)

    def get_session_memory(self, session_id: str = "default", limit: int = 50) -> list[dict]:
        """获取整会话摘要。"""
        return self._recall_session("", limit, session_id)

    def get_user_profile(self) -> dict:
        """获取用户画像（key→value 字典）。"""
        profile: dict[str, str] = {}
        try:
            with self.db.connect() as conn:
                rows = conn.execute(
                    "SELECT key, value FROM user_profile ORDER BY updated_at DESC"
                ).fetchall()
            for row in rows:
                key = str(row["key"] or "")
                if key:
                    profile[key] = str(row["value"] or "")
        except Exception:
            logger.debug("get_user_profile failed", exc_info=True)
        return profile

    def get_preferences(self, limit: int = 50) -> list[dict]:
        """获取用户偏好列表。"""
        return self._recall_preference("", limit)

    def get_task_history(self, limit: int = 10) -> list[dict]:
        """获取任务历史。"""
        return self._recall_task_history("", limit)

    def get_experience(self, query: str = "", limit: int = 10) -> list[dict]:
        """获取经验（支持空 query 返回最近事实）。"""
        return self._recall_experience(query, limit)

    def get_error_corrections(self, tool_name: str = "", limit: int = 10) -> list[dict]:
        """获取指定工具的错误修正经验。"""
        items: list[dict] = []
        try:
            with self.db.connect() as conn:
                if tool_name:
                    rows = conn.execute(
                        """SELECT error_pattern, correction_strategy, confidence,
                                  original_func, correction_func, success_count, fail_count
                           FROM error_corrections
                           WHERE original_func LIKE ? OR correction_func LIKE ?
                           ORDER BY confidence DESC LIMIT ?""",
                        (f"%{tool_name}%", f"%{tool_name}%", limit),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """SELECT error_pattern, correction_strategy, confidence,
                                  original_func, correction_func, success_count, fail_count
                           FROM error_corrections
                           ORDER BY confidence DESC LIMIT ?""",
                        (limit,),
                    ).fetchall()
            for row in rows:
                items.append({
                    "error_pattern": row["error_pattern"],
                    "correction_strategy": row["correction_strategy"],
                    "confidence": float(row["confidence"] or 0.3),
                    "original_func": row["original_func"],
                    "correction_func": row["correction_func"],
                    "success_count": int(row["success_count"] or 0),
                    "fail_count": int(row["fail_count"] or 0),
                })
        except Exception:
            logger.debug("get_error_corrections failed", exc_info=True)
        return items

    # ------------------------------------------------------------------
    # 策略应用
    # ------------------------------------------------------------------

    def apply_policies(self) -> dict[str, int]:
        """应用所有层的保留策略，返回每层删除条数。"""
        stats: dict[str, int] = {}
        now = datetime.now()

        try:
            with self.db.connect() as conn:
                # Working + Session 共用 conversation_digest 表
                # 严格保护未总结(summarized=0)与重要对话(important=1)，防止误删长期记忆原文
                session_policy = self.policies.get(LAYER_SESSION)
                if session_policy and session_policy.cleanup_table:
                    deleted = apply_layer_policy(
                        conn,
                        session_policy,
                        table="conversation_digest",
                        timestamp_col="created_at",
                        where_guard="summarized = 1 AND important = 0",
                        now=now,
                    )
                    stats[LAYER_SESSION] = deleted
                    stats[LAYER_WORKING] = 0  # Working 不单独清理

                # Profile（通常不清理，但策略可能限制容量）
                profile_policy = self.policies.get(LAYER_PROFILE)
                if profile_policy and profile_policy.cleanup_table:
                    stats[LAYER_PROFILE] = apply_layer_policy(
                        conn,
                        profile_policy,
                        table="user_profile",
                        timestamp_col="updated_at",
                        now=now,
                    )

                # Preference
                pref_policy = self.policies.get(LAYER_PREFERENCE)
                if pref_policy and pref_policy.cleanup_table:
                    stats[LAYER_PREFERENCE] = apply_layer_policy(
                        conn,
                        pref_policy,
                        table="inferred_preferences",
                        timestamp_col="created_at",
                        importance_col="confidence",
                        now=now,
                    )

                # Task History
                task_policy = self.policies.get(LAYER_TASK_HISTORY)
                if task_policy and task_policy.cleanup_table:
                    stats[LAYER_TASK_HISTORY] = apply_layer_policy(
                        conn,
                        task_policy,
                        table="task_history",
                        timestamp_col="created_at",
                        now=now,
                    )

                # Experience 跨多表，这里只清理容量超量的
                exp_policy = self.policies.get(LAYER_EXPERIENCE)
                if exp_policy:
                    exp_deleted = 0
                    for table in ("auto_facts", "consolidated_memory", "knowledge_entries"):
                        try:
                            exp_deleted += apply_layer_policy(
                                conn,
                                LayerPolicy(max_items=exp_policy.max_items),
                                table=table,
                                timestamp_col="created_at",
                                now=now,
                            )
                        except Exception:
                            pass
                    stats[LAYER_EXPERIENCE] = exp_deleted

                from .vector_chunks import VectorChunkStore

                VectorChunkStore.sweep_orphan_chunks_in_conn(conn)
                self.db.sweep_orphan_fts_in_conn(conn)
                conn.commit()
        except Exception:
            logger.error("apply_policies failed", exc_info=True)

        return stats

    # ------------------------------------------------------------------
    # 压缩（Working → Session → Consolidated）
    # ------------------------------------------------------------------

    def consolidate_session(self, session_id: str = "default") -> dict:
        """将整会话的对话摘要压缩为一条 consolidated_memory。

        Returns: {"consolidated_id": int, "source_turns": int, "summary": str}
        """
        try:
            with self.db.connect() as conn:
                rows = conn.execute(
                    """SELECT user_text, reply_text FROM conversation_digest
                       WHERE session_id = ?
                       ORDER BY id ASC""",
                    (session_id,),
                ).fetchall()
                if not rows:
                    return {"consolidated_id": None, "source_turns": 0, "summary": ""}

                # 简单压缩：拼接所有 user_text，截断到合理长度
                user_texts = [str(r["user_text"] or "").strip() for r in rows if r["user_text"]]
                reply_texts = [str(r["reply_text"] or "").strip() for r in rows if r["reply_text"]]

                summary_parts = []
                if user_texts:
                    summary_parts.append("用户: " + " | ".join(user_texts[:20]))
                if reply_texts:
                    summary_parts.append("助手: " + " | ".join(reply_texts[:20]))
                summary = "\n".join(summary_parts)
                # 截断到 2000 字符，避免 consolidated_memory 过大
                if len(summary) > 2000:
                    summary = summary[:2000] + "..."

                cur = conn.execute(
                    """INSERT INTO consolidated_memory
                       (summary, source_session_ids, source_turn_count, memory_type, importance)
                       VALUES (?, ?, ?, 'session_summary', 0.6)""",
                    (summary, session_id, len(rows)),
                )
                consolidated_id = int(cur.lastrowid)

                # 索引到 FTS
                self.db._fts_insert(
                    conn,
                    summary,
                    "consolidated",
                    consolidated_id,
                )
                conn.commit()

                return {
                    "consolidated_id": consolidated_id,
                    "source_turns": len(rows),
                    "summary": summary,
                }
        except Exception:
            logger.error("consolidate_session failed", exc_info=True)
            return {"consolidated_id": None, "source_turns": 0, "summary": ""}

    # ------------------------------------------------------------------
    # 统计
    # ------------------------------------------------------------------

    def stats(self) -> dict[str, int]:
        """返回每层记忆的条数统计。"""
        result: dict[str, int] = {}
        table_map = {
            LAYER_WORKING: ("conversation_digest", None),
            LAYER_SESSION: ("conversation_digest", None),
            LAYER_PROFILE: ("user_profile", None),
            LAYER_PREFERENCE: ("inferred_preferences", None),
            LAYER_TASK_HISTORY: ("task_history", None),
            LAYER_EXPERIENCE: (None, ["auto_facts", "consolidated_memory", "knowledge_entries", "error_corrections"]),
        }
        try:
            with self.db.connect() as conn:
                for layer, (table, multi) in table_map.items():
                    if table:
                        try:
                            row = conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()
                            result[layer] = int(row["c"] if row else 0)
                        except Exception:
                            result[layer] = 0
                    elif multi:
                        total = 0
                        for t in multi:
                            try:
                                row = conn.execute(f"SELECT COUNT(*) AS c FROM {t}").fetchone()
                                total += int(row["c"] if row else 0)
                            except Exception:
                                pass
                        result[layer] = total
        except Exception:
            logger.debug("stats failed", exc_info=True)
        return result


def get_task_history(limit: int = 200, db_path: str | None = None) -> list[dict]:
    """模块级 task_history 读取入口（供 P3 evolution.evaluate 调用）。"""
    import sqlite3
    from pathlib import Path

    limit = max(1, min(int(limit or 200), 2000))
    candidates = []
    if db_path:
        candidates.append(Path(db_path))
    root = Path(__file__).resolve().parents[2]
    candidates.extend([
        root / "kekesi" / "data" / "kekesi.db",
        root / "data" / "kekesi.db",
    ])
    for cand in candidates:
        if not cand.exists():
            continue
        try:
            conn = sqlite3.connect(str(cand))
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    "SELECT id, action, result, success, created_at "
                    "FROM task_history ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()
        except Exception:
            continue
    return []
