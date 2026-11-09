"""通用的 Streamable HTTP MCP 客户端（基于 Python 标准库，零第三方依赖）。

支持：
1. Streamable HTTP 与 SSE (Server-Sent Events) 响应解析
2. MCP Session 状态追踪 (mcp-session-id 请求头自动传递与更新)
3. Bearer Token 认证
4. JSON-RPC 2.0 异步调用 (run_in_executor 桥接标准库 urllib)
5. tools/list 与 tools/call 标准 MCP 协议，支持结构化内容与文本内容解包
6. 属性访问包装器 AttrObj（兼容官方 MCP ClientSession 属性访问习惯）
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class AttrObj:
    """通用属性访问包装器，同时支持属性访问与字典下标/方法访问。"""

    data: dict

    def __getattr__(self, key: str) -> Any:
        if key == "data":
            return self.__dict__.get("data", {})
        data = self.__dict__.get("data")
        if isinstance(data, dict):
            return data.get(key)
        return None

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def __contains__(self, key: object) -> bool:
        return key in self.data if isinstance(self.data, dict) else False

    def get(self, key: str, default: Any = None) -> Any:
        if isinstance(self.data, dict):
            return self.data.get(key, default)
        return default

    def to_dict(self) -> dict:
        return self.data if isinstance(self.data, dict) else {}


class StdlibMcpSession:
    """未安装 mcp 包或需要轻量 Streamable HTTP 时的零依赖客户端。"""

    def __init__(
        self,
        url: str,
        token: str = "",
        *,
        client_name: str = "kekesi-mcp-client",
        client_version: str = "0.1.0",
        default_timeout: float = 65.0,
    ):
        self.url = url.rstrip("/")
        self.token = token.strip() if token else ""
        self.session_id: str = ""
        self.client_name = client_name
        self.client_version = client_version
        self.default_timeout = default_timeout
        self._id = 0

    def _post_sync(self, payload: dict, timeout: float = 65.0) -> dict | None:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.session_id:
            headers["mcp-session-id"] = self.session_id

        req = urllib.request.Request(
            self.url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                for k, v in resp.headers.items():
                    if k.lower() == "mcp-session-id" and v:
                        self.session_id = v.strip()
                if resp.status == 202:
                    return None
                ctype = (resp.headers.get("Content-Type") or "").lower()
                raw = resp.read().decode("utf-8", errors="replace").strip()
                if not raw:
                    return None
                if "text/event-stream" in ctype:
                    last_msg = None
                    for line in raw.splitlines():
                        line = line.strip()
                        if line.startswith("data:"):
                            chunk = line[5:].strip()
                            if chunk:
                                try:
                                    last_msg = json.loads(chunk)
                                except json.JSONDecodeError:
                                    continue
                    return last_msg
                return json.loads(raw)
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else ""
            logger.warning("MCP HTTP Error %s: %s | Body: %s", e.code, e.reason, err_body[:200])
            try:
                return json.loads(err_body)
            except Exception:
                raise RuntimeError(f"MCP HTTP {e.code} 错误: {e.reason}") from e

    async def _rpc(self, method: str, params: dict | None = None, timeout: float | None = None) -> dict:
        self._id += 1
        req_id = self._id
        payload = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}
        eff_timeout = timeout if timeout is not None else self.default_timeout
        loop = asyncio.get_running_loop()
        resp = await loop.run_in_executor(None, self._post_sync, payload, eff_timeout)
        if not isinstance(resp, dict):
            raise RuntimeError(f"MCP {method} 返回为空")
        if "error" in resp and resp["error"]:
            raise RuntimeError(f"MCP {method} 错误: {resp['error']}")
        return resp.get("result") or {}

    async def initialize(
        self,
        capabilities: dict | None = None,
        client_info: dict | None = None,
        protocol_version: str = "2025-03-26",
        timeout: float = 20.0,
    ) -> None:
        """执行 MCP initialize 握手并发送 notifications/initialized。"""
        await self._rpc(
            "initialize",
            {
                "protocolVersion": protocol_version,
                "capabilities": capabilities or {},
                "clientInfo": client_info or {"name": self.client_name, "version": self.client_version},
            },
            timeout=timeout,
        )
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            self._post_sync,
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            10.0,
        )

    async def list_tools(self, timeout: float = 20.0) -> AttrObj:
        """列出服务端所有工具。"""
        res = await self._rpc("tools/list", {}, timeout=timeout)
        tools = [AttrObj(t) for t in (res.get("tools") or []) if isinstance(t, dict)]
        return AttrObj({"tools": tools})

    async def call_tool(self, name: str, arguments: dict | None = None, timeout: float | None = None) -> AttrObj:
        """调用 MCP 工具，返回包装后的 AttrObj。"""
        eff_timeout = timeout if timeout is not None else self.default_timeout
        res = await self._rpc(
            "tools/call",
            {"name": name, "arguments": arguments or {}},
            timeout=eff_timeout,
        )
        content = [AttrObj(c) for c in (res.get("content") or []) if isinstance(c, dict)]
        return AttrObj({
            "structuredContent": res.get("structuredContent"),
            "content": content,
            "isError": res.get("isError", False),
        })

    async def ping(self, timeout: float = 5.0) -> bool:
        """探活。"""
        try:
            await self._rpc("ping", {}, timeout=timeout)
            return True
        except Exception:
            return False


# 别名导出
StreamableHttpMcpClient = StdlibMcpSession
