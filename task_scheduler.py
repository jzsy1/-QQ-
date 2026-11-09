"""任务调度器 — 支持跨时间任务规划（提醒、定时执行）"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Callable, Any

from .memory.db import Database

logger = logging.getLogger(__name__)

_TASK_SCHEMA = """
CREATE TABLE IF NOT EXISTS scheduled_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_type TEXT NOT NULL DEFAULT 'reminder',
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    scheduled_time TEXT NOT NULL,
    completed INTEGER DEFAULT 0,
    notified INTEGER DEFAULT 0,
    repeat_type TEXT DEFAULT 'once',
    repeat_interval INTEGER DEFAULT 0,
    state TEXT DEFAULT 'pending',
    last_error TEXT DEFAULT '',
    verified INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);
"""


@dataclass
class ScheduledTask:
    id: int = 0
    task_type: str = "reminder"
    title: str = ""
    description: str = ""
    scheduled_time: str = ""
    completed: bool = False
    notified: bool = False
    repeat_type: str = "once"
    repeat_interval: int = 0
    state: str = "pending"
    last_error: str = ""
    verified: bool = False
    created_at: str = ""
    updated_at: str = ""

    @property
    def scheduled_datetime(self) -> datetime | None:
        try:
            return datetime.fromisoformat(self.scheduled_time.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            try:
                return datetime.strptime(self.scheduled_time, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return None

    @property
    def is_due(self) -> bool:
        dt = self.scheduled_datetime
        if not dt:
            return False
        return datetime.now() >= dt and not self.completed and not self.notified


class TaskScheduler:
    def __init__(self, db: Database, *, poll_interval_sec: int = 60):
        self.db = db
        self._poll_interval_sec = max(15, int(poll_interval_sec))
        self._init_schema()
        self._notification_callback: Callable[[ScheduledTask], None] | None = None
        self._dispatch_callback: Callable[[ScheduledTask], None] | None = None
        self._running = False
        self._thread: threading.Thread | None = None

    def set_dispatch_callback(self, callback: Callable[[ScheduledTask], None]):
        """统一任务分发（TaskEngine 使用）；设置后 _check_due_tasks 走此回调。"""
        self._dispatch_callback = callback

    def set_notification_callback(self, callback: Callable[[ScheduledTask], None]):
        """仅 reminder 的简易回调（兼容旧接线）。"""
        self._notification_callback = callback

    # 兼容旧 API 名
    def set_execute_callback(self, callback: Callable[[ScheduledTask], None]):
        self.set_dispatch_callback(callback)

    def _init_schema(self):
        with self.db.connect() as conn:
            conn.executescript(_TASK_SCHEMA)
            self._migrate_schema(conn)
            conn.commit()

    def _migrate_schema(self, conn) -> None:
        """为旧库补充 state / last_error / verified 列。"""
        rows = conn.execute("PRAGMA table_info(scheduled_tasks)").fetchall()
        cols = {str(r[1]) for r in rows}
        if "state" not in cols:
            conn.execute(
                "ALTER TABLE scheduled_tasks ADD COLUMN state TEXT DEFAULT 'pending'"
            )
        if "last_error" not in cols:
            conn.execute(
                "ALTER TABLE scheduled_tasks ADD COLUMN last_error TEXT DEFAULT ''"
            )
        if "verified" not in cols:
            conn.execute(
                "ALTER TABLE scheduled_tasks ADD COLUMN verified INTEGER DEFAULT 0"
            )

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()
        logger.info("任务调度器已启动")

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("任务调度器已停止")

    def _monitor_loop(self):
        while self._running:
            try:
                self._check_due_tasks()
            except Exception as e:
                logger.error("任务调度器检查失败: %s", e)
            time.sleep(self._poll_interval_sec)

    def _check_due_tasks(self):
        tasks = self.get_due_tasks()
        for task in tasks:
            if self._dispatch_callback:
                try:
                    self._dispatch_callback(task)
                except Exception as e:
                    logger.error("任务分发失败 #%s: %s", task.id, e)
                self.mark_notified(task.id)
                continue
            self._notify(task)
            self.mark_notified(task.id)

    def _notify(self, task: ScheduledTask):
        if self._notification_callback:
            try:
                self._notification_callback(task)
            except Exception as e:
                logger.error("通知回调失败: %s", e)
        logger.info("任务提醒: %s (%s)", task.title, task.scheduled_time)

    def add_task(
        self,
        title: str,
        scheduled_time: datetime,
        description: str = "",
        task_type: str = "reminder",
        repeat_type: str = "once",
        repeat_interval: int = 0,
    ) -> int:
        with self.db.connect() as conn:
            cur = conn.execute(
                """INSERT INTO scheduled_tasks
                   (task_type, title, description, scheduled_time,
                    repeat_type, repeat_interval)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (task_type, title, description, scheduled_time.isoformat(), repeat_type, repeat_interval),
            )
            conn.commit()
            task_id = int(cur.lastrowid)
        logger.info("新增任务: %s -> %s", title, scheduled_time)
        return task_id

    def get_task(self, task_id: int) -> ScheduledTask | None:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM scheduled_tasks WHERE id = ?", (task_id,)
            ).fetchone()
        if not row:
            return None
        return self._row_to_task(row)

    def get_due_tasks(self) -> list[ScheduledTask]:
        now = datetime.now().isoformat()
        with self.db.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM scheduled_tasks
                   WHERE scheduled_time <= ? AND completed = 0 AND notified = 0
                   ORDER BY scheduled_time ASC""",
                (now,),
            ).fetchall()
        return [self._row_to_task(row) for row in rows]

    def get_pending_tasks(self) -> list[ScheduledTask]:
        with self.db.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM scheduled_tasks
                   WHERE completed = 0
                   ORDER BY scheduled_time ASC"""
            ).fetchall()
        return [self._row_to_task(row) for row in rows]

    def complete_task(self, task_id: int) -> bool:
        with self.db.connect() as conn:
            cur = conn.execute(
                """UPDATE scheduled_tasks
                   SET completed = 1, updated_at = datetime('now')
                   WHERE id = ? AND completed = 0""",
                (task_id,),
            )
            conn.commit()
        return cur.rowcount > 0

    def mark_notified(self, task_id: int) -> bool:
        with self.db.connect() as conn:
            cur = conn.execute(
                """UPDATE scheduled_tasks
                   SET notified = 1, updated_at = datetime('now')
                   WHERE id = ?""",
                (task_id,),
            )
            conn.commit()
        return cur.rowcount > 0

    def update_task_state(
        self,
        task_id: int,
        state: str,
        *,
        last_error: str = "",
        verified: bool | None = None,
    ) -> bool:
        sets = ["state = ?", "updated_at = datetime('now')"]
        params: list[Any] = [state]
        if last_error:
            sets.append("last_error = ?")
            params.append(last_error)
        if verified is not None:
            sets.append("verified = ?")
            params.append(1 if verified else 0)
        params.append(task_id)
        with self.db.connect() as conn:
            cur = conn.execute(
                f"UPDATE scheduled_tasks SET {', '.join(sets)} WHERE id = ?",
                tuple(params),
            )
            conn.commit()
        return cur.rowcount > 0

    def delete_task(self, task_id: int) -> bool:
        with self.db.connect() as conn:
            cur = conn.execute(
                "DELETE FROM scheduled_tasks WHERE id = ?", (task_id,)
            )
            conn.commit()
        return cur.rowcount > 0

    @staticmethod
    def _row_to_task(row) -> ScheduledTask:
        keys = row.keys() if hasattr(row, "keys") else []
        return ScheduledTask(
            id=int(row["id"]),
            task_type=str(row["task_type"]),
            title=str(row["title"]),
            description=str(row["description"]),
            scheduled_time=str(row["scheduled_time"]),
            completed=bool(row["completed"]),
            notified=bool(row["notified"]),
            repeat_type=str(row["repeat_type"]),
            repeat_interval=int(row["repeat_interval"]),
            state=str(row["state"]) if "state" in keys else "pending",
            last_error=str(row["last_error"]) if "last_error" in keys else "",
            verified=bool(row["verified"]) if "verified" in keys else False,
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def parse_time_expression(expression: str) -> datetime | None:
        now = datetime.now()
        expr = expression.strip().lower()

        try:
            if "明天" in expr:
                target = now + timedelta(days=1)
                expr = expr.replace("明天", "").strip()
            elif "后天" in expr:
                target = now + timedelta(days=2)
                expr = expr.replace("后天", "").strip()
            elif "今天" in expr:
                target = now
                expr = expr.replace("今天", "").strip()
            else:
                target = now

            hour, minute = TaskScheduler._parse_hms(expr)

            # 时段修饰词对 HH:MM 与中文时间均需生效
            period = "auto"
            if "早上" in expr or "上午" in expr:
                period = "am"
            elif "下午" in expr or "中午" in expr:
                period = "pm"
            elif "晚上" in expr or "夜里" in expr:
                period = "pm"

            if hour is None:
                hour, minute = TaskScheduler._parse_chinese_time(expr, period)
            elif period != "auto":
                # _parse_hms 命中时在此修正时段（_parse_chinese_time 已自行处理，不重复）
                if period == "pm" and hour < 12:
                    hour += 12
                elif period == "am" and hour >= 12:
                    hour -= 12

            if hour is None:
                return None

            target = target.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            return target

        except Exception as e:
            logger.error("时间解析失败: %s", e)

        return None

    @staticmethod
    def _parse_hms(expr: str) -> tuple[int | None, int]:
        """解析 HH:MM 或 HH:MM:SS 格式"""
        m = re.search(r"(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?", expr)
        if not m:
            return None, 0
        hour = int(m.group(1))
        minute = int(m.group(2))
        if hour > 23 or minute > 59:
            return None, 0
        return hour, minute

    @staticmethod
    def _parse_chinese_time(expr: str, period: str = "auto") -> tuple[int | None, int]:
        """解析中文时间：8点、8点半、8点30分、9点15"""
        cleaned = re.sub(r"(早上|上午|下午|晚上|夜里|凌晨|中午)", "", expr).strip()
        m = re.search(r"(\d{1,2})\s*点半|(\d{1,2})\s*点\s*(?:(\d{1,2})\s*分)?", cleaned)
        if not m:
            return None, 0

        if m.group(1):
            hour = int(m.group(1))
            minute = 30
        else:
            hour = int(m.group(2))
            minute = int(m.group(3)) if m.group(3) else 0

        if hour > 23 or minute > 59:
            return None, 0

        if period == "pm" and hour < 12:
            hour += 12
        elif period == "am" and hour == 12:
            hour = 0

        return hour, minute