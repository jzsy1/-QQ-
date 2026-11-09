"""统一工具执行结果 — 禁止未验证的假成功。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ExecutionResult:
    success: bool
    message: str
    verified: bool = False
    action: str = ""
    target: str = ""
    error: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def metadata(self) -> dict[str, Any]:
        """v2.0 别名，与 extra 同义。"""
        return self.extra

    def to_dict(self) -> dict[str, Any]:
        out = {
            "success": self.success,
            "message": self.message,
            "verified": self.verified,
            "action": self.action,
            "target": self.target,
            **self.extra,
        }
        if self.error:
            out["error"] = self.error
        return out


def can_report_success(result: ExecutionResult) -> bool:
    return result.success and result.verified


def from_tuple(
    ok: bool,
    msg: str,
    *,
    verified: bool | None = None,
    action: str = "",
    target: str = "",
) -> ExecutionResult:
    """兼容旧式 tuple[bool, str] 返回值。"""
    if verified is None:
        verified = ok
    return ExecutionResult(
        success=ok,
        message=msg,
        verified=verified,
        action=action,
        target=target,
    )
