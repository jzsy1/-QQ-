"""接入框架配置 — 读 config.yaml 的 integrations 段与 config/bridges/*.json。

Token 一律走 token_env 环境变量或 security/secrets，不落配置文件；
本模块只返回环境变量名，不返回也不打印 token 明文。
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .registry import BridgeManifest

logger = logging.getLogger(__name__)

_PKG_ROOT = Path(__file__).resolve().parent.parent
_BRIDGE_DIR_NAME = "bridges"
_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")


@dataclass
class IntegrationsConfig:
    """框架级开关，对应 config.yaml 的 integrations 段。"""

    enabled: bool = False
    auto_discovery: bool = True
    health_check_interval_sec: float = 15.0
    default_timeout_sec: float = 30.0
    max_concurrent_operations: int = 10
    bridges: list["BridgeConfig"] = field(default_factory=list)


@dataclass
class TransportConfig:
    """单个传输的连接参数。"""

    kind: str
    enabled: bool = True
    url: str = ""
    base_url: str = ""
    token_env: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    connect_timeout_sec: float = 20.0
    call_timeout_sec: float = 30.0

    @classmethod
    def from_dict(cls, kind: str, raw: dict[str, Any]) -> "TransportConfig":
        raw = raw or {}
        return cls(
            kind=kind,
            enabled=bool(raw.get("enabled", True)),
            url=str(raw.get("url") or ""),
            base_url=str(raw.get("base_url") or ""),
            token_env=str(raw.get("token_env") or raw.get("auth_token_env") or ""),
            headers=dict(raw.get("headers") or {}),
            connect_timeout_sec=float(raw.get("connect_timeout_sec") or 20.0),
            call_timeout_sec=float(raw.get("call_timeout_sec") or 30.0),
        )

    def resolve_headers(self) -> dict[str, str]:
        """展开环境变量占位；缺值的 header 整条丢弃，token 永不进返回值以外的地方。"""
        out: dict[str, str] = {}
        for key, val in self.headers.items():
            expanded = _expand_env(str(val))
            if expanded.strip():
                out[key] = expanded
        if self.token_env and not any(k.lower() == "authorization" for k in out):
            token = os.environ.get(self.token_env, "")
            if token:
                out["Authorization"] = f"Bearer {token}"
        return out


@dataclass
class BridgeConfig:
    """一个 Bridge 的完整配置：清单 + 各传输。"""

    manifest: BridgeManifest
    transports: dict[str, TransportConfig] = field(default_factory=dict)
    source: str = ""
    errors: list[str] = field(default_factory=list)

    @property
    def id(self) -> str:
        return self.manifest.id

    def transport(self, kind: str) -> TransportConfig | None:
        conf = self.transports.get(kind)
        if conf is None or not conf.enabled:
            return None
        return conf


def _expand_env(value: str) -> str:
    """把 ${VAR} / $VAR 展开成环境变量值，未设置的展开为空串。"""

    def _sub(match: re.Match) -> str:
        name = match.group(1) or match.group(2)
        return os.environ.get(name, "")

    return _ENV_RE.sub(_sub, value)


def _bridge_dir(config_dir: Path | None = None) -> Path:
    base = Path(config_dir) if config_dir else _PKG_ROOT / "config"
    return base / _BRIDGE_DIR_NAME


def _load_yaml_section(config_dir: Path | None = None) -> dict[str, Any]:
    """从 config.yaml 取 integrations 段；yaml 不可用或文件缺失时返回空。"""
    path = (Path(config_dir) if config_dir else _PKG_ROOT) / "config.yaml"
    if not path.is_file():
        return {}
    try:
        import yaml
    except ImportError:
        logger.debug("yaml 不可用，跳过 integrations 段读取")
        return {}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        logger.warning("读取 config.yaml 失败: %s", exc)
        return {}
    section = raw.get("integrations") if isinstance(raw, dict) else None
    return section if isinstance(section, dict) else {}


def _parse_bridge_file(path: Path) -> BridgeConfig | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Bridge 配置 %s 解析失败: %s", path.name, exc)
        return None
    if not isinstance(raw, dict):
        logger.warning("Bridge 配置 %s 顶层不是对象，已跳过", path.name)
        return None

    raw.setdefault("id", path.stem)
    manifest = BridgeManifest.from_dict(raw)
    errors, _ = manifest.validate()
    transports: dict[str, TransportConfig] = {}
    for kind, conf in (raw.get("transports") or {}).items():
        if isinstance(conf, dict):
            transports[str(kind)] = TransportConfig.from_dict(str(kind), conf)
    return BridgeConfig(
        manifest=manifest, transports=transports, source=str(path), errors=errors
    )


def load_bridges(config_dir: Path | str | None = None) -> list[BridgeConfig]:
    """扫描 config/bridges/*.json。目录不存在返回空列表，不报错。"""
    directory = _bridge_dir(Path(config_dir) if config_dir else None)
    if not directory.is_dir():
        return []
    out: list[BridgeConfig] = []
    for path in sorted(directory.glob("*.json")):
        conf = _parse_bridge_file(path)
        if conf is not None:
            out.append(conf)
    return out


def load_config(config_dir: Path | str | None = None) -> IntegrationsConfig:
    """加载框架配置 + 所有 Bridge 配置。失败一律 fail-soft 到默认值。"""
    base = Path(config_dir) if config_dir else None
    section = _load_yaml_section(base)
    cfg = IntegrationsConfig(
        enabled=bool(section.get("enabled", False)),
        auto_discovery=bool(section.get("auto_discovery", True)),
        health_check_interval_sec=float(section.get("health_check_interval_sec") or 15.0),
        default_timeout_sec=float(section.get("default_timeout_sec") or 30.0),
        max_concurrent_operations=int(section.get("max_concurrent_operations") or 10),
    )
    cfg.bridges = [b for b in load_bridges(base)]
    return cfg
