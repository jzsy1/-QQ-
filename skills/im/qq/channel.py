"""OneBot v11 消息事件解析：归一成插件内部用的字典。

兼容 array 和 CQ 码两种消息格式（NapCat 默认 array，CQ 码也开）。
群聊里 @ 机器人、引用回复机器人都算“点名”。
"""

from __future__ import annotations

import re

_CQ_RE = re.compile(r"\[CQ:[^\]]*\]")
_REPLY_RE = re.compile(r"\[CQ:reply[,\]]")
_AT_RE = re.compile(r"\[CQ:at,qq=(\d+)[,\]]")

_UNESCAPE = (
    ("&#91;", "["),
    ("&#93;", "]"),
    ("&amp;", "&"),
    ("&#44;", ","),
)


def unescape(text: str) -> str:
    for old, new in _UNESCAPE:
        text = text.replace(old, new)
    return text


def parse_message_event(data: dict, self_id: str) -> dict | None:
    """只处理 message 事件，其余返回 None。

    返回字段：chat_key / chat_type / chat_id / user_id / user_name /
    text / at_me / message_id。
    """
    if data.get("post_type") != "message":
        return None
    chat_type = str(data.get("message_type") or "")
    if chat_type not in ("group", "private"):
        return None
    user_id = str(data.get("user_id") or "")
    sender = data.get("sender") or {}
    # card = 群名片（群内备注），nickname = QQ 昵称；显示名优先群名片
    card = str(sender.get("card") or "").strip()
    nickname = str(sender.get("nickname") or "").strip()
    user_name = card or nickname or user_id
    chat_id = str(data.get("group_id") or user_id or "")
    chat_key = f"group_{chat_id}" if chat_type == "group" else f"private_{chat_id}"

    at_me = False
    reply_me = False
    text = ""
    images: list[dict] = []
    message = data.get("message")
    if isinstance(message, list):
        parts: list[str] = []
        for seg in message:
            if not isinstance(seg, dict):
                continue
            seg_type = str(seg.get("type") or "")
            seg_data = seg.get("data") or {}
            if seg_type == "text":
                parts.append(str(seg_data.get("text") or ""))
            elif seg_type == "at":
                if str(seg_data.get("qq")) == str(self_id):
                    at_me = True
            elif seg_type == "reply":
                reply_me = True
            elif seg_type == "image":
                # 收图理解：把段里的原始线索都留下，取字节的路子在 media.py
                ref = {
                    "url": str(seg_data.get("url") or "").strip(),
                    "file": str(seg_data.get("file") or "").strip(),
                    "path": str(seg_data.get("path") or "").strip(),
                    "summary": str(seg_data.get("summary") or "").strip(),
                }
                if any(ref.values()):
                    images.append(ref)
        text = unescape("".join(parts))
        raw = str(data.get("raw_message") or "")
        if not at_me:
            at_me = any(qq == str(self_id) for qq in _AT_RE.findall(raw))
        if not images:
            images = _cq_image_refs(raw)
    else:
        raw = str(message if message is not None
                  else data.get("raw_message") or "")
        at_me = any(qq == str(self_id) for qq in _AT_RE.findall(raw))
        reply_me = bool(_REPLY_RE.search(raw))
        images = _cq_image_refs(raw)
        text = unescape(_CQ_RE.sub("", raw))

    text = re.sub(r"\s+", " ", text).strip()
    if images and not text:
        text = "[图片]"
    if not text and not at_me:
        return None
    if not text:
        return None
    return {
        "chat_key": chat_key,
        "chat_type": chat_type,
        "chat_id": chat_id,
        "user_id": user_id,
        "user_name": user_name,
        "card": card,
        "nickname": nickname,
        "text": text,
        "images": images,
        "at_me": at_me,
        "reply_me": reply_me,
        "message_id": str(data.get("message_id") or ""),
        "self_id": str(self_id or ""),
    }


def _cq_image_refs(raw: str) -> list[dict]:
    """从 CQ 码字符串里抠图片线索（[CQ:image,file=...,url=...]）。

    值里的逗号按 v11 规范转义成 &#44;，所以直接按逗号切属性是安全的。
    """
    refs: list[dict] = []
    for match in re.finditer(r"\[CQ:image,([^\]]*)\]", raw or ""):
        attrs = {}
        for item in match.group(1).split(","):
            key, _, value = item.partition("=")
            if value:
                attrs[key.strip()] = unescape(value.strip())
        ref = {
            "url": attrs.get("url", ""),
            # CQ 里的 file 可能只是文件名，也可能是路径/地址，media 里再判
            "file": attrs.get("file", ""),
            "path": attrs.get("path", ""),
            "summary": attrs.get("summary", ""),
        }
        if any(ref.values()):
            refs.append(ref)
    return refs


def send_action(chat_type: str, chat_id: str, text: str = "",
                image_file: str = "",
                segments: list[dict] | None = None) -> tuple[str, dict]:
    """生成发消息的 (action, params)。纯文字，不碰语音壳。

    image_file 是 OneBot 的 image 段 file 值：本地绝对路径。
    segments 支持 OneBot v11 原生消息段（如 music 卡片、组合消息）。
    """
    if segments:
        message = segments
    elif image_file:
        message = [
            {"type": "text", "data": {"text": text}},
            {"type": "image", "data": {"file": image_file}},
        ]
    else:
        message = text
    try:
        cid = int(chat_id)
    except (TypeError, ValueError):
        cid = chat_id
    if chat_type == "group":
        return "send_group_msg", {"group_id": cid, "message": message}
    return "send_private_msg", {"user_id": cid, "message": message}


def send_segments_action(chat_type: str, chat_id: str,
                         segments: list[dict]) -> tuple[str, dict]:
    """生成发送结构化消息段（例如 music 卡片、图片、文本组合）的 (action, params)。"""
    return send_action(chat_type, chat_id, segments=segments)

