"""系统观察层 — 记录任务/哨兵/执行事件到 QS 事件流。"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class SystemObserver:
    """轻量 Observer：不写业务逻辑，只记录可回放事件。"""

    def __init__(self, qs_store=None):
        self._qs = qs_store

    def bind_qs(self, qs_store) -> None:
        self._qs = qs_store

    def _emit(self, event_type: str, summary: str, payload: dict | None = None) -> None:
        logger.info("[Observer] %s: %s", event_type, summary[:120])
        if not self._qs:
            return
        try:
            self._qs.append(
                event_type,
                summary,
                payload or {},
                session_id="system",
            )
        except Exception:
            logger.debug("Observer 写入 QS 失败", exc_info=True)

    def on_task_due(self, task_id: int, title: str, task_type: str) -> None:
        self._emit(
            "task_due",
            f"任务到期 #{task_id} [{task_type}] {title}",
            {"task_id": task_id, "task_type": task_type, "title": title},
        )

    def on_task_executed(
        self,
        task_id: int,
        func: str,
        *,
        ok: bool,
        message: str,
        verified: bool | None = None,
    ) -> None:
        self._emit(
            "task_executed",
            f"任务#{task_id} {func} {'ok' if ok else 'fail'}: {message[:80]}",
            {
                "task_id": task_id,
                "func": func,
                "ok": ok,
                "message": message,
                "verified": verified,
            },
        )

    def on_reminder(self, task_id: int, title: str, description: str = "") -> None:
        self._emit(
            "reminder",
            f"提醒 #{task_id}: {title}",
            {"task_id": task_id, "title": title, "description": description},
        )

    def on_sentinel_fired(self, rule_id: int, question: str, action_func: str = "") -> None:
        self._emit(
            "sentinel_fired",
            f"哨兵规则#{rule_id} 触发",
            {"rule_id": rule_id, "question": question, "action_func": action_func},
        )

    def on_sentinel_action(
        self,
        rule_id: int,
        func: str,
        *,
        ok: bool,
        message: str,
    ) -> None:
        self._emit(
            "sentinel_action",
            f"哨兵#{rule_id} 执行 {func} {'ok' if ok else 'fail'}",
            {"rule_id": rule_id, "func": func, "ok": ok, "message": message},
        )

    def on_proactive_suggestion(self, text: str, habit_id: int | None = None) -> None:
        self._emit(
            "proactive_suggestion",
            f"主动建议: {text[:80]}",
            {"text": text, "habit_id": habit_id},
        )
