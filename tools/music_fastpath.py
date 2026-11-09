"""听歌写死快路径：意图命中后不交给 LLM 选工具。"""

from __future__ import annotations

import logging
from typing import Any

from kekesi.tools.music_prefs import (
    DEFAULT_SOURCE,
    choice_prompt,
    extract_song_query,
    get_music_pref,
    get_pending_query,
    is_close_music,
    is_control_only,
    is_liked_playlist_intent,
    is_music_intent,
    parse_control_action,
    parse_source_from_text,
    set_music_pref,
    set_pending_query,
    should_defer_music_to_llm,
    source_label,
)
from kekesi.tools.web_music_player import (
    close_qqmusic,
    control_web_music,
    music_login,
    play_music,
)

logger = logging.getLogger(__name__)


def try_music_fastpath(
    user_text: str,
    memory,
    *,
    force: bool = False,
) -> tuple[str, list[dict]] | None:
    """若命中听歌意图且规则可处理，返回 (reply, tool_results)；否则 None（交给 LLM）。"""
    text = (user_text or "").strip()
    if not text:
        return None
    if not force and not is_music_intent(text):
        return None

    # 非白名单（无明确歌名等）→ 交给 LLM；force 时仍允许控制/关音乐/有歌名
    if not force and should_defer_music_to_llm(text):
        logger.info("music fastpath defer → LLM: %s", text[:80])
        return None

    # 1) 关闭音乐：停播并收起体内播放器（禁止走 close_app 误杀）
    if is_close_music(text):
        tools: list[dict] = []
        try:
            ok_p, msg_p = control_web_music("pause")
            tools.append(
                {
                    "func": "control_music",
                    "args": {"action": "pause"},
                    "ok": ok_p,
                    "text": msg_p,
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
        ok_c, msg_c = close_qqmusic()
        tools.append(
            {
                "func": "close_music_space",
                "args": {},
                "ok": ok_c,
                "text": msg_c,
            }
        )
        if ok_c or any(t.get("ok") for t in tools):
            return "好，已关掉正在播的歌。", tools
        return f"没能关掉音乐：{msg_c}", tools

    # 2) 纯控制：暂停/下一首…
    if is_control_only(text):
        action = parse_control_action(text) or "playpause"
        ok, msg = control_web_music(action)
        tools = [
            {
                "func": "control_music",
                "args": {"action": action},
                "ok": ok,
                "text": msg,
            }
        ]
        reply = msg if ok else f"没能控制播放：{msg}"
        return reply, tools

    # 2.5) 「我喜欢」歌单（登录后从收藏列表播，不是搜歌名）
    if is_liked_playlist_intent(text):
        explicit = parse_source_from_text(text)
        pref = get_music_pref(memory)
        sid = explicit or pref or DEFAULT_SOURCE
        if explicit:
            set_music_pref(memory, explicit)
        ok, msg = play_music("", source=sid, show=False, playlist="liked")
        tools = [
            {
                "func": "play_music",
                "args": {"playlist": "liked", "source": sid},
                "ok": ok,
                "text": msg,
            }
        ]
        if ok:
            return msg, tools
        if "登录" in msg or "扫码" in msg:
            music_login(sid)
        return f"没能播放「我喜欢」：{msg}", tools

    # 2) 设置默认播放器（可附带歌名）
    explicit = parse_source_from_text(text)
    query = extract_song_query(text)
    pref = get_music_pref(memory)
    pending = get_pending_query(memory)

    # 仅选播放器（无新歌名）：「网易云」「以后用酷狗」
    if explicit and not query:
        sid = set_music_pref(memory, explicit)
        label = source_label(sid)
        song = pending
        if song:
            set_pending_query(memory, "")
            ok, msg = play_music(song, source=sid, show=False)
            tools = [
                {
                    "func": "play_music",
                    "args": {"query": song, "source": sid},
                    "ok": ok,
                    "text": msg,
                }
            ]
            if ok:
                return f"已设默认{label}。{msg}", tools
            return f"已设默认{label}，但点歌失败：{msg}", tools
        return f"好，默认用{label}听歌。想听什么直接说「我想听歌名」。", []

    # 3) 有歌名但未选播放器 → 询问并挂起歌名
    if query and not explicit and not pref:
        set_pending_query(memory, query)
        return choice_prompt() + f"（已记下要听「{query}」）", []

    # 4) 有歌名 → 用点名平台或默认偏好播放
    if query:
        sid = explicit or pref or DEFAULT_SOURCE
        if explicit:
            set_music_pref(memory, explicit)
        set_pending_query(memory, "")
        ok, msg = play_music(query, source=sid, show=False)
        tools = [
            {
                "func": "play_music",
                "args": {"query": query, "source": sid},
                "ok": ok,
                "text": msg,
            }
        ]
        if ok:
            return msg, tools
        if "登录" in msg or "扫码" in msg:
            music_login(sid)
        return f"点歌没成功：{msg}", tools

    # 5) 无歌名但已有偏好：「我想听歌」
    if pref:
        return f"默认用{source_label(pref)}。想听哪首？说「我想听歌名」就行。", []

    # 6) 无偏好无歌名
    return choice_prompt(), []
