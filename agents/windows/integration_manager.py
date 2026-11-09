"""IntegrationManager — 提权、自启、系统服务与 ACL。"""

from __future__ import annotations

from ...tools.execution_result import ExecutionResult, from_tuple
from ...tools.privilege import elevate_self, get_privilege_status
from ...tools.system_integration import (
    autostart_status,
    grant_full_control,
    install_autostart,
    install_system_service,
    take_ownership,
    uninstall_autostart,
    uninstall_system_service,
)


class IntegrationManager:
    """系统集成与权限提升操作。"""

    def get_privilege_status(self) -> ExecutionResult:
        ok, msg = get_privilege_status()
        return from_tuple(ok, msg, verified=ok, action="get_privilege_status")

    def elevate_self(self) -> ExecutionResult:
        ok, msg = elevate_self()
        return from_tuple(ok, msg, verified=ok, action="elevate_self")

    def install_autostart(self, *, scope: str = "admin", task_id: str | None = None, cancel_check=None) -> ExecutionResult:
        ok, msg = install_autostart(scope=scope, task_id=task_id, cancel_check=cancel_check)
        return from_tuple(
            ok,
            msg,
            verified=ok,
            action="install_autostart",
            target=scope,
        )

    def uninstall_autostart(self, *, task_id: str | None = None, cancel_check=None) -> ExecutionResult:
        ok, msg = uninstall_autostart(task_id=task_id, cancel_check=cancel_check)
        return from_tuple(ok, msg, verified=ok, action="uninstall_autostart")

    def autostart_status(self) -> ExecutionResult:
        ok, msg = autostart_status()
        return from_tuple(ok, msg, verified=ok, action="autostart_status")

    def install_system_service(self, *, service_name: str = "KekesiService", task_id: str | None = None, cancel_check=None) -> ExecutionResult:
        raw = (service_name or "KekesiService").strip() or "KekesiService"
        ok, msg = install_system_service(service_name=raw, task_id=task_id, cancel_check=cancel_check)
        return from_tuple(
            ok,
            msg,
            verified=ok,
            action="install_system_service",
            target=raw,
        )

    def uninstall_system_service(self, *, service_name: str = "KekesiService", task_id: str | None = None, cancel_check=None) -> ExecutionResult:
        raw = (service_name or "KekesiService").strip() or "KekesiService"
        ok, msg = uninstall_system_service(service_name=raw, task_id=task_id, cancel_check=cancel_check)
        return from_tuple(
            ok,
            msg,
            verified=ok,
            action="uninstall_system_service",
            target=raw,
        )

    def take_ownership(self, path: str, *, recurse: bool = True, task_id: str | None = None, cancel_check=None) -> ExecutionResult:
        raw = (path or "").strip()
        if not raw:
            return ExecutionResult(
                success=False,
                message="请提供路径",
                verified=False,
                action="take_ownership",
                target="",
            )
        ok, msg = take_ownership(raw, recurse=recurse, task_id=task_id, cancel_check=cancel_check)
        return from_tuple(ok, msg, verified=ok, action="take_ownership", target=raw)

    def grant_full_control(
        self,
        path: str,
        *,
        account: str = "Administrators",
        recurse: bool = True,
        task_id: str | None = None,
        cancel_check=None,
    ) -> ExecutionResult:
        raw = (path or "").strip()
        if not raw:
            return ExecutionResult(
                success=False,
                message="请提供路径",
                verified=False,
                action="grant_full_control",
                target="",
            )
        acct = (account or "Administrators").strip() or "Administrators"
        ok, msg = grant_full_control(raw, account=acct, recurse=recurse, task_id=task_id, cancel_check=cancel_check)
        return from_tuple(
            ok,
            msg,
            verified=ok,
            action="grant_full_control",
            target=raw,
        )
