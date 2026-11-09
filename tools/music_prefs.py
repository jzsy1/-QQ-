"""听歌播放器偏好与意图解析。"""

from __future__ import annotations

import re
from typing import Any

PREF_KEY = "music_player"
PENDING_QUERY_KEY = "music_pending_query"

# 体内网页访客 id → 展示名
SOURCES: dict[str, dict[str, Any]] = {
    "qq": {
        "label": "QQ音乐",
        "aliases": ("qq音乐", "qq 音乐", "qqmusic", "腾讯音乐", "q音"),
    },
    "netease": {
        "label": "网易云音乐",
        "aliases": ("网易云", "网易云音乐", "网易", "cloudmusic", "netease"),
    },
    "kugou": {
        "label": "酷狗音乐",
        "aliases": ("酷狗", "酷狗音乐", "kugou"),
    },
}

DEFAULT_SOURCE = "qq"

_MUSIC_INTENT_RE = re.compile(
    r"听歌|放歌|点歌|来一首|放一?首|唱的|"
    r"我想听|我要听|想听|要听|帮我放|帮我听|"
    r"播放(?!.*(?:剧|电影|视频|综艺|动漫|纪录片|第\s*\d+\s*集|第.+集))|"
    r"播放.{0,12}(?:歌|曲|音乐|专辑)|听.{0,12}(?:歌|音乐|专辑|电台)|"
    # 注意：勿写成裸「放」，否则「播放xxx」会被误吃
    r"(?:请|帮我|给我)?(?:听|(?<![播])放)\s*[\u4e00-\u9fffA-Za-z0-9·《》]{1,40}|"
    r"(?:放|听|播)(?:一下)?(?:刚才|刚刚|那首|这首)|"
    r"暂停音乐|继续播放|下一首|上一首|换一首|"
    r"关闭音乐|关掉音乐|关闭播放|停止音乐|"
    r"用(?:QQ音乐|网易云|酷狗).{0,8}(?:听|放|播)|"
    r"(?:默认|以后|下次).{0,6}(?:用|走)(?:QQ|网易云|酷狗)",
    re.I,
)

_CONTROL_RE = re.compile(
    r"^(?:请|帮我|给我)?(?:暂停(?:一下|音乐|播放)?|继续播放|继续放|恢复播放|"
    r"下一首|切歌|换一首|上一首|停止播放|停一下|"
    r"关闭音乐|关掉音乐|关闭播放|停止音乐)$"
)

_PREF_SET_RE = re.compile(
    r"(?:默认|以后|下次|之后)?(?:用|使用|走|切换到|改成|设为|设置成)"
    r".{0,4}(?P<src>QQ音乐|QQ\s*音乐|网易云(?:音乐)?|酷狗(?:音乐)?)",
    re.I,
)

_SOURCE_ONLY_RE = re.compile(
    r"^(?:请|就|用|使用)?(?P<src>QQ音乐|QQ\s*音乐|网易云(?:音乐)?|酷狗(?:音乐)?|qq|网易云|酷狗)"
    r"(?:吧|啊|呀|谢谢)?$",
    re.I,
)

_QUERY_RE = re.compile(
    r"(?:我想听|我要听|想听|要听|帮我放|帮我听|来一首|放一首|放一?首|播放|听一下|点播|点歌|听)\s*"
    r"(?P<q>.+)$",
    re.I,
)

_STRIP_SOURCE_RE = re.compile(
    r"^(?:用|使用)?(?:QQ音乐|QQ\s*音乐|网易云(?:音乐)?|酷狗(?:音乐)?|qqmusic)\s*"
    r"(?:给?我)?(?:听|放|播|播放)?\s*",
    re.I,
)

_STRIP_TRAIL_RE = re.compile(r"(?:吧|啊|呀|谢谢|可以吗|好吗)[.。!！?？]*$")

# 整句含这些 → 即使抽到字也更像难意图，交给 LLM
_COMPOUND_OR_REF_RE = re.compile(
    r"刚才|刚刚|上次|上一次|那首|这首|同一首|刚听|"
    r"随便|推荐|来点|放点|"
    r"原唱|翻唱|合唱|现场版|live|不[是要]|换个版本|另一个版本|"
    r"类似|风格|那种|氛围|"
    r"过\s*\d+\s*分钟|等会|等会儿|先.{1,12}再|然后再",
    re.I,
)

# 「我喜欢」歌单：必须有列表/收藏语境，避免和同名歌曲冲突
_LIKED_PLAYLIST_RE = re.compile(
    r"(?:"
    r"我喜欢(?:的)?(?:歌单|列表)+|"  # 我喜欢列表 / 我喜欢歌单 / 我喜欢列表歌单
    r"喜欢(?:歌单|列表)+|"
    r"我喜欢的(?:歌|音乐|歌曲|曲子)|"  # 「喜欢的歌」=收藏集合，不是歌名
    r"我的喜欢|"
    r"红心(?:歌单|列表|歌|音乐)?|"
    r"收藏的(?:歌|音乐|列表|歌曲)|我的收藏|"
    r"我的音乐.{0,12}(?:我喜欢|喜欢)"
    r")",
    re.I,
)

# 明确点同名歌曲（覆盖歌单误判）
_LIKED_AS_SONG_RE = re.compile(
    r"(?:"
    r"[《「\"“]我喜欢[》」\"”]|"
    r"(?:歌名|这首歌|那首歌|单曲).{0,4}我喜欢|"
    r"我喜欢.{0,4}(?:这首歌|那首歌|这首|单曲)"
    r")",
    re.I,
)

# 抽到的 query 若是「类别/氛围」而非歌名 → 不算白名单
_CATEGORY_QUERY_RE = re.compile(
    r"^(?:一首|点|些)?"
    r"(?:好听|安静|伤感|开心|轻松|睡前|抒情|摇滚|流行|热门|经典|"
    r"新歌|新出|最新|流行榜|排行榜|榜单)"
    r"(?:的|点|些)?(?:歌|曲子|音乐|歌曲)?$"
)

_CATEGORY_PREFIX_RE = re.compile(
    r"^(?:好听|安静|伤感|开心|轻松|睡前|抒情|摇滚|流行|热门|经典|"
    r"新歌|新出|最新|流行榜|排行|榜单)"
)


def normalize_source(raw: str | None) -> str | None:
    t = (raw or "").strip().lower().replace(" ", "")
    if not t:
        return None
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
    # 句中点名
    for sid, meta in SOURCES.items():
        for a in (meta["label"], *meta["aliases"]):
            if str(a) in t:
                return sid
    return None


def is_music_intent(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if _SOURCE_ONLY_RE.match(t):
        return True
    if _PREF_SET_RE.search(t):
        return True
    if _CONTROL_RE.match(t):
        return True
    return bool(_MUSIC_INTENT_RE.search(t))


def is_control_only(text: str) -> bool:
    return bool(_CONTROL_RE.match((text or "").strip()))


def parse_control_action(text: str) -> str | None:
    t = (text or "").strip()
    if re.search(r"关闭音乐|关掉音乐|关闭播放|停止音乐|暂停|停止|停一下", t):
        return "pause"
    if re.search(r"继续|恢复", t):
        return "play"
    if re.search(r"下一首|切歌|换一首", t):
        return "next"
    if re.search(r"上一首", t):
        return "previous"
    return None


def is_close_music(text: str) -> bool:
    t = (text or "").strip()
    return bool(re.fullmatch(
        r"(?:请|帮我|给我)?(?:关闭音乐|关掉音乐|关闭播放|停止音乐)",
        t,
    ))


def extract_song_query(text: str) -> str:
    t = (text or "").strip()
    if not t or _CONTROL_RE.match(t) or _SOURCE_ONLY_RE.match(t):
        return ""
    # 去掉偏好设置整句
    if _PREF_SET_RE.search(t) and not _QUERY_RE.search(t):
        return ""
    m = _QUERY_RE.search(t)
    q = (m.group("q") if m else t).strip()
    q = _STRIP_SOURCE_RE.sub("", q).strip()
    q = _STRIP_TRAIL_RE.sub("", q).strip()
    # 去掉残留平台名
    for sid, meta in SOURCES.items():
        for a in (meta["label"], *meta["aliases"]):
            q = re.sub(re.escape(str(a)), "", q, flags=re.I).strip()
    q = re.sub(r"^(?:的|一首|这歌|这首歌)\s*", "", q).strip()
    if len(q) < 1 or q in ("歌", "音乐", "一下"):
        return ""
    return q[:80]


def is_liked_playlist_intent(text: str) -> bool:
    """是否要播「我喜欢 / 收藏」列表。

    仅强信号命中（列表/歌单/收藏/我的音乐…），避免把同名歌曲「我喜欢」写死成歌单。
    """
    t = (text or "").strip()
    if not t:
        return False
    if _LIKED_AS_SONG_RE.search(t):
        return False
    if re.search(r"记住|记得|喜欢.{0,6}(?:主题|颜色|风格|咖啡|安静|亮色|暗色)", t):
        return False
    if not _LIKED_PLAYLIST_RE.search(t):
        return False
    return is_music_intent(t) or bool(
        re.search(r"(?:播放|听|放|播|打开|来)", t)
    )


def is_liked_playlist_query(query: str) -> bool:
    """工具 query 是否明确是歌单别名（裸「我喜欢」不算，留给搜歌）。"""
    q = (query or "").strip()
    if not q:
        return False
    if _LIKED_AS_SONG_RE.search(q):
        return False
    return bool(
        re.fullmatch(
            r"(?:播放|听|放)?(?:一下)?(?:"
            r"我喜欢(?:的)?(?:歌单|列表)+|"
            r"喜欢(?:歌单|列表)+|"
            r"我喜欢的(?:歌|音乐|歌曲|曲子)|我的喜欢|"
            r"红心(?:歌单|列表|歌|音乐)?|"
            r"收藏的(?:歌|音乐|列表|歌曲)|我的收藏"
            r")",
            q,
        )
    )


def is_clear_song_query(query: str) -> bool:
    """白名单：像具体歌名/歌手点名，而不是氛围或榜单描述。"""
    q = (query or "").strip()
    if len(q) < 1:
        return False
    if q in ("歌", "歌曲", "音乐", "曲子", "一下", "的歌"):
        return False
    # 明确歌单别名才排除；裸「我喜欢」当作歌名可走快路径搜歌
    if is_liked_playlist_query(q):
        return False
    # 去掉「一些/一点/一首」外壳再判
    bare = re.sub(r"^(?:一)?(?:些|点|首)\s*", "", q).strip()
    if _CATEGORY_QUERY_RE.match(q) or _CATEGORY_QUERY_RE.match(bare):
        return False
    if _CATEGORY_PREFIX_RE.match(q) or _CATEGORY_PREFIX_RE.match(bare):
        return False
    # 「热门的歌 / 新出的歌曲」
    m = re.match(r"^(.+?)的(?:歌|歌曲|曲子|音乐)$", bare)
    if m and _CATEGORY_PREFIX_RE.match(m.group(1).strip()):
        return False
    return True


def should_defer_music_to_llm(text: str) -> bool:
    """听歌默认交给 LLM；仅关闭/控制/选平台/明确歌名/明确「我喜欢」歌单走快路径。"""
    t = (text or "").strip()
    if not t:
        return False
    # 快路径白名单
    if is_close_music(t) or is_control_only(t):
        return False
    if is_liked_playlist_intent(t):
        return False
    if _SOURCE_ONLY_RE.match(t) or (
        _PREF_SET_RE.search(t) and not _QUERY_RE.search(t)
    ):
        return False
    # 指代、复合、改版本等 → LLM
    if _COMPOUND_OR_REF_RE.search(t):
        return True
    q = extract_song_query(t)
    if q and is_clear_song_query(q):
        return False  # 明确歌名/歌手 → 快路径（含「我想听我喜欢」）
    # 抽不出明确歌名：一律 LLM（不再靠黑名单补词）
    return True


def choice_prompt() -> str:
    names = "、".join(source_label(s) for s in ("qq", "netease", "kugou"))
    return (
        f"听歌我会在科科斯体内用网页版播放（不占桌面）。请选一个默认播放器："
        f"{names}。直接说「网易云」或「以后用酷狗」就行；选好后说「我想听歌名」。"
    )


def get_music_pref(memory) -> str | None:
    if memory is None:
        return None
    try:
        raw = memory.get_pref(PREF_KEY)
    except Exception:
        return None
    return normalize_source(raw)


def set_music_pref(memory, source: str) -> str:
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


def set_pending_query(memory, query: str) -> None:
    if memory is None:
        return
    try:
        if query:
            memory.set_pref(PENDING_QUERY_KEY, query[:80])
        else:
            memory.set_pref(PENDING_QUERY_KEY, "")
    except Exception:
        pass
