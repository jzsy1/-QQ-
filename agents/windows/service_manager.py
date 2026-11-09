"""ServiceManager — Windows 服务查询与控制。"""

from __future__ import annotations

from ...tools.execution_result import ExecutionResult, from_tuple
from ...tools import system_admin


class ServiceManager:
    """服务启停，操作后重新查询状态。"""

    def list_services(self, *, status: str = "all", limit: int = 40) -> ExecutionResult:
        ok, msg = system_admin.list_services(status=status, limit=limit)
        return from_tuple(ok, msg, verified=ok, action="list_services")

    def get_status(self, name: str) -> ExecutionResult:
        ok, msg = system_admin.list_services(status="all", limit=80)
        if not ok:
            return from_tuple(False, msg, verified=False, action="get_service_status", target=name)
        found = name.lower() in msg.lower()
        return ExecutionResult(
            success=found,
            message=msg if found else f"未找到服务 {name}",
            verified=found,
            action="get_service_status",
            target=name,
        )

    def start(self, name: str, *, task_id: str | None = None, cancel_check=None) -> ExecutionResult:
        return self._control(name, "start", task_id=task_id, cancel_check=cancel_check)

    def stop(self, name: str, *, task_id: str | None = None, cancel_check=None) -> ExecutionResult:
        return self._control(name, "stop", task_id=task_id, cancel_check=cancel_check)

    def restart(self, name: str, *, task_id: str | None = None, cancel_check=None) -> ExecutionResult:
        return self._control(name, "restart", task_id=task_id, cancel_check=cancel_check)

    def _control(self, name: str, action: str, *, task_id: str | None = None, cancel_check=None) -> ExecutionResult:
        ok, msg = system_admin.control_service(name, action, task_id=task_id, cancel_check=cancel_check)
        if not ok:
            return from_tuple(ok, msg, verified=False, action=f"service_{action}", target=name)
        status_res = self.get_status(name)
        running = status_res.success and "Running" in (status_res.message or "")
        if action == "start":
            verified = running
        elif action == "stop":
            verified = not running
        else:
            verified = running
        return ExecutionResult(
            success=ok and verified,
            message=msg,
            verified=verified,
            action=f"service_{action}",
            target=name,
        )

    def list_scheduled_tasks(self, *, limit: int = 30) -> ExecutionResult:
        ok, msg = system_admin.list_scheduled_tasks(limit=limit)
        return from_tuple(ok, msg, verified=ok, action="list_scheduled_tasks")

    def run_scheduled_task(self, task_name: str) -> ExecutionResult:
        raw = (task_name or "").strip()
        if not raw:
            return ExecutionResult(
                success=False,
                message="请提供计划任务名称",
                verified=False,
                action="run_scheduled_task",
                target="",
            )
        ok, msg = system_admin.run_scheduled_task(raw)
        return from_tuple(ok, msg, verified=ok, action="run_scheduled_task", target=raw)
