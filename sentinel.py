"""Sentinel — 通用自主触发框架（数据驱动，场景不写死在代码里）。

设计原则：
- 代码只提供「条件类型」原语（空闲/CPU/内存/磁盘），具体规则全部存库；
- 规则由用户口头创建（LLM 调 add_watch_rule），可列出、可删除；
- 触发后先 TTS 询问用户；等待期内用户有任何语音活动 → 取消默认动作，
  交回正常对话流程；超时无回应 → 执行规则里的默认动作（创建时即视为预授权）；
- 触发后 disarm，条件复位（不再满足）后才重新武装，避免连环轰炸。

示例（用户口头教）：
  「以后电脑空闲超过30分钟，就问我要不要关机，一分钟没回答就直接关」
  → add_watch_rule(condition_type=idle_minutes, threshold=30,
                   question=..., action_func=system_power,
                   action_args={"action":"shutdown"}, timeout_sec=60)
"""

from __future__ import annotations

import json
import logging
import threading
import time

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sentinel_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    description TEXT NOT NULL DEFAULT '',
    condition_type TEXT NOT NULL,
    threshold REAL NOT NULL DEFAULT 0,
    question TEXT NOT NULL DEFAULT '',
    action_func TEXT DEFAULT '',
    action_args TEXT DEFAULT '{}',
    timeout_sec INTEGER DEFAULT 60,
    enabled INTEGER DEFAULT 1,
    last_fired TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
"""

# 支持的条件类型（通用原语，新场景=新规则数据，不改代码）
CONDITION_TYPES = {
    "idle_minutes": "键鼠空闲达到 N 分钟",
    "cpu_above": "CPU 使用率超过 N%",
    "memory_above": "内存占用超过 N%",
    "disk_free_below_gb": "C盘剩余空间低于 N GB",
    "cpu_temp_above": "CPU 温度超过 N℃",
    "gpu_temp_above": "GPU 温度超过 N℃",
    "temp_above": "任一硬件温度超过 N℃",
}


def _system_metrics() -> dict:
    """采一次系统指标（复用 winops 健康检查）。"""
    try:
        from .tools import winops

        ok, raw = winops.check_system_health()
        if ok:
            data = json.loads(raw)
            return {
                "cpu_above": float(data.get("cpu_pct") or 0),
                "memory_above": float(data.get("memory_pct") or 0),
            }
    except Exception:
        pass
    return {}


def _thermal_metrics() -> dict:
    """采温度指标供哨兵规则使用。"""
    try:
        from .tools.hardware_monitor import collect_temperatures, _max_by_kind

        temps = collect_temperatures()
        by = _max_by_kind(temps)
        out: dict = {}
        if "cpu" in by:
            out["cpu_temp_above"] = float(by["cpu"])
        if "gpu" in by:
            out["gpu_temp_above"] = float(by["gpu"])
        if "any" in by:
            out["temp_above"] = float(by["any"])
        return out
    except Exception:
        return {}


def _disk_free_gb() -> float | None:
    try:
        import shutil

        usage = shutil.disk_usage("C:\\")
        return usage.free / (1024 ** 3)
    except Exception:
        return None


class Sentinel:
    def __init__(
        self,
        db,
        *,
        execute_fn=None,
        interval_sec: int = 30,
        observer=None,
    ) -> None:
        """execute_fn(func, args, preauth_text) -> (ok, msg)；由 router 注入。"""
        self.db = db
        self._execute_fn = execute_fn
        self._observer = observer
        self._interval = max(10, interval_sec)
        self._lock = threading.Lock()
        self._disarmed: set[int] = set()          # 已触发、等条件复位
        self._last_user_activity = time.time()     # 语音/对话活动（非键鼠）
        self._pending_timer: threading.Timer | None = None
        self._pending_rule_id: int | None = None
        self._ensure_schema()
        self._stop = threading.Event()
        threading.Thread(target=self._loop, name="sentinel", daemon=True).start()

    def _ensure_schema(self) -> None:
        with self.db.connect() as conn:
            conn.executescript(_SCHEMA)
            conn.commit()

    # ── 外部接线 ──────────────────────────────────────────────

    def notify_user_activity(self) -> None:
        """每次用户说话/下指令时调用：取消等待中的默认动作。"""
        self._last_user_activity = time.time()
        with self._lock:
            if self._pending_timer is not None:
                self._pending_timer.cancel()
                self._pending_timer = None
                rid = self._pending_rule_id
                self._pending_rule_id = None
                logger.info("[Sentinel] 用户已回应，取消规则#%s 的默认动作", rid)

    # ── 规则管理（供 LLM 工具调用） ───────────────────────────

    def add_rule(
        self,
        condition_type: str,
        threshold: float,
        question: str,
        *,
        description: str = "",
        action_func: str = "",
        action_args: dict | None = None,
        timeout_sec: int = 60,
    ) -> tuple[bool, str]:
        ct = (condition_type or "").strip()
        if ct not in CONDITION_TYPES:
            return False, (
                f"不支持的条件类型「{ct}」。可用："
                + "；".join(f"{k}（{v}）" for k, v in CONDITION_TYPES.items())
            )
        if threshold <= 0:
            return False, "threshold 须大于 0"
        q = (question or "").strip()
        if not q:
            return False, "请提供触发时要向用户播报的问题"
        with self.db.connect() as conn:
            cur = conn.execute(
                """INSERT INTO sentinel_rules
                   (description, condition_type, threshold, question,
                    action_func, action_args, timeout_sec)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    (description or q)[:120],
                    ct,
                    float(threshold),
                    q[:200],
                    (action_func or "").strip(),
                    json.dumps(action_args or {}, ensure_ascii=False),
                    max(0, int(timeout_sec)),
                ),
            )
            conn.commit()
            rid = cur.lastrowid
        act = f"，超时默认执行 {action_func}" if action_func else "（仅提醒，不自动执行）"
        return True, f"已创建监控规则#{rid}：{CONDITION_TYPES[ct]}→询问{act}"

    def list_rules(self) -> tuple[bool, str]:
        with self.db.connect() as conn:
            rows = conn.execute(
                """SELECT id, description, condition_type, threshold,
                          action_func, timeout_sec, enabled
                   FROM sentinel_rules ORDER BY id"""
            ).fetchall()
        if not rows:
            return True, "当前没有监控规则。"
        lines = []
        for r in rows:
            state = "启用" if r["enabled"] else "停用"
            act = r["action_func"] or "仅提醒"
            lines.append(
                f"#{r['id']} {r['description']}｜{r['condition_type']}≥{r['threshold']:g}"
                f"｜{act}｜{state}"
            )
        return True, "监控规则：\n" + "\n".join(lines)

    def delete_rule(self, rule_id: int) -> tuple[bool, str]:
        with self.db.connect() as conn:
            cur = conn.execute(
                "DELETE FROM sentinel_rules WHERE id = ?", (int(rule_id),)
            )
            conn.commit()
        if cur.rowcount:
            return True, f"已删除监控规则#{rule_id}"
        return False, f"没找到规则#{rule_id}"

    # ── 巡检 ─────────────────────────────────────────────────

    def _loop(self) -> None:
        self._stop.wait(60)
        while not self._stop.is_set():
            try:
                self._scan()
            except Exception:
                logger.debug("[Sentinel] 巡检异常", exc_info=True)
            self._stop.wait(self._interval)

    def _current_value(self, ct: str, metrics: dict) -> float | None:
        if ct == "idle_minutes":
            from .safety.activity_guard import seconds_since_last_input

            sec = seconds_since_last_input()
            return None if sec is None else sec / 60.0
        if ct in ("cpu_above", "memory_above"):
            return metrics.get(ct)
        if ct in ("cpu_temp_above", "gpu_temp_above", "temp_above"):
            return metrics.get(ct)
        if ct == "disk_free_below_gb":
            return _disk_free_gb()
        return None

    def _condition_met(self, ct: str, value: float, threshold: float) -> bool:
        if ct == "disk_free_below_gb":
            return value < threshold
        return value >= threshold

    def _scan(self) -> None:
        with self._lock:
            if self._pending_timer is not None:
                return  # 有等待中的询问，不叠加
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM sentinel_rules WHERE enabled = 1"
            ).fetchall()
        if not rows:
            return

        needs_metrics = any(
            r["condition_type"] in ("cpu_above", "memory_above") for r in rows
        )
        needs_thermal = any(
            r["condition_type"] in ("cpu_temp_above", "gpu_temp_above", "temp_above")
            for r in rows
        )
        metrics = _system_metrics() if needs_metrics else {}
        if needs_thermal:
            metrics.update(_thermal_metrics())

        for r in rows:
            rid = int(r["id"])
            ct = r["condition_type"]
            value = self._current_value(ct, metrics)
            if value is None:
                continue
            met = self._condition_met(ct, value, float(r["threshold"]))
            if not met:
                self._disarmed.discard(rid)  # 条件复位 → 重新武装
                continue
            if rid in self._disarmed:
                continue
            self._fire(dict(r), value)
            return  # 一次只触发一条

    def _fire(self, rule: dict, value: float) -> None:
        rid = int(rule["id"])
        self._disarmed.add(rid)
        question = rule["question"]
        try:
            from .notifications import has_reminder_callbacks, notify_reminder

            if has_reminder_callbacks():
                notify_reminder(question)
            else:
                logger.info("[Sentinel] 无 TTS 回调，仅记录: %s", question)
        except Exception:
            logger.debug("[Sentinel] 播报失败", exc_info=True)

        with self.db.connect() as conn:
            conn.execute(
                "UPDATE sentinel_rules SET last_fired = datetime('now') WHERE id = ?",
                (rid,),
            )
            conn.commit()
        logger.info("[Sentinel] 规则#%s 触发（当前值 %.1f）：%s", rid, value, question)

        action_func = (rule.get("action_func") or "").strip()
        if self._observer:
            try:
                self._observer.on_sentinel_fired(rid, question, action_func)
            except Exception:
                logger.debug("[Sentinel] observer 回调失败", exc_info=True)

        timeout = int(rule.get("timeout_sec") or 0)
        if not action_func or timeout <= 0:
            return  # 仅提醒

        try:
            args = json.loads(rule.get("action_args") or "{}")
        except json.JSONDecodeError:
            args = {}

        def _timeout_execute() -> None:
            with self._lock:
                self._pending_timer = None
                self._pending_rule_id = None
            if not self._execute_fn:
                return
            try:
                # 规则由用户创建即视为预授权：router._build_execute_fn 会用
                # pre_authorized=True 显式放行，这里不再需要拼"确认执行"
                # 这几个字去匹配确认词——那样做等于让任何包含这几个字的文本
                # 都能瞒过闸门，语义上也把"创建规则时的预授权"和"当场口头确认"
                # 混成了同一件事。
                ok, msg = self._execute_fn(
                    action_func, args, f"监控规则#{rid}超时默认动作"
                )
                logger.info("[Sentinel] 规则#%s 超时默认动作 %s: %s", rid, action_func, msg)
                try:
                    from .notifications import notify_reminder

                    notify_reminder(
                        f"{'好的，' if ok else '抱歉，'}{msg[:80]}"
                    )
                except Exception:
                    pass
            except Exception:
                logger.exception("[Sentinel] 默认动作执行失败")

        with self._lock:
            self._pending_rule_id = rid
            self._pending_timer = threading.Timer(timeout, _timeout_execute)
            self._pending_timer.daemon = True
            self._pending_timer.start()
        logger.info(
            "[Sentinel] 等待用户回应 %ss，无回应将执行 %s", timeout, action_func
        )

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            if self._pending_timer:
                self._pending_timer.cancel()
