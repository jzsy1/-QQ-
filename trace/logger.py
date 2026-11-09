"""TraceLoggerAdapter — 自动注入 [trace=abcd1234 span=abcd1234] 前缀。

设计要点：
- 继承 logging.LoggerAdapter，process() 在每条日志前加前缀
- 通过 contextvars 读取当前 trace_id / span_id，无需显式传参
- 若当前无活跃 trace，前缀为 [trace=-]，保持日志格式一致
- 现有 logging.getLogger 调用渐进替换为 get_trace_logger，侵入点优先
"""

from __future__ import annotations

import logging

from .context import current_span_id, current_trace_id


class TraceLoggerAdapter(logging.LoggerAdapter):
    """LoggerAdapter：每条日志前自动注入 trace_id / span_id 前缀。

    用法：
        logger = get_trace_logger(__name__)
        logger.info("task.action tool=%s", tool_name)

    输出形如：
        [trace=abcd1234 span=ef567890] task.action tool=open_app
    """

    def process(self, msg, kwargs):
        tid = current_trace_id()
        sid = current_span_id()
        if tid:
            tid_short = tid[:8]
        else:
            tid_short = "-"
        if sid:
            sid_short = sid[:8]
            prefix = f"[trace={tid_short} span={sid_short}] "
        else:
            prefix = f"[trace={tid_short}] "
        return prefix + str(msg), kwargs


# 模块级缓存：同名 logger 复用同一 adapter，避免重复创建
_logger_cache: dict[str, TraceLoggerAdapter] = {}


def get_trace_logger(name: str) -> TraceLoggerAdapter:
    """获取带 trace_id 注入的 logger。

    与 logging.getLogger(name) 签名兼容，便于渐进替换。
    """
    cached = _logger_cache.get(name)
    if cached is not None:
        return cached
    adapter = TraceLoggerAdapter(logging.getLogger(name), extra={})
    _logger_cache[name] = adapter
    return adapter
