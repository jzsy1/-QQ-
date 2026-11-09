# -*- coding: utf-8 -*-
"""Typed Executors 工具参数契约 — 由 _gen_specs.py 从 registry schema 自动生成。

维护说明：增删工具或改 schema 后，重跑 `python -m kekesi.tools._gen_specs`
覆盖本文件；特殊语义（default/max_len）在 _gen_specs.py 的 _TRUE_DEFAULTS /
_LONG_TEXT_MAXLEN 集中维护，勿手改本文件。"""

from __future__ import annotations

from .typed_executors import ParamSpec

TOOL_SPECS: dict[str, list[ParamSpec]] = {
    "autostart_status": [],
    "check_updates": [],
    "clean_memory": [],
    "clean_ram": [],
    "close_video": [],
    "elevate_self": [],
    "empty_recycle_bin": [],
    "get_bluetooth_status": [],
    "get_current_time": [],
    "get_cursor_pos": [],
    "get_firewall_status": [],
    "get_hardware_info": [],
    "get_network_info": [],
    "get_persona": [],
    "get_power_timeouts": [],
    "get_privilege_status": [],
    "get_system_stats": [],
    "get_system_status": [],
    "get_temp_threshold": [],
    "get_temperatures": [],
    "get_update_history": [],
    "get_wallpaper": [],
    "list_app_volumes": [],
    "list_firewall_rules": [],
    "list_kekesi_files": [],
    "list_monitors": [],
    "list_ocr_languages": [],
    "list_personas": [],
    "list_power_plans": [],
    "list_tasks": [],
    "list_watch_rules": [],
    "list_wifi_networks": [],
    "list_wifi_profiles": [],
    "list_workflows": [],
    "qqmusic_status": [],
    "read_clipboard": [],
    "read_screen": [],
    "repair_system": [],
    "show_desktop": [],
    "uninstall_autostart": [],
    "video_status": [],
    "search_tools": [
        ParamSpec("query", "string", required=True, max_len=200),
    ],
    "open_app": [
        ParamSpec("app_name", "string", required=True, max_len=200),
        ParamSpec("force_new", "boolean", required=False, default=False),
    ],
    "wechat_send_message": [
        ParamSpec("app_name", "string", required=True, max_len=200),
        ParamSpec("contact", "string", required=True, max_len=200),
        ParamSpec("message", "string", required=True, max_len=2000),
    ],
    "wechat_read_messages": [
        ParamSpec("app_name", "string", required=True, max_len=200),
        ParamSpec("scope", "string", required=False, max_len=200, enum=("unread", "current", "contact")),
        ParamSpec("contact", "string", required=False, max_len=200),
        ParamSpec("max_chats", "integer", required=False),
    ],
    "wechat_reply_current": [
        ParamSpec("app_name", "string", required=True, max_len=200),
        ParamSpec("message", "string", required=True, max_len=2000),
    ],
    "open_app_and_type": [
        ParamSpec("app_name", "string", required=True, max_len=200),
        ParamSpec("text", "string", required=True, max_len=2000),
        ParamSpec("press_enter", "boolean", required=False, default=False),
    ],
    "search_web": [
        ParamSpec("query", "string", required=True, max_len=200),
        ParamSpec("max_results", "integer", required=False),
    ],
    "fetch_web_page": [
        ParamSpec("url", "string", required=True, max_len=200),
        ParamSpec("start_index", "integer", required=False),
        ParamSpec("max_length", "integer", required=False),
    ],
    "web_search": [
        ParamSpec("query", "string", required=True, max_len=200),
    ],
    "self_study": [
        ParamSpec("topics", "string", required=False, max_len=200),
        ParamSpec("action", "string", required=False, max_len=40, enum=("run", "list_candidates", "inspect_candidate", "approve_candidate", "reject_candidate")),
        ParamSpec("candidate_ref", "string", required=False, max_len=120),
        ParamSpec("approved", "boolean", required=False, default=False),
        ParamSpec("reason", "string", required=False, max_len=200),
    ],
    "type_text": [
        ParamSpec("text", "string", required=True, max_len=2000),
    ],
    "search_files": [
        ParamSpec("keyword", "string", required=True, max_len=200),
        ParamSpec("search_dir", "string", required=False, max_len=200),
    ],
    "create_document": [
        ParamSpec("content", "string", required=True),
        ParamSpec("title", "string", required=False, max_len=200),
        ParamSpec("filename", "string", required=False, max_len=200),
        ParamSpec("folder", "string", required=False, max_len=200),
        ParamSpec("format", "string", required=False, max_len=200, enum=("docx", "txt", "md")),
    ],
    "run_shell": [
        ParamSpec("command", "string", required=True, max_len=1000),
        ParamSpec("shell", "string", required=False, max_len=200, enum=("powershell", "cmd")),
        ParamSpec("admin", "boolean", required=False, default=False),
    ],
    "remember_app": [
        ParamSpec("app_name", "string", required=True, max_len=200),
        ParamSpec("command", "string", required=True, max_len=200),
    ],
    "remember_fact": [
        ParamSpec("key", "string", required=True, max_len=200),
        ParamSpec("value", "string", required=True, max_len=200),
    ],
    "search_memory": [
        ParamSpec("query", "string", required=True, max_len=200),
        ParamSpec("limit", "integer", required=False),
    ],
    "cleanup_memory": [
        ParamSpec("digest_keep", "integer", required=False),
        ParamSpec("task_keep", "integer", required=False),
        ParamSpec("prune_stale_prefs", "boolean", required=False, default=False),
    ],
    "open_url": [
        ParamSpec("url", "string", required=True, max_len=200),
    ],
    "get_weather": [
        ParamSpec("city", "string", required=True, max_len=200),
    ],
    "set_volume": [
        ParamSpec("level", "integer", required=True),
    ],
    "set_app_volume": [
        ParamSpec("app", "string", required=True, max_len=200),
        ParamSpec("level", "integer", required=True),
    ],
    "mute_app": [
        ParamSpec("app", "string", required=True, max_len=200),
        ParamSpec("muted", "boolean", required=False, default=False),
    ],
    "get_brightness": [
        ParamSpec("monitor", "string", required=False, max_len=200),
    ],
    "set_brightness": [
        ParamSpec("level", "integer", required=True),
        ParamSpec("monitor", "string", required=False, max_len=200),
    ],
    "set_wallpaper": [
        ParamSpec("image_path", "string", required=True, max_len=200),
        ParamSpec("style", "string", required=False, max_len=200, enum=("填充", "适应", "拉伸", "居中", "跨屏")),
    ],
    "set_power_plan": [
        ParamSpec("plan", "string", required=True, max_len=200),
    ],
    "set_power_timeout": [
        ParamSpec("kind", "string", required=True, max_len=200, enum=("sleep", "monitor")),
        ParamSpec("minutes", "integer", required=True),
        ParamSpec("on_battery", "boolean", required=False, default=False),
    ],
    "install_updates": [
        ParamSpec("update_filter", "string", required=False, max_len=200),
    ],
    "block_app_network": [
        ParamSpec("app", "string", required=True, max_len=200),
        ParamSpec("direction", "string", required=False, max_len=200, enum=("out", "in", "both")),
    ],
    "allow_app_network": [
        ParamSpec("app", "string", required=True, max_len=200),
    ],
    "play_music": [
        ParamSpec("query", "string", required=False, max_len=200),
        ParamSpec("playlist", "string", required=False, max_len=200, enum=("liked",)),
        ParamSpec("source", "string", required=False, max_len=200, enum=("qq", "netease", "kugou")),
        ParamSpec("show", "boolean", required=False, default=False),
    ],
    "qqmusic_login": [
        ParamSpec("source", "string", required=False, max_len=200, enum=("qq", "netease", "kugou")),
    ],
    "control_music": [
        ParamSpec("action", "string", required=True, max_len=200, enum=("play", "pause", "playpause", "next", "previous")),
    ],
    "play_video": [
        ParamSpec("query", "string", required=True, max_len=200),
        ParamSpec("episode", "integer", required=False),
        ParamSpec("latest", "boolean", required=False, default=False),
        ParamSpec("source", "string", required=False, max_len=200, enum=("tencent", "bilibili", "iqiyi")),
    ],
    "video_login": [
        ParamSpec("source", "string", required=False, max_len=200, enum=("tencent", "bilibili", "iqiyi")),
    ],
    "set_timer": [
        ParamSpec("seconds", "integer", required=True),
        ParamSpec("message", "string", required=False, max_len=200),
    ],
    "take_screenshot": [
        ParamSpec("title", "string", required=False, max_len=200),
        ParamSpec("hwnd", "integer", required=False),
    ],
    "system_power": [
        ParamSpec("action", "string", required=True, max_len=200, enum=("lock", "sleep", "logoff", "shutdown", "restart")),
    ],
    "list_directory": [
        ParamSpec("path", "string", required=True, max_len=200),
    ],
    "move_to_trash": [
        ParamSpec("path", "string", required=True, max_len=200),
    ],
    "delete_file": [
        ParamSpec("path", "string", required=True, max_len=200),
    ],
    "remember": [
        ParamSpec("fact", "string", required=False, max_len=500),
        ParamSpec("key", "string", required=False, max_len=200),
        ParamSpec("value", "string", required=False, max_len=200),
    ],
    "forget": [
        ParamSpec("keyword", "string", required=True, max_len=200),
    ],
    "recall_screenshot": [
        ParamSpec("screenshot_id", "string", required=True, max_len=200),
    ],
    "list_screenshots": [
        ParamSpec("limit", "integer", required=False),
    ],
    "list_windows": [
        ParamSpec("limit", "integer", required=False),
    ],
    "list_open_windows": [
        ParamSpec("limit", "integer", required=False),
    ],
    "list_desktop_files": [
        ParamSpec("limit", "integer", required=False),
        ParamSpec("extensions", "string", required=False, max_len=200),
        ParamSpec("name_contains", "string", required=False, max_len=200),
    ],
    "list_desktop_icons": [
        ParamSpec("limit", "integer", required=False),
    ],
    "click_desktop_icon": [
        ParamSpec("name", "string", required=True, max_len=200),
        ParamSpec("double", "boolean", required=False, default=True),
        ParamSpec("show_first", "boolean", required=False, default=False),
    ],
    "open_path": [
        ParamSpec("path", "string", required=True, max_len=200),
    ],
    "focus_window": [
        ParamSpec("title", "string", required=True, max_len=200),
    ],
    "close_app": [
        ParamSpec("app_name", "string", required=True, max_len=200),
        ParamSpec("force", "boolean", required=False, default=True),
    ],
    "close_window": [
        ParamSpec("title", "string", required=True, max_len=200),
    ],
    "close_all_apps": [
        ParamSpec("force", "boolean", required=False, default=False),
        ParamSpec("keep_title", "string", required=False, max_len=200),
    ],
    "minimize_window": [
        ParamSpec("title", "string", required=True, max_len=200),
    ],
    "list_processes": [
        ParamSpec("limit", "integer", required=False),
    ],
    "find_process": [
        ParamSpec("keyword", "string", required=True, max_len=200),
        ParamSpec("limit", "integer", required=False),
    ],
    "kill_process": [
        ParamSpec("name_or_pid", "string", required=True, max_len=200),
        ParamSpec("force", "boolean", required=False, default=True),
    ],
    "write_clipboard": [
        ParamSpec("text", "string", required=True, max_len=200),
    ],
    "set_temp_threshold": [
        ParamSpec("cpu", "number", required=False),
        ParamSpec("gpu", "number", required=False),
        ParamSpec("ssd", "number", required=False),
        ParamSpec("other", "number", required=False),
        ParamSpec("alert_enabled", "boolean", required=False, default=False),
        ParamSpec("interval_sec", "integer", required=False),
    ],
    "setup_temp_sensors": [
        ParamSpec("download", "boolean", required=False, default=False),
    ],
    "set_reminder": [
        ParamSpec("title", "string", required=False, max_len=200),
        ParamSpec("time_expr", "string", required=False, max_len=200),
        ParamSpec("seconds", "integer", required=False),
        ParamSpec("message", "string", required=False, max_len=200),
        ParamSpec("description", "string", required=False, max_len=500),
        ParamSpec("action_func", "string", required=False, max_len=200),
    ],
    "schedule_action": [
        ParamSpec("title", "string", required=True, max_len=200),
        ParamSpec("time_expr", "string", required=True, max_len=200),
        ParamSpec("action_func", "string", required=True, max_len=200),
        ParamSpec("description", "string", required=False, max_len=500),
    ],
    "add_watch_rule": [
        ParamSpec("condition_type", "string", required=True, max_len=200, enum=("idle_minutes", "cpu_above", "memory_above", "disk_free_below_gb", "cpu_temp_above", "gpu_temp_above", "temp_above")),
        ParamSpec("threshold", "number", required=True),
        ParamSpec("question", "string", required=True, max_len=500),
        ParamSpec("action_func", "string", required=False, max_len=200),
        ParamSpec("timeout_sec", "integer", required=False),
        ParamSpec("description", "string", required=False, max_len=500),
    ],
    "delete_watch_rule": [
        ParamSpec("rule_id", "integer", required=True),
    ],
    "complete_task": [
        ParamSpec("task_id", "integer", required=True),
    ],
    "delete_task": [
        ParamSpec("task_id", "integer", required=True),
    ],
    "detect_emotion": [
        ParamSpec("text", "string", required=True, max_len=2000),
    ],
    "send_keys": [
        ParamSpec("keys", "string", required=True, max_len=200),
    ],
    "copy_path": [
        ParamSpec("source", "string", required=True, max_len=200),
        ParamSpec("destination", "string", required=True, max_len=200),
    ],
    "move_path": [
        ParamSpec("source", "string", required=True, max_len=200),
        ParamSpec("destination", "string", required=True, max_len=200),
    ],
    "rename_path": [
        ParamSpec("path", "string", required=True, max_len=200),
        ParamSpec("new_name", "string", required=True, max_len=200),
    ],
    "read_registry": [
        ParamSpec("path", "string", required=True, max_len=200),
        ParamSpec("name", "string", required=False, max_len=200),
    ],
    "write_registry": [
        ParamSpec("path", "string", required=True, max_len=200),
        ParamSpec("name", "string", required=True, max_len=200),
        ParamSpec("value", "string", required=True, max_len=200),
        ParamSpec("value_type", "string", required=False, max_len=200, enum=("String", "ExpandString", "DWord", "QWord", "MultiString")),
    ],
    "list_services": [
        ParamSpec("status", "string", required=False, max_len=200, enum=("all", "running", "stopped")),
        ParamSpec("limit", "integer", required=False),
    ],
    "control_service": [
        ParamSpec("name", "string", required=True, max_len=200),
        ParamSpec("action", "string", required=True, max_len=200, enum=("start", "stop", "restart")),
    ],
    "list_scheduled_tasks": [
        ParamSpec("limit", "integer", required=False),
    ],
    "run_scheduled_task": [
        ParamSpec("task_name", "string", required=True, max_len=200),
    ],
    "read_env_var": [
        ParamSpec("name", "string", required=True, max_len=200),
    ],
    "set_env_var": [
        ParamSpec("name", "string", required=True, max_len=200),
        ParamSpec("value", "string", required=True, max_len=200),
    ],
    "list_env_vars": [
        ParamSpec("prefix", "string", required=False, max_len=200),
        ParamSpec("limit", "integer", required=False),
    ],
    "list_installed_software": [
        ParamSpec("query", "string", required=False, max_len=200),
        ParamSpec("limit", "integer", required=False),
    ],
    "install_software": [
        ParamSpec("package", "string", required=True, max_len=200),
        ParamSpec("silent", "boolean", required=False, default=False),
    ],
    "uninstall_software": [
        ParamSpec("package", "string", required=True, max_len=200),
        ParamSpec("silent", "boolean", required=False, default=False),
    ],
    "set_dns": [
        ParamSpec("interface_alias", "string", required=True, max_len=200),
        ParamSpec("primary", "string", required=True, max_len=200),
        ParamSpec("secondary", "string", required=False, max_len=200),
    ],
    "connect_wifi": [
        ParamSpec("ssid", "string", required=True, max_len=200),
        ParamSpec("password", "string", required=False, max_len=200),
    ],
    "set_bluetooth": [
        ParamSpec("on", "boolean", required=True),
    ],
    "run_shell_admin": [
        ParamSpec("command", "string", required=True, max_len=1000),
    ],
    "run_shell_system": [
        ParamSpec("command", "string", required=True, max_len=1000),
    ],
    "install_autostart": [
        ParamSpec("scope", "string", required=False, max_len=200, enum=("admin", "system")),
    ],
    "install_system_service": [
        ParamSpec("service_name", "string", required=False, max_len=200),
    ],
    "uninstall_system_service": [
        ParamSpec("service_name", "string", required=False, max_len=200),
    ],
    "take_ownership": [
        ParamSpec("path", "string", required=True, max_len=200),
        ParamSpec("recurse", "boolean", required=False, default=True),
    ],
    "grant_full_control": [
        ParamSpec("path", "string", required=True, max_len=200),
        ParamSpec("account", "string", required=False, max_len=200),
        ParamSpec("recurse", "boolean", required=False, default=True),
    ],
    "check_system_health": [
        ParamSpec("auto_fix", "boolean", required=False, default=False),
    ],
    "clean_disk": [
        ParamSpec("aggressive", "boolean", required=False, default=False),
    ],
    "find_large_files": [
        ParamSpec("drive", "string", required=False, max_len=200),
        ParamSpec("min_mb", "integer", required=False),
        ParamSpec("limit", "integer", required=False),
        ParamSpec("junk_only", "boolean", required=False, default=False),
    ],
    "kill_memory_hogs": [
        ParamSpec("threshold_mb", "integer", required=False),
    ],
    "mouse_move": [
        ParamSpec("x", "integer", required=True),
        ParamSpec("y", "integer", required=True),
        ParamSpec("smooth", "boolean", required=False, default=False),
    ],
    "mouse_click": [
        ParamSpec("x", "integer", required=False),
        ParamSpec("y", "integer", required=False),
        ParamSpec("button", "string", required=False, max_len=200),
        ParamSpec("double", "boolean", required=False, default=False),
        ParamSpec("virtual", "boolean", required=False, default=False),
    ],
    "mouse_drag": [
        ParamSpec("x1", "integer", required=True),
        ParamSpec("y1", "integer", required=True),
        ParamSpec("x2", "integer", required=True),
        ParamSpec("y2", "integer", required=True),
        ParamSpec("duration", "number", required=False),
    ],
    "mouse_scroll": [
        ParamSpec("amount", "integer", required=False),
        ParamSpec("x", "integer", required=False),
        ParamSpec("y", "integer", required=False),
        ParamSpec("virtual", "boolean", required=False, default=False),
    ],
    "uia_find_element": [
        ParamSpec("name", "string", required=False, max_len=200),
        ParamSpec("automation_id", "string", required=False, max_len=200),
        ParamSpec("control_type", "string", required=False, max_len=200),
        ParamSpec("class_name", "string", required=False, max_len=200),
        ParamSpec("app", "string", required=False, max_len=200),
        ParamSpec("scope", "string", required=False, max_len=200),
        ParamSpec("limit", "integer", required=False),
    ],
    "uia_click_element": [
        ParamSpec("name", "string", required=False, max_len=200),
        ParamSpec("automation_id", "string", required=False, max_len=200),
        ParamSpec("control_type", "string", required=False, max_len=200),
        ParamSpec("class_name", "string", required=False, max_len=200),
        ParamSpec("app", "string", required=False, max_len=200),
        ParamSpec("button", "string", required=False, max_len=200),
        ParamSpec("double", "boolean", required=False, default=False),
    ],
    "uia_get_text": [
        ParamSpec("name", "string", required=False, max_len=200),
        ParamSpec("automation_id", "string", required=False, max_len=200),
        ParamSpec("control_type", "string", required=False, max_len=200),
        ParamSpec("app", "string", required=False, max_len=200),
    ],
    "uia_list_controls": [
        ParamSpec("app", "string", required=False, max_len=200),
        ParamSpec("scope", "string", required=False, max_len=200),
        ParamSpec("control_type", "string", required=False, max_len=200),
        ParamSpec("limit", "integer", required=False),
    ],
    "uia_set_value": [
        ParamSpec("value", "string", required=True, max_len=200),
        ParamSpec("name", "string", required=False, max_len=200),
        ParamSpec("automation_id", "string", required=False, max_len=200),
        ParamSpec("control_type", "string", required=False, max_len=200),
        ParamSpec("app", "string", required=False, max_len=200),
    ],
    "ocr_screen": [
        ParamSpec("language", "string", required=False, max_len=200),
    ],
    "ocr_find_text": [
        ParamSpec("text", "string", required=True, max_len=200),
        ParamSpec("language", "string", required=False, max_len=200),
        ParamSpec("partial", "boolean", required=False, default=True),
    ],
    "click_text": [
        ParamSpec("text", "string", required=True, max_len=200),
        ParamSpec("app", "string", required=False, max_len=200),
        ParamSpec("button", "string", required=False, max_len=200),
        ParamSpec("double", "boolean", required=False, default=False),
        ParamSpec("partial", "boolean", required=False, default=True),
    ],
    "gui_action": [
        ParamSpec("target", "string", required=True, max_len=200),
        ParamSpec("action", "string", required=False, max_len=200, enum=("click", "double_click", "hover")),
        ParamSpec("app", "string", required=False, max_len=200),
        ParamSpec("button", "string", required=False, max_len=200),
        ParamSpec("double", "boolean", required=False, default=False),
        ParamSpec("partial", "boolean", required=False, default=True),
    ],
    "run_workflow": [
        ParamSpec("name", "string", required=True, max_len=200),
    ],
    "save_workflow": [
        ParamSpec("name", "string", required=True, max_len=200),
        ParamSpec("title", "string", required=False, max_len=200),
        ParamSpec("trigger_hint", "string", required=False, max_len=500),
    ],
    "run_sequence": [
        ParamSpec("stop_on_error", "boolean", required=False, default=False),
    ],
    "check_occlusion": [
        ParamSpec("x", "integer", required=False),
        ParamSpec("y", "integer", required=False),
        ParamSpec("title", "string", required=False, max_len=200),
        ParamSpec("hwnd", "integer", required=False),
    ],
    "virtual_click": [
        ParamSpec("x", "integer", required=True),
        ParamSpec("y", "integer", required=True),
        ParamSpec("button", "string", required=False, max_len=200),
        ParamSpec("double", "boolean", required=False, default=False),
    ],
    "virtual_scroll": [
        ParamSpec("amount", "integer", required=False),
        ParamSpec("x", "integer", required=False),
        ParamSpec("y", "integer", required=False),
    ],
    "scan_desktop": [
        ParamSpec("limit", "integer", required=False),
        ParamSpec("include_controls", "boolean", required=False, default=True),
        ParamSpec("include_regions", "boolean", required=False, default=True),
    ],
    "screenshot_window": [
        ParamSpec("title", "string", required=False, max_len=200),
        ParamSpec("hwnd", "integer", required=False),
    ],
    "ocr_window": [
        ParamSpec("title", "string", required=False, max_len=200),
        ParamSpec("hwnd", "integer", required=False),
        ParamSpec("language", "string", required=False, max_len=200),
        ParamSpec("dark_enhance", "boolean", required=False, default=True),
    ],
    "wait_until_found": [
        ParamSpec("text", "string", required=False, max_len=200),
        ParamSpec("control_type", "string", required=False, max_len=200),
        ParamSpec("app", "string", required=False, max_len=200),
        ParamSpec("timeout", "number", required=False),
        ParamSpec("partial", "boolean", required=False, default=True),
    ],
    "wait_until_gone": [
        ParamSpec("text", "string", required=True, max_len=200),
        ParamSpec("app", "string", required=False, max_len=200),
        ParamSpec("timeout", "number", required=False),
    ],
    "wait_for_stable_screen": [
        ParamSpec("timeout", "number", required=False),
        ParamSpec("stable_for", "number", required=False),
    ],
    "wait_for_file": [
        ParamSpec("path", "string", required=True, max_len=200),
        ParamSpec("timeout", "number", required=False),
        ParamSpec("stable_for", "number", required=False),
    ],
    "register_image": [
        ParamSpec("name", "string", required=True, max_len=200),
        ParamSpec("path", "string", required=True, max_len=200),
    ],
    "find_image": [
        ParamSpec("image", "string", required=True, max_len=200),
        ParamSpec("confidence", "number", required=False),
    ],
    "click_image": [
        ParamSpec("image", "string", required=True, max_len=200),
        ParamSpec("confidence", "number", required=False),
        ParamSpec("button", "string", required=False, max_len=200),
        ParamSpec("double", "boolean", required=False, default=False),
        ParamSpec("virtual", "boolean", required=False, default=False),
    ],
    "click_and_type": [
        ParamSpec("text", "string", required=False, max_len=200),
        ParamSpec("target", "string", required=False, max_len=200),
        ParamSpec("x", "integer", required=False),
        ParamSpec("y", "integer", required=False),
        ParamSpec("app", "string", required=False, max_len=200),
        ParamSpec("clear_first", "boolean", required=False, default=False),
        ParamSpec("virtual", "boolean", required=False, default=False),
    ],
    "focus_and_hotkey": [
        ParamSpec("keys", "string", required=True, max_len=200),
        ParamSpec("target", "string", required=False, max_len=200),
        ParamSpec("x", "integer", required=False),
        ParamSpec("y", "integer", required=False),
        ParamSpec("app", "string", required=False, max_len=200),
    ],
    "fill_form": [
        ParamSpec("app", "string", required=False, max_len=200),
        ParamSpec("submit", "string", required=False, max_len=200),
    ],
    "click_menu_item": [
        ParamSpec("path", "string", required=True, max_len=200),
        ParamSpec("app", "string", required=False, max_len=200),
        ParamSpec("separator", "string", required=False, max_len=200),
    ],
    "find_text_location": [
        ParamSpec("text", "string", required=True, max_len=200),
        ParamSpec("app", "string", required=False, max_len=200),
        ParamSpec("partial", "boolean", required=False, default=True),
    ],
    "read_screen_text": [
        ParamSpec("app", "string", required=False, max_len=200),
    ],
    "click_at": [
        ParamSpec("x", "integer", required=True),
        ParamSpec("y", "integer", required=True),
        ParamSpec("button", "string", required=False, max_len=200),
        ParamSpec("double", "boolean", required=False, default=False),
        ParamSpec("virtual", "boolean", required=False, default=False),
    ],
    "hover_text": [
        ParamSpec("text", "string", required=True, max_len=200),
        ParamSpec("app", "string", required=False, max_len=200),
        ParamSpec("partial", "boolean", required=False, default=True),
    ],
    "set_persona": [
        ParamSpec("persona", "string", required=True, max_len=200, enum=("humorous", "serious", "playful", "neutral")),
    ],
    "read_code_file": [
        ParamSpec("path", "string", required=True, max_len=200),
        ParamSpec("start_line", "integer", required=False),
        ParamSpec("end_line", "integer", required=False),
    ],
    "write_code_file": [
        ParamSpec("path", "string", required=True, max_len=200),
        ParamSpec("content", "string", required=True),
        ParamSpec("create", "boolean", required=False, default=False),
    ],
    "reload_module": [
        ParamSpec("module_name", "string", required=True, max_len=200),
    ],
    "find_tool_definition": [
        ParamSpec("func_name", "string", required=True, max_len=200),
    ],
}
