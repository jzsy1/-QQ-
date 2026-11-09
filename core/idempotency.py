"""动作幂等键管理 — 防超时重试与并发击穿导致的重复副作用。

对齐 Universal Integration Framework §3.5/§9：
accepted 不等于 succeeded；超时后外部结果未知时必须返回 unknown，
禁止对有副作用的操作盲目重发。
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)

_NON_SEMANTIC_ARGS = ("idempotency_key", "timestamp", "session_id", "request_id")


class ActionState(str, Enum):
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
    UNKNOWN = "unknown"


@dataclass
class IdempotentRecord:
    key: str
    func: str
    args: dict[str, Any]
    state: ActionState = ActionState.EXECUTING
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    result: Any | None = None
    error: str = ""
    ttl: float = 60.0

    @property
    def is_expired(self) -> bool:
        return (time.time() - self.updated_at) > self.ttl

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "func": self.func,
            "state": self.state.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "error": self.error,
            "expired": self.is_expired,
        }


class IdempotencyManager:
    """线程安全的进程内幂等注册表。"""

    def __init__(
        self,
        default_ttl: float = 60.0,
        *,
        prefix: str = "act",
        max_records: int = 2000,
    ) -> None:
        self.default_ttl = default_ttl
        self.prefix = prefix
        self.max_records = max_records
        self._records: dict[str, IdempotentRecord] = {}
        self._lock = threading.Lock()

    def generate_key(
        self, func: str, args: dict[str, Any], prefix: str | None = None
    ) -> str:
        """按工具名 + 语义参数生成确定性幂等键；args 里显式给了就优先用。"""
        if args.get("idempotency_key"):
            return str(args["idempotency_key"])
        semantic = {
            k: v for k, v in (args or {}).items() if k not in _NON_SEMANTIC_ARGS
        }
        raw = f"{func}:{json.dumps(semantic, sort_keys=True, ensure_ascii=False, default=str)}"
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
        return f"{prefix or self.prefix}_{digest}"

    def check_and_acquire(
        self,
        func: str,
        args: dict[str, Any],
        *,
        key: str | None = None,
        ttl: float | None = None,
    ) -> tuple[bool, str, IdempotentRecord | None]:
        """原子占用一个幂等键。

        返回 (allowed, reason, record)，reason 取值：
        - allowed：首次下发，已置 EXECUTING
        - in_progress：同键仍在执行窗口内，拒绝并发重入
        - already_completed：同键已成功，直接复用历史结果
        - unknown：上一次执行超时/结果不明，禁止盲目重发，须先查外部状态
        """
        act_key = key or self.generate_key(func, args)
        effective_ttl = ttl if ttl is not None else self.default_ttl
        now = time.time()

        with self._lock:
            self._prune_locked()
            record = self._records.get(act_key)

            if record is not None:
                if record.state == ActionState.EXECUTING:
                    if record.is_expired:
                        record.state = ActionState.UNKNOWN
                        record.updated_at = now
                        logger.warning(
                            "幂等键 %s [%s] 执行超时，结果未知，拒绝自动重发",
                            func,
                            act_key,
                        )
                        return False, "unknown", record
                    logger.warning("动作重复触发拦截: %s [%s] 正在执行中", func, act_key)
                    return False, "in_progress", record
                if record.state == ActionState.UNKNOWN:
                    return False, "unknown", record
                if record.state == ActionState.COMPLETED and not record.is_expired:
                    logger.info("动作幂等命中: %s [%s] 已成功，跳过重复下发", func, act_key)
                    return False, "already_completed", record

            self._records[act_key] = IdempotentRecord(
                key=act_key,
                func=func,
                args=dict(args or {}),
                state=ActionState.EXECUTING,
                created_at=now,
                updated_at=now,
                ttl=effective_ttl,
            )
            return True, "allowed", self._records[act_key]

    def mark_completed(self, key: str, result: Any = None) -> None:
        self._transition(key, ActionState.COMPLETED, result=result)

    def mark_failed(self, key: str, error: str = "") -> None:
        """失败允许后续重试，因此直接释放键。"""
        with self._lock:
            self._records.pop(key, None)

    def mark_unknown(self, key: str, error: str = "") -> None:
        self._transition(key, ActionState.UNKNOWN, error=error)

    def resolve_unknown(self, key: str, *, succeeded: bool, result: Any = None) -> None:
        """外部状态查清后收口 unknown 记录。"""
        if succeeded:
            self.mark_completed(key, result)
        else:
            self.mark_failed(key, "resolved_as_failed")

    def get_record(self, key: str) -> IdempotentRecord | None:
        with self._lock:
            return self._records.get(key)

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [r.to_dict() for r in self._records.values()]

    def clear(self) -> None:
        with self._lock:
            self._records.clear()

    def _transition(
        self, key: str, state: ActionState, *, result: Any = None, error: str = ""
    ) -> None:
        with self._lock:
            record = self._records.get(key)
            if record is None:
                return
            record.state = state
            record.updated_at = time.time()
            if result is not None:
                record.result = result
            if error:
                record.error = error

    def _prune_locked(self) -> None:
        if len(self._records) <= self.max_records:
            return
        expired = [k for k, r in self._records.items() if r.is_expired]
        for key in expired:
            self._records.pop(key, None)


# 只对不可逆动作上锁。发消息、读文件这类合法重复会被 TTL 窗口吞掉，故不纳入。
GUARDED_FUNCS = frozenset({"delete_file", "run_shell"})
DEFAULT_TTL_SEC = 30.0


def should_guard(func: str, guarded: frozenset[str] | None = None) -> bool:
    """该工具是否需要幂等锁（默认白名单见 GUARDED_FUNCS）。"""
    return func in (GUARDED_FUNCS if guarded is None else guarded)


_default_manager: IdempotencyManager | None = None
_default_lock = threading.Lock()


def get_default_manager() -> IdempotencyManager:
    """进程内共享的幂等注册表，供 agent_loop / router 的 execute_with_critic 使用。"""
    global _default_manager
    if _default_manager is None:
        with _default_lock:
            if _default_manager is None:
                _default_manager = IdempotencyManager(default_ttl=DEFAULT_TTL_SEC)
    return _default_manager
