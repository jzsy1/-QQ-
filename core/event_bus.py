"""事件总线 — 模块间解耦通信。"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any, Callable

logger = logging.getLogger(__name__)

EventHandler = Callable[[str, dict[str, Any]], None]


class EventBus:
  """轻量发布/订阅事件总线。"""

  def __init__(self) -> None:
    self._handlers: dict[str, list[EventHandler]] = defaultdict(list)

  def subscribe(self, event_type: str, handler: EventHandler) -> None:
    self._handlers[event_type].append(handler)

  def unsubscribe(self, event_type: str, handler: EventHandler) -> None:
    handlers = self._handlers.get(event_type, [])
    if handler in handlers:
      handlers.remove(handler)

  def publish(self, event_type: str, payload: dict[str, Any] | None = None) -> None:
    data = dict(payload or {})
    for handler in list(self._handlers.get(event_type, [])):
      try:
        handler(event_type, data)
      except Exception:
        logger.debug("EventBus handler failed: %s", event_type, exc_info=True)

  def clear(self) -> None:
    self._handlers.clear()
