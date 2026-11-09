# -*- coding: utf-8 -*-
"""通用回复生成管线。

负责「给 brain 一组 messages → 拿原文 → 清洗 → 兜底」这一段，
不关心 QQ 还是直播，也不持有任何业务状态。
"""

from __future__ import annotations

import logging
import time
from typing import Callable

from .reply_text import (
    FilterConfig,
    clean_reply,
    fallback_line,
    salvage_parenthetical,
)

logger = logging.getLogger(__name__)

__all__ = ["cancel_checker", "generate_reply"]


def cancel_checker(stop_event, deadline: float) -> Callable[[], bool]:
    """统一取消检查：业务 stop_event 或超时任一满足即取消。"""

    def _check() -> bool:
        if stop_event is not None and stop_event.is_set():
            return True
        return time.time() > deadline

    return _check


def _call_brain(brain, messages: list[dict], cancel_check, deadline: float) -> str:
    """优先走流式接口；brain 只实现了 chat_once 时自动降级。"""
    stream = getattr(brain, "chat_stream_messages", None)
    if callable(stream):
        return str(stream(messages, cancel_check=cancel_check) or "")
    once = getattr(brain, "chat_once", None)
    if not callable(once):
        raise RuntimeError("brain_unavailable")
    msg = once(messages)
    if isinstance(msg, dict):
        return str(msg.get("content") or "")
    return str(msg or "")


def generate_reply(
    brain,
    messages: list[dict],
    max_chars: int,
    filter_cfg: FilterConfig | None = None,
    *,
    stop_event=None,
    timeout_sec: float = 30.0,
    is_owner: bool = False,
    fallback: bool = True,
) -> tuple[str, str]:
    """生成一条可发送的回复，返回 (reply, error)。

    失败时 reply 为空（fallback=True 时返回兜底句）；error 用于日志。
    """
    if brain is None:
        return (fallback_line(is_owner) if fallback else ""), "brain_unavailable"
    filt = filter_cfg or FilterConfig()
    deadline = time.time() + max(1.0, float(timeout_sec or 30.0))
    check = cancel_checker(stop_event, deadline)
    try:
        raw = _call_brain(brain, messages, check, deadline)
    except Exception as exc:  # noqa: BLE001 - 业务侧只记录原因
        logger.warning("回复生成失败", exc_info=True)
        return (fallback_line(is_owner) if fallback else ""), f"llm_error: {exc}"

    reply = clean_reply(raw, max_chars, filt, is_owner=is_owner)
    if not reply and raw:
        reply = salvage_parenthetical(raw, max_chars, filt, is_owner=is_owner)
    if not reply:
        if fallback:
            return fallback_line(is_owner), "empty_reply"
        return "", "empty_reply"
    return reply, ""
