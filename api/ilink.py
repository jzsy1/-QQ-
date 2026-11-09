"""微信 iLink 官方协议接入 — 个人微信扫码绑定 → 收发文本消息。

链路：手机微信 ↔ iLink 云端（ilinkai.weixin.qq.com）↔ 本地长轮询线程
      → 共享 chat 管线（与 /api/chat、语音同大脑）→ sendmessage 回复。

与企微（wecom，服务器回调推送）不同，iLink 是拉模式：本地线程持续
getupdates 长轮询（服务端挂起约 35s），无需公网回调地址，个人微信可用。

绑定：python -m kekesi.api.ilink  （控制台展示二维码链接并自动打开浏览器，
手机微信扫码确认后凭据自动保存到 data/ilink_credentials.json）。
bot_token 会过期（getupdates 返回 errcode=-14），过期后重新执行绑定即可。

config.yaml 配置：
    ilink:
      enabled: true
"""

from __future__ import annotations

import json
import logging
import random
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"
_CHANNEL_VERSION = "2.0.1-go"  # iLink 协议的客户端版本标识
_QR_POLL_TIMEOUT = 35          # get_qrcode_status 服务端长轮询约 30s，客户端略长
_LOGIN_TIMEOUT = 480           # 整体扫码等待上限（8 分钟）
_QR_MAX_REFRESH = 3            # 二维码过期自动刷新次数上限
_LONGPOLL_TIMEOUT = 40         # getupdates 服务端挂起约 35s + 网络余量
_SEND_TIMEOUT = 15
_ERROR_RETRY_SEC = 3
_IDLE_WAIT_SEC = 15            # 未绑定/未启用时轮询线程的休眠间隔
_EXPIRED_WAIT_SEC = 60         # 会话过期后等待重新绑定的间隔
_TEXT_MAX_CHARS = 4000         # 回复文本长度保护
_ERR_SESSION_EXPIRED = -14
_CRED_FILE = "ilink_credentials.json"
_CURSOR_FILE = "ilink_cursor.txt"

_PKG_ROOT = Path(__file__).resolve().parent.parent

_msg_ids: deque = deque(maxlen=256)
_msg_ids_lock = threading.Lock()

_start_lock = threading.Lock()
_started = False
_stop_event: threading.Event | None = None


class SessionExpiredError(RuntimeError):
    """bot_token 已失效（errcode=-14），需重新扫码绑定。"""


# --------------------------------------------------------------
# 配置与凭据
# --------------------------------------------------------------

def load_ilink_cfg(base_dir: Path) -> dict:
    """读 config.yaml 的 ilink 段；未配置或 enabled=false 返回空 dict。"""
    try:
        import yaml

        cfg_path = Path(base_dir) / "config.yaml"
        if not cfg_path.exists():
            return {}
        with open(cfg_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        cfg = data.get("ilink") or {}
        if not isinstance(cfg, dict) or not cfg.get("enabled"):
            return {}
        return cfg
    except Exception:
        logger.exception("读取 ilink 配置失败")
        return {}


def is_ilink_enabled(base_dir: Path) -> bool:
    return bool(load_ilink_cfg(base_dir))


def _cred_path(base_dir: Path) -> Path:
    return Path(base_dir) / "data" / _CRED_FILE


def _cursor_path(base_dir: Path) -> Path:
    return Path(base_dir) / "data" / _CURSOR_FILE


def load_credentials(base_dir: Path) -> dict:
    """读取已保存的绑定凭据；缺失或字段不全返回空 dict。"""
    try:
        p = _cred_path(base_dir)
        if not p.exists():
            return {}
        with open(p, encoding="utf-8") as f:
            data = json.load(f) or {}
        if not isinstance(data, dict):
            return {}
        creds = {
            "bot_token": str(data.get("bot_token") or "").strip(),
            "base_url": str(data.get("base_url") or "").strip() or _DEFAULT_BASE_URL,
            "bot_id": str(data.get("bot_id") or "").strip(),
            "user_id": str(data.get("user_id") or "").strip(),
        }
        return creds if creds["bot_token"] else {}
    except Exception:
        logger.exception("读取 ilink 凭据失败")
        return {}


def save_credentials(base_dir: Path, creds: dict) -> None:
    """绑定成功后持久化凭据（含 baseurl，微信可能返回区域节点地址）。"""
    p = _cred_path(base_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "bot_token": str(creds.get("bot_token") or "").strip(),
        "base_url": str(creds.get("base_url") or "").strip() or _DEFAULT_BASE_URL,
        "bot_id": str(creds.get("bot_id") or "").strip(),
        "user_id": str(creds.get("user_id") or "").strip(),
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    with open(p, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def _load_cursor(base_dir: Path) -> str:
    try:
        p = _cursor_path(base_dir)
        if p.exists():
            return p.read_text(encoding="utf-8").strip()
    except Exception:
        pass
    return ""


def _save_cursor(base_dir: Path, cursor: str) -> None:
    try:
        p = _cursor_path(base_dir)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(str(cursor or ""), encoding="utf-8")
    except Exception:
        logger.exception("保存 ilink 游标失败")


# --------------------------------------------------------------
# HTTP 底层
# --------------------------------------------------------------

def _api_headers(bot_token: str = "") -> dict:
    headers = {
        "Content-Type": "application/json",
        "iLink-App-ClientVersion": "1",
    }
    token = str(bot_token or "").strip()
    if token:
        headers["AuthorizationType"] = "ilink_bot_token"
        headers["Authorization"] = f"Bearer {token}"
        headers["X-WECHAT-UIN"] = str(random.randint(1000000000, 9999999999))
    return headers


def _base_url(base_url: str) -> str:
    url = str(base_url or "").strip() or _DEFAULT_BASE_URL
    return url if url.endswith("/") else url + "/"


def _api_post(base_url: str, endpoint: str, payload: dict, bot_token: str = "",
              timeout: float = _SEND_TIMEOUT) -> dict:
    import requests

    url = _base_url(base_url) + endpoint
    r = requests.post(url, json=payload, headers=_api_headers(bot_token), timeout=timeout)
    if r.status_code < 200 or r.status_code >= 300:
        raise RuntimeError(f"{endpoint} HTTP {r.status_code}: {r.text[:200]}")
    return r.json() if r.content else {}


def _api_get(base_url: str, endpoint: str, params: dict,
             timeout: float = _QR_POLL_TIMEOUT) -> dict:
    import requests

    url = _base_url(base_url) + endpoint
    r = requests.get(url, params=params, headers=_api_headers(), timeout=timeout)
    if r.status_code < 200 or r.status_code >= 300:
        raise RuntimeError(f"{endpoint} HTTP {r.status_code}: {r.text[:200]}")
    return r.json() if r.content else {}


# --------------------------------------------------------------
# 扫码绑定
# --------------------------------------------------------------

def fetch_qrcode(base_url: str = _DEFAULT_BASE_URL) -> dict:
    """获取登录二维码 → {qrcode, qrcode_img_content}。"""
    data = _api_get(base_url, "ilink/bot/get_bot_qrcode", {"bot_type": 3})
    qrcode = str(data.get("qrcode") or "").strip()
    img = str(data.get("qrcode_img_content") or "").strip()
    if not qrcode or not img:
        raise RuntimeError(f"获取二维码失败: {data}")
    return {"qrcode": qrcode, "qrcode_img_content": img}


def poll_qrcode_status(base_url: str, qrcode: str) -> dict:
    """单次扫码状态长轮询（服务端约 30s）→ {status: wait/scaned/expired/confirmed, ...}。

    客户端超时视为 wait（与官方实现一致）。
    """
    import requests

    try:
        return _api_get(
            base_url, "ilink/bot/get_qrcode_status", {"qrcode": qrcode},
            timeout=_QR_POLL_TIMEOUT,
        )
    except requests.Timeout:
        return {"status": "wait"}


def wait_for_login(base_url: str, qrcode: str, on_event: Callable[[str, str], None] | None = None,
                   deadline: float | None = None) -> dict:
    """循环轮询扫码状态直到 confirmed，返回凭据 dict。

    on_event(kind, detail): kind ∈ scanned / refreshed，用于 UI 提示。
    二维码过期自动刷新（最多 _QR_MAX_REFRESH 次）；整体 _LOGIN_TIMEOUT 超时。
    """
    if deadline is None:
        deadline = time.time() + _LOGIN_TIMEOUT
    current = qrcode
    img_url = ""
    refresh_count = 0
    scanned_notified = False

    while time.time() < deadline:
        status_data = poll_qrcode_status(base_url, current)
        status = str(status_data.get("status") or "").strip().lower()
        if status == "confirmed":
            bot_token = str(status_data.get("bot_token") or "").strip()
            if not bot_token:
                raise RuntimeError("扫码已确认但响应缺少 bot_token")
            return {
                "bot_token": bot_token,
                "base_url": str(status_data.get("baseurl") or "").strip() or base_url,
                "bot_id": str(status_data.get("ilink_bot_id") or "").strip(),
                "user_id": str(status_data.get("ilink_user_id") or "").strip(),
            }
        if status in ("scaned", "scanned"):
            if not scanned_notified:
                scanned_notified = True
                if on_event:
                    on_event("scanned", "已扫码，请在手机上确认绑定")
        elif status == "expired":
            refresh_count += 1
            if refresh_count > _QR_MAX_REFRESH:
                raise RuntimeError("二维码已多次过期，请重新执行绑定")
            new_qr = fetch_qrcode(base_url)
            current = new_qr["qrcode"]
            img_url = new_qr["qrcode_img_content"]
            scanned_notified = False
            if on_event:
                on_event("refreshed", img_url)
        # wait / 未知状态 → 继续下一轮
        time.sleep(1)
    raise RuntimeError("扫码等待超时，请重新执行绑定")


def bind_interactive(base_dir: Path | None = None) -> int:
    """控制台绑定流程：展示二维码链接 → 等待扫码 → 保存凭据。"""
    base = Path(base_dir) if base_dir else _PKG_ROOT
    print("[iLink] 正在获取微信绑定二维码…")
    qr = fetch_qrcode(_DEFAULT_BASE_URL)
    print("[iLink] 请用手机微信扫描二维码完成绑定（会提示绑定确认）：")
    print(f"[iLink] 二维码链接: {qr['qrcode_img_content']}")
    try:
        import webbrowser

        webbrowser.open(qr["qrcode_img_content"])
    except Exception:
        pass

    def _on_event(kind: str, detail: str) -> None:
        if kind == "scanned":
            print("[iLink] 已扫码，请在手机上确认绑定…")
        elif kind == "refreshed":
            print(f"[iLink] 二维码已过期刷新，新链接: {detail}")

    try:
        creds = wait_for_login(_DEFAULT_BASE_URL, qr["qrcode"], _on_event)
    except RuntimeError as e:
        print(f"[iLink] 绑定失败: {e}")
        return 1
    save_credentials(base, creds)
    print(f"[iLink] 绑定成功！bot_id={creds['bot_id'] or '未知'}")
    print("[iLink] 凭据已保存，科科斯现在可以收发你的微信消息了。")
    print("[iLink] 若语音助手正在运行，最多 15 秒后自动生效（无需重启）。")
    return 0


# --------------------------------------------------------------
# 收消息（getupdates 长轮询）
# --------------------------------------------------------------

def poll_updates(creds: dict, cursor: str) -> dict:
    """单次长轮询 → {"msgs": [...], "get_updates_buf": "新游标"}。

    服务端无消息时挂起约 35s 后返回空列表；errcode/ret = -14 表示会话过期。
    """
    data = _api_post(
        creds.get("base_url") or _DEFAULT_BASE_URL,
        "ilink/bot/getupdates",
        {"get_updates_buf": str(cursor or ""), "base_info": {"channel_version": _CHANNEL_VERSION}},
        bot_token=str(creds.get("bot_token") or ""),
        timeout=_LONGPOLL_TIMEOUT,
    )
    ret = int(data.get("ret") or 0)
    errcode = int(data.get("errcode") or 0)
    if ret == _ERR_SESSION_EXPIRED or errcode == _ERR_SESSION_EXPIRED:
        raise SessionExpiredError(f"ret={ret} errcode={errcode}")
    if ret != 0 or errcode != 0:
        raise RuntimeError(f"getupdates 失败: ret={ret} errcode={errcode} {data.get('errmsg') or ''}")
    msgs = data.get("msgs")
    return {
        "msgs": [m for m in msgs if isinstance(m, dict)] if isinstance(msgs, list) else [],
        "get_updates_buf": str(data.get("get_updates_buf") or cursor or ""),
    }


def extract_text(msg: dict) -> str:
    """取消息中第一个文本项内容；非文本消息返回空串。"""
    items = msg.get("item_list")
    if not isinstance(items, list):
        return ""
    for item in items:
        if not isinstance(item, dict):
            continue
        if int(item.get("type") or 0) == 1:
            text_item = item.get("text_item")
            if isinstance(text_item, dict):
                return str(text_item.get("text") or "")
    return ""


def _has_media(msg: dict) -> bool:
    items = msg.get("item_list")
    if not isinstance(items, list):
        return False
    return any(
        isinstance(item, dict) and int(item.get("type") or 0) in (2, 3, 4, 5)
        for item in items
    )


def _seen_msg_id(msg_id: str) -> bool:
    if not msg_id:
        return False
    with _msg_ids_lock:
        if msg_id in _msg_ids:
            return True
        _msg_ids.append(msg_id)
        return False


# --------------------------------------------------------------
# 发消息（sendmessage）
# --------------------------------------------------------------

def send_text(creds: dict, to_user_id: str, text: str, context_token: str = "") -> bool:
    """发送文本消息；超长按字符数保护截断。"""
    user = str(to_user_id or "").strip()
    body = str(text or "").strip()
    if not user or not body:
        return False
    if len(body) > _TEXT_MAX_CHARS:
        body = body[:_TEXT_MAX_CHARS] + "…"
    try:
        _api_post(
            creds.get("base_url") or _DEFAULT_BASE_URL,
            "ilink/bot/sendmessage",
            {
                "msg": {
                    "to_user_id": user,
                    "client_id": uuid.uuid4().hex,
                    "message_type": 2,      # bot 发送
                    "message_state": 2,     # 生成完毕
                    "item_list": [{"type": 1, "text_item": {"text": body}}],
                    "context_token": str(context_token or ""),
                },
                "base_info": {"channel_version": _CHANNEL_VERSION},
            },
            bot_token=str(creds.get("bot_token") or ""),
        )
        return True
    except SessionExpiredError:
        raise
    except Exception:
        logger.exception("iLink 发送消息失败 to=%s", user)
        return False


# --------------------------------------------------------------
# 消息处理与轮询线程
# --------------------------------------------------------------

def _process_msg(dispatch: Callable[[str, str], dict], msg: dict, creds: dict) -> None:
    from_user = str(msg.get("from_user_id") or "").strip()
    if not from_user:
        return
    # message_type=1 用户消息；2 为 bot 回显，忽略
    try:
        mtype = int(msg.get("message_type") or 0)
    except (TypeError, ValueError):
        return
    if mtype != 1:
        return

    context_token = str(msg.get("context_token") or "")
    text = extract_text(msg).strip()
    if not text:
        if _has_media(msg):
            # 语音/图片等：提示仅支持文字（二期再做语音）
            send_text(creds, from_user, "目前只支持文字指令，请用文字发送", context_token)
        return
    if _seen_msg_id(str(msg.get("message_id") or "")):
        logger.info("iLink 重复消息已忽略: %s", msg.get("message_id"))
        return

    result = dispatch(text, f"ilink_{from_user}")
    if result.get("ok"):
        reply = str(result.get("reply") or "已完成。").strip() or "已完成。"
    else:
        reply = f"处理失败：{result.get('error') or '未知错误'}"
    send_text(creds, from_user, reply, context_token)


def _poll_loop(base_dir: Path, dispatch: Callable[[str, str], dict],
               stop_event: threading.Event) -> None:
    base = Path(base_dir)
    last_mtime = 0.0
    creds: dict = {}
    cursor = ""
    expired_logged = False

    while not stop_event.is_set():
        try:
            if not is_ilink_enabled(base):
                stop_event.wait(_IDLE_WAIT_SEC)
                continue
            cred_file = _cred_path(base)
            if not cred_file.exists():
                if not expired_logged:
                    logger.info("iLink 未绑定，等待扫码: python -m kekesi.api.ilink")
                    expired_logged = True
                stop_event.wait(_IDLE_WAIT_SEC)
                continue

            mtime = cred_file.stat().st_mtime
            if mtime != last_mtime:
                fresh = load_credentials(base)
                if not fresh:
                    stop_event.wait(_IDLE_WAIT_SEC)
                    continue
                creds = fresh
                last_mtime = mtime
                cursor = _load_cursor(base)
                expired_logged = False
                logger.info("iLink 已绑定（bot_id=%s），开始接收微信消息", creds.get("bot_id"))

            result = poll_updates(creds, cursor)
            cursor = str(result.get("get_updates_buf") or cursor)
            _save_cursor(base, cursor)
            for msg in result.get("msgs") or []:
                _process_msg(dispatch, msg, creds)
        except SessionExpiredError:
            if not expired_logged:
                expired_logged = True
                logger.warning(
                    "iLink 会话已过期（bot_token 失效），请重新执行绑定: python -m kekesi.api.ilink"
                )
            stop_event.wait(_EXPIRED_WAIT_SEC)
        except Exception:
            logger.exception("iLink 轮询异常")
            stop_event.wait(_ERROR_RETRY_SEC)


def start_polling(base_dir: Path, dispatch: Callable[[str, str], dict]) -> bool:
    """启动 iLink 长轮询后台线程（幂等）；返回是否实际启动。"""
    global _started, _stop_event
    with _start_lock:
        if _started:
            return False
        _started = True
        _stop_event = threading.Event()
        stop_event = _stop_event
    threading.Thread(
        target=_poll_loop,
        args=(Path(base_dir), dispatch, stop_event),
        daemon=True,
        name="ilink-poll",
    ).start()
    return True


def stop_polling() -> None:
    """停止轮询线程（测试与优雅退出用）。"""
    global _started
    ev = _stop_event
    if ev is not None:
        ev.set()
    _started = False


def status_snapshot(base_dir: Path) -> dict:
    """管理后台展示用。"""
    cfg = load_ilink_cfg(base_dir)
    if not cfg:
        return {"enabled": False}
    creds = load_credentials(base_dir)
    return {
        "enabled": True,
        "bound": bool(creds),
        "bot_id": creds.get("bot_id") or "",
        "user_id": creds.get("user_id") or "",
    }


if __name__ == "__main__":
    raise SystemExit(bind_interactive())
