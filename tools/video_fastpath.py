"""看剧写死快路径：意图命中后不交给 LLM 选 open_app。

走独立 video_space；看剧前可静音暂停听歌（不修改音乐实现文件）。
"""

from __future__ import annotations

import logging
from typing import Any

from kekesi.tools.video_prefs import (
    DEFAULT_SOURCE,
    choice_prompt,
    extract_episode,
    extract_video_query,
    extract_video_query_loose,
    get_pending_episode,
    get_pending_query,
    get_video_pref,
    is_close_video,
    is_video_intent,
    parse_source_from_text,
    set_pending,
    set_video_pref,
    should_defer_video_to_llm,
    source_label,
)
from kekesi.tools.web_video_player import close_video, play_video

logger = logging.getLogger(__name__)


def _pause_music_quiet() -> list[dict]:
    tools: list[dict] = []
    try:
        from kekesi.tools.web_music_player import control_web_music

        ok, msg = control_web_music("pause")
        tools.append(
            {
                "func": "control_music",
                "args": {"action": "pause"},
                "ok": ok,
                "text": msg,
            }
        )
    except Exception as e:
        tools.append(
            {
                "func": "control_music",
                "args": {"action": "pause"},
                "ok": False,
                "text": str(e),
            }
        )
    return tools


def try_video_fastpath(
    user_text: str,
    memory,
    *,
    force: bool = False,
) -> tuple[str, list[dict]] | None:
    text = (user_text or "").strip()
    if not text:
        return None
    if not force and not is_video_intent(text):
        return None

    # 最新一集 / 指代等 → 交给 LLM 调 play_video；force 时仍走可解析的点播
    if not force and should_defer_video_to_llm(text):
        logger.info("video fastpath defer → LLM: %s", text[:80])
        return None

    if is_close_video(text):
        tools: list[dict] = []
        ok, msg = close_video()
        tools.append(
            {
                "func": "close_video",
                "args": {},
                "ok": ok,
                "text": msg,
            }
        )
        if ok:
            return "好，已关掉视频。", tools
        return f"没能关掉视频：{msg}", tools

    explicit = parse_source_from_text(text)
    # force（门已开 video）时补抽裸「播放XX」，与直连 play_video 对齐
    query = extract_video_query(text) or (
        extract_video_query_loose(text) if force else ""
    )
    episode = extract_episode(text)
    pref = get_video_pref(memory)
    pending = get_pending_query(memory)
    pending_ep = get_pending_episode(memory)

    # 仅选源站
    if explicit and not query:
        sid = set_video_pref(memory, explicit)
        label = source_label(sid)
        if pending:
            set_pending(memory, "", None)
            tools = _pause_music_quiet()
            ok, msg = play_video(pending, episode=pending_ep, source=sid)
            tools.append(
                {
                    "func": "play_video",
                    "args": {
                        "query": pending,
                        "episode": pending_ep,
                        "source": sid,
                    },
                    "ok": ok,
                    "text": msg,
                }
            )
            if ok:
                return f"已设默认{label}。{msg}", tools
            return f"已设默认{label}，但打开失败：{msg}", tools
        return f"好，默认用{label}看剧。想看什么直接说「看剧名第N集」。", []

    # 有剧名但未选源
    if query and not explicit and not pref:
        set_pending(memory, query, episode)
        note = f"第{episode}集" if episode else ""
        return (
            choice_prompt() + f"（已记下要看「{query}」{note}）",
            [],
        )

    # 有剧名 → 播放
    if query:
        sid = explicit or pref or DEFAULT_SOURCE
        if explicit:
            set_video_pref(memory, explicit)
        set_pending(memory, "", None)
        tools = _pause_music_quiet()
        ok, msg = play_video(query, episode=episode, source=sid)
        tools.append(
            {
                "func": "play_video",
                "args": {"query": query, "episode": episode, "source": sid},
                "ok": ok,
                "text": msg,
            }
        )
        if ok:
            return msg, tools
        # 未登录时 play_video 已弹扫码框，直接把提示给用户（对齐听歌）
        if "登录" in msg or "扫码" in msg:
            tools.append(
                {
                    "func": "video_login",
                    "args": {"source": sid},
                    "ok": True,
                    "text": msg,
                }
            )
            return msg, tools
        return f"看剧没成功：{msg}", tools

    if pref:
        return (
            f"默认用{source_label(pref)}。想看哪部？说「看剧名第N集」就行。",
            [],
        )

    return choice_prompt(), []
