"""角色人设中心 — 人设全部由后台创建,代码不内置任何角色。

与 r2_persona 的「语气风格」(幽默/严肃/俏皮/中性,仅影响进度提示语)不同,
本模块管理的是「角色人设」——即 LLM 对话用的系统提示词(身份与口吻):

- 人设来源: 只有后台创建的人设,持久化到 data/custom_personas.json,无内置角色。
- 激活机制: 当前生效的人设 id 存记忆偏好 persona_role,任何对话入口
  (语音壳 / 手机后台 / 企微 / REPL)在组装系统提示词时统一读取。

全部 fail-soft: 模块异常、文件损坏、偏好失效一律回退到 agent_id 的默认提示词,
不影响主对话流程。
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .brain import get_system_prompt

logger = logging.getLogger(__name__)

# 记住偏好所用的键(存于 memory user_profile 表)
PERSONA_ROLE_PREF = "persona_role"

# 自定义人设存储文件名(相对 kekesi/ 根目录)
CUSTOM_PERSONAS_FILE = "data/custom_personas.json"


def _default_dir() -> Path:
    return Path(__file__).resolve().parent


def _time_now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


class PersonaStore:
    """自定义人设的增删改查,持久化到 data/custom_personas.json。

    字段:id(uuid 前 8 位,不与内置角色冲突)、name、description、system_prompt、
    created_at、updated_at。文件不存在/损坏时容错为空列表,写入时原子替换。
    """

    def __init__(self, base_dir: Path | None = None):
        self.base_dir = base_dir or _default_dir()
        self.path = self.base_dir / CUSTOM_PERSONAS_FILE
        self._lock = threading.RLock()

    # ---- 内部读写 ----
    def _load(self) -> list[dict[str, Any]]:
        try:
            if not self.path.exists():
                return []
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return [d for d in data if isinstance(d, dict)]
            return []
        except Exception as e:  # noqa: BLE001
            logger.warning("读取自定义人设失败(%s),按空列表处理", e)
            return []

    def _save(self, items: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
        tmp.replace(self.path)

    # ---- 查询 ----
    def list_custom(self) -> list[dict[str, Any]]:
        with self._lock:
            return self._load()

    def get_custom(self, persona_id: str) -> dict[str, Any] | None:
        pid = (persona_id or "").strip()
        with self._lock:
            for p in self._load():
                if p.get("id") == pid:
                    return p
        return None

    # ---- 写操作 ----
    def create_custom(
        self,
        name: str,
        description: str = "",
        system_prompt: str = "",
    ) -> dict[str, Any]:
        name = (name or "").strip()
        system_prompt = (system_prompt or "").strip()
        if not name:
            raise ValueError("人设名称不能为空")
        if not system_prompt:
            raise ValueError("系统提示词不能为空")
        with self._lock:
            items = self._load()
            item: dict[str, Any] = {
                "id": "c" + uuid.uuid4().hex[:7],
                "name": name,
                "description": (description or "").strip(),
                "system_prompt": system_prompt,
                "created_at": _time_now(),
                "updated_at": _time_now(),
            }
            items.append(item)
            self._save(items)
            return item

    def update_custom(
        self,
        persona_id: str,
        name: str,
        description: str = "",
        system_prompt: str = "",
    ) -> dict[str, Any]:
        pid = (persona_id or "").strip()
        name = (name or "").strip()
        system_prompt = (system_prompt or "").strip()
        if not name:
            raise ValueError("人设名称不能为空")
        if not system_prompt:
            raise ValueError("系统提示词不能为空")
        with self._lock:
            items = self._load()
            for p in items:
                if p.get("id") == pid:
                    p["name"] = name
                    p["description"] = (description or "").strip()
                    p["system_prompt"] = system_prompt
                    p["updated_at"] = _time_now()
                    self._save(items)
                    return p
        raise KeyError(f"自定义人设不存在: {pid}")

    def delete_custom(self, persona_id: str) -> bool:
        pid = (persona_id or "").strip()
        with self._lock:
            items = self._load()
            remaining = [p for p in items if p.get("id") != pid]
            if len(remaining) == len(items):
                return False
            self._save(remaining)
            return True


def get_active_role(memory) -> str | None:
    """读取当前激活的人设 id(内存偏好 persona_role)。"""
    if memory is None:
        return None
    try:
        role = (memory.get_pref(PERSONA_ROLE_PREF) or "").strip()
        return role or None
    except Exception:  # noqa: BLE001
        logger.debug("读取激活人设偏好失败", exc_info=True)
        return None


def resolve_system_prompt(memory, agent_id: str = "keks", config=None) -> str:
    """按「当前激活人设」解析系统提示词,优先级:

    1. 激活的人设 → 直接用其 system_prompt;
    2. 未激活 / 偏好失效 / 任何异常 → 回退 agent_id 默认提示词。

    任何异常都不会破坏主对话流程。
    """
    try:
        store = PersonaStore()
        role = get_active_role(memory)
        if not role:
            return get_system_prompt(agent_id, config)
        custom = store.get_custom(role)
        if custom is not None:
            return str(custom.get("system_prompt") or "").strip() or get_system_prompt(agent_id, config)
        # 偏好存了未知 id(如被删除的人设)→ 回退
        return get_system_prompt(agent_id, config)
    except Exception:  # noqa: BLE001
        logger.warning("解析人设系统提示词失败,回退默认角色 %s", agent_id, exc_info=True)
        try:
            return get_system_prompt(agent_id, config)
        except Exception:  # noqa: BLE001
            return ""