"""MCP 错误码 — 区分启动/Schema/调用/崩溃。"""

from __future__ import annotations


class McpErrorCode:
    SERVER_START_FAILED = "SERVER_START_FAILED"
    SCHEMA_FAILED = "SCHEMA_FAILED"
    CALL_TIMEOUT = "CALL_TIMEOUT"
    CALL_FAILED = "CALL_FAILED"
    SERVER_CRASHED = "SERVER_CRASHED"
    SHUTDOWN = "SHUTDOWN"
    NOT_FOUND = "NOT_FOUND"
    # 传输成功但服务端返回 isError=true：工具自身报告的业务失败
    TOOL_ERROR = "TOOL_ERROR"


def format_mcp_error(code: str, detail: str = "") -> str:
    prefix = f"[{code}]"
    return f"{prefix} {detail}".strip() if detail else prefix
