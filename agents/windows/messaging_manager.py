"""MessagingManager — IM 应用消息（微信等）。"""

from __future__ import annotations

from typing import Any

from ...tools.execution_result import ExecutionResult, from_tuple
from ...tools import wechat_ops


class MessagingManager:
    """即时通讯操作封装。"""

    def __init__(self, tool_ctx: Any = None) -> None:
        self._ctx = tool_ctx

    def _resolve_launch(self, app_name: str) -> str:
        if self._ctx and hasattr(self._ctx, "resolve_app_command"):
            return self._ctx.resolve_app_command(app_name) or ""
        return ""

    def send_message(
        self,
        contact: str,
        message: str,
        *,
        app_name: str,
        launch_cmd: str = "",
    ) -> ExecutionResult:
        raw_app = (app_name or "").strip()
        if not raw_app:
            return ExecutionResult(
                success=False,
                message="请指定 app_name（发消息使用的应用）",
                verified=False,
                action="wechat_send_message",
                target="",
            )
        cmd = (launch_cmd or "").strip() or self._resolve_launch(raw_app)
        ok, msg = wechat_ops.wechat_send_message(
            contact,
            message,
            launch_cmd=cmd,
            app_name=raw_app,
        )
        return from_tuple(
            ok,
            msg,
            verified=ok,
            action="wechat_send_message",
            target=(contact or raw_app)[:40],
        )

    def read_messages(
        self,
        *,
        app_name: str,
        scope: str = "unread",
        contact: str | None = None,
        max_chats: int = 3,
        launch_cmd: str = "",
    ) -> ExecutionResult:
        raw_app = (app_name or "").strip()
        if not raw_app:
            return ExecutionResult(
                success=False,
                message="请指定 app_name",
                verified=False,
                action="wechat_read_messages",
                target="",
            )
        cmd = (launch_cmd or "").strip() or self._resolve_launch(raw_app)
        ok, msg = wechat_ops.wechat_read_messages(
            launch_cmd=cmd,
            app_name=raw_app,
            scope=(scope or "unread").strip().lower(),
            contact=(contact or "").strip() or None,
            max_chats=max_chats,
        )
        return from_tuple(
            ok,
            msg,
            verified=ok,
            action="wechat_read_messages",
            target=raw_app,
        )

    def reply_current(
        self,
        message: str,
        *,
        app_name: str,
        launch_cmd: str = "",
    ) -> ExecutionResult:
        raw_app = (app_name or "").strip()
        content = (message or "").strip()
        if not raw_app:
            return ExecutionResult(
                success=False,
                message="请指定 app_name",
                verified=False,
                action="wechat_reply_current",
                target="",
            )
        if not content:
            return ExecutionResult(
                success=False,
                message="请指定要回复的内容",
                verified=False,
                action="wechat_reply_current",
                target=raw_app,
            )
        cmd = (launch_cmd or "").strip() or self._resolve_launch(raw_app)
        ok, msg = wechat_ops.wechat_reply_current(
            content,
            launch_cmd=cmd,
            app_name=raw_app,
        )
        return from_tuple(
            ok,
            msg,
            verified=ok,
            action="wechat_reply_current",
            target=raw_app,
        )
