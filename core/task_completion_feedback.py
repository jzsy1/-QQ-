"""Durable, isolated consumer for terminal Supervisor task feedback."""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

from .event_bus import EventBus
from .task_store import TaskStore

logger = logging.getLogger(__name__)


class TaskCompletionFeedbackHook:
  """Consumes the TaskStore outbox off the Supervisor thread.

  The outbox is written before ``task.finished`` is published. On restart,
  pending and interrupted records are replayed. The unique task_id prevents
  duplicate queue rows. Delivery is at least once if a process crashes after
  handler side effects but before the processed marker is committed.
  """

  def __init__(self, event_bus: EventBus, task_store: TaskStore, handler: Callable[[dict[str, Any]], None]):
    self.event_bus = event_bus
    self.task_store = task_store
    self.handler = handler
    self._wake = threading.Event()
    self._stop = threading.Event()
    self.event_bus.subscribe("task.finished", self._on_finished)
    self._wake.set()
    self._thread = threading.Thread(target=self._run, name="task-feedback", daemon=True)
    self._thread.start()

  def _on_finished(self, _event_type: str, _payload: dict[str, Any]) -> None:
    self._wake.set()

  def _run(self) -> None:
    while not self._stop.is_set():
      self._wake.wait(timeout=1.0)
      self._wake.clear()
      try:
        items = self.task_store.pending_feedback()
      except Exception:
        logger.warning("读取任务反馈 outbox 失败", exc_info=True)
        continue
      for payload in items:
        if self._stop.is_set():
          break
        task_id = str(payload.get("task_id") or "")
        try:
          self.task_store.set_feedback_status(task_id, "processing")
          self.handler(payload)
          self.task_store.set_feedback_status(task_id, "processed")
        except Exception as exc:
          logger.warning("任务反馈处理失败 task_id=%s", task_id, exc_info=True)
          try:
            self.task_store.set_feedback_status(task_id, "failed", str(exc))
          except Exception:
            logger.warning("任务反馈重试状态写入失败 task_id=%s", task_id, exc_info=True)
          break

  def close(self, timeout: float = 2.0) -> None:
    self._stop.set()
    self._wake.set()
    self.event_bus.unsubscribe("task.finished", self._on_finished)
    if self._thread.is_alive():
      self._thread.join(timeout=max(0.0, timeout))

