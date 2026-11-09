"""视觉点击 — 文字 → 坐标 → 点击的完整链路。

整合 UIA 和 OCR 两条路径，按可靠性优先级自动选择：
1. UIA 查找（最精确，能识别控件类型）
2. OCR 查找（兜底，能识别任何可见文字，包括非原生控件）

核心能力：
- click_text：点击屏幕上指定文字（先 UIA 后 OCR）
- click_button：点击按钮（按名称/文字）
- read_text_in_region：读取屏幕区域内的文字
- smart_click：智能点击（综合 UIA + OCR + 视觉模型）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .mouse_ops import click as mouse_click, double_click, move_to, get_cursor_pos
from .ocr_ops import ocr_find_text, ocr_screen, OcrResult
from .uia_ops import click_element, find_elements, get_element_text, list_controls, UIASearchResult

logger = logging.getLogger(__name__)


@dataclass
class ClickResult:
    ok: bool
    message: str
    method: str = ""  # uia / ocr / coordinate
    x: int = 0
    y: int = 0


def click_text(
    text: str,
    *,
    window_title: str = "",
    button: str = "left",
    double: bool = False,
    ocr_language: str = "zh-Hans-CN",
) -> ClickResult:
    """点击屏幕上指定的文字。

    策略：
    1. 先尝试 UIA（按 Name 查找控件）—— 精确、能区分控件类型
    2. UIA 失败则用 OCR 在屏幕上找文字坐标 —— 兜底，能识别任何可见文字

    Args:
        text: 要点击的文字
        window_title: 限定窗口标题（UIA 路径会用，OCR 路径忽略）
        button: left/right/middle
        double: 是否双击
        ocr_language: OCR 语言
    """
    text = (text or "").strip()
    if not text:
        return ClickResult(False, "请提供要点击的文字")

    # 1) UIA 路径
    try:
        ok, msg = click_element(
            name=text,
            root_window_title=window_title,
            button=button,
            double=double,
        )
        if ok:
            return ClickResult(True, msg, method="uia")
        logger.info("UIA 点击失败，回退到 OCR: %s", msg)
    except Exception as e:
        logger.warning("UIA 异常: %s", e)

    # 2) OCR 路径
    try:
        ok, msg, pos = ocr_find_text(text, language=ocr_language)
        if ok and pos:
            cx, cy = pos
            if double:
                ok2, msg2 = double_click(cx, cy)
            else:
                ok2, msg2 = mouse_click(cx, cy, button=button)
            if ok2:
                return ClickResult(True, f"{msg}，已点击", method="ocr", x=cx, y=cy)
            return ClickResult(False, f"{msg}，但点击失败: {msg2}", method="ocr", x=cx, y=cy)
        return ClickResult(False, msg or f"未找到「{text}」", method="ocr")
    except Exception as e:
        return ClickResult(False, f"OCR 点击失败: {e}", method="ocr")


def click_button(
    name: str,
    *,
    window_title: str = "",
    ocr_language: str = "zh-Hans-CN",
) -> ClickResult:
    """点击按钮（优先 UIA Button 控件类型，回退 OCR）。"""
    name = (name or "").strip()
    if not name:
        return ClickResult(False, "请提供按钮名称")

    # 1) UIA：限定 Button 控件类型
    try:
        ok, msg = click_element(
            name=name,
            control_type="button",
            root_window_title=window_title,
        )
        if ok:
            return ClickResult(True, msg, method="uia")
    except Exception as e:
        logger.warning("UIA 按钮点击异常: %s", e)

    # 2) OCR 兜底
    return click_text(name, window_title=window_title, ocr_language=ocr_language)


def read_screen_text(
    *,
    region: tuple[int, int, int, int] | None = None,
    window_title: str = "",
    language: str = "zh-Hans-CN",
) -> tuple[bool, str]:
    """读取屏幕（或区域、或窗口）内的文字。

    优先用 OCR（能识别所有可见文字），UIA 仅作为补充。
    """
    # 1) 如果指定了窗口标题，先尝试用 UIA 获取窗口内所有文本控件
    if window_title:
        try:
            result = find_elements(
                control_type="text",
                root_window_title=window_title,
                scope="descendants",
                limit=100,
                timeout=15,
            )
            if result.ok and result.elements:
                texts = [el.text or el.name for el in result.elements if (el.text or el.name)]
                if texts:
                    return True, "窗口内文本（UIA）：\n" + "\n".join(texts)
        except Exception as e:
            logger.warning("UIA 读取文本失败: %s", e)

    # 2) OCR 路径
    try:
        result = ocr_screen(region=region, language=language)
        if result.ok:
            if result.text:
                return True, f"屏幕文字（OCR，{result.message}）：\n{result.text}"
            return True, "OCR 未识别到文字"
        return False, result.message
    except Exception as e:
        return False, f"OCR 失败: {e}"


def smart_click(
    target: str,
    *,
    window_title: str = "",
    button: str = "left",
    double: bool = False,
    ocr_language: str = "zh-Hans-CN",
) -> ClickResult:
    """智能点击：综合 UIA + OCR，自动选择最可靠的方式。

    与 click_text 类似，但会先列出窗口控件帮助诊断，适合复杂场景。

    Args:
        target: 目标文字或控件名
    """
    target = (target or "").strip()
    if not target:
        return ClickResult(False, "请提供点击目标")

    # 1) UIA：不限控件类型，按名称查找
    try:
        ok, msg = click_element(
            name=target,
            root_window_title=window_title,
            button=button,
            double=double,
        )
        if ok:
            return ClickResult(True, msg, method="uia")
    except Exception as e:
        logger.warning("smart_click UIA 异常: %s", e)

    # 2) OCR 兜底
    return click_text(target, window_title=window_title, button=button, double=double, ocr_language=ocr_language)


def click_at(x: int, y: int, *, button: str = "left", double: bool = False) -> tuple[bool, str]:
    """在指定坐标点击（最底层，不经过任何识别）。"""
    if double:
        return double_click(x, y)
    return mouse_click(x, y, button=button)


def hover_text(
    text: str,
    *,
    window_title: str = "",
    ocr_language: str = "zh-Hans-CN",
) -> ClickResult:
    """将鼠标悬停在指定文字上（不点击）。"""
    text = (text or "").strip()
    if not text:
        return ClickResult(False, "请提供文字")

    # UIA 查找坐标
    try:
        result = find_elements(
            name=text,
            root_window_title=window_title,
            limit=1,
            timeout=8,
        )
        if result.ok and result.elements:
            el = result.elements[0]
            cx, cy = el.center
            ok, msg = move_to(cx, cy)
            if ok:
                return ClickResult(True, f"已悬停「{text}」于 ({cx}, {cy})", method="uia", x=cx, y=cy)
    except Exception as e:
        logger.warning("hover UIA 异常: %s", e)

    # OCR 兜底
    try:
        ok, msg, pos = ocr_find_text(text, language=ocr_language)
        if ok and pos:
            cx, cy = pos
            ok2, msg2 = move_to(cx, cy)
            if ok2:
                return ClickResult(True, f"{msg}，已悬停", method="ocr", x=cx, y=cy)
            return ClickResult(False, msg2, method="ocr", x=cx, y=cy)
        return ClickResult(False, msg, method="ocr")
    except Exception as e:
        return ClickResult(False, f"OCR 悬停失败: {e}", method="ocr")
