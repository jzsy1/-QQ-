"""Windows 系统智能体 — 应用/进程/窗口操作的统一入口。"""

from __future__ import annotations

import logging
import re
from typing import Any

from ...tools.execution_result import ExecutionResult, can_report_success
from .process_manager import ProcessManager
from .window_manager import WindowManager

logger = logging.getLogger(__name__)

_OPEN_RE = re.compile(
    r"^(?:请|帮我|给我)?(?:打开|启动|运行)(?:一下|下)?\s*(.+?)[。！？!?\s]*$",
)
_CLOSE_RE = re.compile(
    r"^(?:请|帮我)?(?:关闭|关掉|退出)(?:一下|下)?\s*(.+?)[。！？!?\s]*$",
)
_LIST_WINDOWS_RE = re.compile(
    r"(列出|看看|显示|有哪些).*(窗口|打开了什么|打开的程序)",
)
_DESKTOP_LIST_RE = re.compile(r"(桌面有什么|桌面上有什么|看看桌面|桌面文件)")
_SHOW_DESKTOP_RE = re.compile(r"(显示桌面|回到桌面|最小化所有窗口)")


class WindowsAgent:
    """Windows Agent 门面：App / Process / Window / File / Shell。"""

    def __init__(self, tool_ctx: Any) -> None:
        self._ctx = tool_ctx
        self.windows = WindowManager()
        self.processes = ProcessManager(memory=getattr(tool_ctx, "memory", None))
        from .clipboard_manager import ClipboardManager
        from .env_manager import EnvManager
        from .file_manager import FileManager
        from .package_manager import PackageManager
        from .system_manager import SystemManager
        from .integration_manager import IntegrationManager
        from .messaging_manager import MessagingManager
        from .automation_manager import AutomationManager
        from .network_manager import NetworkManager
        from .input_manager import InputManager
        from .permission_manager import PermissionManager
        from .registry_manager import RegistryManager
        from .service_manager import ServiceManager
        from .shell_manager import ShellManager
        from .screen_manager import ScreenManager
        from .workflow_manager import WorkflowManager
        from .assistant_manager import AssistantManager
        from .devtools_manager import DevToolsManager
        from .media_manager import MediaManager
        from .scheduler_manager import SchedulerManager
        from .skill_acquisition_manager import SkillAcquisitionManager
        from .web_manager import WebManager
        from .uia_manager import UiaManager
        from .vision_manager import VisionManager
        from .verifier import WindowsVerifier

        self.files = FileManager()
        self.packages = PackageManager()
        self.env = EnvManager()
        self.messaging = MessagingManager(tool_ctx)
        self.automation = AutomationManager()
        self.screen = ScreenManager()
        self.workflows = WorkflowManager(tool_ctx)
        self.media = MediaManager()
        self.web = WebManager(tool_ctx)
        self.scheduler = SchedulerManager(tool_ctx)
        self.devtools = DevToolsManager()
        self.skill_acquisition = SkillAcquisitionManager(tool_ctx)
        self.assistant = AssistantManager(tool_ctx)
        self.shell = ShellManager()
        self.registry = RegistryManager()
        self.services = ServiceManager()
        self.network = NetworkManager()
        self.permissions = PermissionManager()
        self.integration = IntegrationManager()
        self.input = InputManager()
        self.clipboard = ClipboardManager()
        self.system = SystemManager()
        self.uia = UiaManager()
        self.vision = VisionManager()
        self.verifier = WindowsVerifier()

    @property
    def apps(self):
        return self._ctx.app_manager

    def open_app(self, name: str, *, force_new: bool = False) -> ExecutionResult:
        return self.apps.open(name, force_new=force_new)

    def open_app_and_type(
        self,
        app_name: str,
        text: str,
        *,
        force_new: bool = False,
        press_enter: bool = False,
    ) -> ExecutionResult:
        import time

        from ...tools.app_control import launch_app_and_type
        from ...tools.app_manager import candidate_names
        from ...tools.desktop_apps import find_desktop_app, refresh_desktop_index
        from ...tools.keyboard_input import send_keys, type_text
        from ...tools.process_ops import focus_app, is_app_running

        raw_app = (app_name or "").strip()
        content = (text or "").strip()
        if not raw_app or not content:
            return ExecutionResult(
                success=False,
                message="请指定应用名称和要输入的内容",
                verified=False,
                action="open_app_and_type",
                target="",
            )

        cmd = self._ctx.resolve_app_command(raw_app)
        if not cmd:
            refresh_desktop_index()
            cmd = self._ctx.resolve_app_command(raw_app)
        if not cmd:
            return ExecutionResult(
                success=False,
                message=(
                    f"在本机应用库中没找到「{raw_app}」，"
                    "请说桌面或开始菜单里显示的名称。"
                ),
                verified=False,
                action="open_app_and_type",
                target=raw_app,
            )

        display = raw_app
        for candidate in candidate_names(raw_app):
            hit = find_desktop_app(candidate)
            if hit:
                display = hit[0]
                break

        memory = getattr(self._ctx, "memory", None)
        if is_app_running(display, cmd, memory=memory) and not force_new:
            focus_app(display, cmd)
            time.sleep(0.5)
            ok, msg = type_text(content, delay_sec=0.2)
            if ok and press_enter:
                time.sleep(0.3)
                send_keys("{ENTER}", delay_sec=0.1)
                msg = f"{msg}，已按回车搜索"
            message = f"已在运行的 {display} 中输入：{msg}" if ok else msg
            return ExecutionResult(
                success=ok,
                message=message,
                verified=ok,
                action="open_app_and_type",
                target=display,
            )

        ok, msg = launch_app_and_type(cmd, content, app_name=display)
        if ok and press_enter:
            time.sleep(0.5)
            send_keys("{ENTER}", delay_sec=0.1)
            msg = f"{msg}，已按回车搜索"
        return ExecutionResult(
            success=ok,
            message=msg,
            verified=ok,
            action="open_app_and_type",
            target=display,
        )

    def close_app(self, name: str, *, force: bool = True) -> ExecutionResult:
        launch = self.apps.resolve_command(name) or ""
        return self.processes.close_app(name, launch_target=launch, force=force)

    def uninstall_app(self, name: str) -> ExecutionResult:
        from ...tools.package_ops import uninstall_software_verified

        ok, msg = uninstall_software_verified(name)
        return ExecutionResult(
            success=ok,
            message=msg,
            verified=ok,
            action="uninstall_software",
            target=name,
        )

    def find_process(self, name: str) -> ExecutionResult:
        from ...tools.process_ops import discover_processes_multi

        pids, names = discover_processes_multi([name])
        if pids or names:
            return ExecutionResult(
                success=True,
                message=f"找到进程：{names or pids}",
                verified=True,
                action="find_process",
                target=name,
                extra={"pids": pids, "names": names},
            )
        return ExecutionResult(
            success=False,
            message=f"未找到与「{name}」相关的进程",
            verified=True,
            action="find_process",
            target=name,
        )

    def run_shell(
        self,
        command: str,
        *,
        timeout: int = 30,
        shell: str = "powershell",
        admin: bool = False,
    ) -> ExecutionResult:
        cancel_check = getattr(self._ctx, "cancel_check", None)
        task_id = getattr(self._ctx, "task_id", None)
        result = self.shell.run(
            command,
            timeout=timeout,
            shell=shell,
            admin=admin,
            cancel_check=cancel_check,
            task_id=task_id,
        )
        return ExecutionResult(
            success=result.success,
            message=result.message,
            verified=result.success and not result.cancelled,
            action="run_shell",
            target=command[:80],
            extra={
                "exit_code": result.exit_code,
                "cancelled": result.cancelled,
                "timed_out": result.timed_out,
            },
        )

    def run_shell_system(self, command: str, *, timeout: int = 180) -> ExecutionResult:
        # SHELL_SYSTEM_CALLER_V1: same pattern as run_shell above:
        # task_id/cancel_check come from self._ctx, not from params.
        cancel_check = getattr(self._ctx, "cancel_check", None)
        task_id = getattr(self._ctx, "task_id", None)
        result = self.shell.run_system(command, timeout=timeout, cancel_check=cancel_check, task_id=task_id)
        return ExecutionResult(
            success=result.success,
            message=result.message,
            verified=result.success,
            action="run_shell_system",
            target=command[:80],
            extra={"exit_code": result.exit_code},
        )

    def _result_reply(self, result: ExecutionResult) -> str:
        if can_report_success(result):
            return result.message or "已完成。"
        if result.message:
            return result.message
        return result.error or "操作未能完成。"

    def try_fastpath(self, user_text: str) -> tuple[str, str, list[dict]] | None:
        text = (user_text or "").strip()
        if not text or len(text) > 48:
            return None

        if _LIST_WINDOWS_RE.search(text):
            result = self.windows.list_windows()
            return self._pack("windows_fastpath", "list_open_windows", {}, result)

        if _DESKTOP_LIST_RE.search(text):
            result = self.windows.list_desktop()
            return self._pack("windows_fastpath", "list_desktop_files", {}, result)

        if _SHOW_DESKTOP_RE.search(text):
            result = self.windows.show_desktop()
            return self._pack("windows_fastpath", "show_desktop", {}, result)

        m = _OPEN_RE.match(text)
        if m:
            app = m.group(1).strip()
            if app and len(app) <= 24:
                result = self.open_app(app)
                return self._pack("windows_fastpath", "open_app", {"app_name": app}, result)

        m = _CLOSE_RE.match(text)
        if m:
            app = m.group(1).strip()
            if app and len(app) <= 24:
                result = self.close_app(app)
                return self._pack("windows_fastpath", "close_app", {"app_name": app}, result)

        return None

    def _pack(
        self,
        layer: str,
        func: str,
        args: dict,
        result: ExecutionResult,
    ) -> tuple[str, str, list[dict]]:
        ok = can_report_success(result)
        summary = (result.message or result.error or "")[:120]
        tools = [{
            "name": func,
            "func": func,
            "ok": ok,
            "args": args,
            "summary": summary,
            "text": summary,
            "verified": result.verified,
            "via": "windows_agent",
        }]
        reply = self._result_reply(result)
        logger.info("WindowsAgent 快路径 %s ok=%s", func, ok)
        return layer, reply, tools
