"""视觉点击 — 文字 → 坐标 → 点击的完整链路。

整合 UIA、OCR、鼠标三大能力，提供高层次的「按文字/图标点击」接口。
策略链：UIA（最精确）→ OCR（兜底）→ 视觉模型（最后防线）。

典型场景：
- click_text("登录")：在屏幕上找到"登录"文字并点击
- click_text("完美世界", app="腾讯视频")：在腾讯视频窗口里点"完美世界"
- click_icon("搜索")：UIA 找不到时用 OCR 兜底
"""

from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass, field

from .mouse_ops import click as mouse_click, double_click, move_to, get_cursor_pos
from .ocr_ops import ocr_find_text, ocr_screen, OcrResult
from .uia_ops import find_elements, UIAElement, UIASearchResult

logger = logging.getLogger(__name__)


@dataclass
class ClickResult:
    """视觉点击结果。"""
    ok: bool
    message: str
    method: str = ""  # uia / ocr / vision / none
    x: int = 0
    y: int = 0
    element: UIAElement | None = None


def click_text(
    text: str,
    *,
    app: str = "",
    button: str = "left",
    double: bool = False,
    partial: bool = True,
) -> ClickResult:
    """在屏幕上找到指定文字并点击。

    策略：
    1. UIA：按名称查找控件（最精确，能区分按钮/链接/文本）
    2. OCR：UIA 找不到时，对屏幕做 OCR 找文字坐标
    3. 都失败则返回失败

    Args:
        text: 要点击的文字
        app: 限定应用窗口标题关键词（提高精度）
        button: left/right/middle
        double: 是否双击
        partial: 是否部分匹配
    """
    text = (text or "").strip()
    if not text:
        return ClickResult(False, "请提供要点击的文字")

    # —— 策略 1：UIA 按名称查找 ——
    try:
        result = find_elements(
            name=text,
            root_window_title=app,
            scope="descendants",
            limit=5,
            timeout=8,
        )
        if result.ok and result.elements:
            # 选第一个可见且启用的元素
            for el in result.elements:
                if el.is_enabled and not el.is_offscreen and el.width > 0 and el.height > 0:
                    cx, cy = el.center
                    if double:
                        ok, msg = double_click(cx, cy)
                    else:
                        ok, msg = mouse_click(cx, cy, button=button)
                    if ok:
                        return ClickResult(
                            ok=True,
                            message=f"已通过 UIA 点击「{text}」于 ({cx}, {cy})",
                            method="uia",
                            x=cx, y=cy,
                            element=el,
                        )
                    return ClickResult(False, f"UIA 定位成功但点击失败: {msg}", method="uia")
            # 所有元素都不可见
            return ClickResult(
                False,
                f"UIA 找到「{text}」但控件不可见或禁用",
                method="uia",
                element=result.elements[0],
            )
    except Exception as e:
        logger.debug("UIA 查找失败，回退 OCR: %s", e)

    # —— 策略 2：OCR 兜底 ——
    try:
        ok, msg, pos = ocr_find_text(text, partial=partial)
        if ok and pos:
            cx, cy = pos
            if double:
                mok, mmsg = double_click(cx, cy)
            else:
                mok, mmsg = mouse_click(cx, cy, button=button)
            if mok:
                return ClickResult(
                    ok=True,
                    message=f"已通过 OCR 点击「{text}」于 ({cx}, {cy})",
                    method="ocr",
                    x=cx, y=cy,
                )
            return ClickResult(False, f"OCR 定位成功但点击失败: {mmsg}", method="ocr")
    except Exception as e:
        logger.debug("OCR 查找失败: %s", e)

    return ClickResult(False, f"未能在屏幕上找到「{text}」", method="none")


def find_text_location(
    text: str,
    *,
    app: str = "",
    partial: bool = True,
) -> tuple[bool, str, tuple[int, int] | None, str]:
    """查找文字位置，不点击。

    Returns:
        (ok, message, (x, y), method)
    """
    text = (text or "").strip()
    if not text:
        return False, "请提供要查找的文字", None, ""

    # UIA 优先
    try:
        result = find_elements(
            name=text,
            root_window_title=app,
            scope="descendants",
            limit=1,
            timeout=8,
        )
        if result.ok and result.elements:
            el = result.elements[0]
            if el.width > 0 and el.height > 0:
                cx, cy = el.center
                return True, f"UIA 找到「{text}」于 ({cx}, {cy})", (cx, cy), "uia"
    except Exception:
        pass

    # OCR 兜底
    try:
        ok, msg, pos = ocr_find_text(text, partial=partial)
        if ok and pos:
            return True, msg, pos, "ocr"
    except Exception:
        pass

    return False, f"未找到「{text}」", None, "none"


def read_screen_text(
    *,
    app: str = "",
    region: tuple[int, int, int, int] | None = None,
) -> tuple[bool, str]:
    """读取屏幕（或指定窗口/区域）的全部文字。

    优先用 UIA 收集所有文本控件（结构化、精确），
    失败则用 OCR（能识别图片中的文字）。
    """
    # —— UIA 收集文本 ——
    try:
        result = find_elements(
            control_type="text",
            root_window_title=app,
            scope="descendants",
            limit=200,
            timeout=15,
        )
        if result.ok and result.elements:
            texts = []
            for el in result.elements:
                t = el.text or el.name
                if t and t.strip():
                    texts.append(t.strip())
            if texts:
                return True, "UIA 文本：\n" + "\n".join(texts)
    except Exception as e:
        logger.debug("UIA 文本收集失败: %s", e)

    # —— OCR 兜底 ——
    try:
        result = ocr_screen(region=region)
        if result.ok and result.text:
            return True, "OCR 文本：\n" + result.text
        if result.ok:
            return True, "屏幕上未识别到文字"
    except Exception as e:
        logger.debug("OCR 失败: %s", e)

    return False, "无法读取屏幕文字"


def click_at(x: int, y: int, *, button: str = "left", double: bool = False) -> tuple[bool, str]:
    """在指定坐标点击（视觉模型给出坐标时的执行入口）。"""
    if double:
        return double_click(int(x), int(y))
    return mouse_click(int(x), int(y), button=button)


def hover_text(text: str, *, app: str = "", partial: bool = True) -> tuple[bool, str]:
    """将鼠标悬停到指定文字上（不点击）。"""
    ok, msg, pos, method = find_text_location(text, app=app, partial=partial)
    if not ok or not pos:
        return False, msg
    cx, cy = pos
    mok, mmsg = move_to(cx, cy)
    if mok:
        return True, f"已悬停到「{text}」于 ({cx}, {cy})（{method}）"
    return False, mmsg


def drag_text_to(
    src_text: str,
    dst_x: int,
    dst_y: int,
    *,
    app: str = "",
    partial: bool = True,
) -> tuple[bool, str]:
    """把屏幕上的某段文字拖拽到目标坐标。"""
    ok, msg, pos, method = find_text_location(src_text, app=app, partial=partial)
    if not ok or not pos:
        return False, msg
    sx, sy = pos
    from .mouse_ops import drag
    dok, dmsg = drag(sx, sy, int(dst_x), int(dst_y))
    if dok:
        return True, f"已将「{src_text}」从 ({sx},{sy}) 拖到 ({dst_x},{dst_y})"
    return False, dmsg
