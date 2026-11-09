"""跨模块提醒通知 — 任务调度器等回调，由语音壳注册 TTS。"""

from __future__ import annotations

import logging
from typing import Callable

logger = logging.getLogger(__name__)

_reminder_callbacks: list[Callable[[str], None]] = []


def register_reminder_callback(callback: Callable[[str], None]) -> None:
    if callback not in _reminder_callbacks:
        _reminder_callbacks.append(callback)


def has_reminder_callbacks() -> bool:
    return bool(_reminder_callbacks)


def notify_reminder(text: str) -> None:
    msg = (text or "").strip()
    if not msg:
        return
    for cb in list(_reminder_callbacks):
        try:
            cb(msg)
        except Exception as e:
            logger.error("提醒回调失败: %s", e)
