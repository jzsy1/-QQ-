# -*- coding: utf-8 -*-
"""静态指令（语音指令记忆训练流）：固化成功动作序列 → 下次模糊匹配直接回放。

设计要点：
- 固化只保留 func + 可变入参键，剔除绝对路径 / 时间戳 / 临时字段，做到与当时环境解耦；
- 匹配用规范化后的字符串相似度评分（稳定、可测），低于阈值回落 LLM 决策；
- 回放成功 raise confidence，失败降低 confidence 并累积 fail_count，达到上限自动禁用并触发重训。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from typing import Any, Callable

logger = logging.getLogger(__name__)

_MAX_STEPS = 12

# 与 record_tool_sequence 一致：这些"信息/记忆/时间"类工具不该进动作序列
_SKIP_STATIC_TOOLS = frozenset(
    {
        "search_memory",
        "remember",
        "get_current_time",
        "list_workflows",
        "run_workflow",
        "save_workflow",
        "read_screen",
        "take_screenshot",
        "get_screen_bounds",
        # 已合并的旧工具名（透明转发到新名），防御性跳过
        "screenshot_window",
        "list_windows",
        "get_system_stats",
        "click_text",
        "click_at",
        "virtual_click",
        "virtual_scroll",
        "remember_fact",
        "set_timer",
        "schedule_action",
        "ocr_find_text",
        "clean_memory",
    }
)
# 入参里应剔除的易变字段（绝对路径含具体文件名、临时/私有字段）
_SKIP_ARGS_KEYS = frozenset({"image", "screenshot", "content", "absolute_path", "cwd"})

# 规范化 trigger 时的语气词 / 标点
_MODAL_WORDS = (
    "帮我", "请", "麻烦", "给", "把", "一下", "可以", "能", "我想", "我要", "请帮我"
)
_PUNCT_RE = re.compile(r"[，。；：、！？,.!?;:、\s'\"“”‘’（）()【】\[\]\-]+")


def _normalize_trigger(text: str) -> str:
    """去掉语气词/标点/首尾空白，压缩连续空白，得到稳定匹配键。"""
    if not text:
        return ""
    t = str(text)
    for w in _MODAL_WORDS:
        t = t.replace(w, "")
    t = _PUNCT_RE.sub("", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _title_from_steps(steps: list[dict]) -> str:
    funcs = [str(s.get("func") or "") for s in steps if isinstance(s, dict)]
    base = funcs[0].replace("_", "")[:6] if funcs else "action"
    return f"{base}×{len(funcs)}" if funcs else "static"


def _sanitize_args(args: dict) -> dict:
    """只保留可变入参键；剔除绝对路径与临时字段。"""
    out: dict[str, Any] = {}
    for k, v in (args or {}).items():
        if str(k).startswith("_"):
            continue
        if k in _SKIP_ARGS_KEYS:
            continue
        # 绝对路径整体剔除（值里含盘符或 /Users 或系统 Temp）
        if isinstance(v, str) and (
            re.match(r"^(?:[A-Za-z]:[\\/]|/(?:Users|home)/|.*[\\/]Temp[\\/])", v)
        ):
            continue
        out[k] = v
    return out


def sanitize_steps_for_static(
    executed_tools: list[dict], *, min_steps: int = 2
) -> list[dict]:
    """从 Agent Loop 记录的动作里提取可固化序列（去只读/自检/记忆类）。"""
    steps: list[dict] = []
    for tr in executed_tools or []:
        if not isinstance(tr, dict):
            continue
        if tr.get("via") == "selfcheck":
            continue
        if not tr.get("ok"):
            continue
        name = str(tr.get("name") or tr.get("func") or "").strip()
        if not name or name.startswith("mcp__"):
            continue
        if name in _SKIP_STATIC_TOOLS or (name and "→retry" in name):
            continue
        args = tr.get("args") if isinstance(tr.get("args"), dict) else {}
        slim = _sanitize_args(args)
        steps.append({"func": name, "args": slim})
    if len(steps) < min_steps:
        return []
    return steps


def compile_static_instruction(
    db,
    steps: list[dict],
    trigger: str,
    *,
    min_steps: int = 2,
) -> tuple[bool, str]:
    """把成功动作序列固化为一条静态指令（source='static'），写入 workflows + workflow_fts。"""
    if not db:
        return False, "记忆库不可用"
    clean = [s for s in (steps or []) if isinstance(s, dict) and s.get("func")]
    if len(clean) < min_steps:
        return False, f"可固化动作过少（{len(clean)}<{min_steps}）"
    clean = clean[:_MAX_STEPS]

    sig_src = json.dumps(
        [{"func": s["func"], "keys": sorted(s["args"].keys())} for s in clean],
        ensure_ascii=False,
        sort_keys=True,
    )
    signature = hashlib.sha1(sig_src.encode("utf-8")).hexdigest()[:32]
    name = f"静态_{signature[:8]}"
    payload = json.dumps(clean, ensure_ascii=False)
    title = _title_from_steps(clean)
    norm = _normalize_trigger(trigger)[:120]
    hint = (trigger or "").strip()[:120]

    with db.connect() as conn:
        # 1) 候选表频率累积（与 evolver 一致，便于后续提升）
        c = conn.execute(
            "SELECT id FROM workflow_candidates WHERE signature = ?", (signature,)
        ).fetchone()
        if c:
            conn.execute(
                """UPDATE workflow_candidates SET
                   frequency = frequency + 1,
                   steps = ?,
                   trigger_hint = CASE WHEN ? != '' THEN ? ELSE trigger_hint END,
                   last_observed = datetime('now')
                   WHERE id = ?""",
                (payload, norm, norm, c["id"]),
            )
        else:
            conn.execute(
                """INSERT INTO workflow_candidates (signature, steps, trigger_hint, frequency)
                   VALUES (?, ?, ?, 1)""",
                (signature, payload, norm),
            )
        # 2) 正式静态指令：重名则追加匹配键并提概率
        w = conn.execute("SELECT id FROM workflows WHERE name = ?", (name,)).fetchone()
        if w:
            wf_id = int(w["id"])
            conn.execute(
                """UPDATE workflows SET
                   steps = ?, title = ?,
                   match_keys = CASE WHEN instr(match_keys, ?) = 0
                                     THEN match_keys || '|' || ?
                                     ELSE match_keys END,
                   status = 'confirmed',
                   enabled = 1,
                   confidence = MIN(1.0, confidence + 0.05),
                   hit_count = hit_count + 1,
                   updated_at = datetime('now')
                   WHERE id = ?""",
                (payload, title, norm, norm, wf_id),
            )
        else:
            cur = conn.execute(
                """INSERT INTO workflows
                   (name, title, steps, trigger_hint, confidence, hit_count, status, source,
                    match_keys, enabled, fail_count)
                   VALUES (?, ?, ?, ?, 0.8, 1, 'confirmed', 'static', ?, 1, 0)""",
                (name, title, payload, hint, norm),
            )
            wf_id = int(cur.lastrowid)
        # 3) 全文索引
        content = " ".join(x for x in (name, norm) if x)
        if content.strip():
            db._workflow_fts_insert(conn, content, wf_id)
        conn.commit()
    return True, f"已生成静态指令「{title or name}」（{len(clean)} 步）。"


def _score_pair(a: str, b: str) -> float:
    """规范化后两串的相似度 0..1（包含 + 字符集合重叠加权），稳定可测。"""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if b in a or a in b:
        return 0.6 + 0.4 * min(len(a), len(b)) / max(len(a), len(b))
    # 无包含关系时，用触发词集合重叠近似
    def grams(s: str) -> set[str]:
        return set(s[i : i + 2] for i in range(max(0, len(s) - 1)))
    ga, gb = grams(a), grams(b)
    if not ga or not gb:
        return 0.0
    inter = len(ga & gb)
    return round(inter / max(len(ga), len(gb), 1), 3)


def match_static_instruction(
    db, user_text: str, *, threshold: float = 0.42
) -> tuple[str, float] | None:
    """模糊匹配静态指令；命中且置信 >= threshold 返回 (name, score)，否则 None 回落 LLM。"""
    if not db:
        return None
    q = _normalize_trigger(user_text)
    if not q:
        return None
    try:
        with db.connect() as conn:
            rows = conn.execute(
                """SELECT id, name, match_keys, trigger_hint
                   FROM workflows
                   WHERE source = 'static' AND enabled = 1 AND status = 'confirmed'"""
            ).fetchall()
    except Exception as e:
        logger.warning("match_static_instruction 查询失败: %s", e)
        return None
    if not rows:
        return None

    best_name, best_score = None, 0.0
    for r in rows:
        keys = [str(r["match_keys"] or "")]
        keys += [
            m.strip() for m in str(r["match_keys"] or "").split("|") if m.strip()
        ]
        if str(r["trigger_hint"] or "").strip():
            keys.append(str(r["trigger_hint"]).strip())
        for k in keys:
            s = _score_pair(q, _normalize_trigger(k))
            if s > best_score:
                best_score = s
                best_name = str(r["name"])
    if best_name is not None and best_score >= threshold:
        return best_name, best_score
    return None


def build_execute_fn(tool_ctx) -> Callable[[str, dict], tuple[bool, str]]:
    """构造回放单步执行函数（复刻 WorkflowManager._exec_step 的安全+执行链路）。"""
    from kekesi.r1_executor import STATUS_SUCCESS, execute_call, r1_to_tool_output
    from kekesi.safety.guard import check_tool_call

    def _fn(func: str, args: dict) -> tuple[bool, str]:
        allowed, deny = check_tool_call(func, args, user_text="", recent_assistant="")
        if not allowed:
            return False, deny or "安全闸拦截"
        try:
            resp = execute_call({"func": func, "args": args}, tool_ctx)
            out = r1_to_tool_output(resp)
            ok = resp.status == STATUS_SUCCESS and bool(out.ok)
            return bool(ok), out.text or ""
        except Exception as e:  # noqa: BLE001
            logger.warning("静态指令回放步骤执行失败 %s: %s", func, e)
            return False, str(e)

    return _fn


def replay_static_instruction(
    db,
    name: str,
    tool_ctx,
    *,
    max_steps: int = 12,
    max_replay_fail: int = 2,
) -> tuple[list[dict], str] | None:
    """回放静态指令；返回 (tools, reply)。失败时降 confidence / fail_count，达上限禁用。"""
    if not db:
        return None, None
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id, steps FROM workflows WHERE name = ? AND enabled = 1", (name,)
        ).fetchone()
    if not row:
        return None, None
    wf_id = int(row["id"])
    try:
        steps = json.loads(row["steps"] or "[]")
    except json.JSONDecodeError:
        return None, None
    if not isinstance(steps, list) or not steps:
        return None, None

    fn = build_execute_fn(tool_ctx)
    tools: list[dict] = []
    all_ok = True
    for step in steps[:max_steps]:
        if not isinstance(step, dict):
            continue
        func = str(step.get("func") or "").strip()
        args = step.get("args") if isinstance(step.get("args"), dict) else {}
        if not func:
            continue
        ok, msg = fn(func, args)
        tools.append({"func": func, "args": args, "ok": ok, "text": (msg or "")[:200]})
        if not ok:
            all_ok = False
            break

    with db.connect() as conn:
        if all_ok:
            conn.execute(
                """UPDATE workflows SET hit_count = hit_count + 1,
                   confidence = MIN(1.0, confidence + 0.05),
                   fail_count = 0, status = 'confirmed', updated_at = datetime('now')
                   WHERE id = ?""",
                (wf_id,),
            )
        else:
            conn.execute(
                """UPDATE workflows SET confidence = MAX(0.0, confidence - 0.1),
                   fail_count = fail_count + 1, last_replay_fail = datetime('now'),
                   updated_at = datetime('now')
                   WHERE id = ?""",
                (wf_id,),
            )
            info = conn.execute(
                "SELECT fail_count FROM workflows WHERE id = ?", (wf_id,)
            ).fetchone()
            if info and int(info["fail_count"] or 0) >= max_replay_fail:
                conn.execute("UPDATE workflows SET enabled = 0 WHERE id = ?", (wf_id,))
        conn.commit()

    if not tools:
        return None, None
    lines = [f"{t['func']}:{'成功' if t['ok'] else '失败'}" for t in tools]
    return tools, ("\n".join(lines) + "\n" if all_ok else "静态指令部分失败：\n" + "\n".join(lines))