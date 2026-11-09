"""配置读写 — 供管理后台在线编辑 config.yaml / mcp.json / router_rules.json"""

from __future__ import annotations

import json
import logging
import re
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_SENSITIVE_KEYS = frozenset({"api_key", "password", "secret", "token"})
_MASK_PLACEHOLDER = "***"


def _mask_value(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    if len(text) <= 8:
        return _MASK_PLACEHOLDER
    return f"{text[:4]}***{text[-4:]}"


def mask_secrets(data: Any) -> Any:
    if isinstance(data, dict):
        out: dict[str, Any] = {}
        for key, val in data.items():
            if key.lower() in _SENSITIVE_KEYS and isinstance(val, str):
                out[key] = _mask_value(val)
            else:
                out[key] = mask_secrets(val)
        return out
    if isinstance(data, list):
        return [mask_secrets(item) for item in data]
    return data


def _is_masked_secret(value: Any) -> bool:
    return isinstance(value, str) and _MASK_PLACEHOLDER in value


def _merge_secrets(new_data: Any, old_data: Any) -> Any:
    if isinstance(new_data, dict) and isinstance(old_data, dict):
        merged: dict[str, Any] = {}
        for key, val in new_data.items():
            if (
                key.lower() in _SENSITIVE_KEYS
                and _is_masked_secret(val)
                and key in old_data
            ):
                merged[key] = old_data[key]
            elif isinstance(val, dict) and isinstance(old_data.get(key), dict):
                merged[key] = _merge_secrets(val, old_data[key])
            else:
                merged[key] = val
        return merged
    return new_data


class ConfigStore:
    def __init__(self, base_dir: Path | None = None):
        self.base_dir = base_dir or Path(__file__).parent
        self.yaml_path = self.base_dir / "config.yaml"
        self.mcp_path = self.base_dir / "mcp.json"
        self.rules_path = self.base_dir / "data" / "router_rules.json"
        self.assistants_path = (
            self.base_dir.parent / "assistant-x-openclaw" / "assistants.json"
        )
        self.keywords_dir = self.base_dir.parent / "assistant-x-openclaw" / "keywords"
        self.aliases_path = self.base_dir / "data" / "app_aliases.json"

    def _load_yaml(self) -> dict[str, Any]:
        if not self.yaml_path.exists():
            return {}
        try:
            import yaml

            with open(self.yaml_path, encoding="utf-8") as f:
                data = yaml.safe_load(f)
            return data if isinstance(data, dict) else {}
        except ImportError:
            from .llm_config import _parse_brain_from_yaml_text

            parsed = _parse_brain_from_yaml_text(
                self.yaml_path.read_text(encoding="utf-8")
            )
            return parsed
        except Exception as e:
            logger.error("读取 config.yaml 失败: %s", e)
            return {}

    def _save_yaml(self, data: dict[str, Any]) -> None:
        try:
            import yaml

            with open(self.yaml_path, "w", encoding="utf-8") as f:
                yaml.safe_dump(
                    data,
                    f,
                    allow_unicode=True,
                    default_flow_style=False,
                    sort_keys=False,
                )
        except ImportError as e:
            raise RuntimeError("保存配置需要 PyYAML：pip install pyyaml") from e

    def load_config(self, masked: bool = True) -> dict[str, Any]:
        raw = self._load_yaml()
        if masked:
            return mask_secrets(raw)
        return raw

    def update_config_sections(self, updates: dict[str, Any]) -> dict[str, Any]:
        if not updates:
            raise ValueError("没有可更新的配置段")
        current = self._load_yaml()
        for section, payload in updates.items():
            if not isinstance(payload, dict):
                raise ValueError(f"配置段 {section} 必须是对象")
            old_section = current.get(section, {})
            if not isinstance(old_section, dict):
                old_section = {}
            current[section] = _merge_secrets(payload, old_section)
        self._save_yaml(current)
        return mask_secrets(current)

    # === 大脑配置：支持多套 profiles，active 指向当前生效的一套 ===
    _BRAIN_PROFILE_META = frozenset({"profiles", "active"})

    def _ensure_brain_profiles(
        self, data: dict[str, Any]
    ) -> tuple[dict[str, Any], str]:
        """确保 brain 使用 profiles 结构；旧的扁平配置首次调用时迁移为默认配置。"""
        brain = data.get("brain")
        if not isinstance(brain, dict):
            brain = {}
            data["brain"] = brain

        profiles = brain.get("profiles")
        if not isinstance(profiles, dict) or not profiles:
            legacy = {
                key: val
                for key, val in brain.items()
                if key not in self._BRAIN_PROFILE_META
            }
            legacy["name"] = "原有配置"
            profiles = {"default": legacy}
            for key in list(brain):
                if key not in self._BRAIN_PROFILE_META:
                    del brain[key]
        brain["profiles"] = profiles

        active = str(brain.get("active") or "").strip()
        if active not in profiles or not isinstance(profiles.get(active), dict):
            active = next(
                (pid for pid, p in profiles.items() if isinstance(p, dict)), ""
            )
        brain["active"] = active
        return brain, active

    def load_brain_profiles(self) -> dict[str, Any]:
        """返回大脑配置列表（api_key 已掩码）与当前生效的配置 ID。"""
        data = self._load_yaml()
        brain, active = self._ensure_brain_profiles(data)
        items: list[dict[str, Any]] = []
        for pid, profile in brain["profiles"].items():
            if not isinstance(profile, dict):
                continue
            item = {"id": pid, **mask_secrets(profile)}
            item.setdefault("name", pid)
            items.append(item)
        return {"active": active, "profiles": items}

    def add_brain_profile(self, name: str) -> dict[str, Any]:
        """新增一套大脑配置：克隆当前生效配置，原配置保留。"""
        data = self._load_yaml()
        brain, active = self._ensure_brain_profiles(data)
        profiles = brain["profiles"]

        name = str(name or "").strip()
        if not name:
            raise ValueError("配置名称不能为空")
        if len(name) > 32:
            raise ValueError("配置名称不能超过 32 个字符")
        if any(
            isinstance(p, dict) and str(p.get("name") or "").strip() == name
            for p in profiles.values()
        ):
            raise ValueError(f"配置名称已存在: {name}")

        source = profiles.get(active)
        profile = (
            json.loads(json.dumps(source, ensure_ascii=False))
            if isinstance(source, dict)
            else {}
        )
        profile["name"] = name

        profile_id = "brain_" + uuid.uuid4().hex[:6]
        while profile_id in profiles:
            profile_id = "brain_" + uuid.uuid4().hex[:6]
        profiles[profile_id] = profile
        self._save_yaml(data)
        return {"id": profile_id, **mask_secrets(profile)}

    def save_brain_profile(
        self, profile_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """保存指定配置并设为当前生效；掩码 api_key 会保留原值。"""
        pid = str(profile_id or "").strip()
        if not pid:
            raise ValueError("缺少配置 ID")
        data = self._load_yaml()
        brain, _active = self._ensure_brain_profiles(data)
        profiles = brain["profiles"]
        old = profiles.get(pid)
        if not isinstance(old, dict):
            raise KeyError(f"大脑配置不存在: {pid}")

        clean = {
            key: val
            for key, val in payload.items()
            if key not in self._BRAIN_PROFILE_META and key not in ("id",)
        }
        name = str(clean.get("name") or old.get("name") or "").strip()
        if not name:
            raise ValueError("配置名称不能为空")
        if len(name) > 32:
            raise ValueError("配置名称不能超过 32 个字符")
        if any(
            other_id != pid
            and isinstance(other, dict)
            and str(other.get("name") or "").strip() == name
            for other_id, other in profiles.items()
        ):
            raise ValueError(f"配置名称已存在: {name}")
        clean["name"] = name

        merged = dict(old)
        merged.update(_merge_secrets(clean, old))
        profiles[pid] = merged
        brain["active"] = pid
        self._save_yaml(data)
        return {"active": pid, "profile": {"id": pid, **mask_secrets(profiles[pid])}}

    def delete_brain_profile(self, profile_id: str) -> dict[str, Any]:
        """删除一套配置（至少保留一套）；删除生效中配置时自动切换到第一套。"""
        pid = str(profile_id or "").strip()
        if not pid:
            raise ValueError("缺少配置 ID")
        data = self._load_yaml()
        brain, _active = self._ensure_brain_profiles(data)
        profiles = brain["profiles"]
        removed = profiles.get(pid)
        if not isinstance(removed, dict):
            raise KeyError(f"大脑配置不存在: {pid}")
        if len(profiles) <= 1:
            raise ValueError("至少保留一套大脑配置")

        profiles.pop(pid)
        if brain.get("active") == pid:
            brain["active"] = next(iter(profiles))
        self._save_yaml(data)
        return {
            "removed": {"id": pid, **mask_secrets(removed)},
            "active": brain["active"],
        }

    def _read_json(self, path: Path) -> Any:
        if not path.exists():
            return {}
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def _write_json(self, path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")

    def load_mcp_servers(self) -> dict[str, Any]:
        data = self._read_json(self.mcp_path)
        return data if isinstance(data, dict) else {}

    def save_mcp_servers(self, data: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise ValueError("MCP 配置必须是对象")
        self._write_json(self.mcp_path, data)
        return data

    def load_routing_rules(self) -> dict[str, Any]:
        data = self._read_json(self.rules_path)
        return data if isinstance(data, dict) else {}

    def save_routing_rules(self, data: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise ValueError("路由规则必须是对象")
        self._write_json(self.rules_path, data)
        return data

    def load_assistants(self) -> dict[str, Any]:
        data = self._read_json(self.assistants_path)
        return data if isinstance(data, dict) else {}

    def save_assistants(self, data: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise ValueError("assistants.json 必须是对象")
        self._write_json(self.assistants_path, data)
        return data

    def create_assistant(self, payload: dict[str, Any]) -> dict[str, Any]:
        """新增助手：克隆默认助手的工作参数，写入 assistants.json 并预建唤醒词文件。"""
        data = self.load_assistants()
        assistants = data.get("assistants")
        if not isinstance(assistants, list) or not assistants:
            raise ValueError("assistants.json 不存在或没有助手")

        name = str(payload.get("name") or "").strip()
        if not name:
            raise ValueError("助手名称不能为空")
        if len(name) > 32:
            raise ValueError("助手名称不能超过 32 个字符")

        existing_ids = {str(a.get("id") or "") for a in assistants if a.get("id")}
        existing_names = {
            str(a.get("name") or "").strip() for a in assistants if a.get("name")
        }
        if name in existing_names:
            raise ValueError(f"助手名称已存在: {name}")

        assistant_id = str(payload.get("id") or "").strip()
        if assistant_id:
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", assistant_id):
                raise ValueError("助手 ID 只能包含字母、数字、下划线或短横线（1~32 位）")
            if assistant_id in existing_ids:
                raise ValueError(f"助手 ID 已存在: {assistant_id}")
        else:
            assistant_id = "asst_" + uuid.uuid4().hex[:6]
            while assistant_id in existing_ids:
                assistant_id = "asst_" + uuid.uuid4().hex[:6]

        # 以默认助手为模板，保留反馈/特效/系统指令等运行参数
        template = next(
            (a for a in assistants if a.get("id") == data.get("default")),
            assistants[0],
        )
        assistant = json.loads(json.dumps(template, ensure_ascii=False))
        assistant.pop("tts_config", None)  # 语音合成走全局 tts_config
        old_name = str(template.get("name") or "").strip()
        assistant["id"] = assistant_id
        assistant["name"] = name
        assistant["enabled"] = True
        # 唤醒词文件沿用模板所在目录命名，不在代码里写死路径
        template_kw = str(template.get("keywords_file") or "").strip()
        assistant["keywords_file"] = (
            str(Path(template_kw).with_name(f"{assistant_id}.txt")).replace("\\", "/")
            if template_kw
            else ""
        )
        if payload.get("asr_mode"):
            assistant["asr_mode"] = str(payload["asr_mode"]).strip()
        if payload.get("asr_language"):
            assistant["asr_language"] = str(payload["asr_language"]).strip()

        # 文案类配置不新造，沿用模板并把旧助手名替换为新名称
        proactive = assistant.get("proactive_config")
        if not isinstance(proactive, dict):
            proactive = {}
        persona = str(payload.get("persona") or "").strip()
        if persona:
            proactive["persona"] = persona
        elif old_name and proactive.get("persona"):
            proactive["persona"] = str(proactive["persona"]).replace(old_name, name)
        assistant["proactive_config"] = proactive
        if old_name:
            for key in ("wake_lines", "exit_lines"):
                lines = assistant.get(key)
                if isinstance(lines, list):
                    assistant[key] = [
                        str(line).replace(old_name, name) for line in lines
                    ]
            fallback = proactive.get("fallback_greeting_lines")
            if isinstance(fallback, list):
                proactive["fallback_greeting_lines"] = [
                    str(line).replace(old_name, name) for line in fallback
                ]

        assistants.append(assistant)
        data["assistants"] = assistants
        self._write_json(self.assistants_path, data)

        self.keywords_dir.mkdir(parents=True, exist_ok=True)
        keyword_path = self.keywords_dir / f"{assistant_id}.txt"
        if not keyword_path.exists():
            keyword_path.write_text("", encoding="utf-8")
        return assistant

    def delete_assistant(self, assistant_id: str) -> dict[str, Any]:
        """删除助手并清理其独占的唤醒词文件；至少保留一个助手。"""
        assistant_id = (assistant_id or "").strip()
        if not assistant_id:
            raise ValueError("缺少助手 ID")
        data = self.load_assistants()
        assistants = data.get("assistants")
        if not isinstance(assistants, list):
            raise ValueError("assistants.json 不存在或格式不正确")

        target = next((a for a in assistants if a.get("id") == assistant_id), None)
        if target is None:
            raise KeyError(f"助手不存在: {assistant_id}")
        if len(assistants) <= 1:
            raise ValueError("至少保留一个助手")

        data["assistants"] = [a for a in assistants if a.get("id") != assistant_id]
        if data.get("default") == assistant_id:
            data["default"] = data["assistants"][0].get("id")

        # 唤醒词文件被其他助手共用时不删除
        keywords_file = str(target.get("keywords_file") or "").strip()
        shared = any(
            str(a.get("keywords_file") or "").strip() == keywords_file
            for a in data["assistants"]
        )
        self._write_json(self.assistants_path, data)

        if keywords_file and not shared:
            name = Path(keywords_file).stem
            if name and name != "global":
                try:
                    (self.keywords_dir / f"{name}.txt").unlink(missing_ok=True)
                except OSError as e:
                    logger.warning("删除唤醒词文件失败: %s", e)
        return target

    def patch_assistants(self, payload: dict[str, Any]) -> dict[str, Any]:
        data = self.load_assistants()
        if not data:
            raise ValueError("assistants.json 不存在或为空")

        if "default" in payload and payload["default"]:
            data["default"] = str(payload["default"]).strip()

        # 全局 TTS API 配置（所有助手共用一份）
        if isinstance(payload.get("tts_config"), dict):
            base = data.get("tts_config")
            if not isinstance(base, dict):
                base = {}
            base.update(payload["tts_config"])
            data["tts_config"] = base

        updates = payload.get("updates") or []
        if isinstance(updates, list):
            by_id = {a.get("id"): a for a in data.get("assistants", []) if a.get("id")}
            for item in updates:
                if not isinstance(item, dict):
                    continue
                aid = str(item.get("id", "")).strip()
                if not aid or aid not in by_id:
                    continue
                target = by_id[aid]
                for key, val in item.items():
                    if key == "id":
                        continue
                    if key == "persona":
                        proactive = target.get("proactive_config")
                        if not isinstance(proactive, dict):
                            proactive = {}
                        proactive["persona"] = str(val or "").strip()
                        target["proactive_config"] = proactive
                    elif key == "tts_config" and isinstance(val, dict):
                        base = target.get("tts_config") or {}
                        if isinstance(base, dict):
                            base.update(val)
                            target["tts_config"] = base
                        else:
                            target["tts_config"] = val
                    else:
                        target[key] = val

        self._write_json(self.assistants_path, data)
        return data

    def list_keyword_files(self) -> list[str]:
        if not self.keywords_dir.is_dir():
            return []
        names: list[str] = []
        for path in sorted(self.keywords_dir.glob("*.txt")):
            if path.name == "global.txt":
                continue
            names.append(path.stem)
        return names

    def load_keywords(self, name: str) -> str:
        name = (name or "").strip()
        if not name or "/" in name or "\\" in name or ".." in name:
            raise ValueError("无效的唤醒词文件名")
        path = self.keywords_dir / f"{name}.txt"
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8")

    def save_keywords(self, name: str, content: str) -> None:
        name = (name or "").strip()
        if not name or "/" in name or "\\" in name or ".." in name:
            raise ValueError("无效的唤醒词文件名")
        self.keywords_dir.mkdir(parents=True, exist_ok=True)
        path = self.keywords_dir / f"{name}.txt"
        text = (content or "").replace("\r\n", "\n")
        if text and not text.endswith("\n"):
            text += "\n"
        path.write_text(text, encoding="utf-8")

    def load_app_aliases(self) -> dict[str, str]:
        from .app_aliases import load_aliases

        return load_aliases()

    def save_app_aliases(self, data: dict[str, Any]) -> dict[str, str]:
        if not isinstance(data, dict):
            raise ValueError("应用别名必须是对象")
        clean: dict[str, str] = {}
        for key, val in data.items():
            if isinstance(key, str) and isinstance(val, str) and key.strip() and val.strip():
                clean[key.strip().lower()] = val.strip()
        self.aliases_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_json(self.aliases_path, clean)
        return clean

    def paths(self) -> dict[str, str]:
        return {
            "config_yaml": str(self.yaml_path),
            "mcp_json": str(self.mcp_path),
            "router_rules": str(self.rules_path),
            "assistants_json": str(self.assistants_path),
            "keywords_dir": str(self.keywords_dir),
            "app_aliases_json": str(self.aliases_path),
        }
