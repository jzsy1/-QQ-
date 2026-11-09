"""把 OneBot 的图片段变成视觉模型能吃的字节。

NapCat 的 image 段里 file 只是文件名、url 是带 rkey 的短期 CDN 地址：
远端模型既抓不动 QQ 图床，也会赶上 rkey 过期（NapCat #807 那类
retcode:1200）。所以一律在本机落地成字节，再转 base64 直传模型。

取字节顺序（前面命中就停）：
  1. 段里自带 path / file 是真实本地路径  → 直接读（bot 和 NapCat 同机）
  2. 段里的 url 是 http(s)               → 本机下载
  3. OneBot get_image(file=文件名)       → NapCat 下载后回本地路径 → 读
  4. OneBot get_image(url=地址)          → 同上
"""

from __future__ import annotations

import base64
import io
import logging
import os
import re
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

_MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024

# (扩展名, 判别函数)
_MAGIC = (
    (".png", lambda b: b[:8] == b"\x89PNG\r\n\x1a\n"),
    (".jpg", lambda b: b[:3] == b"\xff\xd8\xff"),
    (".gif", lambda b: b[:6] in (b"GIF87a", b"GIF89a")),
    (".webp", lambda b: b[:4] == b"RIFF" and b[8:12] == b"WEBP"),
    (".bmp", lambda b: b[:2] == b"BM"),
)
_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".gif": "image/gif",
         ".webp": "image/webp", ".bmp": "image/bmp"}

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def sniff_ext(data: bytes) -> str:
    for ext, test in _MAGIC:
        try:
            if test(data):
                return ext
        except Exception:
            continue
    return ""


def download(url: str, timeout: float = 12.0) -> tuple[bytes, str]:
    """下载并校验确实是图片。返回 (字节, 错误)。"""
    if not str(url or "").lower().startswith(("http://", "https://")):
        return b"", "不是 http(s) 地址"
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": _UA,
            # 老协议图不带 Referer 会回 1x1 的 dummy.gif
            "Referer": "https://cdn.dsm.qpic.cn/merchant/",
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read(_MAX_DOWNLOAD_BYTES + 1)
    except Exception as exc:
        return b"", f"{type(exc).__name__}: {str(exc)[:120]}"
    if len(data) > _MAX_DOWNLOAD_BYTES:
        return b"", "图片过大"
    if not sniff_ext(data):
        return b"", f"拿到的不是图片({len(data)}B)"
    return data, ""


def _local_path(value: str) -> str:
    """段里的 file/path/url 字段，能当本地文件读就返回绝对路径。"""
    s = str(value or "").strip()
    if not s:
        return ""
    if s.lower().startswith("file://"):
        s = urllib.parse.unquote(s[len("file://"):])
        if os.name == "nt" and re.match(r"^/[a-zA-Z]:", s):
            s = s[1:]
    elif "://" in s:
        return ""
    s = s.replace("\\", os.sep) or s
    try:
        if os.path.isfile(s):
            return os.path.abspath(s)
    except (OSError, ValueError):
        return ""
    return ""


def read_local(path: str) -> tuple[bytes, str]:
    try:
        size = os.path.getsize(path)
        if size > _MAX_DOWNLOAD_BYTES:
            return b"", f"本地文件过大({size}B)"
        with open(path, "rb") as f:
            data = f.read()
    except OSError as exc:
        return b"", f"本地读失败：{exc}"
    if not sniff_ext(data):
        return b"", "本地文件不是可识别图片"
    return data, ""


def _from_get_image(transport, key: str, value: str) -> tuple[bytes, str]:
    """调 NapCat get_image 换本地路径（或它已经算好的 base64）。"""
    if transport is None or not value:
        return b"", "无 transport"
    try:
        ok, data, err = transport.call_action_data("get_image", {key: value}, 20.0)
    except Exception as exc:
        return b"", f"get_image 异常：{type(exc).__name__}"
    if not ok or not isinstance(data, dict):
        return b"", f"get_image 失败：{err or '无返回'}"
    b64 = str(data.get("base64") or "").strip()
    if b64:
        try:
            raw = base64.b64decode(b64)
        except Exception:
            return b"", "get_image base64 解不开"
        if sniff_ext(raw):
            return raw, ""
    path = _local_path(str(data.get("file") or ""))
    if path:
        return read_local(path)
    return b"", "get_image 没给可用文件"


def fetch_bytes(ref: dict, transport=None, timeout: float = 12.0):
    """图片段 → (字节, mime, 尝试过程)。全失败时字节为 b""。"""
    ref = ref or {}
    url = str(ref.get("url") or "").strip()
    name = str(ref.get("file") or "").strip()
    trail: list[str] = []

    for key in ("path", "file", "url"):
        path = _local_path(str(ref.get(key) or ""))
        if path:
            data, err = read_local(path)
            if data:
                return data, _MIME[sniff_ext(data)], [f"{key}本地读"]
            trail.append(f"{key}本地:{err}")

    if url.lower().startswith(("http://", "https://")):
        data, err = download(url, timeout)
        if data:
            return data, _MIME[sniff_ext(data)], trail + ["url 本机下载"]
        trail.append(f"url下载:{err}")
    elif url:
        trail.append("url 非 http")

    for key, value in (("file", name), ("url", url)):
        if not value:
            continue
        data, err = _from_get_image(transport, key, value)
        if data:
            return data, _MIME[sniff_ext(data)], trail + [f"get_image({key})"]
        trail.append(f"get_image({key}):{err}")

    return b"", "", trail or ["图片段里没有可用地址"]


def shrink(data: bytes, mime: str, max_bytes: int, max_edge: int):
    """超过 max_bytes 或边长超 max_edge 的图，压到模型够用就行（base64 会再胀 1/3）。

    GIF 不动：PIL 只保留首帧，动图表情没必要给模型看第一帧以外的东西。
    """
    if not data or len(data) <= max_bytes:
        return data, mime
    if mime == "image/gif":
        return data, mime
    try:
        from PIL import Image
    except ImportError:
        return data, mime
    try:
        with Image.open(io.BytesIO(data)) as img:
            img.load()
            if max(img.size) > max_edge:
                img.thumbnail((max_edge, max_edge))
            img = img.convert("RGB")
            quality = 80
            while True:
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=quality, optimize=True)
                out = buf.getvalue()
                if len(out) <= max_bytes or quality <= 45:
                    return out, "image/jpeg"
                quality -= 15
    except Exception:
        logger.debug("图片压缩失败，用原图", exc_info=True)
        return data, mime


def to_data_url(data: bytes, mime: str) -> str:
    return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")
