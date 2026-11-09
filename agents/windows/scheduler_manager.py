"""SchedulerManager — 提醒与定时任务。"""

from __future__ import annotations

from typing import Any

from ...tools.execution_result import ExecutionResult, from_tuple
from ...utils import parse_json_dict


class SchedulerManager:
    """任务调度与提醒。"""

    def __init__(self, tool_ctx: Any = None) -> None:
        self._ctx = tool_ctx

    def _scheduler(self):
        from ...task_scheduler import TaskScheduler

        scheduler = getattr(self._ctx, "_task_scheduler", None) if self._ctx else None
        if scheduler is None and self._ctx is not None:
            scheduler = TaskScheduler(self._ctx.memory.db)
            scheduler.start()
            self._ctx._task_scheduler = scheduler
        return scheduler

    def set_reminder(
        self,
        title: str,
        time_expr: str,
        *,
        description: str = "",
    ) -> ExecutionResult:
        scheduler = self._scheduler()
        if scheduler is None:
            return ExecutionResult(
                success=False,
                message="调度服务不可用",
                verified=False,
                action="set_reminder",
            )
        t = (title or "").strip()
        expr = (time_expr or "").strip()
        if not t or not expr:
            return ExecutionResult(
                success=False,
                message="标题和时间表达式不能为空",
                verified=False,
                action="set_reminder",
            )
        scheduled_dt = scheduler.parse_time_expression(expr)
        if not scheduled_dt:
            return ExecutionResult(
                success=False,
                message=f"无法解析时间表达式：{expr}",
                verified=False,
                action="set_reminder",
            )
        task_id = scheduler.add_task(
            title=t,
            scheduled_time=scheduled_dt,
            description=(description or "").strip(),
        )
        msg = (
            f"好的，已设置提醒！\n任务ID：{task_id}\n"
            f"时间：{scheduled_dt.strftime('%Y-%m-%d %H:%M')}\n标题：{t}"
        )
        return ExecutionResult(
            success=True,
            message=msg,
            verified=True,
            action="set_reminder",
            target=t[:40],
            extra={"task_id": task_id},
        )

    def schedule_action(
        self,
        title: str,
        time_expr: str,
        action_func: str,
        action_args: dict | None = None,
        *,
        description: str = "",
    ) -> ExecutionResult:
        scheduler = self._scheduler()
        if scheduler is None:
            return ExecutionResult(
                success=False,
                message="调度服务不可用",
                verified=False,
                action="schedule_action",
            )
        t = (title or "").strip()
        expr = (time_expr or "").strip()
        func = (action_func or "").strip()
        args = action_args if isinstance(action_args, dict) else {}
        if not t or not expr:
            return ExecutionResult(
                success=False,
                message="标题和时间表达式不能为空",
                verified=False,
                action="schedule_action",
            )
        if not func:
            return ExecutionResult(
                success=False,
                message="请指定到期要执行的工具 action_func",
                verified=False,
                action="schedule_action",
            )
        scheduled_dt = scheduler.parse_time_expression(expr)
        if not scheduled_dt:
            return ExecutionResult(
                success=False,
                message=f"无法解析时间表达式：{expr}",
                verified=False,
                action="schedule_action",
            )
        engine = getattr(self._ctx, "_task_engine", None) if self._ctx else None
        if engine is not None:
            task_id = engine.schedule_action(
                t,
                scheduled_dt,
                func,
                args,
                note=(description or "").strip(),
            )
        else:
            from ...task_engine import ExecutePayload

            payload = ExecutePayload(func=func, args=dict(args))
            task_id = scheduler.add_task(
                title=t,
                scheduled_time=scheduled_dt,
                description=payload.to_description((description or "").strip()),
                task_type="execute",
            )
        msg = (
            f"好的，已安排定时动作！\n任务ID：{task_id}\n"
            f"时间：{scheduled_dt.strftime('%Y-%m-%d %H:%M')}\n"
            f"动作：{func}\n标题：{t}"
        )
        return ExecutionResult(
            success=True,
            message=msg,
            verified=True,
            action="schedule_action",
            target=func[:40],
            extra={"task_id": task_id},
        )

    def list_tasks(self) -> ExecutionResult:
        scheduler = self._scheduler()
        if scheduler is None:
            return ExecutionResult(
                success=False,
                message="调度服务不可用",
                verified=False,
                action="list_tasks",
            )
        tasks = scheduler.get_pending_tasks()
        if not tasks:
            return ExecutionResult(
                success=True,
                message="当前没有待执行的任务",
                verified=True,
                action="list_tasks",
            )
        lines = []
        for task in tasks:
            dt = task.scheduled_datetime
            time_str = dt.strftime("%Y-%m-%d %H:%M") if dt else task.scheduled_time
            kind = task.task_type or "reminder"
            lines.append(f"- ID:{task.id} [{kind}] [{time_str}] {task.title}")
        return ExecutionResult(
            success=True,
            message="待执行任务：\n" + "\n".join(lines),
            verified=True,
            action="list_tasks",
        )

    def complete_task(self, task_id: int) -> ExecutionResult:
        scheduler = self._scheduler()
        if scheduler is None:
            return ExecutionResult(
                success=False,
                message="调度服务不可用",
                verified=False,
                action="complete_task",
            )
        if task_id <= 0:
            return ExecutionResult(
                success=False,
                message="请指定有效的任务ID",
                verified=False,
                action="complete_task",
            )
        ok = scheduler.complete_task(task_id)
        if ok:
            return ExecutionResult(
                success=True,
                message=f"任务 {task_id} 已标记为完成",
                verified=True,
                action="complete_task",
                target=str(task_id),
            )
        return ExecutionResult(
            success=False,
            message=f"任务 {task_id} 不存在或已完成",
            verified=False,
            action="complete_task",
            target=str(task_id),
        )

    def delete_task(self, task_id: int) -> ExecutionResult:
        scheduler = self._scheduler()
        if scheduler is None:
            return ExecutionResult(
                success=False,
                message="调度服务不可用",
                verified=False,
                action="delete_task",
            )
        if task_id <= 0:
            return ExecutionResult(
                success=False,
                message="请指定有效的任务ID",
                verified=False,
                action="delete_task",
            )
        ok = scheduler.delete_task(task_id)
        if ok:
            return ExecutionResult(
                success=True,
                message=f"任务 {task_id} 已删除",
                verified=True,
                action="delete_task",
                target=str(task_id),
            )
        return ExecutionResult(
            success=False,
            message=f"任务 {task_id} 不存在",
            verified=False,
            action="delete_task",
            target=str(task_id),
        )

    @staticmethod
    def parse_action_args(raw) -> dict:
        if isinstance(raw, dict):
            return raw
        return parse_json_dict(raw) or {}
