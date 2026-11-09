"""LLM 配置加载 — 支持 Ollama / OpenAI 兼容 API"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class BrainConfig:
    provider: str = "ollama"  # ollama | openai
    base_url: str = "http://127.0.0.1:11434"
    model: str = "qwen2.5:3b"
    # 视觉模型（可选）：含截图/图片的请求自动切换到该模型；空 = 全部走 model
    vision_model: str = ""
    api_key: str = ""
    timeout_sec: int = 120
    # 是否自动在 base_url 末尾追加 /v1（关闭后原样使用，便于 LM Studio 等本地服务）
    auto_v1: bool = True
    # standard：语音短回复；strong：多模态/大模型，优先 LLM 规划与自然对话
    capability: str = "standard"
    vision: bool = False
    max_reply_chars: int = 80
    # 思考模式开关：True(默认) 不干预模型默认行为；False 透传 chat_template_kwargs.enable_thinking=False
    # 用于关闭 Qwen3 等模型的思考链，避免 KV cache 撑爆内存
    enable_thinking: bool = True
    # 上下文字符预算：0 = 保守兜底（agent_loop 内 5000）；>0 显式指定（换大上下文模型后调大）
    context_max_chars: int = 0
    # 工具清单占上下文预算的最大比例：全池清单超过该比例则 tier0 渐进披露起步，
    # 否则直接全池。<=0 = 兜底 0.25
    tool_briefing_max_ratio: float = 0.25
    # 混合检索（BM25 + 向量语义召回兜底）：嵌入服务只读顶层 embedding 段
    # （后台「语义检索 embedding」面板），该段未启用或 hybrid_enabled=False 时纯 BM25
    hybrid_enabled: bool = False
    hybrid_db_path: str = ""
    hybrid_min_score: float = 0.35
    hybrid_top_k: int = 5

    @property
    def provider_label(self) -> str:
        return "Ollama" if self.provider == "ollama" else "OpenAI兼容"

    def normalized_base_url(self) -> str:
        url = self.base_url.rstrip("/")
        if self.provider == "openai" and self.auto_v1 and not url.endswith("/v1"):
            return url + "/v1"
        return url


def _resolve_api_key(raw: str) -> str:
    if raw:
        return raw.strip()
    for env_name in ("KEKESI_LLM_API_KEY", "OPENAI_API_KEY", "LLM_API_KEY"):
        val = os.environ.get(env_name, "").strip()
        if val:
            return val
    return ""


def _load_raw_config(base: Path) -> dict:
    json_path = base / "config.json"
    yaml_path = base / "config.yaml"

    if json_path.exists():
        with open(json_path, encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}

    if yaml_path.exists():
        try:
            import yaml  # type: ignore

            with open(yaml_path, encoding="utf-8") as f:
                data = yaml.safe_load(f)
                return data if isinstance(data, dict) else {}
        except ImportError:
            return _parse_brain_from_yaml_text(yaml_path.read_text(encoding="utf-8"))

    return {}


def _parse_brain_from_yaml_text(text: str) -> dict:
    """无 PyYAML 时，仅解析 brain 段的常用字段"""
    section = ""
    brain: dict = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.endswith(":") and not stripped.startswith("-"):
            section = stripped[:-1].strip()
            continue
        if section != "brain":
            continue
        m = re.match(r"^([a-z_]+):\s*(.+)$", stripped)
        if not m:
            continue
        key, value = m.group(1), m.group(2).strip().strip('"').strip("'")
        if key in ("provider", "base_url", "model", "vision_model", "api_key"):
            brain[key] = value
        elif key == "timeout_sec":
            try:
                brain[key] = int(value)
            except ValueError:
                pass
        elif key == "auto_v1":
            brain[key] = value.lower() in ("true", "1", "yes", "on")
        elif key == "capability":
            brain[key] = value
        elif key == "vision":
            brain[key] = value.lower() in ("true", "1", "yes", "on")
        elif key == "enable_thinking":
            brain[key] = value.lower() in ("true", "1", "yes", "on")
        elif key == "context_max_chars":
            try:
                brain[key] = int(value)
            except ValueError:
                pass
        elif key == "tool_briefing_max_ratio":
            try:
                brain[key] = float(value)
            except ValueError:
                pass
        elif key == "hybrid_enabled":
            brain[key] = value.lower() in ("true", "1", "yes", "on")
        elif key == "hybrid_db_path":
            brain[key] = value
        elif key == "hybrid_min_score":
            try:
                brain[key] = float(value)
            except ValueError:
                pass
        elif key == "hybrid_top_k":
            try:
                brain[key] = int(value)
            except ValueError:
                pass
    return {"brain": brain} if brain else {}


def _resolve_brain_section(loaded: dict) -> dict:
    """brain.profiles 存在时取 active 指向的那套；否则兼容旧的扁平结构。"""
    brain = loaded.get("brain", {}) if isinstance(loaded, dict) else {}
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


def load_embedding_config(config_dir: Path | None = None) -> dict:
    """语义检索 embedding 段（后台「语义检索 embedding」面板写入处）。"""
    base = config_dir or Path(__file__).parent
    loaded = _load_raw_config(base)
    data = loaded.get("embedding") if isinstance(loaded, dict) else None
    return data if isinstance(data, dict) else {}


def load_brain_config(config_dir: Path | None = None) -> BrainConfig:
    base = config_dir or Path(__file__).parent
    loaded = _load_raw_config(base)
    data = _resolve_brain_section(loaded)

    provider = str(data.get("provider", "ollama")).strip().lower()
    if provider not in ("ollama", "openai"):
        provider = "ollama"

    if provider == "openai":
        default_url = "https://api.openai.com/v1"
        default_model = "gpt-4o-mini"
    else:
        default_url = "http://127.0.0.1:11434"
        default_model = "qwen2.5:3b"

    raw_timeout = data.get("timeout_sec", 120)
    try:
        timeout_sec = int(raw_timeout)
    except (TypeError, ValueError):
        timeout_sec = 120

    raw_auto_v1 = data.get("auto_v1", True)
    if isinstance(raw_auto_v1, str):
        auto_v1 = raw_auto_v1.strip().lower() in ("true", "1", "yes", "on")
    else:
        auto_v1 = bool(raw_auto_v1)

    capability = str(data.get("capability", "standard")).strip().lower()
    if capability not in ("standard", "strong"):
        capability = "standard"

    raw_vision = data.get("vision", capability == "strong")
    if isinstance(raw_vision, str):
        vision = raw_vision.strip().lower() in ("true", "1", "yes", "on")
    else:
        vision = bool(raw_vision)

    raw_max = data.get("max_reply_chars")
    if raw_max is not None:
        try:
            max_reply_chars = int(raw_max)
        except (TypeError, ValueError):
            max_reply_chars = 480 if capability == "strong" else 80
    else:
        max_reply_chars = 480 if capability == "strong" else 80

    raw_enable_thinking = data.get("enable_thinking", True)
    if isinstance(raw_enable_thinking, str):
        enable_thinking = raw_enable_thinking.strip().lower() in ("true", "1", "yes", "on")
    else:
        enable_thinking = bool(raw_enable_thinking)

    raw_ctx = data.get("context_max_chars")
    try:
        context_max_chars = int(raw_ctx)
    except (TypeError, ValueError):
        context_max_chars = 0
    if context_max_chars < 0:
        context_max_chars = 0

    raw_ratio = data.get("tool_briefing_max_ratio")
    try:
        tool_ratio = float(raw_ratio)
    except (TypeError, ValueError):
        tool_ratio = 0.25
    if tool_ratio <= 0:
        tool_ratio = 0.25
    elif tool_ratio > 1:
        tool_ratio = 1.0

    raw_hybrid = data.get("hybrid_enabled", False)
    if isinstance(raw_hybrid, str):
        hybrid_enabled = raw_hybrid.strip().lower() in ("true", "1", "yes", "on")
    else:
        hybrid_enabled = bool(raw_hybrid)

    try:
        hybrid_min_score = float(data.get("hybrid_min_score", 0.35))
    except (TypeError, ValueError):
        hybrid_min_score = 0.35

    try:
        hybrid_top_k = int(data.get("hybrid_top_k", 5))
    except (TypeError, ValueError):
        hybrid_top_k = 5
    if hybrid_top_k <= 0:
        hybrid_top_k = 5

    return BrainConfig(
        provider=provider,
        base_url=str(data.get("base_url", default_url)).strip(),
        model=str(data.get("model", default_model)).strip() or default_model,
        vision_model=str(data.get("vision_model", "")).strip(),
        api_key=_resolve_api_key(str(data.get("api_key", ""))),
        timeout_sec=timeout_sec,
        auto_v1=auto_v1,
        capability=capability,
        vision=vision,
        max_reply_chars=max_reply_chars,
        enable_thinking=enable_thinking,
        context_max_chars=context_max_chars,
        tool_briefing_max_ratio=tool_ratio,
        hybrid_enabled=hybrid_enabled,
        hybrid_db_path=str(data.get("hybrid_db_path") or "").strip(),
        hybrid_min_score=hybrid_min_score,
        hybrid_top_k=hybrid_top_k,
    )


def is_strong_capability(config: BrainConfig) -> bool:
    return getattr(config, "capability", "standard") == "strong"


def is_local_llm_url(base_url: str) -> bool:
    """局域网 / 本机推理服务（LM Studio、Ollama 等）。"""
    u = (base_url or "").strip().lower()
    if not u:
        return False
    if "localhost" in u or "127.0.0.1" in u:
        return True
    if re.search(r"://192\.168\.\d{1,3}\.\d{1,3}", u):
        return True
    if re.search(r"://10\.\d{1,3}\.\d{1,3}\.\d{1,3}", u):
        return True
    if re.search(r"://172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}", u):
        return True
    return False
