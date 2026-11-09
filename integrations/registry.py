"""Integration Registry — 外部系统（Bridge）统一注册中心。

只做登记与能力索引，不承载任何具体外部系统的业务模型。
Bridge 只声明自己实际支持的能力；不支持事件的服务器无需为了合规而实现事件。

工具暴露名沿用现有约定（mcp__<服务器>__<工具> / skill__<owner>__<工具>），
本注册表只在其上补一层 provider 归属与能力/意图元数据，
因此不需要改动 r1_executor 的路由分支。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..contract import sanitize_segment

logger = logging.getLogger(__name__)

CAPABILITIES = ("tools", "state", "events", "resources", "operations")
TRANSPORTS = ("stdio", "http", "streamable_http", "sse", "websocket", "mcp")
PROTOCOL_VERSION = "1.0"


class BridgeState(str, Enum):
    DISCOVERED = "discovered"
    INITIALIZING = "initializing"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    STOPPED = "stopped"
    DISABLED = "disabled"


# 只有这些状态下的 Bridge 工具才允许进候选池
_AVAILABLE_STATES = frozenset(
    {BridgeState.HEALTHY, BridgeState.DEGRADED, BridgeState.INITIALIZING}
)


@dataclass
class BridgeManifest:
    """Bridge 自述清单。字段对齐设计文档 §4.1。"""

    id: str
    name: str = ""
    version: str = "0.0.0"
    bridge_version: str = "0.0.0"
    protocol_version: str = PROTOCOL_VERSION
    transports: list[str] = field(default_factory=list)
    capabilities: list[str] = field(default_factory=list)
    enabled: bool = True
    namespace: str = ""
    # 服务器别名 → 意图；意图正则命中后该 Bridge 的工具才进候选池
    intents: dict[str, str] = field(default_factory=dict)
    # 意图名 → 触发正则（中文话术），与 mcp/bridge._INTENT_PATTERNS 同构
    triggers: dict[str, str] = field(default_factory=dict)
    # 工具原始名 → 中文关键词，用于同服务器内的排序打分
    keywords: dict[str, str] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "BridgeManifest":
        raw = raw or {}
        return cls(
            id=sanitize_segment(raw.get("id")),
            name=str(raw.get("name") or ""),
            version=str(raw.get("version") or "0.0.0"),
            bridge_version=str(raw.get("bridge_version") or "0.0.0"),
            protocol_version=str(raw.get("protocol_version") or PROTOCOL_VERSION),
            transports=[str(t) for t in (raw.get("transports") or [])],
            capabilities=[str(c) for c in (raw.get("capabilities") or [])],
            enabled=bool(raw.get("enabled", True)),
            namespace=sanitize_segment(raw.get("namespace") or raw.get("id")),
            intents={str(k): str(v) for k, v in (raw.get("intents") or {}).items()},
            triggers={str(k): str(v) for k, v in (raw.get("triggers") or {}).items()},
            keywords={str(k): str(v) for k, v in (raw.get("keywords") or {}).items()},
            meta=dict(raw.get("meta") or {}),
        )

    def validate(self) -> tuple[list[str], list[str]]:
        errors: list[str] = []
        warnings: list[str] = []
        if not self.id:
            errors.append("manifest 缺少 id")
        if self.protocol_version != PROTOCOL_VERSION:
            errors.append(
                f"协议版本不兼容：{self.protocol_version}（本框架 {PROTOCOL_VERSION}）"
            )
        unknown_cap = [c for c in self.capabilities if c not in CAPABILITIES]
        if unknown_cap:
            errors.append(f"未知能力声明：{','.join(unknown_cap)}")
        unknown_tr = [t for t in self.transports if t not in TRANSPORTS]
        if unknown_tr:
            warnings.append(f"未知传输声明：{','.join(unknown_tr)}")
        if not self.capabilities:
            warnings.append("未声明任何能力")
        if self.intents and not self.triggers:
            warnings.append("声明了 intents 但无 triggers，将只能命中内置意图正则")
        if not self.name:
            warnings.append("未填写 name")
        return errors, warnings

    def supports(self, capability: str) -> bool:
        return capability in self.capabilities

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "bridge_version": self.bridge_version,
            "protocol_version": self.protocol_version,
            "transports": list(self.transports),
            "capabilities": list(self.capabilities),
            "enabled": self.enabled,
            "namespace": self.namespace,
            "intents": dict(self.intents),
            "triggers": dict(self.triggers),
            "keywords": dict(self.keywords),
        }


@dataclass
class BridgeEntry:
    manifest: BridgeManifest
    state: BridgeState = BridgeState.DISCOVERED
    error: str = ""
    last_ok_ts: float = 0.0
    reconnects: int = 0
    tools: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def available(self) -> bool:
        return self.manifest.enabled and self.state in _AVAILABLE_STATES

    def health(self) -> dict[str, Any]:
        return {
            "bridge_id": self.manifest.id,
            "status": self.state.value,
            "enabled": self.manifest.enabled,
            "transports": list(self.manifest.transports),
            "capabilities": list(self.manifest.capabilities),
            "connected": self.state == BridgeState.HEALTHY,
            "last_ok_ts": self.last_ok_ts,
            "reconnects": self.reconnects,
            "tool_count": len(self.tools),
            "last_error": self.error or None,
        }


class IntegrationRegistry:
    """线程安全的 Bridge 注册中心。"""

    def __init__(self) -> None:
        self._entries: dict[str, BridgeEntry] = {}
        self._lock = threading.RLock()
        self._errors: list[str] = []

    def register(self, manifest: BridgeManifest | dict) -> tuple[bool, list[str]]:
        """登记 Bridge。返回 (是否成功, 错误列表)。"""
        if isinstance(manifest, dict):
            manifest = BridgeManifest.from_dict(manifest)
        errors, warnings = manifest.validate()
        for warn in warnings:
            logger.warning("Bridge %s manifest 警告: %s", manifest.id, warn)
        if errors:
            with self._lock:
                self._errors.extend(f"{manifest.id}: {e}" for e in errors)
            return False, errors

        with self._lock:
            existing = self._entries.get(manifest.id)
            if existing is not None:
                # 幂等重注册：同版本视为刷新，不重置运行状态
                if existing.manifest.version == manifest.version:
                    existing.manifest = manifest
                    return True, []
                existing.manifest = manifest
                existing.state = BridgeState.DISCOVERED
                existing.tools.clear()
                logger.info(
                    "Bridge %s 升级注册 %s -> %s",
                    manifest.id,
                    existing.manifest.version,
                    manifest.version,
                )
                return True, []
            self._entries[manifest.id] = BridgeEntry(
                manifest=manifest,
                state=BridgeState.DISCOVERED if manifest.enabled else BridgeState.DISABLED,
            )
            return True, []

    def unregister(self, bridge_id: str) -> bool:
        """注销 Bridge，只清理自身登记项。"""
        with self._lock:
            return self._entries.pop(sanitize_segment(bridge_id), None) is not None

    def get(self, bridge_id: str) -> BridgeEntry | None:
        with self._lock:
            return self._entries.get(sanitize_segment(bridge_id))

    def list_bridges(self, *, state: BridgeState | None = None) -> list[BridgeEntry]:
        with self._lock:
            entries = list(self._entries.values())
        if state is not None:
            entries = [e for e in entries if e.state == state]
        return entries

    def set_state(
        self, bridge_id: str, state: BridgeState, *, error: str = "", ok: bool = False
    ) -> bool:
        with self._lock:
            entry = self._entries.get(sanitize_segment(bridge_id))
            if entry is None:
                return False
            if state == BridgeState.HEALTHY and entry.state != BridgeState.HEALTHY:
                entry.reconnects += 1
            entry.state = state
            entry.error = error
            if ok:
                entry.last_ok_ts = time.time()
            return True

    def register_tools(self, bridge_id: str, tools: list[dict[str, Any]]) -> int:
        """登记该 Bridge 当前可用工具（覆盖式，断线后可重新发现）。"""
        with self._lock:
            entry = self._entries.get(sanitize_segment(bridge_id))
            if entry is None:
                return 0
            index: dict[str, dict[str, Any]] = {}
            for tool in tools or []:
                exposed = str(tool.get("name") or "")
                if not exposed:
                    continue
                index[exposed] = dict(tool)
            entry.tools = index
            return len(index)

    def clear_tools(self, bridge_id: str) -> None:
        with self._lock:
            entry = self._entries.get(sanitize_segment(bridge_id))
            if entry is not None:
                entry.tools.clear()

    def available_tools(self, capability: str = "tools") -> dict[str, str]:
        """可用工具 → bridge_id。不可用/未声明该能力的 Bridge 工具不进池。"""
        out: dict[str, str] = {}
        with self._lock:
            for bridge_id, entry in self._entries.items():
                if not entry.available or not entry.manifest.supports(capability):
                    continue
                for exposed in entry.tools:
                    out.setdefault(exposed, bridge_id)
        return out

    def bridge_for_server(self, server: str) -> str | None:
        """按服务器别名/命名空间反查归属 Bridge。"""
        key = str(server or "").lower()
        if not key:
            return None
        with self._lock:
            for bridge_id, entry in self._entries.items():
                manifest = entry.manifest
                if key in {a.lower() for a in manifest.intents} or manifest.namespace.lower() == key:
                    return bridge_id
        return None

    def intents_for(self, server: str) -> str | None:
        """服务器别名 → 意图，供 tools_matching 替代硬编码名单。"""
        key = str(server or "").lower()
        with self._lock:
            for entry in self._entries.values():
                if not entry.available:
                    continue
                intents = entry.manifest.intents
                if key in intents:
                    return intents[key]
                if entry.manifest.namespace.lower() == key and intents:
                    return next(iter(intents.values()))
        return None

    def keyword_overrides(self) -> dict[str, str]:
        """合并所有可用 Bridge 声明的工具关键词。"""
        out: dict[str, str] = {}
        with self._lock:
            for entry in self._entries.values():
                if not entry.available:
                    continue
                for tool, keywords in entry.manifest.keywords.items():
                    out.setdefault(tool, keywords)
        return out

    def intent_overrides(self) -> dict[str, str]:
        """合并所有可用 Bridge 声明的服务器别名 → 意图，替代硬编码名单。"""
        out: dict[str, str] = {}
        with self._lock:
            for entry in self._entries.values():
                if not entry.available:
                    continue
                manifest = entry.manifest
                for alias, intent in manifest.intents.items():
                    out.setdefault(alias.lower(), intent)
                if manifest.namespace and manifest.intents:
                    out.setdefault(manifest.namespace.lower(), next(iter(manifest.intents.values())))
        return out

    def trigger_overrides(self) -> dict[str, str]:
        """合并所有可用 Bridge 声明的意图触发正则（字符串形式）。"""
        out: dict[str, str] = {}
        with self._lock:
            for entry in self._entries.values():
                if not entry.available:
                    continue
                for intent, pattern in entry.manifest.triggers.items():
                    out.setdefault(intent, pattern)
        return out

    def health(self) -> dict[str, Any]:
        with self._lock:
            entries = list(self._entries.values())
            errors = list(self._errors)
        return {
            "bridge_count": len(entries),
            "healthy": sum(1 for e in entries if e.state == BridgeState.HEALTHY),
            "available": sum(1 for e in entries if e.available),
            "bridges": [e.health() for e in entries],
            "errors": errors,
        }
