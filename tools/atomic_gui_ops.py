# -*- coding: utf-8 -*-
"""原子 GUI 复合动作 — click_and_type / fill_form / click_menu_item / focus_and_hotkey。"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)


def click_and_type(
    text: str = "",
    *,
    x: int | None = None,
    y: int | None = None,
    target: str = "",
    app: str = "",
    clear_first: bool = False,
    virtual: bool = True,
) -> tuple[bool, str]:
    """先点击再输入。可给坐标，或给 target 走 gui_action。"""
    from .keyboard_input import send_keys, type_text

    parts: list[str] = []
    # 点击
    if x is not None and y is not None:
        if virtual:
            from .virtual_mouse import virtual_click

            ok, msg = virtual_click(int(x), int(y))
        else:
            from .mouse_ops import click

            ok, msg = click(int(x), int(y))
        if not ok:
            return False, f"点击失败: {msg}"
        parts.append(msg)
    elif target:
        from .gui_action import gui_action

        r = gui_action(target, app=app)
        if not r.ok:
            return False, f"定位点击失败: {r.message}"
        parts.append(r.message)
    else:
        return False, "请提供 (x,y) 或 target"

    time.sleep(0.15)
    if clear_first:
        send_keys("^a", delay_sec=0.05)
        time.sleep(0.05)
        send_keys("{DEL}", delay_sec=0.05)
        time.sleep(0.05)
    if text:
        ok, msg = type_text(text, delay_sec=0.1)
        if not ok:
            return False, "；".join(parts + [msg])
        parts.append(msg)
    return True, "；".join(parts)


def focus_and_hotkey(
    keys: str,
    *,
    target: str = "",
    x: int | None = None,
    y: int | None = None,
    app: str = "",
) -> tuple[bool, str]:
    """先聚焦（点一下）再发快捷键，适合 iframe/网页内控件。"""
    from .keyboard_input import send_keys
    from .winops import focus_window_by_title

    parts: list[str] = []
    if app:
        ok, msg = focus_window_by_title(app, timeout=8)
        parts.append(msg if ok else f"聚焦失败:{msg}")
        time.sleep(0.1)

    if x is not None and y is not None:
        from .virtual_mouse import virtual_click

        ok, msg = virtual_click(int(x), int(y))
        if not ok:
            from .mouse_ops import click

            ok, msg = click(int(x), int(y))
        if not ok:
            return False, f"聚焦点击失败: {msg}"
        parts.append(msg)
    elif target:
        from .gui_action import gui_action

        r = gui_action(target, app=app)
        if not r.ok:
            return False, f"聚焦失败: {r.message}"
        parts.append(r.message)

    time.sleep(0.12)
    ok, msg = send_keys(keys, delay_sec=0.05)
    parts.append(msg)
    return ok, "；".join(parts)


def fill_form(
    fields: list[dict],
    *,
    app: str = "",
    submit: str = "",
) -> tuple[bool, str]:
    """批量填表。每项: {target|name, value, clear?} 或 {x,y,value}。"""
    if not isinstance(fields, list) or not fields:
        return False, "fields 须为非空数组"
    results: list[str] = []
    for i, f in enumerate(fields[:20], 1):
        if not isinstance(f, dict):
            results.append(f"{i}.无效项")
            return False, "填表中断：\n" + "\n".join(results)
        value = str(f.get("value") or f.get("text") or "")
        target = str(f.get("target") or f.get("name") or f.get("label") or "")
        x = f.get("x")
        y = f.get("y")
        clear = bool(f.get("clear", True))
        ok, msg = click_and_type(
            value,
            x=int(x) if x is not None else None,
            y=int(y) if y is not None else None,
            target=target,
            app=app or str(f.get("app") or ""),
            clear_first=clear,
        )
        results.append(f"{i}.{target or f'({x},{y})'}:{'成功' if ok else '失败'} {msg[:60]}")
        if not ok:
            return False, "填表中断：\n" + "\n".join(results)
        time.sleep(0.12)

    if submit:
        from .gui_action import gui_action

        r = gui_action(submit, app=app)
        results.append(f"提交:{'成功' if r.ok else '失败'} {r.message[:60]}")
        if not r.ok:
            return False, "填表完成但提交失败：\n" + "\n".join(results)

    return True, "填表完成：\n" + "\n".join(results)


def click_menu_item(
    path: str,
    *,
    app: str = "",
    separator: str = ">",
) -> tuple[bool, str]:
    """按「文件 > 打开」路径逐级点菜单。"""
    path = (path or "").strip()
    if not path:
        return False, "请提供菜单路径，如「文件>打开」"
    parts = [p.strip() for p in path.split(separator) if p.strip()]
    if not parts:
        return False, "菜单路径为空"

    if app:
        from .winops import focus_window_by_title

        focus_window_by_title(app, timeout=8)
        time.sleep(0.15)

    from .gui_action import gui_action
    from .uia_ops import click_element

    logs: list[str] = []
    for i, name in enumerate(parts):
        # 优先 UIA MenuItem
        ok, msg = click_element(
            name=name,
            control_type="MenuItem",
            root_window_title=app,
        )
        if not ok:
            ok, msg = click_element(name=name, root_window_title=app)
        if not ok:
            r = gui_action(name, app=app)
            ok, msg = r.ok, r.message
        logs.append(f"{i + 1}.{name}:{'成功' if ok else '失败'} {msg[:50]}")
        if not ok:
            return False, "菜单导航中断：\n" + "\n".join(logs)
        time.sleep(0.25)
    return True, "菜单已点击：\n" + "\n".join(logs)
