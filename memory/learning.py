"""自主学习服务 — 从对话中自动提取知识、学习行为模式、修正错误、压缩记忆"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
from datetime import date, datetime
from pathlib import Path
from typing import Callable

from .db import Database
from ..intent_utils import CLOSE_APP_OBSERVE_RE, OPEN_APP_OBSERVE_RE

logger = logging.getLogger(__name__)

_SKIP_LEARN_TOOLS = frozenset({
    "remember",
    "forget",
    "cleanup_memory",
    "remember_fact",
    "remember_app",
    "upsert_core_memory",
    "forget_core_memory",
    "search_memory",
    "get_current_time",
    "get_system_status",
    "read_clipboard",
    "list_windows",
    "list_open_windows",
    "list_processes",
    "run_shell",
    "type_text",
    # GUI/观察类中间步骤：不能代表整句意图，学了会污染动作映射
    "gui_action",
    "click_text",
    "click_at",
    "hover_text",
    "send_keys",
    "mouse_click",
    "mouse_move",
    "mouse_scroll",
    "focus_window",
    "minimize_window",
    "read_screen",
    "ocr_screen",
    "ocr_find_text",
    "find_text_location",
    "take_screenshot",
    "uia_click_element",
    "uia_find_element",
    "list_workflows",
    "run_workflow",
    "save_workflow",
    "run_sequence",
    "virtual_click",
    "virtual_scroll",
    "check_occlusion",
    "scan_desktop",
    "inspect_window_som",
    "execute_gui_macro",
    "screenshot_window",
    "ocr_window",
    "wait_until_found",
    "wait_until_gone",
    "wait_for_stable_screen",
    "wait_for_file",
    "register_image",
    "find_image",
    "click_image",
    "click_and_type",
    "focus_and_hotkey",
    "fill_form",
    "click_menu_item",
})

# 学习触发阈值
_CONFIDENCE_BOOST = 0.15
_MIN_AUTO_FACT_CONFIDENCE = 0.3
_MIN_PATTERN_FREQUENCY = 2
_CONSOLIDATE_AFTER_TURNS = 20
# 记忆衰减：30 天未使用的 auto_facts 每天乘以 0.98，rule 类豁免
_FACT_DECAY_FACTOR = 0.98
_FACT_DECAY_IDLE_DAYS = 30
# 跨会话历史总结：累计多少轮触发一次、每次最多送多少轮给 LLM
_SUMMARIZE_AFTER_TURNS = 8
_SUMMARIZE_BATCH = 20
_DUE_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CLOSED_RE = re.compile(
    r"^\s*(?:[-*]\s*)?CLOSED\s*[:：]\s*(.+)$", re.IGNORECASE
)
_FACT_TYPE_CATEGORIES = {
    "承诺": "promise",
    "待办": "promise",
    "promise": "promise",
    "task": "promise",
    "偏好": "preference",
    "preference": "preference",
    "事实": "general",
    "习惯": "general",
    "fact": "general",
    "habit": "general",
    "推断": "inferred",
    "inferred": "inferred",
    "候选": "candidate",
    "candidate": "candidate",
}
_FACT_LINE_RE = re.compile(
    r"^(承诺|待办|偏好|事实|习惯|推断|候选|promise|task|preference|fact|habit|inferred|candidate)\s*[|｜]\s*(.+)$",
    re.IGNORECASE,
)
_FACT_PIPE_RE = re.compile(r"[|｜]")
_FACT_LIST_PREFIX_RE = re.compile(r"^[\d\.\-\*\s]+")
_REASONING_HINTS_RE = re.compile(
    r"(?:\bwe (?:need|extract|should|must)\b|\bthe user\b|\buser says\b|\bdialogue\b|"
    r"\bassistant\b|\bimportant (?:facts?|info)\b|\bno (?:important|clear) (?:facts?|info)\b|"
    r"\breturn empty\b|\binstruction\b|\bcriteria\b|\bbenchmark\b|\bprompt\b|"
    r"\bnot durable\b|\blet'?s\b|\bcould output\b|\bmaybe\b|\bhowever\b|\bfrom context\b|"
    r"\bpreferences?\b|\bcommitments?\b|\bhabits?\b|\bcontent\b|\bone fact per line\b|"
    r"\breturn only\b|\binput\s*:|\btask\s*:|没有重要信息|无重要信息|需要提取|"
    r"需要判断|是否重要|可能偏好|无需提取|未提及|没有提取到|未包含关于用户|"
    r"没有关于用户|对话中没有|对话中未包含|无[，,]因为|应返回空|只返回事实|"
    r"对话\s*[:：]|给定对话|我们需要|需要从用户消息|没有明确|可能输出空)",
    re.IGNORECASE,
)
_EMPTY_FACT_RE = re.compile(
    r"^[（(\[]?\s*(?:无重要信息|没有重要信息|无内容|无需记录|无|空|返回空|"
    r"empty|none|null|n/?a)(?:[，,、\s]*(?:返回空|无需记录|无内容|无))?"
    r"\s*[）)\]]?[。.]?$",
    re.IGNORECASE,
)
_EN_STOPWORDS_RE = re.compile(
    r"\b(?:the|this|that|with|from|for|are|was|were|has|have|had|not|but|however|"
    r"if|only|can|could|should|would|need|maybe|user|assistant|dialogue|content)\b",
    re.IGNORECASE,
)
_DIALOGUE_PREFIX_RE = re.compile(r"^(?:用户|助手|我|user|assistant)\s*[:：]", re.IGNORECASE)
_ASSISTANT_VOICE_RE = re.compile(
    r"^(?:你|您|如果|要是|要不要|我可以|我帮|我给|我直接)", re.IGNORECASE
)
_MAX_FACT_CHARS = 160

# ─── 语义门控规则（陈述/疑问/反问/否定/假设/引用/主体/不确定性）───
_CLAUSE_SPLIT_RE = re.compile(r"[\n。！？!?；;]+")
_QUESTION_END_RE = re.compile(r"(?:[？?]|(?:吗|呢|么)[？?]?)$")
_QUESTION_WORD_RE = re.compile(
    r"(?:什么|哪个|哪些|哪儿|哪里|怎么|怎样|如何|为什么|为何|干嘛|"
    r"是否|是不是|对不对|好不好|行不行|能不能|可不可以|要不要|有没有|"
    r"到底|究竟|难道|岂不是|何必|谁知道)"
)
_RHETORICAL_RE = re.compile(r"(?:难道|岂不是|凭什么|谁说|怎么可能|哪有|我像是.+的人吗)")
_HYPOTHESIS_RE = re.compile(
    r"(?:^|[，,\s])(?:如果|假如|假设|万一|要是|倘若|但凡|哪怕|就算|即便|设想|比方说|比如|例如)"
)
_QUOTE_OTHER_RE = re.compile(
    r'(?:他说|她说|他们说|别人说|朋友说|同事说|听说|据说|有人说|网上说|书上说|歌词写|'
    r'「[^」]*」|“[^”]*”|"[^"]*"|《[^》]*》)'
)
_UNCERTAIN_HEDGE_RE = re.compile(
    r"(?:可能|也许|大概|好像|似乎|差不多|估计|说不定|或许|大约|不太确定|我猜|看起来|应该吧)"
)
_NEGATED_SELF_CLAIM_RE = re.compile(
    r"^我(?:不是|并不是|不算|也没说我是|可没说|才不是|从来不是)"
)
_SECOND_THIRD_SUBJECT_RE = re.compile(
    r"^(?:你|您|他|她|它|他们|她们|别人|大家|有人|助手|科科斯)"
)
_ASSISTANT_SELF_RE = re.compile(
    r"(?:我帮你|我给你|我替你|我来帮|我已经帮|我已为您|我可以帮|我刚才帮|我为你|"
    r"你让我|你叫我|正如我|我是你的助手|我(?:是|叫|名叫)?(?:助手|AI助手|人工智能助手|机器人)(?:科科斯|克克斯|科克斯)?)"
)
_THIRD_PARTY_REPORT_RE = re.compile(
    r"^(?:小?[\u4e00-\u9fff]{1,4}|朋友|同事|家人|老板|老师|同学|邻居|网友|有人|别人)"
    r"(?:说|表示|告诉我|跟我说|觉得|认为|提到|写道|回复)"
)


def classify_utterance_semantics(clause: str) -> dict:
    """对单句做语义门控分析。

    返回字典：
    - modality: 'declarative' | 'interrogative' | 'rhetorical' | 'hypothetical' | 'quoted' | 'uncertain' | 'empty'
    - polarity: 'affirmative' | 'negative'
    - subject: 'user' | 'assistant_or_other' | 'unknown'
    - certainty: 'certain' | 'uncertain' | 'rejected'
    - reason: 判定依据说明
    """
    s = (clause or "").strip()
    if len(s) < 2:
        return {
            "modality": "empty",
            "polarity": "affirmative",
            "subject": "unknown",
            "certainty": "rejected",
            "reason": "empty",
        }
    if _RHETORICAL_RE.search(s):
        return {
            "modality": "rhetorical",
            "polarity": "negative",
            "subject": "user",
            "certainty": "rejected",
            "reason": "rhetorical_question",
        }
    if _QUESTION_END_RE.search(s) or "?" in s or "？" in s or _QUESTION_WORD_RE.search(s):
        return {
            "modality": "interrogative",
            "polarity": "affirmative",
            "subject": "unknown",
            "certainty": "rejected",
            "reason": "question",
        }
    if _HYPOTHESIS_RE.search(s):
        return {
            "modality": "hypothetical",
            "polarity": "affirmative",
            "subject": "user",
            "certainty": "uncertain",
            "reason": "hypothetical",
        }
    # 去掉《标题》后再判引用，避免「我喜欢《十年》」因书名号被误判为引用他人的话
    s_no_book = re.sub(r"《[^》]*》", "", s)
    if _QUOTE_OTHER_RE.search(s_no_book):
        return {
            "modality": "quoted",
            "polarity": "affirmative",
            "subject": "assistant_or_other",
            "certainty": "rejected",
            "reason": "quoted_or_hearsay",
        }
    if _THIRD_PARTY_REPORT_RE.match(s):
        return {
            "modality": "reported",
            "polarity": "affirmative",
            "subject": "assistant_or_other",
            "certainty": "uncertain",
            "reason": "third_party_report",
        }
    if _SECOND_THIRD_SUBJECT_RE.match(s) or _ASSISTANT_SELF_RE.search(s):
        return {
            "modality": "declarative",
            "polarity": "affirmative",
            "subject": "assistant_or_other",
            "certainty": "rejected",
            "reason": "non_user_subject",
        }
    if _NEGATED_SELF_CLAIM_RE.match(s):
        # 例如「我不是程序员」「我并不是上海人」：属于否定澄清，不能当作正面身份事实
        return {
            "modality": "declarative",
            "polarity": "negative",
            "subject": "user",
            "certainty": "uncertain",
            "reason": "negated_identity_or_claim",
        }
    polarity = (
        "negative"
        if any(neg in s for neg in ("不", "没", "别", "讨厌", "从不", "受不了", "拒绝"))
        else "affirmative"
    )
    if _UNCERTAIN_HEDGE_RE.search(s):
        return {
            "modality": "uncertain",
            "polarity": polarity,
            "subject": "user",
            "certainty": "uncertain",
            "reason": "hedged_statement",
        }
    subject = (
        "user"
        if ("我" in s or "用户" in s or s.startswith(("本人", "咱")))
        else "unknown"
    )
    certainty = "certain" if subject == "user" else "uncertain"
    return {
        "modality": "declarative",
        "polarity": polarity,
        "subject": subject,
        "certainty": certainty,
        "reason": "declarative_user_fact" if certainty == "certain" else "implicit_subject",
    }


def _looks_like_reasoning(text: str) -> bool:
    """判断一行是否像模型思考过程/对话转述，而非可入库事实。"""
    s = (text or "").strip()
    if not s:
        return True
    if _DIALOGUE_PREFIX_RE.match(s):
        return True
    if _EMPTY_FACT_RE.match(s):
        return True
    if _REASONING_HINTS_RE.search(s):
        return True
    if _ASSISTANT_VOICE_RE.match(s) and ("你" in s or "您" in s):
        return True
    en_words = re.findall(r"[A-Za-z]{2,}", s)
    return len(en_words) >= 3 and bool(_EN_STOPWORDS_RE.search(s))


class LearningService:
    """自主学习引擎，在每次对话后自动触发分析"""

    def __init__(
        self,
        db: Database,
        llm_chat: Callable | None = None,
        cfg: dict | None = None,
        memory=None,
    ):
        self.db = db
        self.llm_chat = llm_chat
        self._memory = memory
        cfg = cfg or {}
        self._enabled = bool(cfg.get("enabled", cfg.get("auto_learning", True)))
        self._auto_fact = bool(cfg.get("auto_fact", True))
        self._learn_patterns = bool(cfg.get("learn_patterns", True))
        self._error_correction = bool(cfg.get("error_correction", True))
        self._consolidation = bool(cfg.get("consolidation", True))
        self._consolidate_turns = int(cfg.get("consolidate_after_turns", _CONSOLIDATE_AFTER_TURNS))
        self._history_summary = bool(cfg.get("history_summary", True))
        self._summarize_after = max(2, int(cfg.get("summarize_after_turns", _SUMMARIZE_AFTER_TURNS)))
        self._summarize_batch = max(4, int(cfg.get("summarize_batch", _SUMMARIZE_BATCH)))
        self._async = bool(cfg.get("async", True))
        self._learning_lock = threading.Lock()
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        try:
            with self.db.connect() as conn:
                conn.executescript("""
                CREATE TABLE IF NOT EXISTS task_learning_records (
                    task_id TEXT PRIMARY KEY,
                    session_id TEXT DEFAULT 'default',
                    agent_id TEXT DEFAULT 'keks',
                    learned_at TEXT DEFAULT (datetime('now'))
                );
                CREATE INDEX IF NOT EXISTS idx_task_learning_task ON task_learning_records(task_id);
                """)
                conn.commit()
        except Exception as e:
            logger.warning("初始化 task_learning_records 失败: %s", e)

    def has_learned_task(self, task_id: str) -> bool:
        if not task_id:
            return False
        try:
            with self.db.connect() as conn:
                row = conn.execute(
                    "SELECT 1 FROM task_learning_records WHERE task_id = ?",
                    (str(task_id),),
                ).fetchone()
                return row is not None
        except Exception:
            return False

    def record_learned_task(
        self, task_id: str, session_id: str = "default", agent_id: str = "keks"
    ) -> None:
        if not task_id:
            return
        try:
            with self.db.connect() as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO task_learning_records (task_id, session_id, agent_id) VALUES (?, ?, ?)",
                    (str(task_id), str(session_id), str(agent_id)),
                )
                conn.commit()
        except Exception as e:
            logger.warning("记录任务学习状态失败 task_id=%s: %s", task_id, e)

    # ─── 公开 API：每次对话后调用 ────────────────────────────────

    def on_conversation_end(
        self,
        user_text: str,
        reply_text: str,
        session_id: str = "default",
        agent_id: str = "keks",
        tool_results: list[dict] | None = None,
        task_id: str | None = None,
    ):
        """对话结束后，异步触发学习流程"""
        if not self._enabled:
            return
        if task_id and self.has_learned_task(task_id):
            logger.info("任务交互已学习，跳过重复处理 task_id=%s", task_id)
            return

        def _learn():
            if not self._learning_lock.acquire(blocking=False):
                return
            try:
                if task_id and self.has_learned_task(task_id):
                    return
                self._learn_from_turn(user_text, reply_text, session_id, agent_id, tool_results)
                if task_id:
                    self.record_learned_task(task_id, session_id=session_id, agent_id=agent_id)
            except Exception as e:
                logger.warning("自主学习异常: %s", e)
            finally:
                self._learning_lock.release()

        if self._async:
            t = threading.Thread(target=_learn, daemon=True)
            t.start()
        else:
            _learn()

    def on_task_completion(
        self,
        user_text: str,
        reply_text: str,
        session_id: str = "default",
        agent_id: str = "keks",
        tool_results: list[dict] | None = None,
        task_id: str | None = None,
        status: str = "unknown",
        evidence: dict | None = None,
    ) -> None:
        """Learn from task tool outcomes without re-digesting the same dialogue.

        Objective capability calibration is handled from the verifier payload by
        Runtime. This path only updates existing tool correction evidence and
        task deduplication state, avoiding keyword-based fact extraction from
        task goals or verifier prose. Errors propagate so the outbox can retry.
        """
        if not self._enabled:
            return
        with self._learning_lock:
            if task_id and self.has_learned_task(task_id):
                logger.info("任务反馈已学习，跳过重复处理 task_id=%s", task_id)
                return
            self._learn_from_turn(
                user_text, reply_text, session_id, agent_id, tool_results,
                update_cognition=False,
            )
            self._synthesize_execution_policies(
                user_text, tool_results or [], task_id=task_id,
                verified=(status == "success" and bool((evidence or {}).get("verified"))),
            )
            self._update_adopted_policy_outcomes(
                tool_results or [], task_id=task_id, status=status, evidence=evidence or {}
            )
            if task_id:
                self.record_learned_task(task_id, session_id=session_id, agent_id=agent_id)

    @staticmethod
    def _policy_terms(text: str) -> set[str]:
        raw = re.sub(r"[^\w\u4e00-\u9fff]+", " ", str(text or "").lower())
        terms = set(re.findall(r"[a-z0-9_]{2,}", raw))
        for segment in re.findall(r"[\u4e00-\u9fff]{2,}", raw):
            for width in (2, 3, 4):
                terms.update(segment[i:i + width] for i in range(max(0, len(segment) - width + 1)))
        return {t for t in terms if t not in {"帮我", "一下", "然后", "这个", "那个", "怎么", "打开", "关闭"}}

    @staticmethod
    def _policy_task_type(text: str) -> str:
        raw = str(text or "").lower()
        for kind, cues in {
            "windows": ("窗口", "应用", "软件", "打开", "关闭"),
            "file": ("文件", "目录", "文件夹", "路径"),
            "browser": ("网页", "浏览器", "网站"),
            "coding": ("代码", "项目", "编译", "测试", "仓库"),
        }.items():
            if any(cue in raw for cue in cues):
                return kind
        return "general"

    @staticmethod
    def _policy_failure_class(tool_result: dict) -> str:
        explicit = str(tool_result.get("failure_class") or "").strip().lower()
        if explicit in {"tool", "target", "environment", "verification", "unknown"}:
            return explicit
        text = str(tool_result.get("summary") or tool_result.get("text") or tool_result.get("error") or "").lower()
        if any(x in text for x in ("permission denied", "access denied", "权限不足", "网络", "network", "connection")):
            return "environment"
        if any(x in text for x in ("not found", "no such", "未找到", "不存在", "未匹配")):
            return "target"
        if any(x in text for x in ("verify", "verifier", "验证失败", "无法确认")):
            return "verification"
        if any(x in text for x in ("exception", "traceback", "importerror", "modulenotfound", "入口非法")):
            return "tool"
        return "unknown"

    def _synthesize_execution_policies(
        self, user_text: str, tool_results: list[dict], *, task_id: str | None, verified: bool
    ) -> int:
        """Persist observed failure→successful-alternative or parameter-retry rules in error_corrections."""
        failures = [i for i, item in enumerate(tool_results) if isinstance(item, dict) and not item.get("ok")]
        successes = [i for i, item in enumerate(tool_results) if isinstance(item, dict) and item.get("ok")]
        if not failures or not successes:
            return 0
        terms = sorted(self._policy_terms(user_text))[:40]
        applicability = json.dumps({"task_type": self._policy_task_type(user_text), "terms": terms}, ensure_ascii=False)
        written = 0
        with self.db.connect() as conn:
            for failed_index in failures:
                failed = tool_results[failed_index]
                original = str(failed.get("name") or failed.get("func") or "").strip()
                if not original:
                    continue
                args = failed.get("args") if isinstance(failed.get("args"), dict) else {}
                alternative = next((
                    tool_results[i] for i in successes
                    if i > failed_index
                    and (
                        str(tool_results[i].get("name") or tool_results[i].get("func") or "").strip() != original
                        or (tool_results[i].get("args") or {}) != args
                    )
                ), None)
                if alternative is None:
                    continue
                correction = str(alternative.get("name") or alternative.get("func") or "").strip()
                if not correction:
                    continue
                alt_args = alternative.get("args") if isinstance(alternative.get("args"), dict) else {}
                signature = self._signature(original, args)
                reason = str(failed.get("summary") or failed.get("text") or failed.get("error") or "失败原因未确定").strip()[:200]
                cause = self._policy_failure_class(failed)
                if correction == original:
                    strategy = (
                        f"相似目标中 {original} 曾因参数 {json.dumps(args, ensure_ascii=False)} 失败（{reason}），"
                        f"调整参数为 {json.dumps(alt_args, ensure_ascii=False)} 后重试成功；"
                        f"优先使用修正后的参数调用 {original}。"
                    )
                else:
                    strategy = (
                        f"相似目标中 {original} 曾失败（{reason}），随后 {correction} 成功；"
                        f"优先评估 {correction}，避免在相同条件下重复调用 {original}。"
                    )
                rows = conn.execute(
                    "SELECT id,correction_func,correction_strategy,policy_version,policy_state,verified_count,last_task_id,applicability_json "
                    "FROM error_corrections WHERE original_func=? AND error_signature=? ORDER BY policy_version DESC,id DESC",
                    (original, signature),
                ).fetchall()
                row = next(
                    (
                        r for r in rows
                        if r["policy_state"] != "tombstoned"
                        and str(r["correction_func"] or "") in (correction, "")
                    ),
                    None,
                )
                if row is None:
                    version = max((int(r["policy_version"] or 1) for r in rows), default=0) + 1
                    if verified:
                        conn.execute(
                            "UPDATE error_corrections SET policy_state='tombstoned',tombstoned_at=datetime('now') "
                            "WHERE original_func=? AND error_signature=? AND policy_state='active'",
                            (original, signature),
                        )
                    conn.execute(
                        "INSERT INTO error_corrections(error_pattern,tool_name,original_func,error_signature,correction_strategy,correction_func,success_count,fail_count,confidence,policy_state,policy_version,applicability_json,cause_class,verified_count,last_task_id) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (str(user_text or "")[:100], original, original, signature, strategy, correction,
                         1 if verified else 0, 0, 0.7 if verified else 0.4,
                         "active" if verified else "candidate", version, applicability, cause,
                         1 if verified else 0, str(task_id or "")),
                    )
                else:
                    if task_id and str(row["last_task_id"] or "") == str(task_id):
                        continue
                    old_terms = {}
                    try:
                        old_terms = json.loads(next((r["applicability_json"] for r in rows if r["id"] == row["id"]), "{}") or "{}")
                    except (TypeError, json.JSONDecodeError):
                        pass
                    existing_terms = set(old_terms.get("terms") or [])
                    merged_terms = sorted(existing_terms if row["policy_state"] == "active" else existing_terms | set(terms))[:40]
                    merged = json.dumps({"task_type": self._policy_task_type(user_text), "terms": merged_terms}, ensure_ascii=False)
                    count = int(row["verified_count"] or 0) + int(verified)
                    policy_state = "active" if count > 0 else str(row["policy_state"] or "candidate")
                    old_strat = str(row["correction_strategy"] or "")
                    strategy_text = (
                        old_strat
                        if (row["policy_state"] == "active" and old_strat and "待 R3 反思" not in old_strat)
                        else strategy
                    )
                    conn.execute(
                        "UPDATE error_corrections SET correction_func=?,correction_strategy=?,applicability_json=?,cause_class=CASE WHEN cause_class='unknown' THEN ? ELSE cause_class END,verified_count=?,success_count=success_count+?,confidence=MIN(1.0,MAX(confidence,?)+?),policy_state=?,last_task_id=?,tombstoned_at=CASE WHEN ?='active' THEN '' ELSE tombstoned_at END,updated_at=datetime('now') WHERE id=?",
                        (correction, strategy_text, merged, cause, count, int(verified), 0.65 if verified else 0.0, 0.1 if verified else 0.0,
                         policy_state, str(task_id or ""), policy_state, row["id"]),
                    )
                    if verified:
                        conn.execute(
                            "UPDATE error_corrections SET policy_state='tombstoned',tombstoned_at=datetime('now') WHERE original_func=? AND error_signature=? AND id!=? AND policy_state='active'",
                            (original, signature, row["id"]),
                        )
                written += 1
            conn.commit()
        return written

    def get_execution_policies(
        self, intent: str, candidate_tools: list[str] | None = None, *, limit: int = 2, min_confidence: float = 0.6
    ) -> list[dict]:
        """Retrieve verified active policies, plus candidate reference policies with confidence >= 0.6."""
        candidates = None if candidate_tools is None else {str(x) for x in candidate_tools}
        query_terms = self._policy_terms(intent)
        if candidates == set() or not query_terms:
            return []
        eff_min_conf = max(0.6, float(min_confidence))
        with self.db.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM error_corrections
                   WHERE (
                       (policy_state = 'active' AND verified_count > 0 AND confidence >= ?)
                       OR (
                           COALESCE(policy_state, 'candidate') = 'candidate'
                           AND confidence >= ?
                           AND COALESCE(correction_strategy, '') != ''
                           AND INSTR(COALESCE(correction_strategy, ''), '待 R3 反思') = 0
                           AND COALESCE(fail_count, 0) <= COALESCE(success_count, 0) + 2
                       )
                   )
                   ORDER BY CASE WHEN policy_state = 'active' THEN 1 ELSE 0 END DESC,
                            confidence DESC, verified_count DESC
                   LIMIT 100""",
                (float(min_confidence), eff_min_conf),
            ).fetchall()
        ranked = []
        for raw in rows:
            item = dict(raw)
            is_active = str(item.get("policy_state") or "") == "active" and int(item.get("verified_count") or 0) > 0
            correction = str(item.get("correction_func") or "")
            original = str(item.get("original_func") or "")
            if candidates is not None:
                if is_active:
                    if correction not in candidates:
                        continue
                else:
                    # candidate 参考策略：允许匹配 correction_func 或 original_func
                    if correction and correction not in candidates and original not in candidates:
                        continue
            try:
                applicability = json.loads(item.get("applicability_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                applicability = {}
            terms = set(applicability.get("terms") or [])
            if not terms and not is_active:
                terms = self._policy_terms(
                    f"{item.get('error_pattern') or ''} {original} {correction} {item.get('correction_strategy') or ''}"
                )
            if not terms:
                continue
            overlap = len(query_terms & terms)
            coverage = overlap / max(1, min(len(query_terms), len(terms)))
            if overlap < min(2, len(terms)) or coverage < 0.2:
                continue
            item["is_candidate_reference"] = not is_active
            item["match_score"] = round(coverage * 0.7 + (0.3 if is_active else 0.15), 3)
            ranked.append(item)
        ranked.sort(
            key=lambda x: (
                0 if x.get("is_candidate_reference") else 1,
                x["match_score"],
                float(x.get("confidence") or 0),
                int(x.get("verified_count") or 0),
            ),
            reverse=True,
        )
        return ranked[:max(0, min(2, int(limit)))]

    @staticmethod
    def format_execution_policy_context(policies: list[dict]) -> str:
        if not policies:
            return ""
        active_lines = []
        candidate_lines = []
        for p in policies:
            if p.get("is_candidate_reference") or str(p.get("policy_state") or "active") == "candidate":
                candidate_lines.append(
                    f"- [candidate {p['id']} conf={float(p.get('confidence') or 0.6):.2f}] "
                    f"{p.get('correction_strategy', '')}（仅作参考，未验证，不可直接自动执行）"
                )
            else:
                active_lines.append(
                    f"- [policy {p['id']} v{p.get('policy_version', 1)}] {p.get('correction_strategy', '')}"
                )
        blocks = []
        if active_lines:
            blocks.append(
                "【已验证执行经验】仅供本轮规划参考；不得绕过工具权限、风险控制或结果验证。\n"
                + "\n".join(active_lines)
            )
        if candidate_lines:
            blocks.append(
                "【候选执行参考（未验证）】仅供本轮规划参考，不能直接获得执行权限；不得绕过权限校验、危险操作确认或结果验证。\n"
                + "\n".join(candidate_lines)
            )
        return "\n".join(blocks)

    def _update_adopted_policy_outcomes(
        self, tool_results: list[dict], *, task_id: str | None, status: str, evidence: dict
    ) -> int:
        if not task_id:
            return 0
        verified = status == "success" and bool(evidence.get("verified"))
        updated = 0
        with self.db.connect() as conn:
            for item in tool_results:
                if not isinstance(item, dict):
                    continue
                policy_ids = item.get("adopted_policy_ids") or []
                for policy_id in policy_ids:
                    row = conn.execute(
                        "SELECT id,policy_state,policy_version,adoption_success_count,adoption_fail_count,last_adopted_task_id "
                        "FROM error_corrections WHERE id=?",
                        (int(policy_id),),
                    ).fetchone()
                    versions = item.get("adopted_policy_versions") or {}
                    expected_version = versions.get(str(policy_id), versions.get(policy_id))
                    if not row or row["policy_state"] != "active" or row["last_adopted_task_id"] == str(task_id):
                        continue
                    if expected_version is not None and int(expected_version) != int(row["policy_version"] or 1):
                        continue
                    success = bool(item.get("ok")) and verified
                    failure = (
                        status not in {"cancelled", "timeout", "unknown"}
                        and not bool(evidence.get("unknown"))
                        and not bool(item.get("ok"))
                        and self._policy_failure_class(item) == "tool"
                    )
                    success_count = int(row["adoption_success_count"] or 0) + int(success)
                    fail_count = int(row["adoption_fail_count"] or 0) + int(failure)
                    confidence_delta = 0.05 if success else -0.1 if failure else 0.0
                    policy_state = "tombstoned" if fail_count >= 2 and fail_count > success_count else "active"
                    conn.execute(
                        "UPDATE error_corrections SET adoption_count=adoption_count+1,adoption_success_count=?,adoption_fail_count=?,confidence=MAX(0.05,MIN(1.0,confidence+?)),policy_state=?,tombstoned_at=CASE WHEN ?='tombstoned' THEN datetime('now') ELSE tombstoned_at END,last_adopted_task_id=?,last_feedback_task_id=?,updated_at=datetime('now') WHERE id=?",
                        (success_count, fail_count, confidence_delta, policy_state, policy_state, str(task_id), str(task_id), row["id"]),
                    )
                    updated += 1
            conn.commit()
        return updated

    def _learn_from_turn(
        self,
        user_text: str,
        reply_text: str,
        session_id: str,
        agent_id: str,
        tool_results: list[dict] | None,
        *,
        update_cognition: bool = True,
    ):
        if update_cognition:
            try:  # KEKESI_PCE_P2_20261101_V2
                from kekesi.memory.cognition import update_models
                try:
                    from kekesi.memory.perception import recent_perception_context
                    _p2_perc = recent_perception_context(10)
                except Exception:
                    _p2_perc = ""
                # 显式传入 user_text 与 reply_text，防止助手回复污染 user_model
                update_models(
                    f"用户：{user_text or ''}\n助手：{reply_text or ''}",
                    _p2_perc,
                    user_text=user_text or "",
                    reply_text=reply_text or "",
                )
            except Exception:
                pass
        try:  # KEKESI_PCE_P5_20261101
            from kekesi.memory.p5_hooks import p5_post_turn
            _p5_ctx = locals()
            p5_post_turn(_p5_ctx.get("tool_results"), _p5_ctx.get("user_text"), _p5_ctx.get("reply_text"))
        except Exception:
            pass
        if self._auto_fact:
            self._extract_facts(user_text, reply_text, session_id)
        if self._learn_patterns:
            self._observe_pattern(user_text, tool_results)
        if self._error_correction and tool_results:
            self._learn_from_errors(tool_results, user_text)
        if tool_results:
            self._remember_tool_actions(user_text, tool_results)
            self._record_workflow_candidate(user_text, tool_results)
        if self._consolidation:
            self._maybe_consolidate(session_id)

    def _remember_tool_actions(
        self, user_text: str, tool_results: list[dict] | None
    ) -> None:
        """对话结束后兜底：把成功工具调用写入知识库动作表。"""
        if not self._memory or not tool_results:
            return
        text = (user_text or "").strip()
        for tr in tool_results:
            if not tr.get("ok"):
                continue
            name = str(tr.get("name") or "").strip()
            if not text or not name or name in _SKIP_LEARN_TOOLS:
                continue
            if name.startswith("mcp__") or "→retry" in name:
                continue
            try:
                args = tr.get("args") if isinstance(tr.get("args"), dict) else {}
                self._memory.remember_action(text, name, args)
                logger.info("已学习动作映射: %s → %s", text[:40], name)
            except Exception as e:  # noqa: BLE001
                logger.warning("学习动作映射失败: %s", e)
            break

    def _record_workflow_candidate(
        self, user_text: str, tool_results: list[dict] | None
    ) -> None:
        """多步成功序列记入候选工作流，供 evolver 提升。"""
        if not tool_results:
            return
        try:
            from ..tools.workflow_ops import record_tool_sequence

            record_tool_sequence(
                self.db, tool_results, trigger_hint=(user_text or "").strip()
            )
        except Exception as e:  # noqa: BLE001
            logger.debug("记录工作流候选失败: %s", e)

    # ─── 1. 自动事实提取 ────────────────────────────────────────

    def _extract_facts(self, user_text: str, reply_text: str, session_id: str):
        u_clean = (user_text or "").strip()
        if not u_clean:
            return
        facts = self._extract_facts_via_llm(u_clean, reply_text)
        if not facts:
            facts = self._extract_facts_via_rules(u_clean)

        for item in facts:
            # 来源隔离：过滤掉把助手自身表达当作用户事实的提取项
            if self._is_reply_only_contamination(item, u_clean, reply_text):
                logger.debug("拦截疑似源自助手回复的污染事实: %s", item.get("fact"))
                continue
            self._store_or_reconcile_fact(item, session_id)

    @staticmethod
    def _is_reply_only_contamination(item: dict, user_text: str, reply_text: str) -> bool:
        """判断提取出的事实是否实际来自助手回复而非用户陈述。"""
        fact = (item.get("fact") or "").strip()
        if not fact:
            return True
        cat = str(item.get("category") or "general")
        # 承诺类允许助手或用户双方约定（如「周四前把报告发给用户」）
        if cat == "promise":
            return False
        if _ASSISTANT_SELF_RE.search(fact):
            return True
        r_text = (reply_text or "").strip()
        u_text = (user_text or "").strip()
        if not r_text:
            return False
        # 去前缀「用户」后若整句只出现在 reply_text 而与 user_text 毫无重合，判定为回复污染
        core = re.sub(r"^(?:用户|我)", "", fact).strip(" ：:，,。")
        if len(core) >= 4 and core in r_text and core not in u_text:
            # 进一步检查单字重合度（去掉常见虚词）
            stop_chars = set("的了是在有和与及或把被让给向从到对为以于上下中不没也很更最都只就还又再才")
            u_chars = {c for c in u_text if "\u4e00" <= c <= "\u9fff" and c not in stop_chars}
            c_chars = {c for c in core if "\u4e00" <= c <= "\u9fff" and c not in stop_chars}
            if c_chars and not (c_chars & u_chars):
                return True
        return False

    def _extract_facts_via_llm(self, user_text: str, reply_text: str) -> list[dict]:
        if not self.llm_chat:
            return []

        prompt = (
            "从以下【用户发言】中提取关于用户的事实、偏好、习惯和承诺。\n"
            "【助手回复】仅作上下文参考（用于消解代词或确认承诺），严禁把助手自身的属性、能力或观点当成用户事实！\n"
            "每条一行，格式：类型|截止日期|内容\n"
            "- 类型：承诺 / 偏好 / 事实 / 推断 / 候选\n"
            "  * 用户明确肯定的陈述用「事实」或「偏好」\n"
            "  * 带有“可能/大概/也许/如果”等不确定或假设语义的用「候选」或「推断」\n"
            "  * 疑问句、反问句、引用他人的话一律不要提取\n"
            "- 截止日期：仅承诺填写，格式 YYYY-MM-DD；对话没有明确日期就留空\n"
            "- 内容：简短明确，不要推测\n"
            "示例：承诺|2026-10-26|周四前把报告发给用户\n"
            "没有重要信息就返回空。\n\n"
            f"【用户发言】（唯一事实来源）：{user_text}\n"
            f"【助手回复】（仅上下文参考，勿当用户事实）：{reply_text}"
        )

        try:
            result = self.llm_chat(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "你是一个信息提取助手。只从【用户发言】中提取用户相关的事实、偏好、习惯与承诺；"
                            "绝不把助手的自我表述当作用户事实。"
                            "只按「类型|截止日期|内容」格式每行输出一条，不要编号、不要解释。"
                            "没有重要信息就返回空。"
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                tools=None,
            )
            if not result:
                return []
            content = (result.get("content") or "").strip()
            if not content:
                return []
            return self._parse_fact_lines(content)[:5]
        except Exception as e:
            logger.debug("LLM 事实提取失败: %s", e)
            return []

    @staticmethod
    def _parse_fact_lines(content: str) -> list[dict]:
        """只接受「类型|截止日期|内容」结构行，思考过程与对话原文一律丢弃。"""
        facts: list[dict] = []
        seen: set[str] = set()
        for raw in (content or "").splitlines():
            line = _FACT_LIST_PREFIX_RE.sub("", raw.strip()).strip()
            if line.startswith(("|", "｜")):
                line = line.strip("|｜ ").strip()
            if len(line) < 4:
                continue
            m = _FACT_LINE_RE.match(line)
            if not m:
                logger.debug("丢弃非结构化事实行: %s", line[:80])
                continue
            kind = m.group(1).lower()
            parts = [p.strip() for p in _FACT_PIPE_RE.split(m.group(2))]
            if len(parts) >= 2:
                due, fact = parts[0], "|".join(parts[1:]).strip()
            else:
                due, fact = "", parts[0].strip()
            if not (3 <= len(fact) <= _MAX_FACT_CHARS):
                continue
            if _looks_like_reasoning(fact):
                logger.debug("丢弃疑似推理内容: %s", fact[:80])
                continue
            # 语义门控二次校验：即便 LLM 误把问句/反问句输出为事实，也在此拦截或降级
            sem = classify_utterance_semantics(fact)
            if sem["modality"] in ("interrogative", "rhetorical", "quoted"):
                logger.debug("语义门控拦截非陈述提取行(%s): %s", sem["reason"], fact[:80])
                continue
            if fact in seen:
                continue
            seen.add(fact)
            category = _FACT_TYPE_CATEGORIES.get(kind, "general")
            item: dict = {
                "fact": fact,
                "category": category,
                "due_date": due if _DUE_DATE_RE.match(due) else None,
            }
            if category in ("inferred", "candidate") or (
                category != "promise" and sem["certainty"] == "uncertain"
            ):
                item["certainty"] = "uncertain"
                item["source_type"] = (
                    "inferred" if category == "inferred" else "candidate"
                )
            facts.append(item)
        return facts

    def _extract_facts_via_rules(self, user_text: str) -> list[dict]:
        """规则兜底提取：先做分句与语义门控，再匹配句式。

        - 疑问、反问、引用他人的话、非用户主语：直接丢弃，绝不入库；
        - 假设、带不确定修饰词（可能/也许/大概）、否定身份声明：标记为
          ``certainty='uncertain'``，走候选/推断存储，不直接写入确定性事实库；
        - 只有明确的第一人称陈述句才作为确定性事实提取。
        """
        facts: list[dict] = []
        seen: set[str] = set()
        pref_patterns = [
            (
                r"我(?:可能|也许|大概|好像|似乎|差不多|估计|说不定|或许|其实|一直|还是|真的|挺|很|非常|特别)?(喜欢|爱|习惯|一般|通常|经常|每天都|总是)(.+)",
                "preference",
            ),
            (
                r"我(?:可能|也许|大概|好像|似乎|差不多|估计|说不定|或许|其实|一直|还是|真的|挺|很|非常|特别)?(讨厌|不喜欢|从不|受不了)(.+)",
                "preference",
            ),
            (r"我的(.{1,20})(?:可能|也许|大概|好像|似乎)?是(.{1,30})", "general"),
            (r"我(?:可能|也许|大概|好像|似乎)?住[在的](.{1,20})", "general"),
            (r"我叫(.{1,10})", "general"),
            (r"我(?:可能|也许|大概|好像|似乎)?是(.{1,20})", "general"),
        ]
        for raw_clause in _CLAUSE_SPLIT_RE.split(user_text or ""):
            clause = raw_clause.strip()
            if len(clause) < 3:
                continue
            # 检查原始位置末尾是否紧跟问号（兼容分句切掉标点的情况）
            idx = (user_text or "").find(clause)
            tail_char = ""
            if idx >= 0 and idx + len(clause) < len(user_text or ""):
                tail_char = (user_text or "")[idx + len(clause)]
            probe = clause + (tail_char if tail_char in ("？", "?") else "")
            sem = classify_utterance_semantics(probe)
            if sem["certainty"] == "rejected":
                continue
            for pattern, default_cat in pref_patterns:
                m = re.search(pattern, clause)
                if not m:
                    continue
                fact = m.group(0).strip(" ，,。！？!?；;")
                if len(fact) < 3 or fact in seen:
                    continue
                # 二次对匹配出的片段做语义门控（防止复合句前半假设/后半匹配或句尾带“吗/呢”）
                sub_sem = classify_utterance_semantics(fact + (tail_char if tail_char in ("？", "?") else ""))
                if sub_sem["certainty"] == "rejected":
                    continue
                seen.add(fact)
                certainty = (
                    "uncertain"
                    if (sem["certainty"] == "uncertain" or sub_sem["certainty"] == "uncertain")
                    else "certain"
                )
                item: dict = {
                    "fact": fact,
                    "category": default_cat,
                    "due_date": None,
                    "certainty": certainty,
                    "source_role": "user",
                    "source_type": (
                        "rule_candidate" if certainty == "uncertain" else "rule_explicit"
                    ),
                }
                facts.append(item)
                if len(facts) >= 3:
                    return facts
        return facts[:3]

    def _store_or_reconcile_fact(self, item: dict, session_id: str):
        fact = (item.get("fact") or "").strip()
        if not fact:
            return
        category = str(item.get("category") or "general")
        certainty = str(item.get("certainty") or "certain")
        source_role = str(item.get("source_role") or "user")
        source_type = str(item.get("source_type") or "explicit")

        # 组装来源追溯标签
        trace_source = f"session:{session_id}|role:{source_role}|type:{source_type}"

        # 1) 推断类偏好：落入独立的 inferred_preferences 表，不混入确定性 auto_facts
        if category == "inferred" or (category == "preference" and certainty == "uncertain"):
            self._store_inferred_preference(
                fact,
                evidence=trace_source,
                confidence=0.45 if certainty == "uncertain" else 0.55,
            )
            return

        # 2) 不能确认的候选信息：进入候选记忆（status='candidate', confidence=0.25, verified=0），
        #    不参与默认确定性事实注入（_FACT_INJECTION_SQL 仅注入 status='open' 且 confidence>=0.3）
        if certainty == "uncertain" or category == "candidate":
            self._store_candidate_fact(
                fact,
                session_id,
                category="general" if category == "candidate" else category,
                source_role=source_role,
                source_type=source_type or "candidate",
            )
            return

        candidate = self.db.find_similar_fact(fact)
        if candidate:
            old_fact = str(candidate.get("fact") or "").strip()
            if fact == old_fact:
                self.boost_fact_confidence(old_fact)
                return
            decision = self._decide_fact_conflict(fact, old_fact)
            if decision == "REPLACE":
                self._supersede_fact(int(candidate["id"]), fact)
            elif decision == "KEEP_BOTH":
                pass
            else:
                # DUPLICATE 或无法判定：保守跳过，避免写入矛盾记录
                return
        self._store_auto_fact(
            fact,
            session_id,
            category=category,
            due_date=item.get("due_date"),
            source_role=source_role,
            source_type=source_type,
        )

    def _decide_fact_conflict(self, new_fact: str, old_fact: str) -> str:
        if not self.llm_chat:
            return ""
        try:
            result = self.llm_chat(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "你是记忆冲突判定器。比较旧记录与新事实，只输出一个词："
                            "REPLACE（新事实取代旧记录）、KEEP_BOTH（两者都成立）、"
                            "DUPLICATE（意思相同）。"
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"旧记录：{old_fact}\n新事实：{new_fact}\n"
                            "新事实与旧记录是否冲突？答 REPLACE / KEEP_BOTH / DUPLICATE"
                        ),
                    },
                ],
                tools=None,
            )
            content = (result.get("content") or "").strip().upper() if result else ""
            m = re.search(r"(REPLACE|KEEP_BOTH|DUPLICATE)", content)
            return m.group(1) if m else ""
        except Exception as e:
            logger.debug("事实冲突判定失败: %s", e)
            return ""

    def _supersede_fact(self, fact_id: int, new_fact: str):
        with self.db.connect() as conn:
            conn.execute(
                """UPDATE auto_facts SET status = 'superseded', last_used = datetime('now')
                   WHERE id = ?""",
                (fact_id,),
            )
            self.db._fts_delete(conn, "auto_fact", int(fact_id))
            conn.commit()
        logger.info("事实被新记录取代: id=%d → %s", fact_id, new_fact[:60])

    def _store_inferred_preference(
        self,
        fact: str,
        *,
        evidence: str = "",
        confidence: float = 0.5,
    ) -> None:
        """将推断性偏好写入独立的 inferred_preferences 表，并保留来源追溯。"""
        key = fact[:32]
        with self.db.connect() as conn:
            row = conn.execute(
                """SELECT id, confidence FROM inferred_preferences
                   WHERE preference_value = ?""",
                (fact,),
            ).fetchone()
            if row:
                conn.execute(
                    """UPDATE inferred_preferences
                       SET confidence = MIN(0.85, confidence + 0.08),
                           evidence = ?,
                           updated_at = datetime('now')
                       WHERE id = ?""",
                    (evidence, row["id"]),
                )
            else:
                conn.execute(
                    """INSERT INTO inferred_preferences
                       (preference_key, preference_value, evidence, confidence)
                       VALUES (?, ?, ?, ?)""",
                    (key, fact, evidence, float(confidence)),
                )
            conn.commit()
        logger.info("推断偏好记录: %s (%s)", fact, evidence)

    def _store_candidate_fact(
        self,
        fact: str,
        session_id: str,
        *,
        category: str = "general",
        source_role: str = "user",
        source_type: str = "candidate",
    ) -> None:
        """不能确认的信息进入候选记忆（status='candidate', confidence=0.25），
        若累计证据满足 merge.should_promote（>=3 次且无反例）则晋升为 open 事实。"""
        from .merge import should_promote

        with self.db.connect() as conn:
            cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(auto_facts)")}
            existing = conn.execute(
                "SELECT id, status, confidence, hit_count FROM auto_facts WHERE fact = ?",
                (fact,),
            ).fetchone()
            if existing:
                rid = int(existing["id"])
                new_hits = int(existing["hit_count"] or 1) + 1
                evidences = [f"{session_id}:{i}" for i in range(new_hits)]
                if existing["status"] == "candidate" and should_promote(evidences, has_counterexample=False):
                    conn.execute(
                        """UPDATE auto_facts
                           SET status = 'open',
                               confidence = MAX(confidence, 0.5),
                               hit_count = ?,
                               last_used = datetime('now')
                           WHERE id = ?""",
                        (new_hits, rid),
                    )
                    self.db._fts_insert(conn, fact, "auto_fact", rid)
                else:
                    conn.execute(
                        """UPDATE auto_facts
                           SET hit_count = ?,
                               last_used = datetime('now')
                           WHERE id = ?""",
                        (new_hits, rid),
                    )
            elif "source_role" in cols and "source_type" in cols:
                conn.execute(
                    """INSERT INTO auto_facts
                       (fact, category, source_session_id, confidence, verified, status,
                        source_role, source_type)
                       VALUES (?, ?, ?, 0.25, 0, 'candidate', ?, ?)""",
                    (fact, category, session_id, source_role, source_type),
                )
            else:
                conn.execute(
                    """INSERT INTO auto_facts
                       (fact, category, source_session_id, confidence, verified, status)
                       VALUES (?, ?, ?, 0.25, 0, 'candidate')""",
                    (fact, category, session_id),
                )
            conn.commit()
        logger.info("候选记忆暂存(待确认): %s", fact)

    def _store_auto_fact(
        self,
        fact: str,
        session_id: str,
        *,
        category: str = "general",
        due_date: str | None = None,
        source_role: str = "user",
        source_type: str = "explicit",
    ):
        with self.db.connect() as conn:
            cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(auto_facts)")}
            if "source_role" in cols and "source_type" in cols:
                cur = conn.execute(
                    """INSERT INTO auto_facts
                       (fact, category, source_session_id, confidence, status, due_date,
                        source_role, source_type)
                       VALUES (?, ?, ?, 0.6, 'open', ?, ?, ?)""",
                    (fact, category, session_id, due_date, source_role, source_type),
                )
            else:
                cur = conn.execute(
                    """INSERT INTO auto_facts
                       (fact, category, source_session_id, confidence, status, due_date)
                       VALUES (?, ?, ?, 0.6, 'open', ?)""",
                    (fact, category, session_id, due_date),
                )
            fact_id = int(cur.lastrowid)
            self.db._fts_insert(conn, fact, "auto_fact", fact_id)
            conn.commit()
        logger.info("自动记忆: %s", fact)

    # ─── 2. 行为模式学习 ─────────────────────────────────────────

    _PATTERN_TEMPLATES = [
        (OPEN_APP_OBSERVE_RE, "open_app"),
        (CLOSE_APP_OBSERVE_RE, "close_app"),
        (re.compile(r"搜(索|一下)(.{1,30})"), "search"),
        (re.compile(r"播放(.{1,20})"), "play_media"),
        (re.compile(r"音量(.{1,10})"), "volume"),
        (re.compile(r"截图"), "screenshot"),
        (re.compile(r"查(看|一下)(.{1,30})"), "check_status"),
    ]

    def _observe_pattern(self, user_text: str, tool_results: list[dict] | None):
        if not user_text or not tool_results:
            return

        now = datetime.now()
        hour = now.hour
        time_bucket = (
            "morning" if 6 <= hour < 12
            else "afternoon" if 12 <= hour < 18
            else "evening" if 18 <= hour < 22
            else "night"
        )

        for pattern, ptype in self._PATTERN_TEMPLATES:
            m = pattern.search(user_text)
            if m:
                trigger = m.group(0).strip()
                action = user_text.strip()
                self._upsert_pattern(ptype, trigger, action, time_bucket)

    def _upsert_pattern(
        self,
        pattern_type: str,
        trigger: str,
        action: str,
        time_bucket: str,
    ):
        with self.db.connect() as conn:
            row = conn.execute(
                """SELECT id, frequency, confidence FROM behavior_patterns
                   WHERE pattern_type = ? AND trigger_condition = ?""",
                (pattern_type, trigger),
            ).fetchone()

            if row:
                conn.execute(
                    """UPDATE behavior_patterns SET
                       frequency = ?, confidence = MIN(1.0, confidence + 0.05),
                       last_observed = datetime('now'),
                       time_bucket = COALESCE(NULLIF(time_bucket, ''), ?)
                       WHERE id = ?""",
                    (row["frequency"] + 1, time_bucket, row["id"]),
                )
            else:
                conn.execute(
                    """INSERT INTO behavior_patterns
                       (pattern_type, trigger_condition, expected_action, frequency,
                        confidence, time_bucket, auto_suggest)
                       VALUES (?, ?, ?, 1, 0.5, ?, 1)""",
                    (pattern_type, trigger, action, time_bucket),
                )
            conn.commit()

    def get_learned_patterns(
        self,
        min_confidence: float = 0.6,
        limit: int = 10,
    ) -> list[dict]:
        with self.db.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM behavior_patterns
                   WHERE confidence >= ? AND frequency >= ?
                   ORDER BY confidence DESC, frequency DESC
                   LIMIT ?""",
                (min_confidence, _MIN_PATTERN_FREQUENCY, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    # ─── 3. 错误修正学习 ─────────────────────────────────────────

    def _learn_from_errors(self, tool_results: list[dict], user_text: str):
        for tr in tool_results:
            if tr.get("ok"):
                self._reinforce_success(tr.get("name", ""), user_text)
            else:
                self._record_failure(tr.get("name", ""), user_text, tr.get("args", {}))

    def _reinforce_success(self, tool_name: str, user_text: str):
        with self.db.connect() as conn:
            rows = conn.execute(
                """SELECT id, correction_strategy, confidence FROM error_corrections
                   WHERE tool_name = ?""",
                (tool_name,),
            ).fetchall()
            for row in rows:
                conn.execute(
                    """UPDATE error_corrections SET
                       success_count = success_count + 1,
                       confidence = MIN(1.0, confidence + 0.1),
                       updated_at = datetime('now')
                       WHERE id = ?""",
                    (row["id"],),
                )
            conn.commit()

    def _record_failure(self, tool_name: str, user_text: str, args: dict, *, reason: str = ""):
        """记录工具失败。

        零硬编码：不写死"工具A失败就换工具B"的修正字典。
        correction_strategy 留空，由 R3 replan 成功后用 LLM 动态生成的
        reason 回填（见 record_correction_outcome）。
        首次记录时若 R1 提供了失败原因，作为初始 strategy 文本备查。

        防污染：若该 signature 已通过 record_correction_outcome 记录过成功修正策略
        （success_count > 0），说明 R2 闭环已正确归因，此处跳过 fail_count++，
        避免 router._finish 异步触发的 _learn_from_errors 重复污染失败计数。
        """
        sig = self._signature(tool_name, args)
        with self.db.connect() as conn:
            row = conn.execute(
                """SELECT id, fail_count, success_count FROM error_corrections
                   WHERE original_func = ? AND error_signature = ?""",
                (tool_name, sig),
            ).fetchone()

            if row:
                # 已有成功修正策略 → 跳过（避免 router._finish 重复污染）
                if row["success_count"] > 0:
                    return
                # 首次失败初始 confidence=0.4，后续失败下限 0.3（保证 _invoke_replan 阈值 0.3 可查到）
                conn.execute(
                    """UPDATE error_corrections SET
                       fail_count = fail_count + 1,
                       confidence = MAX(0.3, confidence - 0.1),
                       updated_at = datetime('now')
                       WHERE id = ?""",
                    (row["id"],),
                )
            else:
                # 首次失败：strategy 用 R1 失败原因占位，等 replan 成功后回填
                initial_strategy = reason or f"{tool_name} 执行失败，待 R3 反思后补充修正策略"
                conn.execute(
                    """INSERT INTO error_corrections
                       (error_pattern, tool_name, original_func, error_signature,
                        correction_strategy, correction_func, confidence)
                       VALUES (?, ?, ?, ?, ?, '', 0.4)""",
                    (user_text[:100], tool_name, tool_name, sig, initial_strategy),
                )
            conn.commit()

    @staticmethod
    def _signature(tool_name: str, args: dict) -> str:
        """生成失败模式签名：func + 关键 args（去值留键，避免参数值不同建多行）。"""
        keys = sorted(args.keys()) if isinstance(args, dict) else []
        return f"{tool_name}({','.join(keys)})"

    def get_error_corrections(
        self,
        tool_name: str | None = None,
        min_confidence: float = 0.4,
    ) -> list[dict]:
        """查询历史失败教训。

        tool_name 现按 original_func 匹配（原失败工具），更符合"这工具失败过"的语义。
        返回字段含 original_func / correction_func / success_count / fail_count，
        调用方可据此计算策略成功率 success_count / (success+fail)。
        """
        with self.db.connect() as conn:
            if tool_name:
                rows = conn.execute(
                    """SELECT * FROM error_corrections
                       WHERE original_func = ? AND confidence >= ?
                       ORDER BY confidence DESC LIMIT 5""",
                    (tool_name, min_confidence),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT * FROM error_corrections
                       WHERE confidence >= ?
                       ORDER BY confidence DESC LIMIT 10""",
                    (min_confidence,),
                ).fetchall()
            return [dict(r) for r in rows]

    def record_correction_outcome(
        self,
        *,
        original_func: str,
        original_args: dict,
        correction_func: str,
        correction_strategy: str,
        success: bool,
        user_text: str = "",
        failure_reason: str = "",
        correction_args: dict | None = None,
        verified: bool = True,
        verifier_evidence: str = "",
        task_id: str | None = None,
    ) -> dict | None:
        """记录一次"原失败→修正策略→执行结果（含 verifier 验证证据）"的完整闭环。

        这是学习策略的核心入口：
        - success=True 且 verified=True：修正策略经 verifier 确认奏效，success_count++，
          verified_count++，confidence 上调，替换占位文本并回填 applicability_json。
        - success=True 但 verified=False：工具返回成功但 verifier 尚未确认，仅记录候选参考策略，
          不虚增 verified_count，也不直接升为 active。
        - success=False：修正策略失败，fail_count++，confidence 下调。
        """
        sig = self._signature(original_func, original_args or {})
        terms = sorted(self._policy_terms(f"{user_text} {original_func} {correction_func} {failure_reason}"))[:40]
        cause = self._policy_failure_class({"summary": failure_reason})
        if not correction_strategy and success:
            if correction_func == original_func and correction_args is not None:
                correction_strategy = (
                    f"相似目标中 {original_func} 曾因参数 {json.dumps(original_args or {}, ensure_ascii=False)} 失败"
                    f"（{failure_reason or '执行异常'}），调整参数为 {json.dumps(correction_args, ensure_ascii=False)} 后重试成功"
                    + (f"（验证：{verifier_evidence[:80]}）" if verifier_evidence else "")
                    + f"；优先使用修正后的参数调用 {original_func}。"
                )
            else:
                correction_strategy = (
                    f"相似目标中 {original_func} 曾失败（{failure_reason or '执行异常'}），"
                    f"随后改用 {correction_func} 成功"
                    + (f"（验证：{verifier_evidence[:80]}）" if verifier_evidence else "")
                    + f"；优先评估 {correction_func}。"
                )

        with self.db.connect() as conn:
            row = conn.execute(
                """SELECT id, success_count, fail_count, confidence, correction_strategy,
                          policy_state, verified_count, applicability_json, last_task_id
                   FROM error_corrections
                   WHERE original_func = ? AND error_signature = ? AND COALESCE(policy_state, 'candidate') != 'tombstoned'
                   ORDER BY id DESC LIMIT 1""",
                (original_func, sig),
            ).fetchone()
            if not row:
                row = conn.execute(
                    """SELECT id, success_count, fail_count, confidence, correction_strategy,
                              policy_state, verified_count, applicability_json, last_task_id
                       FROM error_corrections
                       WHERE original_func = ? AND COALESCE(policy_state, 'candidate') = 'candidate'
                         AND (error_pattern = ? OR INSTR(COALESCE(correction_strategy, ''), '待 R3 反思') > 0)
                       ORDER BY id DESC LIMIT 1""",
                    (original_func, (user_text or "")[:100]),
                ).fetchone()

            if row:
                if task_id and str(row["last_task_id"] or "") == str(task_id) and int(row["success_count"] or 0) > 0:
                    return dict(row)
                old_app = {}
                try:
                    old_app = json.loads(row["applicability_json"] or "{}")
                except Exception:
                    old_app = {}
                merged_terms = sorted(set(old_app.get("terms") or []) | set(terms))[:40]
                applicability = json.dumps(
                    {
                        "task_type": self._policy_task_type(user_text or row["correction_strategy"] or ""),
                        "terms": merged_terms,
                        "verifier_evidence": str(verifier_evidence or "")[:120],
                    },
                    ensure_ascii=False,
                )
                if success:
                    strategy_field = correction_strategy or row["correction_strategy"]
                    if verified:
                        new_vcount = int(row["verified_count"] or 0) + 1
                        new_scount = int(row["success_count"] or 0) + 1
                        new_conf = min(1.0, max(float(row["confidence"] or 0.4), 0.6) + 0.15)
                        new_state = "active"
                    else:
                        # 工具返回成功但 verifier 未确认：保留 candidate，不计入 verified_count
                        new_vcount = int(row["verified_count"] or 0)
                        new_scount = int(row["success_count"] or 0)
                        new_conf = min(0.6, max(float(row["confidence"] or 0.4), 0.5))
                        new_state = str(row["policy_state"] or "candidate")
                    conn.execute(
                        """UPDATE error_corrections SET
                           success_count = ?,
                           verified_count = ?,
                           correction_func = ?,
                           correction_strategy = ?,
                           confidence = ?,
                           policy_state = ?,
                           applicability_json = ?,
                           cause_class = CASE WHEN COALESCE(cause_class, 'unknown') = 'unknown' THEN ? ELSE cause_class END,
                           last_task_id = COALESCE(?, last_task_id),
                           updated_at = datetime('now')
                           WHERE id = ?""",
                        (
                            new_scount,
                            new_vcount,
                            correction_func,
                            strategy_field,
                            new_conf,
                            new_state,
                            applicability,
                            cause,
                            str(task_id) if task_id else None,
                            row["id"],
                        ),
                    )
                else:
                    conn.execute(
                        """UPDATE error_corrections SET
                           fail_count = fail_count + 1,
                           confidence = MAX(0.05, confidence - 0.1),
                           updated_at = datetime('now')
                           WHERE id = ?""",
                        (row["id"],),
                    )
                target_id = int(row["id"])
            else:
                applicability = json.dumps(
                    {
                        "task_type": self._policy_task_type(user_text),
                        "terms": terms,
                        "verifier_evidence": str(verifier_evidence or "")[:120],
                    },
                    ensure_ascii=False,
                )
                is_verified_ok = bool(success and verified)
                cur = conn.execute(
                    """INSERT INTO error_corrections
                       (error_pattern, tool_name, original_func, error_signature,
                        correction_strategy, correction_func,
                        success_count, fail_count, confidence,
                        policy_state, policy_version, applicability_json,
                        cause_class, verified_count, last_task_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)""",
                    (
                        (user_text or failure_reason or original_func)[:100],
                        original_func,
                        original_func,
                        sig,
                        correction_strategy or f"由 {correction_func} 替代",
                        correction_func if success else "",
                        1 if is_verified_ok else 0,
                        0 if success else 1,
                        0.7 if is_verified_ok else (0.55 if success else 0.3),
                        "active" if is_verified_ok else "candidate",
                        applicability,
                        cause,
                        1 if is_verified_ok else 0,
                        str(task_id or ""),
                    ),
                )
                target_id = int(cur.lastrowid)
            conn.commit()
            updated = conn.execute("SELECT * FROM error_corrections WHERE id = ?", (target_id,)).fetchone()
            return dict(updated) if updated else None

    # ─── 4. 记忆压缩 ─────────────────────────────────────────────

    def _maybe_consolidate(self, session_id: str):
        if not self._history_summary:
            with self.db.connect() as conn:
                count = conn.execute(
                    "SELECT COUNT(*) AS c FROM conversation_digest WHERE session_id = ?",
                    (session_id,),
                ).fetchone()["c"]
            if count >= self._consolidate_turns:
                self.consolidate_session(session_id)
            return
        # 跨会话累计：不依赖单个 session（语音每次唤醒都会换 session_id）
        self.summarize_pending()

    def summarize_pending(self, *, force: bool = False) -> int:
        """把未总结的历史对话批量压缩进 consolidated_memory。

        与旧 consolidate_session 的区别：
        - 跨会话按时间顺序取，不受 session_id 轮换影响
        - 用 summarized 标记增量处理，总结后不删除原始对话
        - 返回本次总结的轮数（0 表示未触发/无数据）
        """
        batch = self._summarize_batch
        # 先原子「认领」一批未总结对话（summarized=2），避免多进程启动时重复总结
        claimed = []
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """SELECT id, session_id, user_text, reply_text, created_at
                   FROM conversation_digest WHERE summarized = 0
                   ORDER BY id ASC LIMIT ?""",
                (batch,),
            ).fetchall()
            from .merge import should_trigger, validate_batch

            pending = conn.execute(
                "SELECT COUNT(*) AS c FROM conversation_digest WHERE summarized = 0"
            ).fetchone()["c"]
            trigger_ok = force or pending >= self._summarize_after or should_trigger(
                pending, has_high_salience=False, idle_seconds=0
            )
            if rows and trigger_ok and (not force or len(rows) >= 2):
                conn.executemany(
                    "UPDATE conversation_digest SET summarized = 2 WHERE id = ?",
                    [(int(r["id"]),) for r in rows],
                )
                claimed = list(rows)
            conn.commit()

        if not claimed:
            return 0

        batch_items = [{"digest_id": int(r["id"]), "text": str(r["user_text"] or "")} for r in claimed]
        if not validate_batch(batch_items):
            return 0
        summary = self._summarize_history(claimed)
        ids = [int(r["id"]) for r in claimed]
        if not summary:
            with self.db.connect() as conn:
                conn.executemany(
                    "UPDATE conversation_digest SET summarized = 0 WHERE id = ?",
                    [(i,) for i in ids],
                )
                conn.commit()
            return 0

        sessions = sorted({str(r["session_id"] or "default") for r in claimed})
        with self.db.connect() as conn:
            from .vector_chunks import VectorChunkStore

            cur = conn.execute(
                """INSERT INTO consolidated_memory
                   (summary, source_session_ids, source_turn_count, memory_type, importance)
                   VALUES (?, ?, ?, 'conversation', 0.8)""",
                (summary, json.dumps(sessions, ensure_ascii=False), len(claimed)),
            )
            mem_id = int(cur.lastrowid)
            conn.executemany(
                "UPDATE conversation_digest SET summarized = 1 WHERE id = ?",
                [(i,) for i in ids],
            )
            VectorChunkStore.purge_for_sources(conn, "conversation", ids)
            self.db._fts_insert(conn, summary, "consolidated", mem_id)
            conn.commit()
        vector = getattr(self._memory, "vector", None) if self._memory else None
        if vector is not None:
            try:
                vector.enqueue_summary(mem_id, summary)
            except Exception:
                logger.debug("摘要切片入队失败", exc_info=True)
        logger.info("历史对话总结: %d 轮 → 摘要 id=%d", len(claimed), mem_id)
        return len(claimed)

    def _reset_stale_claims(self) -> None:
        """上次进程在总结中途退出，把占位的 summarized=2 还原为未总结。"""
        with self.db.connect() as conn:
            conn.execute("UPDATE conversation_digest SET summarized = 0 WHERE summarized = 2")
            conn.commit()

    def summarize_in_background(self, max_batches: int = 40) -> None:
        """启动时后台补齐历史对话摘要（不阻塞助手启动）。"""

        def _run():
            lock = self._acquire_summarize_lock()
            if not lock:
                logger.debug("另一个进程正在总结历史对话，跳过")
                return
            try:
                self.expire_overdue_promises()
                self.apply_decay()
                self._reset_stale_claims()
                for _ in range(max_batches):
                    if not self.summarize_pending(force=True):
                        break
            except Exception as e:  # noqa: BLE001
                logger.warning("后台历史总结失败: %s", e)
            finally:
                try:
                    lock.unlink()
                except OSError:
                    pass

        threading.Thread(target=_run, daemon=True).start()

    def expire_overdue_promises(self) -> int:
        """把已过截止日期的 open 承诺置为 expired，并同步降权对应的对话记录。"""
        with self.db.connect() as conn:
            rows = conn.execute(
                """SELECT id, source_session_id FROM auto_facts
                   WHERE status = 'open' AND category = 'promise'
                     AND due_date IS NOT NULL AND due_date < date('now')"""
            ).fetchall()
            if not rows:
                return 0
            conn.executemany(
                """UPDATE auto_facts
                   SET status = 'expired', last_used = datetime('now')
                   WHERE id = ?""",
                [(r["id"],) for r in rows],
            )
            # 承诺已过期，对应的「重要对话」不再享受长期保留与细节召回，
            # 避免事实库已经作废、摘要里却还当成未完成承诺一直注入。
            for r in rows:
                sid = r["source_session_id"]
                if not sid:
                    continue
                conn.execute(
                    """UPDATE conversation_digest
                       SET important = 0, salience = '承诺(已过期)'
                       WHERE id = (
                           SELECT id FROM conversation_digest
                           WHERE session_id = ? AND important = 1
                             AND salience LIKE '%承诺%'
                           ORDER BY id DESC LIMIT 1
                       )""",
                    (sid,),
                )
            conn.commit()
            logger.info("承诺过期: %d 条", len(rows))
            return len(rows)

    def apply_decay(self, *, force: bool = False) -> int:
        """30 天未使用的事实按日衰减 2%（rule 与 error_correction 类按 merge.should_decay 豁免），同一天只执行一次。"""
        from .merge import should_decay

        if not should_decay("fact"):
            return 0
        today = date.today().isoformat()
        with self.db.connect() as conn:
            if not force:
                row = conn.execute(
                    "SELECT value FROM memory_meta WHERE key = 'fact_decay_date'"
                ).fetchone()
                if row and row["value"] == today:
                    return 0
            cur = conn.execute(
                """UPDATE auto_facts
                   SET confidence = confidence * ?
                   WHERE status = 'open'
                     AND COALESCE(category, '') NOT IN ('rule', 'error_correction')
                     AND COALESCE(last_used, created_at) < datetime('now', ?)""",
                (_FACT_DECAY_FACTOR, f"-{_FACT_DECAY_IDLE_DAYS} days"),
            )
            conn.execute(
                """INSERT INTO memory_meta (key, value) VALUES ('fact_decay_date', ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                (today,),
            )
            conn.commit()
            if cur.rowcount:
                logger.info("记忆衰减: %d 条事实置信度下降", cur.rowcount)
            return cur.rowcount

    def _acquire_summarize_lock(self) -> Path | None:
        """跨进程互斥：同一时刻只允许一个进程做历史总结。"""
        lock = self.db.path.parent / ".history_summarize.lock"
        for _ in range(2):
            try:
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                return lock
            except FileExistsError:
                try:
                    if time.time() - lock.stat().st_mtime < 600:
                        return None
                    lock.unlink()  # 上一次总结进程崩溃遗留的锁
                except OSError:
                    return None
            except OSError:
                return None
        return None

    def _open_promise_rows(self, limit: int = 50) -> list[dict]:
        with self.db.connect() as conn:
            rows = conn.execute(
                """SELECT id, fact FROM auto_facts
                   WHERE status = 'open' AND category = 'promise'
                   ORDER BY created_at ASC LIMIT ?""",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    @staticmethod
    def _split_closed_lines(content: str) -> tuple[str, list[str]]:
        kept: list[str] = []
        closed: list[str] = []
        for line in (content or "").splitlines():
            m = _CLOSED_RE.match(line)
            if m:
                text = m.group(1).strip().strip('"“”')
                if text:
                    closed.append(text)
            elif line.strip():
                kept.append(line)
        return "\n".join(kept).strip(), closed

    def _close_promises(self, closed_lines: list[str]) -> int:
        if not closed_lines:
            return 0
        open_rows = self._open_promise_rows(limit=200)
        closed_ids: set[int] = set()
        with self.db.connect() as conn:
            for line in closed_lines:
                for row in open_rows:
                    fact = str(row["fact"] or "").strip()
                    if not fact or not (line == fact or line in fact or fact in line):
                        continue
                    fact_id = int(row["id"])
                    if fact_id not in closed_ids:
                        conn.execute(
                            """UPDATE auto_facts
                               SET status = 'done', last_used = datetime('now')
                               WHERE id = ?""",
                            (fact_id,),
                        )
                        closed_ids.add(fact_id)
                        logger.info("承诺已兑现: id=%d %s", fact_id, fact[:60])
                    break
            conn.commit()
        return len(closed_ids)

    def _summarize_history(self, rows) -> str:
        if not rows:
            return ""
        fallback = "；".join(
            (str(r["user_text"] or "").strip())[:60] for r in rows
        )[:300]
        if not self.llm_chat:
            return fallback

        conversation = "\n".join(
            f"[{r['created_at']}] 用户：{r['user_text']}\n助手：{r['reply_text']}"
            for r in rows
        )
        system_prompt = (
            "把以下历史对话压缩成一段 300 字以内的记忆摘要，供以后回忆。"
            "保留：用户做过的具体事情（应用/文件/项目名）、明确的偏好与身份信息、"
            "未完成的约定和计划。用「用户…」陈述，不要评论、不要分点。"
        )
        open_promises = self._open_promise_rows()
        if open_promises:
            listing = "\n".join(f"- {r['fact']}" for r in open_promises)
            system_prompt += (
                "\n\n同时扫描以下未关闭的承诺列表，若对话中已兑现，"
                "输出 CLOSED:<原承诺>（每行一条，原样引用承诺原文；未兑现不要输出）。\n"
                f"未关闭的承诺：\n{listing}"
            )
        try:
            result = self.llm_chat(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": conversation},
                ],
                tools=None,
            )
            if result:
                content = (result.get("content") or "").strip()
                if content:
                    summary, closed = self._split_closed_lines(content)
                    self._close_promises(closed)
                    if summary:
                        return summary[:300]
        except Exception as e:  # noqa: BLE001
            logger.debug("LLM 历史总结失败: %s", e)
        return fallback

    def consolidate_session(self, session_id: str):
        with self.db.connect() as conn:
            rows = conn.execute(
                """SELECT user_text, reply_text FROM conversation_digest
                   WHERE session_id = ?
                   ORDER BY id ASC""",
                (session_id,),
            ).fetchall()

        if not rows or len(rows) <= 10:
            return

        old_turns = rows[:-10]
        summary = self._summarize_turns(old_turns, session_id)

        if summary:
            with self.db.connect() as conn:
                cur = conn.execute(
                    """INSERT INTO consolidated_memory
                       (summary, source_session_ids, source_turn_count, memory_type, importance)
                       VALUES (?, ?, ?, 'conversation', 0.7)""",
                    (summary, session_id, len(old_turns)),
                )
                mem_id = int(cur.lastrowid)
                ids = conn.execute(
                    "SELECT id FROM conversation_digest WHERE session_id = ? ORDER BY id ASC LIMIT ?",
                    (session_id, len(old_turns)),
                ).fetchall()
                from .vector_chunks import VectorChunkStore

                del_ids = [int(row["id"]) for row in ids]
                VectorChunkStore.purge_for_sources(conn, "conversation", del_ids)
                for rid in del_ids:
                    conn.execute("DELETE FROM conversation_digest WHERE id = ?", (rid,))
                self.db._fts_insert(conn, summary, "consolidated", mem_id)
                conn.commit()
            logger.info("记忆压缩: %d 轮对话 → 1 条摘要", len(old_turns))

    def _summarize_turns(self, turns: list, session_id: str) -> str:
        if not self.llm_chat or not turns:
            return ""
        if len(turns) <= 3:
            return "；".join(t["user_text"][:50] for t in turns)

        conversation = "\n".join(
            f"用户：{t['user_text']}\n助手：{t['reply_text']}" for t in turns
        )
        try:
            result = self.llm_chat(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "将以下对话历史压缩为一段简洁的摘要（不超过200字），"
                            "保留关键信息：用户做了什么、问了什么、偏好是什么。"
                        ),
                    },
                    {"role": "user", "content": conversation},
                ],
                tools=None,
            )
            if result:
                return (result.get("content") or "").strip()[:300]
        except Exception as e:
            logger.debug("LLM 记忆压缩失败: %s", e)
        return "；".join(t["user_text"][:60] for t in turns[:5])

    # ─── 5. 上下文注入 ───────────────────────────────────────────

    from kekesi.memory.cognition import prepend_models as _p2_prepend_models  # KEKESI_PCE_P2_20261101_V2
    @_p2_prepend_models  # KEKESI_PCE_P2_20261101_V2
    def _p4_orig_learning_context_for_llm(
        self,
        session_id: str = "default",
        user_query: str = "",
    ) -> str:
        lines: list[str] = []
        # KEKESI_PCE_P4_20261101_V3: id-level audit. injected_ids
        # entries are qualified "table:<id>" keys (auto_fact /
        # behavior_pattern / error_correction / <related source_type>),
        # so ids from different tables never collide. Only ids of
        # items actually appended to lines are recorded (post-dedup).
        # The consolidated branch contributes no ids: its getter
        # returns strings only. Model rows from the P2 decorator are
        # not covered here (separate source).
        injected_ids: list[str] = []

        # KEKESI_PCE_P4_20261101_V3: same rows get_auto_facts(limit=10)
        # maps to strings ([r["fact"] for r in list_auto_facts]); kept
        # inline so the row ids stay attached to the injected lines.
        _p4_fact_rows = self.db.list_auto_facts(limit=10)
        facts = [r["fact"] for r in _p4_fact_rows]
        if facts:
            lines.append("【已学会的用户信息】")
            lines.extend(f"- {f}" for f in facts)
            injected_ids.extend(  # KEKESI_PCE_P4_20261101_V3
                f"auto_fact:{r['id']}" for r in _p4_fact_rows
                if r.get("id") is not None
            )

        patterns = self.get_learned_patterns(min_confidence=0.6, limit=5)
        now = datetime.now()
        time_bucket = (
            "morning" if 6 <= now.hour < 12
            else "afternoon" if 12 <= now.hour < 18
            else "evening" if 18 <= now.hour < 22
            else "night"
        )
        if patterns:
            lines.append("【用户行为模式】（当前时段：" + time_bucket + "）")
            for p in patterns:
                lines.append(
                    f"- [{p['pattern_type']}] {p['trigger_condition']}"
                    f"（已出现 {p['frequency']} 次）"
                )
                if p.get("id") is not None:  # KEKESI_PCE_P4_20261101_V3
                    injected_ids.append(f"behavior_pattern:{p['id']}")

        if user_query:
            related = self.search_memory(user_query, limit=3)
            shown = set(facts)
            extra = [r["content"] for r in related if r["content"] not in shown]
            injected_ids.extend(  # KEKESI_PCE_P4_20261101_V3
                f"{r.get('source_type', 'unknown')}:{r['source_id']}"
                for r in related
                if r["content"] not in shown and r.get("source_id") is not None
            )
            if extra:
                lines.append("【相关记忆】")
                lines.extend(f"- {c}" for c in extra)

        corrections = self.get_error_corrections(min_confidence=0.3)
        if corrections:
            lines.append("【工具失败修正经验】")
            for c in corrections[:5]:
                tool = c.get("tool_name") or "未知工具"
                lines.append(f"- {tool}: {c['correction_strategy']}")
                if c.get("id") is not None:  # KEKESI_PCE_P4_20261101_V3
                    injected_ids.append(f"error_correction:{c['id']}")

        consolidated = self.get_consolidated_memories(limit=3)
        if consolidated:
            lines.append("【长期记忆摘要】")
            lines.extend(f"- {c}" for c in consolidated)

        try:  # KEKESI_PCE_P4_20261101_V3
            self._p4_last_injected_ids = injected_ids
        except Exception:
            pass
        return "\n".join(lines)

    # ─── 查询 API ─────────────────────────────────────────────────

    def learning_context_for_llm(self, *args, **kwargs):  # KEKESI_PCE_P4_20261101_V2
        try:
            _p4_reentered = getattr(self, '_p4_audit_reentry', False)
        except Exception:
            _p4_reentered = False
        if _p4_reentered:
            return self._p4_orig_learning_context_for_llm(*args, **kwargs)
        try:
            self._p4_audit_reentry = True
        except Exception:
            return self._p4_orig_learning_context_for_llm(*args, **kwargs)
        try:
            import time as _p4_time
            _p4_t0 = _p4_time.time()
            _p4_result = self._p4_orig_learning_context_for_llm(*args, **kwargs)
            try:
                from kekesi.memory.memory_audit import log_audit
                _p4_q = kwargs.get('user_query', args[1] if len(args) > 1 else '')
                _p4_ids = [i for i in (getattr(self, '_p4_last_injected_ids', []) or []) if i is not None]  # KEKESI_PCE_P4_20261101_V3
                log_audit(query=_p4_q if isinstance(_p4_q, str) else '', injected_ids=_p4_ids, scores={}, latency_ms=(_p4_time.time() - _p4_t0) * 1000.0, result_chars=len(_p4_result) if isinstance(_p4_result, str) else 0)
            except Exception:
                pass
            return _p4_result
        finally:
            try:
                self._p4_audit_reentry = False
            except Exception:
                pass
    def get_auto_facts(self, limit: int = 20) -> list[str]:
        return [r["fact"] for r in self.db.list_auto_facts(limit=limit)]

    def get_consolidated_memories(self, limit: int = 5) -> list[str]:
        with self.db.connect() as conn:
            rows = conn.execute(
                """SELECT summary FROM consolidated_memory
                   ORDER BY importance DESC, created_at DESC
                   LIMIT ?""",
                (limit,),
            ).fetchall()
            return [r["summary"] for r in rows]

    def search_memory(self, query: str, limit: int = 5) -> list[dict]:
        return self.db.search_memory(query, limit=limit)

    def get_stats(self) -> dict:
        with self.db.connect() as conn:
            fts_count = conn.execute("SELECT COUNT(*) AS c FROM memory_fts").fetchone()["c"]
            return {
                "auto_facts": conn.execute("SELECT COUNT(*) AS c FROM auto_facts").fetchone()["c"],
                "patterns": conn.execute("SELECT COUNT(*) AS c FROM behavior_patterns").fetchone()["c"],
                "error_corrections": conn.execute("SELECT COUNT(*) AS c FROM error_corrections").fetchone()["c"],
                "consolidated": conn.execute("SELECT COUNT(*) AS c FROM consolidated_memory").fetchone()["c"],
                "indexed_memories": fts_count,
            }

    def boost_fact_confidence(self, fact: str, amount: float = _CONFIDENCE_BOOST):
        with self.db.connect() as conn:
            conn.execute(
                """UPDATE auto_facts SET
                   confidence = MIN(1.0, MAX(confidence, 0.45) + ?),
                   verified = 1,
                   status = CASE WHEN status = 'candidate' THEN 'open' ELSE status END,
                   last_used = datetime('now')
                   WHERE fact = ?""",
                (amount, fact),
            )
            row = conn.execute(
                "SELECT id FROM auto_facts WHERE fact = ? AND status = 'open'",
                (fact,),
            ).fetchone()
            if row:
                self.db._fts_insert(conn, fact, "auto_fact", int(row["id"]))
            conn.commit()

    def cleanup(self, max_auto_facts: int = 500, max_consolidated: int = 500) -> dict:
        """回收低置信/溢出的 auto_facts 与 consolidated_memory。

        行删除与 memory_fts 索引删除在同一连接、同一事务内完成：
        - 用 BEGIN IMMEDIATE 一次性拿到写锁，避免中途锁升级失败；
        - 不再通过 ``db.unindex_memory()`` 另开连接写 FTS（否则与本连接
          持有的 WAL 写锁互等，直到 busy_timeout 报 database is locked）；
        - 任一步失败整体回滚，保证表与索引不撕裂。
        """
        stats = {"auto_facts_stale": 0, "auto_facts_overflow": 0, "consolidated": 0}
        conn = self.db.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")

            def _purge(table: str, source_type: str, ids: list[int]) -> int:
                for rid in ids:
                    self.db._fts_delete(conn, source_type, rid)
                if ids:
                    conn.executemany(
                        f"DELETE FROM {table} WHERE id = ?", [(rid,) for rid in ids]
                    )
                return len(ids)

            stale = [
                int(r["id"])
                for r in conn.execute(
                    """SELECT id FROM auto_facts
                       WHERE confidence < ? AND verified = 0
                         AND (COALESCE(status, 'open') != 'candidate'
                              OR COALESCE(last_used, created_at) < datetime('now', '-7 days'))""",
                    (_MIN_AUTO_FACT_CONFIDENCE,),
                ).fetchall()
            ]
            stats["auto_facts_stale"] = _purge("auto_facts", "auto_fact", stale)

            overflow = [
                int(r["id"])
                for r in conn.execute(
                    """SELECT id FROM auto_facts WHERE id NOT IN (
                       SELECT id FROM auto_facts ORDER BY confidence DESC LIMIT ?)""",
                    (max_auto_facts,),
                ).fetchall()
            ]
            stats["auto_facts_overflow"] = _purge("auto_facts", "auto_fact", overflow)

            old_cons = [
                int(r["id"])
                for r in conn.execute(
                    """SELECT id FROM consolidated_memory WHERE id NOT IN (
                       SELECT id FROM consolidated_memory
                       ORDER BY importance DESC, created_at DESC LIMIT ?)""",
                    (max_consolidated,),
                ).fetchall()
            ]
            if old_cons:
                from .vector_chunks import VectorChunkStore

                VectorChunkStore.purge_for_sources(conn, "summary", old_cons)
            stats["consolidated"] = _purge("consolidated_memory", "consolidated", old_cons)
            stats["fts_orphans_cleaned"] = self.db.sweep_orphan_fts_in_conn(conn)

            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()
        return stats
