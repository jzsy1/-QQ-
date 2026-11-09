"""工作流存取与执行 — 由 evolver 自动沉淀，也可手动保存。"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)

_MAX_STEPS = 12


def list_workflows(db, *, limit: int = 20) -> tuple[bool, str]:
    if not db:
        return False, "记忆库不可用"
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT name, title, trigger_hint, confidence, status, hit_count
               FROM workflows
               WHERE status IN ('draft', 'confirmed')
               ORDER BY confidence DESC, hit_count DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
    if not rows:
        return True, "暂无已学习的工作流。"
    lines = []
    for r in rows:
        lines.append(
            f"- {r['name']}｜{r['title'] or r['trigger_hint'] or ''}｜"
            f"{r['status']}｜置信{float(r['confidence'] or 0):.2f}"
        )
    return True, "已学会的工作流：\n" + "\n".join(lines)


def save_workflow(
    db,
    name: str,
    steps: list[dict],
    *,
    title: str = "",
    trigger_hint: str = "",
) -> tuple[bool, str]:
    name = (name or "").strip()
    if not name or not steps:
        return False, "需要名称和步骤"
    if len(steps) > _MAX_STEPS:
        return False, f"步骤过多（最多 {_MAX_STEPS}）"
    clean = []
    for s in steps:
        if not isinstance(s, dict):
            continue
        func = str(s.get("func") or s.get("tool") or s.get("name") or "").strip()
        if not func:
            continue
        args = s.get("args") if isinstance(s.get("args"), dict) else {}
        clean.append({"func": func, "args": args})
    if not clean:
        return False, "没有有效步骤"
    payload = json.dumps(clean, ensure_ascii=False)
    with db.connect() as conn:
        conn.execute(
            """INSERT INTO workflows
               (name, title, steps, trigger_hint, confidence, status, source)
               VALUES (?, ?, ?, ?, 0.8, 'confirmed', 'manual')
               ON CONFLICT(name) DO UPDATE SET
                 title = excluded.title,
                 steps = excluded.steps,
                 trigger_hint = excluded.trigger_hint,
                 confidence = MAX(workflows.confidence, 0.8),
                 status = 'confirmed',
                 updated_at = datetime('now')""",
            (name, (title or name)[:80], payload, (trigger_hint or "")[:120]),
        )
        conn.commit()
    return True, f"已保存工作流「{name}」，共 {len(clean)} 步。"


_NESTED_BLOCK = frozenset({"run_sequence", "run_workflow", "save_workflow", "list_workflows", "execute_gui_macro"})
_HIGH_RISK_WORKFLOW_TOOLS = frozenset({
    "delete_file",
    "move_to_trash",
    "empty_recycle_bin",
    "run_shell",
    "run_shell_admin",
    "run_shell_system",
    "kill_process",
    "write_registry",
    "uninstall_software",
    "install_software",
    "system_power",
    "repair_system",
    "take_ownership",
    "grant_full_control",
    "block_app_network",
})
_WORKFLOW_CONFIRM_WORDS = ("确认执行", "确认运行", "确定执行", "同意执行", "批准执行", "确认删除")
_WORKFLOW_EXPLICIT_WORDS = ("流程", "工作流", "自动化", "上次那套", "一键", "按以前的步骤", "老规矩")


def inspect_workflow_preconditions(
    steps: list[dict],
    *,
    check_paths: bool = True,
) -> dict[str, Any]:
    """检查工作流步骤的前置条件、参数完整性与高危工具权限要求。"""
    import os
    import re

    if not isinstance(steps, list) or not steps:
        return {
            "valid": False,
            "reason": "工作流步骤为空或格式无效",
            "requires_confirmation": False,
            "high_risk_tools": [],
            "missing_paths": [],
            "step_tools": [],
        }
    if len(steps) > _MAX_STEPS:
        return {
            "valid": False,
            "reason": f"步骤数量超限（{len(steps)} > {_MAX_STEPS}）",
            "requires_confirmation": False,
            "high_risk_tools": [],
            "missing_paths": [],
            "step_tools": [],
        }

    high_risk: list[str] = []
    missing_paths: list[str] = []
    step_tools: list[str] = []

    for idx, step in enumerate(steps, 1):
        if not isinstance(step, dict):
            return {
                "valid": False,
                "reason": f"第 {idx} 步不是有效字典",
                "requires_confirmation": False,
                "high_risk_tools": [],
                "missing_paths": [],
                "step_tools": step_tools,
            }
        func = str(step.get("func") or step.get("tool") or step.get("name") or "").strip()
        if not func:
            return {
                "valid": False,
                "reason": f"第 {idx} 步缺少工具名 (func)",
                "requires_confirmation": False,
                "high_risk_tools": [],
                "missing_paths": [],
                "step_tools": step_tools,
            }
        if func in _NESTED_BLOCK:
            return {
                "valid": False,
                "reason": f"第 {idx} 步包含禁止嵌套的工具「{func}」",
                "requires_confirmation": False,
                "high_risk_tools": [],
                "missing_paths": [],
                "step_tools": step_tools,
            }
        args = step.get("args")
        if args is not None and not isinstance(args, dict):
            return {
                "valid": False,
                "reason": f"第 {idx} 步 ({func}) 参数 args 须为字典",
                "requires_confirmation": False,
                "high_risk_tools": [],
                "missing_paths": [],
                "step_tools": step_tools,
            }
        args_dict = args or {}
        step_tools.append(func)
        if func in _HIGH_RISK_WORKFLOW_TOOLS and func not in high_risk:
            high_risk.append(func)
        if check_paths and func in ("open_path", "delete_file", "move_to_trash", "copy_path", "move_path", "rename_path"):
            p = str(args_dict.get("path") or args_dict.get("src") or "").strip()
            if p and re.match(r"^[A-Za-z]:[/\\]", p) and not os.path.exists(p):
                missing_paths.append(p)

    if missing_paths:
        return {
            "valid": False,
            "reason": "前置条件未满足，以下目标路径不存在：" + "、".join(missing_paths[:3]),
            "requires_confirmation": bool(high_risk),
            "high_risk_tools": high_risk,
            "missing_paths": missing_paths,
            "step_tools": step_tools,
        }

    return {
        "valid": True,
        "reason": "ok",
        "requires_confirmation": bool(high_risk),
        "high_risk_tools": high_risk,
        "missing_paths": [],
        "step_tools": step_tools,
    }


def search_workflows(
    db,
    query: str,
    *,
    limit: int = 3,
    min_score: float = 0.28,
) -> list[dict[str, Any]]:
    """根据用户请求语义/关键词匹配可用工作流，附带前置条件与风险标记。"""
    if not db:
        return []
    q = (query or "").strip()
    if not q:
        return []
    explicit_ask = any(w in q for w in _WORKFLOW_EXPLICIT_WORDS)
    q_low = q.lower()

    # 提取 2-gram 与关键词供匹配
    import re
    tokens = [
        t for t in re.findall(r"[\u4e00-\u9fff]{2,6}|[a-zA-Z0-9_]{2,20}", q_low)
        if t not in ("工作流", "流程", "自动化", "帮我", "一下", "执行", "运行", "使用", "调用")
    ]
    bigrams: set[str] = set()
    for chunk in re.findall(r"[\u4e00-\u9fff]{2,}", q_low):
        for i in range(len(chunk) - 1):
            bigrams.add(chunk[i : i + 2])
    bigrams -= {"工作", "作流", "流程", "自动", "动化", "帮我", "一下", "执行", "运行"}

    try:
        with db.connect() as conn:
            rows = conn.execute(
                """SELECT id, name, title, steps, trigger_hint, confidence, status, hit_count
                   FROM workflows
                   WHERE status IN ('draft', 'confirmed')
                   ORDER BY confidence DESC, hit_count DESC
                   LIMIT 30"""
            ).fetchall()
    except Exception:
        return []

    scored: list[dict[str, Any]] = []
    for r in rows:
        name = str(r["name"] or "").strip()
        title = str(r["title"] or "").strip()
        hint = str(r["trigger_hint"] or "").strip()
        conf = float(r["confidence"] or 0.5)
        status = str(r["status"] or "draft")
        try:
            steps = json.loads(r["steps"] or "[]")
        except Exception:
            steps = []
        pre = inspect_workflow_preconditions(steps, check_paths=False)
        hay = f"{name} {title} {hint} {' '.join(pre.get('step_tools') or [])}".lower()

        match_score = 0.0
        if name and name.lower() in q_low:
            match_score += 0.85
        if title and len(title) >= 2 and title.lower() in q_low:
            match_score += 0.80
        if hint and len(hint) >= 2 and hint.lower() in q_low:
            match_score += 0.75
        for tok in tokens:
            if tok in hay:
                match_score += 0.35
        if bigrams:
            bg_hits = sum(1 for bg in bigrams if bg in hay)
            match_score += min(0.45, bg_hits * 0.15)

        if match_score < min_score:
            if explicit_ask and not tokens and not bigrams:
                match_score = 0.30 * conf
            elif explicit_ask and conf >= 0.75 and len(rows) <= 5:
                match_score = max(match_score, 0.30)
            else:
                continue

        total_score = round(match_score + conf * 0.2 + (0.05 if status == "confirmed" else 0.0), 4)
        scored.append({
            "id": int(r["id"]),
            "name": name,
            "title": title or hint or name,
            "trigger_hint": hint,
            "status": status,
            "confidence": conf,
            "hit_count": int(r["hit_count"] or 0),
            "steps_count": len(steps) if isinstance(steps, list) else 0,
            "step_tools": pre.get("step_tools") or [],
            "requires_confirmation": bool(pre.get("requires_confirmation")),
            "high_risk_tools": pre.get("high_risk_tools") or [],
            "precondition_valid": bool(pre.get("valid", True)),
            "score": total_score,
        })

    scored.sort(key=lambda x: (x["score"], x["confidence"], x["hit_count"]), reverse=True)
    return scored[: max(1, int(limit))]


def run_sequence(
    steps: list[dict],
    execute_fn: Callable[[str, dict], tuple[bool, str]],
    *,
    stop_on_error: bool = True,
    max_steps: int = 12,
) -> tuple[bool, str]:
    """当场执行临时动作序列（不必先 save_workflow）。

    每步形如 {"func": "gui_action", "args": {...}} 或 {"tool": ..., "args": ...}。
    """
    if not isinstance(steps, list) or not steps:
        return False, "steps 须为非空数组"
    limit = max(1, min(int(max_steps), _MAX_STEPS))
    results: list[str] = []
    all_ok = True
    executed = 0
    for i, step in enumerate(steps[:limit], 1):
        if not isinstance(step, dict):
            results.append(f"{i}.?:无效步骤")
            all_ok = False
            if stop_on_error:
                return False, "序列中断：\n" + "\n".join(results)
            continue
        func = str(step.get("func") or step.get("tool") or step.get("name") or "").strip()
        args = step.get("args") if isinstance(step.get("args"), dict) else {}
        if not func:
            results.append(f"{i}.?:缺少 func")
            all_ok = False
            if stop_on_error:
                return False, "序列中断：\n" + "\n".join(results)
            continue
        if func in _NESTED_BLOCK:
            results.append(f"{i}.{func}:拒绝嵌套")
            all_ok = False
            if stop_on_error:
                return False, "序列中断：\n" + "\n".join(results)
            continue
        ok, msg = execute_fn(func, args)
        executed += 1
        results.append(f"{i}.{func}:{'成功' if ok else '失败'} {(msg or '')[:80]}")
        if not ok:
            all_ok = False
            if stop_on_error:
                return False, "序列中断：\n" + "\n".join(results)
    if executed == 0:
        return False, "没有可执行步骤"
    head = "序列执行完毕" if all_ok else "序列部分失败"
    return all_ok, f"{head}（{executed} 步）：\n" + "\n".join(results)


def run_workflow(
    db,
    name: str,
    execute_fn: Callable[[str, dict], tuple[bool, str]],
    *,
    verify_fn: Callable[[str, dict, str], tuple[bool, str]] | None = None,
    user_confirmed: bool = False,
    user_text: str = "",
) -> tuple[bool, str]:
    name = (name or "").strip()
    if not name:
        return False, "请提供工作流名称"
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id, name, steps, status, confidence FROM workflows WHERE name = ?",
            (name,),
        ).fetchone()
        if not row:
            # 标题模糊
            row = conn.execute(
                """SELECT id, name, steps, status, confidence FROM workflows
                   WHERE title LIKE ? OR trigger_hint LIKE ?
                   ORDER BY confidence DESC LIMIT 1""",
                (f"%{name}%", f"%{name}%"),
            ).fetchone()
            if row:
                name = row["name"]
        if not row:
            return False, f"未找到工作流「{name}」"
        try:
            steps = json.loads(row["steps"] or "[]")
        except json.JSONDecodeError:
            return False, "工作流步骤损坏"
        wf_id = int(row["id"])
        cur_conf = float(row["confidence"] or 0.5)

    if not isinstance(steps, list) or not steps:
        return False, "工作流无步骤"

    pre = inspect_workflow_preconditions(steps, check_paths=True)
    if not pre["valid"]:
        return False, f"工作流「{name}」前置检查未通过：{pre['reason']}"

    has_confirm = bool(user_confirmed) or any(w in (user_text or "") for w in _WORKFLOW_CONFIRM_WORDS)
    if pre["requires_confirmation"] and not has_confirm:
        risk_str = "、".join(pre["high_risk_tools"])
        return (
            False,
            f"工作流「{name}」包含高危或破坏性操作（{risk_str}），为保障安全已拦截自动执行，请向用户确认后再执行。",
        )

    results: list[str] = []
    verified_all = True
    for i, step in enumerate(steps[:_MAX_STEPS], 1):
        if not isinstance(step, dict):
            continue
        func = str(step.get("func") or step.get("tool") or step.get("name") or "").strip()
        args = step.get("args") if isinstance(step.get("args"), dict) else {}
        if not func:
            continue
        ok, msg = execute_fn(func, args)
        if ok and verify_fn is not None:
            try:
                v_ok, v_msg = verify_fn(func, args, msg or "")
                if not v_ok:
                    ok = False
                    verified_all = False
                    msg = f"{msg or ''} [验证未通过: {v_msg}]".strip()
            except Exception as ve:
                ok = False
                verified_all = False
                msg = f"{msg or ''} [验证异常: {ve}]".strip()
        results.append(f"{i}.{func}:{'成功' if ok else '失败'} {(msg or '')[:60]}")
        if not ok:
            new_conf = max(0.0, cur_conf - 0.1)
            new_status = "draft" if new_conf < 0.45 else str(row["status"] or "draft")
            with db.connect() as conn:
                conn.execute(
                    """UPDATE workflows SET
                       confidence = ?,
                       status = ?,
                       updated_at = datetime('now')
                       WHERE id = ?""",
                    (new_conf, new_status, wf_id),
                )
                try:
                    conn.execute(
                        "INSERT INTO task_history (action, result, success) VALUES (?, ?, 0)",
                        (f"workflow:{name}", ("工作流中断: " + "; ".join(results))[:200]),
                    )
                except Exception:
                    pass
                conn.commit()
            return False, "工作流中断：\n" + "\n".join(results)

    new_conf = min(1.0, cur_conf + 0.05)
    new_status = "confirmed" if (verified_all and new_conf >= 0.65) else str(row["status"] or "draft")
    with db.connect() as conn:
        conn.execute(
            """UPDATE workflows SET
               hit_count = hit_count + 1,
               confidence = ?,
               status = ?,
               updated_at = datetime('now')
               WHERE id = ?""",
            (new_conf, new_status, wf_id),
        )
        try:
            conn.execute(
                "INSERT INTO task_history (action, result, success) VALUES (?, ?, 1)",
                (f"workflow:{name}", (f"工作流执行完毕({len(results)}步)")[:200]),
            )
        except Exception:
            pass
        conn.commit()
    return True, f"工作流「{name}」执行完毕：\n" + "\n".join(results)


def record_tool_sequence(
    db,
    tools: list[dict[str, Any]],
    *,
    trigger_hint: str = "",
) -> None:
    """对话成功后记录工具序列，供 evolver 提升为 workflow。"""
    if not db or not tools:
        return
    steps = []
    for tr in tools:
        if not tr.get("ok"):
            return  # 整轮必须全成功
        name = str(tr.get("name") or tr.get("func") or "").strip()
        if not name or name.startswith("mcp__"):
            continue
        if name in (
            "search_memory",
            "remember",
            "remember_fact",
            "upsert_core_memory",
            "forget_core_memory",
            "inspect_window_som",
            "get_current_time",
            "list_workflows",
            "run_workflow",
            "save_workflow",
            "read_screen",
            "take_screenshot",
            # 已合并的旧工具名（透明转发到新名），防御性跳过
            "set_timer",
            "schedule_action",
            "ocr_find_text",
            "clean_memory",
        ):
            continue
        args = tr.get("args") if isinstance(tr.get("args"), dict) else {}
        # 去掉过大字段
        slim = {
            k: v
            for k, v in args.items()
            if k not in ("image", "screenshot", "content") and not str(k).startswith("_")
        }
        steps.append({"func": name, "args": slim})
    if len(steps) < 2:
        return

    import hashlib

    sig_src = json.dumps(
        [{"func": s["func"], "keys": sorted(s["args"].keys())} for s in steps],
        ensure_ascii=False,
        sort_keys=True,
    )
    signature = hashlib.sha1(sig_src.encode("utf-8")).hexdigest()[:32]
    payload = json.dumps(steps, ensure_ascii=False)
    hint = (trigger_hint or "").strip()[:120]
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id, frequency FROM workflow_candidates WHERE signature = ?",
            (signature,),
        ).fetchone()
        if row:
            conn.execute(
                """UPDATE workflow_candidates SET
                   frequency = frequency + 1,
                   steps = ?,
                   trigger_hint = CASE WHEN ? != '' THEN ? ELSE trigger_hint END,
                   last_observed = datetime('now')
                   WHERE id = ?""",
                (payload, hint, hint, row["id"]),
            )
        else:
            conn.execute(
                """INSERT INTO workflow_candidates
                   (signature, steps, trigger_hint, frequency)
                   VALUES (?, ?, ?, 1)""",
                (signature, payload, hint),
            )
        conn.commit()
