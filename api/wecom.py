"""企业微信自建应用接入 — 手机企微发指令 → 科科斯大脑 → 企微推送回复。

链路：企微服务器 →（内网穿透）→ admin server /api/wecom/callback
      → 验签 + AES 解密 → 异步丢给共享 chat 管线（与 /api/chat、语音同大脑）
      → 主动调 message/send 把回复推回企微。

企微要求回调 5 秒内响应，故本模块「立即回空串 + 后台线程处理 + 主动推送」。
仅支持文本消息；语音/图片等类型回复使用提示。

config.yaml 配置：
    wecom:
      enabled: true
      corpid: ww...
      agentid: 1000002
      secret: ...
      token: ...              # 企微后台「接收消息」设置的 Token（非 admin.token）
      encoding_aes_key: ...   # 企微后台生成的 EncodingAESKey（43 位）
      allowed_users: []       # 允许使用的成员 userid，空 = 不限制
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import struct
import threading
import time
import xml.etree.ElementTree as ET
from collections import deque
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 企微应用文本消息上限 2048 字节（UTF-8 中文 3 字节/字），留安全余量
_WECOM_TEXT_MAX_BYTES = 1900
_TOKEN_REFRESH_MARGIN = 300  # access_token 提前 300s 刷新

_msg_ids: deque = deque(maxlen=256)
_msg_ids_lock = threading.Lock()

# 企微 API 强制直连：绕过系统代理（Clash 等）。
# 出口 IP 固定为本机公网出口，避免代理节点漂移导致企微「企业可信IP」校验(60020)失效。
_DIRECT_PROXIES = {"http": None, "https": None}


def load_wecom_cfg(base_dir: Path) -> dict:
    """读 config.yaml 的 wecom 段；未配置或 enabled=false 返回空 dict。"""
    try:
        import yaml

        cfg_path = Path(base_dir) / "config.yaml"
        if not cfg_path.exists():
            return {}
        with open(cfg_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        cfg = data.get("wecom") or {}
        if not isinstance(cfg, dict) or not cfg.get("enabled"):
            return {}
        return cfg
    except Exception:
        logger.exception("读取 wecom 配置失败")
        return {}


def is_wecom_enabled(base_dir: Path) -> bool:
    cfg = load_wecom_cfg(base_dir)
    return bool(cfg) and all(
        str(cfg.get(k) or "").strip()
        for k in ("corpid", "agentid", "secret", "token", "encoding_aes_key")
    )


class WecomCrypto:
    """企微回调加解密协议（官方规范：SHA1 验签 + AES-256-CBC + PKCS7）。"""

    def __init__(self, token: str, encoding_aes_key: str, corpid: str):
        self.token = str(token or "").strip()
        self.corpid = str(corpid or "").strip()
        key = str(encoding_aes_key or "").strip()
        if not self.token or not key or not self.corpid:
            raise ValueError("wecom token / encoding_aes_key / corpid 不能为空")
        if len(key) != 43:
            raise ValueError(f"encoding_aes_key 必须为 43 位，当前 {len(key)} 位")
        self.aes_key = base64.b64decode(key + "=")
        if len(self.aes_key) != 32:
            raise ValueError("encoding_aes_key 解码后不是 32 字节")

    def signature(self, timestamp: str, nonce: str, encrypt: str) -> str:
        items = sorted([self.token, str(timestamp), str(nonce), str(encrypt)])
        return hashlib.sha1("".join(items).encode("utf-8")).hexdigest()

    def verify(self, timestamp: str, nonce: str, encrypt: str, msg_signature: str) -> bool:
        try:
            return bool(msg_signature) and self.signature(timestamp, nonce, encrypt) == str(msg_signature).strip()
        except Exception:
            return False

    def decrypt(self, encrypt_b64: str) -> str:
        """解密密文，返回明文消息体；receiveid 与 corpid 不符时抛 ValueError。"""
        from Crypto.Cipher import AES

        cipher = AES.new(self.aes_key, AES.MODE_CBC, iv=self.aes_key[:16])
        plain = cipher.decrypt(base64.b64decode(encrypt_b64))
        pad = plain[-1]
        if not (1 <= pad <= 32):
            raise ValueError("AES 解密失败：PKCS7 padding 非法")
        plain = plain[:-pad]
        if len(plain) < 20:
            raise ValueError("AES 解密失败：明文过短")
        msg_len = struct.unpack(">I", plain[16:20])[0]
        if msg_len < 0 or 20 + msg_len > len(plain):
            raise ValueError("AES 解密失败：消息长度非法")
        msg = plain[20 : 20 + msg_len].decode("utf-8")
        receive_id = plain[20 + msg_len :].decode("utf-8")
        if receive_id != self.corpid:
            raise ValueError(f"receiveid 不匹配：{receive_id!r}")
        return msg


class WecomClient:
    """access_token 缓存 + 主动发消息。"""

    def __init__(self, corpid: str, secret: str, agentid: int):
        self.corpid = str(corpid).strip()
        self.secret = str(secret).strip()
        self.agentid = int(agentid)
        self._token = ""
        self._token_expire_at = 0.0
        self._lock = threading.Lock()

    def get_token(self) -> str:
        import requests

        with self._lock:
            now = time.time()
            if self._token and now < self._token_expire_at:
                return self._token
            r = requests.get(
                "https://qyapi.weixin.qq.com/cgi-bin/gettoken",
                params={"corpid": self.corpid, "corpsecret": self.secret},
                timeout=10,
                proxies=_DIRECT_PROXIES,
            )
            data = r.json() if r.status_code == 200 else {}
            token = str(data.get("access_token") or "")
            if not token:
                errcode = data.get("errcode")
                # 40014/42001 token 失效 → 清缓存重试一次
                if errcode in (40014, 42001) and self._token:
                    self._token = ""
                    return self.get_token()
                raise RuntimeError(f"获取 access_token 失败: {data}")
            self._token = token
            self._token_expire_at = now + int(data.get("expires_in", 7200)) - _TOKEN_REFRESH_MARGIN
            return self._token

    def send_text(self, touser: str, content: str) -> bool:
        """主动推送文本消息；超长自动按字节截断。"""
        import requests

        user = str(touser or "").strip()
        text = _truncate_bytes(str(content or "").strip(), _WECOM_TEXT_MAX_BYTES)
        if not user or not text:
            return False
        try:
            r = requests.post(
                "https://qyapi.weixin.qq.com/cgi-bin/message/send",
                params={"access_token": self.get_token()},
                json={
                    "touser": user,
                    "msgtype": "text",
                    "agentid": self.agentid,
                    "text": {"content": text},
                },
                timeout=10,
                proxies=_DIRECT_PROXIES,
            )
            data = r.json() if r.status_code == 200 else {}
            if data.get("errcode") != 0:
                logger.warning("企微推送失败 touser=%s: %s", user, data)
                return False
            return True
        except Exception:
            logger.exception("企微推送异常 touser=%s", user)
            return False


def _truncate_bytes(text: str, max_bytes: int) -> str:
    """按 UTF-8 字节数安全截断（不切断多字节字符）。"""
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text
    cut = raw[:max_bytes]
    # 丢弃末尾不完整的多字节字符
    while cut:
        try:
            return cut.decode("utf-8") + "…"
        except UnicodeDecodeError:
            cut = cut[:-1]
    return ""


def parse_message(plain_xml: str) -> dict:
    """解析明文消息 XML → dict（无 Content 的非文本返回 type + from_user）。"""
    root = ET.fromstring(plain_xml)
    get = lambda tag: (root.findtext(tag) or "").strip()  # noqa: E731
    msg = {
        "type": get("MsgType"),
        "from_user": get("FromUserName"),
        "msg_id": get("MsgId"),
        "content": get("Content"),
        "event": get("Event"),
    }
    return msg


def _seen_msg_id(msg_id: str) -> bool:
    if not msg_id:
        return False
    with _msg_ids_lock:
        if msg_id in _msg_ids:
            return True
        _msg_ids.append(msg_id)
        return False


def _resolve_targets(base_dir: Path) -> tuple[WecomCrypto | None, WecomClient | None, list]:
    """按配置构建 crypto/client；配置不全返回 (None, None, [])。"""
    cfg = load_wecom_cfg(base_dir)
    if not cfg:
        return None, None, []
    try:
        crypto = WecomCrypto(cfg.get("token"), cfg.get("encoding_aes_key"), cfg.get("corpid"))
    except ValueError as e:
        logger.error("wecom 配置错误: %s", e)
        return None, None, []
    client = WecomClient(cfg.get("corpid"), cfg.get("secret"), int(cfg.get("agentid") or 0))
    users = cfg.get("allowed_users") or []
    users = [str(u).strip() for u in users if str(u).strip()] if isinstance(users, list) else []
    return crypto, client, users


def handle_callback(base_dir: Path, query: dict, body: str, dispatch) -> tuple[int, str]:
    """企微回调统一入口。

    GET 验证:  query 带 msg_signature/echostr → 返回 (200, 解密后的明文 echostr)
    POST 消息: body 为加密 XML → 立即 (200, "") 后台处理，回复走主动推送

    dispatch: Callable[[str, str], dict] —— (text, session_id) → 执行结果 dict
              （server.execute_chat_text，与 /api/chat 共用同一管线和锁）
    返回 (http_status, response_body_text)
    """
    crypto, client, users = _resolve_targets(base_dir)
    if crypto is None:
        return 404, "wecom disabled"

    timestamp = str((query.get("timestamp") or [""])[0])
    nonce = str((query.get("nonce") or [""])[0])
    msg_signature = str((query.get("msg_signature") or [""])[0])

    if body:
        # POST：解析外层 XML 取 Encrypt
        try:
            encrypt = (ET.fromstring(body).findtext("Encrypt") or "").strip()
        except ET.ParseError:
            logger.warning("wecom 回调 XML 解析失败")
            return 400, "bad xml"
        if not crypto.verify(timestamp, nonce, encrypt, msg_signature):
            logger.warning("wecom 回调验签失败")
            return 400, "bad signature"
        try:
            plain_xml = crypto.decrypt(encrypt)
        except ValueError as e:
            logger.warning("wecom 回调解密失败: %s", e)
            return 400, "bad encrypt"
        try:
            msg = parse_message(plain_xml)
        except ET.ParseError:
            logger.warning("wecom 明文消息 XML 解析失败")
            return 200, ""
        _dispatch_async(dispatch, msg, client, users)
        return 200, ""

    # GET：URL 验证
    echostr = str((query.get("echostr") or [""])[0])
    if not echostr or not crypto.verify(timestamp, nonce, echostr, msg_signature):
        logger.warning("wecom URL 验证失败")
        return 400, "bad signature"
    try:
        return 200, crypto.decrypt(echostr)
    except ValueError as e:
        logger.warning("wecom echostr 解密失败: %s", e)
        return 400, "bad encrypt"


def _dispatch_async(dispatch, msg: dict, client: WecomClient, allowed_users: list) -> None:
    def _work():
        try:
            _process_msg(dispatch, msg, client, allowed_users)
        except Exception:
            logger.exception("wecom 消息处理异常")

    threading.Thread(target=_work, daemon=True, name="wecom-msg").start()


def _process_msg(dispatch, msg: dict, client: WecomClient, allowed_users: list) -> None:
    user = msg.get("from_user") or ""
    if not user:
        return
    if allowed_users and user not in [str(u).strip() for u in allowed_users]:
        client.send_text(user, "你没有使用权限（wecom.allowed_users 未包含你）")
        return

    mtype = msg.get("type")
    if mtype != "text":
        client.send_text(user, "目前只支持文字指令，请用文字发送")
        return

    text = (msg.get("content") or "").strip()
    if not text:
        return
    if _seen_msg_id(msg.get("msg_id")):
        logger.info("wecom 重复消息已忽略: %s", msg.get("msg_id"))
        return

    client.send_text(user, "收到，正在处理…")
    result = dispatch(text, f"wecom_{user}")
    if result.get("ok"):
        reply = result.get("reply") or "已完成。"
    else:
        reply = f"处理失败：{result.get('error') or '未知错误'}"
    client.send_text(user, reply)


def status_snapshot(base_dir: Path) -> dict:
    """管理后台展示用。"""
    cfg = load_wecom_cfg(base_dir)
    if not cfg:
        return {"enabled": False}
    complete = all(
        str(cfg.get(k) or "").strip()
        for k in ("corpid", "agentid", "secret", "token", "encoding_aes_key")
    )
    return {
        "enabled": True,
        "complete": complete,
        "corpid": str(cfg.get("corpid") or ""),
        "agentid": cfg.get("agentid"),
        "allowed_users": cfg.get("allowed_users") or [],
    }
