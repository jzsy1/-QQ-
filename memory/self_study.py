"""SelfStudyService — 后台定时自习：只学科技论文 (arXiv) + 开源项目 (GitHub)，有界自主分析。

链路：
    每日到点（config.yaml self_study.time，错过启动后补跑一次）
    → 挑主题（显式 topics → github_topics / arxiv_categories 种子 → 可选近期对话严格前缀选目）
    → StudyAnalyst.analyze() 驱动 6–8 步有界工具循环（study_sources 纯 HTTP 抓取）
    → finish_note 结构化收口：{verdict, points[], source_refs[], quality, adopt}
    → 严苛入库门槛：拒元笔记、核验 source_refs ⊆ 真实 fetched_urls、要求 verdict + ≥2 条 points
    → 按来源深度分级赋 confidence，分存 study_paper / study_repo (status='confirmed')
    → 若含 adopt 提案则另存 category='study_adopt', status='candidate'（不自动安装，待用户批准）
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from ..tools import study_sources as _default_sources
from ..tools.study_sources import HttpBudget, normalize_source_url
from .db import Database
from .study_agent import StudyAnalyst, StudyOutcome, StudyTopic

logger = logging.getLogger(__name__)

# 默认值集中管理（config.yaml self_study 节可逐项覆盖）
_DEFAULT_TIME = "09:30"
_DEFAULT_MAX_TOPICS = 2
_DEFAULT_NOTE_MAX_CHARS = 600
_DEFAULT_MATERIAL_MAX_CHARS = 6000
_DEFAULT_RESTUDY_DAYS = 7
_DEFAULT_RECENT_DAYS = 7
_DEFAULT_MAX_STEPS = 7
_DEFAULT_WALL_SEC = 180
_DEFAULT_FOCUS = ("paper", "repo")
_DEFAULT_ARXIV_CATEGORIES = ("cs.CL", "cs.AI", "cs.LG", "cs.SE", "cs.MA")
_DEFAULT_GITHUB_TOPICS = (
    "LLM agent memory",
    "TTS",
    "ASR",
    "voice assistant",
    "web automation",
)
_DEFAULT_GITHUB_TOKEN_ENV = "GITHUB_TOKEN"
_POLL_INTERVAL_SEC = 60.0
_RECENT_QUESTION_LIMIT = 30

_STUDY_ACTION = "self_study"
_CATEGORY = "self_study"  # 历史兼容常量
_CATEGORY_PAPER = "study_paper"
_CATEGORY_REPO = "study_repo"
_CATEGORY_ADOPT = "study_adopt"
_STUDY_CATEGORIES = (_CATEGORY_PAPER, _CATEGORY_REPO, _CATEGORY)

_NOTE_PREFIX = "【自习笔记·{topic}】"

# 元笔记拦截正则：凡假装完成但实为“资料不足/无法支撑”的空话一律拒之门外
_META_NOTE_RE = re.compile(
    r"资料不足|无法整理|没有找到|现有资料无法|搜索结果没有|无法支撑|"
    r"未找到相关|暂无相关资料|信息不足|无法得出结论|未能获取到|"
    r"无法提供有效|缺少足够.{0,6}资料|没有有效.{0,6}信息|无法回答|无法核实|"
    r"无法据此|未包含.{0,15}有效|没有可靠说明|未明确定义|"
    r"未提供.{0,15}(?:有效|具体|可核验)|没有提供.{0,18}(?:有效|可靠)",
    re.IGNORECASE,
)

# 选目输出严格前缀：只接受 ARXIV: <英文词> 或 REPO: <检索词>
_TOPIC_PREFIX_RE = re.compile(r"^(ARXIV|REPO)\s*:\s*(.+)$", re.IGNORECASE)

# ─── P1-2：自学主题来源白名单与内部异常标签过滤 ──────────────
ALLOWED_TOPIC_SOURCES = frozenset(
    {
        "user",
        "config",
        "seed",
        "paper",
        "repo",
        "arxiv",
        "github",
        "academic",
        "technical",
        "research",
        "llm_study",
        "evolution_gap",
        "",
    }
)

FORBIDDEN_TOPIC_SOURCES = frozenset(
    {
        "task_history",
        "internal",
        "self_study",
        "error",
        "exception",
        "scheduler",
        "system_status",
        "memory_internal",
        "error_corrections",
        "execution_history",
    }
)

FORBIDDEN_INTERNAL_TAGS = (
    "self_study:",
    "self_study",
    "internal:",
    "task_history",
    "error_corrections",
    "execution_history",
    "memory_audit",
    "memory_fts",
    "auto_facts",
    "knowledge_entries",
    "open:",
    "open_app",
    "open_focus",
    "open_raise",
    "close:",
    "close_app",
    "skill:",
    "skill__",
    "mcp__",
    "gui_action",
    "click_text",
    "execute_gui_macro",
    "run_shell",
    "delete_file",
    "move_to_trash",
)

_INTERNAL_POLLUTION_RE = re.compile(
    r"self_study\s*:|internal\s*:|task_history|"
    r"失败原因与改进|能力短板补齐|执行失败|调用失败|未找到应用|预算已满|"
    r"budget_exhausted|session-start|traceback|operationalerror|sqlite3\.|"
    r"schedule_event|cron_trigger|heartbeat_tick|system_status|"
    r"植物大战僵尸托管|关闭斗地主|打开斗地主",
    re.IGNORECASE,
)

_BARE_INTERNAL_STATES = frozenset(
    {
        "session-start",
        "busy",
        "ok",
        "failed",
        "error",
        "timeout",
        "running",
        "pending",
        "blocked",
        "interrupted",
        "scheduler",
        "heartbeat",
        "tick",
        "unknown",
        "none",
        "null",
    }
)


def normalize_study_topic_content(topic: str) -> str:
    """将主题剥离 ARXIV:/REPO: 等前缀、括号与多余空白并归一化，供深层内容校验。"""
    s = str(topic or "").strip()
    while True:
        m = re.match(r"^(?:ARXIV|REPO|PAPER|GITHUB|STUDY|TOPIC)\s*:\s*(.+)$", s, flags=re.IGNORECASE)
        if not m:
            break
        s = m.group(1).strip()
    s = re.sub(r"^[\"'`“”‘’【】\[\]()（）\s]+|[\"'`“”‘’【】\[\]()（）\s]+$", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def is_valid_study_topic(topic: str, source: str = "user") -> bool:
    """校验主题是否允许进入自学队列：检查来源白名单、原始文本及归一化后的主题内容。"""
    src = str(source or "").strip().lower()
    if src in FORBIDDEN_TOPIC_SOURCES:
        return False
    if src and src not in ALLOWED_TOPIC_SOURCES:
        return False

    raw = str(topic or "").strip()
    if not raw or len(raw) < 2 or len(raw) > 150:
        return False

    norm = normalize_study_topic_content(raw)
    if not norm or len(norm) < 2:
        return False

    raw_lower = raw.lower()
    norm_lower = norm.lower()

    if norm_lower in _BARE_INTERNAL_STATES or raw_lower in _BARE_INTERNAL_STATES:
        return False

    for tag in FORBIDDEN_INTERNAL_TAGS:
        tag_l = tag.lower()
        if tag_l in raw_lower or tag_l in norm_lower:
            return False

    if _INTERNAL_POLLUTION_RE.search(raw) or _INTERNAL_POLLUTION_RE.search(norm):
        return False

    return True


def is_meta_note(text: str) -> bool:
    """判断文本是否属于“资料不足/无法支撑”类无效元笔记。"""
    s = str(text or "").strip()
    if not s:
        return True
    return bool(_META_NOTE_RE.search(s))


class SelfStudyService:
    """自主学习引擎（定时自习版），只学科技论文与开源项目，与 LearningService 互补。"""

    def __init__(
        self,
        db: Database,
        llm_chat: Callable | None = None,
        cfg: dict | None = None,
        memory: Any | None = None,
        sources_mod: Any | None = None,
    ) -> None:
        self.db = db
        self.llm_chat = llm_chat
        self._memory = memory
        self._sources = sources_mod or _default_sources
        cfg = cfg or {}
        self._cfg = dict(cfg)

        self._enabled = bool(cfg.get("enabled", True))
        self._time = str(cfg.get("time") or _DEFAULT_TIME).strip()
        self._topics = [
            str(t).strip()
            for t in (cfg.get("topics") or [])
            if str(t).strip() and is_valid_study_topic(str(t).strip(), source="config")
        ]
        self._max_topics = max(
            1, int(cfg.get("max_topics_per_session") or _DEFAULT_MAX_TOPICS)
        )
        self._note_max_chars = max(
            100, int(cfg.get("note_max_chars") or _DEFAULT_NOTE_MAX_CHARS)
        )
        self._material_max_chars = max(
            1000, int(cfg.get("material_max_chars") or _DEFAULT_MATERIAL_MAX_CHARS)
        )
        self._restudy_days = max(
            1, int(cfg.get("restudy_after_days") or _DEFAULT_RESTUDY_DAYS)
        )
        self._recent_days = max(
            1, int(cfg.get("recent_days") or _DEFAULT_RECENT_DAYS)
        )
        self._use_recent_questions = bool(cfg.get("use_recent_questions", False))

        raw_focus = cfg.get("focus") or list(_DEFAULT_FOCUS)
        self._focus = [
            str(f).strip().lower()
            for f in raw_focus
            if str(f).strip().lower() in ("paper", "repo")
        ] or list(_DEFAULT_FOCUS)

        raw_cats = cfg.get("arxiv_categories")
        self._arxiv_categories = (
            [str(c).strip() for c in raw_cats if str(c).strip()]
            if isinstance(raw_cats, list) and raw_cats
            else list(_DEFAULT_ARXIV_CATEGORIES)
        )

        raw_gh = cfg.get("github_topics")
        self._github_topics = (
            [str(g).strip() for g in raw_gh if str(g).strip()]
            if isinstance(raw_gh, list) and raw_gh
            else list(_DEFAULT_GITHUB_TOPICS)
        )

        self._github_token_env = str(
            cfg.get("github_token_env") or _DEFAULT_GITHUB_TOKEN_ENV
        ).strip()
        self._max_steps = max(
            2, int(cfg.get("max_steps_per_topic") or _DEFAULT_MAX_STEPS)
        )
        self._wall_sec = max(
            15, int(cfg.get("session_wall_sec") or _DEFAULT_WALL_SEC)
        )
        self._budget_cfg = (
            dict(cfg.get("budget")) if isinstance(cfg.get("budget"), dict) else {}
        )
        default_state_file = (
            Path(self.db.path).parent / "study_state.json"
            if getattr(self.db, "path", None)
            else None
        )
        self._state_file: Path | str | None = cfg.get("state_file") or default_state_file

        self._running = False
        self._thread: threading.Thread | None = None
        self._session_lock = threading.Lock()
        self._last_result: dict[str, Any] = {}

    # ─── 生命周期 ────────────────────────────────────────────────

    @property
    def running(self) -> bool:
        return self._running

    def start(self) -> None:
        """启动每日调度线程（幂等）。"""
        if not self._enabled:
            logger.info("自习服务未启用（self_study.enabled=false）")
            return
        if self._running:
            return
        self.cleanup_stale_meta_notes()
        self._running = True
        self._thread = threading.Thread(
            target=self._loop, name="kekesi-self-study", daemon=True
        )
        self._thread.start()
        logger.info(
            "自习服务已启动（每日 %s，单场最多 %d 个主题，方向=%s）",
            self._time,
            self._max_topics,
            ",".join(self._focus),
        )

    def stop(self) -> None:
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._thread = None

    def _loop(self) -> None:
        """调度主循环：到点且今日未跑 → 自习一场。"""
        while self._running:
            try:
                if self._due_now():
                    self._mark_ran()
                    result = self.run_session()
                    logger.info("自习完成: %s", result.get("summary", ""))
            except Exception:
                logger.warning("自习调度异常", exc_info=True)
            time.sleep(_POLL_INTERVAL_SEC)

    # ─── 调度决策 ────────────────────────────────────────────────

    def _scheduled_hhmm(self) -> tuple[int, int]:
        m = re.match(r"^(\d{1,2}):(\d{2})$", self._time)
        if not m:
            logger.warning("self_study.time 格式非法 %r，回退 %s", self._time, _DEFAULT_TIME)
            m = re.match(r"^(\d{1,2}):(\d{2})$", _DEFAULT_TIME)
            return int(m.group(1)), int(m.group(2))  # type: ignore[union-attr]
        return int(m.group(1)), int(m.group(2))

    def _due_now(self) -> bool:
        """当前时间已过每日到点，且今天还没自习过。"""
        now = datetime.now()
        hh, mm = self._scheduled_hhmm()
        if (now.hour, now.minute) < (hh, mm):
            return False
        return not self._ran_today()

    def _ran_today(self) -> bool:
        today_local = datetime.now().strftime("%Y-%m-%d")
        with self.db.connect() as conn:
            row = conn.execute(
                """SELECT id FROM task_history
                   WHERE action = ?
                     AND (
                       date(created_at, 'localtime') = ?
                       OR (created_at >= datetime('now', '-12 hours') AND date(created_at) = date('now'))
                     )
                   LIMIT 1""",
                (_STUDY_ACTION, today_local),
            ).fetchone()
        return row is not None

    def _mark_ran(self) -> None:
        with self.db.connect() as conn:
            conn.execute(
                "INSERT INTO task_history (action, result, success) VALUES (?, ?, 1)",
                (_STUDY_ACTION, "session-start"),
            )
            conn.commit()

    # ─── 手动触发（语音/LLM 工具入口） ──────────────────────────

    def run_now_async(
        self,
        topics: list[str] | None = None,
        *,
        source: str = "user",
    ) -> bool:
        """立即开一场自习（后台线程）。已在自习中或显式主题全部被污染过滤拦截则返回 False。"""
        if self._session_lock.locked():
            return False
        if topics is not None:
            valid_explicit = [
                str(t).strip()
                for t in topics
                if t and str(t).strip() and is_valid_study_topic(str(t).strip(), source=source)
            ]
            if not valid_explicit:
                logger.info("自习手动触发被拦截：无有效学术/技术主题 (raw=%r, source=%r)", topics, source)
                return False
            topics = valid_explicit

        def _run() -> None:
            self.run_session(topics=topics, source=source)

        threading.Thread(target=_run, name="kekesi-self-study-now", daemon=True).start()
        return True

    # ─── 自习会话 ────────────────────────────────────────────────

    def _make_budget(self) -> HttpBudget:
        return HttpBudget(
            daily_limits=self._budget_cfg,
            state_file=self._state_file,
        )

    def _make_analyst(self) -> StudyAnalyst:
        analyst_cfg = {
            "max_steps_per_topic": self._max_steps,
            "session_wall_sec": self._wall_sec,
            "github_token_env": self._github_token_env,
            "material_max_chars": self._material_max_chars,
            "budget": self._budget_cfg,
        }
        return StudyAnalyst(
            db=self.db,
            llm_chat=self.llm_chat,
            cfg=analyst_cfg,
            sources_mod=self._sources,
        )

    def run_session(
        self,
        topics: list[str] | None = None,
        *,
        source: str = "user",
    ) -> dict[str, Any]:
        """执行一场自习：选主题 → StudyAnalyst 有界研读 → 严门槛入库。"""
        if not self._session_lock.acquire(blocking=False):
            logger.info("自习会话进行中，跳过本次触发")
            return {"ok": False, "reason": "busy", "summary": "自习进行中"}
        try:
            picked = self._pick_topics(topics, source=source)
            if not picked:
                self._last_result = {
                    "ok": True,
                    "topics": [],
                    "saved": [],
                    "failed": [],
                    "summary": "没有可学的主题",
                }
                self._log_session(self._last_result)
                return self._last_result

            saved: list[str] = []
            failed: list[str] = []
            budget_skipped: list[str] = []

            for topic_str in picked:
                try:
                    ok = self._study_topic(topic_str, budget_skipped=budget_skipped)
                    if ok:
                        saved.append(topic_str)
                    else:
                        failed.append(topic_str)
                except Exception as e:
                    logger.warning("自习主题「%s」异常: %s", topic_str, e)
                    failed.append(topic_str)

            summary = f"学了 {len(saved)} 个主题（{', '.join(saved)}）"
            if failed:
                summary += f"；{len(failed)} 个未成（{', '.join(failed)}）"
            if budget_skipped:
                summary += f"；超预算跳过 {len(budget_skipped)} 个"
            self._last_result = {
                "ok": bool(saved) or not picked,
                "topics": picked,
                "saved": saved,
                "failed": failed,
                "budgetSkipped": budget_skipped,
                "summary": summary,
            }
            self._log_session(self._last_result)
            return self._last_result
        finally:
            self._session_lock.release()

    def _log_session(self, result: dict[str, Any]) -> None:
        try:
            with self.db.connect() as conn:
                conn.execute(
                    "INSERT INTO task_history (action, result, success) VALUES (?, ?, ?)",
                    (
                        f"{_STUDY_ACTION}:result",
                        str(result.get("summary") or "")[:200],
                        1 if result.get("saved") else 0,
                    ),
                )
                conn.commit()
        except Exception:
            logger.debug("自习结果记录失败", exc_info=True)

    def _log_budget_skip(self, topic: str, reason: str) -> None:
        try:
            with self.db.connect() as conn:
                conn.execute(
                    "INSERT INTO task_history (action, result, success) VALUES (?, ?, 0)",
                    (
                        f"{_STUDY_ACTION}:budget",
                        f"{topic}: {reason}"[:200],
                    ),
                )
                conn.commit()
        except Exception:
            logger.debug("预算跳过记录失败", exc_info=True)

    # ─── 主题选取（只允许论文与开源仓库） ────────────────────────

    def _build_seed_candidates(self) -> list[str]:
        """按显式 topics → github_topics / arxiv_categories 种子顺序构造候选。"""
        candidates: list[str] = []

        # 1. 显式配置的 self_study.topics
        for t in self._topics:
            if t and t not in candidates:
                candidates.append(t)

        # 2. 论文与开源项目种子交替排列，确保每场兼顾 paper 与 repo
        paper_seeds: list[str] = []
        if "paper" in self._focus:
            for idx, gh_kw in enumerate(self._github_topics):
                cat = (
                    self._arxiv_categories[idx % len(self._arxiv_categories)]
                    if self._arxiv_categories
                    else ""
                )
                seed = f"ARXIV: {cat} {gh_kw}".strip() if cat else f"ARXIV: {gh_kw}"
                paper_seeds.append(seed)
            for cat in self._arxiv_categories:
                seed = f"ARXIV: {cat} agent"
                if seed not in paper_seeds:
                    paper_seeds.append(seed)

        repo_seeds: list[str] = []
        if "repo" in self._focus:
            for gh_kw in self._github_topics:
                repo_seeds.append(f"REPO: {gh_kw}")

        max_len = max(len(paper_seeds), len(repo_seeds))
        for i in range(max_len):
            if i < len(paper_seeds) and paper_seeds[i] not in candidates:
                candidates.append(paper_seeds[i])
            if i < len(repo_seeds) and repo_seeds[i] not in candidates:
                candidates.append(repo_seeds[i])

        return candidates

    def _pick_topics(
        self,
        explicit: list[str] | None,
        *,
        source: str = "user",
    ) -> list[str]:
        if explicit is not None:
            picked = [
                str(t).strip()
                for t in explicit
                if t and str(t).strip() and is_valid_study_topic(str(t).strip(), source=source)
            ]
            return picked[: self._max_topics]

        candidates = [
            t for t in self._build_seed_candidates() if is_valid_study_topic(t, source="seed")
        ]

        if self._use_recent_questions:
            recent = self._recent_questions()
            llm_picked = self._pick_topics_via_llm(recent) if recent else []
            # 当显式开启 use_recent_questions 时，优先将 LLM 严格筛选出的技术主题置于静态种子前
            merged: list[str] = []
            for t in list(self._topics) + llm_picked + candidates:
                if t and t not in merged and is_valid_study_topic(t, source="seed"):
                    merged.append(t)
            candidates = merged

        fresh: list[str] = []
        for t in candidates:
            if t and is_valid_study_topic(t, source="seed") and not self._studied_recently(t):
                fresh.append(t)
            if len(fresh) >= self._max_topics:
                break
        return fresh

    def _recent_questions(self) -> list[str]:
        """近 N 天用户原话（仅在 use_recent_questions=true 时供技术选目参考）。"""
        try:
            with self.db.connect() as conn:
                rows = conn.execute(
                    """SELECT user_text FROM conversation_digest
                       WHERE user_text IS NOT NULL AND user_text != ''
                         AND created_at >= datetime('now', ?)
                       ORDER BY id DESC LIMIT ?""",
                    (f"-{self._recent_days} day", _RECENT_QUESTION_LIMIT),
                ).fetchall()
            texts: list[str] = []
            for r in rows:
                t = str(r["user_text"] or "").strip()
                if len(t) >= 4:
                    texts.append(t)
            return texts
        except Exception:
            logger.debug("读取近期对话失败", exc_info=True)
            return []

    def _pick_topics_via_llm(self, recent_texts: list[str]) -> list[str]:
        """从近期对话中提取科技论文或开源项目检索词；无 ARXIV:/REPO: 前缀一律丢弃。"""
        if not self.llm_chat or not recent_texts:
            return []
        joined = "\n".join(f"- {t[:80]}" for t in recent_texts[:_RECENT_QUESTION_LIMIT])
        try:
            result = self.llm_chat(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "你是技术研究选目器。从近期对话中挑选值得检索的【科技论文】或【GitHub 开源项目】主题。\n"
                            "输出格式铁律（每行一条，最多 4 行，不要编号、不要解释）：\n"
                            "只能输出 `ARXIV: <英文检索词>` 或 `REPO: <检索词>`。\n"
                            "日常寒暄、本地软件操作、娱乐直播、生活话题一律不选！没有符合的技术主题就返回空。"
                        ),
                    },
                    {"role": "user", "content": f"近期对话：\n{joined}"},
                ],
                tools=None,
            )
            if not result:
                return []
            content = (result.get("content") or "").strip()
            topics: list[str] = []
            for raw_line in content.split("\n"):
                line = re.sub(r"^[\d\.\-\*\s]+", "", raw_line).strip()
                m = _TOPIC_PREFIX_RE.match(line)
                if not m:
                    continue
                prefix = m.group(1).upper()
                kw = m.group(2).strip()
                if prefix == "ARXIV" and "paper" not in self._focus:
                    continue
                if prefix == "REPO" and "repo" not in self._focus:
                    continue
                if 2 <= len(kw) <= 60:
                    normalized = f"{prefix}: {kw}"
                    if is_valid_study_topic(normalized, source="llm_study") and normalized not in topics:
                        topics.append(normalized)
            return topics[:4]
        except Exception as e:
            logger.debug("LLM 选目失败: %s", e)
            return []

    def _studied_recently(self, topic: str) -> bool:
        """restudy_after_days 天内已确认入库的主题跳过。"""
        st = StudyTopic.from_raw(topic)
        triggers_to_check = {topic.strip(), st.display_name.strip(), st.query.strip()}
        triggers_to_check.discard("")
        if not triggers_to_check:
            return False
        placeholders = ",".join("?" for _ in _STUDY_CATEGORIES)
        trig_placeholders = ",".join("?" for _ in triggers_to_check)
        try:
            with self.db.connect() as conn:
                row = conn.execute(
                    f"""SELECT id FROM knowledge_entries
                        WHERE category IN ({placeholders})
                          AND status = 'confirmed'
                          AND trigger IN ({trig_placeholders})
                          AND created_at >= datetime('now', ?)
                        LIMIT 1""",
                    (*_STUDY_CATEGORIES, *triggers_to_check, f"-{self._restudy_days} day"),
                ).fetchone()
            return row is not None
        except Exception:
            logger.debug("查询已学主题失败", exc_info=True)
            return False

    # ─── 单主题：委托 StudyAnalyst → 严门槛入库 ──────────────────

    def _study_topic(
        self,
        topic: str,
        *,
        budget_skipped: list[str] | None = None,
    ) -> bool:
        if not is_valid_study_topic(topic, source="user"):
            logger.info("自习主题「%s」命中内部标签或非学术主题过滤规则，拒绝入队", topic)
            return False
        default_kind = self._focus[0] if self._focus else "paper"
        study_topic = StudyTopic.from_raw(topic, default_kind=default_kind)
        budget = self._make_budget()
        analyst = self._make_analyst()

        outcome = analyst.analyze(study_topic, budget=budget)
        if not outcome.ok:
            if outcome.abort_reason.startswith("budget_exhausted") or budget.exhausted_reason:
                reason = budget.exhausted_reason or outcome.abort_reason
                logger.info("自习主题「%s」因预算耗尽跳过: %s", topic, reason)
                self._log_budget_skip(topic, reason)
                if budget_skipped is not None:
                    budget_skipped.append(topic)
            else:
                logger.info(
                    "自习主题「%s」分析未达标跳过 (reason=%s, steps=%d)",
                    topic,
                    outcome.abort_reason,
                    outcome.steps_taken,
                )
            return False

        return self._store_note(study_topic, outcome, budget)

    # ─── 入库门槛与置信度分级 ────────────────────────────────────

    @staticmethod
    def _compute_confidence(outcome: StudyOutcome) -> float:
        """按来源深度分级赋 confidence：
        arXiv 全文 0.70 / 仅摘要 0.50 / 仓库 README+源码 0.65 / 仅仓库元信息 0.45
        """
        used = outcome.sources_used or set()
        if outcome.kind == "paper":
            if "arxiv_html" in used:
                return 0.70
            return 0.50
        if "gh_readme_code" in used:
            return 0.65
        return 0.45

    @staticmethod
    def _verify_source_refs(
        source_refs: list[str],
        outcome: StudyOutcome,
        budget: HttpBudget,
    ) -> list[str]:
        """校验 source_refs ⊆ 本轮真实 fetch 过的 URL；过滤掉任何未核验链接。"""
        if not source_refs:
            return []
        fetched_norm = {normalize_source_url(u) for u in (outcome.fetched_urls | budget.fetched_urls) if u}
        if not fetched_norm:
            return []

        verified: list[str] = []
        for ref in source_refs:
            r = str(ref or "").strip()
            if not r:
                continue
            if budget.is_url_verified(r) or normalize_source_url(r) in fetched_norm:
                if r not in verified:
                    verified.append(r)
        return verified

    def _store_note(
        self,
        topic: StudyTopic,
        outcome: StudyOutcome,
        budget: HttpBudget,
    ) -> bool:
        verdict = str(outcome.verdict or "").strip()
        points = [str(p).strip() for p in (outcome.points or []) if str(p).strip()]

        # 1. 结构完整性校验：必须有 verdict 且 ≥2 条 points
        if len(verdict) < 6 or len(points) < 2:
            logger.info(
                "自习笔记结构不达标（verdict=%d字, points=%d条），拒入库「%s」",
                len(verdict),
                len(points),
                topic.raw,
            )
            return False

        # 2. 元笔记正则拦截
        combined_text = verdict + "\n" + "\n".join(points)
        if is_meta_note(combined_text):
            logger.info("自习笔记命中元笔记黑名单正则，拒入库「%s」", topic.raw)
            return False

        # 3. 来源真实性核验：source_refs 必须非空且全部属于本轮真实 fetch 过的 URL
        if not outcome.source_refs:
            logger.info("自习笔记缺少 source_refs，拒入库「%s」", topic.raw)
            return False
        verified_refs = self._verify_source_refs(outcome.source_refs, outcome, budget)
        if len(verified_refs) != len(outcome.source_refs) or not verified_refs:
            logger.info(
                "自习笔记含未核验/编造 URL (refs=%s, fetched=%s)，拒入库「%s」",
                outcome.source_refs,
                list(outcome.fetched_urls)[:5],
                topic.raw,
            )
            return False

        # 4. 组装笔记正文并截断
        points_block = "\n".join(f"- {p}" for p in points[:5])
        refs_block = "来源: " + " , ".join(verified_refs[:3])
        body = f"{verdict}\n{points_block}\n{refs_block}"
        if len(body) > self._note_max_chars:
            # 保留来源行，仅截断要点正文
            body_head = f"{verdict}\n{points_block}"
            max_head = max(80, self._note_max_chars - len(refs_block) - 2)
            body = body_head[:max_head].rstrip() + "\n" + refs_block

        trigger = topic.display_name or topic.query or topic.raw
        payload = _NOTE_PREFIX.format(topic=trigger) + body
        category = _CATEGORY_REPO if outcome.kind == "repo" else _CATEGORY_PAPER
        confidence = self._compute_confidence(outcome)

        try:
            if self.db.is_similar_memory(payload, source_types=["knowledge"]):
                logger.info("自习笔记与已有知识重复，跳过「%s」", trigger)
                return False
            with self.db.connect() as conn:
                cur = conn.execute(
                    """INSERT INTO knowledge_entries
                       (category, trigger, payload, confidence, status)
                       VALUES (?, ?, ?, ?, 'confirmed')""",
                    (category, trigger, payload, confidence),
                )
                entry_id = int(cur.lastrowid)
                conn.commit()
            self.db.index_memory(payload, "knowledge", entry_id)
            logger.info(
                "已记自习笔记 [%s, conf=%.2f]: %s",
                category,
                confidence,
                trigger,
            )
        except Exception as e:
            logger.warning("自习笔记入库失败「%s」: %s", trigger, e)
            return False

        # 5. 接入候选另存（status='candidate'，不注入对话、不自动执行）
        if outcome.adopt and isinstance(outcome.adopt, dict):
            self._store_adopt_candidate(outcome.adopt, verified_refs, confidence)

        return True

    def _store_adopt_candidate(
        self,
        adopt: dict[str, Any],
        verified_refs: list[str],
        confidence: float,
    ) -> bool:
        repo = str(adopt.get("repo") or "").strip()
        why = str(adopt.get("why") or "").strip()
        if not repo or not why:
            return False
        record = {
            "repo": repo,
            "why": why,
            "how": str(adopt.get("how") or "").strip(),
            "license": str(adopt.get("license") or "").strip(),
            "risk": str(adopt.get("risk") or "unknown").strip(),
            "source_refs": verified_refs[:3],
        }
        payload = json.dumps(record, ensure_ascii=False)
        try:
            with self.db.connect() as conn:
                existing = conn.execute(
                    """SELECT id FROM knowledge_entries
                       WHERE category = ? AND trigger = ?
                         AND status IN ('candidate', 'confirmed', 'rejected', 'failed')
                       LIMIT 1""",
                    (_CATEGORY_ADOPT, repo),
                ).fetchone()
                if existing:
                    return False
                conn.execute(
                    """INSERT INTO knowledge_entries
                       (category, trigger, payload, confidence, status)
                       VALUES (?, ?, ?, ?, 'candidate')""",
                    (_CATEGORY_ADOPT, repo, payload, confidence),
                )
                conn.commit()
            logger.info("已登记自习接入候选 (待用户批准): %s", repo)
            return True
        except Exception as e:
            logger.debug("登记接入候选失败 (%s): %s", repo, e)
            return False

    # ─── 历史元笔记清理与状态查询 ────────────────────────────────

    def cleanup_stale_meta_notes(self) -> int:
        """把存量自习笔记中命中元笔记正则或内部异常标签污染的条目标为 status='stale'（保留不删，可回退）。"""
        marked = 0
        try:
            with self.db.connect() as conn:
                rows = conn.execute(
                    """SELECT id, category, trigger, payload FROM knowledge_entries
                       WHERE category IN (?, ?, ?) AND status = 'confirmed'""",
                    (_CATEGORY, _CATEGORY_PAPER, _CATEGORY_REPO),
                ).fetchall()
                stale_ids: list[int] = []
                for r in rows:
                    cat = str(r["category"] or "")
                    trig = str(r["trigger"] or "")
                    pay = str(r["payload"] or "")
                    if cat == _CATEGORY and is_meta_note(pay):
                        stale_ids.append(int(r["id"]))
                    elif trig and not is_valid_study_topic(trig, source="seed"):
                        stale_ids.append(int(r["id"]))
                if stale_ids:
                    placeholders = ",".join("?" for _ in stale_ids)
                    conn.execute(
                        f"UPDATE knowledge_entries SET status = 'stale' WHERE id IN ({placeholders})",
                        tuple(stale_ids),
                    )
                    self.db.sweep_orphan_fts_in_conn(conn)
                    conn.commit()
                    marked = len(stale_ids)
        except Exception as e:
            logger.warning("清理历史元笔记失败: %s", e)
        return marked

    def pending_adopt_candidates(self, limit: int = 10) -> list[dict[str, Any]]:
        """返回待用户批准的开源项目接入候选列表 (status='candidate')。"""
        out: list[dict[str, Any]] = []
        try:
            with self.db.connect() as conn:
                rows = conn.execute(
                    """SELECT id, trigger, payload, confidence, created_at
                       FROM knowledge_entries
                       WHERE category = ? AND status = 'candidate'
                       ORDER BY id DESC LIMIT ?""",
                    (_CATEGORY_ADOPT, max(1, int(limit))),
                ).fetchall()
            for r in rows:
                raw_payload = str(r["payload"] or "")
                try:
                    parsed = json.loads(raw_payload) if raw_payload.startswith("{") else {}
                except Exception:
                    parsed = {}
                out.append(
                    {
                        "id": int(r["id"]),
                        "repo": str(r["trigger"] or parsed.get("repo") or ""),
                        "why": str(parsed.get("why") or raw_payload[:120]),
                        "how": str(parsed.get("how") or ""),
                        "license": str(parsed.get("license") or ""),
                        "risk": str(parsed.get("risk") or ""),
                        "source_refs": parsed.get("source_refs") or [],
                        "confidence": float(r["confidence"] or 0.0),
                        "created_at": str(r["created_at"] or ""),
                    }
                )
        except Exception:
            logger.debug("读取 pendingAdopt 失败", exc_info=True)
        return out

    def inspect_adopt_candidate(self, candidate_ref: int | str) -> dict[str, Any] | None:
        """查询单个 study_adopt 候选方案并生成执行前展示信息（来源、开源协议、变更范围与风险）。"""
        ref_str = str(candidate_ref or "").strip()
        if not ref_str:
            return None
        with self.db.connect() as conn:
            if ref_str.isdigit():
                row = conn.execute(
                    """SELECT id, trigger, payload, confidence, status, created_at
                       FROM knowledge_entries
                       WHERE category = ? AND id = ?""",
                    (_CATEGORY_ADOPT, int(ref_str)),
                ).fetchone()
            else:
                row = conn.execute(
                    """SELECT id, trigger, payload, confidence, status, created_at
                       FROM knowledge_entries
                       WHERE category = ? AND (trigger = ? OR trigger LIKE ?)
                       ORDER BY id DESC LIMIT 1""",
                    (_CATEGORY_ADOPT, ref_str, f"%{ref_str}%"),
                ).fetchone()
        if not row:
            return None
        raw_payload = str(row["payload"] or "")
        try:
            parsed = json.loads(raw_payload) if raw_payload.startswith("{") else {}
        except Exception:
            parsed = {}
        repo = str(row["trigger"] or parsed.get("repo") or "").strip()
        why = str(parsed.get("why") or raw_payload[:120]).strip()
        how = str(parsed.get("how") or "").strip()
        lic = str(parsed.get("license") or "unknown").strip()
        risk = str(parsed.get("risk") or "unknown").strip()
        refs = parsed.get("source_refs") if isinstance(parsed.get("source_refs"), list) else []
        change_scope = (
            f"接入目标：{repo}；拟变更范围：{how or '作为参考架构/插件能力登记至已验证知识库'}；"
            f"写入表：knowledge_entries(category='study_adopt')"
        )
        preview_text = (
            f"【study_adopt 候选方案变更预览】\n"
            f"- 仓库/项目：{repo} (ID={row['id']}, 当前状态={row['status']})\n"
            f"- 来源链接：{', '.join(str(x) for x in refs) if refs else '本地自习记录'}\n"
            f"- 开源协议：{lic}\n"
            f"- 风险评估：{risk}\n"
            f"- 变更范围：{change_scope}\n"
            f"- 接入理由：{why}"
        )
        return {
            "id": int(row["id"]),
            "repo": repo,
            "status": str(row["status"] or "candidate"),
            "why": why,
            "how": how,
            "license": lic,
            "risk": risk,
            "source_refs": refs,
            "confidence": float(row["confidence"] or 0.0),
            "created_at": str(row["created_at"] or ""),
            "change_scope": change_scope,
            "preview_text": preview_text,
            "raw_record": parsed,
        }

    @staticmethod
    def _default_adopt_smoke_test(info: dict[str, Any]) -> tuple[bool, str]:
        """默认冒烟与安全验证：拦截危险命令/恶意脚本与无来源方案。"""
        repo = str(info.get("repo") or "").strip()
        why = str(info.get("why") or "").strip()
        how = str(info.get("how") or "").strip()
        risk = str(info.get("risk") or "").strip().lower()
        if not repo or not why:
            return False, "候选方案缺少仓库名或接入理由"
        if risk in ("critical", "high_malware", "forbidden", "高危恶意"):
            return False, f"候选方案风险等级过高 ({risk})，冒烟安全测试拒绝通过"
        danger_re = re.compile(
            r"(?:rm\s+-rf|del\s+/[fsq]|format\s+[a-z]:|Invoke-Expression|IEX\s*\(|"
            r"curl\s+[^|]+\|\s*(?:sh|bash)|powershell\s+-enc)",
            re.I,
        )
        if danger_re.search(f"{why} {how}"):
            return False, "候选方案接入说明中包含高危执行命令，冒烟验证拦截"
        return True, f"冒烟验证通过（repo={repo}, license={info.get('license') or 'unknown'}）"

    def approve_adopt_candidate(
        self,
        candidate_ref: int | str,
        *,
        approved_by_user: bool = False,
        install_fn: Callable[[dict[str, Any]], tuple[bool, str]] | None = None,
        smoke_test_fn: Callable[[dict[str, Any]], tuple[bool, str]] | None = None,
    ) -> dict[str, Any]:
        """审批并落地 study_adopt 候选方案（含幂等保护、安装执行、冒烟测试、状态晋升与审计日志）。"""
        if not approved_by_user:
            return {
                "ok": False,
                "status": "unapproved",
                "message": "安全门禁拦截：未获得用户明确批准（approved_by_user=False），禁止自动安装或晋升候选方案。",
            }

        info = self.inspect_adopt_candidate(candidate_ref)
        if not info:
            return {
                "ok": False,
                "status": "not_found",
                "message": f"未找到 study_adopt 候选方案：{candidate_ref}",
            }

        entry_id = info["id"]
        repo = info["repo"]
        cur_status = info["status"]

        # 幂等保护：已确认方案不重复安装
        if cur_status == "confirmed":
            return {
                "ok": True,
                "status": "already_confirmed",
                "idempotent": True,
                "candidate_id": entry_id,
                "repo": repo,
                "preview": info["preview_text"],
                "message": f"候选方案「{repo}」此前已完成审批与验证（幂等保护：跳过重复安装）。",
            }
        if cur_status in ("rejected", "failed"):
            return {
                "ok": False,
                "status": cur_status,
                "idempotent": True,
                "candidate_id": entry_id,
                "repo": repo,
                "preview": info["preview_text"],
                "message": f"候选方案「{repo}」已处于 {cur_status} 状态，禁止重复混入安装。",
            }

        # 1. 执行可选安装器（如 SkillAcquirer 落地）
        install_msg = "无需额外插件文件写入（架构/方案级接入）"
        if install_fn is not None:
            try:
                inst_ok, inst_msg = install_fn(info)
            except Exception as ie:
                inst_ok, inst_msg = False, f"安装异常: {ie}"
            install_msg = inst_msg or ""
            if not inst_ok:
                self._mark_adopt_status(
                    entry_id,
                    repo,
                    new_status="failed",
                    extra_meta={"install_error": install_msg, "updated_at": datetime.now().isoformat(timespec="seconds")},
                    audit_action="study_adopt:install_failed",
                    audit_result=install_msg,
                    audit_ok=False,
                )
                return {
                    "ok": False,
                    "status": "install_failed",
                    "candidate_id": entry_id,
                    "repo": repo,
                    "preview": info["preview_text"],
                    "message": f"候选方案「{repo}」安装失败，已回退并标记为 failed：{install_msg}",
                }

        # 2. 执行冒烟测试与安全校验（默认 + 自定义验证器）
        def_ok, def_msg = self._default_adopt_smoke_test(info)
        if not def_ok:
            self._mark_adopt_status(
                entry_id,
                repo,
                new_status="failed",
                extra_meta={"verify_error": def_msg, "updated_at": datetime.now().isoformat(timespec="seconds")},
                audit_action="study_adopt:verify_failed",
                audit_result=def_msg,
                audit_ok=False,
            )
            return {
                "ok": False,
                "status": "verify_failed",
                "candidate_id": entry_id,
                "repo": repo,
                "preview": info["preview_text"],
                "message": f"候选方案「{repo}」冒烟安全验证未通过，禁止转为 confirmed：{def_msg}",
            }

        smoke_msg = def_msg
        if smoke_test_fn is not None:
            try:
                sm_ok, sm_msg = smoke_test_fn(info)
            except Exception as se:
                sm_ok, sm_msg = False, f"冒烟测试异常: {se}"
            if not sm_ok:
                self._mark_adopt_status(
                    entry_id,
                    repo,
                    new_status="failed",
                    extra_meta={"verify_error": sm_msg, "updated_at": datetime.now().isoformat(timespec="seconds")},
                    audit_action="study_adopt:verify_failed",
                    audit_result=sm_msg or "smoke test failed",
                    audit_ok=False,
                )
                return {
                    "ok": False,
                    "status": "verify_failed",
                    "candidate_id": entry_id,
                    "repo": repo,
                    "preview": info["preview_text"],
                    "message": f"候选方案「{repo}」冒烟测试未通过，禁止转为 confirmed：{sm_msg}",
                }
            smoke_msg = f"{def_msg}; {sm_msg}"

        # 3. 验证全部通过，正式转为 confirmed 并同步 FTS + 审计记录
        self._mark_adopt_status(
            entry_id,
            repo,
            new_status="confirmed",
            extra_meta={
                "approved_by_user": True,
                "approved_at": datetime.now().isoformat(timespec="seconds"),
                "install_msg": install_msg,
                "smoke_test": smoke_msg,
                "verified": True,
            },
            audit_action="study_adopt:approved",
            audit_result=f"{install_msg} | {smoke_msg}",
            audit_ok=True,
        )
        return {
            "ok": True,
            "status": "confirmed",
            "idempotent": False,
            "candidate_id": entry_id,
            "repo": repo,
            "preview": info["preview_text"],
            "smoke_test": smoke_msg,
            "message": f"已批准并验证通过「{repo}」，状态已更新为 confirmed。",
        }

    def reject_adopt_candidate(
        self,
        candidate_ref: int | str,
        *,
        reason: str = "用户拒绝接入",
    ) -> dict[str, Any]:
        """拒绝 study_adopt 候选方案，标记为 rejected 并保证后续不再重复混入推荐。"""
        info = self.inspect_adopt_candidate(candidate_ref)
        if not info:
            return {
                "ok": False,
                "status": "not_found",
                "message": f"未找到 study_adopt 候选方案：{candidate_ref}",
            }
        entry_id = info["id"]
        repo = info["repo"]
        if info["status"] == "rejected":
            return {
                "ok": True,
                "status": "already_rejected",
                "idempotent": True,
                "candidate_id": entry_id,
                "repo": repo,
                "message": f"候选方案「{repo}」此前已被拒绝（幂等保护）。",
            }
        self._mark_adopt_status(
            entry_id,
            repo,
            new_status="rejected",
            extra_meta={"rejected_reason": reason or "用户拒绝", "rejected_at": datetime.now().isoformat(timespec="seconds")},
            audit_action="study_adopt:rejected",
            audit_result=reason or "用户拒绝",
            audit_ok=True,
        )
        return {
            "ok": True,
            "status": "rejected",
            "idempotent": False,
            "candidate_id": entry_id,
            "repo": repo,
            "message": f"已拒绝候选方案「{repo}」（原因：{reason or '用户拒绝'}），后续不再重复推荐。",
        }

    def _mark_adopt_status(
        self,
        entry_id: int,
        repo: str,
        *,
        new_status: str,
        extra_meta: dict[str, Any],
        audit_action: str,
        audit_result: str,
        audit_ok: bool,
    ) -> None:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT trigger, payload, confidence FROM knowledge_entries WHERE id = ?",
                (int(entry_id),),
            ).fetchone()
            if not row:
                return
            raw_payload = str(row["payload"] or "")
            try:
                parsed = json.loads(raw_payload) if raw_payload.startswith("{") else {"why": raw_payload}
            except Exception:
                parsed = {"why": raw_payload}
            parsed.update(extra_meta)
            new_payload = json.dumps(parsed, ensure_ascii=False)
            new_conf = max(float(row["confidence"] or 0.7), 0.85) if new_status == "confirmed" else float(row["confidence"] or 0.5)
            conn.execute(
                """UPDATE knowledge_entries
                   SET payload = ?, status = ?, confidence = ?, last_used = datetime('now')
                   WHERE id = ?""",
                (new_payload, new_status, new_conf, int(entry_id)),
            )
            if new_status == "confirmed":
                fts_text = self.db._format_knowledge_content(str(row["trigger"] or repo), new_payload)
                self.db._fts_insert(conn, fts_text, "knowledge", int(entry_id))
            else:
                self.db._fts_delete(conn, "knowledge", int(entry_id))
            try:
                conn.execute(
                    "INSERT INTO task_history (action, result, success) VALUES (?, ?, ?)",
                    (f"{audit_action}:{repo}"[:120], (audit_result or "")[:200], 1 if audit_ok else 0),
                )
            except Exception:
                pass
            conn.commit()
        try:
            from .memory_audit import log_audit

            log_audit(
                query=f"{audit_action}:{repo}"[:200],
                injected_ids=[f"study_adopt:{entry_id}:{new_status}"],
                scores={"ok": 1.0 if audit_ok else 0.0},
                latency_ms=0.0,
                result_chars=len(audit_result or ""),
                db_path=getattr(self.db, "db_path", None) or getattr(self.db, "path", None),
            )
        except Exception:
            pass

    def recent_study_notes(self, limit: int = 5) -> list[dict[str, Any]]:
        """读取最近确认入库的自习笔记摘要（供工具返回体与状态面板展示）。"""
        out: list[dict[str, Any]] = []
        try:
            with self.db.connect() as conn:
                rows = conn.execute(
                    """SELECT id, category, trigger, payload, confidence, created_at
                       FROM knowledge_entries
                       WHERE category IN (?, ?) AND status = 'confirmed'
                       ORDER BY id DESC LIMIT ?""",
                    (_CATEGORY_PAPER, _CATEGORY_REPO, max(1, int(limit))),
                ).fetchall()
            for r in rows:
                out.append(
                    {
                        "id": int(r["id"]),
                        "category": str(r["category"] or ""),
                        "topic": str(r["trigger"] or ""),
                        "payload": str(r["payload"] or ""),
                        "confidence": float(r["confidence"] or 0.0),
                        "created_at": str(r["created_at"] or ""),
                    }
                )
        except Exception:
            logger.debug("读取 recent_study_notes 失败", exc_info=True)
        return out

    def status(self) -> dict[str, Any]:
        """当前自习状态（含最近结果、当日预算与待批接入候选）。"""
        budget_snap = self._make_budget().daily_snapshot()
        return {
            "enabled": self._enabled,
            "time": self._time,
            "focus": list(self._focus),
            "topics": list(self._topics),
            "ranToday": self._ran_today(),
            "inSession": self._session_lock.locked(),
            "lastResult": dict(self._last_result or {}),
            "budget": budget_snap,
            "recentNotes": self.recent_study_notes(limit=5),
            "pendingAdopt": self.pending_adopt_candidates(limit=10),
        }
