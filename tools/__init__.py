"""本地工具"""

from .shell import run_shell
from .file_ops import search_files
from .app_control import launch_app, launch_app_and_type
from .browser_search import open_web_search
from .web_research import configure_web_research, fetch_web_page, search_web
from .keyboard_input import type_text
from .desktop_apps import find_desktop_app, list_desktop_apps, refresh_desktop_index
from .registry import to_ollama_tools, get_tool_names
from .executor import execute_tool, ToolContext

__all__ = [
    "run_shell",
    "search_files",
    "launch_app",
    "launch_app_and_type",
    "type_text",
    "open_web_search",
    "search_web",
    "fetch_web_page",
    "to_ollama_tools",
    "get_tool_names",
    "execute_tool",
    "ToolContext",
    "find_desktop_app",
    "list_desktop_apps",
    "refresh_desktop_index",
]
