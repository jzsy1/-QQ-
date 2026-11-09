# -*- coding: utf-8 -*-
"""QQ 点歌与音乐分享服务。

能力：
1. 智能意图识别与口语化歌名精准清洗（去除“一首”、“这首歌”、“叫xx的歌”等口语噪声）。
2. 精准评分与过滤算法：自动剔除电台节目、有声小说、串烧DJ（非点播原曲），优先命中正版原唱。
3. QQ 音乐首选 + 网易云备用双通道。
4. 原生 OneBot 卡片极简分享：成功时只发一张卡片，绝不发送冗余文本链接。
"""

from __future__ import annotations

import json
import logging
import re
import ssl
import urllib.parse
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

# 点歌触发正则
_MUSIC_PATTERNS = [
    re.compile(r"^(?:点歌|我要听|我想听|要听|想听|来首|来一首|来个|来曲|放首|放一首|放个|放歌曲|放歌|放|播放歌曲|播放|播|听歌|听听|听下|听)\s*(.*)$", re.IGNORECASE),
    re.compile(r".*?(?:我要听|我想听|帮我放首|帮我点首|帮我放|帮我点|给我放|给我点|来首|来一首|放首|放一首|放个|播放歌曲|播放)\s*(.+)$", re.IGNORECASE),
    re.compile(r"^《([^》]+)》$"),  # 直接发《歌名》
]

# 常见口语前缀清洗正则
_PREFIX_CLEAN_RE = re.compile(
    r"^(?:一首|这首|那首|个|一个|首|曲|一曲|下|一下|帮我放首|帮我点首|帮我放|帮我点|给我放|给我点|帮我|请帮我|给我|想听|要听|播放歌曲|播放|放首|放一首|放个|放|来首|来一首|来个|来曲|点首|听听|听下|听|歌名叫|歌名是|叫)\s*",
    re.IGNORECASE,
)

# 常见口语后缀清洗正则
_SUFFIX_CLEAN_RE = re.compile(
    r"(?:这首歌|那首歌|这首|那首|的歌|歌曲|音乐|歌)$",
    re.IGNORECASE,
)

# 标点与语气助词
_PUNCT_CLEAN_RE = re.compile(r"[!！?？。，,~～呀啊吧啦呢歌]+\s*$")


def clean_song_query(query: str) -> str:
    """精准清洗用户输入的口语化歌名，剥离干扰词。"""
    s = (query or "").strip()
    if not s:
        return ""
    # 去除书名号、括号、各类引号
    s = re.sub(r"[《》\"'“”‘’（）\(\)\[\]【】]", "", s)
    # 剥离前缀口语
    s = _PREFIX_CLEAN_RE.sub("", s).strip()
    # 剥离后缀口语
    s = _SUFFIX_CLEAN_RE.sub("", s).strip()
    # 剥离尾部标点与语气词
    s = _PUNCT_CLEAN_RE.sub("", s).strip()
    # 二次清理残留的“一首”、“下”
    s = _PREFIX_CLEAN_RE.sub("", s).strip()
    return s


def is_music_intent(text: str) -> tuple[bool, str]:
    """判断是否为点歌意图。
    
    返回 (is_hit, song_query)。
    如果用户只发了“点歌”/“我要听歌”，返回 (True, "")。
    """
    cleaned = (text or "").strip()
    if not cleaned:
        return False, ""

    for pat in _MUSIC_PATTERNS:
        m = pat.match(cleaned)
        if m:
            raw_song = m.group(1).strip()
            song = clean_song_query(raw_song)
            if song in ("", "歌", "一首", "一首歌", "音乐"):
                return True, ""
            return True, song

    return False, ""


def _rank_qq_songs(query: str, songs: list[dict]) -> dict | None:
    """智能优选 QQ 音乐候选曲目，剔除电台、DJ 串烧与翻唱干扰。"""
    if not songs:
        return None
    clean_q = re.sub(r"\s+", "", query).lower()
    scored = []
    for s in songs:
        name = str(s.get("songname") or "").strip()
        singers = [str(x.get("name") or "") for x in s.get("singer", [])]
        singer_str = "/".join(singers)
        album = str(s.get("albumname") or "").strip()
        combined = f"{name} {album}"

        # 过滤电台节目、有声小说、朗诵
        if re.search(r"(?:第\s*\d+\s*期|（节目）|\(节目\)|电台|有声书|小说|朗诵)", combined):
            score = -100
        else:
            score = 0
            clean_name = re.sub(r"\s+", "", name).lower()

            # 歌名完全相同 -> 极高权重
            if clean_name == clean_q:
                score += 100
            elif clean_q in clean_name:
                score += 60
            elif clean_name in clean_q:
                score += 40

            # 用户查询词命中歌手名
            for sng in singers:
                if sng and sng.lower() in clean_q:
                    score += 50
                    break

            # 避开纯伴奏、非点播类 DJ（除非用户明确搜了 dj/伴奏）
            if "dj" in clean_name and "dj" not in clean_q:
                score -= 30
            if "伴奏" in clean_name and "伴奏" not in clean_q:
                score -= 30

        scored.append((score, s))

    scored.sort(key=lambda x: x[0], reverse=True)
    if scored and scored[0][0] > -50:
        return scored[0][1]
    return songs[0]


def search_qqmusic(query: str) -> dict[str, Any] | None:
    """搜索 QQ 音乐（首选源）。支持多候选智能优选。"""
    clean_q = clean_song_query(query)
    if not clean_q:
        return None
    encoded = urllib.parse.quote(clean_q)
    url = f"https://c.y.qq.com/soso/fcgi-bin/client_search_cp?p=1&n=8&w={encoded}&format=json"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Referer": "https://y.qq.com/",
    }
    try:
        ctx = ssl._create_unverified_context()
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, context=ctx, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))
        song_list = data.get("data", {}).get("song", {}).get("list") or []
        if not song_list:
            return None

        # 智能挑选最佳匹配曲目
        s = _rank_qq_songs(clean_q, song_list)
        if not s:
            return None

        song_id = str(s.get("songid") or "")
        song_mid = str(s.get("songmid") or "")
        title = str(s.get("songname") or clean_q)
        singers = "/".join(str(x.get("name") or "") for x in s.get("singer", []) if x.get("name"))
        album_mid = str(s.get("albummid") or "")
        pic_url = f"https://y.gtimg.cn/music/photo_new/T002R300x300M000{album_mid}.jpg" if album_mid else ""
        return {
            "platform": "qq",
            "id": song_id,
            "mid": song_mid,
            "title": title,
            "singer": singers or "未知歌手",
            "album": str(s.get("albumname") or ""),
            "page_url": f"https://y.qq.com/n/ryqq/songDetail/{song_mid or song_id}",
            "audio_url": "",
            "pic_url": pic_url,
        }
    except Exception as exc:
        logger.warning("QQ音乐搜索失败: %s", exc)
        return None


def search_netease(query: str) -> dict[str, Any] | None:
    """搜索网易云音乐（备用源）。"""
    clean_q = clean_song_query(query)
    if not clean_q:
        return None
    encoded = urllib.parse.quote(clean_q)
    url = f"https://music.163.com/api/search/get/web?s={encoded}&type=1&offset=0&total=true&limit=6"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Referer": "https://music.163.com/",
    }
    try:
        ctx = ssl._create_unverified_context()
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, context=ctx, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))
        songs = data.get("result", {}).get("songs") or []
        if not songs:
            return None

        # 优先精准歌名
        chosen = songs[0]
        clean_q_lower = re.sub(r"\s+", "", clean_q).lower()
        for cand in songs:
            cand_name = re.sub(r"\s+", "", str(cand.get("name") or "")).lower()
            if cand_name == clean_q_lower:
                chosen = cand
                break

        song_id = str(chosen.get("id") or "")
        title = str(chosen.get("name") or clean_q)
        artists = "/".join(str(a.get("name") or "") for a in chosen.get("artists", []) if a.get("name"))
        album_info = chosen.get("album") or {}
        album_name = str(album_info.get("name") or "")
        pic_url = str(album_info.get("picUrl") or "")
        return {
            "platform": "163",
            "id": song_id,
            "title": title,
            "singer": artists or "未知歌手",
            "album": album_name,
            "page_url": f"https://music.163.com/#/song?id={song_id}",
            "audio_url": f"https://music.163.com/song/media/outer/url?id={song_id}.mp3",
            "pic_url": pic_url,
        }
    except Exception as exc:
        logger.warning("网易云搜索失败: %s", exc)
        return None


def search_song(query: str, prefer_platform: str = "qq") -> dict[str, Any] | None:
    """双源搜索：QQ 音乐优先（适应 QQ 平台生态与原生播放），失败则走网易云音乐。"""
    if prefer_platform == "qq":
        res = search_qqmusic(query)
        if res:
            return res
        return search_netease(query)
    res = search_netease(query)
    if res:
        return res
    return search_qqmusic(query)


def sign_music_card(song: dict[str, Any]) -> dict[str, Any] | None:
    """请求卡片签名服务获取原生 QQ 音乐 Ark 卡片 JSON。

    NapCat / QQ 原生 Ark 卡片结构 (com.tencent.music.lua)：
    具有官方签名 token，展示真实的播放按钮、高清封面、歌名与歌手信息，
    绝非纯文本超链接。
    """
    if not song:
        return None
    title = str(song.get("title") or "")
    singer = str(song.get("singer") or "未知歌手")
    page_url = str(song.get("page_url") or "")
    audio_url = str(song.get("audio_url") or page_url)
    pic_url = str(song.get("pic_url") or "https://y.gtimg.cn/mediastyle/global/img/album_300.png")

    payload = {
        "type": "custom",
        "url": page_url,
        "audio": audio_url,
        "title": title,
        "image": pic_url,
        "singer": singer,
    }

    sign_endpoints = [
        "https://ss.xingzhige.com/music_card/card",
    ]

    for endpoint in sign_endpoints:
        try:
            req_data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                endpoint,
                data=req_data,
                headers={"Content-Type": "application/json", "User-Agent": "NapCat"},
            )
            ctx = ssl._create_unverified_context()
            with urllib.request.urlopen(req, context=ctx, timeout=3.5) as resp:
                data = json.loads(resp.read().decode("utf-8", "ignore"))
                if isinstance(data, dict) and (data.get("app") or data.get("meta")):
                    return data
        except Exception as exc:
            logger.debug("请求音乐卡片签名服务失败 (%s): %s", endpoint, exc)

    return None

