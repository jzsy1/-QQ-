"""只读查询短缓存 — 减少高频只读工具的重复 PowerShell 冷启动开销。

仅用于无副作用的查询类工具（系统状态、进程/窗口/服务列表等）。
写操作、带破坏性的工具绝不可缓存。缓存命中 <1ms；TTL 内同一参数直接复用。
线程安全，适配工具守护线程并发调用。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

_store: dict[str, tuple[Any, float]] = {}
_lock = threading.Lock()


def cached_call(
    key: str,
    ttl: float,
    fn: Callable[[], Any],
    cache_if: Callable[[Any], bool] | None = None,
) -> Any:
    """带 TTL 的只读调用缓存。

    key: 缓存键（带参数的工具需把参数拼进 key，避免串味）。
    ttl: 有效期（秒）。
    fn: 实际执行函数（无参可调用，返回值会被缓存）。
    cache_if: 可选判定；返回 False 时不缓存本次结果（如失败的查询）。
    """
    now = time.time()
    with _lock:
        hit = _store.get(key)
        if hit is not None and now - hit[1] < ttl:
            return hit[0]
    value = fn()
    if cache_if is None or cache_if(value):
        with _lock:
            _store[key] = (value, time.time())
    return value


def invalidate(prefix: str = "") -> None:
    """清除缓存。prefix 为空清全部，否则清除以 prefix 开头的键。"""
    with _lock:
        if not prefix:
            _store.clear()
            return
        for k in [k for k in _store if k.startswith(prefix)]:
            _store.pop(k, None)
