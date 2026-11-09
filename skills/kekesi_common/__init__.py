# -*- coding: utf-8 -*-
"""Kekesi 通用大脑核心能力（QQ / 直播接入层共用）。

这里只放与具体业务无关的算法与接口：
- reply_text / reply_engine：回复清洗与生成管线
- llm：独立 LLM 调用（SimpleBrain）与配置
- personas：人设与系统提示词管理
- companion_memory / memory_algo / memory_db：记忆算法与命名空间
- namespaces：记忆命名空间与物理隔离
"""

from __future__ import annotations

from . import (
    companion_memory,
    llm,
    memory_algo,
    memory_db,
    migrate_memory,
    namespaces,
    personas,
    reply_engine,
    reply_text,
)

__all__ = [
    "companion_memory",
    "llm",
    "memory_algo",
    "memory_db",
    "migrate_memory",
    "namespaces",
    "personas",
    "reply_engine",
    "reply_text",
]
