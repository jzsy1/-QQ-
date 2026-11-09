# -*- coding: utf-8 -*-
"""静态指令训练：动态执行成功后跨轮询问"是否已完成"，用户确认后固化为静态指令。

仿 pending_delete 的跨轮确认范式：本轮 set pending（存 user_profile pref），
下一轮 voice 输入时 consume 确认并固化。整个生命周期不依赖 LLM 猜流程。
"""

from __future__ import annotations

import json
import logging

from kekesi.tools.static_instruction import (
    compile_static_instruction,
    sanitize_steps_for_static,
)

logger = logging.getLogger(__name__)

PENDING_COMPILE_KEY = "pending_compile_steps"    # 净化后步骤 JSON
PENDING_COMPILE_TRIG = "pending_compile_trigger"  # 规范化 trigger 原话
_PENDING_COMPILE_OFFER = "pending_compile_offer_at"  # 上次 offer 时间戳（去抖）

_HARD_CONFIRM = (
    "已完成",
    "完成",
    "记下来",
    "记住了",
    "记住吧",
    "好的记住",
    "要记住",
    "记住",
)

# 去除确认动词后带"记录/记住/固化"含义的动作词
_SOFT_CONFIRM_RE_ = ("记得", "记一下吧", "能记住吗", "下次更快", "存下来", "保存下来", "固话")


def should_offer_compile(
    executed_tools: list[dict], *, min_steps: int = 2
) -> bool:
    """是否有足够多、全部成功的真实动作可固化（selfcheck/只读/mcp 已被净化函数过滤）。"""
    if not executed_tools:
        return False
    steps = sanitize_steps_for_static(executed_tools, min_steps=min_steps)
    return len(steps) >= min_steps


def set_pending_compile(memory, steps: list[dict], trigger: str, session_id: str) -> bool:
    """记下待固化步骤与 trigger，供下轮确认时入库。返回是否写入成功。"""
    if memory is None or not steps:
        return False
    try:
        memory.set_pref(PENDING_COMPILE_KEY, json.dumps(steps, ensure_ascii=False))
        memory.set_pref(PENDING_COMPILE_TRIG, (trigger or "").strip()[:120])
        memory.set_pref(_PENDING_COMPILE_OFFER, str(session_id))
        logger.info("pending compile set: %d 步 trigger=%r", len(steps), trigger[:30])
        return True
    except Exception as e:  # noqa: BLE001
        logger.debug("set pending compile failed: %s", e)
        return False


def clear_pending_compile(memory) -> None:
    if memory is None:
        return
    try:
        memory.set_pref(PENDING_COMPILE_KEY, "")
        memory.set_pref(PENDING_COMPILE_TRIG, "")
        memory.set_pref(_PENDING_COMPILE_OFFER, "")
    except Exception:
        pass


def user_confirms_compile(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if any(m in t for m in _HARD_CONFIRM):
        return True
    for w in _SOFT_CONFIRM_RE_:
        if w in t:
            return True
    return False


def try_consume_pending_compile(
    user_text: str,
    memory,
    db,
) -> tuple[str, list[dict]] | None:
    """若本轮是确认且有待固化步骤，固化为静态指令，返回 (reply, [])；否则 None 不打断。"""
    if memory is None or db is None:
        return None
    steps_raw = (memory.get_pref(PENDING_COMPILE_KEY) or "").strip()
    if not steps_raw:
        return None
    if not user_confirms_compile(user_text):
        return None
    try:
        steps = json.loads(steps_raw)
    except json.JSONDecodeError:
        clear_pending_compile(memory)
        return "没记住（步骤数据异常），已忽略。", []
    if not isinstance(steps, list) or not steps:
        clear_pending_compile(memory)
        return "没记住（没有可固化的动作），已忽略。", []

    trigger = (memory.get_pref(PENDING_COMPILE_TRIG) or "").strip()
    from kekesi.tools.static_instruction import _normalize_trigger

    ok, msg = compile_static_instruction(
        db, steps, trigger, min_steps=max(2, len(steps))
    )
    clear_pending_compile(memory)
    if ok:
        return f"好的，已记住这套操作，下次类似请求会直接照做。", []
    return f"没记住：{msg}", []