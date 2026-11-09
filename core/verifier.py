"""物理验证层 — 确认工具操作真实生效。"""

from __future__ import annotations

import logging
import re
from typing import Any

from ..action_selfcheck import run_post_check, verification_enabled
from ..models.evidence import Evidence
from ..models.result import RuntimeExecutionResult
from ..r1_executor import R1Response, STATUS_SUCCESS, r1_to_tool_output
from ..tools.executor import ToolContext
from .execution_result import from_r1_response

logger = logging.getLogger(__name__)


class Verifier:
  """对执行结果做物理复核；UNKNOWN 不能自动改成 SUCCESS。"""

  def verify_action(
    self,
    action: Action,
    result: RuntimeExecutionResult,
    ctx: ToolContext,
    *,
    mcp_bridge: Any | None = None,
  ) -> Evidence:
    # UNKNOWN_GATE_V2_4_3: unknown is not generic failure.
    # Timeout / unknown status / structured unknown evidence / cancelled-after-execution
    # must return Evidence.unknown, not Evidence.failed, so supervisor can map to
    # UNKNOWN and recovery's unknown gate is not masked by evidence.
    # Execution-before cancel (message="cancelled before execution") is known no-effect,
    # so it stays failed and remains retryable.
    if not result.success:
      _is_unknown_result = (
        result.status in ("timeout", "unknown")
        or (isinstance(result.evidence, dict) and result.evidence.get("unknown"))
        or (result.status == "cancelled" and (result.message or "") != "cancelled before execution")
      )
      if _is_unknown_result:
        return Evidence.unknown(result.error or result.message or "tool result unknown")
      return Evidence.failed(result.error or result.message or "execution failed")

    if action.tool == "agent_loop":
      verified = bool(result.evidence.get("verified"))
      reason = ""
      if not verified:
        # 从 agent_loop 回复 / 失败工具提取真实失败原因，
        # 避免笼统 "agent_loop incomplete" 覆盖业务错误（如「页面脚本执行超时」）
        tool_results = result.evidence.get("tool_results") or []
        fail_texts = [
          str(t.get("text") or t.get("summary") or "")
          for t in tool_results
          if not bool(t.get("ok", True))
        ]
        reason = (
          str(result.message or "")[:120]
          or (fail_texts[0][:120] if fail_texts else "")
          or "agent_loop incomplete"
        )
      return Evidence.from_execution(
        verified=verified,
        checks=[{"type": "agent_loop", "ok": verified}],
        observed_state={"layer": result.evidence.get("layer", "")},
        reason=reason,
      )

    if not result.verification_required:
      return Evidence.from_execution(
        verified=True,
        checks=[{"type": "skipped", "tool": action.tool}],
        observed_state=dict(result.evidence),
      )

    r1 = R1Response(
      status=STATUS_SUCCESS if result.success else "error",
      func=action.tool,
      args=dict(action.parameters or {}),
      data={
        "ok": result.success,
        "text": result.message,
        "verified": result.evidence.get("verified"),
        **{k: v for k, v in result.evidence.items() if k not in ("verified",)},
      },
    )
    resp, _extra = run_post_check(
      action.tool,
      action.parameters or {},
      r1,
      ctx,
      mcp_bridge=mcp_bridge,
    )
    out = r1_to_tool_output(resp)
    ok = resp.status == STATUS_SUCCESS and out.ok
    verified_flag = resp.data.get("verified")
    if verified_flag is None:
      verified = ok
    else:
      verified = bool(verified_flag) and ok

    if verified:
      return Evidence.from_execution(
        verified=True,
        checks=[{"type": "post_check", "tool": action.tool, "ok": True}],
        observed_state=dict(resp.data or {}),
      )

    # UNKNOWN: 不能确认时如实返回
    reason = str(resp.data.get("reason") or resp.data.get("text") or out.text or "verify_failed")
    if resp.data.get("verified") is None and ok:
      return Evidence.unknown(reason)
    return Evidence.failed(reason)

  # Q4_TASK_GOAL_VERIFY_V1: verify goal, not tool success.
  # Final SUCCESS gate only. Per-action verify_action is untouched;
  # this method is read-only (no run_post_check, no self-healing execute).
  _GOAL_READONLY_PREFIXES = (
    "get_", "list_", "search_", "read_", "check_", "is_", "query_", "view_",
  )
  _GOAL_CHECKABLE_TOOLS = {
    "open_app": "process_running",
    "close_app": "process_not_running",
    "close_window": "window_not_visible",
    "kill_process": "process_not_running",
    "delete_file": "file_not_exists",
    "move_to_trash": "file_not_exists",
    "focus_window": "window_visible",
    "uninstall_software": "package_not_installed",
    "write_file": "file_exists",
    "create_file": "file_exists",
    "create_directory": "file_exists",
    "move_file": "file_exists",
    "copy_file": "file_exists",
    "rename_file": "file_exists",
  }

  def _goal_target(self, kind: str, params: dict) -> str:
    params = params or {}
    if kind in ("process_running", "process_not_running"):
      return str(params.get("app_name") or params.get("target") or params.get("process") or "").strip()
    if kind in ("file_exists", "file_not_exists"):
      return str(params.get("path") or params.get("dest") or params.get("destination") or "").strip()
    if kind in ("window_visible", "window_not_visible"):
      return str(params.get("title") or params.get("window_title") or "").strip()
    if kind == "pid_not_alive":
      return str(params.get("pid") or "").strip()
    if kind == "package_not_installed":
      return str(params.get("package") or params.get("name") or params.get("app_name") or "").strip()
    return ""

  def _goal_plan_criteria(self, plan: Any) -> list[dict]:
    out: list[dict] = []
    if plan is None:
      return out
    for action in (getattr(plan, "actions", None) or []):
      verification = getattr(action, "verification", None)
      if not isinstance(verification, dict):
        continue
      vtype = str(verification.get("type") or "").strip()
      params = getattr(action, "parameters", None) or {}
      if vtype == "process_and_window":
        # Planner says process+window, but window title cannot be derived
        # reliably from app name alone (app name != window title), so
        # first piece checks process only; window is marked unchecked
        # in checks, never used to fail the goal.
        target = str(params.get("app_name") or "").strip()
        if target:
          out.append({"kind": "process_running", "target": target, "source": "plan", "window_unchecked": True})
      elif vtype == "process_gone":
        target = str(params.get("app_name") or "").strip()
        if target:
          out.append({"kind": "process_not_running", "target": target, "source": "plan"})
      elif vtype in ("process_running", "process_not_running", "file_exists", "file_not_exists", "window_visible", "window_not_visible", "pid_not_alive", "package_not_installed"):
        target = self._goal_target(vtype, params)
        if target:
          out.append({"kind": vtype, "target": target, "source": "plan"})
      # Unknown verification types are ignored here; they do not make
      # the goal pass, they simply do not contribute a criterion.
      # If no criterion at all is collected, verify_goal falls to
      # rule mapping / loop claims, and finally to UNKNOWN.
    return out

  def _goal_acceptance_criteria(self, plan: Any) -> list[dict]:
    # KEKESI_CONTRACT_V0: Plan.acceptance 优先于 Action.verification。
    # State 条目 kind 直接取 8 终态 kinds；readonly/query 原样透传（kind 须为 executed/content），
    # 由 verify_goal 按 type 分发到 _goal_nonstate_check；未知 kind/type 忽略。
    out: list[dict] = []
    for entry in (getattr(plan, "acceptance", None) or []):
      if not isinstance(entry, dict):
        continue
      etype = str(entry.get("type") or "").strip()
      kind = str(entry.get("kind") or "").strip()
      target = str(entry.get("target") or "").strip()
      if not target:
        continue
      if etype == "state":
        if kind not in ("process_running", "process_not_running", "file_exists", "file_not_exists", "window_visible", "window_not_visible", "pid_not_alive", "package_not_installed"):
          continue
        criterion = {"kind": kind, "target": target, "source": "plan", "type": "state"}
        if entry.get("window_unchecked"):
          criterion["window_unchecked"] = True
        out.append(criterion)
      elif etype == "readonly" and kind == "executed":
        out.append({"kind": kind, "target": target, "source": "plan", "type": "readonly"})
      elif etype == "query" and kind == "content":
        out.append({"kind": kind, "target": target, "source": "plan", "type": "query"})
      # 未知 type/kind 忽略，不贡献标准
    return out

  def _goal_rule_criteria(self, goal: str) -> list[dict]:
    # Mirrors Planner._OPEN_RE / _CLOSE_RE (same prefixes, same 24-char
    # app limit). Delete is mapped only when the target looks like a
    # file path, otherwise "删除记事本" is ambiguous (uninstall vs file).
    goal = (goal or "").strip()
    if not goal:
      return []
    m = re.match(r"^(?:请|帮我|给我)?(?:打开|启动|运行)(?:一下|下)?\s*(.+?)[。！？!?\s]*$", goal)
    if m:
      app = m.group(1).strip()
      if app and len(app) <= 24:
        return [{"kind": "process_running", "target": app, "source": "rule"}]
    m = re.match(r"^(?:请|帮我)?(?:关闭|关掉|退出)(?:一下|下)?\s*(.+?)[。！？!?\s]*$", goal)
    if m:
      app = m.group(1).strip()
      if app and len(app) <= 24:
        return [{"kind": "process_not_running", "target": app, "source": "rule"}]
    m = re.match(r"^(?:请|帮我)?(?:删除|删掉)(?:一下|下)?\s*(.+?)[。！？!?\s]*$", goal)
    if m:
      path = m.group(1).strip()
      if path and len(path) <= 260 and ("/" in path or "\\" in path or "." in path):
        return [{"kind": "file_not_exists", "target": path, "source": "rule"}]
    return []

  def _goal_category(self, kind: str, target: str) -> tuple[str, str]:
    # Category for deduplication: process/file/window/pid/package.
    # Open then close the same app must NOT produce two criteria
    # (running AND not running) that can never both pass; last wins.
    if kind in ("process_running", "process_not_running"):
      return ("process", target)
    if kind in ("file_exists", "file_not_exists"):
      return ("file", target)
    if kind in ("window_visible", "window_not_visible"):
      return ("window", target)
    if kind == "pid_not_alive":
      return ("pid", target)
    if kind == "package_not_installed":
      return ("package", target)
    return (kind, target)

  def _goal_loop_criteria(self, tool_results: Any) -> tuple[list[dict], bool, bool]:
    # Returns (criteria, has_uncheckable_goal_tool, executed_any).
    # GOAL_LOOP_READONLY_V1 — shell read-only rules + rule 3:
    # 1. run_shell with a read-only command (safety/shell_guard
    #    .is_readonly_command) is NOT uncheckable: it yields no state
    #    criterion, but a successful run counts as executed_any.
    # 2. Non-read-only run_shell stays uncheckable (rule 2): if the
    #    goal cannot be checked, do not call it success. No loosening.
    # 3. verify_goal maps (no criteria + executed_any) to SUCCESS and
    #    (no criteria + no execution) to UNKNOWN, so a read-only
    #    command like whoami lands SUCCESS while idle chat (zero tool
    #    execution) stays UNKNOWN.
    # COST (accepted): this gate checks goal state only, not answer
    #    quality — "ran a read-only tool but answered poorly" still
    #    lands SUCCESS. Do not use it to judge reply quality.
    # COUPLING: is_readonly_command currently only governs whether
    #    to pop a confirmation; reusing it here means widening the
    #    pass list later will silently change terminal criteria.
    #    exec_locks._READONLY_TOOLS is a separate tool-level list.
    #    Only read-only *shell* counts toward executed_any; prefix
    #    read-only tools (get_/list_/...) stay ignored, not counted.
    from ..safety.shell_guard import is_readonly_command
    # tool_results items (agent_loop.py, confirmed): dict with
    # func/name, ok, args (dict), summary/text; selfcheck extras carry
    # via="selfcheck" and are skipped, not treated as goal claims.
    # Read-only tools are ignored, not uncheckable. Any other
    # goal-changing tool that cannot be checked with current primitives
    # (run_shell, write_registry value, play_*, skill__*, mcp__*, ...)
    # makes the whole goal UNKNOWN, even if checkable claims pass,
    # because the goal may depend on that step.
    # Last claim per target category wins: e.g. open then close the
    # same app means the final claimed state is closed, not opened.
    criteria: dict[tuple[str, str], dict] = {}
    uncheckable = False
    executed_any = False
    if not isinstance(tool_results, list):
      return [], False, False
    for item in tool_results:
      if not isinstance(item, dict):
        uncheckable = True
        continue
      if item.get("via") == "selfcheck" or "selfcheck" in str(item.get("func") or ""):
        continue
      func = str(item.get("func") or item.get("name") or "").strip()
      if not func:
        continue
      if func.startswith(self._GOAL_READONLY_PREFIXES):
        continue
      if func == "run_shell":
        # GOAL_LOOP_READONLY_V1: read-only shell branch (see method header).
        args = item.get("args") if isinstance(item.get("args"), dict) else {}
        cmd = args.get("command")
        if not isinstance(cmd, str):
          cmd = ""
        if is_readonly_command(cmd):
          if bool(item.get("ok")):
            executed_any = True
          continue
        uncheckable = True
        continue
      if func not in self._GOAL_CHECKABLE_TOOLS:
        uncheckable = True
        continue
      args = item.get("args") if isinstance(item.get("args"), dict) else {}
      kind = self._GOAL_CHECKABLE_TOOLS[func]
      # kill_process with pid uses pid_not_alive, not process_not_running.
      if func == "kill_process" and args.get("pid"):
        kind = "pid_not_alive"
      target = self._goal_target(kind, args)
      if not target:
        # Checkable tool without a target cannot be verified.
        uncheckable = True
        continue
      key = self._goal_category(kind, target)
      # Last wins: delete then re-insert to keep last order.
      if key in criteria:
        del criteria[key]
      criteria[key] = {"kind": kind, "target": target, "source": "loop"}
    return list(criteria.values()), uncheckable, executed_any

  def _goal_check(self, criterion: dict, ctx: Any = None) -> tuple[str, str]:
    # Returns ("pass"/"fail"/"unknown", reason).
    # Read-only only: no execute_call, no run_post_check, no self-healing.
    kind = str(criterion.get("kind") or "")
    target = str(criterion.get("target") or "")
    if not kind or not target:
      return "unknown", "criterion missing kind/target"
    try:
      if kind in ("process_running", "process_not_running"):
        from ..tools.process_ops import is_app_running

        # Q4_TASK_GOAL_VERIFY_V1_1: use ctx.memory / launch_target,
        # like WindowsVerifier._check_open_app (windows/verifier.py
        # :217-218). Without them, Chinese names cannot resolve:
        # NAME_ALIASES is empty, find_desktop_app returns a UWP appid
        # (no exe), infer_process_names == []. Production
        # apps_registry already learned 记事本 -> ["notepad"]
        # (verified=1, hit_count=190); memory=None discards it.
        _launch = ""
        _memory = None
        if ctx is not None:
          _memory = getattr(ctx, "memory", None)
          _app_manager = getattr(ctx, "app_manager", None)
          _resolve = getattr(_app_manager, "resolve_command", None) if _app_manager is not None else None
          if callable(_resolve):
            try:
              _launch = str(_resolve(target) or "")
            except Exception:
              _launch = ""
        # Q4_TASK_GOAL_VERIFY_V1_2 / Q4_TASK_GOAL_VERIFY_V1_3:
        # Resolved-or-unknown guard, refined with real process_ops.py and
        # MemoryService.get_app_process_names lookup.
        # Proof of resolution (any one suffices; UWP appid alone NEVER counts):
        # (a) launch_cmd yields a non-appid exe/ASCII process token;
        # (b) memory.get_app_process_names(target) yields non-protected process names
        #     (covers learned Chinese UWP apps like 记事本 -> ["notepad"]);
        # (c) infer_process_names(target, _launch) yields non-protected process names
        #     from NAME_ALIASES or desktop .lnk shortcut target;
        # (d) target itself is an ASCII process name (e.g. notepad).
        from ..tools.process_ops import _HELPER_PROCESSES, _PROTECTED, infer_process_names

        def _valid_proc_names(items: Any) -> list[str]:
          out_names: list[str] = []
          for item in (items or []):
            stem = str(item or "").replace(".exe", "", 1).strip()
            if not stem:
              continue
            low = stem.lower()
            if low in _PROTECTED or low in _HELPER_PROCESSES:
              continue
            out_names.append(stem)
          return out_names

        _resolved = False
        if _launch:
          _lc = _launch.strip()
          if not _lc.startswith(("appid:", "shell:", "http://", "https://", "microsoft-edge:")) and "Microsoft." not in _lc:
            _tok = _lc.replace("\\", "/").split("/")[-1].split()[0] if _lc else ""
            if _tok.lower().endswith(".exe") or (_tok and all(ord(c) < 128 for c in _tok)):
              _resolved = True
        if not _resolved and _memory is not None:
          _get_pnames = getattr(_memory, "get_app_process_names", None)
          if callable(_get_pnames):
            try:
              if _valid_proc_names(_get_pnames(target)):
                _resolved = True
            except Exception:
              pass
        if not _resolved:
          try:
            if _valid_proc_names(infer_process_names(target, _launch)):
              _resolved = True
          except Exception:
            pass
        if not _resolved and target and all(ord(c) < 128 for c in target):
          _resolved = True
        if not _resolved and any(ord(ch) > 127 for ch in target):
          return "unknown", f"process {target} cannot be resolved via app_manager/memory/alias/exe"
        try:
          running = bool(is_app_running(target, _launch, memory=_memory))
        except TypeError:
          # Older is_app_running without a memory kwarg: fall back to
          # positional launch_target only, still better than memory=None.
          running = bool(is_app_running(target, _launch))
        ok = running if kind == "process_running" else (not running)
        return ("pass" if ok else "fail", f"process {target} running={running}")
      if kind in ("window_visible", "window_not_visible"):
        from ..tools.window_ops import window_title_visible

        visible = bool(window_title_visible(target))
        ok = visible if kind == "window_visible" else (not visible)
        return ("pass" if ok else "fail", f"window {target} visible={visible}")
      if kind in ("file_exists", "file_not_exists"):
        from .path_verify import path_still_exists

        exists = bool(path_still_exists(target))
        ok = exists if kind == "file_exists" else (not exists)
        return ("pass" if ok else "fail", f"path {target} exists={exists}")
      if kind == "pid_not_alive":
        from ..tools.process_ops import is_process_alive

        alive = bool(is_process_alive(target))
        return ("pass" if not alive else "fail", f"pid {target} alive={alive}")
      if kind == "package_not_installed":
        from ..tools.package_ops import is_package_installed

        installed = bool(is_package_installed(target))
        return ("pass" if not installed else "fail", f"package {target} installed={installed}")
    except Exception as exc:  # check error != goal not achieved
      return "unknown", f"check error for {kind} {target}: {exc}"
    return "unknown", f"unknown criterion kind {kind}"

  def _goal_nonstate_check(self, criterion: dict, task: Any, tool_results: Any) -> tuple[str, str]:
    # KEKESI_CONTRACT_V0: readonly/query 判定（不走 OS 探针）。
    ctype = str(criterion.get("type") or "").strip()
    target = str(criterion.get("target") or "").strip()
    if not target:
      return "unknown", "criterion missing target"
    results = tool_results if isinstance(tool_results, list) else []
    if ctype == "readonly":
      for item in results:
        if not isinstance(item, dict):
          continue
        func = str(item.get("func") or item.get("name") or "").strip()
        if func == target and item.get("ok"):
          return "pass", ""
      return "unknown", f"readonly tool not executed: {target}"
    if ctype == "query":
      haystack = str(getattr(task, "reply", "") or "")
      for item in results:
        if isinstance(item, dict):
          haystack += "\n" + str(item.get("summary") or "") + "\n" + str(item.get("text") or "")
      if target in haystack:
        return "pass", ""
      return "fail", f"reply lacks expected content: {target}"
    return "unknown", f"unknown non-state criterion type: {ctype}"

  def verify_goal(
    self,
    task: Any,
    plan: Any,
    ctx: Any = None,
    *,
    mcp_bridge: Any = None,
  ) -> Evidence:
    # ctx is used for process resolution only (memory / launch_target,
    # see _goal_check); mcp_bridge is accepted for call-site
    # compatibility with verify_action. Goal checking itself is
    # read-only OS state: no run_post_check, no self-healing execute.
    goal = str(getattr(task, "goal", "") or "")
    tool_results = getattr(task, "tool_results", None)
    _acceptance = getattr(plan, "acceptance", None)
    if _acceptance:
      plan_criteria = self._goal_acceptance_criteria(plan)
    else:
      plan_criteria = self._goal_plan_criteria(plan)
    rule_criteria = self._goal_rule_criteria(goal)
    loop_criteria, loop_uncheckable, loop_executed = self._goal_loop_criteria(tool_results)
    # Deduplicate by target category, priority plan > loop > rule.
    # Plan criteria win over loop/rule for the same target category:
    # if plan says open and loop closed the same app, only the plan
    # criterion is checked, and it will fail correctly (not running),
    # instead of requiring both running AND not running at once.
    merged: dict[tuple[str, str], dict] = {}
    for criterion in plan_criteria + loop_criteria + rule_criteria:
      key = self._goal_category(str(criterion["kind"]), str(criterion["target"]))
      if key not in merged:
        merged[key] = criterion
    criteria = list(merged.values())
    if loop_uncheckable:
      return Evidence.unknown(
        "goal verification unknown: loop used an uncheckable goal-changing tool"
      )
    if not criteria:
      if loop_executed:
        return Evidence.from_execution(
          verified=True,
          checks=[],
          observed_state={"goal_criteria": [], "loop_executed_no_criteria": True},
          reason="",
        )
      return Evidence.unknown("goal verification unknown: no verifiable goal criteria")
    checks: list[dict] = []
    failed: list[str] = []
    unknown_reasons: list[str] = []
    for criterion in criteria:
      if criterion.get("type") in ("readonly", "query"):
        status, reason = self._goal_nonstate_check(criterion, task, tool_results)
      else:
        status, reason = self._goal_check(criterion, ctx)
      entry = {
        "type": "goal",
        "kind": criterion["kind"],
        "target": criterion["target"],
        "source": criterion["source"],
        "ok": status == "pass",
      }
      if criterion.get("window_unchecked"):
        entry["window_unchecked"] = True
      checks.append(entry)
      if status == "fail":
        failed.append(reason)
      elif status == "unknown":
        unknown_reasons.append(reason)
    if unknown_reasons:
      return Evidence.unknown("goal verification unknown: " + "; ".join(unknown_reasons)[:200])
    if failed:
      return Evidence.failed("goal verification failed: " + "; ".join(failed)[:200])
    return Evidence.from_execution(
      verified=True,
      checks=checks,
      observed_state={"goal_criteria": criteria},
      reason="",
    )

  def verify(
    self,
    func: str,
    args: dict,
    r1: R1Response,
    ctx: ToolContext,
    *,
    mcp_bridge: Any | None = None,
  ) -> tuple[R1Response, list[dict], bool, str]:
    """向后兼容：R1 管道验证，返回 (r1, extra_steps, ok, summary)。"""
    resp, extra = run_post_check(
      func,
      args,
      r1,
      ctx,
      mcp_bridge=mcp_bridge,
    )
    out = r1_to_tool_output(resp)
    ok = resp.status == STATUS_SUCCESS and out.ok
    summary = (out.text or "")[:200]
    if not ok and resp.data.get("reason") == "verify_failed":
      logger.info("Verifier 降级 %s: %s", func, summary[:80])
    return resp, extra, ok, summary

  def evaluate(
    self,
    func: str,
    args: dict,
    r1: R1Response,
    ctx: ToolContext,
    *,
    mcp_bridge: Any | None = None,
  ) -> tuple[R1Response, list[dict], bool, str]:
    return self.verify(func, args, r1, ctx, mcp_bridge=mcp_bridge)


def needs_verification(func: str) -> bool:
  return verification_enabled(func)
