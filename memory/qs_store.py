"""QS 事件流 — 不可变操作事件，供 R2 上下文检索与实体消歧。"""

from __future__ import annotations

import json
import logging
import math
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from difflib import SequenceMatcher

from .db import Database

logger = logging.getLogger(__name__)

_FILE_PATH_RE = re.compile(
    r"([A-Za-z]:\\[^\s\"'，,。]+|\~/[^\s\"'，,。]+|/[^\s\"'，,。]+|\S+\.(?:png|jpg|jpeg|txt|pdf|docx?|xlsx?|pptx?|md|csv|json|py|zip))",
    re.I,
)
_SCREENSHOT_SUMMARY_RE = re.compile(
    r"截图[^:：]*[:：]?\s*([^\s/\\]+\.(?:png|jpg|jpeg))|"
    r"保存(?:到)?(?:桌面)?[:：]?\s*([^\s/\\]+\.(?:png|jpg|jpeg))",
    re.I,
)
_DEICTIC_RE = re.compile(
    r"(?:刚才|刚刚|上一步|上一个|上一张|上一份|上次那个|刚保存|刚生成|刚创建|刚打开|刚截|刚才那(?:个|张|份|条))|"
    r"(?:把|将|打开|查看|发给|发送|删除|删掉|重命名|复制|移动|关闭)\s*(?:刚才|刚刚|那个|那张|那份)(?:文件|文档|截图|图片|表格|窗口|软件|程序|路径)?"
)
_UNSAFE_PATH_RE = re.compile(
    r"(?:^\\\\[.?]\\|\.\.[/\\]|^[A-Za-z]:[/\\]Windows[/\\]System32[/\\](?:config|drivers)|"
    r"^[A-Za-z]:[/\\]System Volume Information)",
    re.I,
)


def is_deictic_query(user_text: str) -> bool:
    """检测用户输入是否包含上下文指代（「刚才的文件」「上一个截图」「刚打开的窗口」等）。"""
    t = (user_text or "").strip()
    if not t:
        return False
    return bool(_DEICTIC_RE.search(t))


def _detect_deictic_kind(user_text: str) -> str:
    """根据用户指代词判断目标类型：screenshot / window / app / file。"""
    t = (user_text or "").strip()
    if any(k in t for k in ("截图", "图片", "照片", "那张图", "上一张", "画面")):
        return "screenshot"
    if any(k in t for k in ("窗口", "界面", "对话框")):
        return "window"
    if any(k in t for k in ("软件", "应用", "程序", "客户端")):
        return "app"
    return "file"


def _is_safe_local_path(path_str: str) -> bool:
    """校验指代解析出的路径是否安全（拒绝设备路径、目录穿越、系统敏感目录）。"""
    p = (path_str or "").strip()
    if not p:
        return False
    if _UNSAFE_PATH_RE.search(p):
        return False
    return True


@dataclass
class QsEvent:
    id: int
    session_id: str
    turn_id: str | None
    event_type: str
    summary: str
    payload: dict
    created_at: str
    score: float = 0.0

    @property
    def file_path(self) -> str | None:
        outputs = self.payload.get("outputs") or {}
        if isinstance(outputs, dict):
            path = outputs.get("file_path") or outputs.get("path")
            if path:
                return str(path)
        args = self.payload.get("args") or {}
        if isinstance(args, dict):
            for k in ("path", "file_path", "filename", "dest", "target_path"):
                if args.get(k) and isinstance(args.get(k), str):
                    m_arg = _FILE_PATH_RE.search(str(args.get(k)))
                    if m_arg:
                        return m_arg.group(1)
        text = self.payload.get("text") or self.summary or ""
        m = _FILE_PATH_RE.search(str(text))
        return m.group(1) if m else None

    @property
    def window_or_app_target(self) -> str | None:
        args = self.payload.get("args") or {}
        if isinstance(args, dict):
            for k in ("window_title", "title", "app_name", "name", "app"):
                val = str(args.get(k) or "").strip()
                if val:
                    return val
        return None

    @property
    def func(self) -> str:
        return str(self.payload.get("func") or "")


@dataclass
class QsSearchResult:
    events: list[QsEvent] = field(default_factory=list)
    ambiguous: bool = False
    top_score: float = 0.0
    status: str = "no_match"
    resolved_path: str = ""
    resolved_target: str = ""
    target_kind: str = "file"
    clarification_prompt: str = ""


class QsStore:
    """Append-only 事件流 + 语义/时间加权检索。"""

    def __init__(self, db: Database):
        self.db = db

    def append(
        self,
        event_type: str,
        summary: str,
        payload: dict | None = None,
        *,
        session_id: str = "default",
        turn_id: str | None = None,
    ) -> int:
        text = (summary or "").strip()
        if not text:
            text = event_type
        body = json.dumps(payload or {}, ensure_ascii=False)
        with self.db.connect() as conn:
            cur = conn.execute(
                """INSERT INTO qs_events
                   (session_id, turn_id, event_type, summary, payload)
                   VALUES (?, ?, ?, ?, ?)""",
                (session_id, turn_id, event_type, text, body),
            )
            event_id = int(cur.lastrowid)
            conn.execute(
                """INSERT INTO qs_events_fts(summary, payload, event_type, source_id)
                   VALUES (?, ?, ?, ?)""",
                (text, body, event_type, event_id),
            )
            conn.commit()
        return event_id

    def append_tool_result(
        self,
        func: str,
        args: dict,
        *,
        ok: bool,
        text: str = "",
        r1_status: str = "",
        session_id: str = "default",
        turn_id: str | None = None,
    ) -> int:
        outputs = _extract_outputs(func, text)
        summary = _tool_event_summary(func, args, ok, text, outputs)
        payload = {
            "func": func,
            "args": args,
            "ok": ok,
            "text": text,
            "r1_status": r1_status,
            "outputs": outputs,
        }
        return self.append(
            "tool_result",
            summary,
            payload,
            session_id=session_id,
            turn_id=turn_id,
        )

    def recent(
        self,
        session_id: str = "default",
        *,
        limit: int = 20,
        event_types: list[str] | None = None,
    ) -> list[QsEvent]:
        limit = max(1, min(limit, 100))
        with self.db.connect() as conn:
            if event_types:
                placeholders = ",".join("?" * len(event_types))
                rows = conn.execute(
                    f"""SELECT * FROM qs_events
                        WHERE session_id = ? AND event_type IN ({placeholders})
                        ORDER BY id DESC LIMIT ?""",
                    [session_id, *event_types, limit],
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT * FROM qs_events
                       WHERE session_id = ?
                       ORDER BY id DESC LIMIT ?""",
                    (session_id, limit),
                ).fetchall()
        return [_row_to_event(r) for r in reversed(rows)]

    def search(
        self,
        query: str,
        *,
        session_id: str = "default",
        limit: int = 8,
        event_types: list[str] | None = None,
    ) -> QsSearchResult:
        q = (query or "").strip()
        if not q:
            return QsSearchResult()

        candidates: dict[int, QsEvent] = {}
        with self.db.connect() as conn:
            pattern = f"%{q}%"
            type_filter = ""
            params: list = [session_id, pattern, pattern]
            if event_types:
                placeholders = ",".join("?" * len(event_types))
                type_filter = f" AND event_type IN ({placeholders})"
                params.extend(event_types)
            params.append(limit * 3)
            rows = conn.execute(
                f"""SELECT * FROM qs_events
                    WHERE session_id = ?
                      AND (summary LIKE ? OR payload LIKE ?){type_filter}
                    ORDER BY id DESC LIMIT ?""",
                params,
            ).fetchall()
            for row in rows:
                ev = _row_to_event(row)
                ev.score = _score_event(ev, q)
                candidates[ev.id] = ev

            if len(q) >= 2:
                fts_q = q.replace('"', '""')
                fts_params: list = [fts_q, session_id]
                fts_type = ""
                if event_types:
                    placeholders = ",".join("?" * len(event_types))
                    fts_type = f" AND e.event_type IN ({placeholders})"
                    fts_params.extend(event_types)
                fts_params.append(limit * 3)
                try:
                    fts_rows = conn.execute(
                        f"""SELECT e.*, bm25(qs_events_fts) AS rank
                            FROM qs_events_fts f
                            JOIN qs_events e ON e.id = f.source_id
                            WHERE qs_events_fts MATCH ?
                              AND e.session_id = ?{fts_type}
                            ORDER BY rank
                            LIMIT ?""",
                        fts_params,
                    ).fetchall()
                    for row in fts_rows:
                        ev = _row_to_event(row)
                        bm = abs(float(row["rank"] or 0))
                        ev.score = max(ev.score, _score_event(ev, q) + min(bm, 3.0) * 0.15)
                        candidates[ev.id] = ev
                except Exception:
                    pass

        ranked = sorted(candidates.values(), key=lambda e: e.score, reverse=True)[:limit]
        if not ranked:
            return QsSearchResult()

        top = ranked[0].score
        second = ranked[1].score if len(ranked) > 1 else 0.0
        ambiguous = len(ranked) > 1 and second > 0.5 and (top - second) < 0.12
        return QsSearchResult(events=ranked, ambiguous=ambiguous, top_score=top)

    def resolve_open_targets(
        self,
        user_text: str,
        *,
        session_id: str = "default",
        max_age_hours: float = 24.0,
        min_confidence: float = 0.45,
        check_disk: bool = True,
    ) -> QsSearchResult:
        """上下文指代消歧：基于事件类型、时间衰减、窗口/路径与磁盘状态解析指代目标。

        返回 QsSearchResult，其中 status 取值：
        - 'resolved': 唯一高置信目标，且通过安全与文件存在性校验
        - 'ambiguous': 存在多个相近候选，需向用户澄清
        - 'expired': 存在历史记录但已超过 max_age_hours
        - 'missing_file': 解析到文件路径但磁盘文件已不存在/被移除
        - 'unsafe_path': 命中路径不满足安全边界校验
        - 'low_confidence': 最高候选得分低于 min_confidence
        - 'no_match': 无任何相关操作事件
        """
        kind = _detect_deictic_kind(user_text)
        recent = self.recent(
            session_id, limit=40, event_types=["tool_result"]
        )
        # 若当前会话无工具事件，回退检索跨会话最近事件（语音唤醒切换 session 场景）
        cross_session = False
        if not recent:
            try:
                with self.db.connect() as conn:
                    rows = conn.execute(
                        """SELECT * FROM qs_events
                           WHERE event_type = 'tool_result'
                           ORDER BY id DESC LIMIT 30"""
                    ).fetchall()
                recent = [_row_to_event(r) for r in rows]
                cross_session = True
            except Exception:
                recent = []

        matched_events: list[QsEvent] = []
        expired_events: list[QsEvent] = []

        for ev in recent:
            if not ev.payload.get("ok"):
                continue
            fp = ev.file_path
            win_target = ev.window_or_app_target
            if kind in ("window", "app"):
                if not win_target and ev.func not in (
                    "open_app", "focus_window", "minimize_window", "close_window", "open_app_and_type"
                ):
                    continue
            elif kind == "screenshot":
                is_shot = (
                    ev.func in ("take_screenshot", "recall_screenshot", "read_screen")
                    or "截图" in ev.summary
                    or (fp and fp.lower().endswith((".png", ".jpg", ".jpeg", ".bmp", ".webp")))
                )
                if not is_shot or not fp:
                    continue
            else:
                if not fp and not win_target:
                    continue

            age_h = _event_age_hours(ev.created_at)
            score = _time_weight(ev.created_at)
            if cross_session:
                score *= 0.92
            if kind == "screenshot" and (ev.func == "take_screenshot" or "截图" in ev.summary):
                score += 0.35
            elif kind == "file" and fp:
                score += 0.25
                if ev.func in ("create_document", "write_code_file", "copy_path", "move_path", "open_path"):
                    score += 0.15
                elif ev.func == "take_screenshot" or "截图" in ev.summary:
                    score += 0.20
            elif kind in ("window", "app") and win_target:
                score += 0.30
            if ev.func == "run_shell" and "打开" in ev.summary:
                score += 0.10
            if _text_similarity(user_text, ev.summary) > 0.3:
                score += 0.15
            ev.score = score
            if max_age_hours > 0 and age_h > max_age_hours:
                expired_events.append(ev)
            else:
                matched_events.append(ev)

        matched_events.sort(key=lambda e: e.score, reverse=True)
        if not matched_events and not expired_events:
            fts = self.search(
                "截图 文件 桌面 窗口",
                session_id=session_id,
                limit=6,
                event_types=["tool_result"],
            )
            for e in fts.events:
                if not (e.file_path or e.window_or_app_target):
                    continue
                if max_age_hours > 0 and _event_age_hours(e.created_at) > max_age_hours:
                    expired_events.append(e)
                else:
                    matched_events.append(e)

        if not matched_events:
            if expired_events:
                expired_events.sort(key=lambda e: e.score, reverse=True)
                best_exp = expired_events[0]
                exp_target = best_exp.file_path or best_exp.window_or_app_target or best_exp.summary[:40]
                return QsSearchResult(
                    events=expired_events[:3],
                    ambiguous=False,
                    top_score=best_exp.score,
                    status="expired",
                    target_kind=kind,
                    resolved_target=exp_target,
                    clarification_prompt=(
                        f"最近的相关操作记录（{exp_target}）已超过有效时限，请向用户确认具体要操作的目标名称或路径。"
                    ),
                )
            return QsSearchResult(status="no_match", target_kind=kind)

        # 按实际目标去重，避免对同一文件的连续两次操作被误判为多候选歧义
        deduped: list[QsEvent] = []
        seen_targets: set[str] = set()
        for ev in matched_events:
            key = (ev.file_path or ev.window_or_app_target or ev.summary).strip().lower()
            if key in seen_targets:
                continue
            seen_targets.add(key)
            deduped.append(ev)

        top_ev = deduped[0]
        top = top_ev.score
        second = deduped[1].score if len(deduped) > 1 else 0.0
        # 若用户明确说「上一个 / 刚才那个 / 最新那个」，且最新事件明显比上一条更新（>=10秒），优先取最新
        explicit_latest = any(k in (user_text or "") for k in ("上一个", "上一张", "上一份", "最新", "刚截", "刚保存"))
        time_gap_sec = abs(_parse_ts(deduped[0].created_at) - _parse_ts(deduped[1].created_at)) if len(deduped) > 1 else 999.0
        ambiguous = (
            len(deduped) > 1
            and second > 0.4
            and (top - second) < 0.15
            and not (explicit_latest and time_gap_sec >= 15.0)
        )

        best_fp = top_ev.file_path or ""
        best_target = best_fp or top_ev.window_or_app_target or top_ev.summary[:60]

        if top < min_confidence:
            return QsSearchResult(
                events=deduped[:3],
                ambiguous=ambiguous,
                top_score=top,
                status="low_confidence",
                target_kind=kind,
                resolved_target=best_target,
                clarification_prompt=(
                    f"上下文指代置信度较低（候选：{best_target}），请先向用户确认具体目标，禁止盲目执行。"
                ),
            )

        if ambiguous:
            cand_labels = [
                (e.file_path or e.window_or_app_target or e.summary[:40])
                for e in deduped[:3]
            ]
            return QsSearchResult(
                events=deduped[:3],
                ambiguous=True,
                top_score=top,
                status="ambiguous",
                target_kind=kind,
                resolved_target=best_target,
                clarification_prompt=(
                    "存在多个近期操作候选，请向用户澄清具体指哪一个："
                    + "、".join(f"「{c}」" for c in cand_labels)
                ),
            )

        if best_fp:
            if not _is_safe_local_path(best_fp):
                return QsSearchResult(
                    events=deduped[:3],
                    ambiguous=False,
                    top_score=top,
                    status="unsafe_path",
                    target_kind=kind,
                    resolved_path="",
                    resolved_target=best_fp,
                    clarification_prompt=f"指代路径「{best_fp}」未通过安全边界检查，禁止自动注入执行。",
                )
            if check_disk and (re.match(r"^[A-Za-z]:[/\\]", best_fp) or best_fp.startswith(("/", "~/"))):
                expanded = os.path.expanduser(best_fp)
                if not os.path.exists(expanded):
                    return QsSearchResult(
                        events=deduped[:3],
                        ambiguous=False,
                        top_score=top,
                        status="missing_file",
                        target_kind=kind,
                        resolved_path="",
                        resolved_target=best_fp,
                        clarification_prompt=(
                            f"指代的历史文件「{best_fp}」在磁盘上已不存在或已被移动，请向用户说明并确认最新路径，禁止盲目操作。"
                        ),
                    )

        return QsSearchResult(
            events=deduped[:3],
            ambiguous=False,
            top_score=top,
            status="resolved",
            resolved_path=best_fp,
            resolved_target=best_target,
            target_kind=kind,
        )

    def context_lines(
        self,
        session_id: str = "default",
        *,
        query: str = "",
        limit: int = 5,
    ) -> list[str]:
        if query.strip():
            hits = self.search(query, session_id=session_id, limit=limit)
            events = hits.events
            if not events:
                events = self.recent(session_id, limit=limit, event_types=["tool_result"])
        else:
            events = self.recent(session_id, limit=limit, event_types=["tool_result"])
        lines: list[str] = []
        for ev in events:
            lines.append(f"- [{ev.created_at[:16]}] {ev.summary}")
        return lines


def build_deictic_context_block(
    db: Database,
    user_text: str,
    session_id: str = "default",
    *,
    max_age_hours: float = 24.0,
    check_disk: bool = True,
) -> tuple[str, list[str]]:
    """当用户请求包含上下文指代时，生成【上下文指代消歧】注入块与审计 ID 列表。"""
    if not is_deictic_query(user_text):
        return "", []
    store = QsStore(db)
    res = store.resolve_open_targets(
        user_text,
        session_id=session_id,
        max_age_hours=max_age_hours,
        check_disk=check_disk,
    )
    ctx_lines = store.context_lines(session_id=session_id, query=user_text, limit=3)
    audit_ids: list[str] = []
    block_lines: list[str] = []

    if res.status == "resolved":
        target_desc = res.resolved_path or res.resolved_target
        block_lines.append(
            f"- 已解析指代目标（{res.target_kind}，置信度={res.top_score:.2f}）：{target_desc}"
        )
        for ev in res.events[:1]:
            audit_ids.append(f"qs:resolved:{ev.id}")
    elif res.status in ("ambiguous", "expired", "missing_file", "low_confidence", "unsafe_path"):
        block_lines.append(f"- [需澄清/{res.status}] {res.clarification_prompt}")
        for ev in res.events[:2]:
            audit_ids.append(f"qs:{res.status}:{ev.id}")
    else:
        if not ctx_lines:
            return "", []
        block_lines.append("- 未找到明确匹配的近期文件/窗口目标，若用户指代不明请先询问确认。")
        audit_ids.append("qs:no_match")

    if ctx_lines:
        block_lines.append("近期相关操作轨迹：")
        block_lines.extend(ctx_lines[:3])

    header = "【上下文指代消歧】（供解析「刚才的文件/上一个截图/刚才的窗口」等指代；若标记为[需澄清]必须先向用户确认，禁止瞎猜路径）"
    return header + "\n" + "\n".join(block_lines), audit_ids


def _row_to_event(row) -> QsEvent:
    payload = {}
    try:
        payload = json.loads(row["payload"] or "{}")
    except (json.JSONDecodeError, TypeError):
        payload = {}
    return QsEvent(
        id=int(row["id"]),
        session_id=row["session_id"],
        turn_id=row["turn_id"],
        event_type=row["event_type"],
        summary=row["summary"] or "",
        payload=payload if isinstance(payload, dict) else {},
        created_at=row["created_at"] or "",
    )


def _parse_ts(ts: str) -> float:
    raw = (ts or "").strip()
    if not raw:
        return 0.0
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            return dt.timestamp()
        # SQLite datetime('now') writes naive UTC ('YYYY-MM-DD HH:MM:SS')
        local_ts = dt.timestamp()
        from datetime import timezone
        utc_ts = dt.replace(tzinfo=timezone.utc).timestamp()
        now_ts = datetime.now().timestamp()
        # Choose whichever interpretation is closer to current time and non-future
        if abs(now_ts - utc_ts) < abs(now_ts - local_ts):
            return utc_ts
        return local_ts
    except (ValueError, TypeError):
        return 0.0


def _event_age_hours(created_at: str) -> float:
    ts = _parse_ts(created_at)
    if ts <= 0:
        return 0.0
    return max(0.0, (datetime.now().timestamp() - ts) / 3600.0)


def _time_weight(created_at: str) -> float:
    ts = _parse_ts(created_at)
    if ts <= 0:
        return 0.85
    age_hours = _event_age_hours(created_at)
    return math.exp(-age_hours / 36.0)


def _text_similarity(a: str, b: str) -> float:
    a, b = (a or "").strip(), (b or "").strip()
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return 0.92
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _score_event(ev: QsEvent, query: str) -> float:
    sim = max(
        _text_similarity(query, ev.summary),
        _text_similarity(query, json.dumps(ev.payload, ensure_ascii=False)),
    )
    tw = _time_weight(ev.created_at)
    type_boost = 0.08 if ev.event_type == "tool_result" and ev.payload.get("ok") else 0.0
    return sim * tw + type_boost


def _extract_outputs(func: str, text: str) -> dict:
    outputs: dict = {}
    raw = (text or "").strip()
    if func == "take_screenshot":
        m = _SCREENSHOT_SUMMARY_RE.search(raw)
        if m:
            from ..tools.winops import desktop_dir
            import os

            name = m.group(1) or m.group(2)
            path = os.path.join(desktop_dir(), name)
            outputs["file_path"] = path
            outputs["file_name"] = name
    for m in _FILE_PATH_RE.finditer(raw):
        outputs.setdefault("file_path", m.group(1))
        break
    return outputs


def _tool_event_summary(
    func: str,
    args: dict,
    ok: bool,
    text: str,
    outputs: dict,
) -> str:
    status = "完成" if ok else "失败"
    if func == "take_screenshot" and ok:
        path = outputs.get("file_path") or text
        return f"截图操作{status}，产出文件 {path}"
    if func == "open_app":
        app = args.get("app_name") or "应用"
        return f"打开应用{status}：{app} — {text[:80]}"
    if func == "run_shell" and outputs.get("file_path"):
        return f"Shell 操作{status}，涉及文件 {outputs['file_path']}"
    arg_preview = json.dumps(args, ensure_ascii=False)[:60] if args else ""
    return f"{func} 操作{status} {arg_preview} — {(text or '')[:100]}"
