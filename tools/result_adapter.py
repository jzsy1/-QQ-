"""工具结果兼容层 — tuple / ExecutionResult 统一。"""

from __future__ import annotations

from .execution_result import ExecutionResult, can_report_success, from_tuple


def normalize_result(
    value: ExecutionResult | tuple[bool, str] | dict,
    *,
    action: str = "",
    target: str = "",
    verified: bool | None = None,
) -> ExecutionResult:
    """将旧式返回值规范为 ExecutionResult。"""
    if isinstance(value, ExecutionResult):
        return value
    if isinstance(value, tuple) and len(value) >= 2:
        return from_tuple(
            bool(value[0]),
            str(value[1]),
            verified=verified,
            action=action,
            target=target,
        )
    if isinstance(value, dict):
        ok = bool(value.get("ok", value.get("success", False)))
        msg = str(value.get("message") or value.get("text") or "")
        ver = value.get("verified")
        if ver is None:
            ver = ok
        return ExecutionResult(
            success=ok,
            message=msg,
            verified=bool(ver),
            action=action or str(value.get("action") or ""),
            target=target or str(value.get("target") or ""),
            error=str(value.get("error") or ""),
            extra={k: v for k, v in value.items() if k not in ("ok", "success", "message", "text", "verified")},
        )
    return ExecutionResult(
        success=False,
        message="invalid result",
        verified=False,
        action=action,
        target=target,
    )


def to_tool_output(result: ExecutionResult):
    """ExecutionResult → ToolOutput（供 executor 使用）。"""
    from .executor import ToolOutput

    ok = can_report_success(result) if result.action else result.success
    if result.action and result.verified is False and result.success:
        ok = False
    text = result.message or result.error or ""
    return ToolOutput(ok, text)
