"""PermissionManager — Windows 操作权限等级。"""

from __future__ import annotations

# 0 查询 | 1 应用控制 | 2 文件 | 3 系统配置 | 4 管理员
_TOOL_RISK: dict[str, int] = {
    "get_current_time": 0,
    "list_windows": 0,
    "list_processes": 0,
    "find_process": 0,
    "get_network_info": 0,
    "list_wifi_networks": 0,
    "read_registry": 0,
    "list_services": 0,
    "open_app": 1,
    "close_app": 1,
    "focus_window": 1,
    "type_text": 1,
    "delete_file": 2,
    "move_to_trash": 2,
    "write_registry": 3,
    "control_service": 3,
    "set_dns": 3,
    "run_shell": 3,
    "connect_wifi": 3,
    "uninstall_software": 4,
    "elevate_self": 4,
}

_CONFIRM_LEVEL = 2


class PermissionManager:
    """权限检查与确认策略。"""

    def risk_level(self, tool: str) -> int:
        return _TOOL_RISK.get((tool or "").strip(), 2)

    def requires_confirmation(self, tool: str, *, force: bool = False) -> bool:
        if force:
            return True
        return self.risk_level(tool) >= _CONFIRM_LEVEL

    def allowed(self, tool: str, *, user_confirmed: bool = False) -> tuple[bool, str]:
        level = self.risk_level(tool)
        if level >= _CONFIRM_LEVEL and not user_confirmed:
            return False, f"操作 {tool} 需要用户确认（风险等级 {level}）"
        return True, ""
