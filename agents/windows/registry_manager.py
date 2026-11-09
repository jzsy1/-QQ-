"""RegistryManager — 注册表读写，高风险操作需权限。"""

from __future__ import annotations

from ...tools.execution_result import ExecutionResult, from_tuple
from ...tools import system_admin


class RegistryManager:
    """注册表操作封装，执行后重新读取验证。"""

    def query(self, path: str, name: str = "") -> ExecutionResult:
        ok, msg = system_admin.read_registry(path, name)
        return from_tuple(ok, msg, verified=ok, action="read_registry", target=path)

    def write(
        self,
        path: str,
        name: str,
        value: str,
        *,
        value_type: str = "String",
        task_id: str | None = None,
        cancel_check=None,
    ) -> ExecutionResult:
        ok, msg = system_admin.write_registry(path, name, value, value_type, task_id=task_id, cancel_check=cancel_check)
        if not ok:
            return from_tuple(ok, msg, verified=False, action="write_registry", target=path)
        ok2, msg2 = system_admin.read_registry(path, name)
        verified = ok2 and value in (msg2 or "")
        return ExecutionResult(
            success=ok and verified,
            message=msg if verified else f"{msg}，但读回验证未通过",
            verified=verified,
            action="write_registry",
            target=path,
        )

    def delete(self, path: str, name: str) -> ExecutionResult:
        safe_path = (path or "").strip()
        safe_name = (name or "").strip()
        if not safe_path or not safe_name:
            return ExecutionResult(
                success=False,
                message="请提供注册表路径和值名称",
                verified=False,
                action="delete_registry",
                target=safe_path,
            )
        script = (
            f"Remove-ItemProperty -LiteralPath '{safe_path}' "
            f"-Name '{safe_name}' -ErrorAction Stop; Write-Output OK"
        )
        from ...tools.winops import powershell

        out = powershell(script, timeout=15)
        ok = "OK" in (out or "")
        if ok:
            ok2, _ = system_admin.read_registry(safe_path, safe_name)
            verified = not ok2
        else:
            verified = False
        return ExecutionResult(
            success=ok and verified,
            message=out or "删除失败",
            verified=verified,
            action="delete_registry",
            target=safe_path,
        )
