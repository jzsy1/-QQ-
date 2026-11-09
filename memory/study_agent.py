"""StudyAnalyst — 有界自主研读循环（科技论文 + 开源项目）。

职责：
    - 给定 StudyTopic 与 HttpBudget，驱动 LLM 在 6–8 步有界循环内自主调用抓取工具：
      arxiv_search / arxiv_read / github_search / github_repo_files / github_read_file / finish_note
    - 强制通过 finish_note 结构化收口：{verdict, points[], source_refs[], quality, adopt}
    - 弱模型兜底：若返回 tool_calls 为空但 content 含 <tool_call>，解析抢救；
      连续两轮无有效动作立即放弃该主题（不退化为自由发挥、不入库）
    - StudyOutcome 携带本轮真实抓取过的 URL 集合与来源深度标记，供入库门槛核验
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from ..tools import study_sources as _default_sources
from ..tools.study_sources import HttpBudget

logger = logging.getLogger(__name__)

_DEFAULT_MAX_STEPS = 7
_DEFAULT_WALL_SEC = 180.0
_MAX_LLM_CALLS = 9
_MAX_IDLE_TURNS = 2

_TOOL_CALL_TAG_RE = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL | re.IGNORECASE)
_TOOL_CALL_OPEN_RE = re.compile(r"<(?:tool_call|｜tool▁call▁begin｜|\|tool_call\|)>", re.IGNORECASE)


# ─── 数据结构 ──────────────────────────────────────────────────


@dataclass
class StudyTopic:
    raw: str
    kind: str  # "paper" | "repo"
    query: str
    display_name: str = ""

    def __post_init__(self) -> None:
        if not self.display_name:
            self.display_name = self.query or self.raw

    @classmethod
    def from_raw(cls, raw: str | StudyTopic, default_kind: str = "paper") -> StudyTopic:
        if isinstance(raw, StudyTopic):
            return raw
        text = str(raw or "").strip()
        if not text:
            return cls(raw="", kind=default_kind, query="", display_name="")

        m_arxiv = re.match(r"^(?:ARXIV|PAPER)\s*:\s*(.+)$", text, flags=re.IGNORECASE)
        if m_arxiv:
            q = m_arxiv.group(1).strip()
            return cls(raw=text, kind="paper", query=q, display_name=q)

        m_repo = re.match(r"^(?:REPO|GITHUB)\s*:\s*(.+)$", text, flags=re.IGNORECASE)
        if m_repo:
            q = m_repo.group(1).strip()
            return cls(raw=text, kind="repo", query=q, display_name=q)

        lower = text.lower()
        if any(k in lower for k in ("github", "repo", "开源", "仓库", "mcp", "sdk", "cli", "tool")):
            kind = "repo"
        elif any(k in lower for k in ("arxiv", "paper", "论文", "cs.")):
            kind = "paper"
        else:
            kind = default_kind if default_kind in ("paper", "repo") else "paper"
        return cls(raw=text, kind=kind, query=text, display_name=text)


@dataclass
class StudyOutcome:
    ok: bool
    topic: StudyTopic
    kind: str = "paper"
    verdict: str = ""
    points: list[str] = field(default_factory=list)
    source_refs: list[str] = field(default_factory=list)
    quality: str = "medium"
    adopt: dict[str, Any] | None = None
    fetched_urls: set[str] = field(default_factory=set)
    sources_used: set[str] = field(default_factory=set)
    steps_taken: int = 0
    llm_calls: int = 0
    abort_reason: str = ""


# ─── 工具 Schema 定义 ──────────────────────────────────────────

STUDY_TOOLS_SCHEMA: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "arxiv_search",
            "description": "在 arXiv 检索科技论文（返回标题、摘要、作者、arXiv ID 与链接）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "英文检索关键词（如 'LLM agent memory architecture'）",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "返回篇数，默认 3，范围 1-5",
                    },
                    "sort": {
                        "type": "string",
                        "enum": ["relevance", "submittedDate", "lastUpdatedDate"],
                        "description": "排序方式，默认 relevance",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "arxiv_read",
            "description": "精读指定 arXiv 论文的正文（优先提取 HTML 全文，不可得时回退摘要）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "arxiv_id": {
                        "type": "string",
                        "description": "arXiv 论文编号，如 '2401.12345' 或 '2401.12345v1'",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "最大读取字符数，默认 6000",
                    },
                },
                "required": ["arxiv_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "github_search",
            "description": "在 GitHub 检索开源项目仓库（返回仓库名、描述、星标、协议、默认分支与链接）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "检索关键词（如 'voice assistant python' 或 'LLM memory'）",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "返回仓库数，默认 5，范围 1-8",
                    },
                    "sort": {
                        "type": "string",
                        "enum": ["stars", "updated", "forks"],
                        "description": "排序方式，默认 stars",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "github_repo_files",
            "description": "获取 GitHub 仓库的文件清单（仅保留 *.py/*.md/*.toml/*.yaml 等关键文件）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "owner": {"type": "string", "description": "仓库所有者"},
                    "repo": {"type": "string", "description": "仓库名称"},
                    "ref": {
                        "type": "string",
                        "description": "分支或 tag，默认 HEAD",
                    },
                },
                "required": ["owner", "repo"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "github_read_file",
            "description": "读取 GitHub 仓库中的单个文件内容（如 README.md、pyproject.toml、核心 .py 源码）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "owner": {"type": "string", "description": "仓库所有者"},
                    "repo": {"type": "string", "description": "仓库名称"},
                    "path": {"type": "string", "description": "文件路径，如 'README.md'"},
                    "ref": {"type": "string", "description": "分支名，默认 HEAD"},
                },
                "required": ["owner", "repo", "path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish_note",
            "description": "完成分析并提交结构化自习笔记。读完真实材料后必须调用此工具收口。",
            "parameters": {
                "type": "object",
                "properties": {
                    "verdict": {
                        "type": "string",
                        "description": "一句话中文核心结论（必须具体说明论文方法/结论或项目架构/价值，禁止空话）",
                    },
                    "points": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "2-5 条具体技术要点（每条包含方法细节、关键模块、实验指标或适用边界）",
                    },
                    "source_refs": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "本轮真实读取过的来源 URL 列表（必须与工具返回的 URL 一致，严禁编造）",
                    },
                    "quality": {
                        "type": "string",
                        "enum": ["high", "medium", "low"],
                        "description": "本次资料与分析质量评估",
                    },
                    "adopt": {
                        "type": "object",
                        "description": "可选。若该开源项目值得接入以增强助手自身能力则填写，否则省略不填（仅登记候选，不自动安装）",
                        "properties": {
                            "repo": {"type": "string", "description": "owner/repo"},
                            "why": {"type": "string", "description": "为什么值得接入"},
                            "how": {"type": "string", "description": "如何集成或封装"},
                            "license": {"type": "string", "description": "开源协议"},
                            "risk": {"type": "string", "description": "依赖/安全/体积风险评估"},
                        },
                    },
                },
                "required": ["verdict", "points", "source_refs"],
            },
        },
    },
]

_ANALYST_SYSTEM_PROMPT = (
    "你是科科斯（Kekesi）的自主研读分析员，只研究【科技论文 (arXiv)】与【开源项目 (GitHub)】。\n"
    "工作纪律：\n"
    "1. 你必须先调用检索工具（arxiv_search 或 github_search），再深入精读（arxiv_read 或 github_repo_files + github_read_file 读 README/核心源码），最后调用 finish_note 提交笔记。\n"
    "2. 严禁在不调用读取工具的情况下凭空编造结论或 URL！finish_note.source_refs 只能填写本轮工具真实返回过的 URL。\n"
    "3. 笔记必须有技术含金量：verdict 给出一句话核心技术判断；points 给出 2-5 条具体机制、算法设计、核心模块实现或优缺点。\n"
    "4. 如果检索无结果或资料不足以支撑有价值的技术结论，请不要编造，直接停止或将 quality 设为 low。\n"
    "5. 评估开源项目时，若发现它能直接增强语音助手/Agent 自身能力（且协议友好、依赖轻量），可在 finish_note 的 adopt 字段填写候选提案（系统只会列为待用户批准候选，绝不擅自安装）。"
)


# ─── 弱模型 <tool_call> 文本抢救 ───────────────────────────────


def _extract_first_json_dict(text: str) -> dict[str, Any] | None:
    """用括号配对从文本中提取第一个完整 JSON 对象。"""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    escape = False
    for idx in range(start, len(text)):
        ch = text[idx]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                candidate = text[start : idx + 1]
                try:
                    obj = json.loads(candidate)
                    return obj if isinstance(obj, dict) else None
                except Exception:
                    return None
    return None


def _parse_json_args(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            val = json.loads(raw)
            return val if isinstance(val, dict) else {}
        except Exception:
            extracted = _extract_first_json_dict(raw)
            return extracted or {}
    return {}


def rescue_text_tool_calls(content: str) -> list[dict[str, Any]]:
    """当 LLM 返回的 tool_calls 为空但 content 含有 <tool_call> 时抢救解析。

    优先复用 agent_loop._parse_text_tool_calls（若已加载），否则走内置同构解析。
    """
    text = str(content or "").strip()
    if not text:
        return []

    import sys

    agent_loop_mod = sys.modules.get("kekesi.agent_loop")
    if agent_loop_mod is not None and hasattr(agent_loop_mod, "_parse_text_tool_calls"):
        try:
            rescued = agent_loop_mod._parse_text_tool_calls(text)
            if rescued:
                return rescued
        except Exception:
            logger.debug("调用 agent_loop._parse_text_tool_calls 异常，回退内置解析", exc_info=True)

    out: list[dict[str, Any]] = []
    for i, m in enumerate(_TOOL_CALL_TAG_RE.finditer(text)):
        raw = m.group(1).strip()
        if not raw:
            continue
        data = _parse_json_args(raw)
        if not data:
            continue
        name = str(data.get("name") or "").strip()
        if not name:
            continue
        args = data.get("arguments")
        if args is None:
            args = data.get("args") or data.get("parameters") or {}
        args = _parse_json_args(args)
        out.append(
            {
                "id": f"study_tc_{i}_{uuid.uuid4().hex[:8]}",
                "function": {"name": name, "arguments": args},
            }
        )
    if out:
        return out

    # 漏闭合标签 <tool_call>{"name": ...}
    m_open = _TOOL_CALL_OPEN_RE.search(text)
    if m_open:
        data = _extract_first_json_dict(text[m_open.end() :])
        if data and data.get("name"):
            name = str(data.get("name") or "").strip()
            args = data.get("arguments")
            if args is None:
                args = data.get("args") or data.get("parameters") or {}
            args = _parse_json_args(args)
            return [
                {
                    "id": f"study_tc_open_{uuid.uuid4().hex[:8]}",
                    "function": {"name": name, "arguments": args},
                }
            ]
    return []


# ─── StudyAnalyst 主体 ─────────────────────────────────────────


class StudyAnalyst:
    """有界自主研读分析器（单主题 6-8 步工具循环）。"""

    def __init__(
        self,
        db: Any,
        llm_chat: Callable[..., dict[str, Any] | None] | None,
        cfg: dict[str, Any] | None = None,
        sources_mod: Any = None,
    ) -> None:
        self.db = db
        self.llm_chat = llm_chat
        self.cfg = cfg or {}
        self.sources = sources_mod or _default_sources
        self.max_steps = max(
            2, min(int(self.cfg.get("max_steps_per_topic") or _DEFAULT_MAX_STEPS), 10)
        )
        self.wall_sec = max(
            15.0, float(self.cfg.get("session_wall_sec") or _DEFAULT_WALL_SEC)
        )
        self.token_env = str(self.cfg.get("github_token_env") or "GITHUB_TOKEN").strip()
        self.material_max_chars = max(
            1500, int(self.cfg.get("material_max_chars") or 6000)
        )

    def analyze(
        self,
        topic: StudyTopic | str,
        budget: HttpBudget | None = None,
    ) -> StudyOutcome:
        study_topic = StudyTopic.from_raw(topic)
        if budget is None:
            budget = HttpBudget(daily_limits=self.cfg.get("budget"))

        if not study_topic.query:
            return StudyOutcome(
                ok=False,
                topic=study_topic,
                kind=study_topic.kind,
                abort_reason="empty_topic",
            )
        if self.llm_chat is None:
            return StudyOutcome(
                ok=False,
                topic=study_topic,
                kind=study_topic.kind,
                abort_reason="no_llm",
            )

        deadline = time.monotonic() + self.wall_sec
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": _ANALYST_SYSTEM_PROMPT},
            {"role": "user", "content": self._build_user_prompt(study_topic)},
        ]

        steps_taken = 0
        llm_calls = 0
        idle_turns = 0

        while llm_calls < _MAX_LLM_CALLS:
            if time.monotonic() >= deadline:
                return self._fail_outcome(
                    study_topic, budget, steps_taken, llm_calls, "wall_timeout"
                )
            if budget.exhausted_reason and not budget.fetched_urls:
                return self._fail_outcome(
                    study_topic,
                    budget,
                    steps_taken,
                    llm_calls,
                    f"budget_exhausted:{budget.exhausted_reason}",
                )

            # 步数已满但尚未收口：若已有真实抓取内容，给最后一次收口机会
            if steps_taken >= self.max_steps:
                if not budget.fetched_urls:
                    return self._fail_outcome(
                        study_topic, budget, steps_taken, llm_calls, "max_steps_no_fetch"
                    )
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "已达到单主题工具步数上限。请立即调用 finish_note 提交结构化总结笔记"
                            "（source_refs 仅填上面工具实际返回过的 URL）；若资料不足请放弃。"
                        ),
                    }
                )

            llm_calls += 1
            try:
                resp = self.llm_chat(
                    messages=messages,
                    tools=STUDY_TOOLS_SCHEMA,
                    tool_choice="auto",
                )
            except Exception as e:
                logger.warning("StudyAnalyst LLM 调用异常 (%s): %s", study_topic.raw, e)
                return self._fail_outcome(
                    study_topic, budget, steps_taken, llm_calls, f"llm_error:{e}"
                )

            if not resp or not isinstance(resp, dict):
                idle_turns += 1
                if idle_turns >= _MAX_IDLE_TURNS:
                    return self._fail_outcome(
                        study_topic, budget, steps_taken, llm_calls, "two_idle_turns"
                    )
                continue

            tool_calls = self._extract_tool_calls(resp)
            if not tool_calls:
                idle_turns += 1
                if idle_turns >= _MAX_IDLE_TURNS or steps_taken >= self.max_steps:
                    return self._fail_outcome(
                        study_topic, budget, steps_taken, llm_calls, "two_idle_turns"
                    )
                assistant_content = str(resp.get("content") or "").strip()
                messages.append({"role": "assistant", "content": assistant_content})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "请直接调用工具（如 arxiv_search / arxiv_read / github_search / "
                            "github_repo_files / github_read_file），读完后调用 finish_note 收口，不要只输出纯文本。"
                        ),
                    }
                )
                continue

            idle_turns = 0
            messages.append(
                {
                    "role": "assistant",
                    "content": str(resp.get("content") or ""),
                    "tool_calls": tool_calls,
                }
            )

            for tc in tool_calls:
                fn = tc.get("function") or {}
                fn_name = str(fn.get("name") or "").strip()
                fn_args = _parse_json_args(fn.get("arguments"))
                tc_id = str(tc.get("id") or f"call_{uuid.uuid4().hex[:8]}")

                if fn_name == "finish_note":
                    return self._build_finish_outcome(
                        study_topic, fn_args, budget, steps_taken, llm_calls
                    )

                if steps_taken >= self.max_steps:
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc_id,
                            "content": "步数已达上限，请直接调用 finish_note 收口。",
                        }
                    )
                    continue

                steps_taken += 1
                tool_output = self._dispatch_tool(fn_name, fn_args, budget)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "content": tool_output,
                    }
                )

                if time.monotonic() >= deadline:
                    return self._fail_outcome(
                        study_topic, budget, steps_taken, llm_calls, "wall_timeout"
                    )

        return self._fail_outcome(
            study_topic, budget, steps_taken, llm_calls, "max_llm_calls_exceeded"
        )

    # ─── 内部辅助 ──────────────────────────────────────────────

    def _build_user_prompt(self, topic: StudyTopic) -> str:
        if topic.kind == "repo":
            return (
                f"自习任务（开源项目评估）：{topic.query}\n"
                f"建议步骤：\n"
                f"1) 调用 github_search 搜索相关高星或活跃仓库；\n"
                f"2) 挑选最相关的一个仓库，调用 github_repo_files 查看目录结构；\n"
                f"3) 调用 github_read_file 读取 README.md 及 1-2 个关键源码/配置文件；\n"
                f"4) 调用 finish_note 产出技术评估笔记（若适合增强助手能力可填 adopt 候选）。"
            )
        return (
            f"自习任务（科技论文研读）：{topic.query}\n"
            f"建议步骤：\n"
            f"1) 调用 arxiv_search 搜索相关论文；\n"
            f"2) 挑选最有价值的一篇，调用 arxiv_read 读取正文；\n"
            f"3) 调用 finish_note 产出结构化论文研读笔记。"
        )

    @staticmethod
    def _extract_tool_calls(resp: dict[str, Any]) -> list[dict[str, Any]]:
        raw_tcs = resp.get("tool_calls") or []
        normalized: list[dict[str, Any]] = []
        for i, tc in enumerate(raw_tcs):
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") or {}
            name = str(fn.get("name") or tc.get("name") or "").strip()
            if not name:
                continue
            args = fn.get("arguments")
            if args is None:
                args = tc.get("arguments") or {}
            normalized.append(
                {
                    "id": str(tc.get("id") or f"call_{i}_{uuid.uuid4().hex[:8]}"),
                    "function": {
                        "name": name,
                        "arguments": _parse_json_args(args),
                    },
                }
            )
        if normalized:
            return normalized
        content = str(resp.get("content") or "")
        if "<tool_call" in content.lower() or "tool▁call" in content:
            return rescue_text_tool_calls(content)
        return []

    def _dispatch_tool(
        self,
        name: str,
        args: dict[str, Any],
        budget: HttpBudget,
    ) -> str:
        try:
            if name == "arxiv_search":
                query = str(args.get("query") or "").strip()
                max_res = int(args.get("max_results") or 3)
                sort = str(args.get("sort") or "relevance")
                ok, papers = self.sources.arxiv_search(
                    query, max_results=max_res, sort=sort, budget=budget
                )
                if not ok or not papers:
                    reason = budget.exhausted_reason or "未找到匹配的 arXiv 论文"
                    return json.dumps({"ok": False, "error": reason}, ensure_ascii=False)
                items = [
                    {
                        "arxiv_id": p.arxiv_id,
                        "title": p.title,
                        "published": p.published,
                        "authors": p.authors[:4],
                        "categories": p.categories[:4],
                        "abs_url": p.abs_url,
                        "html_url": p.html_url,
                        "summary": p.summary[:800],
                    }
                    for p in papers
                ]
                return json.dumps({"ok": True, "papers": items}, ensure_ascii=False)

            if name in ("arxiv_read", "arxiv_fulltext"):
                arxiv_id = str(args.get("arxiv_id") or args.get("id") or "").strip()
                max_chars = min(int(args.get("max_chars") or self.material_max_chars), self.material_max_chars)
                ok, text, source_tag = self.sources.arxiv_fulltext(
                    arxiv_id, max_chars=max_chars, budget=budget
                )
                if not ok:
                    return json.dumps({"ok": False, "error": text}, ensure_ascii=False)
                cid = self.sources.strip_arxiv_version(self.sources.clean_arxiv_id(arxiv_id))
                url = (
                    f"https://arxiv.org/html/{cid}"
                    if source_tag in ("html", "ar5iv")
                    else f"https://arxiv.org/abs/{cid}"
                )
                return json.dumps(
                    {
                        "ok": True,
                        "arxiv_id": arxiv_id,
                        "source": source_tag,
                        "url": url,
                        "abs_url": f"https://arxiv.org/abs/{cid}",
                        "content": text,
                    },
                    ensure_ascii=False,
                )

            if name == "github_search":
                query = str(args.get("query") or "").strip()
                max_res = int(args.get("max_results") or 5)
                sort = str(args.get("sort") or "stars")
                ok, repos = self.sources.github_search(
                    query,
                    max_results=max_res,
                    sort=sort,
                    budget=budget,
                    token_env=self.token_env,
                )
                if not ok or not repos:
                    reason = budget.exhausted_reason or "未找到匹配的 GitHub 仓库或已达本场检索上限"
                    return json.dumps({"ok": False, "error": reason}, ensure_ascii=False)
                items = [
                    {
                        "full_name": r.full_name,
                        "owner": r.owner,
                        "repo": r.repo,
                        "stars": r.stars,
                        "language": r.language,
                        "license": r.license,
                        "default_branch": r.default_branch,
                        "html_url": r.html_url,
                        "description": r.description,
                        "topics": r.topics,
                    }
                    for r in repos
                ]
                return json.dumps({"ok": True, "repos": items}, ensure_ascii=False)

            if name == "github_repo_files":
                owner = str(args.get("owner") or "").strip()
                repo = str(args.get("repo") or "").strip()
                ref = str(args.get("ref") or "HEAD").strip()
                ok, files = self.sources.github_repo_files(
                    owner,
                    repo,
                    ref=ref,
                    budget=budget,
                    token_env=self.token_env,
                )
                if not ok:
                    reason = budget.exhausted_reason or "获取仓库目录树失败或已达上限"
                    return json.dumps({"ok": False, "error": reason}, ensure_ascii=False)
                return json.dumps(
                    {
                        "ok": True,
                        "repo_url": f"https://github.com/{owner}/{repo}",
                        "files": files,
                    },
                    ensure_ascii=False,
                )

            if name == "github_read_file":
                owner = str(args.get("owner") or "").strip()
                repo = str(args.get("repo") or "").strip()
                path = str(args.get("path") or "").strip()
                ref = str(args.get("ref") or "HEAD").strip()
                ok, content = self.sources.github_read_file(
                    owner,
                    repo,
                    ref=ref,
                    path=path,
                    budget=budget,
                    token_env=self.token_env,
                )
                if not ok:
                    return json.dumps({"ok": False, "error": content}, ensure_ascii=False)
                return json.dumps(
                    {
                        "ok": True,
                        "repo_url": f"https://github.com/{owner}/{repo}",
                        "path": path,
                        "content": content[: self.material_max_chars],
                    },
                    ensure_ascii=False,
                )

            return json.dumps({"ok": False, "error": f"未知工具: {name}"}, ensure_ascii=False)
        except Exception as e:
            logger.warning("StudyAnalyst 工具执行异常 %s(%s): %s", name, args, e)
            return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)

    def _build_finish_outcome(
        self,
        topic: StudyTopic,
        args: dict[str, Any],
        budget: HttpBudget,
        steps_taken: int,
        llm_calls: int,
    ) -> StudyOutcome:
        verdict = str(args.get("verdict") or "").strip()
        raw_points = args.get("points") or []
        if isinstance(raw_points, str):
            raw_points = [
                re.sub(r"^[\-\*\d\.\s]+", "", line).strip()
                for line in raw_points.splitlines()
                if line.strip()
            ]
        points = [str(p).strip() for p in raw_points if str(p).strip()]

        raw_refs = args.get("source_refs") or []
        if isinstance(raw_refs, str):
            raw_refs = [s.strip() for s in re.split(r"[\s,，]+", raw_refs) if s.strip()]
        source_refs = [str(r).strip() for r in raw_refs if str(r).strip()]

        quality = str(args.get("quality") or "medium").strip().lower()
        adopt = args.get("adopt")
        if not isinstance(adopt, dict) or not str(adopt.get("repo") or "").strip():
            adopt = None

        kind = topic.kind
        has_arxiv = any("arxiv.org" in r for r in source_refs)
        has_github = any("github.com" in r or "githubusercontent.com" in r for r in source_refs)
        if topic.kind == "paper" and has_arxiv:
            kind = "paper"
        elif topic.kind == "repo" and has_github:
            kind = "repo"
        elif has_arxiv and not has_github:
            kind = "paper"
        elif has_github and not has_arxiv:
            kind = "repo"

        if quality == "low":
            return StudyOutcome(
                ok=False,
                topic=topic,
                kind=kind,
                verdict=verdict,
                points=points,
                source_refs=source_refs,
                quality=quality,
                adopt=adopt,
                fetched_urls=set(budget.fetched_urls),
                sources_used=set(budget.sources_used),
                steps_taken=steps_taken,
                llm_calls=llm_calls,
                abort_reason="low_quality_self_reported",
            )

        return StudyOutcome(
            ok=True,
            topic=topic,
            kind=kind,
            verdict=verdict,
            points=points,
            source_refs=source_refs,
            quality=quality,
            adopt=adopt,
            fetched_urls=set(budget.fetched_urls),
            sources_used=set(budget.sources_used),
            steps_taken=steps_taken,
            llm_calls=llm_calls,
        )

    @staticmethod
    def _fail_outcome(
        topic: StudyTopic,
        budget: HttpBudget,
        steps_taken: int,
        llm_calls: int,
        reason: str,
    ) -> StudyOutcome:
        return StudyOutcome(
            ok=False,
            topic=topic,
            kind=topic.kind,
            fetched_urls=set(budget.fetched_urls),
            sources_used=set(budget.sources_used),
            steps_taken=steps_taken,
            llm_calls=llm_calls,
            abort_reason=reason,
        )
