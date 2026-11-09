"""QQ 接入技能入口。"""

from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

_DIR = Path(__file__).resolve().parent
_PKG = "_kekesi_skill_qq"


def _service_module():
    if _PKG not in sys.modules:
        package = types.ModuleType(_PKG)
        package.__path__ = [str(_DIR)]
        package.__package__ = _PKG
        sys.modules[_PKG] = package
    return importlib.import_module(f"{_PKG}.service")


def _call(name: str, *args, **kwargs):
    try:
        module = _service_module()
        return getattr(module, name)(*args, **kwargs)
    except Exception as exc:
        return False, f"QQ 插件未就绪：{type(exc).__name__}: {exc}"


def run_tool(tool_id: str, args: dict):
    args = args or {}
    if tool_id == "qq_start":
        return _call("start_qq")
    if tool_id == "qq_stop":
        return _call("stop_qq")
    if tool_id == "qq_status":
        return _call("qq_status")
    if tool_id == "qq_bind":
        qq_id = str(args.get("qq_id") or "").strip()
        anchor = str(args.get("anchor") or "").strip()
        return _call("bind_qq", qq_id, anchor)
    if tool_id == "qq_sticker_sync":
        try:
            limit = int(args.get("limit") or 60)
        except (TypeError, ValueError):
            limit = 60
        return _call("sync_stickers", limit)
    return False, f"不认识这个工具：{tool_id}"
