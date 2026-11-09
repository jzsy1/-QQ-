"""ScreenManager — 屏幕读取、截图存档与召回。"""

from __future__ import annotations

from pathlib import Path

from ...tools import screen_ops
from ...tools.execution_result import ExecutionResult
from ...tools.screen_ops import ScreenReadResult


class ScreenManager:
    """读屏、截图归档与历史召回。"""

    def read_screen(self) -> ScreenReadResult:
        return screen_ops.read_screen()

    def recall_screenshot(self, screenshot_id: str) -> ScreenReadResult:
        return screen_ops.recall_screenshot(screenshot_id)

    def list_recent(self, limit: int = 10) -> ExecutionResult:
        ids = screen_ops.list_recent_screenshots(limit)
        if not ids:
            return ExecutionResult(
                success=True,
                message="截图存档目录为空。",
                verified=True,
                action="list_screenshots",
            )
        return ExecutionResult(
            success=True,
            message="最近截图 id：\n" + "\n".join(f"- {i}" for i in ids),
            verified=True,
            action="list_screenshots",
            extra={"ids": ids},
        )

    def screenshot_dir(self) -> Path:
        return screen_ops.screenshot_dir()
