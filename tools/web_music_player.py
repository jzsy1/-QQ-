"""统一体内网页点歌客户端（QQ / 网易云 / 酷狗）。"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path

from kekesi.inner_space.client import (
    _request,
    _state_path,
    close_space,
    ensure_space,
)
from kekesi.tools.music_prefs import (
    DEFAULT_SOURCE,
    normalize_source,
    source_label,
)

logger = logging.getLogger(__name__)

_WEBVIEW_DEAD_RE = re.compile(
    r"CoreWebView2|browser process crashed|no longer valid|WebView2",
    re.I,
)


def _legacy_state_path() -> Path:
    return Path(__file__).resolve().parent.parent / "data" / "qqmusic" / "service.json"


def _clear_stale_state() -> None:
    for p in (_state_path(), _legacy_state_path()):
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass


def is_webview_dead_message(msg: str) -> bool:
    return bool(_WEBVIEW_DEAD_RE.search(msg or ""))


def restart_space() -> tuple[bool, str, int | None]:
    """强制关掉体内空间并重新拉起（用于 WebView 崩溃自愈）。"""
    try:
        close_space()
    except Exception:
        pass
    time.sleep(0.8)
    _clear_stale_state()
    return ensure_space(start=True)


def ensure_service(*, start: bool = True) -> tuple[bool, str, int | None]:
    return ensure_space(start=start)


def _play_once(
    query: str,
    *,
    source: str,
    show: bool,
    port: int,
    playlist: str | None = None,
) -> tuple[bool, str]:
    body: dict = {"query": query, "show": bool(show), "source": source}
    if playlist:
        body["playlist"] = playlist
    r = _request(
        "POST",
        "/play",
        body,
        port=port,
        timeout=150.0,
    )
    text = str(r.get("message") or "播放失败")
    if not r.get("ok") and r.get("songs"):
        text += f"（候选曲目 {r.get('songs')}）"
    return bool(r.get("ok")), text


def play_music(
    query: str,
    *,
    source: str | None = None,
    show: bool = False,
    playlist: str | None = None,
) -> tuple[bool, str]:
    from kekesi.tools.music_prefs import is_liked_playlist_query

    q = (query or "").strip()
    pl = (playlist or "").strip().lower() or None
    if pl in ("like", "likes", "favorite", "favourites", "favorites"):
        pl = "liked"
    # 仅强信号 query 才映射到歌单；裸「我喜欢」继续当歌名搜索
    if not pl and is_liked_playlist_query(q):
        pl = "liked"
        q = ""
    if pl != "liked" and not q:
        return False, "请告诉我要听什么歌"
    src = normalize_source(source) or DEFAULT_SOURCE
    label = source_label(src)

    ok, msg, port = ensure_space(start=True)
    if not ok or port is None:
        ok, msg, port = restart_space()
        if not ok or port is None:
            return False, msg

    ok, text = _play_once(q, source=src, show=show, port=port, playlist=pl)
    if ok:
        if label not in text:
            return True, f"[{label}] {text}"
        return True, text

    # 旧体内空间宿主不认 playlist=liked，会回「请提供歌名」→ 强制重启后再试
    stale_host = pl == "liked" and (
        "请提供歌名" in text or "请告诉我要听什么歌" in text
    )
    # WebView 崩 / 连接失败 / 旧宿主：重启后重试一次
    if (
        stale_host
        or is_webview_dead_message(text)
        or "连接体内空间失败" in text
    ):
        logger.warning(
            "体内空间异常或宿主过旧，尝试重启后重播: %s", text[:120]
        )
        ok2, msg2, port2 = restart_space()
        if not ok2 or port2 is None:
            return False, f"{text}；重启体内空间失败：{msg2}"
        time.sleep(1.2)
        ok3, text3 = _play_once(q, source=src, show=show, port=port2, playlist=pl)
        if ok3:
            return True, text3 if label in text3 else f"[{label}] {text3}"
        return False, text3

    return False, text


def music_login(source: str | None = None) -> tuple[bool, str]:
    src = normalize_source(source) or DEFAULT_SOURCE
    ok, msg, port = ensure_space(start=True)
    if not ok or port is None:
        ok, msg, port = restart_space()
        if not ok or port is None:
            return False, msg
    r = _request(
        "POST",
        "/login",
        {"source": src},
        port=port,
        timeout=45.0,
    )
    return bool(r.get("ok")), str(r.get("message") or msg)


def control_web_music(action: str) -> tuple[bool, str]:
    action = (action or "").strip().lower()
    ok, msg, port = ensure_space(start=False)
    if not ok or port is None:
        return False, "体内空间未在运行，请先点歌"
    r = _request("POST", "/control", {"action": action}, port=port, timeout=20.0)
    return bool(r.get("ok")), str(r.get("message") or "控制失败")


def music_status() -> tuple[bool, str]:
    ok, msg, port = ensure_space(start=False)
    if not ok or port is None:
        return True, "科科斯体内空间未运行（网页音乐访客未加载）"
    r = _request("GET", "/status", port=port, timeout=10.0)
    if not r.get("ok"):
        return False, str(r.get("message") or "状态查询失败")
    logged = "已登录" if r.get("loggedIn") else "未登录"
    guest = (r.get("guest") or "qq").strip()
    label = source_label(normalize_source(guest) or guest)
    now = r.get("now") if isinstance(r.get("now"), dict) else {}
    name = (now.get("name") or "").strip()
    artist = (now.get("artist") or "").strip()
    if name:
        track = f"{name}" + (f" - {artist}" if artist else "")
        return True, f"体内空间运行中（{label}，{logged}），当前：{track}"
    last = (r.get("lastQuery") or "").strip()
    extra = f"，上次点歌：{last}" if last else ""
    return True, f"体内空间运行中（{label}，{logged}）{extra}"


def music_show(*, minimize: bool = False) -> tuple[bool, str]:
    ok, msg, port = ensure_space(start=True)
    if not ok or port is None:
        return False, msg
    r = _request("POST", "/show", {"minimize": bool(minimize)}, port=port, timeout=15.0)
    return bool(r.get("ok")), str(r.get("message") or msg)


# 兼容旧导入名
qqmusic_login = music_login
control_qqmusic = control_web_music
qqmusic_status = music_status
qqmusic_show = music_show
close_qqmusic = close_space
