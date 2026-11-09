"""看剧源站偏好与意图解析。"""

from __future__ import annotations

import re
from typing import Any

from .media_intent import is_non_video_look

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

DEFAULT_SOURCE = "tencent"

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
    r"我想看|想看|我要看|要看|帮我看|给我看|来看|"
    # 整句「看+剧名」才算；禁止句中裸「看xxx」误伤闲聊（我看可以/看起来）
    # 「看一下」单独不算：易误伤「截屏看一下 / 看一下桌面」
    r"^(?:请|帮我|给我)?看\s*[《\"“]?[\u4e00-\u9fffA-Za-z0-9·：:]{2,40}[》\"”]?$|"
    r"打开.{0,8}(?:剧|电影|动漫)|"
    r"第\s*\d+\s*集|第[一二三四五六七八九十百零〇两]+\s*集|"
    r"播放.{0,12}(?:剧|电影|视频|综艺|动漫|纪录片)|"
    r"用(?:哔哩哔哩|B站|腾讯视频|爱奇艺|源站).{0,8}(?:看|播)|"
    r"(?:默认|以后|下次).{0,6}(?:用|走)(?:哔哩|B站|腾讯|爱奇艺)|"
    r"关闭视频|关掉视频|关闭播放器|停止视频|暂停视频|"
    r"(?:推荐|有什么好看|好看的|热播|新出).{0,8}(?:剧|电影|电视剧|综艺|动漫)?|"
    r"随便(?:看|来|推荐)?.{0,6}(?:一部|一部剧|个剧|电影)?",
    re.I,
)

# 桌面/截屏观察：绝不能进看剧
_DESKTOP_OBSERVE_RE = re.compile(
    r"截屏|截图|屏幕|桌面|左下角|右下角|左上角|右上角|"
    r"文件夹|回收站|任务栏|文件名|后缀|文档",
    re.I,
)

# 接推荐后只回片名：「诺斯费拉图」
_TITLE_ONLY_RE = re.compile(
    r"^[\u4e00-\u9fffA-Za-z0-9·《》：:\s]{2,20}$"
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
    r"(?:我想看|想看|我要看|要看|帮我看|给我看|来看|看一下|帮我看一下|"
    r"播放(?=.*(?:剧|电影|视频|综艺|动漫|纪录片|第))|"
    r"打开(?=.*(?:剧|电影|动漫)))\s*(?P<q>.+)$|"
    r"^(?:请|帮我|给我)?看\s*(?P<q2>.+)$|"
    # 用腾讯视频/B站/爱奇艺/源站看《剧名》
    r"(?:用|使用)(?:哔哩哔哩|B\s*站|腾讯视频|爱奇艺|bilibili|源站).{0,6}"
    r"(?:看|播|播放)\s*(?P<q3>.+)$",
    re.I,
)

# 门已判定 video 后补抽：裸「播放/播 + 名称」（排除听歌口吻）
_LOOSE_PLAY_RE = re.compile(
    r"^(?:请|帮我|给我)?(?:播放|播一下|播)\s*[《\"“]?(?P<q>[\u4e00-\u9fffA-Za-z0-9·：:]{2,40})",
    re.I,
)
_MUSIC_MOUTH_RE = re.compile(r"(?:的)?(?:歌|歌曲|音乐|专辑|单曲)|听歌|放一首|来一首|我想听|我要听", re.I)

_STRIP_SOURCE_RE = re.compile(
    r"^(?:用|使用)?(?:哔哩哔哩|B\s*站|腾讯视频|爱奇艺|bilibili|源站)\s*"
    r"(?:给?我)?(?:看|播|播放)?\s*",
    re.I,
)

_STRIP_TRAIL_RE = re.compile(r"(?:吧|啊|呀|谢谢|可以吗|好吗)[.。!！?？]*$")
_STRIP_WATCH_RE = re.compile(r"^(?:看|播放|打开)\s*")

# 相对集数 / 指代 / 模糊推荐 → 交给 LLM（对齐听歌 should_defer）
_DEFER_TO_LLM_RE = re.compile(
    r"最新|最近一?集|更新到|更新了|刚更新|"
    r"下一集|上一集|下集|上集|大结局|完结|"
    r"刚才|刚刚|上次|上一次|那部|这部|同一部|继续看|接着看|"
    r"随便|推荐|有什么好看|好看的|热播|热门|新出|新上映|新上线|刚上|"
    r"近期|最近上|排行|榜单|口碑|"
    r"过\s*\d+\s*分钟|等会|等会儿|先.{1,12}再|然后再",
    re.I,
)

# 抽到的 query 若是「类别/模糊描述」而非剧名 → 不算明确点播
_CATEGORY_QUERY_RE = re.compile(
    r"^(?:一部|一部剧|个|点|些)?"
    r"(?:好看|热播|热门|新出|新出来|新上映|新上线|最新|最近|近期|"
    r"推荐|经典|口碑|排行|榜单)"
    r"(?:的|点|些)?"
    r"(?:电视剧|剧集|连续剧|电影|视频|动漫|综艺|纪录片|剧)?$"
)

_CATEGORY_PREFIX_RE = re.compile(
    r"^(?:好看|热播|热门|新出|新出来|新上映|新上线|最新|最近|近期|"
    r"推荐|经典|口碑|排行|榜单)"
)

_CATEGORY_SUFFIX_RE = re.compile(
    r"(?:的)?(?:电视剧|剧集|连续剧|电影|视频|动漫|综艺|纪录片)$"
)


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
    if _DESKTOP_OBSERVE_RE.search(t):
        return False
    # 排除「看一下网络信息/看天气」等非视频观察句被 _VIDEO_INTENT_RE 误判为剧名
    # 对齐 media_intent 弱路由与 tool_retriever._apply_media_policy
    if is_non_video_look(t):
        return False
    if _SOURCE_ONLY_RE.match(t):
        return True
    if _PREF_SET_RE.search(t):
        return True
    if _CLOSE_RE.match(t):
        return True
    if _VIDEO_INTENT_RE.search(t):
        return True
    return False


def is_title_only_followup(text: str) -> bool:
    """短句像片名（无问句语气），用于接推荐后点名。"""
    t = (text or "").strip()
    if not t or not _TITLE_ONLY_RE.match(t):
        return False
    if re.search(r"[吗呢吧呀啊？?！!]|天气|几点|打开|关闭|搜索|推荐", t):
        return False
    return is_clear_video_query(t)


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


def is_clear_video_query(query: str) -> bool:
    """白名单：像具体剧名，而不是「新出来的电视剧」这类描述。"""
    q = (query or "").strip()
    if len(q) < 1:
        return False
    if q in ("剧", "电影", "视频", "动漫", "综艺", "纪录片", "电视剧", "一下"):
        return False
    if _DESKTOP_OBSERVE_RE.search(q) or re.search(
        r"左下角|右下角|左上角|右上角|屏幕|这边|那边",
        q,
    ):
        return False
    bare = re.sub(r"^(?:一)?(?:部|部剧|个|点|些)\s*", "", q).strip()
    if _CATEGORY_QUERY_RE.match(q) or _CATEGORY_QUERY_RE.match(bare):
        return False
    if _CATEGORY_PREFIX_RE.match(q) or _CATEGORY_PREFIX_RE.match(bare):
        return False
    # 「新出来的电视剧 / 最近热播的剧」
    if _CATEGORY_PREFIX_RE.search(q) and _CATEGORY_SUFFIX_RE.search(q):
        return False
    if re.search(r"新出来|新上映|热播的|好看的|推荐的", q):
        return False
    return True


def should_defer_video_to_llm(text: str) -> bool:
    """看剧默认：明确「第N集」/关视频/选源走快路径；模糊/相对语义交给 LLM。"""
    t = (text or "").strip()
    if not t:
        return False
    if is_close_video(t):
        return False
    if _SOURCE_ONLY_RE.match(t) or (
        _PREF_SET_RE.search(t) and not _QUERY_RE.search(t) and not _EPISODE_RE.search(t)
    ):
        return False
    if _DEFER_TO_LLM_RE.search(t):
        return True
    q = extract_video_query(t)
    ep = extract_episode(t)
    # 明确剧名 + 数字集数 → 快路径
    if q and ep is not None and is_clear_video_query(q):
        return False
    # 明确剧名、无相对语义 → 快路径打开
    if q and ep is None and is_clear_video_query(q):
        return False
    # 抽不出明确剧名：一律 LLM
    return True


def extract_video_query(text: str) -> str:
    t = (text or "").strip()
    if not t or _CLOSE_RE.match(t) or _SOURCE_ONLY_RE.match(t):
        return ""
    if _PREF_SET_RE.search(t) and not _QUERY_RE.search(t) and not _EPISODE_RE.search(t):
        return ""
    m = _QUERY_RE.search(t)
    if not m:
        # 禁止整句闲聊当剧名（旧逻辑会把「看起来不错」剥成「起来不错」去播）
        return ""
    q = (m.group("q") or m.group("q2") or m.groupdict().get("q3") or "").strip()
    q = _STRIP_SOURCE_RE.sub("", q).strip()
    q = _STRIP_WATCH_RE.sub("", q).strip()
    # 「帮我看」先于「看一下」匹配时，q 会残留前缀「一下遮天」→ 剥掉口头词
    q = re.sub(r"^(?:一下|下|一部|一部剧|个)\s*", "", q).strip()
    # 去掉集数片段
    q = _EPISODE_RE.sub(" ", q)
    q = _STRIP_TRAIL_RE.sub("", q).strip()
    for sid, meta in SOURCES.items():
        for a in (meta["label"], *meta["aliases"]):
            q = re.sub(re.escape(str(a)), "", q, flags=re.I).strip()
    q = re.sub(r"\s+", " ", q).strip(" 的,-—")
    # 过滤明显非剧名
    if len(q) < 2 or q in ("剧", "电影", "视频", "动漫", "一下"):
        return ""
    # 整词口头语才拒；勿用 ^一下 误杀「一下遮天」剥净前的残留（已在上方剥离）
    if re.search(
        r"^(?:可以|行|好|这个|那个|问题|文件|怎么办|着办|起来|"
        r"怎样|怎么样|什么|哪里|哪儿)$",
        q,
    ):
        return ""
    return q[:80]


def _clean_title_candidate(q: str) -> str:
    q = (q or "").strip()
    q = _EPISODE_RE.sub(" ", q)
    q = _STRIP_TRAIL_RE.sub("", q).strip()
    for sid, meta in SOURCES.items():
        for a in (meta["label"], *meta["aliases"]):
            q = re.sub(re.escape(str(a)), "", q, flags=re.I).strip()
    q = re.sub(r"\s+", " ", q).strip(" 的,-—《》\"“”")
    if len(q) < 2 or q in ("剧", "电影", "视频", "动漫", "一下"):
        return ""
    if not is_clear_video_query(q):
        return ""
    return q[:80]


def extract_video_query_loose(text: str) -> str:
    """门已开 video 后补抽剧名（覆盖裸「播放XX」），不用于改分类门。"""
    t = (text or "").strip()
    if not t or _MUSIC_MOUTH_RE.search(t):
        return ""
    q = extract_video_query(t)
    if q:
        return q
    m = _LOOSE_PLAY_RE.match(t)
    if m:
        return _clean_title_candidate(m.group("q") or "")
    return ""


def resolve_video_play_args(
    text: str,
    *,
    route_query: str = "",
    route_episode: int | None = None,
) -> tuple[str, int | None]:
    """与直连 play_video 对齐：解析剧名+集数（供语音快路径复用）。"""
    ep = route_episode if route_episode is not None else extract_episode(text)
    q = (route_query or "").strip() or extract_video_query(text) or extract_video_query_loose(text)
    if q and is_clear_video_query(q):
        return q[:80], ep
    return "", ep


def utterance_looks_like_video_ondemand(text: str) -> bool:
    """看剧点播话术（含裸播放剧名）；听歌口吻除外。用于语音漏判时兜底走同一 play_video。"""
    t = (text or "").strip()
    if not t or _MUSIC_MOUTH_RE.search(t) or _DESKTOP_OBSERVE_RE.search(t):
        return False
    if is_video_intent(t):
        return True
    if _LOOSE_PLAY_RE.match(t) and extract_video_query_loose(t):
        return True
    return False


def choice_prompt() -> str:
    names = "、".join(source_label(s) for s in ("tencent", "bilibili", "iqiyi"))
    return (
        f"看剧我会在科科斯视频空间用网页版打开并弹窗播放。请选一个默认源站："
        f"{names}。直接说「腾讯视频」或「以后用B站」；选好后说「看剧名第N集」。"
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
