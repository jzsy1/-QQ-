# -*- coding: utf-8 -*-
"""图像模板匹配 — find_image / click_image（Pillow，可选 OpenCV 加速）。"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_IMAGE_MAP: dict[str, str] = {}


@dataclass
class ImageMatch:
    x: int
    y: int
    width: int
    height: int
    confidence: float

    @property
    def center(self) -> tuple[int, int]:
        return self.x + self.width // 2, self.y + self.height // 2


def register_image(name: str, path: str) -> tuple[bool, str]:
    name = (name or "").strip()
    path = (path or "").strip()
    if not name or not path:
        return False, "需要 name 和 path"
    if not Path(path).is_file():
        return False, f"文件不存在：{path}"
    _IMAGE_MAP[name.lower()] = path
    return True, f"已注册图像别名「{name}」→ {path}"


def _resolve_template(image: str) -> Path | None:
    raw = (image or "").strip()
    if not raw:
        return None
    if raw.lower() in _IMAGE_MAP:
        return Path(_IMAGE_MAP[raw.lower()])
    p = Path(raw).expanduser()
    return p if p.is_file() else None


def _match_cv2(screen, template, confidence: float) -> ImageMatch | None:
    import cv2
    import numpy as np

    sc = cv2.cvtColor(np.array(screen), cv2.COLOR_RGB2BGR)
    tp = cv2.cvtColor(np.array(template), cv2.COLOR_RGB2BGR)
    if tp.shape[0] > sc.shape[0] or tp.shape[1] > sc.shape[1]:
        return None
    res = cv2.matchTemplate(sc, tp, cv2.TM_CCOEFF_NORMED)
    min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(res)
    if max_val < confidence:
        return None
    h, w = tp.shape[:2]
    return ImageMatch(int(max_loc[0]), int(max_loc[1]), int(w), int(h), float(max_val))


def _match_pil(screen, template, confidence: float, step: int = 4) -> ImageMatch | None:
    """无 OpenCV 时的粗粒度 SAD 匹配（归一化为 0~1 近似置信度）。"""
    sw, sh = screen.size
    tw, th = template.size
    if tw > sw or th > sh:
        return None
    screen_l = screen.convert("L")
    templ_l = template.convert("L")
    t_pixels = list(templ_l.getdata())
    t_area = tw * th
    best_score = 1e18
    best_xy = (0, 0)
    step = max(1, int(step))
    # 限制搜索点，避免过慢
    max_checks = 8000
    checks = 0
    for y in range(0, sh - th + 1, step):
        for x in range(0, sw - tw + 1, step):
            crop = screen_l.crop((x, y, x + tw, y + th))
            s_pixels = list(crop.getdata())
            sad = 0
            for a, b in zip(s_pixels, t_pixels):
                sad += abs(a - b)
            if sad < best_score:
                best_score = sad
                best_xy = (x, y)
            checks += 1
            if checks >= max_checks:
                break
        if checks >= max_checks:
            break
    # 平均像素差 0~255 → 置信度
    mean_diff = best_score / max(1, t_area)
    conf = max(0.0, 1.0 - mean_diff / 255.0)
    if conf < confidence:
        return None
    return ImageMatch(best_xy[0], best_xy[1], tw, th, conf)


def find_image(
    image: str,
    *,
    confidence: float = 0.8,
    region: tuple[int, int, int, int] | None = None,
) -> tuple[bool, str, ImageMatch | None]:
    """在屏幕上找模板图，返回中心坐标。"""
    try:
        from PIL import Image, ImageGrab
    except ImportError:
        return False, "缺少 Pillow", None

    path = _resolve_template(image)
    if not path:
        return False, f"找不到模板图「{image}」（可用 register_image 注册别名）", None

    try:
        template = Image.open(path).convert("RGB")
        if region:
            x, y, w, h = region
            screen = ImageGrab.grab(bbox=(x, y, x + w, y + h))
            ox, oy = x, y
        else:
            screen = ImageGrab.grab(all_screens=True)
            # 全屏截图的图像 (0,0) 对应虚拟屏原点（多显示器时可为负），
            # 需加该偏移匹配坐标才是真实屏幕坐标
            try:
                import ctypes

                ox = int(ctypes.windll.user32.GetSystemMetrics(76))  # SM_XVIRTUALSCREEN
                oy = int(ctypes.windll.user32.GetSystemMetrics(77))  # SM_YVIRTUALSCREEN
            except Exception:
                ox, oy = 0, 0
        screen = screen.convert("RGB")
    except Exception as e:
        return False, f"截图/读模板失败: {e}", None

    conf = max(0.5, min(float(confidence), 0.99))
    match = None
    try:
        import cv2  # noqa: F401

        match = _match_cv2(screen, template, conf)
    except Exception:
        match = _match_pil(screen, template, conf)

    if not match:
        return False, f"未找到图像「{path.name}」（阈值 {conf}）", None

    match.x += ox
    match.y += oy
    cx, cy = match.center
    return (
        True,
        f"找到「{path.name}」@({cx},{cy}) 置信 {match.confidence:.2f}",
        match,
    )


def click_image(
    image: str,
    *,
    confidence: float = 0.8,
    button: str = "left",
    double: bool = False,
    virtual: bool = True,
) -> tuple[bool, str]:
    ok, msg, match = find_image(image, confidence=confidence)
    if not ok or not match:
        return False, msg
    cx, cy = match.center
    # 物理点击优先（真实生效），失败再虚拟点击兜底（virtual 控制是否允许兜底）
    from .mouse_ops import click, double_click

    if double:
        cok, cmsg = double_click(cx, cy)
    else:
        cok, cmsg = click(cx, cy, button=button)
    if cok:
        return True, f"{msg}；{cmsg}"
    if virtual:
        from .virtual_mouse import virtual_click

        vok, vmsg = virtual_click(cx, cy, button=button, double=double)
        if vok:
            return True, f"{msg}；{vmsg}"
    return False, f"{msg}；{cmsg}"
