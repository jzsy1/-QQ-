"""DevToolsManager — Kekesi 代码自修复工具。"""

from __future__ import annotations

from ...tools.execution_result import ExecutionResult, from_tuple


class DevToolsManager:
    """代码浏览与热重载。"""

    def list_files(self) -> ExecutionResult:
        from ...tools.code_self_repair import list_kekesi_files

        ok, msg = list_kekesi_files()
        return from_tuple(ok, msg, verified=ok, action="list_kekesi_files")

    def read_file(
        self,
        path: str,
        *,
        start_line: int = 1,
        end_line: int = 0,
    ) -> ExecutionResult:
        from ...tools.code_self_repair import read_code_file

        raw = (path or "").strip()
        if not raw:
            return ExecutionResult(
                success=False,
                message="请指定要读取的文件路径",
                verified=False,
                action="read_code_file",
            )
        ok, msg = read_code_file(raw, start_line=start_line, end_line=end_line)
        return from_tuple(ok, msg, verified=ok, action="read_code_file", target=raw[:40])

    def write_file(
        self,
        path: str,
        content: str,
        *,
        create: bool = False,
    ) -> ExecutionResult:
        from ...tools.code_self_repair import write_code_file

        raw = (path or "").strip()
        if not raw or not content:
            return ExecutionResult(
                success=False,
                message="path 和 content 都不能为空",
                verified=False,
                action="write_code_file",
            )
        ok, msg = write_code_file(raw, content, create=create)
        return from_tuple(ok, msg, verified=ok, action="write_code_file", target=raw[:40])

    def reload_module(self, module_name: str) -> ExecutionResult:
        from ...tools.code_self_repair import reload_module

        raw = (module_name or "").strip()
        if not raw:
            return ExecutionResult(
                success=False,
                message="请指定要重载的模块名",
                verified=False,
                action="reload_module",
            )
        ok, msg = reload_module(raw)
        return from_tuple(ok, msg, verified=ok, action="reload_module", target=raw[:40])

    def find_tool_definition(self, func_name: str) -> ExecutionResult:
        from ...tools.code_self_repair import find_tool_definition

        raw = (func_name or "").strip()
        if not raw:
            return ExecutionResult(
                success=False,
                message="请指定要查找的工具名",
                verified=False,
                action="find_tool_definition",
            )
        ok, msg = find_tool_definition(raw)
        return from_tuple(ok, msg, verified=ok, action="find_tool_definition", target=raw[:40])
