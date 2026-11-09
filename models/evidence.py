"""Evidence 模型 — 验证后的现实状态证据。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Evidence:
  """Verifier 返回的验证结果。"""

  verified: bool
  checks: list = field(default_factory=list)
  observed_state: dict = field(default_factory=dict)
  reason: str = ""

  @classmethod
  def from_execution(
    cls,
    *,
    verified: bool,
    checks: list | None = None,
    observed_state: dict | None = None,
    reason: str = "",
  ) -> Evidence:
    return cls(
      verified=verified,
      checks=list(checks or []),
      observed_state=dict(observed_state or {}),
      reason=reason,
    )

  @classmethod
  def failed(cls, reason: str = "execution failed") -> Evidence:
    return cls(verified=False, reason=reason)

  @classmethod
  def unknown(cls, reason: str = "cannot confirm state") -> Evidence:
    return cls(verified=False, reason=reason, observed_state={"state": "unknown"})
