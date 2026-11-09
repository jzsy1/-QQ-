"""工具参数归一化 — 兼容本地小模型常犯的字段名/前缀/布尔值错误。"""

from __future__ import annotations

import re
from typing import Any

from ..intent_utils import (
    CLOSE_VERB_ANYWHERE_RE,
    OPEN_VERB_ANYWHERE_RE,
    extract_action_core,
)

_APP_TOOLS = frozenset({
    "open_app",
    "close_app",
    "open_app_and_type",
    "wechat_send_message",
    "wechat_read_messages",
    "wechat_reply_current",
})

_APP_NAME_KEYS = (
    "app_name",
    "name",
    "app",
    "application",
    "software",
    "program",
    "target",
)

# 从 app_name 值或用户原话里剥掉动词前缀
_APP_VERB_PREFIX_RE = re.compile(
    r"^(?:请|帮我|给我|把)?(?:打开|启动|运行|开启|关闭|关掉|退出|结束|开一下|关一下)\s*",
)


def parse_bool(value: Any, default: bool = False) -> bool:
    """LLM 常返回字符串 'true'/'false'，不能直接用 bool()。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("true", "1", "yes", "on", "是", "对"):
            return True
        if s in ("false", "0", "no", "off", "否", "不"):
            return False
    return default


def _strip_app_verb(name: str) -> str:
    t = (name or "").strip()
    if not t:
        return ""
    prev = None
    while prev != t:
        prev = t
        t = _APP_VERB_PREFIX_RE.sub("", t).strip()
    return t.strip("「」\"'“”‘’ \t")


def _pick_app_name(params: dict) -> str:
    for key in _APP_NAME_KEYS:
        val = params.get(key)
        if val is None:
            continue
        s = str(val).strip()
        if s:
            return _strip_app_verb(s)
    return ""


def _extract_app_from_user_text(user_text: str, *, close: bool = False) -> str:
    t = (user_text or "").strip()
    if not t:
        return ""
    op, core = extract_action_core(t)
    want = "c" if close else "o"
    if op == want and core:
        return _strip_app_verb(core)
    pat = CLOSE_VERB_ANYWHERE_RE if close else OPEN_VERB_ANYWHERE_RE
    if pat.search(t):
        _, core2 = extract_action_core(t)
        if core2:
            return _strip_app_verb(core2)
    return ""


def normalize_app_tool_params(
    func: str,
    params: dict | None,
    *,
    user_text: str = "",
) -> dict:
    """open_app / close_app 等：统一 app_name，剥动词前缀，必要时从用户原话补全。"""
    out = dict(params or {})
    if func not in _APP_TOOLS:
        return out

    app_name = _pick_app_name(out)
    if not app_name and user_text:
        close = func == "close_app"
        app_name = _extract_app_from_user_text(user_text, close=close)
    if app_name:
        out["app_name"] = app_name

    if func in ("open_app", "open_app_and_type"):
        out["force_new"] = parse_bool(out.get("force_new"), default=False)
        if func == "open_app_and_type":
            out["press_enter"] = parse_bool(out.get("press_enter"), default=False)
    if func == "close_app":
        out["force"] = parse_bool(out.get("force"), default=True)

    return out


def normalize_tool_params(
    func: str,
    params: dict | None,
    *,
    user_text: str = "",
) -> dict:
    out = dict(params or {})
    fn = (func or "").strip()
    if not fn:
        return out

    if fn in _APP_TOOLS:
        return normalize_app_tool_params(fn, out, user_text=user_text)

    if fn == "focus_window" and not str(out.get("title") or "").strip():
        title = _pick_app_name(out) or _extract_app_from_user_text(user_text)
        if title:
            out["title"] = title

    if fn == "close_window" and not str(out.get("title") or "").strip():
        title = _pick_app_name(out) or _extract_app_from_user_text(
            user_text, close=True
        )
        if title:
            out["title"] = title

    if fn == "kill_process":
        out["force"] = parse_bool(out.get("force"), default=True)

    # 删/列 Word 文档时自动带后缀过滤，避免桌面一长串 .lnk 把 docx 挤出上下文
    if fn == "list_desktop_files" and not (
        out.get("extensions") or out.get("ext")
    ):
        ut = user_text or ""
        if re.search(r"word|WORD|\.docx|\.doc|文档", ut, re.I) and not re.search(
            r"excel|xlsx|表格|ppt|pdf|所有文件|全部文件", ut, re.I
        ):
            out["extensions"] = "docx,doc"

    return out


def remap_kill_to_close_app(
    func: str,
    args: dict | None,
    user_text: str,
) -> tuple[str, dict]:
    """用户说「关闭XX」时，模型误调 kill_process → 改走 close_app。"""
    fn = (func or "").strip()
    params = dict(args or {})
    t = (user_text or "").strip()
    if fn != "kill_process" or not t:
        return fn, params
    if not CLOSE_VERB_ANYWHERE_RE.search(t):
        return fn, params
    target = str(
        params.get("name_or_pid")
        or params.get("name")
        or params.get("app_name")
        or params.get("app")
        or ""
    ).strip()
    if not target or target.isdigit():
        return fn, params
    app_name = _strip_app_verb(target)
    if not app_name:
        return fn, params
    return "close_app", normalize_app_tool_params(
        "close_app",
        {"app_name": app_name, "force": params.get("force", True)},
        user_text=user_text,
    )
