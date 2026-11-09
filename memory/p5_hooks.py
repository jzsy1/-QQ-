"""P5 single entry point called from the learning chain.

p5_post_turn() runs the episode rebuild and the turn-trajectory case
recording. It never raises: every stage is guarded, so a P5 failure can
never break learning. user_text / reply_text are accepted for future
situation-keyed retrieval; today only the tool trajectory is recorded.

Marker: KEKESI_PCE_P5_20261101
"""

import logging
from datetime import datetime, timezone

MARKER = "KEKESI_PCE_P5_20261101"
logger = logging.getLogger(__name__)


def _normalize_tools(tool_results):
    steps = []
    for item in tool_results or []:
        if isinstance(item, dict):
            name = item.get("func") or item.get("name") or item.get("tool")
            ok = item.get("ok", item.get("success", True))
            error = item.get("error") or ""
        else:
            name = (
                getattr(item, "func", None)
                or getattr(item, "name", None)
                or getattr(item, "tool", None)
            )
            ok = getattr(item, "ok", getattr(item, "success", True))
            error = getattr(item, "error", "") or ""
        steps.append(
            {"name": str(name or "unknown_tool"), "ok": bool(ok), "error": str(error)}
        )
    return steps


def p5_post_turn(tool_results=None, user_text=None, reply_text=None):
    """Episode rebuild + turn case recording. Never raises."""
    try:
        from kekesi.memory.episodes import rebuild_episodes

        rebuild_episodes()
    except Exception:
        logger.exception("P5 episode rebuild failed")
    try:
        steps = _normalize_tools(tool_results)
        if not steps:
            return None
        from kekesi.memory.cases import CaseStore

        failed = [step for step in steps if not step["ok"]]
        names = [step["name"] for step in steps]
        now = datetime.now(timezone.utc)
        if failed:
            first = failed[0]
            lesson = "失败：" + first["name"]
            if first["error"]:
                lesson += "：" + first["error"][:200]
            outcome = "failure"
        else:
            lesson = "成功轨迹：" + " -> ".join(names)
            outcome = "success"
        CaseStore().record_case(
            trajectory=names,
            outcome=outcome,
            lesson=lesson,
            steps=names,
            evidence_ids=["turn:%s" % now.date().isoformat()],
            created_at=now.isoformat(),
        )
    except Exception:
        logger.exception("P5 case recording failed")
    return None
