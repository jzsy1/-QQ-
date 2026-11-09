# -*- coding: utf-8 -*-
"""通用 LLM 客户端（OpenAI 兼容 / Ollama）。

QQ 接入层不依赖主程序 router 也能独立调用 LLM：
- 主程序在运行时，QQ brain 优先复用主 router.brain（同一套人设/模型）；
- 主程序不在时，自动回落到这里，只读 config.yaml 的 brain 段。

实现只用标准库，避免给独立运行包增加额外依赖。
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = ["SimpleBrainConfig", "SimpleBrain", "load_simple_brain"]


@dataclass
class SimpleBrainConfig:
    provider: str = "ollama"
    base_url: str = "http://127.0.0.1:11434"
    model: str = "qwen2.5:3b"
    api_key: str = ""
    timeout_sec: int = 120
    auto_v1: bool = True
    vision_model: str = ""
    capability: str = "standard"

    def normalized_base_url(self) -> str:
        url = (self.base_url or "").rstrip("/")
        if self.provider == "openai" and self.auto_v1 and not url.endswith("/v1"):
            return url + "/v1"
        return url


def _read_yaml(path: Path) -> dict:
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _active_brain_section(data: dict) -> dict:
    brain = data.get("brain") if isinstance(data, dict) else None
    if not isinstance(brain, dict):
        return {}
    profiles = brain.get("profiles")
    if isinstance(profiles, dict) and profiles:
        active = str(brain.get("active") or "").strip()
        selected = profiles.get(active)
        if not isinstance(selected, dict):
            selected = next(
                (p for p in profiles.values() if isinstance(p, dict)), None
            )
        if isinstance(selected, dict):
            return selected
    return brain


def _resolve_api_key(raw: str) -> str:
    value = str(raw or "").strip()
    if value:
        return value
    for name in ("KEKESI_LLM_API_KEY", "OPENAI_API_KEY", "LLM_API_KEY"):
        env = os.environ.get(name, "").strip()
        if env:
            return env
    return ""


def _config_from_section(section: dict) -> SimpleBrainConfig:
    provider = str(section.get("provider") or "ollama").strip().lower()
    if provider not in ("ollama", "openai"):
        provider = "ollama"
    try:
        timeout = int(section.get("timeout_sec") or 120)
    except (TypeError, ValueError):
        timeout = 120
    return SimpleBrainConfig(
        provider=provider,
        base_url=str(section.get("base_url") or "http://127.0.0.1:11434").strip(),
        model=str(section.get("model") or "qwen2.5:3b").strip(),
        api_key=_resolve_api_key(section.get("api_key", "")),
        timeout_sec=max(5, timeout),
        auto_v1=bool(section.get("auto_v1", True)),
        vision_model=str(section.get("vision_model") or "").strip(),
        capability=str(section.get("capability") or "standard").strip(),
    )


class SimpleBrain:
    """与主 Brain 的 chat_once / chat_stream_messages 接口兼容的最小实现。"""

    def __init__(self, config_dir: Path | str | None = None,
                 config: SimpleBrainConfig | None = None):
        self.config_dir = Path(config_dir) if config_dir is not None else Path.cwd()
        self._config_path = self.config_dir / "config.yaml"
        self._lock = threading.RLock()
        self._mtime = 0.0
        self.config = config or self._load_config()

    def _load_config(self) -> SimpleBrainConfig:
        raw = _read_yaml(self._config_path)
        return _config_from_section(_active_brain_section(raw))

    def reload_if_changed(self) -> bool:
        try:
            mtime = self._config_path.stat().st_mtime
        except OSError:
            return False
        with self._lock:
            if mtime <= self._mtime and self._mtime:
                return False
            self._mtime = mtime
            self.config = self._load_config()
            return True

    # -- 调用 ------------------------------------------------------------
    def _post(self, url: str, payload: dict) -> dict:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = "Bearer " + self.config.api_key
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.config.timeout_sec) as resp:
                body = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"LLM 连接失败：{exc}") from exc
        try:
            parsed = json.loads(body)
        except ValueError as exc:
            raise RuntimeError(f"LLM 返回非 JSON：{body[:200]}") from exc
        return parsed if isinstance(parsed, dict) else {}

    def chat_once(self, messages: list[dict],
                  tools: list[dict] | None = None,
                  tool_choice: str | None = None) -> dict | None:
        self.reload_if_changed()
        cfg = self.config
        model = cfg.model
        if cfg.vision_model and _has_image(messages):
            model = cfg.vision_model
        if cfg.provider == "openai":
            url = cfg.normalized_base_url().rstrip("/") + "/chat/completions"
            payload: dict[str, Any] = {
                "model": model,
                "messages": messages,
                "stream": False,
            }
            if tools:
                payload["tools"] = tools
            if tool_choice and tools:
                payload["tool_choice"] = tool_choice
            data = self._post(url, payload)
            choices = data.get("choices") or []
            if not choices:
                return None
            message = choices[0].get("message") or {}
            content = message.get("content")
            if isinstance(content, list):
                content = "".join(
                    str(part.get("text") or "")
                    for part in content
                    if isinstance(part, dict)
                )
            return {"role": "assistant", "content": str(content or "")}

        url = cfg.base_url.rstrip("/") + "/api/chat"
        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
        data = self._post(url, payload)
        message = data.get("message") or {}
        return {"role": "assistant", "content": str(message.get("content") or "")}

    def chat_stream_messages(self, messages: list[dict],
                             cancel_check=None) -> str:
        if callable(cancel_check) and cancel_check():
            return ""
        msg = self.chat_once(messages)
        if not isinstance(msg, dict):
            raise RuntimeError("brain_no_reply")
        return str(msg.get("content") or "")


def _has_image(messages: list[dict]) -> bool:
    for msg in messages:
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, list) and any(
            isinstance(part, dict) and part.get("type") == "image_url"
            for part in content
        ):
            return True
    return False


def load_simple_brain(config_dir: Path | str | None = None) -> SimpleBrain:
    return SimpleBrain(config_dir=config_dir)
