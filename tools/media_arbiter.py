# -*- coding: utf-8 -*-
"""快路径仲裁：意图路由 → video / music / search 工具链，其余交 Agent Loop。"""

from __future__ import annotations

import logging
import re
from typing import Any

from kekesi.tools.media_intent import (
    IntentRoute,
    is_local_system_query,
    is_weather_query,
    looks_like_route_json,
    music_hard,
    resolve_intent_route,
    video_hard,
)

logger = logging.getLogger(__name__)

__all__ = [
    "music_hard",
    "resolve_media_fastpath",
    "try_video_pause_resume",
    "video_hard",
    "video_window_visible",
]

_BARE_PAUSE_RE = re.compile(
    r"^(?:请|帮我|给我)?(?:暂停|继续播放|继续放|恢复播放|继续)(?:一下)?$"
)

# 成本闸：疑似媒体/检索的说法才值得花一次 LLM 轻分类；
# 未命中的说法（打开记事本/微信发消息等）直接回主 Loop，零额外延迟。
# 误放行无害（分类器会判 chat → 回主 Loop），漏放行也无害（主 Loop 兜底）。
_MEDIA_CLASSIFY_HINT_RE = re.compile(
    r"听歌|放歌|点歌|听音乐|放音乐|来一首|放一首|下一首|上一首|切歌|暂停音乐|关闭音乐|歌单|曲|音乐|唱|播放|"
    r"看剧|追剧|看电影|看视频|看动漫|看综艺|电视剧|电影|动漫|综艺|视频|第\s*\d+\s*集|最新一?集|"
    r"热点|热搜|新闻|资讯|头条|天气|气温|预报|热播",
    re.I,
)

# 接续门禁：指代/接续话语单看一句没有完整信息（「换一个别的剧」「放刚才那首」），
# 快路径分类器拿不到语义，直接回主 Loop 用完整历史处理（Homebot: context-aware routing）。
# 只拦白名单 3；硬白名单 0-2（暂停/剧名点播）在门禁之前已返回。
# 「其他/其余」这类含「他」的词不在门禁词表，避免误伤正常说法。
_CONTINUATION_GATE_RE = re.compile(
    r"换个|换一个|换部|换首|换一集|刚才|刚刚|那个|这些|那些|"
    r"它(?!们)|接着|再(来|放|次|遍)|别(的|个)|继续(?!播)|还有吗"
)

# 直播点歌开关/状态：这些说法含「歌」，会被音乐快路径当成歌名去播
# （实测「开点歌」→ query=开点歌，「点歌状态」→ query=状态）。
# 必须回主 Loop 不能进媒体分类。
_LIVE_SONG_CONTROL_RE = re.compile(
    r"^(?:请|帮我|给我|把)?"
    r"(?:(?:开|开启|打开|允许|开通|关|关闭|停|停止|关掉|停了|别)"
    r"(?:直播)?点歌(?:权限|功能|台)?(?:了|吧|啊)?"
    r"|(?:直播)?点歌(?:台)?(?:开|关|开启|关闭)(?:了|吧)?"
    r"|(?:开|关)点唱(?:了|吧)?"
    r"|点歌(?:台)?(?:状态|池|情况|统计)"
    r"|点歌台"
    r"|有人在点歌吗"
    r"|(?:现在放|放的|在放|放)什么歌)$"
)


def looks_like_continuation(text: str) -> bool:
    """疑似接续话语：指代词命中且无书名号/明确剧名歌名标记。"""
    t = (text or "").strip()
    if not t or len(t) > 16:
        return False
    if "《" in t or "》" in t:
        return False
    return bool(_CONTINUATION_GATE_RE.search(t))

_ROUTE_LAYER = {
    "video": "video_fastpath",
    "music": "music_fastpath",
    "search": "search_fastpath",
}


def is_bare_pause_resume(text: str) -> bool:
    return bool(_BARE_PAUSE_RE.match((text or "").strip()))


def video_window_visible() -> bool:
    try:
        from kekesi.video_space.client import ensure_space, _request

        ok, _msg, port = ensure_space(start=False)
        if not ok or port is None:
            return False
        r = _request("GET", "/status", port=port, timeout=8.0)
        return bool(r.get("ok") and r.get("visible"))
    except Exception:
        return False


def try_video_pause_resume(text: str) -> tuple[str, list[dict]] | None:
    t = (text or "").strip()
    if not is_bare_pause_resume(t):
        return None
    if not video_window_visible():
        return None
    action = "pause" if "暂停" in t else "play"
    try:
        from kekesi.tools.web_video_player import control_video

        ok, msg = control_video(action)
    except Exception as e:
        ok, msg = False, str(e)
    tools = [
        {
            "func": "control_video",
            "args": {"action": action},
            "ok": ok,
            "text": msg,
        }
    ]
    if ok:
        reply = "好，已暂停视频。" if action == "pause" else "好，继续播放视频。"
        return reply, tools
    return f"视频控制失败：{msg}", tools


def _run_search_route(
    user_text: str,
    route: IntentRoute,
    brain: Any | None,
) -> tuple[str, list[dict]]:
    from kekesi.tools.web_research import search_web

    q = (route.query or user_text or "").strip()
    # 去掉口头套话，避免搜索串过脏
    q = re.sub(
        r"^(?:请|帮我|给我)?(?:看一下|瞅一眼|查一下|搜一下|搜索|查查|看看)\s*",
        "",
        q,
    ).strip() or q
    ok, raw = search_web(q, max_results=4, fetch_pages=False)
    tools = [
        {
            "func": "search_web",
            "args": {"query": q, "max_results": 4},
            "ok": ok,
            "text": (raw or "")[:240],
        }
    ]
    if not ok:
        return f"搜索没成功：{raw}", tools

    # 主脑压成口语短答（与分类同一模型）
    reply = raw
    if brain is not None and getattr(brain, "is_available", lambda: False)():
        try:
            msg = brain.chat_once(
                [
                    {
                        "role": "system",
                        "content": (
                            "根据搜索结果，用两三句口语向用户汇报，适合语音朗读。"
                            "不要念网址，不要 markdown，不要列很长清单。"
                        ),
                    },
                    {
                        "role": "user",
                        "content": f"用户问：{user_text}\n\n搜索结果：\n{raw[:3500]}",
                    },
                ]
            )
            if isinstance(msg, dict):
                content = str(msg.get("content") or "").strip()
                # 摘要若又吐成路由 JSON，丢掉，改用搜索原文片段
                if content and not looks_like_route_json(content):
                    reply = content[:400]
                else:
                    reply = raw[:280]
        except Exception as e:
            logger.debug("search summarize failed: %s", e)
            reply = raw[:280]
    else:
        reply = raw[:280]
    return reply, tools


def _run_video_route(
    user_text: str,
    route: IntentRoute,
    memory,
) -> tuple[str, list[dict]] | None:
    from kekesi.tools.video_fastpath import try_video_fastpath
    from kekesi.tools.video_prefs import (
        DEFAULT_SOURCE,
        get_video_pref,
        is_close_video,
        resolve_video_play_args,
    )
    from kekesi.tools.web_video_player import play_video

    if is_close_video(user_text):
        return try_video_fastpath(user_text, memory, force=True)

    # 与直连 play_video 同一套剧名/集数解析（含裸「播放XX」补抽）
    q, ep = resolve_video_play_args(
        user_text,
        route_query=route.query or "",
        route_episode=route.episode,
    )
    if q:
        sid = get_video_pref(memory) or DEFAULT_SOURCE
        tools: list[dict] = []
        try:
            from kekesi.tools.web_music_player import control_web_music

            ok_p, msg_p = control_web_music("pause")
            tools.append(
                {
                    "func": "control_music",
                    "args": {"action": "pause"},
                    "ok": ok_p,
                    "text": msg_p,
                }
            )
        except Exception:
            pass
        ok, msg = play_video(q, episode=ep, source=sid)
        tools.append(
            {
                "func": "play_video",
                "args": {"query": q, "episode": ep, "source": sid},
                "ok": ok,
                "text": msg,
            }
        )
        if ok:
            return msg, tools
        return f"看剧没成功：{msg}", tools

    # 无剧名：走原快路径（选源 / pending / 追问）
    return try_video_fastpath(user_text, memory, force=True)


def _run_music_route(
    user_text: str,
    route: IntentRoute,
    memory,
) -> tuple[str, list[dict]] | None:
    from kekesi.tools.music_fastpath import try_music_fastpath
    from kekesi.tools.music_prefs import (
        DEFAULT_SOURCE,
        extract_song_query,
        get_music_pref,
        is_close_music,
        is_control_only,
    )
    from kekesi.tools.web_music_player import play_music

    if is_close_music(user_text) or is_control_only(user_text):
        return try_music_fastpath(user_text, memory, force=True)

    q = (route.query or extract_song_query(user_text) or "").strip()
    if q:
        sid = get_music_pref(memory) or DEFAULT_SOURCE
        ok, msg = play_music(q, source=sid, show=False)
        tools = [
            {
                "func": "play_music",
                "args": {"query": q, "source": sid},
                "ok": ok,
                "text": msg,
            }
        ]
        if ok:
            return msg, tools
        return f"听歌没成功：{msg}", tools

    return try_music_fastpath(user_text, memory, force=True)


def resolve_media_fastpath(
    text: str,
    memory,
    *,
    brain: Any | None = None,
    session_ctx: str = "",
) -> tuple[str, str, list[dict]] | None:
    """返回 (layer, reply, tools)；无法落地则 None → Agent Loop。

    分层仲裁：
      白名单 0-2（硬规则，零 LLM 成本）：
        0. 关闭视频（直连 close_video，不走 Agent Loop 避免误调 close_app）
        1. 裸「暂停/继续」（视频窗口可见时，极低延迟）
        2. 明确剧名点播（有剧名 q → 直连 play_video，低延迟）
      白名单 3（LLM 轻分类，resolve_intent_route）：
        先过成本闸 _MEDIA_CLASSIFY_HINT_RE（非媒体/检索不花 LLM），
        再过 is_local_system_query 护栏（本机状态查询绝不进快路径，
        教训：「看一下网络信息」「查IP」曾被劫持），然后：
        video / music / search 命中 → 对应快路径工具链。
      任何失败（分类 chat / 工具未落地 / 回复吐路由 JSON）一律回主 Loop。
    """
    t = (text or "").strip()
    if not t:
        return None

    # 直播点歌控制必须先于媒体快路径，否则会被当成歌名播放
    if _LIVE_SONG_CONTROL_RE.match(t):
        logger.info("直播点歌控制门禁，回主 Loop: %r", t[:40])
        return None

    # 白名单 0：关闭视频（直连 close_video，不走 Agent Loop 避免误调 close_app）
    from kekesi.tools.video_prefs import is_close_video

    if is_close_video(t):
        hit = _run_video_route(t, IntentRoute("video", via="close_video"), memory)
        if hit is not None:
            reply, tools = hit
            if looks_like_route_json(reply):
                return None
            return "video_fastpath", reply, tools

    # 白名单 1：裸暂停/继续（视频窗口可见时，极低延迟）
    pause_hit = try_video_pause_resume(t)
    if pause_hit is not None:
        reply, tools = pause_hit
        if looks_like_route_json(reply):
            return None
        # control_video 失败 → 回 Loop
        if not all(tr.get("ok") for tr in tools):
            return None
        return "video_fastpath", reply, tools

    # 白名单 2：明确剧名点播（有剧名即直连 play_video，低延迟）
    from kekesi.tools.video_prefs import (
        resolve_video_play_args,
        utterance_looks_like_video_ondemand,
    )

    if utterance_looks_like_video_ondemand(t):
        q, ep = resolve_video_play_args(t)
        if q:
            hit = _run_video_route(
                t,
                IntentRoute(
                    "video",
                    query=q,
                    episode=ep,
                    via="hard_whitelist",
                ),
                memory,
            )
            if hit is not None:
                reply, tools = hit
                if looks_like_route_json(reply):
                    return None
                # play_video 失败 → 回 Loop
                if not all(
                    tr.get("ok")
                    for tr in tools
                    if tr.get("func") == "play_video"
                ):
                    return None
                return "video_fastpath", reply, tools

    # 白名单 3：LLM 轻分类分流（2026-09 重新启用，此前为省一次 LLM 调用曾关闭）
    # 四重护栏：接续门禁（指代话语回主 Loop 用完整历史）+
    # 成本闸（疑似媒体/检索才分类）+ 本机查询护栏（本机状态查询绝不进快路径，
    # 教训：「看一下网络信息」「查IP」曾被劫持）+
    # 天气护栏（天气类有专用工具 get_weather 直答，search_web 只能搜到
    # 天气网站链接摘要，教训：「查一下北京天气」曾被劫持，回复反问用户）。
    # 护栏必须先于分类拦截：分类器误判即真劫持，不能赌 LLM 准不准。
    if looks_like_continuation(t):
        logger.info("接续门禁命中，回主 Loop: %r", t[:40])
        return None
    if (
        _MEDIA_CLASSIFY_HINT_RE.search(t)
        and not is_local_system_query(t)
        and not is_weather_query(t)
    ):
        route = resolve_intent_route(t, brain=brain, session_ctx=session_ctx)
        if route.route != "chat":
            if route.route == "video":
                hit = _run_video_route(t, route, memory)
            elif route.route == "music":
                hit = _run_music_route(t, route, memory)
            else:
                hit = _run_search_route(t, route, brain)
            if hit is not None:
                reply, tools = hit
                if looks_like_route_json(reply):
                    return None
                return _ROUTE_LAYER[route.route], reply, tools

    # 其余全部进主 Loop：chat / 本机查询 / 成本闸未命中 / 分类未落地
    return None
