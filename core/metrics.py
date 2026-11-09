"""Runtime 可观测性指标 — 方案 PART XVI。"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

_bound: "RuntimeMetrics | None" = None


@dataclass
class RuntimeMetrics:
    """进程内运行时指标（task / tool / verification）。"""

    tool_executions: int = 0
    tool_success: int = 0
    tool_verified: int = 0
    tool_failed: int = 0
    false_success_blocked: int = 0
    tasks_completed: int = 0
    tasks_failed: int = 0
    tasks_cancelled: int = 0
    tasks_unknown: int = 0
    tasks_timeout: int = 0
    recovery_attempts: int = 0
    recovery_success: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record_tool(
        self,
        *,
        tool: str,
        success: bool,
        verified: bool,
        claimed_success: bool | None = None,
    ) -> None:
        with self._lock:
            self.tool_executions += 1
            if success:
                self.tool_success += 1
            else:
                self.tool_failed += 1
            if verified:
                self.tool_verified += 1
            if claimed_success is None:
                claimed_success = success
            if claimed_success and not verified:
                self.false_success_blocked += 1

    def record_task(self, status: str) -> None:
        with self._lock:
            key = (status or "").lower()
            if key == "success":
                self.tasks_completed += 1
            elif key == "cancelled":
                self.tasks_cancelled += 1
            elif key == "timeout":
                self.tasks_timeout += 1
            elif key == "unknown":
                self.tasks_unknown += 1
            else:
                self.tasks_failed += 1

    def record_recovery(self, *, success: bool) -> None:
        with self._lock:
            self.recovery_attempts += 1
            if success:
                self.recovery_success += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            execs = max(1, self.tool_executions)
            tasks = (
                self.tasks_completed
                + self.tasks_failed
                + self.tasks_cancelled
                + self.tasks_unknown
                + self.tasks_timeout
            )
            return {
                "tool_executions": self.tool_executions,
                "tool_success": self.tool_success,
                "tool_verified": self.tool_verified,
                "tool_failed": self.tool_failed,
                "false_success_blocked": self.false_success_blocked,
                "tool_success_rate": round(self.tool_success / execs, 4),
                "verification_success_rate": round(
                    self.tool_verified / execs, 4
                ),
                "false_success_rate": round(
                    self.false_success_blocked / execs, 4
                ),
                "tasks_completed": self.tasks_completed,
                "tasks_failed": self.tasks_failed,
                "tasks_cancelled": self.tasks_cancelled,
                "tasks_unknown": self.tasks_unknown,
                "tasks_timeout": self.tasks_timeout,
                "task_success_rate": round(
                    self.tasks_completed / max(1, tasks), 4
                ),
                "recovery_attempts": self.recovery_attempts,
                "recovery_success": self.recovery_success,
                "recovery_rate": round(
                    self.recovery_success / max(1, self.recovery_attempts),
                    4,
                ),
            }


def get_runtime_metrics() -> RuntimeMetrics:
    global _bound
    if _bound is None:
        _bound = RuntimeMetrics()
    return _bound


def bind_runtime_metrics(metrics: RuntimeMetrics) -> None:
    global _bound
    _bound = metrics
