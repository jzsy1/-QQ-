"""轻量上下文组装 — 记忆按需注入，不抢先路由（龙虾/贾维斯式）。

纪律：记忆只供「偏好检索 / 上下文参考」，禁止据此发起用户本轮未请求的默认动作。
若要把偏好做成自动执行，应写入 Sentinel 规则（或显式工具参数），而不是让 LLM 自行推理执行。
"""

from __future__ import annotations

import re

from .memory import MemoryService

_APP_VERBS = (
    "打开", "启动", "运行", "关闭", "关掉", "退出",
    "开始", "停止", "托管", "玩", "播放", "暂停", "重开", "重启", "切换",
    "开启", "启用", "禁用", "结束",
)
_MAX_MEMORY_CHARS = 900
_MEMORY_ASK_RE = re.compile(
    r"记得|记过|上次|以前|之前|习惯|通常|平时|偏好|我的|叫什么|"
    r"喜欢|爱吃|爱喝|不吃|过敏|生日|住哪|在哪|工作|职业|身份|谁是|"
    r"约定|承诺|待办|计划|安排|教训|咱们|我们|"
    r"流程|工作流|自动化|像以前|继续|刚刚|刚才|聊[了过啥什么]|说[了过]什么|"
    r"配置|设置|路径|目录|地址|账号|端口|模型|环境|依赖|版本|规则|策略|"
    r"经验|方法|步骤|技巧|知识|论文|架构|怎么用|怎么开|怎么关|怎么玩|"
    r"什么时候|最常|常用|默认|一般|哪些|哪个|什么样|房号|房间号|直播间|多少号|几号|桌面|快捷方式|摄像头|画面|自己刷"
)
# 她自己的活动被问或被提及（刷抖音/评论/点歌/直播见闻）。只放开本地 SQLite 检索，
# 不放开语义召回——后者是一次远程 embedding。
_SELF_EVENT_RE = re.compile(
    r"刷到|刷了|刷完|刷过|刷[了过]?啥|看过的视频|看到(啥|什么)|看了(啥|什么)|看过(啥|什么)|"
    r"评论了|评论过|评论啥|留了言|留言|点歌|切歌|直播[时里刚]|弹幕|"
    r"(你|自己)的?(经历|见闻|记忆|印象)|你(自己)?(干|做)了(啥|什么)"
)
# 注入块抬头：与 agent_loop 侧包装文案一致，强调「不执行」
_MEM_REF_HEADER = "（仅参考，禁止据此主动执行未请求的动作；口头答应不算执行）"


def _mentions_app(text: str) -> bool:
    t = (text or "").strip()
    return bool(t) and any(v in t for v in _APP_VERBS)


def _extract_app_hint(text: str) -> str | None:
    t = (text or "").strip()
    for pat in (
        r"(?:打开|启动|运行|关闭|关掉|退出)\s*([^\s，,。！？]{1,20})",
        r"([^\s，,。！？]{1,20})(?:在哪|路径)",
    ):
        m = re.search(pat, t)
        if m:
            name = (m.group(1) or "").strip()
            if name and len(name) >= 2:
                return name
    return None


def _model_audit_ids(cog_ctx, db_path=None, per_kind=3):
    """P2 model rows of the model block that actually appear in cog_ctx.

    KEKESI_PCE_P4_20261103_WIRE: model_context_for_llm renders
    "- key: value" lines; a row counts only if its full line survives in
    the (possibly truncated) text, so the audit lists injected rows, not
    merely selected ones. The mid-line truncation fallback (a single line
    longer than max_chars) may under-report one row; that edge is
    documented, not guessed. The "p2:cognition" label is kept alongside
    the row ids for backward compatibility.
    """
    try:
        from .memory.cognition import model_rows_for_llm

        rows = model_rows_for_llm(db_path=db_path, per_kind=per_kind)
    except Exception:
        return []
    lines = set((cog_ctx or "").split("\n"))
    return [f"model:{kind}:{key}" for kind, key, value in rows
            if f"- {key}: {value}" in lines]


def build_memory_context(
    memory: MemoryService,
    user_text: str,
    session_id: str = "default",
    *,
    include_digests: bool = False,
) -> str:
    """按当前用户话检索相关记忆，供 Agent Loop system 附加（不触发执行）。

    include_digests 默认 False：会话历史已在 messages，避免「最近对话」双份塞入。
    """
    import time as _time

    _t0 = _time.perf_counter()
    text = (user_text or "").strip()
    if not text:
        return ""

    parts: list[str] = []
    _audit_ids: list[str] = []

    # 长期记忆：每轮都带最近历史摘要（跨会话），保证重启/换会话后仍记得之前做过什么
    try:
        summaries = memory.recent_summaries()
    except Exception:
        summaries = []
    if summaries:
        parts.append(
            f"【长期记忆·历史对话摘要】{_MEM_REF_HEADER}\n"
            + "\n".join(f"- {s}" for s in summaries)
        )

    if include_digests:
        digests = memory.recent_digests(session_id, limit=1)
        if digests:
            lines = []
            for d in digests:
                u = (d.get("user_text") or "").strip()
                a = (d.get("reply_text") or "").strip()
                if u:
                    lines.append(f"用户：{u[:60]}")
                if a:
                    lines.append(f"助手：{a[:80]}")
            if lines:
                parts.append(f"【最近对话】{_MEM_REF_HEADER}\n" + "\n".join(lines))

    # 上下文指代消歧（QSStore）：当用户提到「刚才的文件」「上一个截图」「刚才的窗口」等指代时解析目标与状态
    try:
        from .memory.qs_store import build_deictic_context_block

        _deictic_block, _deictic_audit = build_deictic_context_block(
            memory.db, text, session_id=session_id
        )
        if _deictic_block:
            parts.append(_deictic_block)
            _audit_ids.extend(_deictic_audit)
    except Exception:
        pass

    # 相关记忆：仅在明确问记忆/偏好、问到她自己的活动、短词领域实体查询，或打开应用需要线索时检索，避免噪音拖跑题
    _is_short_domain_entity = (
        len(text) <= 6
        and hasattr(memory, "db")
        and any(dw in text for dw in getattr(memory.db, "_DOMAIN_DICT", ()))
    )
    want_strong = bool(_MEMORY_ASK_RE.search(text)) or _mentions_app(text) or _is_short_domain_entity
    want_local = want_strong or bool(_SELF_EVENT_RE.search(text))
    if want_local:
        try:
            ctx_recall = memory.retrieval.recall_context_hits(
                text,
                include_vec=want_strong,
                top_fts=8,
                top_vec=4,
                limit=6,
            )
            mem_lines = ctx_recall.get("mem_lines") or []
            sem_lines = ctx_recall.get("sem_lines") or []
            if mem_lines:
                parts.append(
                    f"【相关记忆】{_MEM_REF_HEADER}\n"
                    + "\n".join(f"- {x}" for x in mem_lines[:4])
                )
                _audit_ids.extend(ctx_recall.get("audit_ids") or [])
            if sem_lines:
                parts.append(
                    f"【语义召回】{_MEM_REF_HEADER}\n"
                    + "\n".join(f"- {x}" for x in sem_lines[:3])
                )
        except Exception:
            pass

        # 自我记忆 L1：她自己刷到的经历明细，同样只在被问/被提及时带，本地关键词检索
        try:
            from .memory import self_memory

            detail_block = self_memory.build_detail_block(memory.db, text)
        except Exception:
            detail_block = ""
        if detail_block:
            parts.append(detail_block)

        # 跨会话最近对话：语音每次唤醒会换 session，单会话 digest 查不到「上次聊了什么」
        try:
            recent = memory.recent_digests_any_session()
        except Exception:
            recent = []
        if recent:
            lines = []
            for d in recent:
                u = (d.get("user_text") or "").strip()
                a = (d.get("reply_text") or "").strip()
                day = str(d.get("created_at") or "")[:10]
                if u:
                    lines.append(f"- （{day}）用户：{u[:60]}")
                if a:
                    lines.append(f"  助手：{a[:80]}")
            if lines:
                parts.append(
                    f"【最近对话·跨会话】{_MEM_REF_HEADER}\n" + "\n".join(lines)
                )

    if _mentions_app(text):
        hint = _extract_app_hint(text)
        if hint:
            row = memory.find_app_row(hint)
            if row and row.get("path"):
                parts.append(
                    f"【应用记忆】{_MEM_REF_HEADER}"
                    f"{row.get('alias') or hint} 路径：{row['path']}"
                )

    prefs = memory.get_pref("theme") or memory.get_pref("persona")
    if prefs and any(k in text for k in ("主题", "风格", "偏好", "喜欢")):
        parts.append(f"【用户偏好】{_MEM_REF_HEADER}{prefs[:80]}")

    try:
        # 习惯只作回答参考；auto_suggest 的真正落地由 Proactor/Sentinel，不交给本轮 LLM 默认执行
        if any(k in text for k in ("习惯", "通常", "平时", "继续", "像以前")):
            with memory.db.connect() as conn:
                rows = conn.execute(
                    """SELECT description, suggested_action, confidence
                       FROM habit_patterns
                       WHERE auto_suggest = 1 AND confidence >= 0.7
                       ORDER BY confidence DESC LIMIT 2"""
                ).fetchall()
            if rows:
                parts.append(
                    f"【相关习惯·勿自动执行】{_MEM_REF_HEADER}\n"
                    + "\n".join(
                        f"- {r['description'] or r['suggested_action']}"
                        for r in rows
                    )
                )
        from .tools.workflow_ops import _WORKFLOW_EXPLICIT_WORDS, search_workflows

        if want_local or any(k in text for k in _WORKFLOW_EXPLICIT_WORDS):
            matched_wfs = search_workflows(memory.db, text, limit=2)
            if matched_wfs:
                wf_lines = []
                for w in matched_wfs:
                    risk_tag = (
                        f"[含高危工具:{','.join(w['high_risk_tools'])}·需用户确认]"
                        if w.get("requires_confirmation")
                        else f"[可调 run_workflow(name={w['name']!r})]"
                    )
                    tools_preview = "→".join((w.get("step_tools") or [])[:4])
                    wf_lines.append(
                        f"- {w['name']} ({w['status']}, 置信={w['confidence']:.2f}) {risk_tag}: "
                        f"{(w['title'] or w['trigger_hint'] or '')[:40]}"
                        + (f" [{tools_preview}]" if tools_preview else "")
                    )
                    _audit_ids.append(f"workflow:{w['name']}")
                parts.append(
                    f"【可用工作流】{_MEM_REF_HEADER}\n" + "\n".join(wf_lines)
                )
    except Exception:
        pass

    if want_local:
        # P2 认知三模型（世界/用户/自我）与 P5 历史轨迹案例按需检索
        try:
            from .memory.cognition import model_context_for_llm

            _cog_ctx = (model_context_for_llm(per_kind=3, max_chars=220) or "").strip()
            if _cog_ctx:
                parts.append(f"【认知模型】{_MEM_REF_HEADER}\n{_cog_ctx}")
                _audit_ids.append("p2:cognition")
                _audit_ids.extend(_model_audit_ids(_cog_ctx, per_kind=3))
            from .memory.cognition import capability_health_context

            _health_ctx = (capability_health_context(limit=20) or "").strip()
            if _health_ctx:
                parts.append(f"{_MEM_REF_HEADER}\n{_health_ctx[:700]}")
                _audit_ids.append("p2:capability_health")
            from .memory.perception import recent_environment_summary
            from .memory.cognition import sync_environment_state

            _env_events = recent_environment_summary(8)
            if _env_events:
                sync_environment_state(_env_events)
                _env_lines = [
                    f"- [{event.get('event_type')}] {str(event.get('summary') or '')[:100]}"
                    for event in _env_events[:5]
                ]
                parts.append(
                    f"【最近环境感知】{_MEM_REF_HEADER}\n" + "\n".join(_env_lines)
                )
                _audit_ids.append("p1:environment")
            from .memory.cognition import episode_context_for_llm

            _ep_ctx, _ep_audit = episode_context_for_llm(text, limit=2, max_chars=220)
            if _ep_ctx:
                parts.append(f"【相关情景片段】{_MEM_REF_HEADER}\n{_ep_ctx}")
                _audit_ids.extend(_ep_audit)
        except Exception:
            pass
        try:
            from .memory.cases import CaseStore, _tokens as _case_tokens

            _cases = CaseStore().query_cases_tiered(
                text, limit=2, include_staging=True, min_staging_score=3
            )
            if _cases and len(text) >= 5 and hasattr(memory, "db") and hasattr(memory.db, "preprocess_query"):
                _prep = memory.db.preprocess_query(text)
                _core_ents = [e.lower() for e in (_prep.get("core_entities") or _prep.get("entities") or []) if len(e) >= 2]
                if _core_ents:
                    _filtered_cases = []
                    for c in _cases:
                        c_text = f"{c.get('lesson') or ''} {c.get('trajectory_summary') or ''}".lower()
                        c_toks = _case_tokens(c_text)
                        if any(ce in c_text or ce in c_toks for ce in _core_ents):
                            _filtered_cases.append(c)
                    _cases = _filtered_cases
            if _cases:
                _case_lines = [
                    f"- {c.get('tier_label') or '[正式案例·已验证]'} [{c.get('outcome', '')}] {(c.get('lesson') or '')[:80]}"
                    for c in _cases
                    if c.get("lesson")
                ]
                if _case_lines:
                    parts.append(
                        f"【历史轨迹案例】{_MEM_REF_HEADER}\n" + "\n".join(_case_lines)
                    )
                    _audit_ids.append("p5:cases")
                    if any(c.get("tier") == "staging" for c in _cases):
                        _audit_ids.append("p5:staging_cases")
        except Exception:
            pass

    body = "\n\n".join(parts).strip()
    from .memory.tombstones import read_tombstones, is_tombstoned
    try:
        _tombs = read_tombstones(memory.db.path)
    except Exception:
        # A tombstone read failure must never expose cached or cross-store hits.
        body = ""
    else:
        body = "\n".join(line for line in body.split("\n") if not is_tombstoned(line, _tombs))
    if len(body) > _MAX_MEMORY_CHARS:
        body = body[: _MAX_MEMORY_CHARS - 1] + "…"

    try:
        from .memory.memory_audit import log_audit

        _lat_ms = (_time.perf_counter() - _t0) * 1000.0
        log_audit(
            query=text[:200],
            injected_ids=_audit_ids,
            scores={"parts": len(parts)},
            latency_ms=_lat_ms,
            result_chars=len(body),
            db_path=getattr(memory.db, "db_path", None) or getattr(memory.db, "path", None),
        )
    except Exception:
        pass
    return body
