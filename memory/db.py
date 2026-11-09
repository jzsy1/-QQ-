"""SQLite 数据库初始化与连接"""

from __future__ import annotations

import sqlite3
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS user_profile (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS apps_registry (
    alias TEXT PRIMARY KEY,
    command TEXT NOT NULL,
    confidence REAL DEFAULT 0.5,
    hit_count INTEGER DEFAULT 0,
    success_count INTEGER DEFAULT 0,
    fail_count INTEGER DEFAULT 0,
    verified INTEGER DEFAULT 0,
    process_names TEXT DEFAULT '',
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS knowledge_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL,
    trigger TEXT NOT NULL,
    payload TEXT NOT NULL,
    confidence REAL DEFAULT 0.5,
    hit_count INTEGER DEFAULT 0,
    status TEXT DEFAULT 'pending',
    created_at TEXT DEFAULT (datetime('now')),
    last_used TEXT
);

CREATE TABLE IF NOT EXISTS conversation_digest (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    turn_index INTEGER DEFAULT 0,
    user_text TEXT,
    reply_text TEXT,
    route_layer TEXT,
    summarized INTEGER DEFAULT 0,
    important INTEGER DEFAULT 0,
    salience TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS task_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL,
    result TEXT,
    success INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now'))
);

-- === 自主学习新增表 ===

-- 自动提取的事实（不需要用户主动说"记住"）
CREATE TABLE IF NOT EXISTS auto_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fact TEXT NOT NULL,
    category TEXT DEFAULT 'general',
    source_session_id TEXT,
    confidence REAL DEFAULT 0.6,
    hit_count INTEGER DEFAULT 0,
    verified INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    last_used TEXT,
    status TEXT DEFAULT 'open',
    due_date TEXT
);

-- 用户行为模式（时间、习惯、偏好）
CREATE TABLE IF NOT EXISTS behavior_patterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern_type TEXT NOT NULL,
    trigger_condition TEXT NOT NULL,
    expected_action TEXT NOT NULL,
    frequency INTEGER DEFAULT 1,
    last_observed TEXT DEFAULT (datetime('now')),
    confidence REAL DEFAULT 0.5,
    created_at TEXT DEFAULT (datetime('now'))
);

-- 错误修正经验（从失败中学习）
CREATE TABLE IF NOT EXISTS error_corrections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    error_pattern TEXT NOT NULL,
    tool_name TEXT,
    correction_strategy TEXT NOT NULL,
    success_count INTEGER DEFAULT 0,
    fail_count INTEGER DEFAULT 1,
    confidence REAL DEFAULT 0.3,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    original_func TEXT DEFAULT '',
    correction_func TEXT DEFAULT '',
    error_signature TEXT DEFAULT ''
);

-- 长期记忆压缩（旧对话的摘要）
CREATE TABLE IF NOT EXISTS consolidated_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    summary TEXT NOT NULL,
    source_session_ids TEXT,
    source_turn_count INTEGER DEFAULT 0,
    memory_type TEXT DEFAULT 'general',
    importance REAL DEFAULT 0.5,
    created_at TEXT DEFAULT (datetime('now'))
);

-- 用户偏好推断
CREATE TABLE IF NOT EXISTS inferred_preferences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    preference_key TEXT NOT NULL,
    preference_value TEXT NOT NULL,
    evidence TEXT,
    confidence REAL DEFAULT 0.5,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

-- 全文检索索引（trigram，适合中文子串匹配，替代向量库）
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    content,
    source_type UNINDEXED,
    source_id UNINDEXED,
    tokenize='trigram'
);
"""

# 默认注入条件：open 且未衰减到排除线（rule 类记忆豁免衰减）；
# done 保留 7 天宽限期后不再注入，superseded/expired 永不注入，但显式检索仍可查。
_FACT_INJECTION_SQL = (
    "((status = 'open' AND (confidence >= 0.3 OR category = 'rule')) "
    "OR (status = 'done' AND COALESCE(last_used, created_at) >= datetime('now', '-7 days')))"
)


def resolve_memory_db_path(base_dir: str | Path) -> Path:
    """解析记忆库路径：优先 config.yaml 的 memory.db_path，避免路径写死。"""
    base = Path(base_dir)
    configured = "data/kekesi.db"
    cfg_path = base / "config.yaml"
    if cfg_path.exists():
        try:
            import yaml

            with open(cfg_path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            mem = data.get("memory") or {}
            configured = str(mem.get("db_path") or configured)
        except Exception:
            pass
    path = Path(configured)
    return path if path.is_absolute() else base / path


class Database:
    def __init__(self, db_path: str | Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()
        self._migrate_schema()
        self._seed_apps()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            # busy_timeout 放宽到 15s：voice 入口做长事务/DDL 时仍可能短暂持锁，
            # 5s 不够导致另一个进程启动清理报 database is locked
            conn.execute("PRAGMA busy_timeout=15000")
            conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.Error:
            pass
        return conn

    def _init_schema(self):
        with self.connect() as conn:
            conn.executescript(_SCHEMA)
            conn.commit()

    def _migrate_schema(self):
        with self.connect() as conn:
            cols = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(apps_registry)").fetchall()
            }
            if "verified" not in cols:
                conn.execute(
                    "ALTER TABLE apps_registry ADD COLUMN verified INTEGER DEFAULT 0"
                )
            if "success_count" not in cols:
                conn.execute(
                    "ALTER TABLE apps_registry ADD COLUMN success_count INTEGER DEFAULT 0"
                )
            if "fail_count" not in cols:
                conn.execute(
                    "ALTER TABLE apps_registry ADD COLUMN fail_count INTEGER DEFAULT 0"
                )
            if "process_names" not in cols:
                conn.execute(
                    "ALTER TABLE apps_registry ADD COLUMN process_names TEXT DEFAULT ''"
                )
            self._migrate_error_corrections(conn)
            self._migrate_behavior_patterns(conn)
            self._migrate_conversation_digest(conn)
            self._migrate_auto_facts(conn)
            self._ensure_jarvis_tables(conn)
            self._ensure_memory_meta(conn)
            self._ensure_memory_fts(conn)
            self._ensure_qs_events(conn)
            self._ensure_static_replay(conn)
            from .core_memory import ensure_schema as _ensure_core_memory

            _ensure_core_memory(conn)
            from .self_memory import ensure_schema as _ensure_self_memory

            _ensure_self_memory(conn)
            from .forget import ensure_schema as _ensure_forget

            _ensure_forget(conn)
            self._migrate_ui_cache(conn)
            conn.commit()

    def _migrate_conversation_digest(self, conn: sqlite3.Connection):
        """为历史对话增加「已总结」标记，支持跨会话增量总结。"""
        cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(conversation_digest)").fetchall()
        }
        if not cols:
            return
        if "summarized" not in cols:
            conn.execute(
                "ALTER TABLE conversation_digest ADD COLUMN summarized INTEGER DEFAULT 0"
            )
        # 人脑式记忆：承诺/偏好/未完成/个人信息/纠正标记为重要，清理时长期保留
        if "important" not in cols:
            conn.execute(
                "ALTER TABLE conversation_digest ADD COLUMN important INTEGER DEFAULT 0"
            )
        if "salience" not in cols:
            conn.execute(
                "ALTER TABLE conversation_digest ADD COLUMN salience TEXT DEFAULT ''"
            )
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_conversation_digest_pending
               ON conversation_digest(summarized, id)"""
        )
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_conversation_digest_important
               ON conversation_digest(important, id)"""
        )

    def _migrate_behavior_patterns(self, conn: sqlite3.Connection):
        cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(behavior_patterns)").fetchall()
        }
        if not cols:
            return
        if "time_bucket" not in cols:
            conn.execute(
                "ALTER TABLE behavior_patterns ADD COLUMN time_bucket TEXT DEFAULT ''"
            )
        if "auto_suggest" not in cols:
            conn.execute(
                "ALTER TABLE behavior_patterns ADD COLUMN auto_suggest INTEGER DEFAULT 1"
            )
        if "weekday_mask" not in cols:
            conn.execute(
                "ALTER TABLE behavior_patterns ADD COLUMN weekday_mask INTEGER DEFAULT 127"
            )

    def _migrate_auto_facts(self, conn: sqlite3.Connection):
        """承诺生命周期：status（open/done/expired/superseded）+ due_date。"""
        cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(auto_facts)").fetchall()
        }
        if not cols:
            return
        if "status" not in cols:
            conn.execute(
                "ALTER TABLE auto_facts ADD COLUMN status TEXT DEFAULT 'open'"
            )
        if "due_date" not in cols:
            conn.execute("ALTER TABLE auto_facts ADD COLUMN due_date TEXT")
        if "source_role" not in cols:
            conn.execute(
                "ALTER TABLE auto_facts ADD COLUMN source_role TEXT DEFAULT 'user'"
            )
        if "source_type" not in cols:
            conn.execute(
                "ALTER TABLE auto_facts ADD COLUMN source_type TEXT DEFAULT 'explicit'"
            )
        # 老数据没有生命周期概念，统一视为未关闭
        conn.execute(
            "UPDATE auto_facts SET status = 'open' WHERE status IS NULL OR status = ''"
        )
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_auto_facts_status
               ON auto_facts(status, last_used)"""
        )

    def _ensure_memory_meta(self, conn: sqlite3.Connection):
        conn.execute(
            "CREATE TABLE IF NOT EXISTS memory_meta (key TEXT PRIMARY KEY, value TEXT)"
        )

    def _ensure_jarvis_tables(self, conn: sqlite3.Connection):
        """习惯 / 工作流 / UI 缓存 / 进化指标（桌面贾维斯，不含全屋）。"""
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS habit_patterns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pattern_type TEXT NOT NULL DEFAULT 'time_based',
                description TEXT NOT NULL,
                trigger_condition TEXT NOT NULL DEFAULT '{}',
                suggested_action TEXT NOT NULL,
                confidence REAL DEFAULT 0.5,
                auto_suggest INTEGER DEFAULT 1,
                hit_count INTEGER DEFAULT 0,
                last_suggested TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS workflows (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL DEFAULT '',
                steps TEXT NOT NULL DEFAULT '[]',
                trigger_hint TEXT DEFAULT '',
                confidence REAL DEFAULT 0.5,
                hit_count INTEGER DEFAULT 0,
                status TEXT DEFAULT 'draft',
                source TEXT DEFAULT 'auto',
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS ui_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cache_key TEXT NOT NULL UNIQUE,
                app_title TEXT DEFAULT '',
                target_text TEXT NOT NULL,
                method TEXT DEFAULT 'uia',
                x INTEGER DEFAULT 0,
                y INTEGER DEFAULT 0,
                control_type TEXT DEFAULT '',
                automation_id TEXT DEFAULT '',
                confidence REAL DEFAULT 0.6,
                hit_count INTEGER DEFAULT 0,
                fail_count INTEGER DEFAULT 0,
                last_used TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS learning_stats (
                date TEXT PRIMARY KEY,
                total_requests INTEGER DEFAULT 0,
                local_hit_rate REAL DEFAULT 0,
                cache_hit_rate REAL DEFAULT 0,
                api_calls INTEGER DEFAULT 0,
                avg_confidence REAL DEFAULT 0,
                vision_calls INTEGER DEFAULT 0,
                habits_inferred INTEGER DEFAULT 0,
                workflows_promoted INTEGER DEFAULT 0,
                updated_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS workflow_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signature TEXT NOT NULL UNIQUE,
                steps TEXT NOT NULL,
                trigger_hint TEXT DEFAULT '',
                frequency INTEGER DEFAULT 1,
                last_observed TEXT DEFAULT (datetime('now')),
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_habit_patterns_conf
                ON habit_patterns(confidence DESC, auto_suggest);
            CREATE INDEX IF NOT EXISTS idx_ui_cache_key ON ui_cache(cache_key);
            """
        )

    def _migrate_ui_cache(self, conn: sqlite3.Connection):
        cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(ui_cache)").fetchall()
        }
        if not cols:
            return
        if "rel_x" not in cols:
            conn.execute("ALTER TABLE ui_cache ADD COLUMN rel_x REAL DEFAULT 0")
        if "rel_y" not in cols:
            conn.execute("ALTER TABLE ui_cache ADD COLUMN rel_y REAL DEFAULT 0")

    def _migrate_error_corrections(self, conn: sqlite3.Connection):
        """error_corrections 表升级：支持 original→correction 配对学习。

        新增列：
        - original_func: 原失败工具名（用于跨会话匹配"这个工具失败过"）
        - correction_func: 修正后实际执行的工具名（哪条策略奏效）
        - error_signature: 失败模式签名（func + 关键 args 摘要，替代 user_text 作主键）
        """
        cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(error_corrections)").fetchall()
        }
        if "original_func" not in cols:
            conn.execute(
                "ALTER TABLE error_corrections ADD COLUMN original_func TEXT DEFAULT ''"
            )
        if "correction_func" not in cols:
            conn.execute(
                "ALTER TABLE error_corrections ADD COLUMN correction_func TEXT DEFAULT ''"
            )
        if "error_signature" not in cols:
            conn.execute(
                "ALTER TABLE error_corrections ADD COLUMN error_signature TEXT DEFAULT ''"
            )
        policy_columns = {
            "policy_state": "TEXT DEFAULT 'candidate'",
            "policy_version": "INTEGER DEFAULT 1",
            "applicability_json": "TEXT DEFAULT '{}'",
            "cause_class": "TEXT DEFAULT 'unknown'",
            "verified_count": "INTEGER DEFAULT 0",
            "adoption_count": "INTEGER DEFAULT 0",
            "adoption_success_count": "INTEGER DEFAULT 0",
            "adoption_fail_count": "INTEGER DEFAULT 0",
            "last_task_id": "TEXT DEFAULT ''",
            "last_adopted_task_id": "TEXT DEFAULT ''",
            "last_feedback_task_id": "TEXT DEFAULT ''",
            "tombstoned_at": "TEXT DEFAULT ''",
        }
        for name, declaration in policy_columns.items():
            if name not in cols:
                conn.execute(
                    f"ALTER TABLE error_corrections ADD COLUMN {name} {declaration}"
                )
        # 回填旧数据：tool_name 视为 original_func
        conn.execute(
            """UPDATE error_corrections
               SET original_func = tool_name
               WHERE original_func = '' AND tool_name IS NOT NULL"""
        )
        # 加索引：record_correction_outcome / get_error_corrections 均按
        # (original_func, error_signature) 查询，建联合索引加速
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_error_corrections_orig_sig
               ON error_corrections(original_func, error_signature)"""
        )
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_execution_policy_lookup
               ON error_corrections(policy_state, confidence, verified_count)"""
        )

    def _ensure_qs_events(self, conn: sqlite3.Connection):
        conn.execute(
            """CREATE TABLE IF NOT EXISTS qs_events (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               session_id TEXT NOT NULL DEFAULT 'default',
               turn_id TEXT,
               event_type TEXT NOT NULL,
               summary TEXT NOT NULL,
               payload TEXT NOT NULL DEFAULT '{}',
               created_at TEXT DEFAULT (datetime('now'))
            )"""
        )
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_qs_events_session
               ON qs_events(session_id, id DESC)"""
        )
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE name = 'qs_events_fts'"
        ).fetchone()
        if not row:
            conn.execute(
                """CREATE VIRTUAL TABLE qs_events_fts USING fts5(
                   summary,
                   payload,
                   event_type UNINDEXED,
                   source_id UNINDEXED,
                   tokenize='trigram')"""
            )

    @staticmethod
    def _format_knowledge_content(trigger: str | None, payload: str | None) -> str:
        t = (trigger or "").strip()
        p = (payload or "").strip()
        if t and p and t not in p:
            return f"{t} {p}"
        return p or t

    def _ensure_memory_fts(self, conn: sqlite3.Connection):
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE name = 'memory_fts'"
        ).fetchone()
        if not row:
            conn.execute(
                """CREATE VIRTUAL TABLE memory_fts USING fts5(
                   content, source_type UNINDEXED, source_id UNINDEXED,
                   tokenize='trigram')"""
            )
        count = conn.execute("SELECT COUNT(*) AS c FROM memory_fts").fetchone()["c"]
        if count == 0:
            self._backfill_memory_fts(conn)
        self._sync_knowledge_fts_in_conn(conn)

    def _sync_knowledge_fts_in_conn(self, conn: sqlite3.Connection) -> int:
        """恢复被旧版衰减异常误标为 stale 的 Action 知识，并确保 trigger+payload 与 FTS5 索引完全同步。"""
        import json

        updated = 0
        try:
            candidates = conn.execute(
                """SELECT id, trigger, payload, confidence FROM knowledge_entries
                   WHERE category = 'action'
                     AND status = 'stale'
                     AND COALESCE(confidence, 0.0) >= 0.2
                     AND COALESCE(trigger, '') != ''"""
            ).fetchall()
            revive_ids = []
            for candidate in candidates:
                try:
                    action = json.loads(candidate["payload"] or "")
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(action, dict):
                    continue
                tool = str(action.get("tool") or "").strip()
                if not tool:
                    continue
                corrections = conn.execute(
                    """SELECT success_count, fail_count, tombstoned_at
                       FROM error_corrections
                       WHERE original_func = ? OR tool_name = ?""",
                    (tool, tool),
                ).fetchall()
                has_bad_evidence = any(
                    str(correction["tombstoned_at"] or "").strip()
                    or (
                        int(correction["fail_count"] or 0) >= 2
                        and int(correction["fail_count"] or 0)
                        > int(correction["success_count"] or 0)
                    )
                    for correction in corrections
                )
                if not has_bad_evidence:
                    revive_ids.append(int(candidate["id"]))
            if revive_ids:
                placeholders = ",".join("?" for _ in revive_ids)
                conn.execute(
                    f"""UPDATE knowledge_entries
                        SET status = 'confirmed',
                            confidence = MAX(COALESCE(confidence, 0.65), 0.65)
                        WHERE id IN ({placeholders})""",
                    tuple(revive_ids),
                )
            self.sweep_orphan_fts_in_conn(conn)
            fts_map = {
                int(r["source_id"]): str(r["content"] or "")
                for r in conn.execute(
                    "SELECT source_id, content FROM memory_fts WHERE source_type = 'knowledge'"
                ).fetchall()
                if r["source_id"] is not None
            }
            krows = conn.execute(
                """SELECT id, trigger, payload FROM knowledge_entries
                   WHERE status = 'confirmed'"""
            ).fetchall()
            for r in krows:
                kid = int(r["id"])
                expected = self._format_knowledge_content(r["trigger"], r["payload"])
                if not expected:
                    continue
                cur_fts = fts_map.get(kid)
                trig = (r["trigger"] or "").strip()
                pay = (r["payload"] or "").strip()
                if cur_fts is None or (trig and trig not in cur_fts) or (pay and pay not in cur_fts):
                    self._fts_insert(conn, expected, "knowledge", kid)
                    updated += 1
        except sqlite3.OperationalError:
            pass
        return updated

    def _backfill_memory_fts(self, conn: sqlite3.Connection):
        """首次建 FTS 时，从已有表回填索引（knowledge_entries 同时索引 trigger + payload）"""
        sources = [
            ("auto_fact", "auto_facts", "fact"),
            ("consolidated", "consolidated_memory", "summary"),
            (
                "knowledge",
                "knowledge_entries",
                "CASE WHEN COALESCE(trigger, '') != '' AND INSTR(COALESCE(payload, ''), trigger) = 0 "
                "THEN trigger || ' ' || COALESCE(payload, '') "
                "ELSE COALESCE(payload, trigger, '') END",
            ),
        ]
        for source_type, table, col in sources:
            extra = ""
            if table == "knowledge_entries":
                extra = " WHERE status = 'confirmed'"
            elif table == "auto_facts":
                extra = f" WHERE {_FACT_INJECTION_SQL}"
            rows = conn.execute(
                f"SELECT id, {col} AS content FROM {table}{extra}"
            ).fetchall()
            for row in rows:
                content = (row["content"] or "").strip()
                if content:
                    self._fts_insert(conn, content, source_type, int(row["id"]))

    @staticmethod
    def _fts_escape(query: str) -> str:
        """trigram 分词器：直接 MATCH 子串即可"""
        return (query or "").replace('"', '""').strip()

    def _fts_insert(
        self,
        conn: sqlite3.Connection,
        content: str,
        source_type: str,
        source_id: int,
    ):
        text = (content or "").strip()
        if not text:
            return
        conn.execute(
            "DELETE FROM memory_fts WHERE source_type = ? AND source_id = ?",
            (source_type, source_id),
        )
        conn.execute(
            "INSERT INTO memory_fts(content, source_type, source_id) VALUES (?, ?, ?)",
            (text, source_type, source_id),
        )

    def index_memory(self, content: str, source_type: str, source_id: int):
        """写入/更新全文索引"""
        with self.connect() as conn:
            self._fts_insert(conn, content, source_type, source_id)
            conn.commit()

    @staticmethod
    def _fts_delete(conn: sqlite3.Connection, source_type: str, source_id: int):
        """在调用方给定的连接/事务内删除 FTS 索引（不提交、不另开连接）。"""
        conn.execute(
            "DELETE FROM memory_fts WHERE source_type = ? AND source_id = ?",
            (source_type, int(source_id)),
        )

    @staticmethod
    def sweep_orphan_fts_in_conn(conn: sqlite3.Connection) -> int:
        """在同一事务连接内清理主表已删除或已失效（superseded/deleted）的 FTS 残留行。"""
        deleted = 0
        queries = (
            "DELETE FROM memory_fts WHERE source_type = 'auto_fact' AND source_id NOT IN (SELECT id FROM auto_facts WHERE COALESCE(status, 'open') NOT IN ('superseded', 'candidate'))",
            "DELETE FROM memory_fts WHERE source_type = 'consolidated' AND source_id NOT IN (SELECT id FROM consolidated_memory)",
            "DELETE FROM memory_fts WHERE source_type = 'knowledge' AND source_id NOT IN (SELECT id FROM knowledge_entries WHERE status = 'confirmed')",
            "DELETE FROM memory_fts WHERE source_type = 'entity_edge' AND source_id NOT IN (SELECT id FROM entity_edges WHERE status = 'active')",
        )
        for q in queries:
            try:
                cur = conn.execute(q)
                deleted += max(cur.rowcount, 0)
            except sqlite3.OperationalError:
                pass
        return deleted

    def unindex_memory(
        self,
        source_type: str,
        source_id: int,
        conn: "sqlite3.Connection | None" = None,
    ):
        """删除全文索引。

        传入 ``conn`` 时复用调用方的连接与事务（由调用方负责提交），
        避免持有写事务期间再开独立连接写 FTS 导致 WAL 写锁互等。
        """
        if conn is not None:
            self._fts_delete(conn, source_type, source_id)
            return
        own = self.connect()
        try:
            with own:
                self._fts_delete(own, source_type, source_id)
        finally:
            own.close()

    # ─── 静态指令（语音指令记忆训练流）──────────────────────────────

    def _ensure_static_replay(self, conn: sqlite3.Connection):
        """workflows 表扩展静态指令字段 + workflow_fts 全文检索虚表。"""
        cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(workflows)").fetchall()
        }
        if "match_keys" not in cols:
            conn.execute("ALTER TABLE workflows ADD COLUMN match_keys TEXT DEFAULT ''")
        if "enabled" not in cols:
            conn.execute("ALTER TABLE workflows ADD COLUMN enabled INTEGER DEFAULT 1")
        if "fail_count" not in cols:
            conn.execute("ALTER TABLE workflows ADD COLUMN fail_count INTEGER DEFAULT 0")
        if "last_replay_fail" not in cols:
            conn.execute("ALTER TABLE workflows ADD COLUMN last_replay_fail TEXT")
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE name = 'workflow_fts'"
        ).fetchone()
        if not row:
            conn.execute(
                """CREATE VIRTUAL TABLE workflow_fts USING fts5(
                   content, wf_id UNINDEXED, tokenize='trigram')"""
            )
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM workflow_fts"
        ).fetchone()["c"]
        if count == 0:
            self._backfill_workflow_fts(conn)

    def _backfill_workflow_fts(self, conn: sqlite3.Connection):
        """首次建 workflow_fts 时，从已有 source='static' 的 workflows 回填。"""
        rows = conn.execute(
            """SELECT id, name, match_keys, trigger_hint
               FROM workflows WHERE source = 'static'"""
        ).fetchall()
        for r in rows:
            content = " ".join(
                x for x in (r["name"], r["match_keys"], r["trigger_hint"]) if x
            )
            if content.strip():
                self._workflow_fts_insert(conn, content, int(r["id"]))

    @staticmethod
    def _workflow_fts_insert(
        conn: sqlite3.Connection,
        content: str,
        wf_id: int,
    ):
        text = (content or "").strip()
        if not text:
            return
        conn.execute("DELETE FROM workflow_fts WHERE wf_id = ?", (wf_id,))
        conn.execute(
            "INSERT INTO workflow_fts(content, wf_id) VALUES (?, ?)",
            (text, wf_id),
        )

    def index_workflow_fts(self, content: str, wf_id: int):
        """写入/更新静态指令全文索引。"""
        with self.connect() as conn:
            self._workflow_fts_insert(conn, content, int(wf_id))
            conn.commit()

    _JIEBA_INITIALIZED = False
    _STOPWORDS = frozenset({
        "你还记得", "你记得", "还记得", "记得", "请问", "告诉我", "帮我看看", "帮我查查",
        "帮我", "麻烦", "请", "能不能", "可以", "可不可以", "一下", "来着",
        "之前", "以前", "上次", "刚才", "刚刚", "最近", "平时", "通常", "一般",
        "那个", "这个", "那些", "这些", "什么", "哪些", "哪个", "怎么", "如何",
        "多少", "哪里", "在哪", "是谁", "叫什么", "为什么", "是不是", "有没有",
        "吗", "呢", "呀", "啊", "吧", "嘛", "哈", "哦", "了", "的", "地", "得",
        "着", "过", "把", "被", "给", "让", "向", "往", "从", "到", "在", "和",
        "与", "跟", "或", "而且", "并且", "但是", "不过", "如果", "因为", "所以",
        "虽然", "就是", "还是", "或者", "以及", "关于", "对于", "我的", "我是",
        "我在", "我", "你的", "你是", "你", "咱们", "我们", "他们", "她们", "它们",
        "自己", "大家", "用户", "主人", "助手", "是", "有", "没有", "去", "来", "做", "干", "弄", "搞",
        "看", "看看", "查", "查查", "说", "问", "想", "要", "会", "能", "应该",
        "需要", "时候", "地方", "东西", "事情", "情况", "方面", "问题",
        "今天", "明天", "昨天", "后天", "前天", "上午", "下午", "晚上", "早上", "夜里",
        "进行", "多少度", "几度", "多少号", "几号", "如何", "怎样", "会发生", "发生", "已经",
    })
    _ACTION_VERBS = (
        "打开", "启动", "运行", "关闭", "关掉", "退出", "停止", "开始", "托管",
        "继续", "暂停", "播放", "重开", "重启", "切换", "搜索", "查询", "查看",
        "发送", "下载", "安装", "卸载", "更新", "修改", "设置", "配置", "创建",
        "删除", "清理", "备份", "恢复", "连接", "断开", "开启", "禁用", "启用",
        "浏览", "阅读", "喜欢", "爱喝", "爱吃", "讨厌", "偏好", "习惯", "擅长",
        "使用", "调用", "执行", "玩", "听", "写", "用", "结束",
    )
    _DOMAIN_DICT = (
        "植物大战僵尸", "微信开发者工具", "斗地主", "抖音", "网易云音乐", "QQ音乐", "QQ机器人",
        "微信", "企业微信", "飞书", "钉钉", "英雄联盟", "原神", "黑神话悟空",
        "直播伴侣", "深色主题", "浅色主题", "夜间模式", "白天模式", "向量检索",
        "语义检索", "知识库", "智能体", "多模态", "强化学习", "上下文窗口",
        "注意力机制", "长上下文", "工具调用", "记忆系统",
    )
    _PROTECTED_WORDS = (
        "植物人", "天气预报", "康复训练", "打牌软件", "打牌游戏", "量子力学", "薛定谔", "毅力号",
    )
    _GENERIC_WORDS = frozenset({
        "软件", "应用", "程序", "游戏", "工具", "系统", "服务", "功能", "模块",
        "方法", "方式", "办法", "步骤", "流程", "问题", "情况", "事情", "东西",
        "内容", "信息", "记录", "数据", "结果", "时间", "时候", "地方", "位置",
        "路径", "目录", "屏幕", "画面", "视频", "文件", "代码", "脚本", "模型",
        "控制", "配置", "设置", "密码", "规则", "策略", "经验", "方案", "方程",
        "求解", "发射", "训练", "天气", "预报", "自动", "平时",
        "最多", "几条", "旧配置", "会儿", "一会儿",
    })
    _SYNONYM_MAP: dict[str, tuple[str, ...]] = {
        "打牌": ("斗地主",),
        "打牌软件": ("斗地主",),
        "打牌游戏": ("斗地主",),
        "扑克": ("斗地主",),
        "扑克牌": ("斗地主",),
        "棋牌": ("斗地主",),
        "房号": ("房间号", "直播间"),
        "直播伴侣": ("直播",),
        "打僵尸": ("植物大战僵尸",),
    }
    _VERB_SYNONYM_MAP: dict[str, tuple[str, ...]] = {
        "关掉": ("关闭",),
        "关闭": ("关掉",),
        "退了": ("退出", "关闭"),
        "结束": ("停止",),
        "暂停": ("停止",),
    }

    @classmethod
    def _init_jieba(cls):
        if cls._JIEBA_INITIALIZED:
            return
        try:
            import jieba
            jieba.setLogLevel(20)
            for w in cls._DOMAIN_DICT:
                jieba.add_word(w, freq=100000)
            for w in cls._PROTECTED_WORDS:
                jieba.add_word(w, freq=100000)
            for v in cls._ACTION_VERBS:
                if len(v) >= 2:
                    jieba.add_word(v, freq=5000)
            cls._JIEBA_INITIALIZED = True
        except Exception:
            pass

    @classmethod
    def preprocess_query(cls, query: str) -> dict:
        """统一自然语言查询预处理（五级关键词分级）：
        1. proper_nouns: 专有名词（领域专名、书名号/引号词）
        2. tech_entities: 技术实体（英文/数字代号）
        3. core_entities: 核心实体词（proper_nouns + tech_entities + 具体名词实体 + 同义标准实体）
        4. normal_keywords: 普通关键词（动作动词 + 通用泛词）
        5. ngram_subterms: 复合词细分弱匹配子词（不跨复合词边界切分）
        """
        import re as _re

        raw_query = (query or "").strip()
        if not raw_query:
            return {
                "raw_query": "",
                "proper_nouns": [],
                "tech_entities": [],
                "core_entities": [],
                "normal_keywords": [],
                "ngram_subterms": [],
                "entities": [],
                "verbs": [],
                "keywords": [],
                "fts_or_query": "",
                "alpha_tokens": [],
            }

        cls._init_jieba()
        proper_nouns: list[str] = []
        tech_entities: list[str] = []
        core_entities: list[str] = []
        normal_keywords: list[str] = []
        ngram_subterms: list[str] = []
        verbs: list[str] = []
        alpha_tokens: list[str] = []

        # 1. 提取书名号/引号中的专有名词与实体
        for m in _re.finditer(r"[《“\"'「『]([^》”\"'」』]{2,24})[》”\"'」』]", raw_query):
            ent = m.group(1).strip()
            if ent and ent not in proper_nouns:
                proper_nouns.append(ent)
                if ent not in core_entities:
                    core_entities.append(ent)

        # 2. 提取英文/数字/技术实体 token（如 sqlite-vec, RRF, BM25, XYZ, arXiv, Clash, Verge, OBS）
        for m in _re.finditer(r"[A-Za-z][A-Za-z0-9_.\-+]{1,24}|[A-Za-z0-9_.\-+]*[A-Za-z][A-Za-z0-9_.\-+]*", raw_query):
            tok = m.group(0).strip(".-+_")
            if len(tok) >= 2 and tok not in alpha_tokens:
                alpha_tokens.append(tok)
                if tok not in tech_entities:
                    tech_entities.append(tok)
                if tok not in core_entities:
                    core_entities.append(tok)

        # 3. 匹配领域复合词与已知专有名词
        for dw in cls._DOMAIN_DICT:
            if dw in raw_query and dw not in proper_nouns:
                proper_nouns.append(dw)
                if dw not in core_entities:
                    core_entities.append(dw)

        # 3b. 口语同义词扩展（如「打牌软件」->「斗地主」，「房号」->「房间号」）
        for syn_key, syn_targets in cls._SYNONYM_MAP.items():
            if syn_key in raw_query:
                for target in syn_targets:
                    if target in cls._DOMAIN_DICT and target not in proper_nouns:
                        proper_nouns.append(target)
                    if target not in core_entities:
                        core_entities.append(target)
        if "直播间" in raw_query and ("多少号" in raw_query or "几号" in raw_query or "号" in raw_query):
            if "房间号" not in core_entities:
                core_entities.append("房间号")

        # 4. 提取动作动词与偏好动词（及同义动词）
        for v in cls._ACTION_VERBS:
            if v in raw_query and v not in verbs:
                verbs.append(v)
                for sv in cls._VERB_SYNONYM_MAP.get(v, ()):
                    if sv not in verbs:
                        verbs.append(sv)
        for v_key, v_syns in cls._VERB_SYNONYM_MAP.items():
            if v_key in raw_query:
                for sv in v_syns:
                    if sv not in verbs:
                        verbs.append(sv)

        # 5. 先剔除口语寒暄前缀/疑问助词/时间词，再按动词边界切开防止产生跨边界伪词
        cleaned = _re.sub(
            r"你还记不记得|还记不记得|记不记得|你还记得|你记得|还记得|记得|请问|告诉我|帮我看看|帮我查查|帮我|"
            r"麻烦|能不能|可不可以|可以|"
            r"什么|哪些|哪个|怎么|如何|怎样|多少度|几度|多少号|几号|多少|哪里|在哪|是谁|叫什么|为什么|是不是|有没有|"
            r"今天|明天|昨天|后天|前天|上午|下午|晚上|早上|夜里|进行|会发生|发生|已经|一会儿|会儿|"
            r"来着|一下|之前|以前|上次|刚才|刚刚|平时|通常|一般|"
            r"吗|呢|呀|啊|吧|嘛|哈|哦|"
            r"[，,。！？!?、：:“”\"'（）()《》「」『』\s]+",
            " ",
            raw_query,
        )

        verb_pat = "|".join(sorted((_re.escape(v) for v in cls._ACTION_VERBS if len(v) >= 2), key=len, reverse=True))
        if verb_pat:
            cleaned = _re.sub(f"({verb_pat})", r" \1 ", cleaned)

        segments: list[str] = []
        for seg in cleaned.split():
            seg = _re.sub(r"^(?:我们是|咱们是|我的|我是|我在|你的|你是|我们|咱们|你们|他们|她们|它们|我|你|把|给|让|在|从|到|对|跟|和)+", "", seg).strip()
            seg = _re.sub(r"(?:的|了|着|过|吗|呢|吧|啊|呀)+$", "", seg).strip()
            if seg and seg not in cls._STOPWORDS:
                segments.append(seg)

        # 6. 结巴分词 + 词级分级（具体核心实体 vs 普通泛词 vs 动作词）
        protected_present = [pw for pw in cls._PROTECTED_WORDS if pw in raw_query]
        alpha_lower_set = {t.lower() for t in alpha_tokens}
        try:
            import jieba
            for seg in segments:
                for w in jieba.lcut(seg, cut_all=False):
                    w = w.strip()
                    if len(w) < 2 or w in cls._STOPWORDS:
                        continue
                    # 防止带连字符的英文技术实体（如 fast-tts）被结巴拆成 fast / tts 混入核心词
                    if _re.fullmatch(r"[A-Za-z0-9_.]+", w) and w.lower() not in alpha_lower_set:
                        continue
                    # 防止受保护专名（如「植物人」）被切出前缀（「植物」）污染其它复合词
                    if any(w != pw and w in pw for pw in protected_present if pw == "植物人"):
                        continue
                    if w in cls._ACTION_VERBS:
                        if w not in verbs:
                            verbs.append(w)
                        if w not in normal_keywords:
                            normal_keywords.append(w)
                    elif w in cls._GENERIC_WORDS:
                        if w not in normal_keywords:
                            normal_keywords.append(w)
                    else:
                        if w not in core_entities:
                            core_entities.append(w)
                # 若整段本身是 2~6 字紧凑中文名词短语且不含泛词后缀，补充进对应层级
                if 2 <= len(seg) <= 6 and _re.fullmatch(r"[\u4e00-\u9fff]+", seg) and seg not in cls._ACTION_VERBS and seg not in cls._STOPWORDS:
                    if seg in cls._GENERIC_WORDS:
                        if seg not in normal_keywords:
                            normal_keywords.append(seg)
                    elif not any(seg.endswith(gw) and len(seg) > len(gw) for gw in cls._GENERIC_WORDS):
                        if seg not in core_entities:
                            core_entities.append(seg)
        except Exception:
            for seg in segments:
                if len(seg) >= 2 and seg not in cls._STOPWORDS:
                    if seg in cls._GENERIC_WORDS:
                        if seg not in normal_keywords:
                            normal_keywords.append(seg)
                    elif seg not in core_entities:
                        core_entities.append(seg)

        for v in verbs:
            if len(v) >= 2 and v not in normal_keywords:
                normal_keywords.append(v)

        # 7. 对 >=4 字的复合词，仅通过结巴分词或首尾语义块生成弱匹配子词，禁止跨词边界乱切（如「天气预报」不切出「气预」，「植物人」绝不切出「植物」）
        for ent in list(core_entities) + list(normal_keywords):
            if ent == "植物人" or not _re.fullmatch(r"[\u4e00-\u9fff]{4,8}", ent):
                continue
            # 优先使用前 2 字与后 2 字（双音节构词边界），禁止中间滑窗产生「气预」等伪词
            candidate_subs = [ent[:2], ent[-2:]]
            if len(ent) >= 5:
                candidate_subs.extend([ent[:3], ent[-3:]])
            for sub in candidate_subs:
                if (
                    len(sub) >= 2
                    and sub not in cls._STOPWORDS
                    and sub not in cls._ACTION_VERBS
                    and sub not in core_entities
                    and sub not in normal_keywords
                    and sub not in ngram_subterms
                ):
                    if "植物人" in raw_query and sub == "植物":
                        continue
                    ngram_subterms.append(sub)

        # 8. 汇总排序：核心实体词 -> 普通关键词 -> n-gram 弱匹配词
        keywords: list[str] = []
        for item in core_entities + normal_keywords + ngram_subterms:
            if item and len(item) >= 2 and item not in cls._STOPWORDS and item not in keywords:
                keywords.append(item)

        # 9. 生成适合 FTS5 trigram 的 OR 查询（优先使用核心实体与 >=3 字普通关键词）
        fts_terms: list[str] = []
        for kw in core_entities + normal_keywords:
            if len(kw) >= 3 and kw not in fts_terms:
                fts_terms.append(cls._fts_escape(kw))
        if not fts_terms:
            for kw in ngram_subterms:
                if len(kw) >= 3 and kw not in fts_terms:
                    fts_terms.append(cls._fts_escape(kw))
        if not fts_terms and len(raw_query) >= 3:
            fts_terms.append(cls._fts_escape(raw_query))
        fts_or_query = " OR ".join(fts_terms[:8])

        return {
            "raw_query": raw_query,
            "proper_nouns": proper_nouns,
            "tech_entities": tech_entities,
            "core_entities": core_entities,
            "normal_keywords": normal_keywords,
            "ngram_subterms": ngram_subterms,
            "entities": core_entities,
            "verbs": verbs,
            "keywords": keywords[:14],
            "fts_or_query": fts_or_query,
            "alpha_tokens": alpha_tokens,
        }

    @classmethod
    def _extract_query_terms(cls, query: str) -> list[str]:
        """从自然语言问句中提取核心检索词（结巴分词 + 实体/动词提取 + 停用词过滤）。"""
        info = cls.preprocess_query(query)
        return info["keywords"][:10]

    @staticmethod
    def _parse_ts(ts_val) -> float | None:
        if ts_val is None:
            return None
        if isinstance(ts_val, (int, float)):
            return float(ts_val)
        s = str(ts_val).strip()
        if not s:
            return None
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
        except Exception:
            return None

    def _hydrate_fts_row(self, conn: sqlite3.Connection, source_type: str, source_id: int) -> tuple[float, str | None]:
        """为 FTS 命中条目补齐真实 confidence 与 updated_at。"""
        try:
            if source_type == "auto_fact":
                row = conn.execute(
                    "SELECT COALESCE(confidence, 0.75) AS c, COALESCE(last_used, created_at) AS u FROM auto_facts WHERE id = ?",
                    (source_id,),
                ).fetchone()
            elif source_type == "knowledge":
                row = conn.execute(
                    "SELECT COALESCE(confidence, 0.75) AS c, COALESCE(last_used, created_at) AS u FROM knowledge_entries WHERE id = ?",
                    (source_id,),
                ).fetchone()
            elif source_type == "consolidated":
                row = conn.execute(
                    "SELECT COALESCE(importance, 0.75) AS c, created_at AS u FROM consolidated_memory WHERE id = ?",
                    (source_id,),
                ).fetchone()
            elif source_type == "entity_edge":
                row = conn.execute(
                    "SELECT COALESCE(confidence, 0.8) AS c, COALESCE(updated_at, created_at) AS u FROM entity_edges WHERE id = ?",
                    (source_id,),
                ).fetchone()
            else:
                row = None
            if row:
                return float(row["c"] or 0.75), row["u"]
        except Exception:
            pass
        return 0.75, None

    def _search_memory_like(
        self,
        conn: sqlite3.Connection,
        query: str,
        limit: int,
        source_types: list[str] | None,
        include_closed: bool = False,
    ) -> list[dict]:
        pattern = f"%{query}%"
        fact_extra = (
            " AND status != 'superseded' AND COALESCE(status, 'open') != 'candidate'"
            if include_closed
            else f" AND {_FACT_INJECTION_SQL}"
        )
        sources = [
            ("auto_fact", "auto_facts", "COALESCE(fact, '')", "COALESCE(fact, '') LIKE ?", 1, "confidence", "COALESCE(last_used, created_at)", "fact", fact_extra),
            ("consolidated", "consolidated_memory", "COALESCE(summary, '')", "COALESCE(summary, '') LIKE ?", 1, "importance", "created_at", "knowledge", ""),
            (
                "knowledge",
                "knowledge_entries",
                "CASE WHEN COALESCE(trigger, '') != '' AND INSTR(COALESCE(payload, ''), trigger) = 0 "
                "THEN trigger || ' ' || COALESCE(payload, '') "
                "ELSE COALESCE(payload, trigger, '') END",
                "(COALESCE(trigger, '') LIKE ? OR COALESCE(payload, '') LIKE ?)",
                2,
                "confidence",
                "COALESCE(last_used, created_at)",
                "knowledge",
                " AND status = 'confirmed'",
            ),
        ]
        if source_types:
            sources = [s for s in sources if s[0] in source_types]

        results: list[dict] = []
        seen: set[str] = set()
        for source_type, table, col_expr, where_clause, pat_count, conf_col, ts_col, pool, extra in sources:
            rows = conn.execute(
                f"""SELECT id AS source_id, {col_expr} AS content,
                           COALESCE({conf_col}, 0.7) AS confidence,
                           {ts_col} AS updated_at
                    FROM {table} WHERE {where_clause}{extra}
                    ORDER BY COALESCE({conf_col}, 0.7) DESC, id DESC
                    LIMIT ?""",
                (*([pattern] * pat_count), limit),
            ).fetchall()
            for r in rows:
                content = r["content"]
                if content in seen:
                    continue
                seen.add(content)
                updated_at = r["updated_at"]
                results.append(
                    {
                        "id": f"{source_type}:{r['source_id']}",
                        "content": content,
                        "text": content,
                        "source_type": source_type,
                        "source_id": r["source_id"],
                        "confidence": float(r["confidence"] or 0.7),
                        "updated_at": updated_at,
                        "created_at_ts": self._parse_ts(updated_at),
                        "pool": pool,
                        "rank": 0.0,
                    }
                )
        if not source_types or "entity_edge" in source_types:
            try:
                erows = conn.execute(
                    """SELECT id AS source_id,
                              (subject || ' 的' || relation || '：' || object) AS content,
                              COALESCE(confidence, 0.8) AS confidence,
                              COALESCE(updated_at, created_at) AS updated_at
                       FROM entity_edges
                       WHERE status = 'active'
                         AND (subject LIKE ? OR relation LIKE ? OR object LIKE ?)
                       LIMIT ?""",
                    (pattern, pattern, pattern, limit),
                ).fetchall()
                for r in erows:
                    content = r["content"]
                    if content and content not in seen:
                        seen.add(content)
                        updated_at = r["updated_at"]
                        results.append(
                            {
                                "id": f"entity_edge:{r['source_id']}",
                                "content": content,
                                "text": content,
                                "source_type": "entity_edge",
                                "source_id": r["source_id"],
                                "confidence": float(r["confidence"] or 0.8),
                                "updated_at": updated_at,
                                "created_at_ts": self._parse_ts(updated_at),
                                "pool": "fact",
                                "rank": 0.0,
                            }
                        )
            except sqlite3.OperationalError:
                pass
        return results[:limit]

    def _run_fts_query(
        self,
        conn: sqlite3.Connection,
        fts_expr: str,
        limit: int,
        source_types: list[str] | None,
        include_closed: bool,
    ) -> list[dict]:
        if not fts_expr:
            return []
        type_filter = ""
        params: list = [fts_expr]
        if source_types:
            placeholders = ",".join("?" * len(source_types))
            type_filter = f" AND source_type IN ({placeholders})"
            params.extend(source_types)
        if include_closed:
            fact_filter = (
                " AND (source_type != 'auto_fact' OR source_id IN "
                "(SELECT id FROM auto_facts WHERE status != 'superseded' AND COALESCE(status, 'open') != 'candidate'))"
            )
        else:
            fact_filter = (
                " AND (source_type != 'auto_fact' OR source_id IN "
                f"(SELECT id FROM auto_facts WHERE {_FACT_INJECTION_SQL}))"
            )
        validity_filter = (
            " AND (source_type != 'consolidated' OR source_id IN (SELECT id FROM consolidated_memory))"
            " AND (source_type != 'knowledge' OR source_id IN (SELECT id FROM knowledge_entries WHERE status = 'confirmed'))"
            " AND (source_type != 'entity_edge' OR source_id IN (SELECT id FROM entity_edges WHERE status = 'active'))"
        )
        params.append(limit)
        out: list[dict] = []
        try:
            rows = conn.execute(
                f"""SELECT content, source_type, source_id, bm25(memory_fts) AS rank
                    FROM memory_fts
                    WHERE content MATCH ?{type_filter}{fact_filter}{validity_filter}
                    ORDER BY rank
                    LIMIT ?""",
                params,
            ).fetchall()
            for r in rows:
                st = str(r["source_type"] or "")
                sid = int(r["source_id"])
                pool = "fact" if st in ("auto_fact", "entity_edge") else "knowledge"
                conf, updated_at = self._hydrate_fts_row(conn, st, sid)
                content = r["content"]
                out.append(
                    {
                        "id": f"{st}:{sid}",
                        "content": content,
                        "text": content,
                        "source_type": st,
                        "source_id": sid,
                        "confidence": conf,
                        "updated_at": updated_at,
                        "created_at_ts": self._parse_ts(updated_at),
                        "pool": pool,
                        "rank": float(r["rank"]),
                    }
                )
        except sqlite3.OperationalError:
            pass
        return out

    def search_memory(
        self,
        query: str,
        limit: int = 5,
        source_types: list[str] | None = None,
        *,
        include_closed: bool = False,
    ) -> list[dict]:
        """多通道检索（原句精确检索 + FTS5 trigram / OR BM25 + 关键词覆盖度检索，统一去重排序）。

        include_closed=False（默认）只召回可注入的 auto_facts：
        open 且 confidence>=0.3，或关闭 7 天内的 done；显式检索传 True 可取到
        done/expired 条目。superseded 是已被取代的旧信息，两种模式都不返回。
        """
        q = (query or "").strip()
        if not q or len(q) < 2:
            return []

        prep = self.preprocess_query(q)
        keywords = prep["keywords"]
        proper_nouns = set(prep.get("proper_nouns") or ())
        tech_entities = set(prep.get("tech_entities") or ())
        core_entities = set(prep.get("core_entities") or prep["entities"])
        normal_keywords = set(prep.get("normal_keywords") or ())
        ngram_subterms = set(prep.get("ngram_subterms") or ())
        entities = core_entities
        alpha_tokens = [t.lower() for t in prep["alpha_tokens"]]
        fts_or_query = prep["fts_or_query"]

        candidates: dict[str, dict] = {}
        channel_scores: dict[str, float] = {}

        def _merge_hit(hit: dict, bonus: float, channel: str):
            cid = hit.get("id") or hit.get("content")
            if not cid:
                return
            if cid not in candidates:
                item = dict(hit)
                item["match_channel"] = channel
                candidates[cid] = item
                channel_scores[cid] = bonus
            else:
                channel_scores[cid] = channel_scores.get(cid, 0.0) + bonus * 0.5
                if hit.get("rank", 0.0) < candidates[cid].get("rank", 0.0):
                    candidates[cid]["rank"] = hit["rank"]

        with self.connect() as conn:
            # 通道 1：原始查询精确 LIKE + 原始长句 FTS5 匹配
            exact_likes = self._search_memory_like(
                conn, q, max(limit * 2, 10), source_types, include_closed=include_closed
            )
            for idx, h in enumerate(exact_likes):
                _merge_hit(h, 3.0 - idx * 0.05, "exact")

            if len(q) >= 3:
                raw_fts = self._run_fts_query(
                    conn, self._fts_escape(q), max(limit * 2, 10), source_types, include_closed
                )
                for idx, h in enumerate(raw_fts):
                    bm25_bonus = min(1.5, max(0.2, -float(h.get("rank") or 0.0) * 0.1))
                    _merge_hit(h, 2.5 + bm25_bonus - idx * 0.05, "fts")

            # 判断是否为自然语言问句/口语指令/多词短语
            import re as _re
            is_user_query_or_command = (
                bool(prep["verbs"])
                or len(q) <= 6
                or bool(
                    _re.search(
                        r"我|你|咱们|记得|请问|帮我|什么|哪些|哪个|怎么|如何|多少|哪里|在哪|是谁|为什么|有没有|或者|和|与|[，,。！？!?\s]",
                        q,
                    )
                )
            )
            is_natural_or_missed = len(candidates) == 0 or is_user_query_or_command

            # 通道 2：FTS5 OR 关键词召回（BM25 排序）
            if is_natural_or_missed and fts_or_query and fts_or_query != self._fts_escape(q):
                or_fts = self._run_fts_query(
                    conn, fts_or_query, max(limit * 3, 15), source_types, include_closed
                )
                for idx, h in enumerate(or_fts):
                    bm25_bonus = min(1.2, max(0.1, -float(h.get("rank") or 0.0) * 0.08))
                    _merge_hit(h, 1.5 + bm25_bonus - idx * 0.03, "fts_or")

            # 通道 3：分级关键词召回 + 覆盖度计分（自然语言长句或未满额时补充）
            if is_natural_or_missed and keywords and (len(q) >= 4 or len(candidates) < limit):
                per_kw_limit = max(limit * 2, 8)
                for kw in keywords[:10]:
                    if kw == q:
                        continue
                    if not is_user_query_or_command and len(kw) < 3 and kw not in self._DOMAIN_DICT and kw.lower() not in alpha_tokens:
                        continue
                    if kw in proper_nouns or kw in tech_entities:
                        kw_weight = 1.35
                    elif kw in core_entities:
                        kw_weight = 1.15
                    elif kw in normal_keywords:
                        kw_weight = 0.65
                    else:
                        kw_weight = 0.35
                    sub_hits = self._search_memory_like(
                        conn, kw, per_kw_limit, source_types, include_closed=include_closed
                    )
                    for sh in sub_hits:
                        _merge_hit(sh, kw_weight, "keyword")

            # 统一动态相关性资格判断与分层重排
            q_lower = q.lower()
            ranked_list: list[tuple[float, dict]] = []
            for cid, item in candidates.items():
                content_lower = (item.get("content") or "").lower()
                masked_content = content_lower
                # 复合专名边界保护：若候选含「植物大战僵尸」等 >=4 字领域专名，而查询并未提及该专名，
                # 则屏蔽该专名内部的子词误匹配（防止「植物人」通过「植物」错误命中「植物大战僵尸」）
                for dw in self._DOMAIN_DICT:
                    dw_low = dw.lower()
                    if len(dw) >= 4 and dw_low in masked_content and dw_low not in q_lower and dw not in core_entities:
                        masked_content = masked_content.replace(dw_low, " ")

                matched_proper = [kw for kw in proper_nouns if kw.lower() in masked_content]
                matched_tech = [kw for kw in tech_entities if kw.lower() in masked_content]
                matched_core = [kw for kw in core_entities if kw.lower() in masked_content]
                matched_normal = [kw for kw in normal_keywords if kw.lower() in masked_content]
                matched_ngram = [kw for kw in ngram_subterms if kw.lower() in masked_content]
                matched_kw = [kw for kw in keywords if kw.lower() in masked_content]

                ch = item.get("match_channel")
                if ch not in ("exact", "fts"):
                    cjk_len = sum(1 for c in q if "\u4e00" <= c <= "\u9fff")
                    # 1. 若查询含明确英文/技术实体，且候选既未命中技术实体也未命中专有名词
                    if alpha_tokens and not matched_tech and not matched_proper:
                        if cjk_len <= 8 or len(matched_kw) < 2:
                            continue
                    # 2. 若查询提取出了核心实体词（如「北京+天气预报」「植物人+康复训练」「薛定谔」）：
                    #    - 非精确命中必须至少命中 1 个核心实体
                    #    - 若无专有名词/技术实体/动作动词支撑，且查询含 >=2 个独立核心实体（如「北京」+「天气预报」），
                    #      则不得仅凭单边单一实体（如仅命中「北京」或仅命中「天气预报」）进入长句候选
                    if core_entities and len(q) >= 4:
                        if not matched_core:
                            continue
                        if (
                            not matched_proper
                            and not matched_tech
                            and not prep["verbs"]
                            and len(core_entities) >= 2
                            and len(q) >= 7
                            and (len(matched_core) + len(matched_normal)) < 2
                        ):
                            continue
                        if ("会发生" in q or "发生什么" in q) and len(core_entities) >= 2 and len(matched_core) < len(core_entities):
                            continue
                    # 3. 若查询无核心实体（仅有普通关键词或 n-gram 子词）：
                    #    - n-gram 弱子词不能单独成候选
                    #    - 长句（>=6字）不能仅靠单个宽泛普通词进入候选
                    if not matched_core:
                        if not matched_normal and len(q) >= 4:
                            continue
                        if len(q) >= 6 and len(matched_normal) < 2:
                            continue

                coverage_bonus = (
                    sum(0.75 for _ in matched_proper)
                    + sum(0.75 for kw in matched_tech if kw not in proper_nouns)
                    + sum(0.60 for kw in matched_core if kw not in proper_nouns and kw not in tech_entities)
                    + sum(0.25 for _ in matched_normal)
                    + sum(0.10 for _ in matched_ngram)
                )
                conf = float(item.get("confidence") or 0.7)
                total_score = channel_scores.get(cid, 0.0) + coverage_bonus + conf * 0.3
                item["_search_score"] = round(total_score, 4)
                item["matched_keywords"] = matched_kw
                ranked_list.append((total_score, item))

            ranked_list.sort(key=lambda pair: (pair[0], float(pair[1].get("confidence") or 0.0)), reverse=True)
            ordered = [item for _, item in ranked_list]

            from .tombstones import filter_rows
            return filter_rows(ordered[:limit], self.path, lambda r: r.get("content", ""))

    _REINFORCE_TABLES = {"auto_fact": "auto_facts", "knowledge": "knowledge_entries"}

    def reinforce_hits(self, results: list[dict]) -> int:
        """给实际注入 prompt 的条目记使用痕迹，供衰减与清理判断。

        consolidated/entity_edge 没有这两列，跳过；数据库被别的进程占住时静默不计分。
        last_reinforced_at 由 u1_migration 补列，老库上没这列时不能整条 UPDATE 失败。
        """
        ids_by_table: dict[str, list[int]] = {}
        for item in results or []:
            table = self._REINFORCE_TABLES.get(str(item.get("source_type") or ""))
            sid = item.get("source_id")
            if not table or not isinstance(sid, int):
                continue
            ids_by_table.setdefault(table, []).append(sid)
        if not ids_by_table:
            return 0
        updated = 0
        try:
            with self.connect() as conn:
                for table, ids in ids_by_table.items():
                    cols = {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})")}
                    sets = ["hit_count = COALESCE(hit_count, 0) + 1"]
                    if "last_used" in cols:
                        sets.append("last_used = datetime('now')")
                    if "last_reinforced_at" in cols:
                        sets.append("last_reinforced_at = datetime('now')")
                    marks = ",".join("?" * len(ids))
                    cur = conn.execute(
                        f"UPDATE {table} SET {', '.join(sets)} WHERE id IN ({marks})",
                        ids,
                    )
                    updated += cur.rowcount
                conn.commit()
        except sqlite3.OperationalError:
            return 0
        return updated

    def list_auto_facts(
        self,
        limit: int = 20,
        *,
        include_closed: bool = False,
    ) -> list[dict]:
        """按注入规则返回 auto_facts（默认排除已关闭/被取代/低置信条目）。"""
        where = (
            " WHERE status != 'superseded'"
            if include_closed
            else f" WHERE {_FACT_INJECTION_SQL}"
        )
        with self.connect() as conn:
            rows = conn.execute(
                f"""SELECT id, fact, category, status, due_date, confidence,
                           hit_count, verified, created_at, last_used
                    FROM auto_facts{where}
                    ORDER BY confidence DESC, hit_count DESC, created_at DESC
                    LIMIT ?""",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def find_similar_fact(self, text: str) -> dict | None:
        """找与新事实最相似的未关闭旧条目，供冲突判定。"""
        q = (text or "").strip()
        if not q:
            return None

        cols = "id, fact, category, status, confidence"
        with self.connect() as conn:
            row = conn.execute(
                f"""SELECT {cols} FROM auto_facts
                    WHERE fact = ? AND status IN ('open', 'candidate')
                    ORDER BY confidence DESC LIMIT 1""",
                (q,),
            ).fetchone()
            if row:
                return dict(row)
            if len(q) >= 4:
                row = conn.execute(
                    f"""SELECT {cols} FROM auto_facts
                        WHERE status = 'open'
                          AND (fact LIKE ? OR ? LIKE ('%' || fact || '%'))
                        ORDER BY confidence DESC LIMIT 1""",
                    (f"%{q[:12]}%", q),
                ).fetchone()
                if row:
                    return dict(row)

            prep_entities = self.preprocess_query(q)["entities"]
            min_entity_matches = max(2, (len(prep_entities) + 1) // 2) if prep_entities else 1
            for hit in self.search_memory(q, limit=5, source_types=["auto_fact"]):
                ch = hit.get("match_channel")
                matched_kw = hit.get("matched_keywords") or []
                matched_ent = [k for k in matched_kw if k in prep_entities]
                if ch not in ("exact", "fts") and len(matched_ent) < min_entity_matches:
                    continue
                row = conn.execute(
                    f"""SELECT {cols} FROM auto_facts
                        WHERE id = ? AND status = 'open'""",
                    (hit.get("source_id"),),
                ).fetchone()
                if row:
                    return dict(row)
        return None

    def is_similar_memory(
        self,
        text: str,
        source_types: list[str] | None = None,
    ) -> bool:
        """判断是否与已有记忆高度相似（用于去重）"""
        q = (text or "").strip()
        if not q:
            return False

        with self.connect() as conn:
            if not source_types or "auto_fact" in source_types:
                row = conn.execute(
                    "SELECT id FROM auto_facts WHERE fact = ? AND status = 'open'", (q,)
                ).fetchone()
                if row:
                    return True
                if len(q) >= 4:
                    row = conn.execute(
                        """SELECT id FROM auto_facts
                           WHERE status = 'open'
                             AND (fact LIKE ? OR ? LIKE ('%' || fact || '%'))""",
                        (f"%{q[:12]}%", q),
                    ).fetchone()
                    if row:
                        return True
            if source_types and "knowledge" in source_types:
                row = conn.execute(
                    """SELECT id FROM knowledge_entries
                       WHERE status = 'confirmed'
                         AND (payload = ? OR (LENGTH(?) >= 16 AND payload LIKE ?))""",
                    (q, q, f"%{q[:24]}%"),
                ).fetchone()
                if row:
                    return True

        prep_entities = self.preprocess_query(q)["entities"]
        min_entity_matches = max(2, int(len(prep_entities) * 0.7)) if len(prep_entities) >= 2 else 1
        hits = self.search_memory(q, limit=3, source_types=source_types or ["auto_fact"])
        for h in hits:
            if h.get("match_channel") in ("exact", "fts"):
                return True
            matched_kw = h.get("matched_keywords") or []
            matched_ent = [k for k in matched_kw if k in prep_entities]
            if len(q) <= 32 and len(matched_ent) >= min_entity_matches:
                return True
        return False

    def _seed_apps(self):
        seeds = {
            "记事本": "notepad",
            "计算器": "calc",
            "资源管理器": "explorer",
            "此电脑": "shell:MyComputerFolder",
            "我的电脑": "shell:MyComputerFolder",
            "回收站": "shell:RecycleBinFolder",
            "垃圾箱": "shell:RecycleBinFolder",
            "控制面板": "shell:ControlPanelFolder",
        }
        with self.connect() as conn:
            for alias, cmd in seeds.items():
                conn.execute(
                    """INSERT INTO apps_registry (alias, command, verified)
                       VALUES (?, ?, 1)
                       ON CONFLICT(alias) DO UPDATE SET
                       command = CASE
                         WHEN apps_registry.verified = 1 THEN apps_registry.command
                         ELSE excluded.command
                       END,
                       verified = MAX(apps_registry.verified, 1),
                       updated_at = datetime('now')""",
                    (alias, cmd),
                )
            conn.commit()
