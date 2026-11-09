"""Event Engine — 授权后的定时与主动任务。"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

logger = logging.getLogger(__name__)


@dataclass
class EventRule:
    id: str
    event_type: str
    condition: dict = field(default_factory=dict)
    action: dict = field(default_factory=dict)
    enabled: bool = True
    last_run: str | None = None
    next_run: str | None = None


class EventEngine:
    """轻量事件调度：定时触发已授权动作。"""

    def __init__(self, *, poll_interval_sec: float = 30.0) -> None:
        self._rules: dict[str, EventRule] = {}
        self._handlers: dict[str, Callable[[EventRule], None]] = {}
        self._poll_interval = max(5.0, float(poll_interval_sec))
        self._running = False
        self._thread: threading.Thread | None = None

    def register_handler(self, event_type: str, handler: Callable[[EventRule], None]) -> None:
        self._handlers[event_type] = handler

    def add_rule(
        self,
        event_type: str,
        condition: dict,
        action: dict,
        *,
        enabled: bool = True,
    ) -> str:
        rid = uuid.uuid4().hex[:12]
        self._rules[rid] = EventRule(
            id=rid,
            event_type=event_type,
            condition=dict(condition or {}),
            action=dict(action or {}),
            enabled=enabled,
        )
        return rid

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="event-engine", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False

    def _loop(self) -> None:
        while self._running:
            now = datetime.now()
            for rule in list(self._rules.values()):
                if not rule.enabled:
                    continue
                if not self._is_due(rule, now):
                    continue
                handler = self._handlers.get(rule.event_type)
                if handler:
                    try:
                        handler(rule)
                        rule.last_run = now.isoformat()
                    except Exception:
                        logger.debug("event handler failed %s", rule.id, exc_info=True)
            time.sleep(self._poll_interval)

    @staticmethod
    def _is_due(rule: EventRule, now: datetime) -> bool:
        if not rule.next_run:
            return False
        try:
            due = datetime.fromisoformat(rule.next_run)
        except ValueError:
            return False
        return now >= due
