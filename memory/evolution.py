"""P3 evolution closed loop for Kekesi memory.

Marker: KEKESI_PCE_P3_20261101
Anchor: kekesi main 6436da4 (2026-11-01). The parallel P2 deliverable
``kekesi/memory/cognition.py`` exposes ``query_model(kind, key)`` with
kind in {"world", "user", "self"}; it is imported lazily so this module
keeps working before P2 lands (cognition gaps then simply come back empty).

Loop (closed across Evolver ticks, not within one call):
    evaluate() -> propose_goals() -> adopt_graded() -> behavior change
    -> fresh task rows -> evaluate() again.

Safety rules (locked by the user):
  * adopt_graded() NEVER applies a proposal that touches system files or
    could damage the system: such proposals go to the quarantine log and
    return "quarantined"; the applier is never invoked on that path.
  * Unknown kinds and unknown risk fail closed to "pending", never auto.
  * prune_skills() deletes only skills unactivated for >= 100 days, and
    only by moving the skill directory to a dated backup outside the
    skill pool (no auto-restore). Nothing is deleted without a backup.
  * This module is stdlib-only and imports nothing from kekesi at module
    level, so importing it can never break the Evolver tick.
"""

from __future__ import annotations

import ast
import json
import shutil
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

MARKER = "KEKESI_PCE_P3_20261101"
PRUNE_DAYS_THRESHOLD = 100
MAX_GOALS = 10

__all__ = [
    "MARKER",
    "PRUNE_DAYS_THRESHOLD",
    "evaluate",
    "propose_goals",
    "adopt_graded",
    "prune_skills",
    "uninstall_skill",
    "render_executable_main",
    "upgrade_crystallized_main",
    "run_cycle",
]


def _utcnow_iso():
    return datetime.now(timezone.utc).isoformat()


def _repo_root():
    # evolution.py lives at <root>/kekesi/memory/evolution.py
    return Path(__file__).resolve().parents[2]


def _data_dir():
    return _repo_root() / "data" / "evolution"


def _skills_root():
    return _repo_root() / "kekesi" / "skills"


def _append_jsonl(path, record):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def _row_get(row, *names, default=None):
    for name in names:
        if isinstance(row, dict):
            if name in row:
                return row[name]
        else:
            value = getattr(row, name, None)
            if value is not None:
                return value
    return default


def _success_flag(value):
    if value is True or value == 1:
        return True
    if value is False or value == 0:
        return False
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("1", "true", "ok", "success", "succeeded"):
            return True
        if text in ("0", "false", "fail", "failed", "error"):
            return False
    return None


def _parse_created_at(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, (int, float)):
        stamp = float(value)
        if stamp > 1e12:
            stamp /= 1000.0
        try:
            return datetime.fromtimestamp(stamp, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            try:
                return _parse_created_at(float(text))
            except (TypeError, ValueError):
                return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return None


_INTERNAL_ACTION_PREFIXES = (
    "self_study",
    "self_study:",
    "internal:",
    "task_history",
    "scheduler:",
    "proactor:",
    "system_status",
    "memory_internal",
)


def _is_internal_action_name(name: str) -> bool:
    s = str(name or "").strip().lower()
    if not s or s == "unknown":
        return True
    for pref in _INTERNAL_ACTION_PREFIXES:
        if s == pref or s.startswith(pref):
            return True
    return False


def _is_valid_evolution_study_topic(topic: str, source: str = "evolution_gap") -> bool:
    s = str(topic or "").strip()
    if not s:
        return False
    try:
        from kekesi.memory.self_study import is_valid_study_topic
        return bool(is_valid_study_topic(s, source=source))
    except Exception:
        low = s.lower()
        for bad in (
            "self_study:",
            "self_study",
            "internal:",
            "task_history",
            "失败原因与改进",
            "能力短板补齐",
            "执行失败",
            "调用失败",
            "open:",
            "open_app",
            "close:",
            "close_app",
            "skill:",
            "skill__",
            "mcp__",
        ):
            if bad in low:
                return False
        return len(s) >= 2


def evaluate(task_rows=None, exec_rows=None, error_rows=None):
    """Self-evaluation from history rows.

    task_rows: task_history rows (action, success, created_at).
    exec_rows: execution_history rows (tool, success).
    error_rows: error_corrections rows (signature/tool, count).
    error_rows refine the failure ranking only; they never change the
    success rate, because they may duplicate task rows.
    """
    total = 0
    ok_count = 0
    failures = Counter()

    def feed(rows):
        nonlocal total, ok_count
        for row in rows or []:
            name = str(_row_get(row, "action", "tool", "name") or "unknown").strip()
            if _is_internal_action_name(name):
                continue
            flag = _success_flag(_row_get(row, "success", "ok"))
            if flag is None:
                continue
            total += 1
            if flag:
                ok_count += 1
            else:
                failures[name] += 1

    feed(task_rows)
    feed(exec_rows)
    for row in error_rows or []:
        signature = str(_row_get(row, "signature", "tool", "name") or "unknown").strip()
        if _is_internal_action_name(signature):
            continue
        try:
            count = int(_row_get(row, "count", default=1) or 1)
        except (TypeError, ValueError):
            count = 1
        failures[signature] += count
    top = [{"tool": tool, "count": count} for tool, count in failures.most_common(5)]
    return {
        "total": total,
        "ok": ok_count,
        "failed": total - ok_count,
        "success_rate": (ok_count / total) if total else None,
        "top_failures": top,
        "most_failed_capability": top[0]["tool"] if top else None,
    }


def _load_task_rows(limit=200):
    """Best-effort read of recent task_history rows (read-only).

    Uses the existing retrieval API if present; adapts to its signature
    (limit kwarg or no args). Any failure degrades to an empty list.
    """
    try:
        from kekesi.memory import retrieval
    except Exception:
        return []
    fn = getattr(retrieval, "get_task_history", None)
    if fn is None:
        fn = getattr(retrieval, "_recall_task_history", None)
    if fn is None:
        return []
    try:
        rows = fn(limit=limit)
    except TypeError:
        try:
            rows = fn()
        except Exception:
            return []
    except Exception:
        return []
    try:
        return list(rows or [])
    except Exception:
        return []


def _normalize_topics(raw):
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw.strip()] if raw.strip() else []
    if isinstance(raw, (list, tuple)):
        out = []
        for item in raw:
            text = str(item).strip()
            if text:
                out.append(text)
        return out
    if isinstance(raw, dict):
        for key in ("gaps", "learning_gaps", "topics", "items"):
            if isinstance(raw.get(key), (list, tuple)):
                return _normalize_topics(raw[key])
        out = []
        for key, value in raw.items():
            if value:
                out.append(str(key) + ": " + str(value))
        return out
    return [str(raw).strip()] if str(raw).strip() else []


def _cognition_gaps():
    """Learning gaps from the P2 self model (empty before P2 lands)."""
    try:
        from kekesi.memory import cognition
    except Exception:
        return []
    query = getattr(cognition, "query_model", None)
    if query is None:
        return []
    for key in ("learning_gaps", "gaps"):
        try:
            raw = query("self", key)
        except Exception:
            raw = None
        if raw:
            return _normalize_topics(raw)
    return []


def propose_goals(eval_result=None, extra_gaps=None):
    """Learning goals from gaps, as validated academic/technical topic strings for StudyAnalyst.

    Internal tool failures (e.g. self_study:*, open:*, skill:*, task_history) are handled
    by LearningService/execution_policies and MUST NOT pollute the academic self-study queue.
    """
    topics = []

    def add(topic):
        topic = str(topic or "").strip()
        if topic and topic not in topics and _is_valid_evolution_study_topic(topic):
            topics.append(topic)

    ev = eval_result or {}
    for gap in _normalize_topics(ev.get("study_topics") or ev.get("research_gaps")):
        add(gap)
    for gap in _cognition_gaps():
        add(gap)
    for gap in _normalize_topics(extra_gaps):
        add(gap)
    return topics[:MAX_GOALS]


_SYSTEM_KINDS = ("system_file", "system_config", "system")
_AUTO_KINDS = ("text", "parameter")


def adopt_graded(proposal, applier=None, data_dir=None):
    """Graded adoption. Never auto-applies system-affecting proposals.

    proposal fields: id (optional), kind, risk ("low"/"high"),
    touches_system_files (bool), payload (any).
    Returns a dict with status one of: quarantined, auto_applied,
    recorded, pending, apply_failed. "recorded" means low-risk but no
    applier was bound, so the change is only logged for the owning
    subsystem; it is NOT claimed as applied.
    """
    proposal = dict(proposal or {})
    pid = proposal.get("id") or uuid.uuid4().hex[:12]
    kind = str(proposal.get("kind", "")).strip()
    risk = str(proposal.get("risk", "")).strip().lower()
    touches = bool(proposal.get("touches_system_files"))
    base = Path(data_dir) if data_dir else _data_dir()
    record = {
        "id": pid,
        "kind": kind,
        "risk": risk,
        "touches_system_files": touches,
        "proposal": proposal,
        "at": _utcnow_iso(),
    }
    if touches or kind in _SYSTEM_KINDS:
        record["status"] = "quarantined"
        _append_jsonl(base / "adopt_quarantine.jsonl", record)
        return {
            "id": pid,
            "status": "quarantined",
            "applied": False,
            "reason": "touches system files or could damage the system; isolated pending user confirmation",
        }
    if risk == "low" and kind in _AUTO_KINDS:
        if applier is None:
            record["status"] = "recorded"
            _append_jsonl(base / "adopted.jsonl", record)
            return {
                "id": pid,
                "status": "recorded",
                "applied": False,
                "reason": "low-risk; no applier bound, recorded for the owning subsystem",
            }
        try:
            result = applier(proposal)
        except Exception as exc:
            record["status"] = "apply_failed"
            record["error"] = str(exc)
            _append_jsonl(base / "adopted.jsonl", record)
            return {"id": pid, "status": "apply_failed", "applied": False, "error": str(exc)}
        record["status"] = "auto_applied"
        record["result"] = result
        _append_jsonl(base / "adopted.jsonl", record)
        return {"id": pid, "status": "auto_applied", "applied": True, "result": result}
    record["status"] = "candidate"
    _append_jsonl(base / "adopt_pending.jsonl", record)
    return {
        "id": pid,
        "status": "pending",
        "applied": False,
        "reason": "high-risk or unknown kind; pending approval",
    }


def _skill_key_from_action(action):
    if not isinstance(action, str) or not action.startswith("skill__"):
        return None
    parts = action.split("__")
    if len(parts) < 3:
        return None
    return parts[1] + "/" + parts[2]


def uninstall_skill(skill_key, skills_root=None, data_dir=None, now=None):
    """Uninstall entry: move one skill out of the pool into a dated backup.

    The move IS the deletion from the pool and the backup in one step;
    nothing is auto-restored.
    """
    root = Path(skills_root) if skills_root else _skills_root()
    base = Path(data_dir) if data_dir else _data_dir()
    category, _, name = skill_key.partition("/")
    src = root / category / name
    if not src.is_dir():
        return {"skill": skill_key, "status": "not_found", "deleted": False}
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%d")
    backup_root = base / "skill_backups"
    backup_root.mkdir(parents=True, exist_ok=True)
    dest = backup_root / (skill_key.replace("/", "__") + "_" + stamp)
    suffix = 1
    while dest.exists():
        suffix += 1
        dest = backup_root / (skill_key.replace("/", "__") + "_" + stamp + "_" + str(suffix))
    shutil.move(str(src), str(dest))
    _append_jsonl(base / "pruned.jsonl", {
        "skill": skill_key,
        "status": "deleted",
        "backup": str(dest),
        "at": _utcnow_iso(),
    })
    return {"skill": skill_key, "status": "deleted", "deleted": True, "backup": str(dest)}


def prune_skills(task_rows=None, skills_root=None, data_dir=None,
                 days_threshold=PRUNE_DAYS_THRESHOLD, dry_run=False, now=None):
    """Prune skills unactivated for >= days_threshold days.

    Health per skill = usage + failure rate + days since last activation.
    Usage and last activation come from task_history actions
    (skill__<category>__<skill>__<tool>); skills with no task rows fall
    back to the skill directory mtime as a last-active proxy. Deletion
    goes through uninstall_skill (move to outside-pool backup).
    """
    now_dt = now or datetime.now(timezone.utc)
    usage = {}
    failed = {}
    last_active = {}
    for row in task_rows or []:
        action = _row_get(row, "action", "tool", "name")
        key = _skill_key_from_action(action)
        if key is None:
            continue
        usage[key] = usage.get(key, 0) + 1
        flag = _success_flag(_row_get(row, "success", "ok"))
        if flag is False:
            failed[key] = failed.get(key, 0) + 1
        stamp = _parse_created_at(_row_get(row, "created_at"))
        if stamp is not None and (key not in last_active or stamp > last_active[key]):
            last_active[key] = stamp
    root = Path(skills_root) if skills_root else _skills_root()
    deleted = []
    kept = []
    if root.is_dir():
        for category_dir in sorted(root.iterdir()):
            if not category_dir.is_dir():
                continue
            for skill_dir in sorted(category_dir.iterdir()):
                if not skill_dir.is_dir():
                    continue
                key = category_dir.name + "/" + skill_dir.name
                total = usage.get(key, 0)
                fails = failed.get(key, 0)
                if key in last_active:
                    days = (now_dt - last_active[key]).days
                    source = "task_history"
                else:
                    try:
                        mtime = datetime.fromtimestamp(skill_dir.stat().st_mtime, tz=timezone.utc)
                    except OSError:
                        mtime = None
                    days = (now_dt - mtime).days if mtime is not None else None
                    source = "dir_mtime_proxy"
                health = {
                    "usage": total,
                    "failures": fails,
                    "failure_rate": (fails / total) if total else None,
                    "days_since_active": days,
                    "source": source,
                }
                if days is not None and days >= days_threshold:
                    if dry_run:
                        deleted.append({"skill": key, "status": "would_delete", "health": health})
                    else:
                        entry = uninstall_skill(key, skills_root=root, data_dir=data_dir, now=now_dt)
                        entry["health"] = health
                        deleted.append(entry)
                else:
                    kept.append({"skill": key, "health": health})
    return {
        "deleted": deleted,
        "kept": kept,
        "threshold_days": days_threshold,
        "dry_run": dry_run,
    }


_RUNNER_TEMPLATE = """# Crystallized skill step runner (KEKESI_PCE_P3_20261101).
# STEPS are plain data: dicts with name/tool/action + args/params, or
# plain strings. main() executes them in order through dispatch, a
# callable taking (name, args) supplied by the skill host. Without a
# bound dispatch, steps are recorded as unbound, never claimed as done.

STEPS = __STEPS_REPR__

def main(dispatch=None):
    outcomes = []
    for step in STEPS:
        if isinstance(step, dict):
            name = (
                step.get("name")
                or step.get("tool")
                or step.get("action")
                or step.get("func")
                or "step"
            )
            args = step.get("args") or step.get("params") or {}
        else:
            name, args = str(step), {}
        if dispatch is None:
            outcomes.append({"step": name, "ok": False, "error": "no dispatch bound"})
            continue
        try:
            result = dispatch(name, args)
            outcomes.append({"step": name, "ok": True, "result": result})
        except Exception as exc:
            outcomes.append({"step": name, "ok": False, "error": str(exc)})
    return outcomes


def run_tool(tool_id: str = "run", args: dict | None = None) -> tuple[bool, str]:
    dispatch = (args or {}).get("_dispatch") if isinstance(args, dict) else None
    if callable(dispatch):
        outcomes = main(dispatch=dispatch)
        ok = all(bool(item.get("ok")) for item in outcomes) if outcomes else False
        summary = " -> ".join(str(item.get("step", "")) for item in outcomes)
        return ok, f"已执行 {len(outcomes)} 步：{summary}"
    names = [
        (s.get("func") or s.get("name") or s.get("tool") or str(s))
        if isinstance(s, dict)
        else str(s)
        for s in STEPS
    ]
    return True, f"结晶技能步骤（共 {len(names)} 步）：{' -> '.join(str(x) for x in names)}"


if __name__ == "__main__":
    import json
    print(json.dumps(main(), ensure_ascii=False, default=str))
"""


def render_executable_main(steps):
    """Render an executable step-runner main.py for crystallized STEPS."""
    try:
        steps_repr = repr(list(steps or []))
        ast.parse(steps_repr)
    except Exception:
        steps_repr = "[]"
    src = _RUNNER_TEMPLATE.replace("__STEPS_REPR__", steps_repr)
    try:
        ast.parse(src)
    except SyntaxError:
        src = _RUNNER_TEMPLATE.replace("__STEPS_REPR__", "[]")
    return src


def upgrade_crystallized_main(package_dir, data_dir=None):
    """Rewrite an already-crystallized stub main.py into the runner.

    Keeps an existing STEPS literal if one parses; otherwise starts with
    an empty step list. The old file is kept once as main.py.bak.
    """
    pkg = Path(package_dir)
    main_py = pkg / "main.py"
    if not main_py.is_file():
        return {"package": str(pkg), "status": "no_main_py", "upgraded": False}
    text = main_py.read_text(encoding="utf-8")
    if MARKER in text:
        return {"package": str(pkg), "status": "already_runner", "upgraded": False}
    steps = []
    try:
        tree = ast.parse(text)
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "STEPS" for target in node.targets
            ):
                steps = ast.literal_eval(node.value)
                break
    except Exception:
        steps = []
    if not isinstance(steps, list):
        steps = []
    bak = pkg / "main.py.bak"
    if not bak.exists():
        bak.write_text(text, encoding="utf-8", newline="\n")
    main_py.write_text(render_executable_main(steps), encoding="utf-8", newline="\n")
    return {"package": str(pkg), "status": "upgraded", "upgraded": True, "steps": len(steps)}


def run_cycle(task_rows=None, proposals=None, study_submit=None, goal_sink=None,
              data_dir=None, dry_run=False, now=None):
    """One closed-loop turn. Never raises: stage errors are recorded.

    The loop closes across Evolver ticks: this turn's adopt/prune
    changes behavior, and the next tick's evaluate() reads fresh rows.
    """
    report = {"marker": MARKER, "dry_run": dry_run, "errors": []}
    try:
        rows = list(task_rows) if task_rows is not None else _load_task_rows()
    except Exception as exc:
        rows = []
        report["errors"].append("load_task_rows: " + str(exc))
    try:
        evaluation = evaluate(task_rows=rows)
    except Exception as exc:
        evaluation = {
            "total": 0, "ok": 0, "failed": 0, "success_rate": None,
            "top_failures": [], "most_failed_capability": None,
        }
        report["errors"].append("evaluate: " + str(exc))
    report["evaluation"] = evaluation
    try:
        goals = propose_goals(evaluation)
    except Exception as exc:
        goals = []
        report["errors"].append("propose_goals: " + str(exc))
    report["goals"] = goals
    if study_submit is not None and goals:
        try:
            report["study_submitted"] = study_submit(goals)
        except Exception as exc:
            report["study_submitted"] = None
            report["errors"].append("study_submit: " + str(exc))
    # KEKESI_GOAL_SINK_V1: 目标持久化 v1——Evolver 目标同步写 goals 表
    # （与 study_submit 并行消费，互不替代；失败只记 errors，不中断 tick）。
    if goal_sink is not None and goals:
        try:
            report["goal_saved"] = goal_sink(goals, source="evolution")
        except Exception as exc:
            report["goal_saved"] = None
            report["errors"].append("goal_sink: " + str(exc))
    adopted = []
    for proposal in proposals or []:
        try:
            adopted.append(adopt_graded(proposal, data_dir=data_dir))
        except Exception as exc:
            adopted.append({"status": "error", "error": str(exc)})
    report["adopted"] = adopted
    try:
        report["pruned"] = prune_skills(task_rows=rows, data_dir=data_dir,
                                        dry_run=dry_run, now=now)
    except Exception as exc:
        report["pruned"] = {"deleted": [], "kept": [], "error": str(exc)}
        report["errors"].append("prune_skills: " + str(exc))
    report["at"] = _utcnow_iso()
    if not dry_run:
        try:
            log_dir = Path(data_dir) if data_dir else _data_dir()
            _append_jsonl(log_dir / "cycles.jsonl", report)
        except Exception:
            pass
    return report
