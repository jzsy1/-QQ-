"""Event Gateway — 外部系统事件的接收、去重、过滤、限流与重放。

职责边界：只负责把事件可靠地送给订阅者。
**是否因此触发一次 LLM 思考由 Kekesi 调度层决定**，本模块不做任何 Agent 调用。

补齐 core/event_bus.py 缺的部分：过滤、环形缓冲、去重、顺序/缺口检测、
背压限流、Last-Event-ID 重放。EventBus 仍可作为下游 sink 复用。
"""

from __future__ import annotations

import itertools
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

logger = logging.getLogger(__name__)

SEVERITIES = ("debug", "info", "warning", "error", "critical")
_SEVERITY_RANK = {name: idx for idx, name in enumerate(SEVERITIES)}

EventHandler = Callable[["IntegrationEvent"], None]

_event_ids = itertools.count(1)


@dataclass
class IntegrationEvent:
    """统一事件结构，字段对齐设计文档 §3.3。"""

    provider: str
    type: str
    data: dict[str, Any] = field(default_factory=dict)
    event_id: str = ""
    timestamp: float = field(default_factory=time.time)
    seq: int = 0
    severity: str = "info"

    def __post_init__(self) -> None:
        if not self.event_id:
            self.event_id = f"evt_{next(_event_ids):06d}"
        if self.severity not in _SEVERITY_RANK:
            self.severity = "info"

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "IntegrationEvent":
        raw = raw or {}
        timestamp = raw.get("timestamp")
        if isinstance(timestamp, str):
            timestamp = _parse_iso(timestamp)
        return cls(
            provider=str(raw.get("provider") or ""),
            type=str(raw.get("type") or raw.get("event_type") or ""),
            data=dict(raw.get("data") or {}),
            event_id=str(raw.get("event_id") or raw.get("id") or ""),
            timestamp=float(timestamp or time.time()),
            seq=int(raw.get("seq") or 0),
            severity=str(raw.get("severity") or "info"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "provider": self.provider,
            "type": self.type,
            "timestamp": self.timestamp,
            "seq": self.seq,
            "severity": self.severity,
            "data": self.data,
        }


def _parse_iso(value: str) -> float:
    try:
        from datetime import datetime

        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return time.time()


@dataclass
class Subscription:
    token: int
    handler: EventHandler
    providers: frozenset[str] | None = None
    types: frozenset[str] | None = None
    type_prefixes: tuple[str, ...] = ()
    min_severity: str = "debug"
    min_interval_sec: float = 0.0
    _last_fired: dict[str, float] = field(default_factory=dict, repr=False)

    def matches(self, event: IntegrationEvent) -> bool:
        if self.providers is not None and event.provider not in self.providers:
            return False
        if _SEVERITY_RANK[event.severity] < _SEVERITY_RANK[self.min_severity]:
            return False
        if self.types is None and not self.type_prefixes:
            return True
        if self.types is not None and event.type in self.types:
            return True
        return any(event.type.startswith(p) for p in self.type_prefixes)

    def allow_by_rate(self, event: IntegrationEvent) -> bool:
        """按 (订阅, 事件类型) 限流，防游戏 Tick 级事件打爆下游。"""
        if self.min_interval_sec <= 0:
            return True
        key = f"{event.provider}:{event.type}"
        now = time.monotonic()
        last = self._last_fired.get(key)
        if last is not None and (now - last) < self.min_interval_sec:
            return False
        self._last_fired[key] = now
        return True


class EventGateway:
    """线程安全的事件网关。"""

    def __init__(
        self,
        *,
        buffer_size: int = 512,
        dedupe_window: int = 1024,
        bus: Any | None = None,
    ) -> None:
        self._buffer: deque[IntegrationEvent] = deque(maxlen=max(1, buffer_size))
        self._dedupe: deque[str] = deque(maxlen=max(1, dedupe_window))
        self._seen: set[str] = set()
        self._subs: dict[int, Subscription] = {}
        self._next_token = itertools.count(1)
        self._lock = threading.RLock()
        self._bus = bus
        self._last_seq: dict[str, int] = {}
        self._counters = {
            "ingested": 0,
            "delivered": 0,
            "duplicate": 0,
            "filtered": 0,
            "rate_limited": 0,
            "dropped": 0,
            "gaps": 0,
        }

    def subscribe(
        self,
        handler: EventHandler,
        *,
        providers: Iterable[str] | None = None,
        types: Iterable[str] | None = None,
        type_prefixes: Iterable[str] = (),
        min_severity: str = "debug",
        min_interval_sec: float = 0.0,
    ) -> int:
        """订阅事件，返回可用于 unsubscribe 的 token。

        providers / types 都为 None 表示订阅全部；type_prefixes 支持
        "duplicant." 这类前缀通配。
        """
        with self._lock:
            token = next(self._next_token)
            self._subs[token] = Subscription(
                token=token,
                handler=handler,
                providers=frozenset(providers) if providers is not None else None,
                types=frozenset(types) if types is not None else None,
                type_prefixes=tuple(type_prefixes),
                min_severity=min_severity,
                min_interval_sec=min_interval_sec,
            )
            return token

    def unsubscribe(self, token: int) -> bool:
        with self._lock:
            return self._subs.pop(token, None) is not None

    def ingest(self, raw: dict[str, Any] | IntegrationEvent) -> str:
        """接收一条事件，返回处理结果码。

        delivered / duplicate / filtered / rate_limited / dropped / rejected
        """
        event = raw if isinstance(raw, IntegrationEvent) else IntegrationEvent.from_dict(raw)
        if not event.provider or not event.type:
            self._bump("filtered")
            return "rejected"

        with self._lock:
            self._bump("ingested")
            if event.event_id in self._seen:
                self._bump("duplicate")
                return "duplicate"
            self._detect_gap_locked(event)
            self._seen.add(event.event_id)
            self._dedupe.append(event.event_id)
            if len(self._seen) > len(self._dedupe):
                self._seen = set(self._dedupe)
            if len(self._buffer) == self._buffer.maxlen:
                self._bump("dropped")
            self._buffer.append(event)
            subs = list(self._subs.values())

        matched = 0
        fired = 0
        for sub in subs:
            if not sub.matches(event):
                continue
            matched += 1
            if not sub.allow_by_rate(event):
                self._bump("rate_limited")
                continue
            self._dispatch(sub, event)
            fired += 1

        if fired:
            return "delivered"
        if matched:
            return "rate_limited"
        self._bump("filtered")
        return "filtered"

    def replay(
        self,
        *,
        last_event_id: str | None = None,
        provider: str | None = None,
        limit: int = 100,
    ) -> list[IntegrationEvent]:
        """断线重连后从 Last-Event-ID 之后重放缓冲区里的事件。"""
        with self._lock:
            events = list(self._buffer)
        if provider:
            events = [e for e in events if e.provider == provider]
        if last_event_id:
            index = next(
                (i for i, e in enumerate(events) if e.event_id == last_event_id), None
            )
            # 找不到说明已被环形缓冲淘汰，只能全量重放并让调用方自己去重
            events = events[index + 1 :] if index is not None else events
        return events[-max(0, limit) :]

    def last_event_id(self, provider: str | None = None) -> str:
        with self._lock:
            events = list(self._buffer)
        for event in reversed(events):
            if provider is None or event.provider == provider:
                return event.event_id
        return ""

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "buffered": len(self._buffer),
                "buffer_size": self._buffer.maxlen,
                "subscribers": len(self._subs),
                "last_seq": dict(self._last_seq),
                **self._counters,
            }

    def clear(self) -> None:
        with self._lock:
            self._buffer.clear()
            self._dedupe.clear()
            self._seen.clear()
            self._last_seq.clear()

    def _dispatch(self, sub: Subscription, event: IntegrationEvent) -> None:
        try:
            sub.handler(event)
            self._bump("delivered")
        except Exception:
            logger.debug("事件订阅者处理失败 %s/%s", event.provider, event.type, exc_info=True)
        if self._bus is not None:
            try:
                self._bus.publish(f"integration.{event.provider}.{event.type}", event.to_dict())
            except Exception:
                logger.debug("事件转发 EventBus 失败", exc_info=True)

    def _detect_gap_locked(self, event: IntegrationEvent) -> None:
        if event.seq <= 0:
            return
        last = self._last_seq.get(event.provider)
        if last is not None and event.seq > last + 1:
            self._bump("gaps")
            logger.warning(
                "事件序号缺口 %s: %d -> %d", event.provider, last, event.seq
            )
        if last is None or event.seq > last:
            self._last_seq[event.provider] = event.seq

    def _bump(self, key: str) -> None:
        with self._lock:
            self._counters[key] = self._counters.get(key, 0) + 1
