#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TOOL_WORKER_V2_4_5: per-call worker for read-only tools (4-5 v2).

v1 is retired. Changes in v2:
- Result goes to a temp file given as argv[2], never to stdout. The
  caller sets stdout/stderr to DEVNULL, so no anonymous-pipe deadlock
  at any output size. Payload goes in as a temp file argv[1].
- No stdlib approximation. Direct reuse first (winops / hardware_monitor
  / process_ops, signature-filtered, str return only), then execute_tool
  with a NoopMemory (log_task etc. are no-ops; the main process still
  logs memory on the original path after the worker is dead). If both
  fail, worker_error=True and the caller falls back to in-process
  guarded, so the format is always the in-process format.
- limit and other args are passed through unchanged, filtered only by
  the real function's signature. No hard-coded 333/200 limits.
- This file never writes memory, files (except its own result file),
  or GUI state, and never imports MCP/skill bridges at top level.
"""
from __future__ import annotations

import inspect
import json
import os
import sys

WORKER_WHITELIST = frozenset({
    "get_system_status",
    "get_hardware_info",
    "list_processes",
    "list_open_windows",
    "list_directory",
})
WORKER_TIMEOUT = 15.0

_DIRECT = {
    "get_system_status": [("winops", "get_system_status")],
    "get_hardware_info": [("hardware_monitor", "get_hardware_info"),
                          ("winops", "get_hardware_info")],
    "list_processes": [("process_ops", "list_processes"),
                       ("winops", "list_processes")],
    "list_open_windows": [("winops", "list_open_windows")],
    "list_directory": [("file_ops", "list_directory"),
                       ("winops", "list_directory")],
}


class _WorkerOutput:
    def __init__(self, ok, text, inject_messages=None):
        self.ok = bool(ok)
        self.text = text or ""
        self.inject_messages = inject_messages or []


class _NoopMemory:
    """Empty memory for worker reuse only: never persists anything."""

    def log_task(self, *args, **kwargs):
        return None

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return lambda *args, **kwargs: None


def _candidate_packages(payload):
    pkgs = []
    for mod in (payload.get("tool_module"), payload.get("ctx_module"),
                __package__):
        if not mod:
            continue
        if "." in mod:
            pkgs.append(mod.rsplit(".", 1)[0])
            pkgs.append(mod.rsplit(".", 1)[0].rsplit(".", 1)[0]
                        if "." in mod.rsplit(".", 1)[0] else mod.rsplit(".", 1)[0])
        else:
            pkgs.append(mod)
    if __package__:
        pkgs.append(__package__)
    pkgs.append("kekesi")
    out = []
    for p in pkgs:
        if p and p not in out:
            out.append(p)
    return out


def _call_filtered(fn, args):
    sig = inspect.signature(fn)
    params = sig.parameters
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return fn(**dict(args))
    kwargs = {}
    for name, p in params.items():
        if name == "self":
            continue
        if name in args:
            kwargs[name] = args[name]
        elif p.default is inspect.Parameter.empty and \
                p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD,
                           inspect.Parameter.KEYWORD_ONLY):
            raise TypeError("missing required argument: %s" % name)
    return fn(**kwargs)


def _convert_return(out):
    if isinstance(out, str):
        return True, out, []
    if isinstance(out, tuple) and len(out) >= 2 and isinstance(out[0], bool):
        return bool(out[0]), str(out[1]), []
    if isinstance(out, dict) and "text" in out:
        ok = out.get("ok", out.get("success", True))
        return bool(ok), str(out["text"]), list(out.get("inject_messages") or [])
    raise RuntimeError("direct return is not str-like, fallback")


def _try_direct(func, args, payload):
    import importlib
    pkgs = _candidate_packages(payload)
    last_err = None
    for mod_short, fn_name in _DIRECT.get(func, []):
        for pkg in pkgs:
            for mod_name in ("%s.%s" % (pkg, mod_short), mod_short):
                try:
                    mod = importlib.import_module(mod_name)
                    fn = getattr(mod, fn_name)
                except Exception as e:
                    last_err = e
                    continue
                try:
                    out = _call_filtered(fn, args)
                    return _convert_return(out)
                except Exception as e:
                    last_err = e
                    continue
    raise RuntimeError("direct reuse failed: %r" % (last_err,))


def _try_execute_tool(func, args, payload):
    import importlib
    tool_module = payload.get("tool_module") or "kekesi.tools.executor"
    ctx_module = payload.get("ctx_module") or tool_module
    tool_mod = importlib.import_module(tool_module)
    execute_tool = getattr(tool_mod, "execute_tool")
    try:
        ctx_mod = importlib.import_module(ctx_module)
        ToolContext = getattr(ctx_mod, "ToolContext")
    except Exception:
        ToolContext = getattr(tool_mod, "ToolContext")
    sig = inspect.signature(ToolContext.__init__)
    params = set(sig.parameters) - {"self"}
    pure = {}
    for key in ("task_id", "tools_cfg", "apps_from_rules", "web_search_engine"):
        if key in payload and key in params:
            pure[key] = payload[key]
    if "memory" in params:
        pure["memory"] = _NoopMemory()
    try:
        ctx = ToolContext(**pure)
    except TypeError:
        ctx = ToolContext()
    try:
        setattr(ctx, "memory", _NoopMemory())
    except Exception:
        pass
    for key in ("task_id", "tools_cfg", "apps_from_rules"):
        if key in payload and not hasattr(ctx, key):
            try:
                setattr(ctx, key, payload[key])
            except Exception:
                pass
    out = execute_tool(func, args, ctx)
    if isinstance(out, tuple) and len(out) >= 2:
        return bool(out[0]), str(out[1]), []
    return bool(getattr(out, "ok", False)), str(getattr(out, "text", "")), \
        list(getattr(out, "inject_messages", []) or [])


def worker_main(payload):
    func = payload.get("func") if isinstance(payload, dict) else None
    args = payload.get("args") if isinstance(payload, dict) else None
    if not isinstance(func, str) or func not in WORKER_WHITELIST \
            or not isinstance(args, dict):
        return {"ok": False, "text": "不在 worker 白名单",
                "inject_messages": [], "worker_error": True}
    try:
        ok, text, inject = _try_direct(func, args, payload)
        return {"ok": ok, "text": text, "inject_messages": inject,
                "worker_error": False}
    except Exception:
        pass
    try:
        ok, text, inject = _try_execute_tool(func, args, payload)
        return {"ok": ok, "text": text, "inject_messages": inject,
                "worker_error": False}
    except Exception as e:
        return {"ok": False, "text": "worker 执行失败：%s" % e,
                "inject_messages": [], "worker_error": True}


def _kill_tree(proc):
    try:
        proc.kill()
    except Exception:
        pass
    try:
        proc.wait(timeout=2)
    except Exception:
        pass
    if proc.poll() is None and os.name == "nt":
        try:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, timeout=5)
        except Exception:
            pass
        try:
            proc.wait(timeout=2)
        except Exception:
            pass


def run_tool_in_worker(func, args, ctx, cancel_check=None,
                       timeout=WORKER_TIMEOUT, ctx_module=None,
                       tool_module=None):
    """Run one read-only tool in a worker process. None = fallback."""
    import logging
    import subprocess
    import tempfile
    import time
    logger = logging.getLogger(__name__)
    if func not in WORKER_WHITELIST:
        return None
    payload = {"func": func, "args": dict(args or {})}
    if ctx_module:
        payload["ctx_module"] = ctx_module
    if tool_module:
        payload["tool_module"] = tool_module
    for key in ("task_id", "tools_cfg", "apps_from_rules"):
        try:
            value = getattr(ctx, key, None)
            json.dumps(value)
            payload[key] = value
        except Exception:
            continue
    try:
        data = json.dumps(payload, ensure_ascii=False)
    except Exception:
        return None
    here = os.path.abspath(__file__)
    pkg_dir = os.path.dirname(here)
    root = os.path.dirname(pkg_dir)
    env = dict(os.environ)
    env["PYTHONPATH"] = root + os.pathsep + env.get("PYTHONPATH", "")
    worker_mod = (__package__ + ".tool_worker") if __package__ \
        else "kekesi.tool_worker"
    payload_path = result_path = None
    proc = None
    try:
        fd, payload_path = tempfile.mkstemp(prefix="kekesi_worker_in_", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
        fd, result_path = tempfile.mkstemp(prefix="kekesi_worker_out_", suffix=".json")
        os.close(fd)
        proc = subprocess.Popen(
            [sys.executable, "-m", worker_mod, payload_path, result_path],
            cwd=root, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace")
    except Exception:
        logger.exception("4-5 worker 启动失败 %s", func)
        for p in (payload_path, result_path):
            if p:
                try:
                    os.unlink(p)
                except Exception:
                    pass
        return None
    start = time.time()
    try:
        while True:
            rc = proc.poll()
            if rc is not None:
                break
            if cancel_check is not None:
                try:
                    if cancel_check():
                        _kill_tree(proc)
                        return _WorkerOutput(False, "操作已取消。")
                except Exception:
                    pass
            if time.time() - start > timeout:
                _kill_tree(proc)
                return _WorkerOutput(False, "执行超时、结果未知：%s" % func)
            time.sleep(0.2)
        if rc != 0:
            logger.warning("4-5 worker 退出码 %s func=%s", rc, func)
            return None
        try:
            with open(result_path, encoding="utf-8") as f:
                result = json.loads(f.read() or "{}")
        except Exception:
            logger.warning("4-5 worker 结果文件无法解析 func=%s", func)
            return None
        if result.get("worker_error"):
            return None
        return _WorkerOutput(result.get("ok", False),
                             result.get("text", ""),
                             result.get("inject_messages") or [])
    except Exception:
        logger.exception("4-5 worker 等待异常 %s", func)
        if proc is not None:
            _kill_tree(proc)
        return None
    finally:
        for p in (payload_path, result_path):
            if p:
                try:
                    os.unlink(p)
                except Exception:
                    pass


def _main():
    payload = {}
    result_path = sys.argv[2] if len(sys.argv) >= 3 else None
    try:
        if len(sys.argv) >= 2 and os.path.isfile(sys.argv[1]):
            with open(sys.argv[1], encoding="utf-8") as f:
                payload = json.loads(f.read() or "{}")
        else:
            payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        payload = {}
    try:
        result = worker_main(payload)
    except Exception as e:
        result = {"ok": False, "text": "worker 异常：%s" % e,
                  "inject_messages": [], "worker_error": True}
    if result_path:
        try:
            with open(result_path, "w", encoding="utf-8") as f:
                f.write(json.dumps(result, ensure_ascii=False))
        except Exception:
            pass
    else:
        sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    _main()
