"""Windows Agent 子模块 — v2.0 执行层。"""

from .app_manager import AppManager
from .assistant_manager import AssistantManager
from .automation_manager import AutomationManager
from .clipboard_manager import ClipboardManager
from .devtools_manager import DevToolsManager
from .env_manager import EnvManager
from .file_manager import FileManager
from .integration_manager import IntegrationManager
from .input_manager import InputManager
from .media_manager import MediaManager
from .messaging_manager import MessagingManager
from .network_manager import NetworkManager
from .package_manager import PackageManager
from .permission_manager import PermissionManager
from .process_manager import ProcessManager
from .registry_manager import RegistryManager
from .scheduler_manager import SchedulerManager
from .screen_manager import ScreenManager
from .service_manager import ServiceManager
from .shell_manager import ShellManager
from .system_manager import SystemManager
from .uia_manager import UiaManager
from .vision_manager import VisionManager
from .web_manager import WebManager
from .workflow_manager import WorkflowManager
from .verifier import WindowsVerifier
from .window_manager import WindowManager
from .windows_agent import WindowsAgent

__all__ = [
    "AssistantManager",
    "AppManager",
    "AutomationManager",
    "ClipboardManager",
    "DevToolsManager",
    "EnvManager",
    "FileManager",
    "InputManager",
    "MediaManager",
    "MessagingManager",
    "IntegrationManager",
    "NetworkManager",
    "PackageManager",
    "PermissionManager",
    "ProcessManager",
    "RegistryManager",
    "SchedulerManager",
    "ScreenManager",
    "ServiceManager",
    "ShellManager",
    "SystemManager",
    "UiaManager",
    "VisionManager",
    "WebManager",
    "WorkflowManager",
    "WindowManager",
    "WindowsAgent",
    "WindowsVerifier",
]
