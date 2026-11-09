"""微信自动化 — 客户区坐标 + 键盘双策略（Qt 窗口无 UIA 控件）"""

from __future__ import annotations

import base64
import json
import logging
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from .app_control import launch_app
from .ocr_ops import OcrLine, ocr_screen
from .process_ops import focus_app, is_app_running
from .winops import get_clipboard, set_clipboard

logger = logging.getLogger(__name__)

_SCRIPT_PATH = Path(__file__).with_name("wechat_send.ps1")
_READ_SCRIPT_PATH = Path(__file__).with_name("wechat_read.ps1")
_REPLY_SCRIPT_PATH = Path(__file__).with_name("wechat_reply.ps1")
_NO_WINDOW = 0x08000000

_TIME_RE = re.compile(r"^\d{1,2}:\d{2}$")
_UNREAD_RE = re.compile(r"^\d{1,3}$")
_NOISE_LEFT = frozenset({
    "微信", "Weixin", "WeChat", "搜索", "通讯录", "发现", "小程序",
    "聊天", "文件传输助手", "折叠的聊天",
})


@dataclass
class _SidebarRow:
    name: str = ""
    preview: str = ""
    unread: int = 0
    click_y: int = 0
    lines: list[str] = field(default_factory=list)


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _wechat_window_visible() -> bool:
    if not _READ_SCRIPT_PATH.exists():
        return True
    try:
        result = _run_ps_script(_READ_SCRIPT_PATH, ["-Action", "get_regions"], timeout=15)
        return result.returncode == 0
    except Exception:
        return False


def _ensure_app(app_name: str, launch_cmd: str | None) -> tuple[bool, str]:
    display = (app_name or "").strip()
    if not display:
        return False, "请指定应用名称（app_name）"
    if is_app_running(display, launch_cmd or ""):
        if launch_cmd and not _wechat_window_visible():
            launch_app(launch_cmd)
            time.sleep(2.0)
        focus_app(display, launch_cmd or "")
        time.sleep(0.6)
        return True, f"{display} 已在运行"

    if not launch_cmd:
        return False, f"未找到「{display}」的启动方式"
    ok, msg = launch_app(launch_cmd)
    if not ok:
        return False, msg
    time.sleep(3.0)
    focus_app(display, launch_cmd)
    time.sleep(1.0)
    return True, f"已打开 {display}"


def _run_ps_script(
    script: Path,
    args: list[str],
    *,
    timeout: int = 45,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
        creationflags=_NO_WINDOW,
    )


def _run_send_script(contact: str, message: str, mode: str = "auto") -> subprocess.CompletedProcess:
    return _run_ps_script(
        _SCRIPT_PATH,
        [_b64(contact), _b64(message), mode],
    )


def _parse_failure(result: subprocess.CompletedProcess, app_name: str, action: str) -> str:
    err = (result.stderr or "").strip()
    out = (result.stdout or "").strip()
    display = (app_name or "应用").strip()
    logger.error("%s %s 失败 code=%s stderr=%s", display, action, result.returncode, err[:300])
    if "WECHAT_NOT_RUNNING" in err:
        return f"{display} 未运行，请先打开主窗口"
    if "NO_WINDOW" in err or "WINDOW_TOO_SMALL" in err:
        return f"找不到 {display} 主窗口或窗口太小，请把窗口显示出来并尽量放大"
    detail = err.split(":")[-1].strip() if err else out
    return f"{display} {action}失败: {detail[:100] or '未知错误'}"


def _parse_send_failure(result: subprocess.CompletedProcess, app_name: str) -> str:
    return _parse_failure(result, app_name, "发消息")


def _get_panel_regions(app_name: str) -> tuple[bool, dict | str]:
    if not _READ_SCRIPT_PATH.exists():
        return False, f"缺少脚本 {_READ_SCRIPT_PATH.name}"

    result = _run_ps_script(_READ_SCRIPT_PATH, ["-Action", "get_regions"], timeout=30)
    if result.returncode != 0:
        return False, _parse_failure(result, app_name, "读取窗口")

    raw = (result.stdout or "").strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return False, "无法解析微信窗口区域"
    if not data.get("ok"):
        return False, "微信窗口区域无效"
    return True, data


def _region_tuple(panel: dict) -> tuple[int, int, int, int]:
    return (
        int(panel["x"]),
        int(panel["y"]),
        int(panel["w"]),
        int(panel["h"]),
    )


def _ocr_panel(panel: dict) -> tuple[bool, str, list[OcrLine]]:
    result = ocr_screen(region=_region_tuple(panel))
    if not result.ok:
        return False, result.message, []
    return True, result.text, result.lines


def _group_sidebar_lines(lines: list[OcrLine], panel_top: int) -> list[_SidebarRow]:
    if not lines:
        return []

    sorted_lines = sorted(lines, key=lambda ln: ln.center[1])
    groups: list[list[OcrLine]] = []
    current: list[OcrLine] = []
    last_y: int | None = None

    for line in sorted_lines:
        y = line.center[1]
        if last_y is not None and abs(y - last_y) > 42:
            if current:
                groups.append(current)
            current = [line]
        else:
            current.append(line)
        last_y = y
    if current:
        groups.append(current)

    rows: list[_SidebarRow] = []
    for group in groups:
        texts = [ln.text.strip() for ln in group if ln.text.strip()]
        if not texts:
            continue
        if any(t in _NOISE_LEFT for t in texts):
            continue

        unread = 0
        filtered: list[str] = []
        for t in texts:
            if _UNREAD_RE.match(t):
                unread = max(unread, int(t))
                continue
            if _TIME_RE.match(t):
                continue
            filtered.append(t)
        if not filtered:
            continue

        name = filtered[0]
        preview = filtered[1] if len(filtered) > 1 else ""
        ys = [ln.center[1] for ln in group]
        click_y = int(sum(ys) / len(ys))
        rows.append(
            _SidebarRow(
                name=name,
                preview=preview,
                unread=unread,
                click_y=click_y,
                lines=texts,
            )
        )
    return rows


def _click_sidebar_row(app_name: str, click_y: int) -> tuple[bool, str]:
    result = _run_ps_script(
        _READ_SCRIPT_PATH,
        ["-Action", "click_left", "-ClickY", str(click_y)],
        timeout=20,
    )
    if result.returncode != 0:
        return False, _parse_failure(result, app_name, "切换会话")
    return True, "ok"


def _open_contact(app_name: str, contact: str) -> tuple[bool, str]:
    result = _run_ps_script(
        _READ_SCRIPT_PATH,
        ["-Action", "open_contact", "-ContactB64", _b64(contact)],
        timeout=35,
    )
    if result.returncode != 0:
        return False, _parse_failure(result, app_name, "打开联系人")
    return True, "ok"


def _read_chat_panel(regions: dict) -> tuple[bool, str]:
    chat = regions.get("chat_panel") or {}
    ok, text, _lines = _ocr_panel(chat)
    if not ok:
        return False, text
    cleaned = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    if not cleaned:
        return True, "（未识别到聊天内容，请确认当前会话已打开且窗口可见）"
    return True, cleaned


def _format_read_result(
    *,
    scope: str,
    sessions: list[dict],
) -> str:
    if not sessions:
        if scope == "unread":
            return "当前没有识别到未读微信消息（左侧会话列表无未读数字标记）。"
        return "未能读取到微信消息内容。"

    parts: list[str] = []
    for item in sessions:
        head = f"【{item.get('contact', '未知联系人')}】"
        if item.get("preview"):
            head += f" 预览：{item['preview']}"
        if item.get("unread"):
            head += f" 未读{item['unread']}条"
        parts.append(head)
        parts.append(item.get("messages") or "（无消息文本）")
        parts.append("")
    return "\n".join(parts).strip()


def wechat_read_messages(
    launch_cmd: str | None = None,
    *,
    app_name: str = "",
    scope: str = "unread",
    contact: str | None = None,
    max_chats: int = 3,
) -> tuple[bool, str]:
    """
    读取微信消息（OCR 左栏会话 + 右侧聊天区）。
    scope: unread=未读会话, current=当前会话, contact=指定联系人。
    """
    display = (app_name or "").strip()
    if not display:
        return False, "请指定 app_name（读取消息使用的应用）"

    scope = (scope or "unread").strip().lower()
    if scope not in ("unread", "current", "contact"):
        return False, "scope 只能是 unread、current 或 contact"

    contact = (contact or "").strip()
    if scope == "contact" and not contact:
        return False, "scope=contact 时必须提供 contact"

    max_chats = max(1, min(int(max_chats or 3), 5))

    ok, prep = _ensure_app(display, launch_cmd)
    if not ok:
        return False, prep

    ok, regions_or_msg = _get_panel_regions(display)
    if not ok:
        return False, str(regions_or_msg)
    regions = regions_or_msg  # type: ignore[assignment]

    sessions: list[dict] = []

    if scope == "current":
        ok, chat_text = _read_chat_panel(regions)
        if not ok:
            return False, chat_text
        sessions.append({"contact": "当前会话", "messages": chat_text})
        return True, _format_read_result(scope=scope, sessions=sessions)

    if scope == "contact":
        ok, msg = _open_contact(display, contact)
        if not ok:
            return False, msg
        time.sleep(0.8)
        ok, regions_or_msg = _get_panel_regions(display)
        if not ok:
            return False, str(regions_or_msg)
        regions = regions_or_msg  # type: ignore[assignment]
        ok, chat_text = _read_chat_panel(regions)
        if not ok:
            return False, chat_text
        sessions.append({"contact": contact, "messages": chat_text})
        return True, _format_read_result(scope=scope, sessions=sessions)

    left = regions.get("left_panel") or {}
    ok, _text, lines = _ocr_panel(left)
    if not ok:
        return False, _text

    rows = _group_sidebar_lines(lines, int(left.get("y", 0)))
    unread_rows = [r for r in rows if r.unread > 0]
    if not unread_rows:
        ok, chat_text = _read_chat_panel(regions)
        if ok and chat_text and "未识别" not in chat_text:
            sessions.append({"contact": "当前会话", "messages": chat_text})
            return True, _format_read_result(scope=scope, sessions=sessions)
        return True, _format_read_result(scope=scope, sessions=[])

    for row in unread_rows[:max_chats]:
        ok, msg = _click_sidebar_row(display, row.click_y)
        if not ok:
            logger.warning("切换会话失败 %s: %s", row.name, msg)
            continue
        time.sleep(0.9)
        ok, regions_or_msg = _get_panel_regions(display)
        if not ok:
            continue
        regions = regions_or_msg  # type: ignore[assignment]
        ok, chat_text = _read_chat_panel(regions)
        if not ok:
            continue
        sessions.append(
            {
                "contact": row.name,
                "preview": row.preview,
                "unread": row.unread,
                "messages": chat_text,
            }
        )

    if not sessions:
        return False, "识别到未读标记，但未能读取会话内容，请把微信窗口放大后重试"
    return True, _format_read_result(scope=scope, sessions=sessions)


def wechat_reply_current(
    message: str,
    launch_cmd: str | None = None,
    *,
    app_name: str = "",
) -> tuple[bool, str]:
    """在当前已打开的微信会话中直接回复（不切换联系人）。"""
    message = (message or "").strip()
    display = (app_name or "").strip()
    if not display:
        return False, "请指定 app_name"
    if not message:
        return False, "请告诉我要回复什么内容"

    ok, prep = _ensure_app(display, launch_cmd)
    if not ok:
        return False, prep

    if not _REPLY_SCRIPT_PATH.exists():
        return False, f"缺少脚本 {_REPLY_SCRIPT_PATH.name}"

    saved_clipboard = get_clipboard()
    try:
        result = _run_ps_script(_REPLY_SCRIPT_PATH, [_b64(message)], timeout=30)
        if result.returncode == 0:
            return True, f"已在当前会话回复：{message[:40]}"
        return False, _parse_failure(result, display, "回复")
    except subprocess.TimeoutExpired:
        return False, f"{display} 回复超时，请确认窗口在前台"
    except Exception as e:
        logger.exception("%s 回复异常", display)
        return False, f"{display} 回复异常: {e}"
    finally:
        time.sleep(0.2)
        try:
            set_clipboard(saved_clipboard)
        except Exception:
            pass


def wechat_send_message(
    contact: str,
    message: str,
    launch_cmd: str | None = None,
    *,
    app_name: str = "",
) -> tuple[bool, str]:
    """
    指定应用（通常为微信）内 UI 自动化：聚焦主窗口 → 搜索联系人 → 发送消息。
    auto 模式：坐标（客户区+DPI）优先，失败回退键盘策略；失败时自动重试一次。
    """
    contact = (contact or "").strip()
    message = (message or "").strip()
    display = (app_name or "").strip()
    if not display:
        return False, "请指定发消息使用的应用（app_name）"
    if not contact:
        return False, "请告诉我要找谁"
    if not message:
        return False, "请告诉我要发送什么内容"

    ok, prep = _ensure_app(display, launch_cmd)
    if not ok:
        return False, prep

    if not _SCRIPT_PATH.exists():
        return False, f"缺少脚本 {_SCRIPT_PATH.name}"

    saved_clipboard = get_clipboard()

    try:
        last_err = ""
        for attempt in range(2):
            if attempt > 0:
                logger.info("%s 发送重试第 %d 次", display, attempt + 1)
                focus_app(display, launch_cmd or "")
                time.sleep(0.8)

            result = _run_send_script(contact, message, mode="auto")
            out = (result.stdout or "").strip()
            logger.info("%s 脚本 stdout: %s", display, out[:200])

            if result.returncode == 0:
                return True, f"已向 {contact} 发送：{message[:40]}"

            last_err = _parse_send_failure(result, display)

        return False, last_err
    except subprocess.TimeoutExpired:
        return False, f"{display} 操作超时，请确认窗口在前台且已登录"
    except Exception as e:
        logger.exception("%s 发消息异常", display)
        return False, f"{display} 发消息异常: {e}"
    finally:
        time.sleep(0.2)
        try:
            set_clipboard(saved_clipboard)
        except Exception:
            pass
