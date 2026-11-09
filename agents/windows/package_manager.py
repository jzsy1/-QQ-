"""PackageManager — winget 软件安装/卸载/列表。"""

from __future__ import annotations

from ...tools.execution_result import ExecutionResult, from_tuple
from ...tools import package_ops


class PackageManager:
    """软件包管理，卸载必须 verified。"""

    def list_installed(self, *, query: str = "", limit: int = 30) -> ExecutionResult:
        ok, msg = package_ops.list_installed_software(query=query, limit=limit)
        return from_tuple(
            ok,
            msg,
            verified=ok,
            action="list_installed_software",
            target=(query or "all")[:40],
        )

    def install(self, package: str, *, silent: bool = True) -> ExecutionResult:
        raw = (package or "").strip()
        if not raw:
            return ExecutionResult(
                success=False,
                message="请提供软件包 ID 或名称",
                verified=False,
                action="install_software",
                target="",
            )
        ok, msg = package_ops.install_software(raw, silent=silent)
        return from_tuple(
            ok,
            msg,
            verified=ok,
            action="install_software",
            target=raw,
        )

    def uninstall(self, package: str, *, silent: bool = True) -> ExecutionResult:
        raw = (package or "").strip()
        if not raw:
            return ExecutionResult(
                success=False,
                message="请提供软件包 ID 或名称",
                verified=False,
                action="uninstall_software",
                target="",
            )
        ok, msg, verified = package_ops.uninstall_software_verified(
            raw, silent=silent
        )
        success = ok and verified
        return ExecutionResult(
            success=success,
            message=msg,
            verified=verified,
            action="uninstall_software",
            target=raw,
        )
