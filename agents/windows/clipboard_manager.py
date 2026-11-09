"""ClipboardManager — 剪贴板读写。"""

from __future__ import annotations

from ...tools import winops
from ...tools.execution_result import ExecutionResult


class ClipboardManager:
    """系统剪贴板操作。"""

    def read(self) -> ExecutionResult:
        text = winops.get_clipboard()
        preview = (text or "").strip()
        if len(preview) > 500:
            preview = preview[:500] + "…"
        return ExecutionResult(
            success=True,
            message=preview or "（剪贴板为空）",
            verified=True,
            action="read_clipboard",
            target="",
            extra={"length": len((text or "").strip())},
        )

    def write(self, text: str) -> ExecutionResult:
        raw = str(text or "")
        if not raw.strip():
            return ExecutionResult(
                success=False,
                message="请提供要写入剪贴板的文字",
                verified=False,
                action="write_clipboard",
                target="",
            )
        winops.set_clipboard(raw)
        return ExecutionResult(
            success=True,
            message=f"已写入剪贴板（{len(raw)} 字）",
            verified=True,
            action="write_clipboard",
            target=raw[:40],
        )
