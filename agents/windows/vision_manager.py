"""VisionManager — OCR 屏幕识别。"""

from __future__ import annotations

from ...tools.execution_result import ExecutionResult, from_tuple
from ...tools.ocr_ops import (
    list_ocr_languages,
    ocr_find_text,
    ocr_screen,
)


class VisionManager:
    """OCR 与屏幕文字识别。"""

    def ocr_screen(
        self,
        *,
        region: tuple[int, int, int, int] | None = None,
        language: str = "zh-Hans-CN",
    ) -> ExecutionResult:
        result = ocr_screen(region=region, language=language)
        if not result.ok:
            return ExecutionResult(
                success=False,
                message=result.message,
                verified=False,
                action="ocr_screen",
                target=language,
            )
        if not result.lines:
            return ExecutionResult(
                success=True,
                message="未识别到文字",
                verified=True,
                action="ocr_screen",
                target=language,
            )
        lines = []
        for ln in result.lines:
            cx, cy = ln.center
            lines.append(f"  [{cx},{cy}] {ln.text}")
        return ExecutionResult(
            success=True,
            message=f"识别到 {len(result.lines)} 行：\n" + "\n".join(lines),
            verified=True,
            action="ocr_screen",
            target=language,
            extra={"line_count": len(result.lines)},
        )

    def ocr_find_text(
        self,
        text: str,
        *,
        region: tuple[int, int, int, int] | None = None,
        language: str = "zh-Hans-CN",
        partial: bool = True,
    ) -> ExecutionResult:
        raw = (text or "").strip()
        if not raw:
            return ExecutionResult(
                success=False,
                message="请提供要查找的文字",
                verified=False,
                action="ocr_find_text",
                target="",
            )
        ok, msg, pos = ocr_find_text(
            raw,
            region=region,
            language=language,
            partial=partial,
        )
        extra: dict = {}
        if pos:
            extra["x"], extra["y"] = int(pos[0]), int(pos[1])
        message = f"{msg}（坐标 {pos[0]},{pos[1]}）" if ok and pos else msg
        return ExecutionResult(
            success=ok,
            message=message,
            verified=ok,
            action="ocr_find_text",
            target=raw,
            extra=extra,
        )

    def list_languages(self) -> ExecutionResult:
        ok, msg = list_ocr_languages()
        return from_tuple(ok, msg, verified=ok, action="list_ocr_languages")

    def scan_desktop(
        self,
        *,
        limit: int = 20,
        include_controls: bool = True,
        include_regions: bool = True,
    ) -> ExecutionResult:
        from ...tools.scan_desktop_ops import scan_desktop

        ok, msg = scan_desktop(
            limit=int(limit),
            include_controls=include_controls,
            include_regions=include_regions,
        )
        return from_tuple(ok, msg, verified=ok, action="scan_desktop")

    def screenshot_window(
        self,
        *,
        title: str = "",
        hwnd: int = 0,
        path: str = "",
    ) -> ExecutionResult:
        from ...tools.window_capture import capture_window

        ok, msg, saved = capture_window(
            title=title,
            hwnd=int(hwnd or 0),
            path=path,
        )
        extra = {"path": saved} if saved else {}
        return ExecutionResult(
            success=ok,
            message=msg,
            verified=ok,
            action="screenshot_window",
            target=(title or str(hwnd or "window"))[:40],
            extra=extra,
        )

    def ocr_window(
        self,
        *,
        title: str = "",
        hwnd: int = 0,
        language: str = "zh-Hans-CN",
        dark_enhance: bool = True,
    ) -> ExecutionResult:
        from ...tools.window_capture import ocr_window

        ok, msg = ocr_window(
            title=title,
            hwnd=int(hwnd or 0),
            language=language,
            dark_enhance=dark_enhance,
        )
        return from_tuple(
            ok,
            msg,
            verified=ok,
            action="ocr_window",
            target=(title or str(hwnd or "window"))[:40],
        )

    def register_image(self, name: str, path: str) -> ExecutionResult:
        from ...tools.image_match_ops import register_image

        ok, msg = register_image(name, path)
        return from_tuple(ok, msg, verified=ok, action="register_image", target=(name or "")[:40])

    def find_image(self, image: str, *, confidence: float = 0.8) -> ExecutionResult:
        from ...tools.image_match_ops import find_image

        ok, msg, match = find_image(image, confidence=float(confidence))
        extra: dict = {}
        message = msg
        if ok and match:
            cx, cy = match.center
            extra["x"], extra["y"] = cx, cy
            message = f"{msg}（中心 {cx},{cy}）"
        return ExecutionResult(
            success=ok,
            message=message,
            verified=ok,
            action="find_image",
            target=(image or "")[:40],
            extra=extra,
        )

    def click_image(
        self,
        image: str,
        *,
        confidence: float = 0.8,
        button: str = "left",
        double: bool = False,
        virtual: bool = True,
    ) -> ExecutionResult:
        from ...tools.image_match_ops import click_image

        ok, msg = click_image(
            image,
            confidence=float(confidence),
            button=button,
            double=double,
            virtual=virtual,
        )
        return from_tuple(ok, msg, verified=ok, action="click_image", target=(image or "")[:40])
