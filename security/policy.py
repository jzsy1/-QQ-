"""安全策略 — 权限、确认、审计边界。

支持从 YAML 配置加载风险等级和确认策略（规范 PART XVII）。
加载顺序：YAML > 内置硬编码默认（稳定优先）。

配置文件：
    config/permissions.yaml — 风险等级 + 确认策略 + 路径白名单

向后兼容：
    - RiskLevel / risk_for_tool / requires_confirmation 接口不变
    - YAML 不存在或解析失败时降级到硬编码默认
    - 新增 PolicyConfig 类提供完整配置访问
"""

from __future__ import annotations

import logging
import os
import threading
from enum import IntEnum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ================================================================
# 风险等级
# ================================================================


class RiskLevel(IntEnum):
    LOW = 0
    MEDIUM = 1
    HIGH = 2
    CRITICAL = 3


_RISK_LEVEL_NAMES: dict[str, RiskLevel] = {
    "low": RiskLevel.LOW,
    "medium": RiskLevel.MEDIUM,
    "high": RiskLevel.HIGH,
    "critical": RiskLevel.CRITICAL,
    "none": RiskLevel.LOW,  # none 等同 low（不确认）
}


def _parse_risk_level(val: Any) -> RiskLevel:
    """解析风险等级（支持字符串或数字）。"""
    if isinstance(val, RiskLevel):
        return val
    if isinstance(val, int):
        try:
            return RiskLevel(val)
        except ValueError:
            return RiskLevel.LOW
    if isinstance(val, str):
        return _RISK_LEVEL_NAMES.get(val.strip().lower(), RiskLevel.LOW)
    return RiskLevel.LOW


# ================================================================
# 内置默认（硬编码兜底）
# ================================================================

_DEFAULT_TOOL_RISK: dict[str, RiskLevel] = {
    "delete_file": RiskLevel.HIGH,
    "move_to_trash": RiskLevel.MEDIUM,
    "write_registry": RiskLevel.HIGH,
    "control_service": RiskLevel.HIGH,
    "run_shell": RiskLevel.MEDIUM,
    "uninstall_software": RiskLevel.CRITICAL,
    "kill_process": RiskLevel.MEDIUM,
}

_DEFAULT_CONFIRMATION_MIN_LEVEL = RiskLevel.MEDIUM
_DEFAULT_ALWAYS_ALLOW: frozenset[str] = frozenset()
_DEFAULT_ALWAYS_CONFIRM: frozenset[str] = frozenset({"uninstall_software"})
_DEFAULT_SESSION_CACHE_TTL = 300


# ================================================================
# PolicyConfig — 从 YAML 加载的完整策略配置
# ================================================================


class PolicyConfig:
    """安全策略配置（从 permissions.yaml 加载）。

    Attributes:
        tool_risk: 工具名 → RiskLevel
        confirmation_min_level: 需要确认的最低风险等级
        always_allow: 永不需要确认的工具集合
        always_confirm: 永远需要确认的工具集合
        session_cache_ttl_sec: 会话内免重复确认的 TTL
        allowed_write_paths: 允许写入的目录前缀列表
        blocked_write_paths: 禁止写入的目录前缀列表
        blocked_delete_paths: 禁止删除的目录前缀列表
        source: 配置来源描述（"yaml" / "default"）
    """

    def __init__(
        self,
        *,
        tool_risk: dict[str, RiskLevel] | None = None,
        confirmation_min_level: RiskLevel = _DEFAULT_CONFIRMATION_MIN_LEVEL,
        always_allow: set[str] | None = None,
        always_confirm: set[str] | None = None,
        session_cache_ttl_sec: int = _DEFAULT_SESSION_CACHE_TTL,
        allowed_write_paths: list[str] | None = None,
        blocked_write_paths: list[str] | None = None,
        blocked_delete_paths: list[str] | None = None,
        source: str = "default",
    ) -> None:
        # 合并：YAML 提供的覆盖默认，但默认仍保留（YAML 未列出的工具用默认风险）
        self._tool_risk: dict[str, RiskLevel] = dict(_DEFAULT_TOOL_RISK)
        if tool_risk:
            self._tool_risk.update(tool_risk)

        self.confirmation_min_level = confirmation_min_level
        self.always_allow: set[str] = set(always_allow or _DEFAULT_ALWAYS_ALLOW)
        self.always_confirm: set[str] = set(always_confirm or _DEFAULT_ALWAYS_CONFIRM)
        self.session_cache_ttl_sec = int(session_cache_ttl_sec)
        self.allowed_write_paths: list[str] = list(allowed_write_paths or [])
        self.blocked_write_paths: list[str] = list(blocked_write_paths or [])
        self.blocked_delete_paths: list[str] = list(blocked_delete_paths or [])
        self.source = source

    # ------------------------------------------------------------------
    # 查询 API
    # ------------------------------------------------------------------

    def risk_for_tool(self, tool: str) -> RiskLevel:
        """获取工具的风险等级。"""
        if not isinstance(tool, str):
            return RiskLevel.LOW
        return self._tool_risk.get(tool.strip(), RiskLevel.LOW)

    def requires_confirmation(self, tool: str) -> bool:
        """判断工具是否需要用户确认。

        规则：
        1. always_confirm 中的工具 → True
        2. always_allow 中的工具 → False
        3. 风险等级 >= confirmation_min_level → True
        4. 否则 → False
        """
        if not isinstance(tool, str):
            return False
        name = tool.strip()
        if not name:
            return False
        if name in self.always_confirm:
            return True
        if name in self.always_allow:
            return False
        return self.risk_for_tool(name) >= self.confirmation_min_level

    def is_path_writable(self, path: str) -> tuple[bool, str]:
        """检查路径是否可写。

        Returns: (是否允许, 原因)
        """
        if not isinstance(path, str):
            return False, "非字符串路径"
        p = path.strip()
        if not p:
            return False, "空路径"

        # 先检查黑名单（优先级高）
        for blocked in self.blocked_write_paths:
            if p.startswith(blocked):
                return False, f"路径在黑名单: {blocked}"

        # 再检查白名单（若配置了白名单，必须在白名单内）
        if self.allowed_write_paths:
            for allowed in self.allowed_write_paths:
                if p.startswith(allowed):
                    return True, "在白名单"
            return False, "路径不在白名单"

        # 无白名单配置时默认允许（仅黑名单生效）
        return True, "无白名单限制"

    def is_path_deletable(self, path: str) -> tuple[bool, str]:
        """检查路径是否可删除。"""
        if not isinstance(path, str):
            return False, "非字符串路径"
        p = path.strip()
        if not p:
            return False, "空路径"
        for blocked in self.blocked_delete_paths:
            if p.startswith(blocked):
                return False, f"路径在删除黑名单: {blocked}"
        return True, "允许删除"

    def to_dict(self) -> dict:
        """导出为字典（调试用）。"""
        return {
            "tool_risk": {k: v.name.lower() for k, v in self._tool_risk.items()},
            "confirmation_min_level": self.confirmation_min_level.name.lower(),
            "always_allow": sorted(self.always_allow),
            "always_confirm": sorted(self.always_confirm),
            "session_cache_ttl_sec": self.session_cache_ttl_sec,
            "allowed_write_paths": self.allowed_write_paths,
            "blocked_write_paths": self.blocked_write_paths,
            "blocked_delete_paths": self.blocked_delete_paths,
            "source": self.source,
        }


# ================================================================
# YAML 加载
# ================================================================


def _find_permissions_yaml(config_dir: str | Path | None = None) -> Path | None:
    """查找 permissions.yaml 文件位置。

    当显式传了 config_dir 时，只在该目录及其 config/ 子目录查找；
    未传时才 fallback 到包目录。
    """
    candidates: list[Path] = []
    if config_dir:
        candidates.append(Path(config_dir) / "permissions.yaml")
        candidates.append(Path(config_dir) / "config" / "permissions.yaml")
    else:
        # 仅在未显式指定时才搜索包目录
        pkg_dir = Path(__file__).resolve().parent.parent
        candidates.append(pkg_dir / "config" / "permissions.yaml")
        candidates.append(pkg_dir / "permissions.yaml")

    for cand in candidates:
        if cand.is_file():
            return cand
    return None


def load_policy_config(
    config_dir: str | Path | None = None,
    *,
    force_reload: bool = False,
) -> PolicyConfig:
    """加载策略配置（带缓存）。

    Args:
        config_dir: 配置目录路径（None 时自动查找）
        force_reload: 强制重新加载（忽略缓存）

    Returns: PolicyConfig 实例
    """
    global _cached_config, _cached_config_dir
    with _config_lock:
        if not force_reload and _cached_config is not None:
            if _cached_config_dir == (str(config_dir) if config_dir else None):
                return _cached_config

        yaml_path = _find_permissions_yaml(config_dir)
        if yaml_path is None:
            logger.info("permissions.yaml 未找到，使用硬编码默认策略")
            config = PolicyConfig(source="default")
        else:
            try:
                config = _load_yaml_policy(yaml_path)
                logger.info("策略配置已加载: %s", yaml_path)
            except Exception:
                logger.error("策略配置加载失败 %s，降级到默认", yaml_path, exc_info=True)
                config = PolicyConfig(source="default")

        _cached_config = config
        _cached_config_dir = str(config_dir) if config_dir else None
        return config


def _load_yaml_policy(yaml_path: Path) -> PolicyConfig:
    """从 YAML 文件解析策略配置。"""
    try:
        import yaml
    except ImportError:
        logger.warning("PyYAML 未安装，使用硬编码默认策略")
        return PolicyConfig(source="default")

    with open(yaml_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    if not isinstance(data, dict):
        return PolicyConfig(source="default")

    # 解析 tool_risk
    tool_risk: dict[str, RiskLevel] = {}
    raw_risk = data.get("tool_risk") or {}
    if isinstance(raw_risk, dict):
        for tool_name, level_val in raw_risk.items():
            if not isinstance(tool_name, str):
                continue
            tool_risk[tool_name.strip()] = _parse_risk_level(level_val)

    # 解析 confirmation
    raw_confirm = data.get("confirmation") or {}
    if not isinstance(raw_confirm, dict):
        raw_confirm = {}

    min_level = _parse_risk_level(
        raw_confirm.get("min_level", _DEFAULT_CONFIRMATION_MIN_LEVEL)
    )

    always_allow: set[str] = set()
    raw_allow = raw_confirm.get("always_allow") or []
    if isinstance(raw_allow, list):
        for item in raw_allow:
            if isinstance(item, str) and item.strip():
                always_allow.add(item.strip())

    always_confirm: set[str] = set(_DEFAULT_ALWAYS_CONFIRM)
    raw_always_confirm = raw_confirm.get("always_confirm") or []
    if isinstance(raw_always_confirm, list):
        for item in raw_always_confirm:
            if isinstance(item, str) and item.strip():
                always_confirm.add(item.strip())

    session_cache_ttl = int(raw_confirm.get("session_cache_ttl_sec", _DEFAULT_SESSION_CACHE_TTL))

    # 解析 path_policy
    raw_path = data.get("path_policy") or {}
    if not isinstance(raw_path, dict):
        raw_path = {}

    allowed_write = _extract_str_list(raw_path.get("allowed_write_paths"))
    blocked_write = _extract_str_list(raw_path.get("blocked_write_paths"))
    blocked_delete = _extract_str_list(raw_path.get("blocked_delete_paths"))

    # 仓库根（kekesi 包与 assistant-x-openclaw 的共同父目录）按代码位置推导，
    # 随整棵树搬迁，避免把盘符写死在配置里
    repo_root = str(Path(__file__).resolve().parents[2])
    if repo_root not in allowed_write:
        allowed_write.append(repo_root)

    return PolicyConfig(
        tool_risk=tool_risk,
        confirmation_min_level=min_level,
        always_allow=always_allow,
        always_confirm=always_confirm,
        session_cache_ttl_sec=session_cache_ttl,
        allowed_write_paths=allowed_write,
        blocked_write_paths=blocked_write,
        blocked_delete_paths=blocked_delete,
        source=f"yaml:{yaml_path.name}",
    )


def _extract_str_list(val: Any) -> list[str]:
    """从 YAML 值提取字符串列表。"""
    if not val:
        return []
    if isinstance(val, str):
        return [val]
    if isinstance(val, list):
        return [str(x).strip() for x in val if isinstance(x, str) and x.strip()]
    return []


# ================================================================
# 缓存
# ================================================================

_cached_config: PolicyConfig | None = None
_cached_config_dir: str | None = None
_config_lock = threading.Lock()


def reset_cached_config() -> None:
    """重置缓存（仅用于测试）。"""
    global _cached_config, _cached_config_dir
    with _config_lock:
        _cached_config = None
        _cached_config_dir = None


# ================================================================
# 向后兼容 API（模块级函数，使用缓存的 PolicyConfig）
# ================================================================


def risk_for_tool(tool: str) -> RiskLevel:
    """获取工具的风险等级（向后兼容）。"""
    return load_policy_config().risk_for_tool(tool)


def requires_confirmation(tool: str) -> bool:
    """判断工具是否需要用户确认（向后兼容）。"""
    return load_policy_config().requires_confirmation(tool)


def is_path_writable(path: str) -> tuple[bool, str]:
    """检查路径是否可写。"""
    return load_policy_config().is_path_writable(path)


def is_path_deletable(path: str) -> tuple[bool, str]:
    """检查路径是否可删除。"""
    return load_policy_config().is_path_deletable(path)
