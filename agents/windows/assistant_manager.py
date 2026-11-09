"""AssistantManager — 个性设定与情感分析。"""

from __future__ import annotations

from typing import Any

from ...tools.execution_result import ExecutionResult


class AssistantManager:
    """助手人格与情感辅助。"""

    def __init__(self, tool_ctx: Any = None) -> None:
        self._ctx = tool_ctx

    def detect_emotion(self, text: str) -> ExecutionResult:
        from ...emotion_recognizer import analyze_emotion

        raw = (text or "").strip()
        if not raw:
            return ExecutionResult(
                success=False,
                message="请提供要分析的文本",
                verified=False,
                action="detect_emotion",
            )
        result = analyze_emotion(raw)
        msg = (
            f"情感分析结果：\n- 情感类型：{result['name']}\n"
            f"- 置信度：{result['confidence']:.2f}\n"
            f"- 描述：{result['description']}\n"
            f"- 建议语气：{result['response_tone']}"
        )
        return ExecutionResult(
            success=True,
            message=msg,
            verified=True,
            action="detect_emotion",
            target=raw[:40],
            extra=result,
        )

    def set_persona(self, persona: str) -> ExecutionResult:
        from ...r2_persona import get_persona_config, list_personas

        raw = (persona or "").strip()
        valid = list_personas()
        if raw not in valid:
            return ExecutionResult(
                success=False,
                message=f"无效的个性风格，可选值: {', '.join(valid)}",
                verified=False,
                action="set_persona",
            )
        if self._ctx and getattr(self._ctx, "memory", None):
            self._ctx.memory.set_pref("persona", raw)
        cfg = get_persona_config(raw)
        return ExecutionResult(
            success=True,
            message=f"好的，已切换到「{cfg['name']}」风格！{cfg['description']}",
            verified=True,
            action="set_persona",
            target=raw,
        )

    def get_persona(self) -> ExecutionResult:
        from ...r2_persona import get_persona_config

        current = "neutral"
        if self._ctx and getattr(self._ctx, "memory", None):
            current = self._ctx.memory.get_pref("persona") or "neutral"
        cfg = get_persona_config(current)
        return ExecutionResult(
            success=True,
            message=f"当前个性风格：「{cfg['name']}」— {cfg['description']}",
            verified=True,
            action="get_persona",
            target=current,
        )

    def list_personas(self) -> ExecutionResult:
        from ...r2_persona import get_persona_config, list_personas

        lines = []
        for p in list_personas():
            cfg = get_persona_config(p)
            lines.append(f"- {p}（{cfg['name']}）：{cfg['description']}")
        return ExecutionResult(
            success=True,
            message="可用的个性风格：\n" + "\n".join(lines),
            verified=True,
            action="list_personas",
        )
