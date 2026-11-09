"""表情包库：目录 + index.json 关键词，命中优先、没命中按概率随机。

素材三个来源，都走 add() 落到同一个目录：
  - QQ 账号自己收藏的表情（memes.sync_favorite）
  - 对方发来的动图表情（service._bank_gif），素材全靠这里长
  - QQ 官方表情搜索（memes.search / service._pick_sticker），搜到就入库，下次不再联网
文件名去掉扩展名当兜底关键词，index.json 里的 keywords 才是主关键词。
发出去的只有动图，所以能发是存进来的门槛：不是 gif、比发送上限还大的都别占库位。
"""

from __future__ import annotations

import hashlib
import itertools
import json
import logging
import os
import random
import threading
import time
from pathlib import Path

from . import media

logger = logging.getLogger(__name__)

_STICKER_DIR = Path(__file__).resolve().parent / "stickers"
_INDEX_NAME = "index.json"
_SCAN_TTL_SEC = 60.0
_MIN_KEYWORD_CHARS = 2
# 库太大时按修改时间淘汰最旧的非手工图（手工放的图不带 source 不淘汰）
_MAX_BANK_FILES = 600
# 存进来的上限 = 发得出去的上限：超过这个的截图占了库位也永远发不出去
_MAX_ADD_BYTES = 4 * 1024 * 1024

_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp"}

_lock = threading.Lock()
_cache: list[dict] = []
_cache_ts = 0.0
_index: dict[str, dict] = {}
_index_loaded = False
_index_dirty = False
# md5 -> 文件名，入库查重用
_by_md5: dict[str, str] = {}
_tmp_seq = itertools.count()


def _reload() -> None:
    """叫下一次 _scan 重新列目录（调用方已持锁）。"""
    global _cache_ts
    _cache_ts = 0.0


def sticker_dir() -> Path:
    _STICKER_DIR.mkdir(parents=True, exist_ok=True)
    return _STICKER_DIR


def _index_path() -> Path:
    return sticker_dir() / _INDEX_NAME


def _load_index() -> None:
    """读盘一次。之后内存里的那份就是权威，写盘失败也不会退化成重复入库。"""
    global _index, _by_md5, _index_loaded, _index_dirty
    if _index_loaded:
        return
    _index_loaded = True
    try:
        raw = json.loads(_index_path().read_text(encoding="utf-8"))
        disk = raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        disk = {}
    for name, info in disk.items():
        if name not in _index and isinstance(info, dict):
            _index[name] = info
            if info.get("md5"):
                _by_md5.setdefault(str(info["md5"]), name)


def _flush_index() -> None:
    """写索引。Windows 上索引正被别的进程读着时 replace 会 ACCESS_DENIED，
    所以用唯一临时名重试，最后退回直接覆写。"""
    global _index_dirty
    if not _index_dirty:
        return
    path = _index_path()
    payload = json.dumps(_index, ensure_ascii=False, indent=1)
    tmp = path.with_name(f".{_INDEX_NAME}.{os.getpid()}.{next(_tmp_seq)}.tmp")
    try:
        tmp.write_text(payload, encoding="utf-8")
    except OSError:
        logger.warning("表情包索引临时文件写失败", exc_info=True)
        return
    err: OSError | None = None
    for i in range(3):
        try:
            os.replace(tmp, path)
            _index_dirty = False
            return
        except OSError as exc:
            err = exc
            time.sleep(0.05 * (i + 1))
    try:
        path.write_text(payload, encoding="utf-8")
        tmp.unlink(missing_ok=True)
        _index_dirty = False
        logger.info("表情包索引改为直接覆写（replace 被占用：%s）", err)
    except OSError:
        logger.warning("表情包索引写入失败", exc_info=True)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _entry(path: Path) -> dict:
    """一张图 + 它的关键词（索引优先，文件名兜底）。

    索引里的关键词是人自己给的表情描述，一个字也算（"奶"）；
    文件名容易乱命中，要求两个字以上。
    """
    info = _index.get(path.name) or {}
    keys: list[str] = []
    for key in list(info.get("keywords") or []):
        key = str(key).strip()
        if key and key not in keys:
            keys.append(key)
    stem = path.stem.strip()
    if len(stem) >= _MIN_KEYWORD_CHARS and stem not in keys:
        keys.append(stem)
    return {"path": path, "keys": keys, "gif": path.suffix.lower() == ".gif",
            "source": str(info.get("source") or "")}


def _scan() -> list[dict]:
    """扫目录，带 TTL 缓存；新丢进来的图不用重启就能生效。"""
    global _cache, _cache_ts, _index_dirty
    now = time.time()
    with _lock:
        if _cache and now - _cache_ts <= _SCAN_TTL_SEC:
            return _cache
        _load_index()
        entries = []
        try:
            for p in sorted(sticker_dir().iterdir()):
                if p.is_file() and p.suffix.lower() in _EXT:
                    entries.append(_entry(p))
        except OSError:
            entries = []
        for name in [n for n in list(_index)
                     if not (sticker_dir() / n).exists()]:
            info = _index.pop(name) or {}
            if info.get("md5"):
                _by_md5.pop(str(info["md5"]), None)
            _index_dirty = True
        _flush_index()
        _cache = entries
        _cache_ts = now
        return _cache


def count() -> int:
    return len(_scan())


def pick(text: str, chance: float = 0.0) -> tuple[Path, str] | None:
    """按聊天文本挑一张动图：关键词命中优先（取最长命中），否则按概率随机。

    只发 GIF——表情包要动才叫表情包（超管明确要求），库里剩下的
    jpg/png 只当素材，永远不会被选中。
    返回 (图片路径, 命中方式)；None 表示不发。
    """
    entries = [e for e in _scan() if e["gif"]]
    if not entries:
        return None
    text = str(text or "")
    if text:
        best: tuple[int, dict, str] | None = None
        for entry in entries:
            for key in entry["keys"]:
                # 单字关键词只在短句里认，否则"别/了"这种会常年乱命中
                if len(key) < _MIN_KEYWORD_CHARS and len(text) > 8:
                    continue
                if key in text and (best is None or len(key) > best[0]):
                    best = (len(key), entry, key)
        if best is not None:
            return best[1]["path"], f"kw:{best[2]}"
    if chance > 0 and random.random() < chance:
        entry = random.choice(entries)
        return entry["path"], "random"
    return None


def gif_count() -> int:
    """库里真正能发的动图张数。"""
    return sum(1 for e in _scan() if e["gif"])


def has_gif() -> bool:
    """库里有没有发得出去的动图——决定要不要去线上搜的那一位。"""
    return any(e["gif"] for e in _scan())


def known(data: bytes) -> bool:
    """这张图是否已经在库里（按内容 md5）。同步用它区分"新存"和"复用"。"""
    if not data:
        return False
    digest = hashlib.md5(data).hexdigest()
    with _lock:
        _load_index()
        name = _by_md5.get(digest)
        return bool(name) and (sticker_dir() / name).exists()


def add(data: bytes, ext: str, keywords: list[str], source: str) -> Path | None:
    """把一张图的字节落进库，记关键词。同一内容重复入库直接复用。

    失败返回 None。
    """
    global _index_dirty
    # 扩展名只认字节里的魔数，接口字段/URL 后缀经常骗人（gif 存成 .jpg）
    sniffed = media.sniff_ext(data)
    ext = (sniffed if sniffed in _EXT else (ext or "")).lower() or ".jpg"
    if ext not in _EXT or not data or len(data) > _MAX_ADD_BYTES:
        return None
    digest = hashlib.md5(data).hexdigest()
    with _lock:
        _load_index()
        old_name = _by_md5.get(digest)
        old = sticker_dir() / old_name if old_name else None
        if old is not None and old.exists():
            # 重复同步时把新关键词并进去，不再多存一份文件
            info = _index.get(old_name) or {}
            keys = sorted(set(info.get("keywords") or [])
                          | {str(k).strip() for k in keywords if str(k).strip()})
            if keys != sorted(info.get("keywords") or []):
                info["keywords"] = keys
                _index[old_name] = info
                _index_dirty = True
                _flush_index()
                _reload()
            return old
        base = "".join(ch for ch in " ".join(keywords)
                       if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")[:16]
        name = f"{base or source}_{int(time.time() * 1000) % 10_000_000}{ext}"
        path = sticker_dir() / name
        i = 1
        while path.exists():
            path = sticker_dir() / f"{name[:-len(ext)]}_{i}{ext}"
            i += 1
        try:
            path.write_bytes(data)
        except OSError:
            logger.warning("表情包落盘失败: %s", path.name, exc_info=True)
            return None
        keys = sorted({str(k).strip() for k in keywords if str(k).strip()})
        _index[path.name] = {"keywords": keys, "source": source,
                             "md5": digest, "ts": int(time.time())}
        _by_md5[digest] = path.name
        _index_dirty = True
        _trim_locked()
        _flush_index()
        _reload()
        return path


def _trim_locked() -> None:
    """超上限时按索引时间戳淘汰自动收的图，手工丢进去的不动。"""
    auto = [(info.get("ts", 0), name) for name, info in _index.items()
            if info.get("source")]
    if len(auto) <= _MAX_BANK_FILES:
        return
    auto.sort()
    for _, name in auto[:len(auto) - _MAX_BANK_FILES]:
        try:
            (sticker_dir() / name).unlink(missing_ok=True)
        except OSError:
            continue
        info = _index.pop(name, None) or {}
        if info.get("md5"):
            _by_md5.pop(str(info["md5"]), None)
