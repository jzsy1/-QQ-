# -*- coding: utf-8 -*-
"""通用回复文本清洗与过滤算法（通用大脑核心模块）。

纯算法：不依赖 QQ、直播或任何具体业务模块。
QQ 与直播接入层共用同一套清洗规则，业务侧只提供 FilterConfig。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "DEFAULT_BLOCKED_WORDS",
    "DEFAULT_WAKE_WORDS",
    "DEFAULT_OWNER_NAMES",
    "FilterConfig",
    "clean_reply",
    "salvage_parenthetical",
    "fallback_line",
]

DEFAULT_BLOCKED_WORDS: tuple[str, ...] = (
    "加微信",
    "加vx",
    "加VX",
    "加QQ",
    "qq群",
    "QQ群",
    "私信我",
    "刷单",
    "兼职",
    "代练",
    "推广",
    "扫码",
    "http://",
    "https://",
    "www.",
    ".com",
)
DEFAULT_WAKE_WORDS: tuple[str, ...] = ("小金鱼",)
# 人设里现实主人的称呼；非主人发言时清掉顺嘴喊错的称呼。
DEFAULT_OWNER_NAMES: tuple[str, ...] = ("鱼片",)

_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_MD_MARK_RE = re.compile(r"[`*_~#>|]")
_EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F1E6-\U0001F1FF"
    "\uFE0F\u200D\u20E3"
    "]+"
)
_SPACE_RE = re.compile(r"\s+")
_PREFIX_RE = re.compile(r"^(?:回复|答|主播|我)\s*[:：]\s*")
_PAREN_RE = re.compile(r"[（(\[【][^）)\]】]{1,16}[）)\]】]")


@dataclass(frozen=True)
class FilterConfig:
    """清洗参数；默认值与直播历史配置保持一致。"""

    max_danmaku_chars: int = 80
    blocked_words: tuple[str, ...] = DEFAULT_BLOCKED_WORDS
    sensitive_words: tuple[str, ...] = ()
    wake_words: tuple[str, ...] = DEFAULT_WAKE_WORDS
    owner_names: tuple[str, ...] = DEFAULT_OWNER_NAMES


def _strip_owner_alias(value: str, owner_names: tuple[str, ...]) -> str:
    for owner in owner_names:
        owner = str(owner or "").strip()
        if not owner:
            continue
        value = re.sub(
            rf"(?:你这|啊|呀|喂|呢)?\s*{re.escape(owner)}(?=[，,。！!？?\s]|$)",
            "",
            value,
        ).strip()
        value = re.sub(r"^[，,、\s]+", "", value)
        value = re.sub(r"[，,、\s]+$", "", value)
    return value


def clean_reply(text: str, max_chars: int, filt: FilterConfig | None = None,
                is_owner: bool = False) -> str:
    """把 LLM 原文清洗成可发送的一条口语回复。

    纯函数，无 I/O。空结果表示这条不该发。
    """
    if not text:
        return ""
    filt = filt or FilterConfig()
    value = str(text).strip()
    value = _MD_LINK_RE.sub(r"\1", value)
    value = _URL_RE.sub("", value)
    value = _MD_MARK_RE.sub("", value)
    value = _EMOJI_RE.sub("", value)
    value = value.replace("\r", " ").replace("\n", " ")
    value = _SPACE_RE.sub(" ", value).strip()
    value = value.strip("\"'“”‘’「」『』 ")
    for word in filt.wake_words:
        if word:
            value = value.replace(word, "我")
    value = _PREFIX_RE.sub("", value)
    value = _PAREN_RE.sub("", value)
    value = _SPACE_RE.sub(" ", value).strip()
    value = value.strip("\"'“”‘’「」『』 ")
    if not is_owner:
        value = _strip_owner_alias(value, filt.owner_names)
    if not value:
        return ""
    lowered = value.lower()
    for word in filt.sensitive_words:
        if word and word.lower() in lowered:
            return ""
    if len(value) > max_chars:
        cut = value[:max_chars]
        stop = max(cut.rfind("。"), cut.rfind("！"), cut.rfind("？"), cut.rfind("；"))
        if stop >= max_chars // 2:
            cut = cut[: stop + 1]
        value = cut.rstrip("，、；：,;: ")
    if not any(ch.isalnum() or "\u4e00" <= ch <= "\u9fff" for ch in value):
        return ""
    return value


def salvage_parenthetical(text: str, max_chars: int,
                          filt: FilterConfig | None = None,
                          is_owner: bool = False) -> str:
    """LLM 只输出了括号动作词时，去掉括号再试一次清洗。"""
    if not text:
        return ""
    cand = re.sub(r"[()（）\[\]【】\"']", " ", str(text)).strip()
    cand = _SPACE_RE.sub(" ", cand).strip()
    if not cand:
        return ""
    return clean_reply(cand, max_chars, filt, is_owner=is_owner)


def fallback_line(is_owner: bool = False) -> str:
    """清洗过度或 LLM 空回复时的兜底，避免消息石沉大海。"""
    return "主人，我在呢。" if is_owner else "在呢。"
