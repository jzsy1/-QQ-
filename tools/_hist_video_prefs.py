"""看剧源站偏好与意图解析。"""

from __future__ import annotations

import re
from typing import Any

PREF_KEY = "video_player"
PENDING_QUERY_KEY = "video_pending_query"
PENDING_EPISODE_KEY = "video_pending_episode"

SOURCES: dict[str, dict[str, Any]] = {
    "bilibili": {
        "label": "哔哩哔哩",
        "aliases": ("b站", "b 站", "哔哩", "哔哩哔哩", "bilibili", "bilibli"),
    },
    "tencent": {
        "label": "腾讯视频",
        "aliases": ("腾讯视频", "腾讯", "tx视频", "v.qq", "tencent"),
    },
    "iqiyi": {
        "label": "爱奇艺",
        "aliases": ("爱奇艺", "奇艺", "iqiyi", "iqy"),
    },
}

DEFAULT_SOURCE = "bilibili"

_CN_NUM = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}

_VIDEO_INTENT_RE = re.compile(
    r"看剧|追剧|看电影|看视频|看动漫|看综艺|看纪录片|"
    r"我想看|想看|帮我看|打开.{0,8}(?:剧|电影|动漫)|"
    r"第\s*\d+\s*集|第[一二三四五六七八九十百零〇两]+\s*集|"
    r"播放.{0,12}(?:剧|电影|视频|综艺|动漫|纪录片)|"
    r"用(?:哔哩哔哩|B站|腾讯视频|爱奇艺).{0,8}(?:看|播)|"
    r"(?:默认|以后|下次).{0,6}(?:用|走)(?:哔哩|B站|腾讯|爱奇艺)|"
    r"关闭视频|关掉视频|关闭播放器|停止视频",
    re.I,
)

_CLOSE_RE = re.compile(
    r"^(?:请|帮我|给我)?(?:关闭视频|关掉视频|关闭播放器|停止视频|"
    r"关掉(?:这个)?(?:视频|播放器)|不看了)$"
)

_PREF_SET_RE = re.compile(
    r"(?:默认|以后|下次|之后)?(?:用|使用|走|切换到|改成|设为|设置成)"
    r".{0,4}(?P<src>哔哩哔哩|B\s*站|腾讯视频|爱奇艺)",
    re.I,
)

_SOURCE_ONLY_RE = re.compile(
    r"^(?:请|就|用|使用)?(?P<src>哔哩哔哩|B\s*站|腾讯视频|爱奇艺|bilibili|腾讯|爱奇艺)"
    r"(?:吧|啊|呀|谢谢)?$",
    re.I,
)

_EPISODE_RE = re.compile(
    r"(?:第\s*(?P<n>\d+)\s*集)|(?:第\s*(?P<cn>[一二三四五六七八九十百零〇两]+)\s*集)|"
    r"(?:(?:EP|ep|E)\s*(?P<e>\d+))|(?:(?P<n2>\d+)\s*集)",
)

_QUERY_RE = re.compile(
    r"(?:我想看|想看|帮我看|给我看|来看|播放|看一下|打开)\s*(?P<q>.+)$",
    re.I,
)

_STRIP_SOURCE_RE = re.compile(
    r"^(?:用|使用)?(?:哔哩哔哩|B\s*站|腾讯视频|爱奇艺|bilibili)\s*"
    r"(?:给?我)?(?:看|播|播放)?\s*",
    re.I,
)

_STRIP_TRAIL_RE = re.compile(r"(?:吧|啊|呀|谢谢|可以吗|好吗)[.。!！?？]*$")
_STRIP_WATCH_RE = re.compile(r"^(?:看|播放|打开)\s*")


def _cn_to_int(s: str) -> int | None:
    s = (s or "").strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if s == "十":
        return 10
    if "十" in s:
        parts = s.split("十")
        tens = _CN_NUM.get(parts[0], 1) if parts[0] else 1
        ones = _CN_NUM.get(parts[1], 0) if len(parts) > 1 and parts[1] else 0
        return tens * 10 + ones
    if len(s) == 1 and s in _CN_NUM:
        return _CN_NUM[s]
    return None


def normalize_source(raw: str | None) -> str | None:
    t = (raw or "").strip().lower().replace(" ", "")
    if not t:
        return None
    # 常见简写
    if t in ("b站", "b", "bl", "bili"):
        return "bilibili"
    if t in ("腾讯", "tx"):
        return "tencent"
    for sid, meta in SOURCES.items():
        if t == sid:
            return sid
        label = str(meta["label"]).lower().replace(" ", "")
        if t == label or label in t or t in label:
            return sid
        for a in meta["aliases"]:
            al = str(a).lower().replace(" ", "")
            if t == al or al in t or t in al:
                return sid
    return None


def source_label(source: str) -> str:
    meta = SOURCES.get(source) or {}
    return str(meta.get("label") or source)


def parse_source_from_text(text: str) -> str | None:
    t = (text or "").strip()
    if not t:
        return None
    m = _PREF_SET_RE.search(t)
    if m:
        return normalize_source(m.group("src"))
    m = _SOURCE_ONLY_RE.match(t)
    if m:
        return normalize_source(m.group("src"))
    for sid, meta in SOURCES.items():
        for a in (meta["label"], *meta["aliases"]):
            if str(a) in t:
                return sid
    return None


def is_video_intent(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if _SOURCE_ONLY_RE.match(t):
        return True
    if _PREF_SET_RE.search(t):
        return True
    if _CLOSE_RE.match(t):
        return True
    return bool(_VIDEO_INTENT_RE.search(t))


def is_close_video(text: str) -> bool:
    return bool(_CLOSE_RE.match((text or "").strip()))


def extract_episode(text: str) -> int | None:
    t = (text or "").strip()
    if not t:
        return None
    m = _EPISODE_RE.search(t)
    if not m:
        return None
    if m.group("n"):
        return int(m.group("n"))
    if m.group("n2"):
        return int(m.group("n2"))
    if m.group("e"):
        return int(m.group("e"))
    if m.group("cn"):
        return _cn_to_int(m.group("cn"))
    return None


def extract_video_query(text: str) -> str:
    t = (text or "").strip()
    if not t or _CLOSE_RE.match(t) or _SOURCE_ONLY_RE.match(t):
        return ""
    if _PREF_SET_RE.search(t) and not _QUERY_RE.search(t) and not _EPISODE_RE.search(t):
        return ""
    m = _QUERY_RE.search(t)
    q = (m.group("q") if m else t).strip()
    q = _STRIP_SOURCE_RE.sub("", q).strip()
    q = _STRIP_WATCH_RE.sub("", q).strip()
    # 去掉集数片段
    q = _EPISODE_RE.sub(" ", q)
    q = _STRIP_TRAIL_RE.sub("", q).strip()
    for sid, meta in SOURCES.items():
        for a in (meta["label"], *meta["aliases"]):
            q = re.sub(re.escape(str(a)), "", q, flags=re.I).strip()
    q = re.sub(r"\s+", " ", q).strip(" 的,-—")
    if len(q) < 1 or q in ("剧", "电影", "视频", "动漫", "一下"):
        return ""
    return q[:80]


def choice_prompt() -> str:
    names = "、".join(source_label(s) for s in ("bilibili", "tencent", "iqiyi"))
    return (
        f"看剧我会在科科斯体内用网页版打开并弹窗播放。请选一个默认源站："
        f"{names}。直接说「B站」或「以后用腾讯视频」；选好后说「看剧名第N集」。"
    )


def get_video_pref(memory) -> str | None:
    if memory is None:
        return None
    try:
        raw = memory.get_pref(PREF_KEY)
    except Exception:
        return None
    return normalize_source(raw)


def set_video_pref(memory, source: str) -> str:
    sid = normalize_source(source) or DEFAULT_SOURCE
    if memory is not None:
        try:
            memory.set_pref(PREF_KEY, sid)
        except Exception:
            pass
    return sid


def get_pending_query(memory) -> str:
    if memory is None:
        return ""
    try:
        return (memory.get_pref(PENDING_QUERY_KEY) or "").strip()
    except Exception:
        return ""


def get_pending_episode(memory) -> int | None:
    if memory is None:
        return None
    try:
        raw = (memory.get_pref(PENDING_EPISODE_KEY) or "").strip()
        return int(raw) if raw.isdigit() else None
    except Exception:
        return None


def set_pending(memory, query: str, episode: int | None = None) -> None:
    if memory is None:
        return
    try:
        memory.set_pref(PENDING_QUERY_KEY, (query or "")[:80])
        memory.set_pref(
            PENDING_EPISODE_KEY, str(int(episode)) if episode else ""
        )
    except Exception:
        pass
