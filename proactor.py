"""Proactor — 主动建议：习惯时段 + 系统健康；每日上限；可拒绝。

不含全屋/IoT；仅桌面助手场景。
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime

logger = logging.getLogger(__name__)

# 整句拒绝才短路；含「打开/帮我」等任务时交给 LLM
_PURE_DISMISS_RE = re.compile(
    r"^(?:好的?[，,]?)?(?:不用了|别提醒(?:我)?了?|不要提醒|别主动(?:了)?|"
    r"闭嘴|取消建议|别建议(?:了)?|少烦我)[。！!？?\s]*$",
)
_DISMISS_HINT_RE = re.compile(
    r"不用了|别提醒|不要提醒|别主动|取消建议|别建议",
)
_TASK_HINT_RE = re.compile(
    r"打开|关闭|启动|搜索|帮我|执行|点一下|点击|发送|查询|播放|提醒我|设置",
)
_MAX_DAILY = 3
_MIN_CONFIDENCE = 0.7


def _time_bucket(hour: int | None = None) -> str:
    h = datetime.now().hour if hour is None else hour
    if 6 <= h < 12:
        return "morning"
    if 12 <= h < 18:
        return "afternoon"
    if 18 <= h < 22:
        return "evening"
    return "night"


class Proactor:
    def __init__(
        self,
        db=None,
        *,
        interval_sec: int = 120,
        max_daily: int = _MAX_DAILY,
        min_confidence: float = _MIN_CONFIDENCE,
        speak_enabled: bool = True,
        quiet_hours: tuple[int, int] = (23, 8),
    ) -> None:
        self._db = db
        self._interval = max(60, interval_sec)
        self._max_daily = max(1, max_daily)
        self._min_confidence = min_confidence
        self._speak_enabled = speak_enabled
        self._quiet_hours = quiet_hours
        self._lock = threading.Lock()
        self._pending: str | None = None
        self._pending_habit_id: int | None = None
        # take 之后仍保留，供用户说「不用了」关掉对应习惯
        self._last_offered_habit_id: int | None = None
        self._daily_count = 0
        self._daily_date = datetime.now().strftime("%Y-%m-%d")
        self._last_habit_key: str | None = None
        self._last_health_text: str | None = None
        self._spoken_text: str | None = None
        self._spoken_at: float = 0.0
        self._goal_driver = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="proactor", daemon=True)
        self._thread.start()

    def _in_quiet_hours(self) -> bool:
        start, end = self._quiet_hours
        h = datetime.now().hour
        if start <= end:
            return start <= h < end
        return h >= start or h < end

    def _try_speak(self, text: str, habit_id: int | None, key: str | None) -> bool:
        """有 TTS 回调且不在安静时段 → 直接开口播报，计入每日额度。"""
        if not self._speak_enabled or self._in_quiet_hours():
            return False
        try:
            from .notifications import has_reminder_callbacks, notify_reminder

            if not has_reminder_callbacks():
                return False
            with self._lock:
                if self._daily_count >= self._max_daily:
                    return False
                self._daily_count += 1
                if habit_id is not None:
                    self._last_offered_habit_id = habit_id
                if key:
                    self._last_habit_key = key
            notify_reminder(text)
            with self._lock:
                self._spoken_text = text
                self._spoken_at = time.time()
            if habit_id and self._db:
                try:
                    with self._db.connect() as conn:
                        conn.execute(
                            """UPDATE habit_patterns SET
                               hit_count = hit_count + 1,
                               last_suggested = datetime('now')
                               WHERE id = ?""",
                            (habit_id,),
                        )
                        conn.commit()
                except Exception:
                    pass
            logger.info("[Proactor] 已主动播报: %s", text)
            return True
        except Exception:
            logger.debug("Proactor 播报失败", exc_info=True)
            return False

    def bind_db(self, db) -> None:
        self._db = db

    def bind_goal_driver(self, goal_driver) -> None:
        """Host rate-limited autonomous goal scans on the existing Proactor loop."""
        self._goal_driver = goal_driver
        handoff = getattr(goal_driver, "use_external_scheduler", None)
        if callable(handoff):
            handoff()

    def _scan_goals(self) -> None:
        driver = self._goal_driver
        if driver is None:
            return
        try:
            driver.scan_if_due()
        except Exception:
            logger.warning("Proactor 自主目标扫描失败", exc_info=True)

    def _roll_day(self) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        if today != self._daily_date:
            self._daily_date = today
            self._daily_count = 0
            self._last_habit_key = None
            self._last_health_text = None

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self._scan()
            except Exception:
                logger.debug("Proactor 巡检异常", exc_info=True)

    def _scan(self) -> None:
        self._scan_goals()
        with self._lock:
            self._roll_day()
            if self._pending or self._daily_count >= self._max_daily:
                return

        habit = self._pick_habit_suggestion()
        if habit:
            text, habit_id, key = habit
            with self._lock:
                if self._pending or self._daily_count >= self._max_daily:
                    return
                if key == self._last_habit_key:
                    return
            # 能开口就直接说（真·主动）；否则入队等下轮对话注入
            if self._try_speak(text, habit_id, key):
                return
            with self._lock:
                if self._pending or self._daily_count >= self._max_daily:
                    return
                self._pending = text
                self._pending_habit_id = habit_id
                # 注意：不在入队时写 _last_habit_key，等真正投递给用户后再写，
                # 避免未投递就永久跳过
            logger.info("[Proactor] 习惯建议入队: %s", text)
            return

        try:
            from .tools import winops

            ok, raw = winops.check_system_health()
            if not ok:
                return
            data = json.loads(raw)
            issues = list(data.get("issues") or [])
            warnings = list(data.get("warnings") or [])
            hints = issues + warnings
            if not hints:
                return
            text = "先生，检测到：" + "、".join(hints[:3])
            if len(hints) > 3:
                text += " 等"
            text += "，需要我帮忙看看吗？"
            with self._lock:
                if text == self._last_health_text:
                    return  # 同样的健康问题当天只提一次
                self._last_health_text = text
            if self._try_speak(text, None, None):
                return
            with self._lock:
                if self._pending or self._daily_count >= self._max_daily:
                    return
                self._pending = text
                self._pending_habit_id = None
            logger.info("[Proactor] 健康建议入队: %s", text)
        except Exception:
            pass

    def _pick_habit_suggestion(self) -> tuple[str, int | None, str] | None:
        if not self._db:
            return None
        bucket = _time_bucket()
        with self._db.connect() as conn:
            rows = conn.execute(
                """SELECT id, description, trigger_condition, suggested_action,
                          confidence, auto_suggest
                   FROM habit_patterns
                   WHERE auto_suggest = 1 AND confidence >= ?
                   ORDER BY confidence DESC, hit_count DESC
                   LIMIT 20""",
                (self._min_confidence,),
            ).fetchall()
        for row in rows:
            try:
                cond = json.loads(row["trigger_condition"] or "{}")
            except json.JSONDecodeError:
                cond = {}
            if not isinstance(cond, dict):
                continue
            if cond.get("time_bucket") and cond.get("time_bucket") != bucket:
                continue
            action = (row["suggested_action"] or "").strip()
            if not action:
                continue
            key = f"{bucket}:{row['id']}"
            return action, int(row["id"]), key
        return None

    def recent_spoken(self, max_age_sec: float = 300.0) -> str | None:
        """最近一次主动播报的内容（供下一轮对话补上下文）。取走即清。"""
        with self._lock:
            if not self._spoken_text:
                return None
            if time.time() - self._spoken_at > max_age_sec:
                self._spoken_text = None
                return None
            s = self._spoken_text
            self._spoken_text = None
            return s

    def take_suggestion(self) -> str | None:
        with self._lock:
            self._roll_day()
            if self._daily_count >= self._max_daily:
                self._pending = None
                return None
            s = self._pending
            habit_id = self._pending_habit_id
            self._pending = None
            self._pending_habit_id = None
            pending_key = None

        if not s:
            habit = self._pick_habit_suggestion()
            if habit:
                text, hid, key = habit
                with self._lock:
                    if self._daily_count >= self._max_daily:
                        return None
                    if key == self._last_habit_key:
                        return None
                    s = text
                    habit_id = hid
                    pending_key = key

        if not s:
            return None

        with self._lock:
            self._daily_count += 1
            if habit_id is not None:
                self._last_offered_habit_id = habit_id
                if pending_key:
                    self._last_habit_key = pending_key
                else:
                    self._last_habit_key = f"{_time_bucket()}:{habit_id}"

        if habit_id and self._db:
            try:
                with self._db.connect() as conn:
                    conn.execute(
                        """UPDATE habit_patterns SET
                           hit_count = hit_count + 1,
                           last_suggested = datetime('now')
                           WHERE id = ?""",
                        (habit_id,),
                    )
                    conn.commit()
            except Exception:
                pass
        return s

    def note_user_text(self, text: str) -> bool:
        """处理拒绝主动建议。

        返回 True 表示本句应短路（纯拒绝、不进 LLM）；
        返回 False 表示继续走 Agent Loop（可能仍关闭习惯提醒）。
        """
        t = (text or "").strip()
        if not t:
            return False

        wants_dismiss = bool(_DISMISS_HINT_RE.search(t) or _PURE_DISMISS_RE.match(t))
        if not wants_dismiss:
            return False

        with self._lock:
            self._pending = None
            habit_id = self._pending_habit_id or self._last_offered_habit_id
            self._pending_habit_id = None

        if habit_id and self._db:
            try:
                with self._db.connect() as conn:
                    conn.execute(
                        "UPDATE habit_patterns SET auto_suggest = 0 WHERE id = ?",
                        (habit_id,),
                    )
                    conn.commit()
                logger.info("[Proactor] 用户拒绝主动建议 id=%s", habit_id)
            except Exception:
                pass

        # 夹带任务意图 → 不短路，让 LLM 继续干活
        if _TASK_HINT_RE.search(t) and not _PURE_DISMISS_RE.match(t):
            return False
        return bool(_PURE_DISMISS_RE.match(t) or (
            _DISMISS_HINT_RE.search(t) and len(t) <= 12 and not _TASK_HINT_RE.search(t)
        ))

    def close(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=2)
