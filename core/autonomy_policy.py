"""Action-level risk classification for background autonomous tasks."""

from __future__ import annotations

from typing import Any

_R0_READ_ONLY_TOOLS = frozenset({
    "search_tools",
    "get_current_time",
    "get_network_info",
    "list_wifi_networks",
    "list_processes",
    "find_process",
    "list_windows",
    "list_services",
})

_R1_REVERSIBLE_TOOLS = frozenset({
    "open_app",
    "close_app",
    "focus_window",
    "minimize_window",
    "maximize_window",
    "set_volume",
})

_R3_DESTRUCTIVE_TOOLS = frozenset({
    "delete_file",
    "move_to_trash",
    "empty_recycle_bin",
    "uninstall_software",
    "kill_process",
    "system_power",
    "elevate_self",
    "run_shell",
})

_R2_SIDE_EFFECT_TOOLS = frozenset({
    "write_file",
    "copy_file",
    "move_file",
    "rename_file",
    "create_directory",
    "write_registry",
    "control_service",
    "send_message",
    "send_email",
    "post_social_media",
    "play_music",
    "play_video",
    "search_web",
})


def classify_autonomy_action(tool_name: str, args: dict[str, Any] | None = None) -> str:
    """Classify from the concrete tool and arguments; unknown tools fail closed."""
    name = str(tool_name or "").strip()
    params = args if isinstance(args, dict) else {}
    if name in _R0_READ_ONLY_TOOLS:
        return "R0"
    if name in _R3_DESTRUCTIVE_TOOLS:
        return "R3"
    if name in _R1_REVERSIBLE_TOOLS:
        # Treat malformed, broad or potentially destructive R1 requests as R2.
        # These actions still require exact one-use approval in the executor.
        if name == "close_app" and params.get("force") is not False:
            return "R2"
        if name == "open_app":
            app_name = params.get("app_name")
            if not isinstance(app_name, str) or not app_name.strip() or len(app_name) > 200:
                return "R2"
            if params.get("force_new", False) is not False:
                return "R2"
        if name in {"focus_window", "minimize_window", "maximize_window"}:
            title = params.get("title")
            if not isinstance(title, str) or not title.strip() or len(title) > 200:
                return "R2"
        if name == "set_volume":
            level = params.get("level")
            if isinstance(level, bool) or not isinstance(level, int) or not 0 <= level <= 100:
                return "R2"
        return "R1"
    if name in _R2_SIDE_EFFECT_TOOLS or name.startswith(("mcp__", "skill__")):
        return "R2"
    return "R2"


def autonomy_action_decision(tool_name: str, args: dict[str, Any] | None = None) -> tuple[bool, str, str]:
    risk = classify_autonomy_action(tool_name, args)
    if risk == "R0":
        return True, risk, ""
    return False, risk, f"自主执行通道仅允许 R0 只读探测；{tool_name} 被判定为 {risk}，当前已阻止。"
