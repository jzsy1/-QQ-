"""tools.yaml 配置加载器 — 工具开关、超时、限流、参数限制。

规范 PART XVII：工具开关配置化（零硬编码）。
加载 config/tools.yaml，与 config.yaml 的 tools 段互补：
- config.yaml 控制粗粒度开关（app_control_enabled 等）
- tools.yaml 控制细粒度超时/限流/参数限制

加载顺序：tools.yaml > config.yaml > 代码内默认值（fail-soft）
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ================================================================
# 内置默认（硬编码兜底）
# ================================================================

_DEFAULT_ENABLED: dict[str, bool] = {
    "app_control": True,
    "file_ops": True,
    "shell": True,
    "registry": True,
    "network": True,
    "process": True,
    "service": True,
    "input": True,
    "browser": True,
    "media": True,
    "system": True,
    "web_research": True,
}

_DEFAULT_TIMEOUT: dict[str, int] = {
    "shell": 30,
    "uninstall": 300,
    "app_control": 60,
    "close_app": 25,
    "kill_process": 15,
    "default": 60,
}

_DEFAULT_LIMITS: dict[str, int] = {
    "list_files_max": 500,
    "list_processes_max": 1000,
    "search_web_max_results": 10,
    "type_text_max_chars": 5000,
    "run_shell_max_cmd_length": 2000,
}

_DEFAULT_RATE_LIMIT: dict[str, Any] = {
    "enabled": False,
    "max_calls_per_min": 60,
    "per_tool": {},
}


# ================================================================
# ToolsConfig
# ================================================================


class ToolsConfig:
    """工具配置（从 tools.yaml 加载）。

    Attributes:
        enabled: 工具类别开关
        timeout: 工具超时（秒）
        rate_limit: 限流配置
        limits: 参数限制
        source: 配置来源
    """

    def __init__(
        self,
        *,
        enabled: dict[str, bool] | None = None,
        timeout: dict[str, int] | None = None,
        rate_limit: dict[str, Any] | None = None,
        limits: dict[str, int] | None = None,
        source: str = "default",
    ) -> None:
        self.enabled: dict[str, bool] = dict(_DEFAULT_ENABLED)
        if enabled:
            self.enabled.update(enabled)

        self.timeout: dict[str, int] = dict(_DEFAULT_TIMEOUT)
        if timeout:
            self.timeout.update(timeout)

        self.rate_limit: dict[str, Any] = dict(_DEFAULT_RATE_LIMIT)
        if rate_limit:
            self.rate_limit.update(rate_limit)

        self.limits: dict[str, int] = dict(_DEFAULT_LIMITS)
        if limits:
            self.limits.update(limits)

        self.source = source

    # ------------------------------------------------------------------
    # 查询 API
    # ------------------------------------------------------------------

    def is_enabled(self, category: str) -> bool:
        """检查工具类别是否启用。"""
        if not isinstance(category, str):
            return True  # 未知类别默认启用（安全默认）
        return self.enabled.get(category.strip().lower(), True)

    def get_timeout(self, tool: str, *, default: int | None = None) -> int:
        """获取工具超时（秒）。"""
        if not isinstance(tool, str):
            return default if default is not None else self.timeout.get("default", 60)
        val = self.timeout.get(tool.strip().lower())
        if val is not None:
            try:
                return int(val)
            except (TypeError, ValueError):
                pass
        return default if default is not None else self.timeout.get("default", 60)

    def get_limit(self, name: str, *, default: int = 0) -> int:
        """获取参数限制值。"""
        if not isinstance(name, str):
            return default
        try:
            return int(self.limits.get(name.strip(), default))
        except (TypeError, ValueError):
            return default

    def is_rate_limited(self) -> bool:
        """是否启用限流。"""
        return bool(self.rate_limit.get("enabled", False))

    def get_rate_limit_per_min(self) -> int:
        """获取全局每分钟最大调用次数。"""
        try:
            return int(self.rate_limit.get("max_calls_per_min", 60))
        except (TypeError, ValueError):
            return 60

    def get_tool_rate_limit_per_min(self, tool: str) -> int | None:
        """获取单工具每分钟最大调用次数（None 表示无特别限制）。"""
        if not isinstance(tool, str):
            return None
        per_tool = self.rate_limit.get("per_tool") or {}
        if not isinstance(per_tool, dict):
            return None
        val = per_tool.get(tool.strip().lower())
        if val is None:
            return None
        try:
            return int(val)
        except (TypeError, ValueError):
            return None

    def to_dict(self) -> dict:
        """导出为字典（调试用）。"""
        return {
            "enabled": dict(self.enabled),
            "timeout": dict(self.timeout),
            "rate_limit": dict(self.rate_limit),
            "limits": dict(self.limits),
            "source": self.source,
        }


# ================================================================
# YAML 加载
# ================================================================


def _find_tools_yaml(config_dir: str | Path | None = None) -> Path | None:
    """查找 tools.yaml 文件位置。"""
    candidates: list[Path] = []
    if config_dir:
        candidates.append(Path(config_dir) / "tools.yaml")
        candidates.append(Path(config_dir) / "config" / "tools.yaml")
    else:
        pkg_dir = Path(__file__).resolve().parent
        candidates.append(pkg_dir / "config" / "tools.yaml")
        candidates.append(pkg_dir / "tools.yaml")

    for cand in candidates:
        if cand.is_file():
            return cand
    return None


def load_tools_config(
    config_dir: str | Path | None = None,
    *,
    force_reload: bool = False,
) -> ToolsConfig:
    """加载工具配置（带缓存）。"""
    global _cached_tools_config, _cached_tools_dir
    with _tools_lock:
        if not force_reload and _cached_tools_config is not None:
            if _cached_tools_dir == (str(config_dir) if config_dir else None):
                return _cached_tools_config

        yaml_path = _find_tools_yaml(config_dir)
        if yaml_path is None:
            logger.info("tools.yaml 未找到，使用硬编码默认配置")
            config = ToolsConfig(source="default")
        else:
            try:
                config = _load_yaml_tools(yaml_path)
                logger.info("工具配置已加载: %s", yaml_path)
            except Exception:
                logger.error("工具配置加载失败 %s，降级到默认", yaml_path, exc_info=True)
                config = ToolsConfig(source="default")

        _cached_tools_config = config
        _cached_tools_dir = str(config_dir) if config_dir else None
        return config


def _load_yaml_tools(yaml_path: Path) -> ToolsConfig:
    """从 YAML 文件解析工具配置。"""
    try:
        import yaml
    except ImportError:
        logger.warning("PyYAML 未安装，使用硬编码默认工具配置")
        return ToolsConfig(source="default")

    with open(yaml_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    if not isinstance(data, dict):
        return ToolsConfig(source="default")

    # 解析 enabled
    enabled: dict[str, bool] = {}
    raw_enabled = data.get("enabled") or {}
    if isinstance(raw_enabled, dict):
        for k, v in raw_enabled.items():
            if isinstance(k, str):
                enabled[k.strip().lower()] = bool(v)

    # 解析 timeout
    timeout: dict[str, int] = {}
    raw_timeout = data.get("timeout") or {}
    if isinstance(raw_timeout, dict):
        for k, v in raw_timeout.items():
            if isinstance(k, str):
                try:
                    timeout[k.strip().lower()] = int(v)
                except (TypeError, ValueError):
                    pass

    # 解析 rate_limit
    raw_rate = data.get("rate_limit") or {}
    if not isinstance(raw_rate, dict):
        raw_rate = {}
    rate_limit: dict[str, Any] = {}
    if raw_rate:
        rate_limit["enabled"] = bool(raw_rate.get("enabled", False))
        try:
            rate_limit["max_calls_per_min"] = int(raw_rate.get("max_calls_per_min", 60))
        except (TypeError, ValueError):
            rate_limit["max_calls_per_min"] = 60
        per_tool: dict[str, int] = {}
        raw_per_tool = raw_rate.get("per_tool") or {}
        if isinstance(raw_per_tool, dict):
            for k, v in raw_per_tool.items():
                if isinstance(k, str):
                    try:
                        per_tool[k.strip().lower()] = int(v)
                    except (TypeError, ValueError):
                        pass
        rate_limit["per_tool"] = per_tool

    # 解析 limits
    limits: dict[str, int] = {}
    raw_limits = data.get("limits") or {}
    if isinstance(raw_limits, dict):
        for k, v in raw_limits.items():
            if isinstance(k, str):
                try:
                    limits[k.strip()] = int(v)
                except (TypeError, ValueError):
                    pass

    return ToolsConfig(
        enabled=enabled,
        timeout=timeout,
        rate_limit=rate_limit,
        limits=limits,
        source=f"yaml:{yaml_path.name}",
    )


# ================================================================
# 缓存
# ================================================================

_cached_tools_config: ToolsConfig | None = None
_cached_tools_dir: str | None = None
_tools_lock = threading.Lock()


def reset_cached_tools_config() -> None:
    """重置缓存（仅用于测试）。"""
    global _cached_tools_config, _cached_tools_dir
    with _tools_lock:
        _cached_tools_config = None
        _cached_tools_dir = None
