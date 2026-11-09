"""Scheduler — 高级事件调度器。

规范 PART IX：
    Scheduler：定时任务、系统事件、文件事件、应用事件、网络事件。
    主动行为必须基于用户授权。

定位：
    Scheduler 是 EventEngine 的上层封装，提供：
    1. 5 类触发器（scheduled/system/file/app/network）的统一调度
    2. 高级 API（add_cron_rule / add_interval_rule / add_one_shot_rule 等）
    3. 触发器状态管理（ctx 缓存、next_run 推进、one-shot 自动禁用）
    4. 与现有 EventEngine 共存：定时类规则可同时委托给 EventEngine

不破坏现有 EventEngine：
    - EventEngine 继续作为"轻量定时调度器"使用（router.py 已用）
    - Scheduler 独立轮询线程，自己维护规则副本
    - 若传入 event_engine，定时类规则会同步注册到 EventEngine（双写）

线程模型：
    主线程 → Scheduler.start() → 后台 poll 线程
    poll 线程：遍历规则 → 找触发器 → check → 触发则调 handler
    handler 在 poll 线程中执行（应保持短小，长任务应投递到 TaskEngine）
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from datetime import datetime
from typing import Any, Callable

from .engine import EventEngine, EventRule
from .triggers import Trigger, get_trigger, list_trigger_types

logger = logging.getLogger(__name__)

# 默认值集中管理
_DEFAULT_POLL_INTERVAL_SEC = 15.0
_MIN_POLL_INTERVAL_SEC = 5.0
_DEFAULT_HANDLER_TIMEOUT_SEC = 30.0


class Scheduler:
    """高级事件调度器。

    用法：
        sched = Scheduler(poll_interval_sec=15)
        sched.register_handler("system", lambda rule: print("系统告警", rule))
        sched.add_system_rule(metric="cpu_high", threshold=90, action={"func": "alert"})
        sched.start()
        ...
        sched.stop()
    """

    def __init__(
        self,
        *,
        poll_interval_sec: float = _DEFAULT_POLL_INTERVAL_SEC,
        event_engine: EventEngine | None = None,
        custom_triggers: dict[str, Trigger] | None = None,
    ) -> None:
        self._poll_interval = max(
            _MIN_POLL_INTERVAL_SEC, float(poll_interval_sec or _DEFAULT_POLL_INTERVAL_SEC)
        )
        self._event_engine = event_engine
        self._rules: dict[str, EventRule] = {}
        self._handlers: dict[str, Callable[[EventRule], None]] = {}
        self._custom_triggers: dict[str, Trigger] = dict(custom_triggers or {})
        self._ctx: dict[str, Any] = {}  # 触发器共享上下文（缓存等）
        self._running = False
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # 属性
    # ------------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._running

    @property
    def rule_count(self) -> int:
        return len(self._rules)

    @property
    def poll_interval_sec(self) -> float:
        return self._poll_interval

    # ------------------------------------------------------------------
    # handler 注册
    # ------------------------------------------------------------------

    def register_handler(
        self, event_type: str, handler: Callable[[EventRule], None]
    ) -> None:
        """为指定 event_type 注册 handler。后注册的覆盖前者。"""
        self._handlers[(event_type or "").strip().lower()] = handler

    # ------------------------------------------------------------------
    # 规则管理（通用）
    # ------------------------------------------------------------------

    def add_rule(
        self,
        event_type: str,
        condition: dict,
        action: dict,
        *,
        enabled: bool = True,
        rule_id: str | None = None,
    ) -> str:
        """添加一条规则，返回 rule_id。

        对于 scheduled 类型，若提供了 event_engine，会同步注册到 EventEngine。
        """
        event_type = (event_type or "").strip().lower()
        rid = rule_id or uuid.uuid4().hex[:12]
        rule = EventRule(
            id=rid,
            event_type=event_type,
            condition=dict(condition or {}),
            action=dict(action or {}),
            enabled=bool(enabled),
        )
        with self._lock:
            self._rules[rid] = rule

        # 同步到 EventEngine（仅 scheduled 类型，保持向后兼容）
        if event_type == "scheduled" and self._event_engine is not None:
            try:
                # EventEngine.add_rule 会生成自己的 id，这里用 condition 透传
                self._event_engine.add_rule(
                    event_type,
                    condition,
                    action,
                    enabled=enabled,
                )
            except Exception:
                logger.debug("sync to EventEngine failed", exc_info=True)

        return rid

    def remove_rule(self, rule_id: str) -> bool:
        """删除规则。"""
        with self._lock:
            return self._rules.pop(rule_id, None) is not None

    def enable_rule(self, rule_id: str) -> bool:
        with self._lock:
            rule = self._rules.get(rule_id)
            if rule:
                rule.enabled = True
                return True
            return False

    def disable_rule(self, rule_id: str) -> bool:
        with self._lock:
            rule = self._rules.get(rule_id)
            if rule:
                rule.enabled = False
                return True
            return False

    def list_rules(self) -> list[dict]:
        """列出所有规则的快照。"""
        with self._lock:
            return [
                {
                    "id": r.id,
                    "event_type": r.event_type,
                    "condition": dict(r.condition),
                    "action": dict(r.action),
                    "enabled": r.enabled,
                    "last_run": r.last_run,
                    "next_run": r.next_run,
                }
                for r in self._rules.values()
            ]

    # ------------------------------------------------------------------
    # 便捷 API — 定时
    # ------------------------------------------------------------------

    def add_one_shot_rule(
        self, when: datetime, action: dict, *, enabled: bool = True
    ) -> str:
        """一次性定时规则：到指定时间触发一次后自动禁用。"""
        return self.add_rule(
            "scheduled",
            {"mode": "one_shot", "at": when.isoformat()},
            action,
            enabled=enabled,
        )

    def add_interval_rule(
        self,
        interval_sec: int,
        action: dict,
        *,
        enabled: bool = True,
    ) -> str:
        """间隔触发规则：每 interval_sec 秒触发一次。"""
        return self.add_rule(
            "scheduled",
            {"mode": "interval", "interval_sec": int(interval_sec)},
            action,
            enabled=enabled,
        )

    def add_daily_rule(
        self,
        time_str: str,
        action: dict,
        *,
        enabled: bool = True,
    ) -> str:
        """每日定时规则：每天 time_str (HH:MM) 触发。"""
        return self.add_rule(
            "scheduled",
            {"mode": "daily", "time": time_str},
            action,
            enabled=enabled,
        )

    # ------------------------------------------------------------------
    # 便捷 API — 系统
    # ------------------------------------------------------------------

    def add_system_rule(
        self,
        metric: str,
        action: dict,
        *,
        threshold: float | None = None,
        drive: str | None = None,
        enabled: bool = True,
    ) -> str:
        """系统事件规则。

        metric: cpu_high / memory_high / disk_low / on_battery / on_ac
        """
        cond: dict = {"metric": metric}
        if threshold is not None:
            cond["threshold"] = threshold
        if drive is not None:
            cond["drive"] = drive
        return self.add_rule("system", cond, action, enabled=enabled)

    # ------------------------------------------------------------------
    # 便捷 API — 文件
    # ------------------------------------------------------------------

    def add_file_rule(
        self,
        path: str,
        event: str,
        action: dict,
        *,
        enabled: bool = True,
    ) -> str:
        """文件事件规则。

        event: exists / not_exists / modified / size_changed
        """
        return self.add_rule(
            "file",
            {"path": path, "event": event},
            action,
            enabled=enabled,
        )

    # ------------------------------------------------------------------
    # 便捷 API — 应用
    # ------------------------------------------------------------------

    def add_app_rule(
        self,
        process: str,
        event: str,
        action: dict,
        *,
        enabled: bool = True,
    ) -> str:
        """应用事件规则。

        event: running / not_running / started / stopped
        """
        return self.add_rule(
            "app",
            {"process": process, "event": event},
            action,
            enabled=enabled,
        )

    # ------------------------------------------------------------------
    # 便捷 API — 网络
    # ------------------------------------------------------------------

    def add_network_rule(
        self,
        event: str,
        action: dict,
        *,
        host: str | None = None,
        port: int | None = None,
        timeout_sec: float | None = None,
        enabled: bool = True,
    ) -> str:
        """网络事件规则。

        event: host_reachable / host_unreachable / ip_changed /
               wifi_connected / wifi_disconnected
        """
        cond: dict = {"event": event}
        if host is not None:
            cond["host"] = host
        if port is not None:
            cond["port"] = port
        if timeout_sec is not None:
            cond["timeout_sec"] = timeout_sec
        return self.add_rule("network", cond, action, enabled=enabled)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._loop, name="kekesi-scheduler", daemon=True
        )
        self._thread.start()
        logger.info(
            "Scheduler 已启动（规则 %d 条，轮询 %.1fs）",
            len(self._rules),
            self._poll_interval,
        )

    def stop(self) -> None:
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._thread = None
        logger.info("Scheduler 已停止")

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        while self._running:
            try:
                self._tick()
            except Exception:
                logger.error("scheduler tick 异常", exc_info=True)
            time.sleep(self._poll_interval)

    def _tick(self) -> None:
        """单次轮询：检查所有规则。"""
        now = datetime.now()
        # 复制一份规则快照，避免 handler 中修改导致迭代问题
        with self._lock:
            rules_snapshot = list(self._rules.values())

        for rule in rules_snapshot:
            if not self._running:
                break
            if not rule.enabled:
                continue

            trigger = self._get_trigger(rule.event_type)
            if trigger is None:
                continue

            try:
                fired, reason = trigger.check(rule, self._ctx)
            except Exception:
                logger.debug(
                    "trigger.check 异常 rule=%s type=%s",
                    rule.id,
                    rule.event_type,
                    exc_info=True,
                )
                continue

            if not fired:
                continue

            # 触发动作
            handler = self._handlers.get(rule.event_type)
            if handler is None:
                logger.debug(
                    "无 handler for type=%s rule=%s", rule.event_type, rule.id
                )
                # 仍更新 last_run，避免反复尝试
                rule.last_run = now.isoformat()
                self._advance_scheduled(rule)
                continue

            try:
                handler(rule)
                rule.last_run = now.isoformat()
                logger.info(
                    "scheduler 触发 rule=%s type=%s action=%s",
                    rule.id,
                    rule.event_type,
                    rule.action.get("func") or rule.action.get("tool") or "?",
                )
            except Exception:
                logger.error(
                    "scheduler handler 异常 rule=%s type=%s",
                    rule.id,
                    rule.event_type,
                    exc_info=True,
                )

            # 触发后推进 next_run（仅 scheduled 类型）
            self._advance_scheduled(rule)

    def _get_trigger(self, event_type: str) -> Trigger | None:
        """获取触发器：先查自定义，再查内置。"""
        et = (event_type or "").strip().lower()
        if et in self._custom_triggers:
            return self._custom_triggers[et]
        return get_trigger(et)

    @staticmethod
    def _advance_scheduled(rule: EventRule) -> None:
        """触发后推进 scheduled 类型的 next_run。"""
        if rule.event_type != "scheduled":
            return
        try:
            from .triggers import ScheduledTrigger
            ScheduledTrigger.advance_after_fire(rule)
        except Exception:
            logger.debug("advance_scheduled failed", exc_info=True)
