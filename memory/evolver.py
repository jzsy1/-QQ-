"""周期性进化引擎 — 习惯推断、工作流沉淀、知识衰减（桌面场景，不含全屋）。"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
import re
from typing import Any, Callable

from .db import Database

logger = logging.getLogger(__name__)

_MIN_HABIT_COUNT = 5
_MIN_WORKFLOW_COUNT = 3
_HABIT_CONFIDENCE = 0.72
_STALE_DAYS = 30
_DECAY_PER_DAY = 0.01


def _time_bucket(hour: int) -> str:
    if 6 <= hour < 12:
        return "morning"
    if 12 <= hour < 18:
        return "afternoon"
    if 18 <= hour < 22:
        return "evening"
    return "night"


def _bucket_label(bucket: str) -> str:
    return {
        "morning": "早上",
        "afternoon": "下午",
        "evening": "傍晚",
        "night": "夜里",
    }.get(bucket, bucket)


class Evolver:
    def __init__(
        self,
        db: Database,
        *,
        interval_sec: int = 6 * 3600,
        skills_root: Path | str | None = None,
        reload_skills_fn: Callable[[], Any] | None = None,
        study_submit_fn: Callable[[list[str]], Any] | None = None,
        goal_sink: Callable[..., Any] | None = None,  # KEKESI_EVOLVER_GOAL_SINK_V1
    ) -> None:
        self.db = db
        self._interval = max(3600, int(interval_sec))
        self.skills_root = Path(skills_root) if skills_root else None
        self.reload_skills_fn = reload_skills_fn
        self.study_submit_fn = study_submit_fn
        self.goal_sink = goal_sink
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._loop, name="evolver", daemon=True
        )

    def start(self) -> None:
        if self._thread.is_alive():
            return
        self._thread.start()
        logger.info("进化引擎已启动（间隔 %ss）", self._interval)

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        # 启动后稍等，避免与冷启动抢资源
        self._stop.wait(90)
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:
                logger.exception("进化引擎执行失败")
            self._stop.wait(self._interval)

    def run_once(self) -> dict:
        """立即跑一轮进化，返回摘要。"""
        from kekesi.memory.evolution import run_cycle as _p3_run_cycle  # KEKESI_PCE_P3_20261101_V2
        from kekesi.memory.self_study import is_valid_study_topic

        def _filtered_study_submit(topics: list[str] | None):
            if not self.study_submit_fn or not topics:
                return None
            clean = [
                str(t).strip()
                for t in topics
                if t and is_valid_study_topic(str(t).strip(), source="evolution_gap")
            ]
            if not clean:
                return False
            return self.study_submit_fn(clean)

        _p3_run_cycle(study_submit=_filtered_study_submit if self.study_submit_fn else None, goal_sink=self.goal_sink)  # KEKESI_PCE_P3_20261101_V2
        habits = self._infer_habits()
        workflows = self._promote_workflows()
        crystallized = self._crystallize_workflows()
        decayed = self._decay_stale_knowledge()
        self._prune_habits()
        stats = self._write_stats(habits, workflows)
        summary = {
            "habits_inferred": habits,
            "workflows_promoted": workflows,
            "skills_crystallized": crystallized,
            "knowledge_decayed": decayed,
            "stats": stats,
        }
        logger.info("[Evolver] %s", summary)
        return summary

    def _infer_habits(self) -> int:
        """从 task_history + behavior_patterns 推断时段习惯。"""
        since = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        with self.db.connect() as conn:
            rows = conn.execute(
                """SELECT action, created_at FROM task_history
                   WHERE success = 1 AND created_at >= ?
                   ORDER BY id DESC LIMIT 2000""",
                (since,),
            ).fetchall()

        # action 形如 open_app:微信 / click_text:登录
        bucket_actions: dict[str, Counter] = defaultdict(Counter)
        for row in rows:
            action = (row["action"] or "").strip()
            if not action or action.startswith(("search_memory", "get_", "list_", "read_")):
                continue
            created = row["created_at"] or ""
            try:
                hour = datetime.fromisoformat(created.replace("Z", "")).hour
            except ValueError:
                continue
            bucket = _time_bucket(hour)
            # 只记应用类与明确动作
            key = action.split(":", 1)[0] if ":" in action else action
            # open / open_focus / open_raise 等均视为打开应用习惯
            if key in (
                "open",
                "open_focus",
                "open_raise",
                "open_app",
                "close_app",
                "gui_action",
                "search_web",
            ):
                bucket_actions[bucket][action] += 1
            elif action.startswith("open") or "打开" in action:
                bucket_actions[bucket][action] += 1

        created = 0
        with self.db.connect() as conn:
            for bucket, counter in bucket_actions.items():
                for action, freq in counter.most_common(8):
                    if freq < _MIN_HABIT_COUNT:
                        continue
                    desc = f"{_bucket_label(bucket)}常做：{action}"
                    condition = json.dumps(
                        {"time_bucket": bucket, "action": action},
                        ensure_ascii=False,
                    )
                    suggest = self._action_to_suggestion(action)
                    row = conn.execute(
                        """SELECT id, confidence FROM habit_patterns
                           WHERE pattern_type = 'time_based'
                             AND trigger_condition = ?""",
                        (condition,),
                    ).fetchone()
                    conf = min(1.0, 0.5 + 0.05 * freq)
                    if row:
                        conn.execute(
                            """UPDATE habit_patterns SET
                               description = ?, suggested_action = ?,
                               confidence = MAX(confidence, ?),
                               updated_at = datetime('now')
                               WHERE id = ?""",
                            (desc, suggest, conf, row["id"]),
                        )
                    else:
                        conn.execute(
                            """INSERT INTO habit_patterns
                               (pattern_type, description, trigger_condition,
                                suggested_action, confidence, auto_suggest)
                               VALUES ('time_based', ?, ?, ?, ?, 1)""",
                            (desc, condition, suggest, max(conf, _HABIT_CONFIDENCE)),
                        )
                        created += 1

            # 同步 behavior_patterns 高频项
            bp_rows = conn.execute(
                """SELECT id, pattern_type, trigger_condition, expected_action,
                          frequency, confidence, time_bucket, auto_suggest
                   FROM behavior_patterns
                   WHERE frequency >= ? AND confidence >= 0.6""",
                (_MIN_HABIT_COUNT,),
            ).fetchall()
            for bp in bp_rows:
                bucket = (bp["time_bucket"] or "").strip() or "morning"
                condition = json.dumps(
                    {
                        "time_bucket": bucket,
                        "trigger": bp["trigger_condition"],
                        "from": "behavior_patterns",
                    },
                    ensure_ascii=False,
                )
                suggest = f"要帮你「{bp['expected_action'][:40]}」吗？"
                exists = conn.execute(
                    "SELECT id FROM habit_patterns WHERE trigger_condition = ?",
                    (condition,),
                ).fetchone()
                if not exists:
                    conn.execute(
                        """INSERT INTO habit_patterns
                           (pattern_type, description, trigger_condition,
                            suggested_action, confidence, auto_suggest)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (
                            bp["pattern_type"] or "time_based",
                            f"{_bucket_label(bucket)}习惯：{bp['trigger_condition']}",
                            condition,
                            suggest,
                            float(bp["confidence"] or 0.7),
                            int(bp["auto_suggest"] if bp["auto_suggest"] is not None else 1),
                        ),
                    )
                    created += 1
            conn.commit()
        return created

    @staticmethod
    def _action_to_suggestion(action: str) -> str:
        for prefix in ("open_focus:", "open_raise:", "open:", "open_app:"):
            if action.startswith(prefix):
                app = action.split(":", 1)[1]
                return f"要帮你打开{app}吗？"
        if action.startswith("open"):
            return "要帮你打开常用软件吗？"
        if "search_web" in action or action.startswith("search_web"):
            return "要帮你搜点什么吗？"
        if action.startswith("gui_action:"):
            return f"要帮你再点一下「{action.split(':', 1)[1]}」吗？"
        return f"通常这时你会做「{action[:30]}」，要继续吗？"

    def _promote_workflows(self) -> int:
        """同序列 ≥3 次且验证通过 → workflows 草稿/确认。"""
        promoted = 0
        with self.db.connect() as conn:
            rows = conn.execute(
                """SELECT id, signature, steps, trigger_hint, frequency
                   FROM workflow_candidates
                   WHERE frequency >= ?
                   ORDER BY frequency DESC LIMIT 50""",
                (_MIN_WORKFLOW_COUNT,),
            ).fetchall()
            for row in rows:
                try:
                    steps = json.loads(row["steps"] or "[]")
                except json.JSONDecodeError:
                    continue
                if not isinstance(steps, list) or len(steps) < 2:
                    continue
                name = f"auto_{row['signature'][:24]}"
                title = (row["trigger_hint"] or "").strip() or f"自动流程 {row['signature'][:8]}"
                existing = conn.execute(
                    "SELECT id, hit_count, status FROM workflows WHERE name = ?",
                    (name,),
                ).fetchone()
                conf = min(1.0, 0.5 + 0.08 * int(row["frequency"]))
                status = "confirmed" if int(row["frequency"]) >= 5 else "draft"
                payload = json.dumps(steps, ensure_ascii=False)
                if existing:
                    conn.execute(
                        """UPDATE workflows SET steps = ?, title = ?, confidence = ?,
                           status = ?, updated_at = datetime('now')
                           WHERE id = ?""",
                        (
                            payload,
                            title[:80],
                            conf,
                            status,
                            existing["id"],
                        ),
                    )
                else:
                    conn.execute(
                        """INSERT INTO workflows
                           (name, title, steps, trigger_hint, confidence, status, source)
                           VALUES (?, ?, ?, ?, ?, ?, 'auto')""",
                        (
                            name,
                            title[:80],
                            payload,
                            (row["trigger_hint"] or "")[:120],
                            conf,
                            status,
                        ),
                    )
                    promoted += 1
            conn.commit()
        return promoted

    def _crystallize_workflows(self, skills_root: Path | str | None = None) -> int:
        """将已确认(confirmed)的高频多步工作流自动结晶为本地技能包（skills/windows/<name>/）。"""
        from kekesi.memory.evolution import render_executable_main as _p3_render_main  # KEKESI_PCE_P3_20261101_V2
        root = Path(skills_root) if skills_root else self.skills_root
        if root is None:
            return 0
        with self.db.connect() as conn:
            rows = conn.execute(
                """SELECT name, title, steps, trigger_hint, confidence
                   FROM workflows
                   WHERE status = 'confirmed' AND confidence >= 0.7
                   ORDER BY confidence DESC LIMIT 20"""
            ).fetchall()
        if not rows:
            return 0

        import yaml

        created = 0
        win_dir = root / "windows"
        win_dir.mkdir(parents=True, exist_ok=True)
        for row in rows:
            raw_name = re.sub(r"[^a-z0-9_]+", "_", str(row["name"] or "").lower()).strip("_")
            if not raw_name or len(raw_name) < 3:
                continue
            skill_name = raw_name[:32]
            skill_dir = win_dir / skill_name
            if (skill_dir / "skill.yaml").is_file() and (skill_dir / "main.py").is_file():
                continue
            try:
                steps = json.loads(row["steps"] or "[]")
            except Exception:
                continue
            if not isinstance(steps, list) or len(steps) < 2:
                continue
            title = str(row["title"] or row["trigger_hint"] or skill_name).strip()[:80]
            kws = [w for w in re.split(r"[\s,，。、/|]+", title) if len(w) >= 2][:6]
            manifest = {
                "name": skill_name,
                "version": "1.0.0",
                "description": f"自动结晶工作流：{title}",
                "category": "windows",
                "entry": "main.py",
                "crystallized": True,
                "workflow_name": str(row["name"]),
                "tools": [
                    {
                        "id": "run",
                        "description": f"一键执行固化流程：{title}",
                        "keywords": kws or [skill_name],
                        "parameters": {"type": "object", "properties": {}, "required": []},
                    }
                ],
            }
            steps_src = repr(steps)
            code = (
                f"# 自动结晶技能：{title}\n\n"
                f"STEPS = {steps_src}\n\n"
                "def run_tool(tool_id: str, args: dict) -> tuple[bool, str]:\n"
                "    summary = ' -> '.join(str(s.get('func', '')) for s in STEPS)\n"
                f"    return True, f'已按结晶流程「{title}」执行 {{len(STEPS)}} 步：{{summary}}'\n"
            )
            try:
                skill_dir.mkdir(parents=True, exist_ok=True)
                (skill_dir / "skill.yaml").write_text(
                    yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False),
                    encoding="utf-8",
                )
                (skill_dir / "main.py").write_text(_p3_render_main(steps), encoding="utf-8")  # KEKESI_PCE_P3_20261101_V2
                created += 1
            except Exception:
                logger.debug("工作流结晶写入失败: %s", skill_name, exc_info=True)
        if created and self.reload_skills_fn:
            try:
                self.reload_skills_fn()
            except Exception:
                logger.debug("结晶技能热重载失败", exc_info=True)
        return created

    def _decay_stale_knowledge(self) -> int:
        cutoff = (datetime.now() - timedelta(days=_STALE_DAYS)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        # KEKESI_EVOLVER_DECAY_FIX_20261104
        # 扣量按真实 tick 间隔折算：0.01/天 × interval/86400，恢复设计速率
        # （旧逻辑每轮固定扣一周的量 0.07，6 小时一轮时新知识约 1.5 天即被扣到 stale）
        # last_used 为 NULL 的新知识要求 created_at 超过 30 天才起扣，给 30 天 grace。
        decay_step = (
            _DECAY_PER_DAY * max(1, int(getattr(self, "_interval", 86400))) / 86400.0
        )
        with self.db.connect() as conn:
            cur = conn.execute(
                """UPDATE knowledge_entries SET
                   confidence = MAX(0.0, confidence - ?)
                   WHERE status = 'confirmed'
                     AND (
                       (last_used IS NOT NULL AND last_used < ?)
                       OR (last_used IS NULL AND created_at < ?)
                     )""",
                (decay_step, cutoff, cutoff),
            )
            # 过低置信度标 stale 并同步清理 FTS5 索引
            conn.execute(
                """UPDATE knowledge_entries SET status = 'stale'
                   WHERE status = 'confirmed' AND confidence < 0.3"""
            )
            self.db.sweep_orphan_fts_in_conn(conn)
            conn.execute(
                """UPDATE ui_cache SET confidence = MAX(0.0, confidence - 0.05)
                   WHERE last_used IS NULL OR last_used < ?""",
                (cutoff,),
            )
            conn.commit()
            return int(cur.rowcount or 0)

    def _prune_habits(self, max_total: int = 60) -> None:
        """习惯表防膨胀：低置信/被拒绝的旧条目清掉，总量封顶。"""
        with self.db.connect() as conn:
            conn.execute(
                """DELETE FROM habit_patterns
                   WHERE auto_suggest = 0
                     AND updated_at < datetime('now', '-30 days')"""
            )
            conn.execute(
                "DELETE FROM habit_patterns WHERE confidence < 0.3"
            )
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM habit_patterns"
            ).fetchone()
            total = int(row["c"] or 0)
            if total > max_total:
                conn.execute(
                    """DELETE FROM habit_patterns WHERE id IN (
                         SELECT id FROM habit_patterns
                         ORDER BY confidence ASC, hit_count ASC
                         LIMIT ?
                       )""",
                    (total - max_total,),
                )
            conn.commit()

    def _write_stats(self, habits: int, workflows: int) -> dict:
        today = datetime.now().strftime("%Y-%m-%d")
        with self.db.connect() as conn:
            total = conn.execute(
                "SELECT COUNT(*) AS c FROM conversation_digest WHERE date(created_at)=?",
                (today,),
            ).fetchone()["c"]
            avg_conf = conn.execute(
                """SELECT AVG(confidence) AS a FROM knowledge_entries
                   WHERE status = 'confirmed'"""
            ).fetchone()["a"]
            cache_hits = conn.execute(
                "SELECT COALESCE(SUM(hit_count),0) AS c FROM ui_cache"
            ).fetchone()["c"]
            cache_fails = conn.execute(
                "SELECT COALESCE(SUM(fail_count),0) AS c FROM ui_cache"
            ).fetchone()["c"]
            denom = max(1, int(cache_hits) + int(cache_fails))
            cache_rate = float(cache_hits) / denom
            conn.execute(
                """INSERT INTO learning_stats
                   (date, total_requests, avg_confidence, cache_hit_rate,
                    habits_inferred, workflows_promoted, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
                   ON CONFLICT(date) DO UPDATE SET
                     total_requests = excluded.total_requests,
                     avg_confidence = excluded.avg_confidence,
                     cache_hit_rate = excluded.cache_hit_rate,
                     habits_inferred = learning_stats.habits_inferred + excluded.habits_inferred,
                     workflows_promoted = learning_stats.workflows_promoted + excluded.workflows_promoted,
                     updated_at = datetime('now')""",
                (
                    today,
                    int(total or 0),
                    float(avg_conf or 0),
                    cache_rate,
                    habits,
                    workflows,
                ),
            )
            conn.commit()
        return {
            "date": today,
            "total_requests": int(total or 0),
            "avg_confidence": float(avg_conf or 0),
            "cache_hit_rate": cache_rate,
        }
