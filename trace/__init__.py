"""Kekesi Global Trace — 全局运行追踪系统。

任何一次用户请求都能从「开始说话」一路追踪到「最终回复」，
知道经过了什么阶段、每段耗时多少、哪里失败、为什么失败。

公共 API：
    from kekesi.trace import start_trace, span, current_trace_id, get_trace_logger, TraceStore
"""

from __future__ import annotations

from .config import TraceConfig
from .context import (
    Trace,
    Span,
    current_span,
    current_span_id,
    current_trace,
    current_trace_id,
    span,
    start_trace,
)
from .logger import TraceLoggerAdapter, get_trace_logger
from .store import TraceStore

__all__ = [
    "Span",
    "Trace",
    "TraceConfig",
    "TraceLoggerAdapter",
    "TraceStore",
    "current_span",
    "current_span_id",
    "current_trace",
    "current_trace_id",
    "get_trace_logger",
    "span",
    "start_trace",
]
