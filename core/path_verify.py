"""文件路径解析与删除后复核。"""

from __future__ import annotations

import os

from ..tools.file_ops import resolve_user_path


def resolve_tool_path(path: str) -> str:
    """与 executor 删除逻辑一致的路径解析。"""
    raw = (path or "").strip()
    if not raw:
        return ""
    expanded = os.path.expanduser(raw)
    if os.path.exists(expanded):
        return os.path.abspath(expanded)
    found = resolve_user_path(raw)
    if found:
        return found
    return os.path.abspath(expanded) if expanded else raw


def path_still_exists(path: str) -> bool:
    resolved = resolve_tool_path(path)
    if not resolved:
        return False
    return os.path.exists(resolved)
