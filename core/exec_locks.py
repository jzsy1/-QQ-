"""执行域锁 — 按资源域序列化，允许跨域并行。"""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from typing import Iterator

from ..tools.registry import get_tool_category

logger = logging.getLogger(__name__)

DOMAIN_NONE = "none"
DOMAIN_UI = "ui"
DOMAIN_FILE = "file"
DOMAIN_SYSTEM = "system"
DOMAIN_NETWORK = "network"
DOMAIN_MEDIA = "media"
DOMAIN_DEFAULT = "default"

# 只读查询类工具不加锁
_READONLY_TOOLS = frozenset({
    "get_current_time",
    "set_timer",
    "get_weather",
    "get_system_status",
    "get_hardware_info",
    "get_temperatures",
    "get_temp_threshold",
    "get_system_stats",
    "check_system_health",
    "get_network_info",
    "list_wifi_networks",
    "list_processes",
    "find_process",
    "list_windows",
    "list_open_windows",
    "list_desktop_files",
    "list_desktop_icons",
    "list_directory",
    "search_files",
    "search_memory",
    "search_web",
    "web_search",
    "list_workflows",
    "list_watch_rules",
    "list_screenshots",
    "recall_screenshot",
    "read_screen",
    "read_screen_text",
    "ocr_screen",
    "ocr_find_text",
    "find_text_location",
    "find_image",
    "uia_find_element",
    "uia_list_controls",
    "uia_get_text",
    "list_installed_software",
    "list_services",
    "list_scheduled_tasks",
    "autostart_status",
    "get_privilege_status",
    "read_registry",
    "read_env_var",
    "list_env_vars",
    "read_clipboard",
    "read_code_file",
    "find_tool_definition",
    "qqmusic_status",
    "video_status",
    "check_occlusion",
    "scan_desktop",
    "screenshot_window",
    "take_screenshot",
    "ocr_window",
    "wait_until_found",
    "wait_until_gone",
    "wait_for_stable_screen",
    "wait_for_file",
})

_CATEGORY_TO_DOMAIN: dict[str, str] = {
    "时间": DOMAIN_NONE,
    "天气": DOMAIN_NONE,
    "记忆": DOMAIN_NONE,
    "应用": DOMAIN_UI,
    "输入": DOMAIN_UI,
    "屏幕": DOMAIN_UI,
    "UIA": DOMAIN_UI,
    "IM": DOMAIN_UI,
    "其他": DOMAIN_UI,
    "文件": DOMAIN_FILE,
    "代码": DOMAIN_FILE,
    "系统": DOMAIN_SYSTEM,
    "进程": DOMAIN_SYSTEM,
    "权限": DOMAIN_SYSTEM,
    "服务": DOMAIN_SYSTEM,
    "注册表": DOMAIN_SYSTEM,
    "环境": DOMAIN_SYSTEM,
    "软件": DOMAIN_SYSTEM,
    "网络": DOMAIN_NETWORK,
    "媒体": DOMAIN_MEDIA,
    "监控": DOMAIN_SYSTEM,
    "剪贴板": DOMAIN_DEFAULT,
}


class ExecLockRegistry:
    """按域管理互斥锁。"""

    def __init__(self) -> None:
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def _get_lock(self, domain: str) -> threading.Lock | None:
        if domain == DOMAIN_NONE:
            return None
        with self._guard:
            if domain not in self._locks:
                self._locks[domain] = threading.Lock()
            return self._locks[domain]

    def try_acquire(self, domain: str) -> bool:
        lock = self._get_lock(domain)
        if lock is None:
            return True
        acquired = lock.acquire(blocking=False)
        if not acquired:
            logger.debug("执行域 %s 忙", domain)
        return acquired

    def release(self, domain: str) -> None:
        lock = self._get_lock(domain)
        if lock is None:
            return
        try:
            lock.release()
        except RuntimeError:
            logger.warning("释放执行域锁失败: %s", domain)


_registry = ExecLockRegistry()


def execution_domain(func: str) -> str:
    """根据工具名推断执行域。"""
    name = (func or "").strip()
    if not name:
        return DOMAIN_DEFAULT
    if name.startswith("mcp__"):
        return DOMAIN_NETWORK
    if name in _READONLY_TOOLS:
        return DOMAIN_NONE
    cat = get_tool_category(name)
    return _CATEGORY_TO_DOMAIN.get(cat, DOMAIN_DEFAULT)


@contextmanager
def domain_lock(func: str) -> Iterator[tuple[str, bool]]:
    """尝试获取工具所属域锁。未获取到时 yielded acquired=False。"""
    domain = execution_domain(func)
    acquired = _registry.try_acquire(domain)
    try:
        yield domain, acquired
    finally:
        if acquired:
            _registry.release(domain)
