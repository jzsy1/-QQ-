"""FileManager — 文件 CRUD，删除后验证。"""

from __future__ import annotations

import os

from ...core.path_verify import path_still_exists
from ...tools.execution_result import ExecutionResult
from ...tools import winops


class FileManager:
    """文件操作封装，禁止未验证的假删除。"""

    def delete(self, path: str, *, use_trash: bool = True) -> ExecutionResult:
        raw = (path or "").strip()
        if not raw:
            return ExecutionResult(
                success=False,
                message="请提供文件路径",
                verified=False,
                action="delete_file",
                target="",
            )
        expanded = os.path.abspath(os.path.expanduser(raw))
        if not os.path.exists(expanded):
            return ExecutionResult(
                success=True,
                message=f"路径不存在，无需删除：{raw}",
                verified=True,
                action="delete_file",
                target=raw,
            )

        ok, msg = winops.move_to_trash(expanded)
        action = "move_to_trash" if use_trash else "delete_file"
        verified = not path_still_exists(expanded)
        success = ok and verified
        if ok and not verified:
            msg = f"{msg}，但路径仍存在：{expanded}"

        return ExecutionResult(
            success=success,
            message=msg,
            verified=verified,
            action=action,
            target=raw,
        )

    def exists(self, path: str) -> bool:
        return os.path.exists(os.path.abspath(os.path.expanduser((path or "").strip())))

    def open_path(self, path: str) -> ExecutionResult:
        from ...tools.file_ops import open_path as open_path_impl

        raw = (path or "").strip()
        if not raw:
            return ExecutionResult(
                success=False,
                message="请提供要打开的路径",
                verified=False,
                action="open_path",
                target="",
            )
        ok, msg = open_path_impl(raw)
        return ExecutionResult(
            success=ok,
            message=msg,
            verified=ok,
            action="open_path",
            target=raw,
        )

    def list_directory(self, path: str = "~") -> ExecutionResult:
        raw = (path or "~").strip() or "~"
        ok, msg = winops.list_directory(raw)
        return ExecutionResult(
            success=ok,
            message=msg,
            verified=ok,
            action="list_directory",
            target=raw,
        )

    def empty_recycle_bin(self) -> ExecutionResult:
        ok, msg = winops.empty_recycle_bin()
        return ExecutionResult(
            success=ok,
            message=msg,
            verified=ok,
            action="empty_recycle_bin",
            target="",
        )

    def copy(self, source: str, destination: str) -> ExecutionResult:
        src = (source or "").strip()
        dst = (destination or "").strip()
        if not src or not dst:
            return ExecutionResult(
                success=False,
                message="请提供源路径和目标路径",
                verified=False,
                action="copy_path",
                target="",
            )
        from ...tools.file_ops import copy_path

        ok, msg = copy_path(src, dst)
        return ExecutionResult(
            success=ok,
            message=msg,
            verified=ok,
            action="copy_path",
            target=src,
        )

    def move(self, source: str, destination: str) -> ExecutionResult:
        src = (source or "").strip()
        dst = (destination or "").strip()
        if not src or not dst:
            return ExecutionResult(
                success=False,
                message="请提供源路径和目标路径",
                verified=False,
                action="move_path",
                target="",
            )
        from ...tools.file_ops import move_path

        ok, msg = move_path(src, dst)
        return ExecutionResult(
            success=ok,
            message=msg,
            verified=ok,
            action="move_path",
            target=src,
        )

    def rename(self, path: str, new_name: str) -> ExecutionResult:
        raw = (path or "").strip()
        name = (new_name or "").strip()
        if not raw or not name:
            return ExecutionResult(
                success=False,
                message="请提供路径和新名称",
                verified=False,
                action="rename_path",
                target="",
            )
        from ...tools.file_ops import rename_path

        ok, msg = rename_path(raw, name)
        return ExecutionResult(
            success=ok,
            message=msg,
            verified=ok,
            action="rename_path",
            target=raw,
        )

    def search(self, keyword: str, *, search_dir: str | None = None) -> ExecutionResult:
        raw = (keyword or "").strip()
        if not raw:
            return ExecutionResult(
                success=False,
                message="请提供搜索关键词",
                verified=False,
                action="search_files",
                target="",
            )
        from ...tools.file_ops import search_files

        ok, msg = search_files(raw, search_dir=search_dir or None)
        return ExecutionResult(
            success=ok,
            message=msg,
            verified=ok,
            action="search_files",
            target=raw,
        )

    def create_document(
        self,
        content: str,
        *,
        title: str = "",
        filename: str = "",
        folder: str = "",
        fmt: str = "docx",
    ) -> ExecutionResult:
        from ...tools.file_ops import create_document as create_document_impl

        body = (content or "").strip()
        if not body:
            return ExecutionResult(
                success=False,
                message="文档内容不能为空",
                verified=False,
                action="create_document",
                target="",
            )
        ok, msg = create_document_impl(
            body,
            title=title,
            filename=filename,
            folder=folder,
            fmt=fmt,
        )
        return ExecutionResult(
            success=ok,
            message=msg,
            verified=ok,
            action="create_document",
            target=(filename or title or "document")[:40],
        )
