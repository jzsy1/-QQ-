"""RuntimeExecutionResult — 与 tools.ExecutionResult 对齐的运行时结果。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..tools.execution_result import ExecutionResult, can_report_success


@dataclass
class RuntimeExecutionResult:
  """统一执行结果（v2.0 规范）。"""

  success: bool
  status: str
  message: str = ""
  data: Any | None = None
  error: str | None = None
  exit_code: int | None = None
  duration_ms: int | None = None
  verification_required: bool = True
  evidence: dict = field(default_factory=dict)

  @property
  def verified(self) -> bool:
    return bool(self.evidence.get("verified"))

  def can_report_success(self) -> bool:
    if not self.success:
      return False
    if self.verification_required:
      return self.verified
    return True

  @classmethod
  def from_tool_result(cls, result: ExecutionResult, *, status: str = "ok") -> RuntimeExecutionResult:
    return cls(
      success=result.success,
      status=status if result.success else "error",
      message=result.message,
      error=result.error or None,
      verification_required=True,
      evidence={"verified": result.verified, **result.extra},
    )

  @classmethod
  def timeout(cls, message: str = "tool timeout") -> RuntimeExecutionResult:
    return cls(success=False, status="timeout", message=message, error=message)

  @classmethod
  def unknown(cls, message: str = "tool result unknown") -> RuntimeExecutionResult:
    # UNKNOWN_GATE_V2_4_3: structured unknown marker, distinct from generic failure.
    return cls(
      success=False,
      status="unknown",
      message=message,
      error=message,
      verification_required=True,
      evidence={"verified": False, "unknown": True},
    )

  @property
  def is_unknown(self) -> bool:
    # AUTHOR_TODOS_4_3_4_4_V1: include 'timeout' in is_unknown.
    return self.status in ("timeout", "unknown") or bool(
      isinstance(self.evidence, dict) and self.evidence.get("unknown")
    )

  @classmethod
  def from_error(cls, message: str) -> RuntimeExecutionResult:
    return cls(success=False, status="error", message=message, error=message)


__all__ = ["RuntimeExecutionResult", "ExecutionResult", "can_report_success"]
