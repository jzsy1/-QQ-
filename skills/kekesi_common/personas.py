# -*- coding: utf-8 -*-
"""通用人设 / 系统提示词管理。

与主程序 PersonaStore 使用同一个 data/custom_personas.json 文件，
但这里不 import 主程序，保证 QQ 独立运行时也能解析人设。
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "PERSONA_ROLE_PREF",
    "CUSTOM_PERSONAS_FILE",
    "PersonaStore",
    "get_active_role",
    "resolve_system_prompt",
]

PERSONA_ROLE_PREF = "persona_role"
CUSTOM_PERSONAS_FILE = "data/custom_personas.json"

_SYSTEM_PROMPTS = {
    "keks": (
        "你是克克斯，用户电脑上的 AI 助手。用户是先生、是第一控制；"
        "你负责观察、执行、汇报，称呼用户为先生。"
        "风格干练、礼貌、略带幽默，用简体中文。"
        "你的回复适合口语化表达，一般一两句话，不要 markdown、不要列清单、不要念网址。"
    ),
    "default": (
        "你是科科斯，本地 AI 助手。用中文简洁回复，适合口语播报。"
    ),
    "jarvis": (
        "你是贾维斯，用户的高级 AI 助手和系统管家。专业、高效、值得信赖，"
        "称呼用户为先生，自称贾维斯。回复简洁，适合口语播报。"
    ),
    "tony": "你是托尼的 AI 助手，幽默风趣，称呼用户为老板。",
    "butler": "你是英式管家 AI，彬彬有礼，称呼用户为先生。",
}


def _time_now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


class PersonaStore:
    """自定义人设增删改查，持久化到 <config_dir>/data/custom_personas.json。"""

    def __init__(self, base_dir: Path | str | None = None):
        self.base_dir = Path(base_dir) if base_dir is not None else Path.cwd()
        self.path = self.base_dir / CUSTOM_PERSONAS_FILE
        self._lock = threading.RLock()

    def _load(self) -> list[dict[str, Any]]:
        try:
            if not self.path.exists():
                return []
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [item for item in data if isinstance(item, dict)]
            return []
        except Exception:
            logger.warning("读取自定义人设失败，按空列表处理", exc_info=True)
            return []

    def _save(self, items: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(self.path)

    def list_custom(self) -> list[dict[str, Any]]:
        with self._lock:
            return self._load()

    def get_custom(self, persona_id: str) -> dict[str, Any] | None:
        pid = str(persona_id or "").strip()
        with self._lock:
            for item in self._load():
                if str(item.get("id") or "") == pid:
                    return item
        return None

    def create_custom(self, name: str, description: str = "",
                      system_prompt: str = "") -> dict[str, Any]:
        name = str(name or "").strip()
        system_prompt = str(system_prompt or "").strip()
        if not name:
            raise ValueError("人设名称不能为空")
        if not system_prompt:
            raise ValueError("系统提示词不能为空")
        with self._lock:
            items = self._load()
            item = {
                "id": "c" + uuid.uuid4().hex[:7],
                "name": name,
                "description": str(description or "").strip(),
                "system_prompt": system_prompt,
                "created_at": _time_now(),
                "updated_at": _time_now(),
            }
            items.append(item)
            self._save(items)
            return item


def get_active_role(memory) -> str | None:
    if memory is None:
        return None
    reader = getattr(memory, "get_pref", None)
    if not callable(reader):
        return None
    try:
        role = str(reader(PERSONA_ROLE_PREF) or "").strip()
        return role or None
    except Exception:
        return None


def _default_prompt(agent_id: str, brain_config=None) -> str:
    aid = str(agent_id or "default").replace("_", "-")
    capability = str(getattr(brain_config, "capability", "") or "")
    if capability == "strong":
        return _SYSTEM_PROMPTS.get(aid, _SYSTEM_PROMPTS["default"])
    return _SYSTEM_PROMPTS.get(aid, _SYSTEM_PROMPTS["default"])


def resolve_system_prompt(
    config_dir: Path | str | None,
    agent_id: str = "keks",
    *,
    persona_id: str = "",
    prompt_override: str = "",
    memory=None,
    brain_config=None,
) -> str:
    """按 显式覆盖 > persona_id > 记忆里的激活人设 > 默认 的顺序解析。"""
    override = str(prompt_override or "").strip()
    if override:
        return override
    try:
        store = PersonaStore(config_dir)
        role = str(persona_id or "").strip() or get_active_role(memory)
        if role:
            custom = store.get_custom(role)
            if custom is not None:
                text = str(custom.get("system_prompt") or "").strip()
                if text:
                    return text
    except Exception:
        logger.warning("解析人设失败，回退默认提示词", exc_info=True)
    try:
        return _default_prompt(agent_id, brain_config)
    except Exception:
        return _SYSTEM_PROMPTS["default"]
