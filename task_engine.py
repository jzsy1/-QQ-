"""任务引擎 — 统一定时提醒与到期动作执行（Task Engine）。"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Callable

from .memory.db import Database
from .task_scheduler import ScheduledTask, TaskScheduler

logger = logging.getLogger(__name__)

_EXECUTE_META_PREFIX = "__execute__:"


class TaskState(str, Enum):
    """定时任务状态（scheduled_tasks 表，保持 legacy 值）。"""

    CREATED = "created"
    PLANNING = "planning"
    PENDING = "pending"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    RUNNING = "executing"
    SUCCESS = "completed"


_TERMINAL_STATES = frozenset({
    TaskState.COMPLETED,
    TaskState.FAILED,
    TaskState.CANCELLED,
})


@dataclass
class ExecutePayload:
    func: str
    args: dict
    max_retries: int = 0

    def to_description(self, note: str = "") -> str:
        body = {
            "func": self.func,
            "args": self.args,
            "max_retries": self.max_retries,
            "note": (note or "").strip(),
        }
        return _EXECUTE_META_PREFIX + json.dumps(body, ensure_ascii=False)

    @classmethod
    def from_description(cls, description: str) -> ExecutePayload | None:
        text = (description or "").strip()
        if not text.startswith(_EXECUTE_META_PREFIX):
            return None
        try:
            data = json.loads(text[len(_EXECUTE_META_PREFIX):])
        except json.JSONDecodeError:
            return None
        func = str(data.get("func") or "").strip()
        if not func:
            return None
        args = data.get("args") or {}
        if not isinstance(args, dict):
            args = {}
        return cls(
            func=func,
            args=dict(args),
            max_retries=int(data.get("max_retries") or 0),
        )


class TaskEngine:
    """扩展 TaskScheduler：reminder 播报 + execute 走 R1→Verifier。"""

    def __init__(
        self,
        db: Database,
        *,
        poll_interval_sec: int = 60,
        max_retries: int = 0,
    ):
        self.scheduler = TaskScheduler(db, poll_interval_sec=poll_interval_sec)
        self.max_retries = max(0, int(max_retries))
        self._execute_fn: Callable[[ScheduledTask, str, dict], tuple[bool, str]] | None = None
        self._observer: Any | None = None
        self._reminder_fn: Callable[[ScheduledTask], None] | None = None

    def set_execute_fn(
        self,
        fn: Callable[[ScheduledTask, str, dict], tuple[bool, str]],
    ) -> None:
        self._execute_fn = fn

    def set_reminder_fn(self, fn: Callable[[ScheduledTask], None]) -> None:
        self._reminder_fn = fn

    def set_observer(self, observer) -> None:
        self._observer = observer

    def start(self) -> None:
        self.scheduler.set_dispatch_callback(self._dispatch)
        self.scheduler.start()

    def stop(self) -> None:
        self.scheduler.stop()

    def transition(
        self,
        task_id: int,
        state: TaskState | str,
        *,
        last_error: str = "",
        verified: bool | None = None,
    ) -> bool:
        value = state.value if isinstance(state, TaskState) else str(state)
        return self.scheduler.update_task_state(
            task_id,
            value,
            last_error=last_error,
            verified=verified,
        )

    def schedule_reminder(
        self,
        title: str,
        scheduled_time: datetime,
        description: str = "",
    ) -> int:
        task_id = self.scheduler.add_task(
            title=title,
            scheduled_time=scheduled_time,
            description=description,
            task_type="reminder",
        )
        self.transition(task_id, TaskState.PENDING)
        return task_id

    def schedule_action(
        self,
        title: str,
        scheduled_time: datetime,
        func: str,
        args: dict | None = None,
        *,
        note: str = "",
    ) -> int:
        payload = ExecutePayload(
            func=func.strip(),
            args=dict(args or {}),
            max_retries=self.max_retries,
        )
        task_id = self.scheduler.add_task(
            title=title,
            scheduled_time=scheduled_time,
            description=payload.to_description(note),
            task_type="execute",
        )
        self.transition(task_id, TaskState.CREATED)
        return task_id

    def _dispatch(self, task: ScheduledTask) -> None:
        if self._observer:
            try:
                self._observer.on_task_due(task.id, task.title, task.task_type)
            except Exception:
                logger.debug("Observer on_task_due 失败", exc_info=True)

        if task.task_type == "execute":
            self._run_execute(task)
            return

        self.transition(task.id, TaskState.EXECUTING)
        if self._reminder_fn:
            try:
                self._reminder_fn(task)
            except Exception as e:
                logger.error("提醒回调失败: %s", e)
                self.transition(task.id, TaskState.FAILED, last_error=str(e))
                return
        elif self._observer:
            self._observer.on_reminder(task.id, task.title, task.description)
        self.transition(task.id, TaskState.COMPLETED, verified=True)
        logger.info("任务提醒: %s (%s)", task.title, task.scheduled_time)

    def _run_execute(self, task: ScheduledTask) -> None:
        payload = ExecutePayload.from_description(task.description)
        if not payload:
            logger.error("任务#%s 缺少 execute 载荷", task.id)
            self.transition(task.id, TaskState.FAILED, last_error="missing_payload")
            self.scheduler.complete_task(task.id)
            return
        if not self._execute_fn:
            logger.error("TaskEngine 未绑定 execute_fn，跳过任务#%s", task.id)
            self.transition(task.id, TaskState.FAILED, last_error="no_execute_fn")
            self.scheduler.mark_notified(task.id)
            return

        func, args = payload.func, payload.args
        self.transition(task.id, TaskState.PLANNING)
        self.transition(task.id, TaskState.EXECUTING)

        ok, msg = False, ""
        try:
            ok, msg = self._execute_fn(task, func, args)
        except Exception as e:  # noqa: BLE001
            logger.exception("任务#%s 执行异常", task.id)
            msg = str(e)

        self.transition(task.id, TaskState.VERIFYING)
        final = TaskState.COMPLETED if ok else TaskState.FAILED
        self.transition(
            task.id,
            final,
            last_error="" if ok else msg,
            verified=ok,
        )

        if self._observer:
            try:
                self._observer.on_task_executed(
                    task.id,
                    func,
                    ok=ok,
                    message=msg,
                    verified=ok,
                )
            except Exception:
                logger.debug("Observer on_task_executed 失败", exc_info=True)

        self.scheduler.complete_task(task.id)
        logger.info(
            "任务#%s 执行 %s: %s",
            task.id,
            "成功" if ok else "失败",
            msg[:120],
        )
