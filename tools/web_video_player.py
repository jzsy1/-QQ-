"""体内网页看剧客户端（腾讯视频 / B站 / 爱奇艺）— 走独立 video_space。"""

from __future__ import annotations

import logging
import re
import time

from kekesi.tools.video_prefs import (
    DEFAULT_SOURCE,
    normalize_source,
    source_label,
)
from kekesi.video_space.client import (
    _request,
    _state_path,
    close_space,
    ensure_space,
)

logger = logging.getLogger(__name__)

_WEBVIEW_DEAD_RE = re.compile(
    r"CoreWebView2|browser process crashed|no longer valid|WebView2",
    re.I,
)


def _clear_stale_state() -> None:
    try:
        _state_path().unlink(missing_ok=True)
    except OSError:
        pass


def is_webview_dead_message(msg: str) -> bool:
    return bool(_WEBVIEW_DEAD_RE.search(msg or ""))


def restart_space() -> tuple[bool, str, int | None]:
    try:
        close_space()
    except Exception:
        pass
    time.sleep(0.8)
    _clear_stale_state()
    return ensure_space(start=True)


def _play_once(
    query: str,
    *,
    episode: int | None,
    source: str,
    port: int,
    latest: bool = False,
) -> tuple[bool, str, dict]:
    payload: dict = {
        "query": query,
        "source": source,
        "show": True,
        "latest": bool(latest),
    }
    if episode is not None:
        payload["episode"] = int(episode)
    r = _request(
        "POST",
        "/play_video",
        payload,
        port=port,
        timeout=120.0,
    )
    text = str(r.get("message") or "打开视频失败")
    return bool(r.get("ok")), text, r


def play_video(
    query: str,
    *,
    episode: int | None = None,
    source: str | None = None,
    latest: bool = False,
) -> tuple[bool, str]:
    q = (query or "").strip()
    if not q:
        return False, "请告诉我要看什么剧"
    src = normalize_source(source) or DEFAULT_SOURCE
    label = source_label(src)

    ok, msg, port = ensure_space(start=True)
    if not ok or port is None:
        ok, msg, port = restart_space()
        if not ok or port is None:
            return False, msg

    ok, text, _ = _play_once(
        q, episode=episode, source=src, port=port, latest=latest
    )
    if ok:
        if label not in text:
            return True, f"[{label}] {text}"
        return True, text

    if is_webview_dead_message(text) or "连接视频空间失败" in text:
        logger.warning("视频空间异常，重启后重试看剧: %s", text[:120])
        ok2, msg2, port2 = restart_space()
        if not ok2 or port2 is None:
            return False, f"{text}；重启失败：{msg2}"
        ok3, text3, _ = _play_once(
            q, episode=episode, source=src, port=port2, latest=latest
        )
        if ok3:
            return True, text3 if label in text3 else f"[{label}] {text3}"
        return False, text3

    return False, text


def video_login(source: str | None = None) -> tuple[bool, str]:
    src = normalize_source(source) or DEFAULT_SOURCE
    ok, msg, port = ensure_space(start=True)
    if not ok or port is None:
        ok, msg, port = restart_space()
        if not ok or port is None:
            return False, msg
    r = _request(
        "POST",
        "/video/login",
        {"source": src},
        port=port,
        timeout=45.0,
    )
    return bool(r.get("ok")), str(r.get("message") or msg)


def control_video(action: str) -> tuple[bool, str]:
    act = (action or "").strip().lower()
    if act not in ("pause", "play", "resume", "stop"):
        return False, f"不支持的视频控制: {action}"
    if act == "resume":
        act = "play"
    ok, msg, port = ensure_space(start=False)
    if not ok or port is None:
        return False, "视频空间未运行"
    r = _request(
        "POST",
        "/control",
        {"action": act},
        port=port,
        timeout=12.0,
    )
    return bool(r.get("ok")), str(r.get("message") or msg)


def close_video() -> tuple[bool, str]:
    ok, msg, port = ensure_space(start=False)
    if ok and port is not None:
        try:
            _request(
                "POST",
                "/control",
                {"action": "pause"},
                port=port,
                timeout=10.0,
            )
        except Exception:
            pass
    return close_space()


def video_status() -> tuple[bool, str]:
    ok, msg, port = ensure_space(start=False)
    if not ok or port is None:
        return True, "视频空间未运行"
    r = _request("GET", "/status", port=port, timeout=12.0)
    if not r.get("ok"):
        return False, str(r.get("message") or "状态查询失败")
    guest = (r.get("guest") or "").strip()
    label = source_label(normalize_source(guest) or guest) if guest else "未知"
    title = str((r.get("video") or {}).get("title") or r.get("lastQuery") or "").strip()
    href = str(r.get("guestUrl") or "")[:80]
    visible = r.get("visible")
    logged = "已登录" if r.get("loggedIn") else "未登录"
    extra = f"，当前：{title}" if title else ""
    vis = "窗口已显示" if visible else "窗口未显示"
    return True, f"视频空间运行中（{label}，{logged}，{vis}）{extra} {href}".strip()
