"""工具开关策略 — 与 config.yaml tools 段联动"""

from __future__ import annotations

import re

_SHELL_TOOLS = frozenset({"run_shell", "run_shell_admin", "run_shell_system"})

_FILE_TOOLS = frozenset(
    {
        "list_directory",
        "search_files",
        "copy_path",
        "move_path",
        "rename_path",
        "delete_file",
        "move_to_trash",
        "empty_recycle_bin",
        "open_path",
        "create_document",
        "list_desktop_files",
        "find_large_files",
        "list_kekesi_files",
        "wait_for_file",
    }
)

_APP_TOOLS = frozenset(
    {
        "open_app",
        "close_app",
        "open_app_and_type",
        "focus_window",
        "close_window",
        "minimize_window",
        "list_windows",
        "list_open_windows",
        "close_all_apps",
        "wechat_send_message",
        "wechat_read_messages",
        "wechat_reply_current",
        "play_music",
        "control_music",
        "qqmusic_login",
        "qqmusic_status",
        "play_video",
        "close_video",
        "video_login",
        "video_status",
        "mute_app",
        "set_app_volume",
        "list_app_volumes",
    }
)

# 默认不放进候选池，除非用户明确要进程/Shell 操作（避免本地模型误选 kill_process）
_DEFER_BY_DEFAULT = frozenset({
    "kill_process",
    "find_process",
    "list_processes",
    "run_shell",
    "run_shell_admin",
    "run_shell_system",
})

# 触发放行进程类工具的广义词：查询/列出/内存/占用/资源 等
_PROCESS_SEEK_RE = re.compile(
    r"进程|内存|占用|CPU|运行中|资源占用|前几位|top|列(?:(?:出|一下))?",
    re.I,
)


def is_tool_enabled(name: str, tools_cfg: dict | None) -> bool:
    cfg = tools_cfg or {}
    if name in _SHELL_TOOLS:
        return bool(cfg.get("shell_enabled", True))
    if name in _FILE_TOOLS:
        return bool(cfg.get("file_ops_enabled", True))
    if name in _APP_TOOLS:
        return bool(cfg.get("app_control_enabled", True))
    return True


def filter_enabled_tool_names(
    names: list[str], tools_cfg: dict | None
) -> list[str]:
    """按分组开关过滤候选池：被关闭的那组工具直接不进池（模型看不到）。

    与执行期的 is_tool_enabled 同一套判定，避免「模型选了才被拒」。
    """
    if not tools_cfg:
        return names
    return [name for name in names if is_tool_enabled(name, tools_cfg)]


def filter_deferred_tool_names(
    names: list[str],
    user_text: str,
    *,
    explicit: bool | None = None,
) -> list[str]:
    """未明确提及进程/Shell 时，延后高危工具进入候选池。"""
    if explicit is None:
        explicit = bool(
            re.search(
                r"结束进程|杀掉|kill|进程列表|列出进程|运行命令|命令行|shell|终端|提权|管理员运行",
                user_text or "",
                re.I,
            )
        )
    if explicit:
        return names
    # 进程相关工具：查询/列出/内存占用 类意图放行 find/list，仍压制 kill_process
    if _PROCESS_SEEK_RE.search(user_text or ""):
        allowed = [n for n in names if n not in _DEFER_BY_DEFAULT or n in (
            "find_process", "list_processes"
        )]
        # 未明确"结束/杀"时仍不放 kill_process
        if "结束" not in (user_text or "") and "杀" not in (user_text or ""):
            return [n for n in allowed if n != "kill_process"]
        return allowed
    return [n for n in names if n not in _DEFER_BY_DEFAULT]
