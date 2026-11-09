"""U4: memory audit log. KEKESI_PCE_P4_20261101_V2."""
import sqlite3
import time
from pathlib import Path

MARKER = "KEKESI_PCE_P4_20261101_V2"


def _repo_root():
    return Path(__file__).resolve().parents[2]


def ensure_audit_table(conn):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS memory_audit ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL, query TEXT, "
        "injected_ids TEXT, scores TEXT, latency_ms REAL, result_chars INTEGER)"
    )


def log_audit(query="", injected_ids=None, scores=None, latency_ms=0.0,
              result_chars=0, conn=None, db_path=None):
    """Append one audit row. Never raises.

    With an explicit conn, a SAVEPOINT is used so the caller's outer
    transaction is never committed by this function. Without conn, the
    first existing default DB is used; when none exists, silently skip.
    """
    try:
        import json
        ids_text = json.dumps(list(injected_ids or []), ensure_ascii=False)
        scores_text = json.dumps(scores or {}, ensure_ascii=False)
        row = (time.time(), query or "", ids_text, scores_text,
               float(latency_ms or 0.0), int(result_chars or 0))
        if conn is not None:
            conn.execute("SAVEPOINT p4_audit")
            try:
                ensure_audit_table(conn)
                conn.execute(
                    "INSERT INTO memory_audit (created_at, query, injected_ids, "
                    "scores, latency_ms, result_chars) VALUES (?, ?, ?, ?, ?, ?)",
                    row,
                )
                conn.execute("RELEASE p4_audit")
            except Exception:
                try:
                    conn.execute("ROLLBACK TO p4_audit")
                    conn.execute("RELEASE p4_audit")
                except Exception:
                    pass
            return
        path = Path(db_path) if db_path else None
        if path is None:
            for cand in (_repo_root() / "data" / "kekesi.db",
                        _repo_root() / "kekesi" / "data" / "kekesi.db"):
                if cand.exists():
                    path = cand
                    break
        if path is None:
            return
        conn2 = sqlite3.connect(str(path))
        try:
            ensure_audit_table(conn2)
            conn2.execute(
                "INSERT INTO memory_audit (created_at, query, injected_ids, "
                "scores, latency_ms, result_chars) VALUES (?, ?, ?, ?, ?, ?)",
                row,
            )
            conn2.commit()
        finally:
            conn2.close()
    except Exception:
        pass


_NEG_USER_FEEDBACK_WORDS = (
    "不对", "错了", "不是这个", "答非所问", "不是问这个", "搞错了", "你记错了", "胡说", "乱说"
)


def _resolve_audit_db_path(db_path=None) -> Path | None:
    if db_path:
        p = Path(db_path)
        return p if p.exists() else None
    for cand in (
        _repo_root() / "kekesi" / "data" / "kekesi.db",
        _repo_root() / "data" / "kekesi.db",
    ):
        if cand.exists():
            return cand
    return None


def _parse_iso_or_epoch(val) -> float:
    if val is None:
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    raw = str(val).strip()
    if not raw:
        return 0.0
    try:
        return float(raw)
    except ValueError:
        pass
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            return dt.timestamp()
        local_ts = dt.timestamp()
        utc_ts = dt.replace(tzinfo=timezone.utc).timestamp()
        now_ts = time.time()
        return utc_ts if abs(now_ts - utc_ts) < abs(now_ts - local_ts) else local_ts
    except Exception:
        return 0.0


def _channel_of_id(raw_id: str) -> str:
    s = str(raw_id or "").strip()
    if not s:
        return "unknown"
    if s.startswith("p2:capability_health"):
        return "p2:capability_health"
    if s.startswith("p2:cognition") or s.startswith("model:"):
        return "p2:cognition"
    if s.startswith("p1:environment"):
        return "p1:environment"
    if s.startswith("p5:staging_cases"):
        return "p5:staging_cases"
    if s.startswith("p5:cases"):
        return "p5:cases"
    if s.startswith("p5:episode"):
        return "p5:episode"
    if s.startswith("qs:"):
        return "qs:deictic"
    if s.startswith("workflow:"):
        return "workflow"
    if s.startswith("study_adopt:"):
        return "study_adopt"
    prefix = s.split(":", 1)[0]
    return prefix or "unknown"


def analyze_audit_quality(
    db_path=None,
    *,
    window_hours: float = 720.0,
    limit: int = 500,
    feedback_window_sec: float = 120.0,
) -> dict:
    """只读统计与质量分析消费方（不修改任何线上检索权重或数据库状态）。

    结合 execution_history / task_history (Verifier 执行结果) 与 conversation_digest (用户反馈)，
    严格区分「被注入 (injected)」与「经验证有效 (verified_effective)」。
    """
    import json

    path = _resolve_audit_db_path(db_path)
    if path is None:
        return {
            "total_queries": 0,
            "injected_queries": 0,
            "zero_hit_count": 0,
            "zero_hit_rate": 0.0,
            "zero_hit_queries": [],
            "latency_ms": {"avg": 0.0, "p50": 0.0, "p95": 0.0, "max": 0.0},
            "channel_contributions": {},
            "duplicate_injections": {"count": 0, "rate": 0.0, "examples": []},
            "effectiveness": {
                "injected_count": 0,
                "verified_effective": 0,
                "verified_ineffective": 0,
                "injected_unverified": 0,
                "injection_rate": 0.0,
                "verified_effective_rate": 0.0,
            },
            "potential_false_recalls": [],
            "weights_modified": False,
        }

    cutoff = time.time() - float(window_hours) * 3600.0 if window_hours and window_hours > 0 else 0.0
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        ensure_audit_table(conn)
        rows = conn.execute(
            """SELECT id, created_at, query, injected_ids, scores, latency_ms, result_chars
               FROM memory_audit
               WHERE created_at >= ?
               ORDER BY id DESC LIMIT ?""",
            (cutoff, max(1, int(limit))),
        ).fetchall()

        # 读取执行验证结果（execution_history + task_history）与用户反馈（conversation_digest）
        exec_events: list[tuple[float, bool, str]] = []
        try:
            eh_cols = {r[1] for r in conn.execute("PRAGMA table_info(execution_history)").fetchall()}
            t_col = "tool" if "tool" in eh_cols else "tool_name" if "tool_name" in eh_cols else "'tool'"
            for r in conn.execute(
                f"SELECT created_at, success, {t_col} FROM execution_history ORDER BY id DESC LIMIT 1000"
            ).fetchall():
                ts = _parse_iso_or_epoch(r[0])
                if ts > 0:
                    exec_events.append((ts, bool(r[1]), str(r[2] or "")))
        except Exception:
            pass
        try:
            for r in conn.execute(
                "SELECT created_at, success, action FROM task_history ORDER BY id DESC LIMIT 1000"
            ).fetchall():
                ts = _parse_iso_or_epoch(r["created_at"])
                if ts > 0:
                    exec_events.append((ts, bool(r["success"]), str(r["action"] or "")))
        except Exception:
            pass

        user_turns: list[tuple[float, str]] = []
        try:
            for r in conn.execute(
                "SELECT created_at, user_text FROM conversation_digest ORDER BY id DESC LIMIT 1000"
            ).fetchall():
                ts = _parse_iso_or_epoch(r["created_at"])
                if ts > 0:
                    user_turns.append((ts, str(r["user_text"] or "")))
        except Exception:
            pass
    finally:
        conn.close()

    total = len(rows)
    if total == 0:
        return {
            "total_queries": 0,
            "injected_queries": 0,
            "zero_hit_count": 0,
            "zero_hit_rate": 0.0,
            "zero_hit_queries": [],
            "latency_ms": {"avg": 0.0, "p50": 0.0, "p95": 0.0, "max": 0.0},
            "channel_contributions": {},
            "duplicate_injections": {"count": 0, "rate": 0.0, "examples": []},
            "effectiveness": {
                "injected_count": 0,
                "verified_effective": 0,
                "verified_ineffective": 0,
                "injected_unverified": 0,
                "injection_rate": 0.0,
                "verified_effective_rate": 0.0,
            },
            "potential_false_recalls": [],
            "weights_modified": False,
        }

    latencies: list[float] = []
    zero_hit_queries: list[str] = []
    zero_hit_count = 0
    injected_count = 0
    channel_counts: dict[str, int] = {}
    dup_count = 0
    dup_examples: list[dict] = []

    verified_effective = 0
    verified_ineffective = 0
    injected_unverified = 0
    potential_false_recalls: list[dict] = []

    prev_q = None
    prev_ids_tuple = None
    prev_ts = 0.0

    # Process chronologically from oldest to newest
    for r in reversed(rows):
        q = str(r["query"] or "").strip()
        ts = float(r["created_at"] or 0.0)
        lat = max(0.0, float(r["latency_ms"] or 0.0))
        chars = int(r["result_chars"] or 0)
        latencies.append(lat)

        try:
            ids_list = json.loads(r["injected_ids"] or "[]")
            if not isinstance(ids_list, list):
                ids_list = []
        except Exception:
            ids_list = []
        ids_str = [str(x) for x in ids_list if str(x).strip()]

        if not ids_str and chars == 0:
            zero_hit_count += 1
            if q and q not in zero_hit_queries and len(zero_hit_queries) < 15:
                zero_hit_queries.append(q[:80])
            continue

        if not ids_str:
            # Has summary/digest chars but no specific memory channel IDs
            zero_hit_count += 1
            if q and q not in zero_hit_queries and len(zero_hit_queries) < 15:
                zero_hit_queries.append(q[:80])
            continue

        injected_count += 1

        # 1. Channel contributions
        seen_ch_in_turn: set[str] = set()
        for raw_id in ids_str:
            ch = _channel_of_id(raw_id)
            channel_counts[ch] = channel_counts.get(ch, 0) + 1
            seen_ch_in_turn.add(ch)

        # 2. Duplicate injection check (within same turn or back-to-back within 10s)
        has_intra_dup = len(ids_str) > len(set(ids_str))
        has_inter_dup = (
            prev_q == q
            and prev_ids_tuple == tuple(ids_str)
            and abs(ts - prev_ts) <= 10.0
        )
        if has_intra_dup or has_inter_dup:
            dup_count += 1
            if len(dup_examples) < 10:
                dup_examples.append({
                    "audit_id": int(r["id"]),
                    "query": q[:60],
                    "injected_ids": ids_str[:6],
                    "reason": "intra_turn_duplicate" if has_intra_dup else "rapid_repeat_injection",
                })
        prev_q = q
        prev_ids_tuple = tuple(ids_str)
        prev_ts = ts

        # 3. Correlate with Verifier / execution outcomes & user feedback within feedback_window_sec
        # Sort execution events by absolute time delta to this query
        turn_execs = [
            (abs(e_ts - ts), e_ok, e_name)
            for (e_ts, e_ok, e_name) in exec_events
            if 0.0 <= (e_ts - ts) <= feedback_window_sec
        ]
        turn_execs.sort(key=lambda x: x[0])
        nearest_exec = (turn_execs[0][1], turn_execs[0][2]) if turn_execs else None

        win_neg_fb = [
            u_txt
            for (u_ts, u_txt) in user_turns
            if 1.0 <= (u_ts - ts) <= feedback_window_sec
            and any(nw in u_txt for nw in _NEG_USER_FEEDBACK_WORDS)
        ]

        has_neg_fb = bool(win_neg_fb)
        has_exec_fail = nearest_exec is not None and not nearest_exec[0]
        has_exec_ok = nearest_exec is not None and nearest_exec[0]

        if has_neg_fb or has_exec_fail:
            verified_ineffective += 1
            reason_parts = []
            if has_neg_fb:
                reason_parts.append(f"user_negative_feedback({win_neg_fb[0][:30]})")
            if has_exec_fail:
                failed_tools = [n for _, ok, n in turn_execs if not ok]
                reason_parts.append(f"verifier_or_tool_failed({','.join(failed_tools[:2])})")
            if len(potential_false_recalls) < 15:
                potential_false_recalls.append({
                    "audit_id": int(r["id"]),
                    "query": q[:60],
                    "injected_ids": ids_str[:6],
                    "reason": "; ".join(reason_parts),
                })
        elif has_exec_ok:
            verified_effective += 1
        else:
            injected_unverified += 1
            # Also flag suspiciously noisy recall on tiny queries
            if len(q) <= 3 and len(ids_str) >= 6 and len(potential_false_recalls) < 15:
                potential_false_recalls.append({
                    "audit_id": int(r["id"]),
                    "query": q[:60],
                    "injected_ids": ids_str[:6],
                    "reason": f"short_query_high_fanout({len(ids_str)} items)",
                })

    lat_sorted = sorted(latencies)
    p50 = lat_sorted[int(0.50 * (len(lat_sorted) - 1))] if lat_sorted else 0.0
    p95 = lat_sorted[int(0.95 * (len(lat_sorted) - 1))] if lat_sorted else 0.0

    total_ch_hits = max(1, sum(channel_counts.values()))
    channel_contributions = {
        ch: {
            "hits": cnt,
            "share": round(cnt / total_ch_hits, 4),
        }
        for ch, cnt in sorted(channel_counts.items(), key=lambda x: x[1], reverse=True)
    }

    return {
        "total_queries": total,
        "injected_queries": injected_count,
        "zero_hit_count": zero_hit_count,
        "zero_hit_rate": round(zero_hit_count / max(1, total), 4),
        "zero_hit_queries": zero_hit_queries,
        "latency_ms": {
            "avg": round(sum(latencies) / max(1, len(latencies)), 2),
            "p50": round(p50, 2),
            "p95": round(p95, 2),
            "max": round(max(latencies) if latencies else 0.0, 2),
        },
        "channel_contributions": channel_contributions,
        "duplicate_injections": {
            "count": dup_count,
            "rate": round(dup_count / max(1, injected_count), 4),
            "examples": dup_examples,
        },
        "effectiveness": {
            "injected_count": injected_count,
            "verified_effective": verified_effective,
            "verified_ineffective": verified_ineffective,
            "injected_unverified": injected_unverified,
            "injection_rate": round(injected_count / max(1, total), 4),
            "verified_effective_rate": round(verified_effective / max(1, injected_count), 4),
        },
        "potential_false_recalls": potential_false_recalls,
        "weights_modified": False,
    }


def format_audit_quality_report(report: dict) -> str:
    """将 analyze_audit_quality 的结构化统计渲染为可读 Markdown 质量报告。"""
    lat = report.get("latency_ms") or {}
    eff = report.get("effectiveness") or {}
    dup = report.get("duplicate_injections") or {}
    ch_map = report.get("channel_contributions") or {}
    lines = [
        "# Memory Audit 检索质量分析报告（只读分析·未修改线上权重）",
        f"- 审计总查询数：`{report.get('total_queries', 0)}`",
        f"- 触发记忆注入查询数：`{report.get('injected_queries', 0)}` (注入率 `{eff.get('injection_rate', 0.0) * 100:.1f}%`)",
        f"- 空结果/未命中通道查询数：`{report.get('zero_hit_count', 0)}` (空结果率 `{report.get('zero_hit_rate', 0.0) * 100:.1f}%`)",
        f"- 检索延迟：`avg={lat.get('avg', 0.0)}ms`, `p50={lat.get('p50', 0.0)}ms`, `p95={lat.get('p95', 0.0)}ms`, `max={lat.get('max', 0.0)}ms`",
        f"- 重复注入率：`{dup.get('rate', 0.0) * 100:.1f}%` (`{dup.get('count', 0)}` 次)",
        "",
        "## 注入有效性核验（结合 Verifier/工具执行结果与用户反馈）",
        f"- **经验证有效 (`verified_effective`)**：`{eff.get('verified_effective', 0)}` (占注入轮次 `{eff.get('verified_effective_rate', 0.0) * 100:.1f}%`)",
        f"- **经验证无效/误召回嫌疑 (`verified_ineffective`)**：`{eff.get('verified_ineffective', 0)}`",
        f"- **仅注入待验证 (`injected_unverified`，纯问答未触发工具验证)**：`{eff.get('injected_unverified', 0)}`",
        "",
        "## 各记忆通道命中贡献",
    ]
    if ch_map:
        for ch, info in ch_map.items():
            lines.append(f"- `{ch}`: `{info['hits']}` 次 (`{info['share'] * 100:.1f}%`)")
    else:
        lines.append("- 暂无通道命中数据")

    pfr = report.get("potential_false_recalls") or []
    if pfr:
        lines.append("\n## 潜在误召回/执行失败关联样本")
        for item in pfr[:5]:
            lines.append(f"- [audit#{item['audit_id']}] query=`{item['query']}` -> `{item['injected_ids']}` ({item['reason']})")
    return "\n".join(lines)
