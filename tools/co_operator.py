"""Co-Operator — 第二操作者执行引擎：

1. 焦点借还协议（borrow_foreground_focus）：
   等待用户键鼠微空闲 → 备份原前台窗口句柄(HWND)、鼠标位置、剪贴板 →
   极速完成前台交互 → 立即还原原窗口焦点、鼠标坐标与剪贴板，不打断用户手头工作。
2. UIA + OCR 融合 Set-of-Mark 标注（inspect_window_som）：
   一次调用拉取窗口内可交互 UIA 控件 + 局部 OCR 文字块，去重后分配数字编号 [#1], [#2]…
   并在截图上绘制编号框，支持按编号直接点击/输入。
3. 连续宏动作流（execute_gui_macro）：
   允许一次看屏后连续下发「点搜索框 → 输入文字 → 回车 → 点发送」多步微动作，
   优先走后台 UIA Invoke/ValuePattern，必须前台时自动套用焦点借还协议，
   并将成功命中的控件坐标按窗口相对比例（rel_x, rel_y）写入 ui_cache 形成肌肉记忆。
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import logging
import os
import sys
import tempfile
import time
from typing import Any, Iterator

from ..safety.activity_guard import mark_agent_control, seconds_since_last_input
from .gui_action import (
    _cache_key,
    _click_xy,
    _save_cache,
    _window_region,
    gui_action,
)
from .keyboard_input import send_keys, type_text
from .mouse_ops import get_cursor_pos, move_to
from .ocr_ops import ocr_screen
from .uia_ops import (
    UIAElement,
    _try_invoke_pattern,
    find_elements,
    set_element_value,
)
from .window_ops import focus_window, minimize_window
from .winops import get_clipboard, set_clipboard

logger = logging.getLogger(__name__)

_INTERACTIVE_UIA_TYPES = {
    "button",
    "edit",
    "checkbox",
    "combobox",
    "hyperlink",
    "listitem",
    "menuitem",
    "tabitem",
    "radiobutton",
    "document",
    "splitbutton",
}


@dataclass
class SomItem:
    """Set-of-Mark 融合标注项。"""

    id: int
    label: str
    kind: str  # "uia" | "ocr"
    control_type: str
    automation_id: str
    class_name: str
    x: int
    y: int
    width: int
    height: int
    window_title: str = ""
    rel_x: float = 0.0
    rel_y: float = 0.0

    def to_line(self) -> str:
        tag = f"UIA:{self.control_type}" if self.kind == "uia" else "OCR"
        aid = f" id={self.automation_id}" if self.automation_id else ""
        return f"[#{self.id}] [{tag}{aid}] {self.label[:40]} @({self.x},{self.y})"


# 最近一次 inspect_window_som 生成的编号映射表
_SOM_MAP: dict[int, SomItem] = {}
_SOM_WINDOW: str = ""


def get_som_map() -> dict[int, SomItem]:
    return dict(_SOM_MAP)


def set_som_map_for_test(items: list[SomItem], window_title: str = "") -> None:
    global _SOM_MAP, _SOM_WINDOW
    _SOM_MAP = {it.id: it for it in items}
    _SOM_WINDOW = window_title


# ----------------------------------------------------------------------
# 1. 焦点借还协议（Focus Borrow & Restore）
# ----------------------------------------------------------------------


def _get_foreground_hwnd() -> int:
    if sys.platform != "win32":
        return 0
    try:
        import ctypes

        return int(ctypes.windll.user32.GetForegroundWindow() or 0)
    except Exception:
        return 0


def _restore_foreground_hwnd(hwnd: int) -> bool:
    if sys.platform != "win32" or not hwnd:
        return False
    try:
        import ctypes

        user32 = ctypes.windll.user32
        if not user32.IsWindow(hwnd):
            return False
        return bool(user32.SetForegroundWindow(hwnd))
    except Exception:
        return False


def _restore_cursor(pos: tuple[int, int]) -> bool:
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        return bool(ctypes.windll.user32.SetCursorPos(int(pos[0]), int(pos[1])))
    except Exception:
        return False


@contextmanager
def borrow_foreground_focus(
    *,
    wait_idle_sec: float = 1.0,
    max_wait_sec: float = 2.5,
    restore_focus: bool = True,
    restore_cursor: bool = True,
    restore_clipboard: bool = False,
) -> Iterator[dict[str, Any]]:
    """借用前台焦点与鼠标，操作完成后自动归还给用户先前的窗口与坐标。"""
    if wait_idle_sec > 0 and max_wait_sec > 0:
        deadline = time.time() + max_wait_sec
        while time.time() < deadline:
            idle = seconds_since_last_input()
            if idle is None or idle >= wait_idle_sec:
                break
            time.sleep(0.08)

    mark_agent_control(15.0)
    orig_hwnd = _get_foreground_hwnd()
    orig_cursor = get_cursor_pos() if restore_cursor else (0, 0)
    orig_clip = ""
    if restore_clipboard:
        try:
            orig_clip = get_clipboard() or ""
        except Exception:
            orig_clip = ""

    state = {
        "orig_hwnd": orig_hwnd,
        "orig_cursor": orig_cursor,
    }
    try:
        yield state
    finally:
        if restore_clipboard and orig_clip:
            try:
                set_clipboard(orig_clip)
            except Exception:
                pass
        if restore_cursor and orig_cursor != (0, 0):
            _restore_cursor(orig_cursor)
        if restore_focus and orig_hwnd:
            curr = _get_foreground_hwnd()
            if curr != orig_hwnd:
                _restore_foreground_hwnd(orig_hwnd)


# ----------------------------------------------------------------------
# 2. UIA + OCR 融合 Set-of-Mark（SoM）
# ----------------------------------------------------------------------


def _active_window_title() -> str:
    if sys.platform != "win32":
        return ""
    try:
        import ctypes

        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return ""
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(n + 2)
        user32.GetWindowTextW(hwnd, buf, n + 2)
        return buf.value.strip()
    except Exception:
        return ""


def _collect_uia_candidates(window_title: str, limit: int = 40) -> list[UIAElement]:
    """拉取窗口内可交互或有明确名称的 UIA 控件（合并为单次 PowerShell UIA 查询，避免 7 次冷启动）。"""
    types_order = ("Button", "Edit", "ListItem", "TabItem", "MenuItem", "Hyperlink", "CheckBox")
    res = find_elements(
        name="",
        control_type=",".join(types_order),
        root_window_title=window_title,
        scope="descendants",
        limit=limit,
        timeout=8,
    )
    if res.ok and res.elements:
        rank = {ct.lower(): i for i, ct in enumerate(types_order)}
        elems = sorted(
            res.elements,
            key=lambda e: rank.get((e.control_type or "").lower(), len(types_order)),
        )
        return elems[:limit]
    return []


def _near_existing(cx: int, cy: int, items: list[SomItem], tol: int = 20) -> bool:
    for it in items:
        if abs(cx - it.x) <= tol and abs(cy - it.y) <= tol:
            return True
    return False


def _annotate_som_image(
    region: tuple[int, int, int, int] | None,
    items: list[SomItem],
) -> str:
    """在窗口截图上画出 [#id] 标记框，保存到临时文件供多模态或排查使用。"""
    try:
        from PIL import ImageDraw, ImageGrab
    except ImportError:
        return ""
    try:
        if region:
            rx, ry, rw, rh = region
            img = ImageGrab.grab(bbox=(rx, ry, rx + rw, ry + rh)).convert("RGB")
            ox, oy = rx, ry
        else:
            img = ImageGrab.grab(all_screens=True).convert("RGB")
            ox, oy = 0, 0
        draw = ImageDraw.Draw(img)
        for it in items:
            bx = max(0, it.x - it.width // 2 - ox)
            by = max(0, it.y - it.height // 2 - oy)
            bw = max(12, it.width)
            bh = max(12, it.height)
            color = "#00c853" if it.kind == "uia" else "#ff5252"
            draw.rectangle([bx, by, bx + bw, by + bh], outline=color, width=2)
            tag = f"#{it.id}"
            draw.rectangle([bx, max(0, by - 14), bx + 8 * len(tag) + 4, by], fill=color)
            draw.text((bx + 2, max(0, by - 13)), tag, fill="#ffffff")
        out_path = os.path.join(tempfile.gettempdir(), "kekesi_som.png")
        img.save(out_path, "PNG")
        return out_path
    except Exception as e:
        logger.debug("SoM 标注绘图失败: %s", e)
        return ""


def inspect_window_som(
    window_title: str = "",
    *,
    include_ocr: bool = True,
    annotate_image: bool = True,
    max_elements: int = 40,
) -> tuple[bool, str, dict[str, Any]]:
    """融合 UIA 控件树与局部 OCR，生成带 [#编号] 的可交互目标清单。"""
    global _SOM_MAP, _SOM_WINDOW
    title = (window_title or "").strip() or _active_window_title()
    region = _window_region(title) if title else None

    som_items: list[SomItem] = []
    next_id = 1

    # 1) UIA 控件优先（带语义类型与 AutomationId，支持后台 Invoke）
    try:
        uia_els = _collect_uia_candidates(title, limit=max_elements)
    except Exception as e:
        logger.debug("SoM UIA 采集失败: %s", e)
        uia_els = []

    for el in uia_els:
        if not el.is_enabled or el.is_offscreen or el.width <= 0 or el.height <= 0:
            continue
        label = (el.name or el.text or el.automation_id or "").strip()
        ct_low = (el.control_type or "").lower()
        # Edit 框即使无文字也保留（常为搜索框/消息输入框）
        if not label and ct_low != "edit":
            continue
        cx, cy = el.center
        if _near_existing(cx, cy, som_items):
            continue
        rel_x, rel_y = 0.0, 0.0
        if region and region[2] > 0 and region[3] > 0:
            rel_x = round((cx - region[0]) / region[2], 4)
            rel_y = round((cy - region[1]) / region[3], 4)
        som_items.append(
            SomItem(
                id=next_id,
                label=label or f"<{el.control_type}>",
                kind="uia",
                control_type=el.control_type or "Control",
                automation_id=el.automation_id or "",
                class_name=el.class_name or "",
                x=cx,
                y=cy,
                width=el.width,
                height=el.height,
                window_title=title,
                rel_x=rel_x,
                rel_y=rel_y,
            )
        )
        next_id += 1
        if len(som_items) >= max_elements:
            break

    # 2) OCR 补充自绘 UI 区域（如微信/Electron 内部文字按钮）
    if include_ocr and len(som_items) < max_elements:
        try:
            ocr_res = ocr_screen(region=region)
            if ocr_res.ok and ocr_res.lines:
                for ln in ocr_res.lines:
                    txt = (ln.text or "").strip()
                    if not txt:
                        continue
                    cx, cy = ln.center
                    if cx <= 0 and cy <= 0:
                        continue
                    if _near_existing(cx, cy, som_items, tol=22):
                        continue
                    w = int(sum(wd.width for wd in ln.words)) if ln.words else 48
                    h = int(max((wd.height for wd in ln.words), default=20))
                    rel_x, rel_y = 0.0, 0.0
                    if region and region[2] > 0 and region[3] > 0:
                        rel_x = round((cx - region[0]) / region[2], 4)
                        rel_y = round((cy - region[1]) / region[3], 4)
                    som_items.append(
                        SomItem(
                            id=next_id,
                            label=txt,
                            kind="ocr",
                            control_type="Text",
                            automation_id="",
                            class_name="",
                            x=cx,
                            y=cy,
                            width=w,
                            height=h,
                            window_title=title,
                            rel_x=rel_x,
                            rel_y=rel_y,
                        )
                    )
                    next_id += 1
                    if len(som_items) >= max_elements:
                        break
        except Exception as e:
            logger.debug("SoM OCR 补充失败: %s", e)

    _SOM_MAP = {it.id: it for it in som_items}
    _SOM_WINDOW = title

    img_path = _annotate_som_image(region, som_items) if annotate_image and som_items else ""
    if not som_items:
        return (
            False,
            f"未在窗口「{title or '当前屏幕'}」中识别到可交互目标",
            {"window": title, "count": 0, "image": img_path},
        )

    lines = [it.to_line() for it in som_items]
    header = (
        f"窗口「{title or '当前屏幕'}」共标注 {len(som_items)} 个目标"
        "（可用 execute_gui_macro 按 id 一次连续执行多步点击/输入）："
    )
    return (
        True,
        header + "\n" + "\n".join(lines),
        {"window": title, "count": len(som_items), "image": img_path},
    )


# ----------------------------------------------------------------------
# 3. 连续宏动作流（Macro Action Executor + UI Muscle Memory）
# ----------------------------------------------------------------------


def _click_som_item(
    item: SomItem,
    *,
    double: bool = False,
    button: str = "left",
    db=None,
) -> tuple[bool, str]:
    """点击 SoM 编号项：UIA 项优先后台 InvokePattern，失败或 OCR 项走物理/虚拟点击，并写入肌肉记忆。"""
    app = item.window_title or _SOM_WINDOW
    if item.kind == "uia" and not double and button == "left":
        inv_ok, inv_msg = _try_invoke_pattern(
            name=item.label if not item.label.startswith("<") else "",
            automation_id=item.automation_id,
            control_type=item.control_type,
            class_name=item.class_name,
            root_window_title=app,
        )
        if inv_ok:
            if db and item.label and not item.label.startswith("<"):
                _save_cache(
                    db,
                    key=_cache_key(item.label, app),
                    app=app,
                    target=item.label,
                    method="uia",
                    x=item.x,
                    y=item.y,
                    control_type=item.control_type,
                    automation_id=item.automation_id,
                    rel_x=item.rel_x,
                    rel_y=item.rel_y,
                )
            return True, f"后台 Invoke 激活 #{item.id}「{item.label}」"

    ok, msg = _click_xy(item.x, item.y, double=double, button=button)
    if ok and db and item.label and not item.label.startswith("<"):
        _save_cache(
            db,
            key=_cache_key(item.label, app),
            app=app,
            target=item.label,
            method=item.kind,
            x=item.x,
            y=item.y,
            control_type=item.control_type,
            automation_id=item.automation_id,
            rel_x=item.rel_x,
            rel_y=item.rel_y,
        )
    return ok, f"点击 #{item.id}「{item.label}」：{msg}"


def execute_gui_macro(
    steps: list[dict[str, Any]],
    *,
    window_title: str = "",
    borrow_focus: bool = True,
    stop_on_error: bool = True,
    db=None,
) -> tuple[bool, str]:
    """一次性连续执行多步 GUI 微动作，支持按 SoM #编号 或文字目标操作，并自动借还焦点。

    每步格式示例：
      - {"op": "click", "id": 1} 或 {"op": "click", "target": "搜索"}
      - {"op": "double_click", "id": 2}
      - {"op": "type", "text": "老张", "id": 1, "enter": true}
      - {"op": "key", "keys": "{ENTER}"}
      - {"op": "wait", "ms": 300}
      - {"op": "focus", "window": "微信"}
      - {"op": "minimize", "window": "微信"}
    """
    if not isinstance(steps, list) or not steps:
        return False, "steps 必须是非空动作列表"
    app = (window_title or _SOM_WINDOW or "").strip()
    logs: list[str] = []
    all_ok = True

    with borrow_foreground_focus(
        restore_focus=borrow_focus,
        restore_cursor=borrow_focus,
        restore_clipboard=False,
    ):
        if app and any(
            str(s.get("op") or "").lower() in ("type", "key", "hotkey")
            for s in steps
            if isinstance(s, dict)
        ):
            focus_window(app)

        for idx, raw in enumerate(steps[:12], 1):
            if not isinstance(raw, dict):
                continue
            op = str(raw.get("op") or raw.get("action") or "click").strip().lower()
            som_id = raw.get("id")
            target = str(raw.get("target") or "").strip()
            step_app = str(raw.get("app") or raw.get("window") or app).strip()

            if op in ("click", "double_click"):
                double = op == "double_click" or bool(raw.get("double"))
                button = str(raw.get("button") or "left")
                if som_id is not None and int(som_id) in _SOM_MAP:
                    ok, msg = _click_som_item(
                        _SOM_MAP[int(som_id)], double=double, button=button, db=db
                    )
                elif target:
                    res = gui_action(
                        target,
                        action="double_click" if double else "click",
                        app=step_app,
                        button=button,
                        db=db,
                    )
                    ok, msg = res.ok, res.message
                else:
                    ok, msg = False, f"未找到编号 #{som_id} 且未提供 target"

            elif op == "type":
                text = str(raw.get("text") or "")
                enter = bool(raw.get("enter", False))
                used_bg = False
                if som_id is not None and int(som_id) in _SOM_MAP:
                    item = _SOM_MAP[int(som_id)]
                    if (
                        item.kind == "uia"
                        and item.control_type.lower() == "edit"
                        and not enter
                    ):
                        vok, vmsg = set_element_value(
                            text,
                            name=item.label if not item.label.startswith("<") else "",
                            automation_id=item.automation_id,
                            control_type="Edit",
                            root_window_title=step_app,
                        )
                        if vok:
                            ok, msg = True, f"后台填入 #{item.id}：{vmsg}"
                            used_bg = True
                    if not used_bg:
                        _click_som_item(item, db=db)
                elif target:
                    gui_action(target, app=step_app, db=db)

                if not used_bg:
                    ok, msg = type_text(text, delay_sec=0.12)
                    if ok and enter:
                        kok, kmsg = send_keys("{ENTER}", delay_sec=0.08)
                        msg = f"{msg} + 回车({'成功' if kok else kmsg})"

            elif op in ("key", "hotkey", "send_keys"):
                keys = str(raw.get("keys") or raw.get("key") or "").strip()
                ok, msg = send_keys(keys, delay_sec=0.1)

            elif op == "wait":
                ms = max(0, min(int(raw.get("ms") or 200), 5000))
                time.sleep(ms / 1000.0)
                ok, msg = True, f"等待 {ms}ms"

            elif op == "focus":
                ok, msg = focus_window(step_app)

            elif op == "minimize":
                ok, msg = minimize_window(step_app)

            elif op == "hover":
                if som_id is not None and int(som_id) in _SOM_MAP:
                    it = _SOM_MAP[int(som_id)]
                    ok, msg = move_to(it.x, it.y)
                elif target:
                    res = gui_action(target, action="hover", app=step_app, db=db)
                    ok, msg = res.ok, res.message
                else:
                    ok, msg = False, "hover 需要 id 或 target"
            else:
                ok, msg = False, f"未知动作 op={op}"

            logs.append(f"{idx}.{op}: {'成功' if ok else '失败'} ({msg})")
            if not ok:
                all_ok = False
                if stop_on_error:
                    return False, "宏动作中断：\n" + "\n".join(logs)

    if not logs:
        return False, "没有可执行的有效步骤"
    status = "宏动作全部完成" if all_ok else "宏动作部分完成"
    return all_ok, f"{status}（共 {len(logs)} 步）：\n" + "\n".join(logs)
