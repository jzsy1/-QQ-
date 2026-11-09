"""应用管理 — 统一路径发现、注册、启动与验证。"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from ..app_aliases import NAME_ALIASES
from ..shell_folders import resolve_shell_folder
from .app_control import launch_app
from .app_verify import execution_to_ok_msg, verify_launch_result
from .desktop_apps import find_desktop_app, refresh_desktop_index
from .execution_result import ExecutionResult, can_report_success

logger = logging.getLogger(__name__)

_TRUSTED_SHORT_CMDS = frozenset({
    "notepad",
    "calc",
    "explorer",
    "msedge",
    "mspaint",
    "cmd",
    "powershell",
    "control",
    "taskmgr",
})


def is_valid_launch_target(cmd: str) -> bool:
    if not cmd or not cmd.strip():
        return False
    cmd = cmd.strip()
    lower = cmd.lower()
    if lower.startswith(("appid:", "shell:", "http://", "https://", "microsoft-edge:")):
        return True
    if os.path.isfile(cmd):
        return True
    if lower in _TRUSTED_SHORT_CMDS:
        return True
    if "/" in cmd or "\\" in cmd:
        return False
    return False


def candidate_names(name: str) -> list[str]:
    raw = name.strip()
    if not raw:
        return []
    names = [raw]
    alias = NAME_ALIASES.get(raw.lower())
    if alias and alias not in names:
        names.append(alias)
    return names


@dataclass
class AppEntry:
    name: str
    path: str
    source: str = "manual"


class AppManager:
    """本机应用路径注册表 + 多源发现 + 启动验证。"""

    def __init__(self, memory, apps_from_rules: dict[str, str] | None = None):
        self.memory = memory
        self.apps_from_rules = dict(apps_from_rules or {})
        self._manual: dict[str, str] = {}

    def register(self, name: str, path: str) -> None:
        key = (name or "").strip().lower()
        path = (path or "").strip()
        if not key or not path:
            return
        self._manual[key] = path
        self.memory.register_app(name.strip(), path, verified=False)
        logger.info("AppManager 注册 %s → %s", name, path[:80])

    def find(self, name: str) -> AppEntry | None:
        resolved = self.resolve(name)
        if not resolved:
            return None
        display, path, source = resolved
        return AppEntry(name=display, path=path, source=source)

    def resolve(self, name: str) -> tuple[str, str, str] | None:
        """返回 (显示名, 启动目标, 来源)。"""
        query = (name or "").strip()
        if not query:
            return None

        key = query.lower()
        manual = self._manual.get(key)
        if manual:
            return query, manual, "manual"

        shell_hit = resolve_shell_folder(query)
        if shell_hit:
            display, cmd = shell_hit
            self.memory.register_app(display, cmd, verified=True)
            return display, cmd, "shell"

        context_apps = self.memory.recent_app_aliases()
        row = self.memory.find_app_row(query, context_apps=context_apps)

        if row and self.memory.is_app_trusted(row, name=query):
            cmd = row["command"]
            if is_valid_launch_target(cmd):
                self.memory.find_app(query)
                return row.get("alias") or query, cmd, "memory_trusted"

        for candidate in candidate_names(query):
            installed = find_desktop_app(candidate, context_apps=context_apps)
            if installed:
                display, path = installed
                self.memory.register_app(display, path, verified=False)
                return display, path, "desktop"

        if row:
            cmd = row["command"]
            conf = float(row.get("confidence") or 0.5)
            if cmd and is_valid_launch_target(cmd) and conf >= 0.25:
                self.memory.find_app(query)
                return row.get("alias") or query, cmd, "memory"

        lower = query.lower()
        for app_name, command in self.apps_from_rules.items():
            if app_name.lower() in lower or lower in app_name.lower():
                if is_valid_launch_target(command):
                    self.memory.register_app(app_name, command, verified=False)
                    return app_name, command, "rules"
                for candidate in candidate_names(app_name):
                    alias_hit = find_desktop_app(candidate, context_apps=context_apps)
                    if alias_hit:
                        display, path = alias_hit
                        self.memory.register_app(display, path, verified=False)
                        return display, path, "desktop"

        return None

    def resolve_command(self, name: str) -> str | None:
        hit = self.resolve(name)
        return hit[1] if hit else None

    def open(
        self,
        name: str,
        *,
        force_new: bool = False,
        timeout: float = 8.0,
    ) -> ExecutionResult:
        """查找并启动应用，带物理验证。"""
        from .process_ops import focus_app, has_app_main_window, is_app_running

        query = (name or "").strip()
        if not query:
            return ExecutionResult(
                success=False,
                message="请指定要打开的应用名称",
                verified=False,
                action="open_app",
                target="",
            )

        hit = self.resolve(query)
        if not hit:
            refresh_desktop_index()
            hit = self.resolve(query)
        if not hit:
            return ExecutionResult(
                success=False,
                message=f"在本机应用库中没找到「{query}」，请说桌面或开始菜单里显示的名称。",
                verified=False,
                action="open_app",
                target=query,
            )

        display, cmd, _source = hit

        if is_app_running(display, cmd, memory=self.memory) and not force_new:
            if has_app_main_window(display, cmd, memory=self.memory):
                ok_focus, focus_msg = focus_app(display, cmd)
                if ok_focus:
                    return ExecutionResult(
                        success=True,
                        message=f"好的，已切换到 {display}。",
                        verified=True,
                        action="open_app",
                        target=display,
                    )
            launched, launch_msg = launch_app(cmd)
            result = verify_launch_result(
                launched,
                launch_msg,
                display,
                cmd,
                self.memory,
                timeout=timeout,
                # 等主窗口出现而非仅进程：避免冷机窗口未聚焦时后续 type_text 输错窗口
                require_window=True,
            )
            return result

        launched, launch_msg = launch_app(cmd)
        return verify_launch_result(
            launched,
            launch_msg,
            display,
            cmd,
            self.memory,
            timeout=timeout,
            require_window=True,
        )

    def open_as_tool_tuple(
        self,
        name: str,
        *,
        force_new: bool = False,
        timeout: float = 8.0,
    ) -> tuple[bool, str, str, str]:
        """返回 (ok, message, display, launch_cmd) 供 executor 写记忆。"""
        result = self.open(name, force_new=force_new, timeout=timeout)
        hit = self.resolve(name)
        display = hit[0] if hit else name
        cmd = hit[1] if hit else ""
        return can_report_success(result), result.message, display, cmd
