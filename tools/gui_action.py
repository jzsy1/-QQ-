"""GUI 分层动作 — L0 UI 缓存 → L1 UIA → L2 OCR（桌面，不含视觉云端 L3 强制）。"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from .mouse_ops import click as mouse_click, double_click
from .ocr_ops import ocr_find_text
from .uia_ops import find_elements, click_element as uia_click_element

logger = logging.getLogger(__name__)

_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_NUM_RE = re.compile(r"[零一二两三四五六七八九十百千]+")


def _cn_to_int(s: str) -> int | None:
    """中文数字转阿拉伯（支持到千位，够集数/页码/章节用）。"""
    total, section, num = 0, 0, 0
    for ch in s:
        if ch in _CN_DIGITS:
            num = _CN_DIGITS[ch]
        elif ch == "十":
            section += (num or 1) * 10
            num = 0
        elif ch == "百":
            section += (num or 1) * 100
            num = 0
        elif ch == "千":
            section += (num or 1) * 1000
            num = 0
        else:
            return None
    total = section + num
    return total if total > 0 or s == "零" else None


def _target_variants(target: str) -> list[str]:
    """生成目标文字变体：原文 + 中文数字→阿拉伯数字版（ASR 归一，通用）。"""
    variants = [target]
    converted = target
    for m in _CN_NUM_RE.finditer(target):
        val = _cn_to_int(m.group(0))
        if val is not None:
            converted = converted.replace(m.group(0), str(val), 1)
    if converted != target:
        variants.append(converted)
    return variants


@dataclass
class GuiActionResult:
    ok: bool
    message: str
    layer: str = ""  # L0 / L1 / L2 / none
    x: int = 0
    y: int = 0


def _cache_key(target: str, app: str = "") -> str:
    return f"{(app or '').strip().lower()}::{(target or '').strip().lower()}"


def _lookup_cache(db, key: str) -> dict | None:
    if not db:
        return None
    with db.connect() as conn:
        row = conn.execute(
            """SELECT * FROM ui_cache
               WHERE cache_key = ? AND confidence >= 0.4 AND fail_count < 3""",
            (key,),
        ).fetchone()
        return dict(row) if row else None


# 裸坐标直点的最低置信度（automation_id 路径会先经 UIA 复验，不受此限）
_RAW_COORD_MIN_CONFIDENCE = 0.75


def _save_cache(
    db,
    *,
    key: str,
    app: str,
    target: str,
    method: str,
    x: int,
    y: int,
    control_type: str = "",
    automation_id: str = "",
    rel_x: float = 0.0,
    rel_y: float = 0.0,
) -> None:
    if not db:
        return
    if rel_x == 0.0 and rel_y == 0.0 and app and x > 0 and y > 0:
        reg = _window_region(app)
        if reg and reg[2] > 0 and reg[3] > 0:
            rel_x = round((x - reg[0]) / reg[2], 4)
            rel_y = round((y - reg[1]) / reg[3], 4)
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id, hit_count FROM ui_cache WHERE cache_key = ?", (key,)
        ).fetchone()
        if row:
            conn.execute(
                """UPDATE ui_cache SET
                   method = ?, x = ?, y = ?, control_type = ?, automation_id = ?,
                   rel_x = ?, rel_y = ?,
                   confidence = MIN(1.0, confidence + 0.08),
                   hit_count = hit_count + 1, fail_count = 0,
                   last_used = datetime('now')
                   WHERE id = ?""",
                (method, x, y, control_type, automation_id, rel_x, rel_y, row["id"]),
            )
        else:
            conn.execute(
                """INSERT INTO ui_cache
                   (cache_key, app_title, target_text, method, x, y,
                    control_type, automation_id, rel_x, rel_y, confidence, hit_count, last_used)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0.7, 1, datetime('now'))""",
                (key, app, target, method, x, y, control_type, automation_id, rel_x, rel_y),
            )
        conn.commit()


def _mark_cache_fail(db, key: str) -> None:
    if not db:
        return
    with db.connect() as conn:
        conn.execute(
            """UPDATE ui_cache SET
               fail_count = fail_count + 1,
               confidence = MAX(0.0, confidence - 0.15)
               WHERE cache_key = ?""",
            (key,),
        )
        conn.commit()


def _click_xy(x: int, y: int, *, double: bool, button: str) -> tuple[bool, str]:
    """物理点击（SendInput，真实生效）优先，失败再用虚拟点击兜底。

    旧逻辑虚拟点击优先：PostMessage 对 UWP/Electron/微信等常被忽略，
    返回"成功"但实际没点上，故调整为物理点击优先。
    """
    try:
        from .mouse_ops import is_click_blocked_by_uipi

        blocked = is_click_blocked_by_uipi(x, y)
        if blocked:
            return False, blocked
    except Exception:
        pass
    if double:
        ok, msg = double_click(x, y)
    else:
        ok, msg = mouse_click(x, y, button=button)
    if ok:
        return True, msg
    try:
        from .virtual_mouse import virtual_click

        vok, vmsg = virtual_click(x, y, button=button, double=double)
        if vok:
            return True, f"{msg}；虚拟点击兜底成功：{vmsg}"
        return False, f"物理点击失败: {msg}；虚拟点击失败: {vmsg}"
    except Exception as e:
        return False, f"物理点击失败: {msg}；虚拟点击异常: {e}"


def _ocr_relocate_near(target: str, x: int, y: int) -> tuple[bool, str, tuple[int, int] | None]:
    """点击前的屏幕重定位：在缓存坐标附近搜索目标，返回**当前最新位置**。

    原则"每次点击前先看一次屏幕"：缓存坐标仅作搜索范围提示，
    实际点击使用本次识别到的位置，窗口移动/布局变化后也能点准。
    """
    try:
        from .ocr_ops import ocr_find_text

        pad_x, pad_y = 160, 90
        region = (int(x) - pad_x, int(y) - pad_y, pad_x * 2, pad_y * 2)
        return ocr_find_text(target, region=region)
    except Exception:
        return False, "", None


def _window_region(app: str) -> tuple[int, int, int, int] | None:
    """按标题查找可见窗口的物理矩形 (x, y, w, h)，供 OCR 限定区域搜索。

    精确标题命中优先；找不到返回 None（调用方退回全屏搜索）。
    """
    app = (app or "").strip()
    if not app:
        return None
    try:
        import ctypes
        import ctypes.wintypes as wt

        user32 = ctypes.windll.user32
        best: tuple[int, int, int, int, int] | None = None

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
        def _cb(hwnd, _):
            nonlocal best
            if not user32.IsWindowVisible(hwnd):
                return True
            n = user32.GetWindowTextLengthW(hwnd)
            if n <= 0:
                return True
            buf = ctypes.create_unicode_buffer(n + 2)
            user32.GetWindowTextW(hwnd, buf, n + 2)
            title = buf.value
            needle = app.lower()
            tlow = title.lower()
            if needle in tlow:
                r = wt.RECT()
                if user32.GetWindowRect(hwnd, ctypes.byref(r)):
                    w, h = int(r.right - r.left), int(r.bottom - r.top)
                    if w > 10 and h > 10:
                        cand = (int(r.left), int(r.top), w, h, 1 if tlow == needle else 0)
                        if best is None or cand[4] > best[4]:
                            best = cand
            return True

        user32.EnumWindows(_cb, 0)
        if best:
            return best[0], best[1], best[2], best[3]
    except Exception as e:
        logger.debug("窗口区域查询失败: %s", e)
    return None


def gui_action(
    target: str,
    *,
    action: str = "click",
    app: str = "",
    double: bool = False,
    button: str = "left",
    partial: bool = True,
    db=None,
) -> GuiActionResult:
    """统一 GUI 入口：按文字/控件名点击（L0→L1→L2）。"""
    target = (target or "").strip()
    if not target:
        return GuiActionResult(False, "请提供要点击的文字或控件名", "none")

    action = (action or "click").strip().lower()
    if action not in ("click", "double_click", "hover"):
        action = "click"
    if action == "double_click":
        double = True

    key = _cache_key(target, app)

    # —— L0：UI 缓存坐标（含窗口内相对比例坐标 rel_x/rel_y 自适应） ——
    cached = _lookup_cache(db, key)
    if cached and action in ("click", "double_click"):
        x, y = int(cached.get("x") or 0), int(cached.get("y") or 0)
        rx, ry = float(cached.get("rel_x") or 0.0), float(cached.get("rel_y") or 0.0)
        if app and 0.0 < rx < 1.0 and 0.0 < ry < 1.0:
            win_reg = _window_region(app)
            if win_reg and win_reg[2] > 0 and win_reg[3] > 0:
                x = int(win_reg[0] + rx * win_reg[2])
                y = int(win_reg[1] + ry * win_reg[3])
        if x > 0 and y > 0:
            # 若有 automation_id，优先再走一遍 UIA 验证
            aid = (cached.get("automation_id") or "").strip()
            if aid:
                try:
                    ok, msg = uia_click_element(
                        automation_id=aid,
                        root_window_title=app,
                        double=double,
                    )
                    if ok:
                        _save_cache(
                            db,
                            key=key,
                            app=app,
                            target=target,
                            method="uia",
                            x=x,
                            y=y,
                            automation_id=aid,
                        )
                        return GuiActionResult(True, f"L0缓存命中(UIA验证)：{msg}", "L0", x, y)
                except Exception:
                    pass
            # 无 automation_id 的裸坐标：缓存坐标只作搜索提示，先看屏幕重定位，
            # 点的是本次识别到的最新位置；重定位失败则落到 L1/L2 重新识别
            if float(cached.get("confidence") or 0) >= _RAW_COORD_MIN_CONFIDENCE:
                ok, msg, pos = _ocr_relocate_near(
                    str(cached.get("target_text") or target), x, y
                )
                if ok and pos:
                    fx, fy = int(pos[0]), int(pos[1])
                    cok, cmsg = _click_xy(fx, fy, double=double, button=button)
                    if cok:
                        _save_cache(
                            db,
                            key=key,
                            app=app,
                            target=target,
                            method="ocr",
                            x=fx,
                            y=fy,
                        )
                        return GuiActionResult(
                            True, f"L0缓存引导重定位后点击：{cmsg}", "L0", fx, fy
                        )
                    _mark_cache_fail(db, key)

    variants = _target_variants(target)

    # —— L1：UIA ——
    for variant in variants:
        try:
            result = find_elements(
                name=variant,
                root_window_title=app,
                scope="descendants",
                limit=5,
                timeout=8,
            )
            if result.ok and result.elements:
                for el in result.elements:
                    if el.is_enabled and not el.is_offscreen and el.width > 0 and el.height > 0:
                        cx, cy = el.center
                        if action == "hover":
                            from .mouse_ops import move_to

                            ok, msg = move_to(cx, cy)
                        elif not double and button == "left":
                            from .uia_ops import _try_invoke_pattern

                            inv_ok, inv_msg = _try_invoke_pattern(
                                name=el.name or variant,
                                automation_id=el.automation_id,
                                control_type=el.control_type,
                                class_name=el.class_name,
                                root_window_title=app,
                            )
                            if inv_ok:
                                ok, msg = True, f"后台 Invoke 激活「{el.name or variant}」"
                            else:
                                ok, msg = _click_xy(cx, cy, double=double, button=button)
                        else:
                            ok, msg = _click_xy(cx, cy, double=double, button=button)
                        if ok:
                            _save_cache(
                                db,
                                key=key,
                                app=app,
                                target=target,
                                method="uia",
                                x=cx,
                                y=cy,
                                control_type=el.control_type,
                                automation_id=el.automation_id,
                            )
                            return GuiActionResult(
                                True, f"L1 UIA：{msg}", "L1", cx, cy
                            )
        except Exception as e:
            logger.debug("gui_action L1 失败: %s", e)

    # —— L2：OCR ——（app 指定时仅搜窗口内；未命中明确报错，不做全屏兜底，
    # 防止误点屏幕其他区域的同名文字。app 未指定或窗口找不到时才全屏搜索）
    last_msg = ""
    region = _window_region(app) if app else None
    scoped = bool(app) and region is not None
    try:
        for variant in variants:
            ok, msg, pos = ocr_find_text(variant, region=region, partial=partial)
            last_msg = msg
            if ok and pos:
                x, y = int(pos[0]), int(pos[1])
                if action == "hover":
                    from .mouse_ops import move_to

                    cok, cmsg = move_to(x, y)
                else:
                    cok, cmsg = _click_xy(x, y, button=button, double=double)
                if cok:
                    _save_cache(
                        db,
                        key=key,
                        app=app,
                        target=target,
                        method="ocr",
                        x=x,
                        y=y,
                    )
                    scope = "窗口内" if scoped else "全屏"
                    return GuiActionResult(True, f"L2 OCR（{scope}）：{cmsg}", "L2", x, y)
                return GuiActionResult(False, f"L2 找到文字但点击失败：{cmsg}", "L2")
        if scoped:
            return GuiActionResult(
                False,
                f"未在「{app}」窗口内找到「{target}」。"
                "若目标在弹窗/子窗口中，可去掉 app 参数重试全屏搜索。",
                "none",
            )
        return GuiActionResult(
            False,
            f"未找到「{target}」（UIA/OCR均未命中）：{last_msg}。"
            "若目标可能在列表深处，可先 mouse_scroll 滚动再重试。",
            "none",
        )
    except Exception as e:
        return GuiActionResult(False, f"GUI 操作失败：{e}", "none")
