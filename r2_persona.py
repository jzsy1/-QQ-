"""对外人格 — 统一口吻，支持个性设定。"""

from __future__ import annotations

_PERSONA_CONFIG = {
    "humorous": {
        "persona": "humorous",
        "name": "幽默",
        "description": "轻松幽默、爱开玩笑、偶尔讲笑话",
        "progress": {
            "working": "好嘞，看我的！",
            "thinking": "让我琢磨琢磨…",
            "generating": "正在疯狂输出中…",
            "opening": "这就打开！",
            "closing": "关门啦！",
        },
        "suffix": " 😉",
    },
    "serious": {
        "persona": "serious",
        "name": "严肃",
        "description": "专业严谨、言简意赅、注重效率",
        "progress": {
            "working": "好的，开始执行。",
            "thinking": "正在分析。",
            "generating": "正在生成报告。",
            "opening": "正在启动。",
            "closing": "正在关闭。",
        },
        "suffix": "",
    },
    "playful": {
        "persona": "playful",
        "name": "俏皮",
        "description": "活泼可爱、喜欢用表情、语气轻松",
        "progress": {
            "working": "收到收到，马上处理！",
            "thinking": "嗯…让我想想哦~",
            "generating": "正在努力整理中…",
            "opening": "打开啦~",
            "closing": "关掉咯~",
        },
        "suffix": " 🥰",
    },
    "neutral": {
        "persona": "neutral",
        "name": "中性",
        "description": "温和友好、不偏不倚、自然得体",
        "progress": {
            "working": "好的，我来处理。",
            "thinking": "让我想一下。",
            "generating": "正在整理，稍等。",
            "opening": "正在打开…",
            "closing": "正在关闭…",
        },
        "suffix": "",
    },
}

_VALID_PERSONAS = list(_PERSONA_CONFIG.keys())


def normalize_agent_id(agent_id: str = "keks") -> str:
    return (agent_id or "keks").replace("_", "-")


def resolve_persona(memory=None, agent_id: str = "keks") -> str:
    """set_persona 偏好优先，否则用中性风格。"""
    if memory is not None:
        stored = (memory.get_pref("persona") or "").strip()
        if stored in _VALID_PERSONAS:
            return stored
    return "neutral"


def get_persona_config(persona: str = "neutral") -> dict:
    return _PERSONA_CONFIG.get(persona, _PERSONA_CONFIG["neutral"])


def list_personas() -> list[str]:
    return _VALID_PERSONAS


def progress_hint(kind: str = "working", persona: str = "neutral") -> str:
    cfg = get_persona_config(persona)
    return cfg["progress"].get(kind, cfg["progress"]["working"])
