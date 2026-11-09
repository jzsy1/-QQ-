"""Trace + Span 上下文管理器 — 基于 contextvars 跨阶段串联 trace_id。

设计要点：
- _current_trace / _current_span 用 contextvars.ContextVar，子线程/异步任务自动隔离
- start_trace 在 AgentRouter.handle 入口调用；span 在关键阶段包裹
- Span 在 __exit__ 时调 store.save_span，Trace 在 __exit__ 时调 store.save_trace
- fail-soft：store 调用失败只 log.debug，不影响主流程
- 性能：span 内存中累积 attributes/events，__exit__ 一次性写库
"""

from __future__ import annotations

import contextvars
import logging
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Iterator

if TYPE_CHECKING:
    from .store import TraceStore

logger = logging.getLogger(__name__)

# 上下文变量：跨函数/跨 await 传递，但子线程/asyncio.Task 自动 copy（不互相干扰）
_current_trace: contextvars.ContextVar["Trace | None"] = contextvars.ContextVar(
    "kekesi_trace", default=None
)
_current_span: contextvars.ContextVar["Span | None"] = contextvars.ContextVar(
    "kekesi_span", default=None
)


def _now_iso() -> str:
    return datetime.now().isoformat()


def _short_id(value: str | None) -> str:
    """trace_id / span_id 取前 8 位用于日志前缀。"""
    if not value:
        return "-"
    return value[:8]


@dataclass
class Span:
    """trace 内的一个阶段（如 planner.create / executor.execute:open_app）。"""

    name: str
    trace_id: str
    span_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    parent_span_id: str = ""
    started_at: float = field(default_factory=time.monotonic)
    started_at_iso: str = field(default_factory=_now_iso)
    finished_at: float | None = None
    finished_at_iso: str = ""
    duration_ms: int = 0
    status: str = "success"
    attributes: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    events: list[dict[str, Any]] = field(default_factory=list)
    _store: "TraceStore | None" = field(default=None, init=False, repr=False)
    _token: contextvars.Token | None = field(default=None, init=False, repr=False)

    def set_attribute(self, key: str, value: Any) -> None:
        """记录 span 属性（如 tool=success, duration_ms=123）。"""
        try:
            self.attributes[key] = value
        except Exception:
            pass

    def add_event(self, name: str, payload: dict | None = None) -> None:
        """记录 span 内事件（如 "retry attempt 1"）。"""
        try:
            self.events.append(
                {
                    "name": name,
                    "at": _now_iso(),
                    "payload": payload or {},
                }
            )
        except Exception:
            pass

    def set_error(self, error: str | Exception) -> None:
        """标记 span 失败并记录错误原因。"""
        self.status = "failed"
        if isinstance(error, BaseException):
            self.error = f"{type(error).__name__}: {error}"
        else:
            self.error = str(error or "")

    def _finish(self, status: str = "success", error: str = "") -> None:
        if self.finished_at is not None:
            return  # 防止重复结束
        self.finished_at = time.monotonic()
        self.finished_at_iso = _now_iso()
        self.duration_ms = int((self.finished_at - self.started_at) * 1000)
        if status and status != "success":
            self.status = status
        if error:
            self.error = str(error)
        # fail-soft 写库
        if self._store is not None:
            try:
                self._store.save_span(self)
            except Exception:
                logger.debug(
                    "Span store.save_span failed: %s", self.span_id, exc_info=True
                )


@dataclass
class Trace:
    """一次用户请求的全链路（从开始说话到最终回复）。"""

    user_text: str
    agent_id: str = "keks"
    session_id: str = "default"
    trace_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    task_id: str = ""
    started_at: float = field(default_factory=time.monotonic)
    started_at_iso: str = field(default_factory=_now_iso)
    finished_at: float | None = None
    finished_at_iso: str = ""
    duration_ms: int = 0
    status: str = "running"
    reply: str = ""
    error: str = ""
    layer: str = ""
    span_count: int = 0
    user_text_max: int = field(default=500, init=False, repr=False)
    reply_max: int = field(default=500, init=False, repr=False)
    _store: "TraceStore | None" = field(default=None, init=False, repr=False)
    _token: contextvars.Token | None = field(default=None, init=False, repr=False)
    _span_counter: int = field(default=0, init=False, repr=False)

    def finish(
        self,
        status: str = "success",
        *,
        reply: str = "",
        error: str = "",
        layer: str = "",
        task_id: str = "",
    ) -> None:
        """结束 trace 并持久化。"""
        if self.finished_at is not None:
            return
        self.finished_at = time.monotonic()
        self.finished_at_iso = _now_iso()
        self.duration_ms = int((self.finished_at - self.started_at) * 1000)
        self.status = status
        if reply:
            self.reply = reply
        if error:
            self.error = error
        if layer:
            self.layer = layer
        if task_id:
            self.task_id = task_id
        # fail-soft 写库
        if self._store is not None:
            try:
                self._store.save_trace(self)
            except Exception:
                logger.debug(
                    "Trace store.save_trace failed: %s",
                    self.trace_id,
                    exc_info=True,
                )

    def _incr_span(self) -> None:
        self._span_counter += 1
        self.span_count = self._span_counter


@contextmanager
def start_trace(
    user_text: str,
    *,
    agent_id: str = "keks",
    session_id: str = "default",
    task_id: str = "",
    store: "TraceStore | None" = None,
    user_text_max: int = 500,
    reply_max: int = 500,
) -> Iterator[Trace]:
    """在 AgentRouter.handle 入口开启一个 trace。

    用法：
        with start_trace(text, agent_id=..., session_id=..., store=self.trace_store) as t:
            reply, layer = do_work()
            t.finish("success", reply=reply, layer=layer)

    若 store 为 None（trace 禁用或测试模式），仍维持 contextvars 上下文，
    但不持久化；current_trace_id() 仍可访问。
    """
    trace = Trace(
        user_text=user_text,
        agent_id=agent_id,
        session_id=session_id,
        task_id=task_id,
    )
    trace._store = store
    trace.user_text_max = user_text_max
    trace.reply_max = reply_max
    # 立即写一条 running 状态的 trace，便于外部查询"正在跑的请求"
    if store is not None:
        try:
            store.save_trace(trace)
        except Exception:
            logger.debug("start_trace initial save failed", exc_info=True)
    token = _current_trace.set(trace)
    trace._token = token
    try:
        yield trace
    finally:
        # 若用户没显式 finish，自动以 running→unknown 状态收尾
        if trace.finished_at is None:
            trace.finish(status="unknown")
        _current_trace.reset(token)


@contextmanager
def span(name: str, **attributes) -> Iterator[Span]:
    """在关键阶段包裹，自动继承当前 trace_id 和 parent_span_id。

    用法：
        with span("planner.create") as s:
            plan = self.planner.create(task)
            s.set_attribute("actions_count", len(plan.actions))

    若当前无活跃 trace，仍然创建一个 span 对象（便于代码无需 if 判断），
    但不持久化；attributes 仍可记录在内存中。
    """
    parent = _current_span.get()
    parent_id = parent.span_id if parent else ""
    trace = _current_trace.get()
    trace_id = trace.trace_id if trace else ""
    store = trace._store if trace else None

    s = Span(
        name=name,
        trace_id=trace_id,
        parent_span_id=parent_id,
        attributes=dict(attributes) if attributes else {},
    )
    s._store = store
    if trace is not None:
        trace._incr_span()
    token = _current_span.set(s)
    s._token = token
    try:
        yield s
    except BaseException as exc:
        # 异常时标记 span 失败，但继续向上抛
        s._finish(status="failed", error=f"{type(exc).__name__}: {exc}")
        _current_span.reset(token)
        raise
    else:
        s._finish(status="success")
        _current_span.reset(token)


def current_trace() -> Trace | None:
    """获取当前 context 的活跃 trace（用于 span 内访问 trace 属性）。"""
    return _current_trace.get()


def current_trace_id() -> str | None:
    """获取当前 trace_id（用于日志前缀注入）。"""
    t = _current_trace.get()
    return t.trace_id if t else None


def current_span() -> Span | None:
    """获取当前活跃 span。"""
    return _current_span.get()


def current_span_id() -> str | None:
    """获取当前 span_id（用于日志前缀注入）。"""
    s = _current_span.get()
    return s.span_id if s else None
