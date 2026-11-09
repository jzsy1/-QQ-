"""Tool Gateway — 把 MCP / Skill / Bridge 工具统一成一张带归属的索引。

关键约束：**不改现有候选筛选算法**。
`mcp/bridge.py::tools_matching` 的硬编码服务器名单与关键词表继续作为默认，
本网关只把 Bridge Manifest 里声明的 intents / triggers / keywords 注入为覆盖项。
没有 Bridge 注册时，行为与改造前逐字节一致。
"""

from __future__ import annotations

import logging
from typing import Any

from .registry import IntegrationRegistry

logger = logging.getLogger(__name__)

_MCP_PREFIX = "mcp__"
_SKILL_PREFIX = "skill__"


def _server_of(full_name: str) -> str:
    parts = str(full_name or "").split("__")
    return parts[1] if len(parts) >= 3 else ""


class ToolGateway:
    """统一工具索引 + 候选池覆盖注入。"""

    def __init__(self, registry: IntegrationRegistry) -> None:
        self.registry = registry

    def attach(self, mcp_bridge: Any) -> None:
        """把 registry 的声明注入 MCP 桥的候选筛选。桥未提供覆盖点时静默跳过。"""
        if mcp_bridge is None:
            return
        intents = self.registry.intent_overrides()
        triggers = self.registry.trigger_overrides()
        keywords = self.registry.keyword_overrides()
        for attr, values in (
            ("intent_overrides", intents),
            ("trigger_overrides", triggers),
            ("keyword_overrides", keywords),
        ):
            slot = getattr(mcp_bridge, attr, None)
            if isinstance(slot, dict):
                slot.update(values)
        if intents or triggers or keywords:
            logger.info(
                "ToolGateway 注入覆盖：intents=%d triggers=%d keywords=%d",
                len(intents),
                len(triggers),
                len(keywords),
            )

    def detach(self, mcp_bridge: Any) -> None:
        """撤销注入（Bridge 注销时用），只清自己注入过的键。"""
        if mcp_bridge is None:
            return
        for attr, values in (
            ("intent_overrides", self.registry.intent_overrides()),
            ("trigger_overrides", self.registry.trigger_overrides()),
            ("keyword_overrides", self.registry.keyword_overrides()),
        ):
            slot = getattr(mcp_bridge, attr, None)
            if isinstance(slot, dict):
                for key in values:
                    slot.pop(key, None)

    def index_mcp_tools(self, mcp_bridge: Any) -> int:
        """把 MCP 桥已发现的工具按服务器归属登记进 registry。

        没有对应 Bridge 声明的服务器不会凭空建条目——它们继续走原有硬编码路径。
        """
        if mcp_bridge is None:
            return 0
        names = list(getattr(mcp_bridge, "tool_names", None) or [])
        schemas = {
            str(s.get("name")): s
            for s in (getattr(mcp_bridge, "_schemas", None) or [])
            if isinstance(s, dict)
        }
        buckets: dict[str, list[dict[str, Any]]] = {}
        for full in names:
            if not full.startswith(_MCP_PREFIX):
                continue
            bridge_id = self.registry.bridge_for_server(_server_of(full))
            if bridge_id is None:
                continue
            schema = schemas.get(full) or {}
            buckets.setdefault(bridge_id, []).append(
                {
                    "name": full,
                    "raw_name": full.split("__", 2)[-1],
                    "description": schema.get("description") or "",
                    "input_schema": schema.get("input_schema") or {},
                    "transport": "mcp",
                }
            )
        total = 0
        for bridge_id, tools in buckets.items():
            total += self.registry.register_tools(bridge_id, tools)
        return total

    def index_skill_tools(self, skill_bridge: Any) -> int:
        """把技能桥暴露的工具登记进 registry（技能 id 即命名空间）。"""
        if skill_bridge is None:
            return 0
        exposed = getattr(skill_bridge, "tool_names", None)
        if exposed is None:
            exposed = list(getattr(skill_bridge, "tools", None) or {})
        total = 0
        buckets: dict[str, list[dict[str, Any]]] = {}
        for full in list(exposed or []):
            if not str(full).startswith(_SKILL_PREFIX):
                continue
            body = str(full)[len(_SKILL_PREFIX) :]
            parts = body.split("__")
            if len(parts) < 3:
                continue
            owner = "/".join(parts[:-1])
            bridge_id = self.registry.bridge_for_server(owner) or self.registry.bridge_for_server(
                parts[-2]
            )
            if bridge_id is None:
                continue
            buckets.setdefault(bridge_id, []).append(
                {"name": full, "raw_name": parts[-1], "transport": "skill"}
            )
        for bridge_id, tools in buckets.items():
            total += self.registry.register_tools(bridge_id, tools)
        return total

    def unified_tools(self) -> list[dict[str, Any]]:
        """统一工具视图：provider / tool_id / capabilities / risk。

        risk 复用 security/policy 的既有分级，不另立一套。
        """
        from ..security.policy import risk_for_tool

        out: list[dict[str, Any]] = []
        for entry in self.registry.list_bridges():
            if not entry.available:
                continue
            for exposed, tool in entry.tools.items():
                out.append(
                    {
                        "provider": entry.manifest.id,
                        "tool_id": exposed,
                        "raw_name": tool.get("raw_name") or exposed,
                        "transport": tool.get("transport") or "",
                        "description": tool.get("description") or "",
                        "capabilities": list(entry.manifest.capabilities),
                        "risk": risk_for_tool(exposed).name.lower(),
                        "enabled": True,
                    }
                )
        return out

    def candidates(self, mcp_bridge: Any, user_text: str, *, max_n: int | None = None) -> list[str]:
        """候选工具筛选仍委托给现有实现，本网关不重写算法。"""
        if mcp_bridge is None:
            return []
        kwargs = {} if max_n is None else {"max_n": max_n}
        return list(mcp_bridge.tools_matching(user_text or "", **kwargs))
