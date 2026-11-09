"""Trace 配置加载 — 从 config.yaml 的 trace section 读取。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TraceConfig:
    """全局追踪系统配置（零硬编码：所有默认值可被 config.yaml 覆盖）。"""

    enabled: bool = True
    store_keep_days: int = 7
    store_max_traces: int = 5000
    log_trace_id: bool = True
    user_text_max_chars: int = 500
    reply_max_chars: int = 500

    @classmethod
    def from_dict(cls, d: dict | None) -> "TraceConfig":
        """从 config.yaml 的 trace section 构造；缺失字段用默认值。"""
        d = d or {}
        if not isinstance(d, dict):
            return cls()
        try:
            return cls(
                enabled=bool(d.get("enabled", True)),
                store_keep_days=int(d.get("store_keep_days", 7)),
                store_max_traces=int(d.get("store_max_traces", 5000)),
                log_trace_id=bool(d.get("log_trace_id", True)),
                user_text_max_chars=int(d.get("user_text_max_chars", 500)),
                reply_max_chars=int(d.get("reply_max_chars", 500)),
            )
        except (TypeError, ValueError):
            return cls()
