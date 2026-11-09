"""可取消执行协议 — 统一 cancel_check 与 CancellationToken。"""

from __future__ import annotations

import threading
from typing import Callable

from .process_registry import cancel_task as cancel_task_processes


class CancellationToken:
    """线程安全的取消令牌。"""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def check(self) -> bool:
        return self.is_cancelled()

    def as_check(self) -> Callable[[], bool]:
        return self.check


def merge_cancel_checks(
    *checks: Callable[[], bool] | None,
) -> Callable[[], bool] | None:
    """合并多个取消检查；任一为真则取消。"""
    active = [c for c in checks if c is not None]
    if not active:
        return None

    def _merged() -> bool:
        return any(c() for c in active)

    return _merged


def cancel_with_processes(task_id: str, token: CancellationToken | None = None) -> int:
    """取消令牌 + 终止关联子进程。"""
    if token is not None:
        token.cancel()
    return cancel_task_processes(task_id)
