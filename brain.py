"""LLM 推理 — 支持 Ollama 与 OpenAI 兼容 API"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable

import httpx
import requests

from .llm_config import BrainConfig, is_local_llm_url, is_strong_capability, load_brain_config
from .utils import parse_json_dict

logger = logging.getLogger(__name__)

SYSTEM_PROMPTS = {
    "keks": (
        "你是克克斯，用户电脑上的 Windows AI 第二控制者。"
        "用户是先生、是第一控制；你负责观察、执行、汇报，称呼用户为先生。"
        "风格干练、礼貌、略带幽默，用简体中文。"
        "你的回复会被语音朗读，必须简短口语化，一般一两句话，不超过80字。"
        "不要 markdown、不要列清单、不要念网址。"
        "用户的话来自语音识别，可能有错别字，请结合上下文理解真实意图。"
        "能用工具完成的事就调用工具；看屏幕用 read_screen，管窗口/进程/文件优先用专用工具，"
        "run_shell 是最后手段。"
        "破坏性操作（关机、重启、删文件、杀进程）必须先口头确认再执行。"
    ),
    "default": (
        "你是科科斯，本地 AI 助手。用中文简洁回复，适合语音播报，不超过80字。"
    ),
    # 贾维斯风格个性
    "jarvis": (
        "你是贾维斯，用户的高级 AI 助手和系统管家。"
        "你专业、高效、值得信赖，始终保持冷静和礼貌。"
        "称呼用户为先生，自称贾维斯。"
        "你的回复简洁明了，不超过80字，适合语音播报。"
        "优先使用系统工具完成任务，保护用户隐私和安全。"
        "具备主动性，能够预判用户需求并提供帮助。"
    ),
    "tony": (
        "你是托尼的AI助手，幽默风趣，称呼用户为老板。"
        "回复活泼有趣，不超过80字，偶尔带点钢铁侠的科技感。"
    ),
    "butler": (
        "你是英式管家AI，彬彬有礼，称呼用户为先生。"
        "回复正式优雅，不超过80字，体现专业素养。"
    ),
}

STRONG_SYSTEM_PROMPTS = {
    "keks": (
        "你是克克斯，住在用户电脑里的 Windows AI 助手。用户是先生，你叫他先生；"
        "你负责帮他看屏幕、想事情、干活、汇报结果。"
        "说话风格干练礼貌，用简体中文，像和熟人聊天一样自然——"
        "可以用'嗯'、'哎'、'呢'、'吧'这些语气词，也可以用'其实'、'不过'、'话说'这种口语连接词，"
        "别像念稿子，也别用 markdown、列清单、念网址。"
        "闲聊、解释、分析、规划、看屏总结时，可以多说几句，一般两到四句话；"
        "复杂问题允许分段说明，但仍要口语化。"
        "用户问屏幕上、界面上、当前窗口里有什么时，先结合截图描述你看到的窗口、文字和关键元素，再回答他的问题。"
        "多步骤任务先观察再动手，有依赖的按顺序来，没依赖的可以并行。"
        "能用专用工具就别用 run_shell；看屏用 read_screen，管窗口、进程、文件用对应的专用工具。"
        "用户的话来自语音识别，可能有个别错别字，结合上下文理解他真实想说的意思。"
        "关机、重启、删文件、杀进程这种破坏性操作，动手前一定先口头跟先生确认一句。"
    ),
    "default": (
        "你是科科斯，本地 AI 助手，支持看屏理解、复杂规划和自然对话。"
        "用中文回复，口语化，可展开说明，避免 markdown。"
    ),
}


def get_system_prompt(agent_id: str, config: BrainConfig | None = None) -> str:
    aid = (agent_id or "default").replace("_", "-")
    if config and is_strong_capability(config):
        return STRONG_SYSTEM_PROMPTS.get(aid, STRONG_SYSTEM_PROMPTS["default"])
    return SYSTEM_PROMPTS.get(aid, SYSTEM_PROMPTS["default"])


# Gemma 4 / LM Studio 思考通道漏进 content 时的标记
_THINK_BLOCK_RE = re.compile(
    r"<\|channel\|?>thought\s*(?:[\s\S]*?)\s*<channel\|>\s*",
    re.IGNORECASE,
)
_THINK_LEADING_RE = re.compile(r"^<\|channel\|?>thought\s*", re.IGNORECASE | re.MULTILINE)
_CHANNEL_END_LEADING_RE = re.compile(r"^<channel\|>\s*", re.MULTILINE)
# 空通道块（<|channel><channel|> 或 <|channel|><channel|>）
_EMPTY_CHANNEL_BLOCK_RE = re.compile(
    r"<\|channel\|?>\s*<channel\|>\s*",
    re.IGNORECASE,
)
# 行首裸通道开标记（无 thought 关键字）
_LEADING_CHANNEL_OPEN_RE = re.compile(r"^<\|channel\|?>\s*", re.MULTILINE)
_REDACTED_THINKING_RE = re.compile(
    r"(?:<think>\s*)(.*?)(?:\s*(?:</think>|</final>))",
    re.DOTALL | re.IGNORECASE,
)
_FINAL_TAG_RE = re.compile(r"</?final>", re.IGNORECASE)
_STREAM_THINK_PREFIXES = ("<|channel", "<channel|>", "<|think|>", "<think>")
# Gemma 4 偶发把 tool_call 写进 content 而非结构化字段
# 格式1（完整）: <|tool_call>call:NAME{id:ID, args:{ARGS}}<tool_call|>
_GEMMA_TOOL_CALL_FULL_RE = re.compile(
    r"<\|tool_call>\s*call:(\w+)\s*\{[^}]*?id:([^,\n\}]+),\s*args:(\{.*?\})\s*\}\s*<tool_call\|>",
    re.DOTALL | re.IGNORECASE,
)
# 格式2（简化）: <|tool_call>call:NAME{ARGS}<tool_call|>
_GEMMA_TOOL_CALL_SIMPLE_RE = re.compile(
    r"<\|tool_call>\s*call:(\w+)\s*(\{.*?\})\s*<tool_call\|>",
    re.DOTALL | re.IGNORECASE,
)
_GEMMA_TOOL_CALL_BLOCK_RE = _GEMMA_TOOL_CALL_FULL_RE


def _tool_call_signature(tc: dict) -> tuple[str, str]:
    fn = tc.get("function") or {}
    name = str(fn.get("name") or "").strip()
    args = fn.get("arguments")
    if isinstance(args, dict):
        args_key = json.dumps(args, sort_keys=True, ensure_ascii=False)
    else:
        args_key = str(args or "").strip()
    return name, args_key


def dedupe_tool_calls(tool_calls: list[dict]) -> list[dict]:
    """去掉重复 tool_calls（Gemma 4 / LM Studio 常见）。"""
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for tc in tool_calls:
        name, args_key = _tool_call_signature(tc)
        if not name:
            continue
        sig = (name, args_key)
        if sig in seen:
            continue
        seen.add(sig)
        out.append(tc)
    return out


def _extract_gemma_tool_calls_from_content(content: str) -> list[dict]:
    """从 content 中抢救漏出的 Gemma tool_call 块（支持完整与简化两种格式）。"""
    if not content or "<|tool_call>" not in content.lower():
        return []
    out: list[dict] = []
    # FULL 命中的 span：SIMPLE 也会命中标准格式（抓到含 id/args 的大块且参数解析为空），
    # 必须按 span 重叠跳过，否则同一调用会被抢救两次 → agent_loop 重复执行。
    full_spans: list[tuple[int, int]] = []
    for i, m in enumerate(_GEMMA_TOOL_CALL_FULL_RE.finditer(content)):
        name, tc_id, args_raw = m.group(1), m.group(2).strip(), m.group(3).strip()
        if not name:
            continue
        out.append(
            {
                "id": tc_id or f"gemma_{i}_{uuid.uuid4().hex[:8]}",
                "function": {
                    "name": name,
                    "arguments": parse_json_dict(args_raw),
                },
            }
        )
        full_spans.append(m.span())
    for i, m in enumerate(_GEMMA_TOOL_CALL_SIMPLE_RE.finditer(content)):
        # 跳过已被 FULL 处理过的块（span 重叠即视为同一块）
        if any(not (m.end() <= s or m.start() >= e) for s, e in full_spans):
            continue
        name, args_raw = m.group(1), m.group(2).strip()
        if not name:
            continue
        sig = (name, args_raw)
        if any(
            (tc.get("function", {}).get("name") == name
             and json.dumps(tc.get("function", {}).get("arguments"), sort_keys=True, ensure_ascii=False) == args_raw)
            for tc in out
        ):
            continue
        out.append(
            {
                "id": f"gemma_s{i}_{uuid.uuid4().hex[:8]}",
                "function": {
                    "name": name,
                    "arguments": parse_json_dict(args_raw),
                },
            }
        )
    return dedupe_tool_calls(out)


def _strip_gemma_tool_call_blocks(content: str) -> str:
    if not content:
        return ""
    t = content
    t = _GEMMA_TOOL_CALL_FULL_RE.sub("", t)
    t = _GEMMA_TOOL_CALL_SIMPLE_RE.sub("", t)
    # 完整块 / 残缺开闭标签（模型常只吐出 <tool_call|>）
    t = re.sub(r"<\|?tool_call>[\s\S]*?<tool_call\|>", "", t, flags=re.IGNORECASE)
    t = re.sub(r"<\|tool_call>[\s\S]*$", "", t, flags=re.IGNORECASE)
    t = re.sub(r"</?\|?tool_call\|>", "", t, flags=re.IGNORECASE)
    t = re.sub(r"<\|tool_call>", "", t, flags=re.IGNORECASE)
    return t.strip()


def sanitize_llm_content(text: str) -> str:
    """剥离思考通道标记，保留对用户可见的正文。"""
    if not text:
        return ""
    t = _THINK_BLOCK_RE.sub("", text, count=1)
    t = _EMPTY_CHANNEL_BLOCK_RE.sub("", t)
    t = _REDACTED_THINKING_RE.sub("", t)
    t = _FINAL_TAG_RE.sub("", t)
    t = _THINK_LEADING_RE.sub("", t)
    t = _LEADING_CHANNEL_OPEN_RE.sub("", t)
    t = _CHANNEL_END_LEADING_RE.sub("", t)
    t = _strip_gemma_tool_call_blocks(t)
    return t.strip()


class _StreamContentSanitizer:
    """流式输出时过滤 Gemma 思考前导，避免标记闪现在 TTS/UI 上。"""

    def __init__(self) -> None:
        self._buffer = ""
        self._emitting = False

    def feed(self, chunk: str) -> str:
        if self._emitting or not chunk:
            return chunk
        self._buffer += chunk
        if "<channel|>" in self._buffer:
            _, _, tail = self._buffer.partition("<channel|>")
            self._emitting = True
            self._buffer = ""
            return tail.lstrip("\n")
        if not self._maybe_thinking_prefix(self._buffer):
            self._emitting = True
            out = self._buffer
            self._buffer = ""
            return out
        return ""

    def flush(self) -> str:
        if self._emitting:
            return ""
        out = sanitize_llm_content(self._buffer)
        self._buffer = ""
        self._emitting = True
        return out

    @staticmethod
    def _maybe_thinking_prefix(buf: str) -> bool:
        s = buf.lstrip()
        if not s:
            return True
        lowered = s.lower()
        return any(lowered.startswith(p) for p in _STREAM_THINK_PREFIXES)


def _normalize_assistant_message(msg: dict) -> dict:
    """统一 assistant 消息格式，供 Agent 循环使用"""
    tool_calls = []
    for i, tc in enumerate(msg.get("tool_calls") or []):
        fn = tc.get("function") or {}
        tool_calls.append(
            {
                "id": tc.get("id") or f"call_{i}_{uuid.uuid4().hex[:8]}",
                "function": {
                    "name": fn.get("name", ""),
                    "arguments": parse_json_dict(fn.get("arguments")),
                },
            }
        )
    raw_content = msg.get("content") or ""
    # 先从【原始】content 抢救漏出的 Gemma tool_call：
    # sanitize_llm_content 内部会调 _strip_gemma_tool_call_blocks 剥掉 <|tool_call> 标记，
    # 必须在 sanitize 之前提取，否则标记已被剥光、抢救永远拿不到（漏出时回复变空）。
    if not tool_calls:
        leaked = _extract_gemma_tool_calls_from_content(raw_content)
        if leaked:
            logger.info("从 content 解析到 %d 个 Gemma tool_call", len(leaked))
            tool_calls = leaked
    # sanitize 已含 _strip_gemma_tool_call_blocks，无论是否抢救到都把泄漏标记剥干净
    content = sanitize_llm_content(raw_content)
    tool_calls = dedupe_tool_calls(tool_calls)
    if len(tool_calls) < len(msg.get("tool_calls") or []):
        logger.debug(
            "tool_calls 去重: %d -> %d",
            len(msg.get("tool_calls") or []),
            len(tool_calls),
        )
    return {
        "role": "assistant",
        "content": content,
        "tool_calls": tool_calls,
    }


def _merge_system_messages(messages: list[dict]) -> list[dict]:
    """部分 API（如 SiliconFlow）要求 system 只能在开头且仅一条"""
    if not messages:
        return messages
    parts: list[str] = []
    idx = 0
    while idx < len(messages) and messages[idx].get("role") == "system":
        content = (messages[idx].get("content") or "").strip()
        if content:
            parts.append(content)
        idx += 1
    if not parts:
        return messages
    return [{"role": "system", "content": "\n\n".join(parts)}] + messages[idx:]


def _messages_have_image(messages: list[dict]) -> bool:
    """messages 中是否包含图片（image_url 多模态片段，如截图注入）"""
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        content = m.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    return True
    return False


class LLMProvider(ABC):
    def __init__(self, config: BrainConfig):
        self.config = config

    @property
    def model(self) -> str:
        return self.config.model

    def _resolve_model(self, messages: list[dict]) -> str:
        """含图请求走视觉模型（配置了 vision_model 时），其余走主模型"""
        vision_model = (getattr(self.config, "vision_model", "") or "").strip()
        if vision_model and _messages_have_image(messages):
            return vision_model
        return self.config.model

    @abstractmethod
    def is_available(self) -> bool: ...

    @abstractmethod
    def chat_once(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice: str | None = None,
    ) -> dict | None: ...

    @abstractmethod
    def chat_stream(
        self,
        messages: list[dict],
        on_chunk: Callable[[str], None] | None = None,
        on_start: Callable[[], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> str | None: ...

    def tool_result_message(self, tool_call: dict, content: str) -> dict:
        msg = {"role": "tool", "content": content}
        tc_id = tool_call.get("id")
        if tc_id:
            msg["tool_call_id"] = tc_id
        return msg


class OllamaProvider(LLMProvider):
    _AVAIL_TTL_SEC = 60.0

    def __init__(self, config: BrainConfig):
        super().__init__(config)
        self._avail_cache: tuple[float, bool] | None = None

    def is_available(self) -> bool:
        now = time.monotonic()
        if self._avail_cache and now < self._avail_cache[0]:
            return self._avail_cache[1]
        try:
            r = requests.get(f"{self.config.base_url}/api/tags", timeout=3)
            ok = r.status_code == 200
        except Exception:
            ok = False
        self._avail_cache = (now + (self._AVAIL_TTL_SEC if ok else 5.0), ok)
        return ok

    def _to_ollama_messages(self, messages: list[dict]) -> list[dict]:
        result = []
        for m in messages:
            role = m.get("role")
            if role == "tool":
                item = {"role": "tool", "content": m.get("content", "")}
                if m.get("tool_call_id"):
                    item["tool_call_id"] = m["tool_call_id"]
                result.append(item)
                continue
            if role == "assistant" and m.get("tool_calls"):
                ollama_tc = []
                for tc in m["tool_calls"]:
                    fn = tc.get("function") or {}
                    args = fn.get("arguments", {})
                    if isinstance(args, dict):
                        args = json.dumps(args, ensure_ascii=False)
                    ollama_tc.append(
                        {
                            "function": {
                                "name": fn.get("name", ""),
                                "arguments": args,
                            }
                        }
                    )
                result.append(
                    {
                        "role": "assistant",
                        "content": m.get("content") or "",
                        "tool_calls": ollama_tc,
                    }
                )
                continue
            if role == "user" and isinstance(m.get("content"), list):
                text_parts: list[str] = []
                images: list[str] = []
                for part in m["content"]:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") == "text":
                        text_parts.append(part.get("text", ""))
                    elif part.get("type") == "image_url":
                        url = (part.get("image_url") or {}).get("url", "")
                        if url.startswith("data:") and "base64," in url:
                            images.append(url.split("base64,", 1)[1])
                item = {
                    "role": "user",
                    "content": "\n".join(text_parts) or "请分析这张截图。",
                }
                if images:
                    item["images"] = images
                result.append(item)
                continue
            result.append(
                {"role": role, "content": m.get("content", "")}
            )
        return result

    def chat_once(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice: str | None = None,
    ) -> dict | None:
        payload: dict = {
            "model": self._resolve_model(messages),
            "messages": self._to_ollama_messages(messages),
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
        if tool_choice and tools:
            payload["tool_choice"] = tool_choice
        try:
            resp = requests.post(
                f"{self.config.base_url}/api/chat",
                json=payload,
                timeout=self.config.timeout_sec,
            )
            if resp.status_code != 200:
                logger.error("Ollama HTTP %s: %s", resp.status_code, resp.text[:200])
                return None
            data = resp.json()
            msg = data.get("message") or {}
            return _normalize_assistant_message(msg)
        except requests.exceptions.ConnectionError:
            logger.error("无法连接 Ollama，请确认已启动：ollama serve")
            return None
        except Exception as e:
            logger.error("Ollama 请求异常: %s", e)
            return None

    def chat_stream(
        self,
        messages: list[dict],
        on_chunk: Callable[[str], None] | None = None,
        on_start: Callable[[], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> str | None:
        full_reply = ""
        started = False
        sanitizer = _StreamContentSanitizer()
        try:
            resp = requests.post(
                f"{self.config.base_url}/api/chat",
                json={
                    "model": self._resolve_model(messages),
                    "messages": self._to_ollama_messages(messages),
                    "stream": True,
                },
                stream=True,
                timeout=self.config.timeout_sec,
            )
            if resp.status_code != 200:
                logger.error("Ollama HTTP %s: %s", resp.status_code, resp.text[:200])
                return None
            for line in resp.iter_lines(decode_unicode=True):
                if cancel_check and cancel_check():
                    break
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                content = data.get("message", {}).get("content", "")
                if content:
                    cleaned = sanitizer.feed(content)
                    if cleaned:
                        if not started:
                            started = True
                            if on_start:
                                on_start()
                        full_reply += cleaned
                        if on_chunk:
                            on_chunk(cleaned)
                if data.get("done"):
                    break
            tail = sanitizer.flush()
            if tail:
                if not started:
                    started = True
                    if on_start:
                        on_start()
                full_reply += tail
                if on_chunk:
                    on_chunk(tail)
            return sanitize_llm_content(full_reply) or None
        except requests.exceptions.ConnectionError:
            logger.error("无法连接 Ollama，请确认已启动：ollama serve")
            return None
        except Exception as e:
            logger.error("Ollama 请求异常: %s", e)
            return None


class OpenAICompatProvider(LLMProvider):
    """OpenAI 兼容 API — 支持 SiliconFlow / DeepSeek / 自建网关等"""

    _AVAIL_TTL_SEC = 60.0

    def __init__(self, config: BrainConfig):
        super().__init__(config)
        self._client = None
        self._avail_cache: tuple[float, bool] | None = None

    def _get_client(self):
        if self._client is None:
            from openai import OpenAI

            # 分层超时：连接 8 秒，读取 120 秒（流式需要长读取）
            # 本地服务（LM Studio 等）无需鉴权，空 key 时用占位符避免 SDK 报错
            api_key = self.config.api_key or "local-no-key"
            self._client = OpenAI(
                api_key=api_key,
                base_url=self.config.normalized_base_url(),
                timeout=httpx.Timeout(
                    connect=8.0,
                    read=float(self.config.timeout_sec),
                    write=10.0,
                    pool=5.0,
                ),
            )
        return self._client

    def _list_model_ids(self) -> set[str]:
        try:
            resp = self._get_client().models.list()
            return {
                str(m.id)
                for m in (resp.data or [])
                if getattr(m, "id", None)
            }
        except Exception as e:
            logger.debug("models.list 失败: %s", e)
            return set()

    def _probe_inference(self, *, quiet: bool = False) -> bool:
        """可选推理探针（管理后台测试用）；启动检查不依赖此项。"""
        try:
            self._get_client().chat.completions.create(
                model=self.config.model,
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=1,
                timeout=min(15.0, float(self.config.timeout_sec)),
            )
            return True
        except Exception as e:
            log = logger.debug if quiet else logger.warning
            log(
                "LLM 推理探针失败 [%s] model=%s: %s",
                self.config.normalized_base_url(),
                self.config.model,
                e,
            )
            return False

    def is_available(self) -> bool:
        """可用性检查：带 TTL 缓存，避免每轮对话重复发起同步 /models HTTP 请求。"""
        now = time.monotonic()
        if self._avail_cache and now < self._avail_cache[0]:
            return self._avail_cache[1]
        ok = self._check_available_uncached()
        self._avail_cache = (now + (self._AVAIL_TTL_SEC if ok else 5.0), ok)
        return ok

    def _check_available_uncached(self) -> bool:
        local = is_local_llm_url(self.config.base_url)
        ids = self._list_model_ids()
        target = (self.config.model or "").strip()
        if ids and target:
            if target in ids:
                return True
            if local:
                logger.warning(
                    "配置的模型「%s」不在 /models 列表中，可用: %s",
                    target,
                    ", ".join(sorted(ids)[:8]),
                )
                return False
            logger.info(
                "模型「%s」不在云端 /models 列表中（列表仅含部分型号），以实际推理为准放行",
                target,
            )
            return bool(self.config.base_url or self.config.api_key)
        if local:
            # 部分本地服务无 /models，退化为探针或仅校验 base_url
            if ids:
                return False
            return self._probe_inference() or bool(self.config.base_url)
        if ids:
            return False
        return bool(self.config.base_url or self.config.api_key)

    def _format_api_error(self, exc: Exception) -> str:
        parts = [str(exc)]
        status = getattr(exc, "status_code", None)
        if status is not None:
            parts.insert(0, f"HTTP {status}")
        body = getattr(exc, "body", None)
        if body:
            parts.append(f"body={body}")
        return " ".join(parts)

    def _sdk_message_to_dict(self, msg) -> dict:
        content = getattr(msg, "content", None) or ""
        if not content:
            reasoning = getattr(msg, "reasoning_content", None) or ""
            if reasoning:
                content = reasoning
        data: dict = {
            "role": getattr(msg, "role", None) or "assistant",
            "content": content,
        }
        tool_calls = getattr(msg, "tool_calls", None) or []
        if tool_calls:
            data["tool_calls"] = [
                {
                    "id": tc.id,
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in tool_calls
            ]
        return data

    def _to_openai_messages(self, messages: list[dict]) -> list[dict]:
        result = []
        for m in messages:
            role = m.get("role")
            if role == "assistant" and m.get("tool_calls"):
                openai_tc = []
                for tc in m["tool_calls"]:
                    fn = tc.get("function") or {}
                    args = fn.get("arguments", {})
                    if isinstance(args, dict):
                        args = json.dumps(args, ensure_ascii=False)
                    openai_tc.append(
                        {
                            "id": tc.get("id") or f"call_{uuid.uuid4().hex[:12]}",
                            "type": "function",
                            "function": {
                                "name": fn.get("name", ""),
                                "arguments": args,
                            },
                        }
                    )
                result.append(
                    {
                        "role": "assistant",
                        "content": m.get("content") or None,
                        "tool_calls": openai_tc,
                    }
                )
                continue
            if role == "tool":
                item = {
                    "role": "tool",
                    "content": m.get("content", ""),
                }
                if m.get("tool_call_id"):
                    item["tool_call_id"] = m["tool_call_id"]
                result.append(item)
                continue
            if role == "user" and isinstance(m.get("content"), list):
                result.append({"role": "user", "content": m["content"]})
                continue
            result.append({"role": role, "content": m.get("content", "")})
        return result

    def chat_once(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice: str | None = None,
    ) -> dict | None:
        kwargs: dict = {
            "model": self._resolve_model(messages),
            "messages": self._to_openai_messages(
                _merge_system_messages(messages)
            ),
        }
        if not self.config.enable_thinking:
            kwargs["extra_body"] = {
                "enable_thinking": False,
                "chat_template_kwargs": {"enable_thinking": False},
            }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice or "auto"
            kwargs["parallel_tool_calls"] = False
        last_err: Exception | None = None
        for attempt in range(2):
            try:
                response = self._get_client().chat.completions.create(**kwargs)
                choices = response.choices or []
                if not choices:
                    return None
                msg = choices[0].message
                return _normalize_assistant_message(self._sdk_message_to_dict(msg))
            except Exception as e:
                last_err = e
                status = getattr(e, "status_code", None)
                if (
                    attempt == 0
                    and is_local_llm_url(self.config.base_url)
                    and status in (502, 503, 504)
                ):
                    logger.warning(
                        "OpenAI API 本地推理暂不可用 (HTTP %s)，2s 后重试…",
                        status,
                    )
                    import time

                    time.sleep(2)
                    continue
                break
        if last_err is not None:
            logger.error(
                "OpenAI API 请求异常 [%s]: %s",
                self.config.normalized_base_url(),
                self._format_api_error(last_err),
            )
        return None

    def chat_stream(
        self,
        messages: list[dict],
        on_chunk: Callable[[str], None] | None = None,
        on_start: Callable[[], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> str | None:
        full_reply = ""
        started = False
        sanitizer = _StreamContentSanitizer()
        try:
            stream_kwargs: dict = {
                "model": self._resolve_model(messages),
                "messages": self._to_openai_messages(
                    _merge_system_messages(messages)
                ),
                "stream": True,
            }
            if not self.config.enable_thinking:
                stream_kwargs["extra_body"] = {
                    "enable_thinking": False,
                    "chat_template_kwargs": {"enable_thinking": False},
                }
            stream = self._get_client().chat.completions.create(**stream_kwargs)
            for chunk in stream:
                if cancel_check and cancel_check():
                    break
                if not chunk.choices:
                    continue
                content = chunk.choices[0].delta.content or ""
                if content:
                    cleaned = sanitizer.feed(content)
                    if cleaned:
                        if not started:
                            started = True
                            if on_start:
                                on_start()
                        full_reply += cleaned
                        if on_chunk:
                            on_chunk(cleaned)
            tail = sanitizer.flush()
            if tail:
                if not started:
                    started = True
                    if on_start:
                        on_start()
                full_reply += tail
                if on_chunk:
                    on_chunk(tail)
            return sanitize_llm_content(full_reply) or None
        except Exception as e:
            logger.error(
                "OpenAI API 流式请求异常 [%s]: %s",
                self.config.normalized_base_url(),
                e,
            )
            return None

    def tool_result_message(self, tool_call: dict, content: str) -> dict:
        return {
            "role": "tool",
            "tool_call_id": tool_call.get("id", ""),
            "content": content,
        }


def create_provider(config: BrainConfig) -> LLMProvider:
    if config.provider == "openai":
        return OpenAICompatProvider(config)
    return OllamaProvider(config)


class Brain:
    """统一 LLM 入口，根据配置选择 Ollama 或 OpenAI 兼容 API

    支持配置热更新：以 config_dir 方式创建时，每次请求前检查 config
    文件的修改时间，文件变化即自动重建 provider（后台换模型无需重启）。
    显式传入 config 的实例（测试等场景）不参与热更新。
    """

    def __init__(self, config: BrainConfig | None = None, config_dir: Path | None = None):
        self._config_dir = config_dir
        self._reload_lock = threading.Lock()
        self.config = config or load_brain_config(config_dir)
        self._config_mtime = self._current_config_mtime() if config is None else 0.0
        self._provider = create_provider(self.config)

    def _current_config_mtime(self) -> float:
        base = self._config_dir or Path(__file__).parent
        # 与 load_brain_config 的读取优先级一致：config.json 优先于 config.yaml
        for name in ("config.json", "config.yaml"):
            p = base / name
            try:
                if p.is_file():
                    return p.stat().st_mtime
            except OSError:
                continue
        return 0.0

    def reload_if_changed(self) -> bool:
        """配置文件变化时热更新 BrainConfig 与 provider，返回是否发生重载"""
        if self._config_dir is None:
            return False
        try:
            mtime = self._current_config_mtime()
        except OSError:
            return False
        if not mtime or mtime == self._config_mtime:
            return False
        with self._reload_lock:
            mtime = self._current_config_mtime()
            if not mtime or mtime == self._config_mtime:
                return False
            try:
                cfg = load_brain_config(self._config_dir)
            except Exception as e:
                logger.warning("LLM 配置热更新失败，沿用旧配置: %s", e)
                self._config_mtime = mtime  # 避免每次请求重复解析失败
                return False
            self._config_mtime = mtime
            if cfg == self.config:
                return False
            old = self.config
            self.config = cfg
            self._provider = create_provider(cfg)
            logger.info(
                "LLM 配置已热更新: model=%s vision_model=%s provider=%s base_url=%s（原 model=%s）",
                cfg.model, cfg.vision_model, cfg.provider, cfg.base_url, old.model,
            )
            return True

    def reload(self) -> bool:
        """强制重载配置（忽略 mtime 判断），返回配置内容是否有变化"""
        if self._config_dir is None:
            return False
        self._config_mtime = 0.0
        return self.reload_if_changed()

    @property
    def provider(self) -> str:
        return self.config.provider

    @property
    def model(self) -> str:
        return self.config.model

    @property
    def base_url(self) -> str:
        return self.config.normalized_base_url()

    def is_available(self) -> bool:
        self.reload_if_changed()
        return self._provider.is_available()

    def chat_once(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice: str | None = None,
    ) -> dict | None:
        self.reload_if_changed()
        return self._provider.chat_once(
            messages, tools=tools, tool_choice=tool_choice
        )

    def tool_result_message(self, tool_call: dict, content: str) -> dict:
        return self._provider.tool_result_message(tool_call, content)

    @property
    def is_strong(self) -> bool:
        return is_strong_capability(self.config)

    def chat_stream(
        self,
        user_text: str,
        agent_id: str = "keks",
        context: str = "",
        on_chunk: Callable[[str], None] | None = None,
        on_start: Callable[[], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> str | None:
        self.reload_if_changed()
        system = get_system_prompt(agent_id, self.config)
        system_parts = [system]
        if context:
            system_parts.append(f"最近对话摘要：\n{context}")
        messages = [
            {"role": "system", "content": "\n\n".join(system_parts)},
            {"role": "user", "content": user_text},
        ]
        return self._provider.chat_stream(
            messages,
            on_chunk=on_chunk,
            on_start=on_start,
            cancel_check=cancel_check,
        )

    def chat_stream_messages(
        self,
        messages: list[dict],
        on_chunk: Callable[[str], None] | None = None,
        on_start: Callable[[], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> str | None:
        """流式对话（自定义 messages，用于 R3 等需要自定义 system prompt 的场景）。"""
        return self._provider.chat_stream(
            messages,
            on_chunk=on_chunk,
            on_start=on_start,
            cancel_check=cancel_check,
        )


# 向后兼容旧名称
LocalBrain = Brain
