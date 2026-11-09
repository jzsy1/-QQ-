"""v2.0 统一执行结果协议 — 禁止未验证的假成功。"""

from __future__ import annotations

from typing import Any

from ..r1_executor import R1Response, STATUS_SUCCESS, r1_to_tool_output
from ..tools.execution_result import (
    ExecutionResult,
    can_report_success,
    from_tuple,
)

__all__ = [
    "ExecutionResult",
    "can_report_success",
    "from_r1_response",
    "from_tuple",
]


def from_r1_response(resp: R1Response) -> ExecutionResult:
    """将 R1 响应转为统一 ExecutionResult。"""
    out = r1_to_tool_output(resp)
    data = resp.data or {}
    verified = data.get("verified")
    if verified is None:
        verified = out.ok and resp.status == STATUS_SUCCESS
    error = ""
    if not out.ok:
        error = str(data.get("reason") or data.get("text") or out.text or "")
    return ExecutionResult(
        success=resp.status == STATUS_SUCCESS and out.ok,
        message=(out.text or "").strip(),
        verified=bool(verified),
        action=resp.func or "",
        target=str(
            (resp.args or {}).get("app_name")
            or (resp.args or {}).get("target")
            or (resp.args or {}).get("process")
            or ""
        ),
        error=error,
        extra={
            "r1_status": resp.status,
            "func": resp.func,
            "args": dict(resp.args or {}),
            **{k: v for k, v in data.items() if k not in ("text", "ok", "verified")},
        },
    )


def result_to_summary(result: ExecutionResult, *, max_len: int = 200) -> str:
    text = (result.message or result.error or "").strip()
    return text[:max_len]
