"""表情包素材来源。

1. sync_favorite：把 QQ 账号自己收藏的表情拉下来入库（NapCat
   fetch_custom_face_detail，desc 就是人自己写的关键词）。
2. search：QQ 官方表情搜索（和 QQ 客户端表情面板搜的是同一个接口），
   只收 gif，搜到即入库，下次不再联网。库里有动图时不该走到这里
   （service._pick_sticker 把关），线上搜只是空库时的兜底。
3. 对方发来的动图由 service._bank_gif 直接收进库（字节已经取到手了）。

只用标准库；素材一律落成字节写进 stickers/，发送时给 NapCat 本地路径
（base64 分支会换成随机文件名，.gif 的扩展名一丢就被当静态图）。
第三方 meme 接口（utting/66mz8/dmoe）实测本机已全部不可达。
"""

from __future__ import annotations

import json
import logging
import random
import re
import urllib.parse
import urllib.request

from . import media, stickers

logger = logging.getLogger(__name__)

SEARCH_URL = "https://h5api.sginput.qq.com/wxbq/search"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_MAX_RESULT_BYTES = 2 * 1024 * 1024


def _get_json(url: str, timeout: float) -> tuple[dict, str]:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "ignore")), ""
    except Exception as exc:
        return {}, f"{type(exc).__name__}: {str(exc)[:120]}"


def _ext_of(url: str, fmt: str) -> str:
    """URL/接口字段猜的扩展名，只在字节魔数看不出来时兜底。"""
    fmt = str(fmt or "").lower().strip()
    if fmt in ("gif", "jpg", "png", "webp"):
        return f".{fmt}"
    tail = urllib.parse.urlparse(str(url or "")).path.rsplit(".", 1)
    if len(tail) == 2 and tail[1].lower() in ("gif", "jpg", "jpeg", "png", "webp"):
        return "." + tail[1].lower()
    return ""


def sync_favorite(transport, limit: int = 60, timeout: float = 20.0) -> dict:
    """把账号收藏的表情同步进 stickers/ 库。"""
    out = {"fetched": 0, "added": 0, "reused": 0, "skipped": 0, "error": ""}
    if transport is None:
        out["error"] = "OneBot 未连接"
        return out
    ok, data, err = transport.call_action_data(
        "fetch_custom_face_detail", {"count": max(1, min(200, limit))}, timeout)
    if not ok or not isinstance(data, list):
        out["error"] = err or "没拿到表情列表"
        return out
    for item in data:
        url = ""
        desc = ""
        if isinstance(item, dict):
            url = str(item.get("url") or "").strip()
            desc = str(item.get("desc") or item.get("name") or "").strip()
        elif isinstance(item, str):
            url = item.strip()
        if not url:
            out["skipped"] += 1
            continue
        body, derr = media.download(url, timeout=max(6.0, timeout * 0.6))
        if not body:
            out["skipped"] += 1
            out["error"] = f"下载失败({derr[:60]})"
            continue
        out["fetched"] += 1
        if stickers.known(body):
            out["reused"] += 1
            continue
        path = stickers.add(body, _ext_of(url, ""), [desc] if desc else [],
                            "fav")
        if path is None:
            out["skipped"] += 1
        else:
            out["added"] += 1
    return out


def search_keys(text: str) -> list[str]:
    """从一句话里整出最多三个搜索词：最长的中文串 → 后 4 字 → 后 2 字。"""
    runs = re.findall(r"[\u4e00-\u9fff]{2,}", str(text or ""))
    if not runs:
        return []
    run = max(runs, key=len)[:10]
    keys = [run]
    for n in (4, 2):
        if len(run) > n:
            keys.append(run[-n:])
    seen: list[str] = []
    for key in keys:
        if key not in seen:
            seen.append(key)
    return seen


def search(text: str, timeout: float = 6.0, num: int = 30):
    """QQ 官方表情搜索，只要动图。返回 (字节, ".gif", 搜索词) 或 None。

    接口同时会返回 jpg/png 缩略，那些一律不要：先按声明格式筛，
    下载后还得字节魔数确实是 gif 才认（表情包要能动，超管明确要求）。
    """
    for key in search_keys(text)[:3]:
        url = SEARCH_URL + "?" + urllib.parse.urlencode(
            {"key": key, "page": 1, "num": num})
        data, err = _get_json(url, timeout)
        if err:
            logger.info("表情搜索请求失败 %s: %s", key, err)
            continue
        items = data.get("data")
        if str(data.get("hit")) not in ("0", "") or not isinstance(items, list):
            continue
        cands = [it for it in items
                 if isinstance(it, dict)
                 and str(it.get("indexUrl") or "").startswith("http")
                 and _looks_gif(it)
                 and 0 < int(it.get("fileSize") or 0) <= _MAX_RESULT_BYTES]
        if not cands:
            continue
        random.shuffle(cands)
        for it in cands[:4]:
            body, derr = media.download(str(it["indexUrl"]), timeout)
            if not body:
                logger.info("表情搜索命中的图下载失败 %s: %s", key, derr)
                continue
            if media.sniff_ext(body) != ".gif":
                continue
            return body, ".gif", key
    return None


def _looks_gif(item: dict) -> bool:
    fmt = str(item.get("format") or "").lower().strip()
    if fmt:
        return fmt == "gif"
    return ".gif" in urllib.parse.urlparse(
        str(item.get("indexUrl") or "")).path.lower()
