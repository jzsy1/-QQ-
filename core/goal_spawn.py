"""目标派生 — Goal → Task 的唯一入口（v1）。

v1（KEKESI_GOAL_SPAWN_V1）：后台派生走 v2 handle 路径（返回 Task），
不用交互式 legacy 路径；授权发生在目标创建时（见方案 §一原则 4）。
本文件只依赖标准库（logging）；runtime/goal_store 均按鸭子类型传入。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def spawn_goal_task(
    runtime: Any,
    goal_store: Any,
    goal: Any,
    *,
    deadline_at: str | None = None,
    max_tool_calls: int | None = None,
    autonomous: bool = False,
    goal_id: str | None = None,
) -> Any:
    """派生一次目标任务：经监督链执行，终态后记回 goal。记录失败不掩盖任务结果
    （下次 reconcile 纠正）。返回 Task（runtime.handle 的返回值）。"""
    runtime_kwargs = {}
    if deadline_at:
        runtime_kwargs["deadline_at"] = deadline_at
    if max_tool_calls is not None:
        runtime_kwargs["max_tool_calls"] = int(max_tool_calls)
    if autonomous:
        runtime_kwargs["autonomous"] = True
    if goal_id:
        runtime_kwargs["goal_id"] = str(goal_id)
    task = runtime.handle(goal.goal, **runtime_kwargs)
    try:
        goal_store.record_task(
            goal.id, task.id, task.status,
            error=getattr(task, "error", "") or "",
        )
    except Exception:
        logger.warning("目标 %s 的任务终态回写失败，待 reconcile 纠正", goal.id)
    return task
