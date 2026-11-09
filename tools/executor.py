"""统一工具执行器"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..app_aliases import NAME_ALIASES
from ..contract import is_tool_visible
from ..tools_policy import is_tool_enabled
from ..memory import MemoryService
from ..utils import parse_json_dict
from ._cache import cached_call, invalidate
from .param_normalize import normalize_tool_params, parse_bool
from .typed_executors import validate_and_coerce
from .app_manager import AppManager, candidate_names, is_valid_launch_target
from .process_manager import ProcessManager
from .desktop_apps import find_desktop_app, refresh_desktop_index
from .file_ops import resolve_user_path
from .process_ops import (
    collect_process_names,
    discover_process_names,
)
from .registry import get_tool, register_builtin_tools
from .result_adapter import to_tool_output

logger = logging.getLogger(__name__)

# 工具去重别名：旧名 → 保留名。在 execute_tool 最前替换，
# 使已固化的工作流 / 静态指令中存留的旧工具名继续可用（透明转发）。
_TOOL_ALIASES: dict[str, str] = {
    "get_system_stats": "get_system_status",
    "list_windows": "list_open_windows",
    "click_at": "mouse_click",
    "click_text": "gui_action",
    "run_shell_admin": "run_shell",
    "virtual_click": "mouse_click",
    "virtual_scroll": "mouse_scroll",
    "screenshot_window": "take_screenshot",
    # —— C/D 级合并 ——
    "remember_fact": "remember",
    "set_timer": "set_reminder",
    "schedule_action": "set_reminder",
    "ocr_find_text": "find_text_location",
    "clean_memory": "clean_ram",
}

def _resolve_tool_alias(name: str) -> str:
    return _TOOL_ALIASES.get(name, name)

_READ_CACHE_PREFIXES = (
    "get_system_status",
    "get_hardware_info",
    "list_processes",
    "list_open_windows",
    "list_desktop_files",
    "list_desktop_icons",
    "list_services",
    "get_network_info",
)


def _invalidate_read_caches() -> None:
    """写操作后清只读查询短缓存，避免 TTL 内读到旧状态。"""
    for prefix in _READ_CACHE_PREFIXES:
        invalidate(prefix)


_CHINESE_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
    "壹": 1, "贰": 2, "叁": 3, "肆": 4, "伍": 5,
    "陆": 6, "柒": 7, "捌": 8, "玖": 9,
}

_CHINESE_UNITS = {
    "十": 10, "拾": 10, "百": 100, "佰": 100,
    "千": 1000, "仟": 1000, "万": 10000, "萬": 10000,
}


def _chinese_to_int(text: str) -> int | None:
    """中文数字转整数，支持 0-99999：
    基本数字：一二三...九（小写）
    财务大写：壹贰叁...玖（大写）
    单位：十/拾、百/佰、千/仟、万/萬
    示例：'二十'->20, '五十'->50, '十五'->15, '十'->10,
          '一百'->100, '一百二十三'->123, '一千'->1000, '一万'->10000
    """
    if not isinstance(text, str):
        return None
    s = text.strip()
    if not s:
        return None

    if s.isdigit():
        try:
            return int(s)
        except ValueError:
            return None

    if s.startswith("-") and s[1:].isdigit():
        try:
            return -int(s[1:])
        except ValueError:
            return None

    if len(s) == 1 and s in _CHINESE_DIGITS:
        return _CHINESE_DIGITS[s]

    if len(s) == 1 and s in _CHINESE_UNITS:
        return _CHINESE_UNITS[s]

    if all(c in _CHINESE_DIGITS for c in s):
        result = 0
        for c in s:
            result = result * 10 + _CHINESE_DIGITS[c]
        return result

    all_chars = {**_CHINESE_DIGITS, **_CHINESE_UNITS}
    if not all(c in all_chars for c in s):
        return None

    result = 0
    current = 0
    for c in s:
        if c in _CHINESE_UNITS:
            unit = _CHINESE_UNITS[c]
            if current == 0:
                current = 1
            if unit == 10000:
                result = (result + current * unit) if result else current * unit
                current = 0
            else:
                result += current * unit
                current = 0
        elif c in _CHINESE_DIGITS:
            current = _CHINESE_DIGITS[c]
    result += current
    return result if result > 0 else None


def _normalize_chinese_numbers_in_args(args: Any) -> Any:
    """递归遍历 args，把所有中文数字字符串转成 int 类型。
    只转纯数字字符串（如 '二十'、'50'），不转含其他字符的字符串（如路径、应用名）。
    """
    if isinstance(args, dict):
        return {k: _normalize_chinese_numbers_in_args(v) for k, v in args.items()}
    if isinstance(args, list):
        return [_normalize_chinese_numbers_in_args(v) for v in args]
    if isinstance(args, str):
        s = args.strip()
        if not s:
            return args
        if s.isdigit() or (s.startswith("-") and s[1:].isdigit()):
            try:
                return int(s)
            except ValueError:
                return args
        cn = _chinese_to_int(s)
        if cn is not None:
            return cn
        return args
    return args


def _safe_int(value, default: int = 0, min_val: int | None = None, max_val: int | None = None) -> int:
    """安全地将值转为整数，支持中文数字（如 '二十'->20），失败时返回默认值。"""
    v = None
    if isinstance(value, (int, float)):
        v = int(value)
    elif isinstance(value, str):
        s = value.strip()
        if s.isdigit() or (s.startswith("-") and s[1:].isdigit()):
            try:
                v = int(s)
            except ValueError:
                pass
        if v is None:
            cn = _chinese_to_int(s)
            if cn is not None:
                v = cn
    if v is None:
        return default
    if min_val is not None:
        v = max(min_val, v)
    if max_val is not None:
        v = min(max_val, v)
    return v


@dataclass
class ToolOutput:
    ok: bool
    text: str
    inject_messages: list[dict] = field(default_factory=list)

# 可信的系统短命令（无完整路径也可用）— 见 app_manager._TRUSTED_SHORT_CMDS
_TRUSTED_SHORT_CMDS = frozenset({
    "notepad",
    "calc",
    "explorer",
    "msedge",
    "mspaint",
    "cmd",
    "powershell",
    "control",
    "taskmgr",
})


def _is_valid_launch_target(cmd: str) -> bool:
    return is_valid_launch_target(cmd)


def _candidate_names(name: str) -> list[str]:
    return candidate_names(name)


@dataclass
class ToolContext:
    memory: MemoryService
    apps_from_rules: dict[str, str] = field(default_factory=dict)
    web_search_engine: str = "bing"
    web_research_cfg: dict | None = None
    tools_cfg: dict | None = None
    _task_scheduler: Any = None
    _task_engine: Any = None
    _sentinel: Any = None
    _skill_acquirer: Any = None
    _reload_skills: Any = None
    cancel_check: Callable[[], bool] | None = None
    task_id: str | None = None
    _app_manager: AppManager | None = field(default=None, repr=False)
    _process_manager: ProcessManager | None = field(default=None, repr=False)
    _windows_agent: Any = field(default=None, repr=False)
    _skill_bridge: Any = field(default=None, repr=False)
    _execution_history: Any = field(default=None, repr=False)

    @property
    def execution_history(self):
        return self._execution_history

    @execution_history.setter
    def execution_history(self, value) -> None:
        self._execution_history = value

    @property
    def skill_bridge(self):
        return self._skill_bridge

    @skill_bridge.setter
    def skill_bridge(self, value) -> None:
        self._skill_bridge = value

    @property
    def windows_agent(self):
        if self._windows_agent is None:
            from ..agents.windows_agent import WindowsAgent

            self._windows_agent = WindowsAgent(self)
        return self._windows_agent

    @property
    def app_manager(self) -> AppManager:
        if self._app_manager is None:
            self._app_manager = AppManager(self.memory, self.apps_from_rules)
        return self._app_manager

    @property
    def process_manager(self) -> ProcessManager:
        if self._process_manager is None:
            self._process_manager = ProcessManager(self.memory)
        return self._process_manager

    def resolve_app_command(self, name: str) -> str | None:
        return self.app_manager.resolve_command(name)


def repair_app_registry(memory: MemoryService, apps_from_rules: dict[str, str]):
    """修复记忆库里的错误裸命令（如 微信→wechat）；已验证条目不修改。"""
    refresh_desktop_index()
    with memory.db.connect() as conn:
        rows = conn.execute(
            "SELECT alias, command, verified FROM apps_registry"
        ).fetchall()
        for row in rows:
            alias, command = row["alias"], row["command"]
            if row["verified"]:
                continue
            if _is_valid_launch_target(command):
                continue
            fixed = None
            for candidate in _candidate_names(alias):
                hit = find_desktop_app(candidate)
                if hit:
                    fixed = hit[1]
                    break
            if not fixed:
                for app_name, cmd in apps_from_rules.items():
                    if app_name.lower() == alias.lower() and _is_valid_launch_target(cmd):
                        fixed = cmd
                        break
            if fixed:
                memory.register_app(alias, fixed, verified=False)
                logger.info("已修复应用记忆 %s → %s", alias, fixed[:80])


def _learn_open_processes(
    memory: MemoryService, display: str, launch_cmd: str
) -> str | None:
    """打开成功后异步反查并记住进程名，不阻塞主流程。"""
    import threading

    def _do():
        try:
            import time

            time.sleep(0.5)
            found = discover_process_names(display)
            if not found:
                _d, found = collect_process_names(display, launch_cmd, memory=None)
            if found:
                memory.learn_app_process_names(display, found, command=launch_cmd)
                logger.info("已学习进程名 %s → %s", display, found)
            else:
                logger.warning("打开成功但未识别进程名: display=%s cmd=%s", display, launch_cmd[:80])
        except Exception as e:
            logger.warning("学习进程名失败 %s: %s", display, e)

    t = threading.Thread(target=_do, daemon=True)
    t.start()
    return None


def _open_msg(base: str, learn_note: str | None) -> str:
    if not learn_note:
        return base
    return base.rstrip("。") + "。" + learn_note


def _finish_open_app(
    ctx: ToolContext,
    display: str,
    cmd: str,
    result,
    task_label: str,
) -> ToolOutput:
    """仅 verified 成功时写入记忆。"""
    from .execution_result import ExecutionResult, can_report_success

    if isinstance(result, ExecutionResult):
        ok = can_report_success(result)
        msg = result.message
    else:
        ok, msg = result
    if ok:
        ctx.memory.record_app_outcome(display, success=True)
        _learn_open_processes(ctx.memory, display, cmd)  # CLOSED_LOOP_P5_EXEC_20261030: learn_note was always None
    else:
        ctx.memory.record_app_outcome(display, success=False)
    ctx.memory.log_task(task_label, msg, ok)
    return ToolOutput(ok, msg)


def execute_tool(
    name: str,
    arguments: dict | str | None,
    ctx: ToolContext,
) -> ToolOutput:
    register_builtin_tools()
    params = parse_json_dict(arguments)
    params = normalize_tool_params(name, params)
    name = _resolve_tool_alias(name)
    # 旧名 → 新名 参数签名差异映射（click_text 的 text → gui_action 的 target）
    if name == "gui_action" and "target" not in params and params.get("text"):
        params["target"] = str(params.pop("text"))

    skill_bridge = ctx.skill_bridge
    registry_disabled = bool(
        name.startswith("skill__")
        and skill_bridge is not None
        and hasattr(skill_bridge, "is_disabled")
        and skill_bridge.is_disabled(name)
    )
    if not is_tool_visible(
        name,
        lambda n: is_tool_enabled(n, ctx.tools_cfg),
        registry_disabled=registry_disabled,
    ):
        if registry_disabled:
            return ToolOutput(False, f"技能「{name}」已停用")
        return ToolOutput(False, f"工具「{name}」已在管理后台关闭")

    # Typed Executors：集中类型校验与强类型转换，校验失败不进 handler
    _t_ok, params, _t_err = validate_and_coerce(name, params)
    if not _t_ok:
        return ToolOutput(False, f"参数校验失败：{_t_err}")

    try:
        if name.startswith("skill__"):
            bridge = ctx.skill_bridge
            if bridge is None:
                return ToolOutput(False, f"技能桥未初始化：{name}")
            entry = getattr(bridge, "_dispatch", {}).get(name)
            if entry and getattr(bridge, "registry", None):
                s_rec = bridge.registry._skills.get(entry[0]) or {}
                s_meta = s_rec.get("meta") if isinstance(s_rec, dict) else {}
                wf_name = (s_meta or {}).get("workflow_name") if isinstance(s_meta, dict) else None
                if wf_name and getattr(ctx, "windows_agent", None):
                    out = to_tool_output(ctx.windows_agent.workflows.run_workflow(str(wf_name)))
                    ctx.memory.log_task(f"skill_wf:{wf_name[:20]}", (out.text or "")[:48], out.ok)
                    return out
            result = bridge.execute(name, params)
            out = to_tool_output(result)
            ctx.memory.log_task(f"skill:{name[-24:]}", (out.text or "")[:48], out.ok)
            return out

        if name == "get_current_time":
            out = to_tool_output(ctx.windows_agent.system.get_current_time())
            return out

        if name == "open_app":
            app_name = str(params.get("app_name", "")).strip()
            force_new = parse_bool(params.get("force_new"), default=False)
            if not app_name:
                return ToolOutput(False, "请指定要打开的应用名称")
            result = ctx.windows_agent.open_app(app_name, force_new=force_new)
            hit = ctx.app_manager.resolve(app_name)
            display = hit[0] if hit else app_name
            cmd = hit[1] if hit else (ctx.resolve_app_command(app_name) or "")
            return _finish_open_app(
                ctx, display, cmd, result, f"open:{app_name}"
            )

        if name == "wechat_send_message":
            contact = str(params.get("contact", "")).strip()
            message = str(params.get("message", "")).strip()
            app_name = str(params.get("app_name", "")).strip()
            out = to_tool_output(
                ctx.windows_agent.messaging.send_message(
                    contact, message, app_name=app_name
                )
            )
            ctx.memory.log_task(f"wechat:{contact[:16]}", (out.text or "")[:48], out.ok)
            return out

        if name == "wechat_read_messages":
            app_name = str(params.get("app_name", "")).strip()
            scope = str(params.get("scope") or "unread").strip().lower()
            contact = str(params.get("contact") or "").strip() or None
            max_chats = params.get("max_chats")
            try:
                max_chats_int = int(max_chats) if max_chats is not None else 3
            except (TypeError, ValueError):
                max_chats_int = 3
            out = to_tool_output(
                ctx.windows_agent.messaging.read_messages(
                    app_name=app_name,
                    scope=scope,
                    contact=contact,
                    max_chats=max_chats_int,
                )
            )
            ctx.memory.log_task(f"wechat_read:{scope}", (out.text or "")[:80], out.ok)
            return out

        if name == "wechat_reply_current":
            app_name = str(params.get("app_name", "")).strip()
            message = str(params.get("message", "")).strip()
            out = to_tool_output(
                ctx.windows_agent.messaging.reply_current(
                    message, app_name=app_name
                )
            )
            ctx.memory.log_task("wechat_reply", (out.text or "")[:80], out.ok)
            return out

        if name == "open_app_and_type":
            app_name = str(params.get("app_name", "")).strip()
            text = str(params.get("text", "")).strip()
            force_new = parse_bool(params.get("force_new"), default=False)
            press_enter = parse_bool(params.get("press_enter"), default=False)
            result = ctx.windows_agent.open_app_and_type(
                app_name,
                text,
                force_new=force_new,
                press_enter=press_enter,
            )
            display = app_name
            for candidate in _candidate_names(app_name):
                hit = find_desktop_app(candidate)
                if hit:
                    display = hit[0]
                    break
            if result.success:
                ctx.memory.record_app_outcome(display, success=True)
            else:
                ctx.memory.record_app_outcome(display, success=False)
            out = to_tool_output(result)
            ctx.memory.log_task(f"open_type:{app_name}", out.text or "", out.ok)
            return out

        if name == "search_web":
            query = str(params.get("query", "")).strip()
            max_results = params.get("max_results", 5)
            try:
                max_results = int(max_results)
            except (TypeError, ValueError):
                max_results = 5
            out = to_tool_output(
                ctx.windows_agent.web.search_web(query, max_results=max_results)
            )
            ctx.memory.log_task(f"search_web:{query[:48]}", (out.text or "")[:120], out.ok)
            return out

        if name == "self_study":
            svc = getattr(ctx, "_self_study", None)
            if svc is None:
                return ToolOutput(
                    False, "自习服务未启用（config.yaml 里 self_study.enabled 打开后重启生效）"
                )
            act = str(params.get("action") or "run").strip().lower()
            cand_ref = str(params.get("candidate_ref") or params.get("repo") or "").strip()
            if act == "list_candidates":
                pending = svc.pending_adopt_candidates(limit=10) if hasattr(svc, "pending_adopt_candidates") else []
                if not pending:
                    return ToolOutput(True, "当前暂无待审批的 study_adopt 开源接入候选方案。")
                lines = [
                    f"- [{c['id']}] {c['repo']} (license={c.get('license') or 'unknown'}, risk={c.get('risk') or 'unknown'}): {c.get('why', '')[:80]}"
                    for c in pending
                ]
                return ToolOutput(True, "待审批接入候选方案（需用户明确批准才安装）：\n" + "\n".join(lines))
            if act == "inspect_candidate":
                info = svc.inspect_adopt_candidate(cand_ref) if hasattr(svc, "inspect_adopt_candidate") else None
                if not info:
                    return ToolOutput(False, f"未找到候选方案：{cand_ref}")
                return ToolOutput(True, info["preview_text"])
            if act == "approve_candidate":
                approved = bool(params.get("approved", False))
                res = svc.approve_adopt_candidate(cand_ref, approved_by_user=approved)
                ctx.memory.log_task(f"self_study:approve:{cand_ref[:40]}", str(res.get("message") or "")[:120], bool(res.get("ok")))
                return ToolOutput(bool(res.get("ok")), f"{res.get('preview', '')}\n\n{res.get('message', '')}".strip())
            if act == "reject_candidate":
                reason = str(params.get("reason") or "用户拒绝接入").strip()
                res = svc.reject_adopt_candidate(cand_ref, reason=reason)
                ctx.memory.log_task(f"self_study:reject:{cand_ref[:40]}", str(res.get("message") or "")[:120], bool(res.get("ok")))
                return ToolOutput(bool(res.get("ok")), str(res.get("message") or ""))
            raw = params.get("topics")
            topics: list[str] = []
            if isinstance(raw, list):
                topics = [str(t).strip() for t in raw if str(t).strip()]
            elif isinstance(raw, str):
                topics = [
                    t.strip() for t in re.split(r"[，,、;；]", raw) if t.strip()
                ]
            started = svc.run_now_async(topics or None)
            if started:
                msg = (
                    f"好，我开始自习了（主题：{'、'.join(topics) if topics else '自动挑选科技论文与开源项目'}），"
                    "学到的会记进小本本，之后可以考考我。"
                )
            else:
                msg = "我正在自习中，学完这场再来。"
            try:
                st = svc.status() if hasattr(svc, "status") else {}
                recent_notes = st.get("recentNotes") or []
                if recent_notes:
                    briefs = [
                        f"《{n.get('topic', '')}》({n.get('category', '')})"
                        for n in recent_notes[:3]
                        if n.get("topic")
                    ]
                    if briefs:
                        msg += f"\n近期已学：{ '、'.join(briefs) }。"
                pending = st.get("pendingAdopt") or []
                if pending:
                    cands = [
                        f"{c.get('repo', '')}（{c.get('why', '')[:40]}）"
                        for c in pending[:3]
                        if c.get("repo")
                    ]
                    if cands:
                        msg += (
                            f"\n待批准接入候选（需您明确同意才安装）："
                            + "；".join(cands)
                        )
            except Exception:
                pass
            ctx.memory.log_task("self_study:trigger", msg[:120], True)
            return ToolOutput(started, msg)

        if name == "fetch_web_page":
            url = str(params.get("url", "")).strip()
            start_index = params.get("start_index", 0)
            max_length = params.get("max_length")
            try:
                start_index = int(start_index)
            except (TypeError, ValueError):
                start_index = 0
            out = to_tool_output(
                ctx.windows_agent.web.fetch_web_page(
                    url,
                    start_index=start_index,
                    max_length=int(max_length) if max_length is not None else None,
                )
            )
            ctx.memory.log_task(f"fetch_web:{url[:48]}", (out.text or "")[:120], out.ok)
            return out

        if name == "web_search":
            query = str(params.get("query", "")).strip()
            out = to_tool_output(
                ctx.windows_agent.web.web_search(query, engine=ctx.web_search_engine)
            )
            ctx.memory.log_task(f"web_search:{query[:48]}", out.text or "", out.ok)
            return out

        if name == "type_text":
            text = str(params.get("text", "")).strip()
            window = str(
                params.get("window_title") or params.get("title") or ""
            ).strip()
            if not text:
                return ToolOutput(False, "请告诉我要输入什么内容")
            if window:
                result = ctx.windows_agent.input.focus_and_type(window, text)
            else:
                result = ctx.windows_agent.input.type_plain(text)
            out = to_tool_output(result)
            ctx.memory.log_task(f"type:{text[:32]}", out.text or "", out.ok)
            return out

        if name == "search_files":
            keyword = str(params.get("keyword", "")).strip()
            search_dir = params.get("search_dir")
            out = to_tool_output(
                ctx.windows_agent.files.search(
                    keyword,
                    search_dir=str(search_dir).strip() if search_dir else None,
                )
            )
            ctx.memory.log_task(f"search:{keyword}", (out.text or "")[:48], out.ok)
            return out

        if name == "create_document":
            content = str(params.get("content") or "")
            out = to_tool_output(
                ctx.windows_agent.files.create_document(
                    content,
                    title=str(params.get("title") or ""),
                    filename=str(params.get("filename") or ""),
                    folder=str(params.get("folder") or ""),
                    fmt=str(params.get("format") or params.get("fmt") or "docx"),
                )
            )
            ctx.memory.log_task("create_document", (out.text or "")[:80], out.ok)
            return out

        if name == "run_shell":
            command = str(params.get("command", "")).strip()
            if not command:
                return ToolOutput(False, "命令不能为空")
            shell = str(params.get("shell", "powershell")).strip() or "powershell"
            admin = bool(params.get("admin", False))
            timeout = int(params.get("timeout") or 0) or int(
                (getattr(ctx, "tools_cfg", None) or {}).get("shell_timeout", 90)
            )
            wa = ctx.windows_agent
            result = wa.run_shell(
                command, timeout=timeout, shell=shell, admin=admin
            )
            if (
                not result.success
                and not admin
                and "权限不足" in (result.message or "")
            ):
                logger.info("run_shell 权限不足，自动以管理员重试")
                result = wa.run_shell(
                    command, timeout=timeout, shell=shell, admin=True
                )
            out = to_tool_output(result)
            ctx.memory.log_task(
                f"shell:{command[:48]}", (out.text or "")[:60], out.ok
            )
            return out

        if name == "remember_app":
            app_name = str(params.get("app_name", "")).strip()
            command = str(params.get("command", "")).strip()
            if not app_name or not command:
                return ToolOutput(False, "请提供应用名称和启动命令")
            ctx.memory.register_app(app_name, command, verified=True)
            return ToolOutput(True, f"好的，已记住并标记验证打开{app_name}的方式。")

        if name == "cleanup_memory":
            digest_keep = _safe_int(params.get("digest_keep"), default=50, min_val=0)
            task_keep = _safe_int(params.get("task_keep"), default=200, min_val=0)
            stats = ctx.memory.cleanup(
                digest_keep=digest_keep,
                task_keep=task_keep,
                prune_stale_prefs=bool(params.get("prune_stale_prefs", True)),
            )
            parts = []
            if stats["digest_deleted"]:
                parts.append(f"对话摘要 {stats['digest_deleted']} 条")
            if stats["task_deleted"]:
                parts.append(f"任务日志 {stats['task_deleted']} 条")
            if stats["prefs_deleted"]:
                parts.append(f"无效偏好 {stats['prefs_deleted']} 条")
            msg = "已清理：" + "、".join(parts) if parts else "没有需要清理的内容"
            return ToolOutput(True, msg)

        if name == "remember":
            # 合并原 remember_fact：key+value 走结构化偏好，fact 走自由文本记忆
            # 注意：validate_and_coerce 会给缺省可选参数填 None，必须用 or "" 兜底
            key = str(params.get("key") or "").strip()
            value = str(params.get("value") or "").strip()
            fact = str(params.get("fact") or "").strip()
            if key and value:
                prev = ctx.memory.get_pref(key)
                ctx.memory.set_pref(key, value)
                if prev and prev != value:
                    out = ToolOutput(True, f"好的，已更新：{key} 从 {prev} 改为 {value}。")
                elif prev:
                    out = ToolOutput(True, f"好的，{key} 仍是 {value}。")
                else:
                    out = ToolOutput(True, f"好的，已记住：{key} 是 {value}。")
                ctx.memory.log_task(f"remember:{key[:32]}", out.text or "", True)
                return out
            if not fact:
                return ToolOutput(False, "请提供要记住的内容（fact，或 key+value）")
            ok, msg = ctx.memory.remember(fact)
            ctx.memory.log_task(f"remember:{fact[:32]}", msg, ok)
            return ToolOutput(ok, msg)

        if name == "upsert_core_memory":
            from ..memory.core_memory import upsert_edge

            ok, msg = upsert_edge(
                ctx.memory.db,
                str(params.get("subject") or ""),
                str(params.get("relation") or ""),
                str(params.get("object") or ""),
            )
            ctx.memory.log_task(f"core_memory:{str(params.get('subject') or '')[:24]}", msg, ok)
            return ToolOutput(ok, msg)

        if name == "forget_core_memory":
            from ..memory.core_memory import forget_edge

            n = forget_edge(
                ctx.memory.db,
                str(params.get("subject") or ""),
                str(params.get("relation") or ""),
            )
            return ToolOutput(True, f"已忘掉 {n} 条相关记忆。" if n else "没有找到要忘掉的记忆。")

        if name == "search_memory":
            query = str(params.get("query") or params.get("q") or "").strip()
            if not query:
                return ToolOutput(False, "请提供检索关键词")
            limit = _safe_int(params.get("limit"), default=5, min_val=1, max_val=10)
            lines: list[str] = []
            hits = ctx.memory.db.search_memory(query, limit=limit, include_closed=True)
            for h in hits:
                content = (h.get("content") or "").strip()
                if content:
                    st = h.get("source_type") or ""
                    lines.append(f"- [{st}] {content[:200]}")
            row = ctx.memory.find_app_row(query)
            if row and row.get("path"):
                alias = row.get("alias") or query
                lines.append(f"- [app] {alias} 路径：{row['path']}")
            if not lines:
                return ToolOutput(True, f"未找到与「{query}」相关的记忆。")
            return ToolOutput(True, "相关记忆：\n" + "\n".join(lines[:limit]))

        if name == "open_url":
            url = str(params.get("url", "")).strip()
            out = to_tool_output(ctx.windows_agent.system.open_url(url))
            ctx.memory.log_task(f"url:{url[:48]}", out.text or "", out.ok)
            return out

        if name == "get_weather":
            city = str(params.get("city", "")).strip()
            out = to_tool_output(ctx.windows_agent.system.get_weather(city))
            ctx.memory.log_task(f"weather:{city}", out.text or "", out.ok)
            return out

        if name == "set_volume":
            level = _safe_int(params.get("level", 50), default=50, min_val=0, max_val=100)
            out = to_tool_output(ctx.windows_agent.system.set_volume(level))
            ctx.memory.log_task(f"volume:{level}", out.text or "", out.ok)
            return out

        if name == "list_app_volumes":
            out = to_tool_output(ctx.windows_agent.system.list_app_volumes())
            ctx.memory.log_task("app_volumes:list", out.text or "", out.ok)
            return out

        if name == "list_monitors":
            out = to_tool_output(ctx.windows_agent.system.list_monitors())
            ctx.memory.log_task("monitors:list", out.text or "", out.ok)
            return out

        if name == "get_brightness":
            monitor = str(params.get("monitor") or params.get("display") or "").strip()
            out = to_tool_output(
                ctx.windows_agent.system.get_brightness(monitor or None)
            )
            ctx.memory.log_task(f"brightness:get:{monitor}", out.text or "", out.ok)
            return out

        if name == "set_brightness":
            monitor = str(params.get("monitor") or params.get("display") or "").strip()
            level = _safe_int(params.get("level", 50), default=50, min_val=0, max_val=100)
            out = to_tool_output(
                ctx.windows_agent.system.set_brightness(level, monitor or None)
            )
            ctx.memory.log_task(f"brightness:set:{monitor}:{level}", out.text or "", out.ok)
            return out

        if name == "get_wallpaper":
            out = to_tool_output(ctx.windows_agent.system.get_wallpaper())
            ctx.memory.log_task("wallpaper:get", out.text or "", out.ok)
            return out

        if name == "set_wallpaper":
            image_path = str(
                params.get("image_path") or params.get("path") or params.get("image") or ""
            ).strip()
            style = str(params.get("style") or "").strip()
            out = to_tool_output(
                ctx.windows_agent.system.set_wallpaper(image_path, style or None)
            )
            ctx.memory.log_task(f"wallpaper:set:{image_path}", out.text or "", out.ok)
            return out

        if name == "list_power_plans":
            out = to_tool_output(ctx.windows_agent.system.list_power_plans())
            ctx.memory.log_task("power_plans:list", out.text or "", out.ok)
            return out

        if name == "set_power_plan":
            plan = str(params.get("plan") or params.get("name") or "").strip()
            out = to_tool_output(ctx.windows_agent.system.set_power_plan(plan))
            ctx.memory.log_task(f"power_plan:set:{plan}", out.text or "", out.ok)
            return out

        if name == "get_power_timeouts":
            out = to_tool_output(ctx.windows_agent.system.get_power_timeouts())
            ctx.memory.log_task("power_timeouts:get", out.text or "", out.ok)
            return out

        if name == "set_power_timeout":
            raw_kind = str(params.get("kind") or "").strip().lower()
            if raw_kind in ("睡眠", "sleep", "standby") or "睡" in raw_kind:
                kind = "sleep"
            else:
                kind = "monitor"  # 熄屏/显示/screen/monitor 均归关闭显示
            minutes = _safe_int(params.get("minutes"), default=None, min_val=0, max_val=1440)
            if minutes is None:
                out = to_tool_output(False, "minutes 必须是 0-1440 的数字（0=从不）")
            else:
                on_battery = params.get("on_battery")
                ob = bool(on_battery) if on_battery is not None else None
                out = to_tool_output(
                    ctx.windows_agent.system.set_power_timeout(kind, minutes, ob)
                )
            ctx.memory.log_task(f"power_timeout:{kind}:{minutes}", out.text or "", out.ok)
            return out

        if name == "check_updates":
            out = to_tool_output(ctx.windows_agent.system.check_updates())
            ctx.memory.log_task("wupdate:check", out.text or "", out.ok)
            return out

        if name == "install_updates":
            update_filter = str(
                params.get("update_filter") or params.get("filter") or params.get("title") or ""
            ).strip()
            out = to_tool_output(
                ctx.windows_agent.system.install_updates(update_filter or None)
            )
            ctx.memory.log_task(f"wupdate:install:{update_filter}", out.text or "", out.ok)
            return out

        if name == "get_update_history":
            out = to_tool_output(ctx.windows_agent.system.get_update_history())
            ctx.memory.log_task("wupdate:history", out.text or "", out.ok)
            return out

        if name == "get_firewall_status":
            out = to_tool_output(ctx.windows_agent.system.get_firewall_status())
            ctx.memory.log_task("firewall:status", out.text or "", out.ok)
            return out

        if name == "list_firewall_rules":
            out = to_tool_output(ctx.windows_agent.system.list_firewall_rules())
            ctx.memory.log_task("firewall:list", out.text or "", out.ok)
            return out

        if name == "block_app_network":
            app = str(
                params.get("app") or params.get("application") or params.get("name") or ""
            ).strip()
            direction = str(params.get("direction") or "").strip()
            out = to_tool_output(
                ctx.windows_agent.system.block_app_network(app, direction or None)
            )
            ctx.memory.log_task(f"firewall:block:{app}", out.text or "", out.ok)
            return out

        if name == "allow_app_network":
            app = str(
                params.get("app") or params.get("application") or params.get("name") or ""
            ).strip()
            out = to_tool_output(ctx.windows_agent.system.allow_app_network(app))
            ctx.memory.log_task(f"firewall:allow:{app}", out.text or "", out.ok)
            return out

        if name == "set_app_volume":
            app = str(params.get("app") or params.get("application") or "").strip()
            level = _safe_int(params.get("level"), default=50, min_val=0, max_val=100)
            out = to_tool_output(ctx.windows_agent.system.set_app_volume(app, level))
            ctx.memory.log_task(f"app_volume:{app}:{level}", out.text or "", out.ok)
            return out

        if name == "mute_app":
            app = str(params.get("app") or params.get("application") or "").strip()
            muted_raw = params.get("muted")
            # 缺省=静音（工具名即意图）；显式传 false 才是取消静音
            muted = True if muted_raw is None else bool(muted_raw)
            out = to_tool_output(
                ctx.windows_agent.system.mute_app(app, muted=muted)
            )
            ctx.memory.log_task(f"mute_app:{app}:{muted}", out.text or "", out.ok)
            return out

        if name == "play_music":
            query = str(params.get("query") or params.get("song") or "").strip()
            show = bool(params.get("show", False))
            source = str(params.get("source") or params.get("player") or "").strip() or None
            playlist = str(params.get("playlist") or params.get("list") or "").strip() or None
            out = to_tool_output(
                ctx.windows_agent.media.play_music(
                    query, source=source, show=show, playlist=playlist
                )
            )
            tag = playlist or query
            ctx.memory.log_task(f"play_music:{tag[:40]}", out.text or "", out.ok)
            return out

        if name == "qqmusic_login":
            source = str(params.get("source") or "").strip() or None
            out = to_tool_output(ctx.windows_agent.media.music_login(source))
            ctx.memory.log_task("qqmusic_login", out.text or "", out.ok)
            return out

        if name == "qqmusic_status":
            out = to_tool_output(ctx.windows_agent.media.music_status())
            ctx.memory.log_task("qqmusic_status", out.text or "", out.ok)
            return out

        if name == "control_music":
            action = str(params.get("action", "playpause")).strip()
            out = to_tool_output(
                ctx.windows_agent.media.control_music(
                    action,
                    system_fallback=ctx.windows_agent.system.control_music,
                )
            )
            ctx.memory.log_task(f"music:{action}", out.text or "", out.ok)
            return out

        if name == "play_video":
            query = str(params.get("query") or params.get("title") or "").strip()
            source = str(params.get("source") or params.get("player") or "").strip() or None
            latest = bool(params.get("latest"))
            ep_raw = params.get("episode")
            episode = None
            if isinstance(ep_raw, str) and ep_raw.strip().lower() in (
                "latest",
                "newest",
                "最新",
                "最新一集",
            ):
                latest = True
            elif ep_raw is not None and str(ep_raw).strip() != "":
                try:
                    episode = int(ep_raw)
                except (TypeError, ValueError):
                    episode = None
            out = to_tool_output(
                ctx.windows_agent.media.play_video(
                    query, episode=episode, source=source, latest=latest
                )
            )
            ctx.memory.log_task(f"play_video:{query[:40]}", out.text or "", out.ok)
            return out

        if name == "video_login":
            source = str(params.get("source") or "").strip() or None
            out = to_tool_output(ctx.windows_agent.media.video_login(source))
            ctx.memory.log_task("video_login", out.text or "", out.ok)
            return out

        if name == "video_status":
            out = to_tool_output(ctx.windows_agent.media.video_status())
            ctx.memory.log_task("video_status", out.text or "", out.ok)
            return out

        if name == "close_video":
            out = to_tool_output(ctx.windows_agent.media.close_video())
            ctx.memory.log_task("close_video", out.text or "", out.ok)
            return out

        if name == "take_screenshot":
            _title = str(params.get("title") or "").strip()
            _hwnd = int(params.get("hwnd") or 0)
            if _title or _hwnd:
                # 窗口截图（原 screenshot_window）：PrintWindow，被遮挡仍可抓到
                shot = (
                    ctx.windows_agent.screen.screenshot_dir()
                    / f"win_{int(time.time())}.png"
                )
                out = to_tool_output(
                    ctx.windows_agent.vision.screenshot_window(
                        title=_title,
                        hwnd=_hwnd,
                        path=str(shot),
                    )
                )
                return out
            out = to_tool_output(ctx.windows_agent.system.take_screenshot())
            ctx.memory.log_task("screenshot", out.text or "", out.ok)
            return out

        if name == "system_power":
            action = str(params.get("action", "")).strip()
            out = to_tool_output(ctx.windows_agent.system.system_power(action))
            ctx.memory.log_task(f"power:{action}", out.text or "", out.ok)
            return out

        if name == "list_directory":
            path = str(params.get("path", "~")).strip() or "~"
            out = to_tool_output(ctx.windows_agent.files.list_directory(path))
            ctx.memory.log_task(f"ls:{path[:32]}", (out.text or "")[:48], out.ok)
            return out

        if name in ("move_to_trash", "delete_file"):
            path = str(params.get("path", "")).strip()
            if not path:
                return ToolOutput(False, "请告诉我要删除哪个文件")
            resolved = path
            expanded = os.path.expanduser(path)
            if os.path.exists(expanded):
                resolved = os.path.abspath(expanded)
            else:
                found = resolve_user_path(path)
                if found:
                    resolved = found
            use_trash = name == "move_to_trash"
            result = ctx.windows_agent.files.delete(resolved, use_trash=use_trash)
            out = to_tool_output(result)
            ctx.memory.log_task(f"trash:{path[:32]}", out.text or "", out.ok)
            return out

        if name == "empty_recycle_bin":
            out = to_tool_output(ctx.windows_agent.files.empty_recycle_bin())
            ctx.memory.log_task("empty_recycle_bin", (out.text or "")[:80], out.ok)
            return out

        if name == "forget":
            keyword = str(params.get("keyword", "")).strip()
            ok, msg = ctx.memory.forget(keyword)
            # 不把被遗忘的关键词写进 task_history，否则刚删掉又被记回去
            ctx.memory.log_task("forget", msg, ok)
            return ToolOutput(ok, msg)

        if name == "read_screen":
            result = ctx.windows_agent.screen.read_screen()
            ctx.memory.log_task("read_screen", result.text[:48], result.ok)
            return ToolOutput(
                result.ok, result.text, inject_messages=result.inject_messages
            )

        if name == "recall_screenshot":
            shot_id = str(params.get("screenshot_id") or params.get("id") or "").strip()
            result = ctx.windows_agent.screen.recall_screenshot(shot_id)
            ctx.memory.log_task(
                f"recall_shot:{shot_id[:24]}", result.text[:48], result.ok
            )
            return ToolOutput(
                result.ok, result.text, inject_messages=result.inject_messages
            )

        if name == "list_screenshots":
            limit = _safe_int(params.get("limit"), default=10, min_val=1, max_val=50)
            out = to_tool_output(ctx.windows_agent.screen.list_recent(limit))
            return out

        if name == "list_open_windows":
            limit = _safe_int(params.get("limit", 40), default=40, min_val=5, max_val=120)

            def _low():
                return ctx.windows_agent.windows.list_windows(limit=limit)

            result = cached_call(
                f"list_open_windows:{limit}",
                4.0,
                _low,
                cache_if=lambda r: getattr(r, "success", False),
            )
            out = to_tool_output(result)
            ctx.memory.log_task("list_open_windows", (out.text or "")[:48], out.ok)
            return out

        if name == "list_windows":
            # 轻量窗口列表（进程名/PID/标题）；要句柄/激活态/窗口矩形时用 list_open_windows
            limit = _safe_int(params.get("limit", 40), default=40, min_val=5, max_val=120)

            def _legacy():
                return ctx.windows_agent.windows.list_legacy(limit=limit)

            result = cached_call(
                f"list_windows:{limit}",
                4.0,
                _legacy,
                cache_if=lambda r: getattr(r, "success", False),
            )
            out = to_tool_output(result)
            ctx.memory.log_task("list_windows", (out.text or "")[:48], out.ok)
            return out

        if name == "list_desktop_files":
            limit = _safe_int(params.get("limit", 80), default=80, min_val=5, max_val=200)
            extensions = params.get("extensions") or params.get("ext")
            if isinstance(extensions, list):
                pass
            elif extensions is not None:
                extensions = str(extensions)
            else:
                extensions = None
            name_contains = str(
                params.get("name_contains") or params.get("keyword") or ""
            ).strip() or None
            result = ctx.windows_agent.windows.list_desktop(
                limit=limit,
                extensions=extensions,
                name_contains=name_contains,
            )
            out = to_tool_output(result)
            ctx.memory.log_task("list_desktop_files", (out.text or "")[:48], out.ok)
            return out

        if name == "list_desktop_icons":
            limit = _safe_int(params.get("limit", 80), default=80, min_val=5, max_val=200)
            result = ctx.windows_agent.windows.list_desktop_icons(limit=limit)
            out = to_tool_output(result)
            ctx.memory.log_task("list_desktop_icons", (out.text or "")[:48], out.ok)
            return out

        if name == "show_desktop":
            result = ctx.windows_agent.windows.show_desktop()
            out = to_tool_output(result)
            ctx.memory.log_task("show_desktop", (out.text or "")[:48], out.ok)
            return out

        if name == "click_desktop_icon":
            icon = str(params.get("name") or params.get("icon") or "").strip()
            if not icon:
                return ToolOutput(False, "请提供桌面图标名称")
            double = params.get("double")
            if double is None:
                double = True
            show_first = params.get("show_first")
            if show_first is None:
                show_first = True
            result = ctx.windows_agent.windows.click_desktop_icon(
                icon,
                double=bool(double),
                show_first=bool(show_first),
            )
            out = to_tool_output(result)
            ctx.memory.log_task(f"click_desktop:{icon[:24]}", (out.text or "")[:48], out.ok)
            return out

        if name == "open_path":
            path = str(params.get("path") or params.get("file") or "").strip()
            result = ctx.windows_agent.files.open_path(path)
            out = to_tool_output(result)
            ctx.memory.log_task(f"open_path:{path[:40]}", (out.text or "")[:48], out.ok)
            return out

        if name == "focus_window":
            title = str(params.get("title", "")).strip()
            if not title:
                return ToolOutput(False, "请提供窗口标题")
            result = ctx.windows_agent.windows.focus_with_fallback(
                title,
                launch_target=ctx.resolve_app_command(title) or "",
                memory=ctx.memory,
            )
            out = to_tool_output(result)
            ctx.memory.log_task(f"focus:{title[:24]}", out.text or "", out.ok)
            return out

        if name == "close_app":
            app_name = str(params.get("app_name", "")).strip()
            force = parse_bool(params.get("force"), default=True)
            if not app_name:
                return ToolOutput(False, "请指定要关闭的应用名称")
            result = ctx.windows_agent.close_app(app_name, force=force)
            out = to_tool_output(result)
            ctx.memory.log_task(f"close_app:{app_name}", out.text or "", out.ok)
            if out.ok:
                _invalidate_read_caches()
            return out

        if name == "close_window":
            title = str(params.get("title", "")).strip()
            force = parse_bool(params.get("force"), default=False)
            result = ctx.windows_agent.windows.close(title, force=force)
            out = to_tool_output(result)
            ctx.memory.log_task(f"close:{title[:24]}", out.text or "", out.ok)
            if out.ok:
                _invalidate_read_caches()
            return out

        if name == "close_all_apps":
            force = bool(params.get("force", False))
            keep = str(params.get("keep_title", "")).strip()
            result = ctx.windows_agent.windows.close_all(force=force, keep_title=keep)
            out = to_tool_output(result)
            ctx.memory.log_task(f"close_all:force={force}", (out.text or "")[:48], out.ok)
            if out.ok:
                _invalidate_read_caches()
            return out

        if name == "minimize_window":
            title = str(params.get("title", "")).strip()
            result = ctx.windows_agent.windows.minimize(title)
            out = to_tool_output(result)
            ctx.memory.log_task(f"minimize:{title[:24]}", out.text or "", out.ok)
            return out

        if name == "list_processes":
            limit = _safe_int(params.get("limit", 40), default=40, min_val=5, max_val=100)

            def _lp():
                return ctx.windows_agent.processes.list_processes(limit=limit)

            result = cached_call(
                f"list_processes:{limit}",
                5.0,
                _lp,
                cache_if=lambda r: getattr(r, "success", False),
            )
            out = to_tool_output(result)
            ctx.memory.log_task("list_processes", (out.text or "")[:48], out.ok)
            return out

        if name == "find_process":
            keyword = str(params.get("keyword", "")).strip()
            limit = _safe_int(params.get("limit", 20), default=20, min_val=1, max_val=50)
            result = ctx.windows_agent.processes.find_process(keyword, limit=limit)
            out = to_tool_output(result)
            ctx.memory.log_task(f"find_proc:{keyword[:24]}", (out.text or "")[:48], out.ok)
            return out

        if name == "kill_process":
            target = str(params.get("name_or_pid", "")).strip()
            force = bool(params.get("force", True))
            result = ctx.windows_agent.processes.kill_process(target, force=force)
            out = to_tool_output(result)
            ctx.memory.log_task(f"kill:{target[:24]}", out.text or "", out.ok)
            if out.ok:
                _invalidate_read_caches()
            return out

        if name == "read_clipboard":
            out = to_tool_output(ctx.windows_agent.clipboard.read())
            ctx.memory.log_task("read_clipboard", (out.text or "")[:48], out.ok)
            return out

        if name == "write_clipboard":
            text = str(params.get("text", ""))
            out = to_tool_output(ctx.windows_agent.clipboard.write(text))
            ctx.memory.log_task("write_clipboard", (out.text or "")[:48], out.ok)
            return out

        if name == "get_system_status":

            def _status():
                return ctx.windows_agent.system.get_system_status()

            result = cached_call(
                "get_system_status",
                8.0,
                _status,
                cache_if=lambda r: getattr(r, "success", False),
            )
            out = to_tool_output(result)
            ctx.memory.log_task("system_status", (out.text or "")[:48], out.ok)
            return out
        elif name == "check_system_health":
            auto_fix = bool(params.get("auto_fix", False))

            def _health():
                return ctx.windows_agent.system.check_system_health(auto_fix=auto_fix)

            result = cached_call(
                "check_system_health",
                5.0,
                _health,
                cache_if=lambda r: getattr(r, "success", False),
            )
            out = to_tool_output(result)
            ctx.memory.log_task("system_health", (out.text or "")[:48], out.ok)
            return out

        if name == "get_hardware_info":

            def _hw():
                return ctx.windows_agent.system.get_hardware_info()

            result = cached_call(
                "get_hardware_info",
                12.0,
                _hw,
                cache_if=lambda r: getattr(r, "success", False),
            )
            out = to_tool_output(result)
            ctx.memory.log_task("hardware_info", (out.text or "")[:48], out.ok)
            return out
        if name == "get_temperatures":
            out = to_tool_output(ctx.windows_agent.system.get_temperatures())
            ctx.memory.log_task("temperatures", (out.text or "")[:48], out.ok)
            return out
        if name == "set_temp_threshold":
            def _opt_float(key: str) -> float | None:
                if key not in params or params.get(key) is None:
                    return None
                try:
                    return float(params.get(key))
                except (TypeError, ValueError):
                    return None

            alert = params.get("alert_enabled")
            alert_b = None if alert is None else bool(alert)
            interval = params.get("interval_sec")
            interval_i = None
            if interval is not None:
                try:
                    interval_i = int(interval)
                except (TypeError, ValueError):
                    interval_i = None
            out = to_tool_output(
                ctx.windows_agent.system.set_temp_threshold(
                    cpu=_opt_float("cpu"),
                    gpu=_opt_float("gpu"),
                    ssd=_opt_float("ssd"),
                    other=_opt_float("other"),
                    alert_enabled=alert_b,
                    interval_sec=interval_i,
                )
            )
            ctx.memory.log_task("temp_threshold", (out.text or "")[:48], out.ok)
            return out
        if name == "get_temp_threshold":
            out = to_tool_output(ctx.windows_agent.system.get_temp_threshold())
            return out
        if name == "setup_temp_sensors":
            download = True if "download" not in params else bool(params.get("download"))
            out = to_tool_output(
                ctx.windows_agent.system.setup_temp_sensors(download=download)
            )
            ctx.memory.log_task("setup_temp_sensors", (out.text or "")[:48], out.ok)
            return out

        if name == "send_keys":
            keys = str(params.get("keys", "")).strip()
            window = str(
                params.get("window_title") or params.get("title") or ""
            ).strip()
            if not keys:
                return ToolOutput(False, "请提供按键序列")
            result = ctx.windows_agent.input.send_hotkey(keys, window_title=window)
            out = to_tool_output(result)
            ctx.memory.log_task(f"keys:{keys[:24]}", out.text or "", out.ok)
            return out

        if name == "copy_path":
            src = str(params.get("source", "")).strip()
            dst = str(params.get("destination", "")).strip()
            out = to_tool_output(ctx.windows_agent.files.copy(src, dst))
            ctx.memory.log_task(f"copy:{src[:24]}", (out.text or "")[:48], out.ok)
            return out

        if name == "move_path":
            src = str(params.get("source", "")).strip()
            dst = str(params.get("destination", "")).strip()
            out = to_tool_output(ctx.windows_agent.files.move(src, dst))
            ctx.memory.log_task(f"move:{src[:24]}", (out.text or "")[:48], out.ok)
            return out

        if name == "rename_path":
            path = str(params.get("path", "")).strip()
            new_name = str(params.get("new_name", "")).strip()
            out = to_tool_output(ctx.windows_agent.files.rename(path, new_name))
            ctx.memory.log_task(f"rename:{path[:24]}", (out.text or "")[:48], out.ok)
            return out

        if name == "read_registry":
            path = str(params.get("path", "")).strip()
            reg_name = str(params.get("name", "")).strip()
            if not path:
                return ToolOutput(False, "请提供注册表路径")
            result = ctx.windows_agent.registry.query(path, reg_name)
            out = to_tool_output(result)
            ctx.memory.log_task(f"regread:{path[:24]}", (out.text or "")[:48], out.ok)
            return out

        if name == "write_registry":
            path = str(params.get("path", "")).strip()
            reg_name = str(params.get("name", "")).strip()
            value = str(params.get("value", ""))
            vtype = str(params.get("value_type", "String")).strip()
            if not path or not reg_name:
                return ToolOutput(False, "请提供注册表路径和值名称")
            result = ctx.windows_agent.registry.write(path, reg_name, value, value_type=vtype, task_id=ctx.task_id, cancel_check=ctx.cancel_check)
            out = to_tool_output(result)
            ctx.memory.log_task(f"regwrite:{path[:24]}", (out.text or "")[:48], out.ok)
            return out

        if name == "list_services":
            status = str(params.get("status", "all")).strip()
            limit = _safe_int(params.get("limit", 40), default=40, min_val=5, max_val=80)

            def _list_services():
                return ctx.windows_agent.services.list_services(status=status, limit=limit)

            result = cached_call(
                f"list_services:{status}:{limit}",
                10.0,
                _list_services,
                cache_if=lambda r: getattr(r, "success", False),
            )
            out = to_tool_output(result)
            ctx.memory.log_task("services", (out.text or "")[:48], out.ok)
            return out

        if name == "control_service":
            svc = str(params.get("name", "")).strip()
            action = str(params.get("action", "")).strip().lower()
            if not svc or not action:
                return ToolOutput(False, "请提供服务名和操作（start/stop/restart）")
            services = ctx.windows_agent.services
            if action == "start":
                result = services.start(svc, task_id=ctx.task_id, cancel_check=ctx.cancel_check)
            elif action == "stop":
                result = services.stop(svc, task_id=ctx.task_id, cancel_check=ctx.cancel_check)
            elif action == "restart":
                result = services.restart(svc, task_id=ctx.task_id, cancel_check=ctx.cancel_check)
            else:
                return ToolOutput(False, f"不支持的服务操作：{action}")
            out = to_tool_output(result)
            ctx.memory.log_task(f"svc:{svc}:{action}", out.text or "", out.ok)
            if out.ok:
                _invalidate_read_caches()
            return out

        if name == "list_scheduled_tasks":
            limit = _safe_int(params.get("limit", 30), default=30, min_val=5, max_val=80)
            out = to_tool_output(
                ctx.windows_agent.services.list_scheduled_tasks(limit=limit)
            )
            ctx.memory.log_task("tasks", (out.text or "")[:48], out.ok)
            return out

        if name == "run_scheduled_task":
            task = str(params.get("task_name", "")).strip()
            out = to_tool_output(ctx.windows_agent.services.run_scheduled_task(task))
            ctx.memory.log_task(f"taskrun:{task[:24]}", (out.text or "")[:48], out.ok)
            return out

        if name == "read_env_var":
            var_name = str(params.get("name", "")).strip()
            out = to_tool_output(ctx.windows_agent.env.read(var_name))
            ctx.memory.log_task(f"envread:{var_name[:20]}", (out.text or "")[:48], out.ok)
            return out

        if name == "set_env_var":
            var_name = str(params.get("name", "")).strip()
            value = str(params.get("value", ""))
            out = to_tool_output(ctx.windows_agent.env.set(var_name, value))
            ctx.memory.log_task(f"envset:{var_name[:20]}", (out.text or "")[:48], out.ok)
            return out

        if name == "list_env_vars":
            prefix = str(params.get("prefix", "")).strip()
            limit = _safe_int(params.get("limit", 40), default=40, min_val=5, max_val=100)
            out = to_tool_output(
                ctx.windows_agent.env.list_vars(prefix=prefix, limit=limit)
            )
            ctx.memory.log_task("envlist", (out.text or "")[:48], out.ok)
            return out

        if name == "list_installed_software":
            query = str(params.get("query", "")).strip()
            limit = _safe_int(params.get("limit", 30), default=30, min_val=5, max_val=60)
            out = to_tool_output(
                ctx.windows_agent.packages.list_installed(query=query, limit=limit)
            )
            ctx.memory.log_task("softlist", (out.text or "")[:48], out.ok)
            return out

        if name == "install_software":
            package = str(params.get("package", "")).strip()
            silent = bool(params.get("silent", True))
            out = to_tool_output(
                ctx.windows_agent.packages.install(package, silent=silent)
            )
            ctx.memory.log_task(f"install:{package[:24]}", (out.text or "")[:48], out.ok)
            return out

        if name == "uninstall_software":
            package = str(params.get("package", "")).strip()
            silent = bool(params.get("silent", True))
            out = to_tool_output(
                ctx.windows_agent.packages.uninstall(package, silent=silent)
            )
            ctx.memory.log_task(f"uninstall:{package[:24]}", (out.text or "")[:48], out.ok)
            return out

        if name == "get_network_info":
            result = cached_call(
                "get_network_info",
                10.0,
                ctx.windows_agent.network.get_network_info,
                cache_if=lambda r: getattr(r, "success", False),
            )
            out = to_tool_output(result)
            ctx.memory.log_task("network", (out.text or "")[:48], out.ok)
            return out

        if name == "set_dns":
            alias = str(params.get("interface_alias", "")).strip()
            primary = str(params.get("primary", "")).strip()
            secondary = str(params.get("secondary", "")).strip()
            result = ctx.windows_agent.network.set_dns(alias, primary, secondary, task_id=ctx.task_id, cancel_check=ctx.cancel_check)
            out = to_tool_output(result)
            ctx.memory.log_task(f"dns:{alias[:16]}", out.text or "", out.ok)
            if out.ok:
                _invalidate_read_caches()
            return out

        if name == "list_wifi_networks":
            result = ctx.windows_agent.network.list_wifi()
            out = to_tool_output(result)
            ctx.memory.log_task("wifi_scan", (out.text or "")[:48], out.ok)
            return out

        if name == "list_wifi_profiles":
            result = ctx.windows_agent.network.list_profiles()
            out = to_tool_output(result)
            ctx.memory.log_task("wifi_profiles", (out.text or "")[:48], out.ok)
            return out

        if name == "connect_wifi":
            ssid = str(params.get("ssid", "")).strip()
            password = str(params.get("password", "")).strip()
            result = ctx.windows_agent.network.connect_wifi(ssid, password)
            out = to_tool_output(result)
            ctx.memory.log_task(f"wifi:{ssid[:20]}", out.text or "", out.ok)
            if out.ok:
                _invalidate_read_caches()
            return out

        if name == "get_bluetooth_status":
            out = to_tool_output(ctx.windows_agent.network.bluetooth_status())
            ctx.memory.log_task("bluetooth_status", (out.text or "")[:48], out.ok)
            return out

        if name == "set_bluetooth":
            on = bool(params.get("on", False))
            out = to_tool_output(ctx.windows_agent.network.set_bluetooth(on=on))
            ctx.memory.log_task("bluetooth_switch", (out.text or "")[:48], out.ok)
            if out.ok:
                _invalidate_read_caches()
            return out

        if name == "get_privilege_status":
            out = to_tool_output(ctx.windows_agent.integration.get_privilege_status())
            ctx.memory.log_task("priv_status", (out.text or "")[:48], out.ok)
            return out

        if name == "elevate_self":
            out = to_tool_output(ctx.windows_agent.integration.elevate_self())
            ctx.memory.log_task("elevate_self", (out.text or "")[:48], out.ok)
            return out

        if name == "run_shell_system":
            command = str(params.get("command", "")).strip()
            if not command:
                return ToolOutput(False, "命令不能为空")
            result = ctx.windows_agent.run_shell_system(command)
            out = to_tool_output(result)
            ctx.memory.log_task(f"shell_system:{command[:40]}", (out.text or "")[:60], out.ok)
            return out

        if name == "install_autostart":
            scope = str(params.get("scope", "admin")).strip() or "admin"
            out = to_tool_output(
                ctx.windows_agent.integration.install_autostart(scope=scope, task_id=ctx.task_id, cancel_check=ctx.cancel_check)
            )
            ctx.memory.log_task(f"autostart:{scope}", (out.text or "")[:60], out.ok)
            return out

        if name == "uninstall_autostart":
            out = to_tool_output(ctx.windows_agent.integration.uninstall_autostart(task_id=ctx.task_id, cancel_check=ctx.cancel_check))
            ctx.memory.log_task("autostart_off", (out.text or "")[:48], out.ok)
            return out

        if name == "autostart_status":
            out = to_tool_output(ctx.windows_agent.integration.autostart_status())
            ctx.memory.log_task("autostart_status", (out.text or "")[:48], out.ok)
            return out

        if name == "install_system_service":
            svc = str(params.get("service_name", "KekesiService")).strip() or "KekesiService"
            out = to_tool_output(
                ctx.windows_agent.integration.install_system_service(service_name=svc, task_id=ctx.task_id, cancel_check=ctx.cancel_check)
            )
            ctx.memory.log_task(f"svc_install:{svc}", (out.text or "")[:60], out.ok)
            return out

        if name == "uninstall_system_service":
            svc = str(params.get("service_name", "KekesiService")).strip() or "KekesiService"
            out = to_tool_output(
                ctx.windows_agent.integration.uninstall_system_service(service_name=svc, task_id=ctx.task_id, cancel_check=ctx.cancel_check)
            )
            ctx.memory.log_task(f"svc_remove:{svc}", (out.text or "")[:48], out.ok)
            return out

        if name == "take_ownership":
            path = str(params.get("path", "")).strip()
            recurse = bool(params.get("recurse", True))
            out = to_tool_output(
                ctx.windows_agent.integration.take_ownership(path, recurse=recurse, task_id=ctx.task_id, cancel_check=ctx.cancel_check)
            )
            ctx.memory.log_task(f"takeown:{path[:32]}", (out.text or "")[:60], out.ok)
            return out

        if name == "grant_full_control":
            path = str(params.get("path", "")).strip()
            account = str(params.get("account", "Administrators")).strip() or "Administrators"
            recurse = bool(params.get("recurse", True))
            out = to_tool_output(
                ctx.windows_agent.integration.grant_full_control(
                    path, account=account, recurse=recurse, task_id=ctx.task_id, cancel_check=ctx.cancel_check
                )
            )
            ctx.memory.log_task(f"grant:{path[:32]}", (out.text or "")[:60], out.ok)
            return out

        # —— 系统自修复与清理 ——
        if name == "repair_system":
            out = to_tool_output(ctx.windows_agent.system.repair_system())
            ctx.memory.log_task("repair_system", (out.text or "")[:60], out.ok)
            return out

        if name == "clean_ram":
            out = to_tool_output(ctx.windows_agent.system.clean_memory())
            ctx.memory.log_task("clean_ram", (out.text or "")[:60], out.ok)
            return out

        if name == "clean_disk":
            items = params.get("items")
            if isinstance(items, list):
                items = [str(i) for i in items] if items else None
            aggressive = bool(params.get("aggressive", False))
            out = to_tool_output(
                ctx.windows_agent.system.clean_disk(items=items, aggressive=aggressive)
            )
            ctx.memory.log_task("clean_disk", (out.text or "")[:60], out.ok)
            return out

        if name == "find_large_files":
            drive = str(params.get("drive") or "C")
            min_mb = int(params.get("min_mb") or 100)
            limit = int(params.get("limit") or 40)
            junk_only = bool(params.get("junk_only", False))
            out = to_tool_output(
                ctx.windows_agent.system.find_large_files(
                    drive=drive,
                    min_mb=min_mb,
                    limit=limit,
                    junk_only=junk_only,
                )
            )
            ctx.memory.log_task("find_large_files", (out.text or "")[:60], out.ok)
            return out

        if name == "kill_memory_hogs":
            threshold_mb = int(params.get("threshold_mb") or 500)
            exclude = params.get("exclude")
            if isinstance(exclude, list):
                exclude = [str(e) for e in exclude]
            out = to_tool_output(
                ctx.windows_agent.system.kill_memory_hogs(
                    threshold_mb=threshold_mb, exclude=exclude
                )
            )
            ctx.memory.log_task("kill_hogs", (out.text or "")[:60], out.ok)
            return out

        # —— 鼠标控制 ——
        if name == "mouse_move":
            x = int(params.get("x", 0))
            y = int(params.get("y", 0))
            smooth = bool(params.get("smooth", False))
            out = to_tool_output(ctx.windows_agent.input.move_mouse(x, y, smooth=smooth))
            return out

        if name == "mouse_click":
            x = params.get("x")
            y = params.get("y")
            x = int(x) if x is not None else None
            y = int(y) if y is not None else None
            button = str(params.get("button", "left"))
            double = bool(params.get("double", False))
            virtual = bool(params.get("virtual", False))
            out = to_tool_output(
                ctx.windows_agent.input.mouse_click(
                    x, y, button=button, double=double, virtual=virtual
                )
            )
            return out

        if name == "mouse_drag":
            x1 = int(params.get("x1", 0))
            y1 = int(params.get("y1", 0))
            x2 = int(params.get("x2", 0))
            y2 = int(params.get("y2", 0))
            duration = float(params.get("duration") or 0.4)
            out = to_tool_output(
                ctx.windows_agent.input.mouse_drag(x1, y1, x2, y2, duration=duration)
            )
            return out

        if name == "mouse_scroll":
            amount = _safe_int(params.get("amount"), default=-3)
            x = params.get("x")
            y = params.get("y")
            x = int(x) if x is not None else None
            y = int(y) if y is not None else None
            virtual = bool(params.get("virtual", False))
            out = to_tool_output(
                ctx.windows_agent.input.mouse_scroll(amount, x=x, y=y, virtual=virtual)
            )
            return out

        if name == "get_cursor_pos":
            out = to_tool_output(ctx.windows_agent.input.get_cursor_pos())
            return out

        # —— UIA 自动化 ——
        if name == "uia_find_element":
            result = ctx.windows_agent.uia.find_elements(
                name=str(params.get("name") or ""),
                automation_id=str(params.get("automation_id") or ""),
                control_type=str(params.get("control_type") or ""),
                class_name=str(params.get("class_name") or ""),
                scope=str(params.get("scope") or "descendants"),
                root_window_title=str(params.get("app") or ""),
                limit=_safe_int(params.get("limit"), default=20, min_val=1),
            )
            out = to_tool_output(result)
            ctx.memory.log_task("uia_find", (out.text or "")[:48], out.ok)
            return out

        if name == "uia_click_element":
            result = ctx.windows_agent.uia.click_element(
                name=str(params.get("name") or ""),
                automation_id=str(params.get("automation_id") or ""),
                control_type=str(params.get("control_type") or ""),
                class_name=str(params.get("class_name") or ""),
                root_window_title=str(params.get("app") or ""),
                button=str(params.get("button") or "left"),
                double=bool(params.get("double") or False),
            )
            out = to_tool_output(result)
            ctx.memory.log_task("uia_click", (out.text or "")[:48], out.ok)
            return out

        if name == "uia_get_text":
            result = ctx.windows_agent.uia.get_element_text(
                name=str(params.get("name") or ""),
                automation_id=str(params.get("automation_id") or ""),
                control_type=str(params.get("control_type") or ""),
                root_window_title=str(params.get("app") or ""),
            )
            out = to_tool_output(result)
            return out

        if name == "uia_list_controls":
            result = ctx.windows_agent.uia.list_controls(
                root_window_title=str(params.get("app") or ""),
                scope=str(params.get("scope") or "children"),
                control_type=str(params.get("control_type") or ""),
                limit=_safe_int(params.get("limit"), default=50, min_val=1),
            )
            out = to_tool_output(result)
            return out

        if name == "uia_set_value":
            result = ctx.windows_agent.uia.set_element_value(
                str(params.get("value") or ""),
                name=str(params.get("name") or ""),
                automation_id=str(params.get("automation_id") or ""),
                control_type=str(params.get("control_type") or "edit"),
                root_window_title=str(params.get("app") or ""),
            )
            out = to_tool_output(result)
            return out

        # —— OCR ——
        if name == "ocr_screen":
            region = params.get("region")
            if isinstance(region, list) and len(region) == 4:
                region = tuple(int(v) for v in region)
            else:
                region = None
            language = str(params.get("language", "zh-Hans-CN"))
            out = to_tool_output(
                ctx.windows_agent.vision.ocr_screen(region=region, language=language)
            )
            return out

        if name == "list_ocr_languages":
            out = to_tool_output(ctx.windows_agent.vision.list_languages())
            return out

        # —— 视觉点击 ——
        if name == "gui_action":
            target = str(params.get("target") or params.get("text") or "").strip()
            action = str(params.get("action") or "click")
            app = str(params.get("app") or "")
            button = str(params.get("button") or "left")
            double = bool(params.get("double", False))
            partial = bool(params.get("partial", True))
            if double and action == "click":
                action = "double_click"
            result = ctx.windows_agent.input.gui_action(
                target,
                action=action,
                app=app,
                button=button,
                double=double,
                partial=partial,
                db=ctx.memory.db if ctx.memory else None,
            )
            out = to_tool_output(result)
            layer = (result.extra or {}).get("layer", "")
            ctx.memory.log_task(f"gui_action:{target[:40]}", layer or (out.text or "")[:48], out.ok)
            return out

        if name == "find_text_location":
            text = str(params.get("text", "")).strip()
            app = str(params.get("app", ""))
            partial = bool(params.get("partial", True))
            result = ctx.windows_agent.input.find_text_location(
                text, app=app, partial=partial
            )
            out = to_tool_output(result)
            ctx.memory.log_task("find_text_location", text[:40], out.ok)
            return out

        if name == "read_screen_text":
            app = str(params.get("app", ""))
            region = params.get("region")
            if isinstance(region, list) and len(region) == 4:
                region = tuple(int(v) for v in region)
            else:
                region = None
            result = ctx.windows_agent.input.read_screen_text(app=app, region=region)
            out = to_tool_output(result)
            ctx.memory.log_task("read_screen_text", app[:24] or "screen", out.ok)
            return out

        if name == "hover_text":
            text = str(params.get("text", "")).strip()
            app = str(params.get("app", ""))
            partial = bool(params.get("partial", True))
            result = ctx.windows_agent.input.hover_text(text, app=app, partial=partial)
            out = to_tool_output(result)
            ctx.memory.log_task("hover_text", text[:40], out.ok)
            return out

        # —— 工作流 / 临时序列 / 遮挡 / 虚拟鼠标 ——
        if name in ("list_workflows", "run_workflow", "save_workflow", "run_sequence"):
            wf = ctx.windows_agent.workflows
            if name == "list_workflows":
                out = to_tool_output(wf.list_workflows())
                return out
            if name == "save_workflow":
                steps = params.get("steps")
                out = to_tool_output(
                    wf.save_workflow(
                        str(params.get("name") or ""),
                        steps,
                        title=str(params.get("title") or ""),
                        trigger_hint=str(params.get("trigger_hint") or ""),
                    )
                )
                return out
            if name == "run_sequence":
                steps = params.get("steps")
                stop_on_error = bool(params.get("stop_on_error", True))
                out = to_tool_output(
                    wf.run_sequence(steps, stop_on_error=stop_on_error)
                )
                ctx.memory.log_task("run_sequence", (out.text or "")[:60], out.ok)
                return out
            out = to_tool_output(
                wf.run_workflow(
                    str(params.get("name") or ""),
                    user_confirmed=bool(params.get("confirmed", False)),
                )
            )
            ctx.memory.log_task(
                f"workflow:{params.get('name', '')}", (out.text or "")[:60], out.ok
            )
            return out

        if name == "check_occlusion":
            x = params.get("x")
            y = params.get("y")
            out = to_tool_output(
                ctx.windows_agent.input.check_occlusion(
                    x=int(x) if x is not None else None,
                    y=int(y) if y is not None else None,
                    hwnd=int(params.get("hwnd") or 0),
                    title=str(params.get("title") or ""),
                )
            )
            return out

        if name == "inspect_window_som":
            from .co_operator import inspect_window_som

            ok, msg, extra = inspect_window_som(
                window_title=str(params.get("window_title") or params.get("title") or ""),
                include_ocr=bool(params.get("include_ocr", True)),
            )
            return ToolOutput(ok, msg, data=extra)

        if name == "execute_gui_macro":
            from .co_operator import execute_gui_macro

            steps = params.get("steps") if isinstance(params.get("steps"), list) else []
            ok, msg = execute_gui_macro(
                steps,
                window_title=str(params.get("window_title") or params.get("app") or ""),
                borrow_focus=bool(params.get("borrow_focus", True)),
                stop_on_error=bool(params.get("stop_on_error", True)),
                db=ctx.memory.db if ctx.memory else None,
            )
            if ctx.memory:
                ctx.memory.log_task("execute_gui_macro", msg[:60], ok)
            return ToolOutput(ok, msg)

        if name == "scan_desktop":
            _lim = params.get("limit")
            out = to_tool_output(
                ctx.windows_agent.vision.scan_desktop(
                    limit=int(_lim) if _lim is not None else 20,
                    include_controls=bool(params.get("include_controls", True)),
                    include_regions=bool(params.get("include_regions", True)),
                )
            )
            return out

        if name == "ocr_window":
            out = to_tool_output(
                ctx.windows_agent.vision.ocr_window(
                    title=str(params.get("title") or ""),
                    hwnd=int(params.get("hwnd") or 0),
                    language=str(params.get("language") or "zh-Hans-CN"),
                    dark_enhance=bool(params.get("dark_enhance", True)),
                )
            )
            return out

        if name == "wait_until_found":
            out = to_tool_output(
                ctx.windows_agent.automation.wait_until_found(
                    str(params.get("text") or ""),
                    control_type=str(params.get("control_type") or ""),
                    app=str(params.get("app") or ""),
                    timeout=float(params.get("timeout") or 15),
                    partial=bool(params.get("partial", True)),
                )
            )
            return out

        if name == "wait_until_gone":
            out = to_tool_output(
                ctx.windows_agent.automation.wait_until_gone(
                    str(params.get("text") or ""),
                    app=str(params.get("app") or ""),
                    timeout=float(params.get("timeout") or 15),
                )
            )
            return out

        if name == "wait_for_stable_screen":
            out = to_tool_output(
                ctx.windows_agent.automation.wait_for_stable_screen(
                    timeout=float(params.get("timeout") or 10),
                    stable_for=float(params.get("stable_for") or 0.8),
                )
            )
            return out

        if name == "wait_for_file":
            out = to_tool_output(
                ctx.windows_agent.automation.wait_for_file(
                    str(params.get("path") or ""),
                    timeout=float(params.get("timeout") or 30),
                    stable_for=float(params.get("stable_for") or 1.0),
                )
            )
            return out

        if name == "register_image":
            out = to_tool_output(
                ctx.windows_agent.vision.register_image(
                    str(params.get("name") or ""),
                    str(params.get("path") or ""),
                )
            )
            return out

        if name == "find_image":
            out = to_tool_output(
                ctx.windows_agent.vision.find_image(
                    str(params.get("image") or ""),
                    confidence=float(params.get("confidence") or 0.8),
                )
            )
            return out

        if name == "click_image":
            out = to_tool_output(
                ctx.windows_agent.vision.click_image(
                    str(params.get("image") or ""),
                    confidence=float(params.get("confidence") or 0.8),
                    button=str(params.get("button") or "left"),
                    double=bool(params.get("double", False)),
                    virtual=bool(params.get("virtual", True)),
                )
            )
            return out

        if name == "click_and_type":
            x = params.get("x")
            y = params.get("y")
            out = to_tool_output(
                ctx.windows_agent.input.click_and_type(
                    str(params.get("text") or ""),
                    x=int(x) if x is not None else None,
                    y=int(y) if y is not None else None,
                    target=str(params.get("target") or ""),
                    app=str(params.get("app") or ""),
                    clear_first=bool(params.get("clear_first", False)),
                    virtual=bool(params.get("virtual", True)),
                )
            )
            return out

        if name == "focus_and_hotkey":
            x = params.get("x")
            y = params.get("y")
            out = to_tool_output(
                ctx.windows_agent.input.focus_and_hotkey(
                    str(params.get("keys") or ""),
                    target=str(params.get("target") or ""),
                    x=int(x) if x is not None else None,
                    y=int(y) if y is not None else None,
                    app=str(params.get("app") or ""),
                )
            )
            return out

        if name == "fill_form":
            fields = params.get("fields")
            out = to_tool_output(
                ctx.windows_agent.input.fill_form(
                    fields,
                    app=str(params.get("app") or ""),
                    submit=str(params.get("submit") or ""),
                )
            )
            return out

        if name == "click_menu_item":
            out = to_tool_output(
                ctx.windows_agent.input.click_menu_item(
                    str(params.get("path") or ""),
                    app=str(params.get("app") or ""),
                    separator=str(params.get("separator") or ">"),
                )
            )
            return out

        if name == "detect_emotion":
            out = to_tool_output(
                ctx.windows_agent.assistant.detect_emotion(str(params.get("text", "")))
            )
            return out

        # ============================================================
        # 自主监控规则（Sentinel）
        # ============================================================
        if name in ("add_watch_rule", "list_watch_rules", "delete_watch_rule"):
            sentinel = ctx._sentinel
            if sentinel is None:
                return ToolOutput(False, "监控服务未启动")
            if name == "add_watch_rule":
                args_obj = params.get("action_args")
                ok, msg = sentinel.add_rule(
                    str(params.get("condition_type") or ""),
                    float(params.get("threshold") or 0),
                    str(params.get("question") or ""),
                    description=str(params.get("description") or ""),
                    action_func=str(params.get("action_func") or ""),
                    action_args=args_obj if isinstance(args_obj, dict) else {},
                    timeout_sec=_safe_int(params.get("timeout_sec"), default=60, min_val=0),
                )
                ctx.memory.log_task("watch_rule_add", msg[:60], ok)
                return ToolOutput(ok, msg)
            if name == "list_watch_rules":
                ok, msg = sentinel.list_rules()
                return ToolOutput(ok, msg)
            ok, msg = sentinel.delete_rule(_safe_int(params.get("rule_id"), default=0))
            return ToolOutput(ok, msg)

        # ============================================================
        # 任务调度工具（set_reminder 已合并原 set_timer / schedule_action）
        # ============================================================
        if name in ("set_reminder", "list_tasks", "complete_task", "delete_task"):
            sched = ctx.windows_agent.scheduler
            if name == "set_reminder":
                time_expr = str(params.get("time_expr") or "").strip()
                seconds = _safe_int(
                    params.get("seconds"), default=0, min_val=1
                )
                action_func = str(params.get("action_func") or "").strip()
                title = str(params.get("title") or "").strip()
                message = str(params.get("message") or "").strip()
                if seconds and not time_expr:
                    # 倒计时路径（原 set_timer）：到点播放提示音
                    out = to_tool_output(
                        ctx.windows_agent.system.set_timer(
                            seconds, message or title or "时间到"
                        )
                    )
                    ctx.memory.log_task(f"timer:{seconds}", out.text or "", out.ok)
                    return out
                if action_func:
                    # 定时动作路径（原 schedule_action）
                    action_args = params.get("action_args") or params.get("args") or {}
                    out = to_tool_output(
                        sched.schedule_action(
                            title,
                            time_expr,
                            action_func,
                            sched.parse_action_args(action_args),
                            description=str(params.get("description") or ""),
                        )
                    )
                    return out
                # 自然语言时间提醒
                out = to_tool_output(
                    sched.set_reminder(
                        title,
                        time_expr,
                        description=str(
                            params.get("description") or message or ""
                        ),
                    )
                )
                return out
            if name == "list_tasks":
                out = to_tool_output(sched.list_tasks())
                return out
            if name == "complete_task":
                out = to_tool_output(
                    sched.complete_task(int(params.get("task_id", 0) or 0))
                )
                return out
            out = to_tool_output(
                sched.delete_task(int(params.get("task_id", 0) or 0))
            )
            return out

        if name == "set_persona":
            out = to_tool_output(
                ctx.windows_agent.assistant.set_persona(str(params.get("persona", "")))
            )
            return out

        if name == "get_persona":
            out = to_tool_output(ctx.windows_agent.assistant.get_persona())
            return out

        if name == "list_personas":
            out = to_tool_output(ctx.windows_agent.assistant.list_personas())
            return out

        if name == "list_kekesi_files":
            out = to_tool_output(ctx.windows_agent.devtools.list_files())
            return out

        if name == "read_code_file":
            out = to_tool_output(
                ctx.windows_agent.devtools.read_file(
                    str(params.get("path", "")),
                    start_line=int(params.get("start_line", 1) or 1),
                    end_line=int(params.get("end_line", 0) or 0),
                )
            )
            return out

        if name == "write_code_file":
            out = to_tool_output(
                ctx.windows_agent.devtools.write_file(
                    str(params.get("path", "")),
                    str(params.get("content", "")),
                    create=bool(params.get("create", False)),
                )
            )
            return out

        if name == "reload_module":
            out = to_tool_output(
                ctx.windows_agent.devtools.reload_module(
                    str(params.get("module_name", ""))
                )
            )
            return out

        if name == "find_tool_definition":
            out = to_tool_output(
                ctx.windows_agent.devtools.find_tool_definition(
                    str(params.get("func_name", ""))
                )
            )
            return out

        if name == "acquire_skill":
            out = to_tool_output(
                ctx.windows_agent.skill_acquisition.acquire(str(params.get("need", "")))
            )
            return out

        if name == "uninstall_skill":
            out = to_tool_output(
                ctx.windows_agent.skill_acquisition.uninstall(
                    str(params.get("skill_ref", ""))
                )
            )
            return out

        if get_tool(name) is None:
            return ToolOutput(False, f"未知工具: {name}")
        return ToolOutput(False, f"工具 {name} 尚未实现")

    except Exception as e:
        logger.exception("工具执行异常 %s: %s", name, e)
        return ToolOutput(False, f"执行失败: {e}")


# ============================================================================
# 工具执行超时守护（口头回复纪律的底层支撑）
# 大脑根据工具的真实返回结果回复；动作类操作若长时间无响应（默认 6 秒）即中断，
# 回报「未完成」及原因，避免卡死或谎称已完成。
# ============================================================================

# 动作类工具：执行应很快返回，默认 6 秒无响应即中断
_ACTION_TOOLS = frozenset(
    {
        "open_app",
        "close_app",
        "close_window",
        "kill_process",
        "focus_window",
        "minimize_window",
        "delete_file",
        "move_to_trash",
    }
)

# 各工具默认超时（秒）；未列出的用 _DEFAULT_TOOL_TIMEOUT
# 原则：守护超时 >= 工具内部 PowerShell/subprocess 预算 + 小余量，
# 否则"正常工作"会被误判"未完成"。守护是防"真卡死"的兜底，不是用来砍正常耗时的。
# 动作类可被 config.tools.action_timeout_sec 全局收紧（如想要更激进的 6 秒中断）。
_TOOL_TIMEOUTS: dict[str, float] = {
    # —— 动作类：对齐各自内部预算 ——
    "open_app": 25,        # 启动 + verify(主窗口/UWP回退≤8s×多次) + 异步学习；UWP冷启动慢，放宽对齐 close_app
    "close_app": 25,       # PID 反查(8s) + 批量杀除(6s) + 复核 + fallback(8s)
    "close_window": 12,
    "list_open_windows": 8,
    "list_desktop_files": 8,
    "list_desktop_icons": 12,
    "show_desktop": 8,
    "click_desktop_icon": 15,
    "open_path": 8,
    "kill_process": 12,    # shutdown 内部 8s + 复核
    "focus_window": 15,    # focus_window_by_title 内部 12s
    "minimize_window": 10,
    "delete_file": 30,     # 大文件夹移回收站可能较慢
    "move_to_trash": 30,
    # —— 输入/媒体/电源：快 ——
    "type_text": 8,
    "open_app_and_type": 25,
    "send_keys": 6,
    "read_clipboard": 4,
    "write_clipboard": 4,
    "set_volume": 5,
    "list_app_volumes": 8,
    "set_app_volume": 8,
    "mute_app": 8,
    "list_monitors": 15,
    "get_brightness": 15,
    "set_brightness": 20,
    "get_wallpaper": 5,
    "set_wallpaper": 8,
    "list_power_plans": 10,
    "set_power_plan": 15,
    "get_power_timeouts": 15,
    "set_power_timeout": 20,
    "check_updates": 320,
    "install_updates": 920,
    "get_update_history": 60,
    "get_firewall_status": 20,
    "list_firewall_rules": 30,
    "block_app_network": 60,
    "allow_app_network": 60,
    # —— 科科斯内部工具：轻量快速 ——
    "add_watch_rule": 8,
    "list_watch_rules": 8,
    "complete_task": 8,
    "list_tasks": 8,
    "detect_emotion": 8,
    "find_tool_definition": 8,
    "get_persona": 8,
    "list_personas": 8,
    "set_persona": 8,
    "list_kekesi_files": 8,
    "read_code_file": 8,
    "write_code_file": 10,
    "reload_module": 10,
    # 自我扩展：联网检索多源 → 模型选品 → 下载 → 适配生成 → 校验 → 原子安装 → 热重载，
    # 是长链路一次性动作（内部各有 http/LLM 超时兜底），守护超时须给足预算
    "acquire_skill": 180,
    "uninstall_skill": 30,  # 删目录/改 mcp.json + 热重载，本地操作很快
    "set_reminder": 8,
    "play_music": 90,
    "qqmusic_login": 60,
    "qqmusic_status": 10,
    "control_music": 20,
    "play_video": 120,
    "video_login": 60,
    "video_status": 12,
    "close_video": 20,
    "system_power": 8,
    "get_current_time": 4,
    "take_screenshot": 20,   # 含窗口截图（PrintWindow）路径
    "read_screen": 30,
    "recall_screenshot": 15,
    "list_screenshots": 5,
    # —— 批量/列举/系统查询 ——
    "close_all_apps": 30,
    "list_processes": 12,
    "find_process": 25,
    "list_directory": 8,
    "search_files": 25,
    "create_document": 20,
    "copy_path": 40,
    "move_path": 40,
    "rename_path": 8,
    "empty_recycle_bin": 120,
    "get_system_status": 15,
    "get_hardware_info": 30,
    "get_temperatures": 25,
    "set_temp_threshold": 10,
    "get_temp_threshold": 5,
    "setup_temp_sensors": 180,
    "get_network_info": 20,
    "read_registry": 12,
    "write_registry": 70,
    "list_services": 20,
    "control_service": 40,
    "list_scheduled_tasks": 20,
    "run_scheduled_task": 20,
    "read_env_var": 8,
    "set_env_var": 12,
    "list_env_vars": 10,
    # —— 网络/Wi-Fi ——
    "get_weather": 20,
    "open_url": 8,
    "web_search": 10,
    "search_web": 50,
    "fetch_web_page": 50,
    "list_wifi_networks": 30,
    "list_wifi_profiles": 15,
    "connect_wifi": 45,
    "set_dns": 70,
    "get_bluetooth_status": 25,
    "set_bluetooth": 35,
    "wechat_send_message": 30,
    "wechat_read_messages": 60,
    "wechat_reply_current": 30,
    # —— 软件包（可能很慢）——
    "list_installed_software": 60,
    "install_software": 600,
    "uninstall_software": 600,
    # —— Shell / 提权 / 系统集成（含 UAC 交互，给足时间）——
    # run_shell 可能先普通执行(≤90s)再自动管理员重试(≤180s)，故放宽到 300s
    "run_shell": 300,
    "get_privilege_status": 20,
    "elevate_self": 15,
    "run_shell_system": 240,
    "install_autostart": 240,
    "uninstall_autostart": 120,
    "autostart_status": 15,
    "install_system_service": 120,
    "uninstall_system_service": 80,
    "take_ownership": 180,
    "grant_full_control": 120,
    # —— 系统自修复 ——
    "check_system_health": 180,   # sfc /verifyonly 可能较慢
    "repair_system": 360,         # sfc /scannow 可能很慢
    # —— 内存与磁盘清理 ——
    "clean_ram": 45,
    "clean_disk": 120,
    "find_large_files": 300,
    "kill_memory_hogs": 30,
    # —— 鼠标 ——
    "mouse_move": 5,
    "mouse_click": 8,
    "mouse_drag": 15,
    "mouse_scroll": 5,
    "get_cursor_pos": 3,
    # —— UIA ——
    "uia_find_element": 15,
    "uia_click_element": 20,
    "uia_get_text": 15,
    "uia_list_controls": 25,
    "uia_set_value": 15,
    # —— OCR ——
    "ocr_screen": 45,
    "list_ocr_languages": 10,
    # —— 视觉点击 ——
    "gui_action": 60,        # UIA + OCR 兜底可能较慢（原 click_text 预算）
    "find_text_location": 60,
    "read_screen_text": 60,
    "hover_text": 60,
    "list_workflows": 5,
    "run_workflow": 180,
    "save_workflow": 10,
    "run_sequence": 180,
    "check_occlusion": 5,
    "scan_desktop": 90,
    "ocr_window": 60,
    "wait_until_found": 60,
    "wait_until_gone": 60,
    "wait_for_stable_screen": 30,
    "wait_for_file": 120,
    "register_image": 3,
    "find_image": 30,
    "click_image": 30,
    "click_and_type": 25,
    "focus_and_hotkey": 20,
    "fill_form": 90,
    "click_menu_item": 45,
    # —— 记忆类 ——
    "remember": 8,
    "forget": 8,
    "remember_app": 5,
    "search_memory": 5,
    "cleanup_memory": 20,
}

_DEFAULT_TOOL_TIMEOUT = 15.0


def tool_timeout_for(name: str, tools_cfg: dict | None = None) -> float:
    """返回某工具的执行超时（秒）。动作类可被 config.tools.action_timeout_sec 覆盖。"""
    name = _resolve_tool_alias(name)
    base = _TOOL_TIMEOUTS.get(name, _DEFAULT_TOOL_TIMEOUT)
    if tools_cfg and name in _ACTION_TOOLS:
        try:
            override = float(tools_cfg.get("action_timeout_sec", 0) or 0)
            if override > 0:
                return override
        except (TypeError, ValueError):
            pass
    return base


def execute_tool_guarded(
    name: str,
    arguments: dict | str | None,
    ctx: ToolContext,
    timeout: float | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> ToolOutput:
    """带超时守护地执行工具。

    在守护线程里运行 execute_tool；若超过 deadline 仍无响应，则中断等待并
    返回「未完成 + 原因」，由大脑据实播报（后台线程作为守护线程自然结束，结果丢弃）。
    """
    t = timeout if timeout is not None else tool_timeout_for(
        name, getattr(ctx, "tools_cfg", None)
    )
    result_box: dict[str, ToolOutput] = {}

    def _worker():
        try:
            result_box["out"] = execute_tool(name, arguments, ctx)
        except Exception as e:  # noqa: BLE001
            result_box["out"] = ToolOutput(False, f"执行失败: {e}")

    th = threading.Thread(target=_worker, name=f"tool-{name}", daemon=True)
    th.start()
    deadline = time.time() + t
    while th.is_alive():
        if cancel_check and cancel_check():
            logger.info("工具 %s 被用户打断", name)
            try:
                from ..core.process_registry import cancel_task, get_current_task

                tid = get_current_task()
                if tid:
                    cancel_task(tid)
            except Exception:
                pass
            return ToolOutput(False, "操作已取消。")
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        th.join(timeout=min(0.2, remaining))

    if th.is_alive():
        logger.warning("工具 %s 超过 %.0fs 无响应，已中断等待，结果未知", name, t)
        return ToolOutput(
            False,
            f"执行超时、结果未知：「{name}」执行超过 {int(t)} 秒仍无响应，已中断等待。"
            "可能是目标程序卡死、未响应，或弹出了需要手动处理的窗口。",
        )
    return result_box.get("out", ToolOutput(False, "执行未返回结果"))
