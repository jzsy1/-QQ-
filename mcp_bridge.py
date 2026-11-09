"""兼容 re-export — MCP 桥接已迁移到 kekesi/mcp/bridge.py。

本文件仅用于向后兼容现有 import（from kekesi.mcp_bridge import McpBridge）。
新代码请使用 from kekesi.mcp.bridge import McpBridge 或 from kekesi.mcp import McpBridge。

迁移日期：v2.0 目录结构调整
"""

from __future__ import annotations

from .mcp.bridge import McpBridge, default_config_path, load_config

__all__ = ["McpBridge", "default_config_path", "load_config"]
