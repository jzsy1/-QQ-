"""Universal Integration Framework — 外部系统统一接入层。

主系统只面向这里的契约，不直接依赖任何具体 Bridge。
现有 MCP / Skill 链路保持不变；本层在其上补注册中心、事件网关与幂等。
"""

from .config import BridgeConfig, IntegrationsConfig, TransportConfig, load_bridges, load_config
from .event_gateway import EventGateway, IntegrationEvent, Subscription
from .registry import (
    CAPABILITIES,
    TRANSPORTS,
    BridgeEntry,
    BridgeManifest,
    BridgeState,
    IntegrationRegistry,
)
from .tool_gateway import ToolGateway

__all__ = [
    "BridgeConfig",
    "BridgeEntry",
    "BridgeManifest",
    "BridgeState",
    "CAPABILITIES",
    "EventGateway",
    "IntegrationEvent",
    "IntegrationRegistry",
    "IntegrationsConfig",
    "Subscription",
    "TRANSPORTS",
    "ToolGateway",
    "TransportConfig",
    "load_bridges",
    "load_config",
]
