# -*- coding: utf-8 -*-
"""条件等待 — 文字/控件出现消失、画面稳定、文件就绪。"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)


def wait_until_found(
    text: str = "",
    *,
    control_type: str = "",
    app: str = "",
    timeout: float = 15,
    interval: float = 0.6,
    partial: bool = True,
) -> tuple[bool, str]:
    """等待屏幕上出现指定文字（UIA→OCR）或指定类型控件。"""
    text = (text or "").strip()
    if not text and not control_type:
        return False, "请提供 text 或 control_type"
    deadline = time.time() + max(1.0, float(timeout))
    last = ""
    while time.time() < deadline:
        # UIA
        try:
            from .uia_ops import find_elements

            result = find_elements(
                name=text,
                control_type=control_type,
                root_window_title=app,
                limit=3,
                timeout=6,
            )
            if result.ok and result.elements:
                for el in result.elements:
                    if el.is_offscreen or el.width <= 0:
                        continue
                    if text and partial:
                        if text.lower() not in (el.name or "").lower() and text.lower() not in (
                            el.text or ""
                        ).lower():
                            # name 条件已在 find 里，仍算命中
                            pass
                    cx, cy = el.center
                    return True, f"已出现（UIA）「{el.name or text}」@({cx},{cy})"
        except Exception as e:
            last = str(e)

        # OCR
        if text:
            try:
                from .ocr_ops import ocr_find_text

                ok, msg, pos = ocr_find_text(text, partial=partial)
                if ok and pos:
                    return True, f"已出现（OCR）{msg} @({pos[0]},{pos[1]})"
                last = msg
            except Exception as e:
                last = str(e)

        time.sleep(max(0.2, float(interval)))

    return False, f"等待超时 {timeout}s 未出现「{text or control_type}」：{last}"


def wait_until_gone(
    text: str,
    *,
    app: str = "",
    timeout: float = 15,
    interval: float = 0.6,
    partial: bool = True,
) -> tuple[bool, str]:
    """等待文字从屏幕消失。"""
    text = (text or "").strip()
    if not text:
        return False, "请提供 text"
    deadline = time.time() + max(1.0, float(timeout))
    while time.time() < deadline:
        found = False
        try:
            from .uia_ops import find_elements

            result = find_elements(name=text, root_window_title=app, limit=1, timeout=5)
            if result.ok and result.elements:
                el = result.elements[0]
                if not el.is_offscreen and el.width > 0:
                    found = True
        except Exception:
            pass
        if not found:
            try:
                from .ocr_ops import ocr_find_text

                ok, _msg, pos = ocr_find_text(text, partial=partial)
                if ok and pos:
                    found = True
            except Exception:
                pass
        if not found:
            return True, f"「{text}」已消失"
        time.sleep(max(0.2, float(interval)))
    return False, f"等待超时 {timeout}s，「{text}」仍在"


def _screen_fingerprint() -> bytes:
    try:
        from PIL import ImageGrab

        img = ImageGrab.grab(all_screens=True)
        img = img.resize((64, 36))
        return hashlib.md5(img.tobytes()).digest()
    except Exception:
        return b""


def wait_for_stable_screen(
    *,
    timeout: float = 10,
    stable_for: float = 0.8,
    interval: float = 0.25,
) -> tuple[bool, str]:
    """等待画面指纹连续 stable_for 秒不变。"""
    deadline = time.time() + max(1.0, float(timeout))
    last = b""
    stable_since = 0.0
    while time.time() < deadline:
        fp = _screen_fingerprint()
        now = time.time()
        if fp and fp == last:
            if stable_since <= 0:
                stable_since = now
            elif now - stable_since >= float(stable_for):
                return True, f"画面已稳定（连续 {stable_for}s 无变化）"
        else:
            last = fp
            stable_since = now if fp else 0.0
        time.sleep(max(0.1, float(interval)))
    return False, f"等待画面稳定超时 {timeout}s"


def wait_for_file(
    path: str,
    *,
    timeout: float = 30,
    stable_for: float = 1.0,
    interval: float = 0.4,
) -> tuple[bool, str]:
    """等待文件出现且大小在 stable_for 秒内不变（下载完成）。"""
    p = Path(path or "").expanduser()
    if not str(p):
        return False, "请提供文件路径"
    deadline = time.time() + max(1.0, float(timeout))
    last_size = -1
    stable_since = 0.0
    while time.time() < deadline:
        if p.is_file():
            try:
                size = p.stat().st_size
            except OSError:
                size = -1
            now = time.time()
            if size == last_size and size >= 0:
                if stable_since <= 0:
                    stable_since = now
                elif now - stable_since >= float(stable_for):
                    return True, f"文件已就绪：{p}（{size} 字节）"
            else:
                last_size = size
                stable_since = now
        time.sleep(max(0.15, float(interval)))
    if p.is_file():
        return False, f"文件已出现但大小未稳定：{p}"
    return False, f"等待文件超时：{p}"
