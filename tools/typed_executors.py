# -*- coding: utf-8 -*-
"""Typed Executors — 工具参数的类型化契约与集中校验层。

将散落在 execute_tool 各分支的 str()/parse_bool()/int() try-except 收口为
统一的运行时校验器：在 execute_tool 入口（normalize_tool_params 之后、
分发之前）调用 validate_and_coerce，校验通过则用强类型 dict 替换原 params，
失败则直接返回 ToolOutput(False, ...) 不进 handler，让 LLM 据错误信息纠正。

设计原则：
- 零硬编码：spec 是数据（ParamSpec 列表），不是代码分支
- 向后兼容：未注册 spec 的工具直接 passthrough，行为不变
- 健壮性：coerce 失败返回清晰错误，绝不抛异常
- 复用：布尔转换沿用 param_normalize.parse_bool 的语义
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .param_normalize import parse_bool

logger = logging.getLogger(__name__)

__all__ = ["ParamSpec", "validate_and_coerce"]


@dataclass(frozen=True)
class ParamSpec:
    """单个工具参数的类型契约。"""

    name: str
    type: str = "string"  # string | boolean | integer | number
    required: bool = False
    default: Any = None
    max_len: int | None = None  # 字符串最大长度（截断而非报错，防下游崩）
    min_val: float | None = None  # 数值下限（clamp）
    max_val: float | None = None  # 数值上限（clamp）
    enum: tuple[str, ...] | None = None  # 枚举值，越界报错


def _coerce_bool(v: Any) -> tuple[bool, bool]:
    """返回 (ok, value)；ok=False 表示无法判定为布尔。"""
    if isinstance(v, bool):
        return True, v
    if isinstance(v, (int, float)):
        return True, bool(v)
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("true", "1", "yes", "on", "是", "对"):
            return True, True
        if s in ("false", "0", "no", "off", "否", "不"):
            return True, False
    return False, False


def _is_blank(v: Any) -> bool:
    if v is None:
        return True
    if isinstance(v, str) and not v.strip():
        return True
    return False


def _get_tool_specs() -> dict[str, list[ParamSpec]]:
    """延迟导入，避免 typed_executors ↔ _tool_specs 循环依赖。"""
    from ._tool_specs import TOOL_SPECS
    return TOOL_SPECS


def validate_and_coerce(
    name: str, params: dict | None
) -> tuple[bool, dict, str]:
    """按 TOOL_SPECS[name] 校验并强类型转换 params。

    返回 (ok, coerced_params, error_msg)：
    - ok=True  ：coerced_params 为强类型 dict（含默认值填充），未知字段保留
    - ok=False ：coerced_params 为空，error_msg 给 LLM 可读的纠正提示
    - 工具未注册 spec ：passthrough，返回原 params
    """
    specs = _get_tool_specs()
    spec = specs.get(name)
    if spec is None:
        return True, dict(params or {}), ""

    src = params or {}
    out: dict[str, Any] = {}

    for p in spec:
        v = src.get(p.name)
        if _is_blank(v):
            if p.required:
                return False, {}, f"缺少必填参数「{p.name}」"
            out[p.name] = p.default
            continue

        if p.type == "boolean":
            ok, val = _coerce_bool(v)
            if not ok:
                return (
                    False,
                    {},
                    f"参数「{p.name}」应为布尔值（true/false），"
                    f"收到 {v!r}",
                )
            coerced: Any = val
        elif p.type == "integer":
            try:
                coerced = int(str(v).strip())
            except (TypeError, ValueError):
                return (
                    False,
                    {},
                    f"参数「{p.name}」应为整数，收到 {v!r}",
                )
        elif p.type == "number":
            try:
                coerced = float(str(v).strip())
            except (TypeError, ValueError):
                return (
                    False,
                    {},
                    f"参数「{p.name}」应为数值，收到 {v!r}",
                )
        else:  # string
            # max_len=None 表示内容类参数（代码/文档正文），不做 strip 以保留格式
            coerced = str(v) if p.max_len is None else str(v).strip()
            if p.max_len is not None and len(coerced) > p.max_len:
                coerced = coerced[: p.max_len]
                logger.info(
                    "工具 %s 参数 %s 超长截断至 %d 字符",
                    name,
                    p.name,
                    p.max_len,
                )

        if p.enum is not None and coerced not in p.enum:
            return (
                False,
                {},
                f"参数「{p.name}」取值 {coerced!r} 不在允许范围"
                f"（{', '.join(p.enum)}）",
            )

        if p.min_val is not None and isinstance(coerced, (int, float)):
            if coerced < p.min_val:
                coerced = type(coerced)(p.min_val)
        if p.max_val is not None and isinstance(coerced, (int, float)):
            if coerced > p.max_val:
                coerced = type(coerced)(p.max_val)

        out[p.name] = coerced

    # 未知字段保留：LLM 偶尔多传字段，不报错，交给 handler 自行忽略
    for k, v in src.items():
        if k not in out:
            out[k] = v

    return True, out, ""
