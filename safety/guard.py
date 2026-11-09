"""工具安全闸门 — 破坏性操作须明确确认词；叠加 shell / 键鼠守卫。"""

from __future__ import annotations

import logging
import re

from .activity_guard import check_user_idle, mark_agent_control
from .denylist import (
    deny_sensitive_path_tool,
    scan_denylist,
)
from .shell_guard import check_shell_command

logger = logging.getLogger(__name__)

# 看剧/听歌默认走体内网页播放器，禁止误开桌面客户端（除非用户点名客户端/桌面版）
_EXPLICIT_DESKTOP_CLIENT_RE = re.compile(
    r"(?:打开|启动|运行).{0,6}(?:桌面版|客户端|软件|应用)|"
    r"桌面(?:版|客户端)|客户端打开|用客户端",
    re.I,
)
_VIDEO_DESKTOP_APP_RE = re.compile(
    r"腾讯视频|爱奇艺|优酷|芒果(?:TV|tv)?|哔哩哔哩|bilibili|B\s*站|iqiyi|腾讯视屏",
    re.I,
)
_MUSIC_DESKTOP_APP_RE = re.compile(
    r"QQ音乐|网易云(?:音乐)?|酷狗(?:音乐)?|酷我(?:音乐)?",
    re.I,
)

# 禁止单独用「确认」「可以」——易误放行
_CONFIRM_MARKERS = frozenset({
    "确认执行",
    "我确认执行",
    "我确认",
    "确定执行",
    "执行吧",
    "就这样执行",
    "好的执行",
})

_S3_FUNCS = frozenset({
    "system_power",
    "delete_file",
    "empty_recycle_bin",
    "kill_process",
    "write_registry",
    "uninstall_software",
    "move_to_trash",
})

# shell 类工具的确认与否交给 shell_guard 按命令内容判定
_SHELL_FUNCS = frozenset({"run_shell", "run_shell_admin", "run_shell_system"})

_S3_POWER_ACTIONS = frozenset({"shutdown", "restart", "logoff"})

_GUI_TOOLS = frozenset({
    "gui_action",
    "click_text",
    "click_at",
    "hover_text",
    "mouse_move",
    "mouse_click",
    "mouse_drag",
    "mouse_scroll",
    "type_text",
    "send_keys",
    "uia_click_element",
    "uia_set_value",
    "virtual_click",
    "virtual_scroll",
    "run_sequence",
    "click_image",
    "click_and_type",
    "focus_and_hotkey",
    "fill_form",
    "click_menu_item",
})


def needs_confirmation(func: str, args: dict | None = None) -> bool:
    fn = (func or "").strip()
    params = args or {}
    if fn == "system_power":
        action = str(params.get("action") or "").strip().lower()
        return action in _S3_POWER_ACTIONS
    if fn == "close_all_apps":
        return True
    if fn in _SHELL_FUNCS:
        cmd = str(params.get("command") or "")
        _allowed, _reason, needs = check_shell_command(cmd)
        return needs
    if fn in _S3_FUNCS:
        return True
    # config/permissions.yaml 里 confirmation.always_confirm 显式列出的工具，
    # 原先只影响审计记录（router._build_execute_fn 里的 permission_level），
    # 交互确认闸门读不到；这里补上，让该配置真正生效。
    # 注意：只接入 always_confirm 这个"明确要求确认"的清单，不接入整套
    # risk_level >= confirmation_min_level 的判断——后者会把 write_file /
    # type_text / click 等大量默认免确认的高频操作也变成需要确认，
    # 和 _GUI_TOOLS / _READONLY 类设计的"降低日常啰嗦度"初衷冲突，
    # 属于更大的行为变更，不在这次修复范围内，需要你单独确认后再接。
    try:
        from ..security.policy import load_policy_config

        if fn in load_policy_config().always_confirm:
            return True
    except Exception:
        logger.debug("读取 security.policy 的 always_confirm 配置失败，跳过", exc_info=True)
    return False


def _user_confirmed(session_context: str | None) -> bool:
    t = (session_context or "").strip()
    if not t:
        return False
    return any(m in t for m in _CONFIRM_MARKERS)


def _block_desktop_media_client(
    func: str, params: dict, user_text: str
) -> str | None:
    """看剧/听歌误调 open_app 打开桌面客户端时拦截，引导 play_video / play_music。"""
    fn = (func or "").strip()
    if fn not in ("open_app", "open_app_and_type"):
        return None
    if _EXPLICIT_DESKTOP_CLIENT_RE.search(user_text or ""):
        return None
    app = str(params.get("app_name") or params.get("name") or "").strip()
    if not app:
        return None
    if _VIDEO_DESKTOP_APP_RE.search(app):
        return (
            "看剧请用 play_video（科科斯视频空间网页播放），"
            "不要 open_app 打开腾讯视频/爱奇艺/B站桌面客户端。"
            "用户说「最新一集」时设 latest=true；明确集数传 episode。"
            "仅当用户明确说打开桌面客户端/软件时才可用 open_app。"
        )
    if _MUSIC_DESKTOP_APP_RE.search(app):
        return (
            "听歌请用 play_music（体内网页播放器），"
            "不要 open_app 打开 QQ/网易云/酷狗桌面客户端。"
            "仅当用户明确说打开桌面客户端/软件时才可用 open_app。"
        )
    return None


def check_tool_call(
    func: str,
    args: dict | None = None,
    *,
    user_text: str = "",
    recent_assistant: str = "",
    pre_authorized: bool = False,
    pre_authorized_reason: str = "",
) -> tuple[bool, str]:
    """返回 (允许执行, 拒绝原因或空)。

    pre_authorized: 供定时任务 / 哨兵监控规则 / 事件规则等后台无人值守通道使用——
    这些动作的"授权"发生在用户创建规则的那一刻，而不是本次执行的这一刻，
    因此不应该也无法要求用户在执行瞬间说"确认执行"。用这个显式参数标记，
    而不是像以前那样把"确认执行"几个字硬拼进 user_text 去匹配 _CONFIRM_MARKERS——
    那样做会让确认闸门对交互式确认和"规则创建时预授权"这两件语义不同的事
    做出完全相同的判断，且任何调用方不小心把这句话拼进普通文本里都会被误放行。
    denylist / 路径保护 / 空闲检测等硬性拦截对预授权调用同样生效，不受影响。
    """
    fn = (func or "").strip()
    params = args or {}

    # 通用危险指令黑名单（denylist）：用户原文危险意图 → 直接拒绝
    deny_reason = scan_denylist(user_text)
    if deny_reason:
        return False, f"安全策略拦截：{deny_reason}"

    # 敏感文件工具 path 参数指向系统关键目录 → 拒绝
    deny_path = deny_sensitive_path_tool(fn, params)
    if deny_path:
        return False, f"安全策略拦截：{deny_path}"

    idle_ok, idle_reason = check_user_idle(fn)
    if not idle_ok:
        return False, idle_reason

    media_block = _block_desktop_media_client(fn, params, user_text)
    if media_block:
        return False, media_block

    if fn in _SHELL_FUNCS:
        cmd = str(params.get("command") or "")
        allowed, reason, needs_confirm = check_shell_command(cmd)
        if not allowed:
            return False, reason
        if needs_confirm and not (
            pre_authorized
            or _user_confirmed(user_text)
            or _user_confirmed(recent_assistant)
        ):
            return (
                False,
                f"该 Shell 命令较危险（{reason}），请说「确认执行」后再试。",
            )
        if needs_confirm and pre_authorized:
            logger.info(
                "Shell 命令 %r 经预授权放行（%s），非实时用户确认",
                cmd[:80], pre_authorized_reason or "后台通道",
            )
        return True, ""

    if not needs_confirmation(fn, params):
        if fn in _GUI_TOOLS:
            mark_agent_control()
        return True, ""

    if pre_authorized:
        if fn in _GUI_TOOLS:
            mark_agent_control()
        logger.info(
            "工具 %s 经预授权放行（%s），非实时用户确认",
            fn, pre_authorized_reason or "后台通道",
        )
        return True, ""

    if _user_confirmed(user_text) or _user_confirmed(recent_assistant):
        if fn in _GUI_TOOLS:
            mark_agent_control()
        return True, ""

    if fn == "system_power":
        action = str(params.get("action") or "").strip()
        return (
            False,
            f"关机/重启/注销需您明确说「确认执行」后再执行（当前请求：{action}）。",
        )
    if fn == "kill_process":
        return False, "结束进程可能影响系统，请说「确认执行」后再执行。"
    if fn in ("delete_file", "move_to_trash", "empty_recycle_bin"):
        return False, "删除或清空文件需您说「确认执行」后再执行。"
    if fn == "close_all_apps":
        return False, "关闭所有应用影响较大，请说「确认执行」后再执行。"
    return False, "该操作需要您说「确认执行」后再执行。"
