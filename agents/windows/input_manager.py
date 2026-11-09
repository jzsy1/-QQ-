"""InputManager — 键鼠输入，必须绑定目标窗口。"""

from __future__ import annotations

from ...tools.execution_result import ExecutionResult, from_tuple
from ...tools.keyboard_input import send_keys, type_text
from ...tools.window_ops import focus_window, window_title_visible


class InputManager:
    """输入操作封装。"""

    def type_plain(self, text: str) -> ExecutionResult:
        content = (text or "").strip()
        if not content:
            return ExecutionResult(
                success=False,
                message="请告诉我要输入什么内容",
                verified=False,
                action="type_text",
            )
        ok, msg = type_text(content, delay_sec=0.2)
        return from_tuple(ok, msg, verified=ok, action="type_text", target=content[:40])

    def focus(self, window_title: str) -> ExecutionResult:
        title = (window_title or "").strip()
        if not title:
            return ExecutionResult(
                success=False,
                message="请提供窗口标题",
                verified=False,
                action="focus_window",
            )
        ok, msg = focus_window(title)
        verified = ok and window_title_visible(title)
        return ExecutionResult(
            success=ok and verified,
            message=msg if verified else f"{msg}，但未能确认窗口「{title}」已置前",
            verified=verified,
            action="focus_window",
            target=title,
        )

    def focus_and_type(self, window_title: str, text: str) -> ExecutionResult:
        title = (window_title or "").strip()
        content = (text or "").strip()
        if not title or not content:
            return ExecutionResult(
                success=False,
                message="请提供窗口标题和要输入的文本",
                verified=False,
                action="type_text",
            )
        ok, msg = focus_window(title)
        if not ok:
            return from_tuple(False, msg, verified=False, action="focus_window", target=title)
        ok2, msg2 = type_text(content)
        verified = ok2 and window_title_visible(title)
        return ExecutionResult(
            success=ok2 and verified,
            message=msg2 if verified else f"{msg2}，但未能确认窗口「{title}」已置前",
            verified=verified,
            action="type_text",
            target=title,
        )

    def hotkey(self, keys: str, *, window_title: str = "") -> ExecutionResult:
        if window_title:
            ok, msg = focus_window(window_title)
            if not ok:
                return from_tuple(False, msg, verified=False, action="hotkey", target=window_title)
        ok, msg = send_keys(keys)
        return from_tuple(ok, msg, verified=ok, action="send_keys", target=keys)

    def send_hotkey(self, keys: str, *, window_title: str = "") -> ExecutionResult:
        return self.hotkey(keys, window_title=window_title)

    def gui_action(
        self,
        target: str,
        *,
        action: str = "click",
        app: str = "",
        button: str = "left",
        double: bool = False,
        partial: bool = True,
        db=None,
    ) -> ExecutionResult:
        from ...tools.gui_action import gui_action as gui_action_impl

        raw = (target or "").strip()
        if not raw:
            return ExecutionResult(
                success=False,
                message="请提供要操作的界面目标文字",
                verified=False,
                action="gui_action",
                target="",
            )
        result = gui_action_impl(
            raw,
            action=action,
            app=app,
            button=button,
            double=double,
            partial=partial,
            db=db,
        )
        return ExecutionResult(
            success=result.ok,
            message=result.message,
            verified=result.ok,
            action="gui_action",
            target=raw,
            extra={"layer": result.layer, "x": result.x, "y": result.y},
        )

    def click_text(
        self,
        text: str,
        *,
        app: str = "",
        button: str = "left",
        double: bool = False,
        partial: bool = True,
    ) -> ExecutionResult:
        from ...tools.vision_click import click_text as vision_click_text

        raw = (text or "").strip()
        if not raw:
            return ExecutionResult(
                success=False,
                message="请提供要点击的文字",
                verified=False,
                action="click_text",
                target="",
            )
        result = vision_click_text(
            raw,
            app=app,
            button=button,
            double=double,
            partial=partial,
        )
        return ExecutionResult(
            success=result.ok,
            message=result.message,
            verified=result.ok,
            action="click_text",
            target=raw,
            extra={"method": result.method, "x": result.x, "y": result.y},
        )

    def find_text_location(
        self,
        text: str,
        *,
        app: str = "",
        partial: bool = True,
    ) -> ExecutionResult:
        from ...tools.vision_click import find_text_location as vision_find

        raw = (text or "").strip()
        if not raw:
            return ExecutionResult(
                success=False,
                message="请提供要查找的文字",
                verified=False,
                action="find_text_location",
                target="",
            )
        ok, msg, pos, method = vision_find(raw, app=app, partial=partial)
        extra: dict = {"method": method}
        if pos:
            extra["x"], extra["y"] = int(pos[0]), int(pos[1])
        message = f"{msg}（策略: {method}）" if ok and pos else msg
        return ExecutionResult(
            success=ok,
            message=message,
            verified=ok,
            action="find_text_location",
            target=raw,
            extra=extra,
        )

    def read_screen_text(
        self,
        *,
        app: str = "",
        region: tuple[int, int, int, int] | None = None,
    ) -> ExecutionResult:
        from ...tools.vision_click import read_screen_text as vision_read

        ok, msg = vision_read(app=app, region=region)
        return ExecutionResult(
            success=ok,
            message=msg,
            verified=ok,
            action="read_screen_text",
            target=(app or "screen")[:40],
        )

    def click_at(
        self,
        x: int,
        y: int,
        *,
        button: str = "left",
        double: bool = False,
        virtual: bool = False,
    ) -> ExecutionResult:
        ix, iy = int(x), int(y)
        if virtual:
            from ...tools.virtual_mouse import virtual_click

            ok, msg = virtual_click(ix, iy, button=button, double=double)
        else:
            from ...tools.vision_click import click_at as vision_click_at

            ok, msg = vision_click_at(ix, iy, button=button, double=double)
        return ExecutionResult(
            success=ok,
            message=msg,
            verified=ok,
            action="click_at",
            target=f"{ix},{iy}",
            extra={"virtual": virtual, "button": button, "double": double},
        )

    def hover_text(
        self,
        text: str,
        *,
        app: str = "",
        partial: bool = True,
    ) -> ExecutionResult:
        from ...tools.vision_click import hover_text as vision_hover

        raw = (text or "").strip()
        if not raw:
            return ExecutionResult(
                success=False,
                message="请提供要悬停的文字",
                verified=False,
                action="hover_text",
                target="",
            )
        ok, msg = vision_hover(raw, app=app, partial=partial)
        return ExecutionResult(
            success=ok,
            message=msg,
            verified=ok,
            action="hover_text",
            target=raw,
        )

    def move_mouse(self, x: int, y: int, *, smooth: bool = False) -> ExecutionResult:
        from ...tools.mouse_ops import move_to

        ok, msg = move_to(int(x), int(y), smooth=smooth)
        return ExecutionResult(
            success=ok,
            message=msg,
            verified=ok,
            action="mouse_move",
            target=f"{int(x)},{int(y)}",
        )

    def mouse_click(
        self,
        x: int | None = None,
        y: int | None = None,
        *,
        button: str = "left",
        double: bool = False,
        virtual: bool = False,
    ) -> ExecutionResult:
        if virtual:
            if x is None or y is None:
                return ExecutionResult(
                    success=False,
                    message="虚拟点击需要提供 x,y 坐标",
                    verified=False,
                    action="mouse_click",
                    target="",
                )
            from ...tools.virtual_mouse import virtual_click

            ok, msg = virtual_click(int(x), int(y), button=button, double=double)
        elif double:
            from ...tools.mouse_ops import double_click

            ok, msg = double_click(int(x) if x is not None else None, int(y) if y is not None else None)
        else:
            from ...tools.mouse_ops import click as mouse_click_impl

            ok, msg = mouse_click_impl(
                int(x) if x is not None else None,
                int(y) if y is not None else None,
                button=button,
            )
        target = f"{x},{y}" if x is not None and y is not None else "cursor"
        return ExecutionResult(
            success=ok,
            message=msg,
            verified=ok,
            action="mouse_click",
            target=target,
            extra={"virtual": virtual, "double": double, "button": button},
        )

    def mouse_drag(
        self,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        *,
        duration: float = 0.4,
    ) -> ExecutionResult:
        from ...tools.mouse_ops import drag

        ok, msg = drag(int(x1), int(y1), int(x2), int(y2), duration=float(duration))
        return ExecutionResult(
            success=ok,
            message=msg,
            verified=ok,
            action="mouse_drag",
            target=f"{int(x1)},{int(y1)}->{int(x2)},{int(y2)}",
        )

    def mouse_scroll(
        self,
        amount: int,
        *,
        x: int | None = None,
        y: int | None = None,
        virtual: bool = False,
    ) -> ExecutionResult:
        if virtual:
            from ...tools.virtual_mouse import virtual_scroll

            ok, msg = virtual_scroll(
                int(amount),
                x=int(x) if x is not None else None,
                y=int(y) if y is not None else None,
            )
            return from_tuple(
                ok,
                msg,
                verified=ok,
                action="mouse_scroll",
                target=str(int(amount)),
                extra={"virtual": True},
            )
        from ...tools.mouse_ops import scroll

        ok, msg = scroll(int(amount), x=x, y=y)
        return ExecutionResult(
            success=ok,
            message=msg,
            verified=ok,
            action="mouse_scroll",
            target=str(int(amount)),
        )

    def get_cursor_pos(self) -> ExecutionResult:
        from ...tools.mouse_ops import get_cursor_pos

        x, y = get_cursor_pos()
        return ExecutionResult(
            success=True,
            message=f"当前鼠标位置: ({x}, {y})",
            verified=True,
            action="get_cursor_pos",
            target=f"{x},{y}",
            extra={"x": x, "y": y},
        )

    def check_occlusion(
        self,
        *,
        x: int | None = None,
        y: int | None = None,
        hwnd: int = 0,
        title: str = "",
    ) -> ExecutionResult:
        from ...tools.occlusion_ops import check_occlusion

        ok, msg = check_occlusion(
            x=x,
            y=y,
            hwnd=int(hwnd or 0),
            title=title,
        )
        target = f"{x},{y}" if x is not None and y is not None else (title or str(hwnd))
        return from_tuple(ok, msg, verified=ok, action="check_occlusion", target=target[:40])

    def virtual_click(
        self,
        x: int,
        y: int,
        *,
        button: str = "left",
        double: bool = False,
    ) -> ExecutionResult:
        from ...tools.virtual_mouse import virtual_click as virtual_click_impl

        ok, msg = virtual_click_impl(int(x), int(y), button=button, double=double)
        return from_tuple(
            ok,
            msg,
            verified=ok,
            action="virtual_click",
            target=f"{int(x)},{int(y)}",
        )

    def virtual_scroll(
        self,
        amount: int,
        *,
        x: int | None = None,
        y: int | None = None,
    ) -> ExecutionResult:
        from ...tools.virtual_mouse import virtual_scroll as virtual_scroll_impl

        ok, msg = virtual_scroll_impl(int(amount), x=x, y=y)
        return from_tuple(ok, msg, verified=ok, action="virtual_scroll", target=str(int(amount)))

    def click_and_type(
        self,
        text: str,
        *,
        x: int | None = None,
        y: int | None = None,
        target: str = "",
        app: str = "",
        clear_first: bool = False,
        virtual: bool = True,
    ) -> ExecutionResult:
        from ...tools.atomic_gui_ops import click_and_type as click_and_type_impl

        ok, msg = click_and_type_impl(
            text,
            x=x,
            y=y,
            target=target,
            app=app,
            clear_first=clear_first,
            virtual=virtual,
        )
        return from_tuple(ok, msg, verified=ok, action="click_and_type", target=(target or text or "")[:40])

    def focus_and_hotkey(
        self,
        keys: str,
        *,
        target: str = "",
        x: int | None = None,
        y: int | None = None,
        app: str = "",
    ) -> ExecutionResult:
        from ...tools.atomic_gui_ops import focus_and_hotkey as focus_and_hotkey_impl

        ok, msg = focus_and_hotkey_impl(
            keys,
            target=target,
            x=x,
            y=y,
            app=app,
        )
        return from_tuple(ok, msg, verified=ok, action="focus_and_hotkey", target=keys[:40])

    def fill_form(
        self,
        fields: list,
        *,
        app: str = "",
        submit: str = "",
    ) -> ExecutionResult:
        from ...tools.atomic_gui_ops import fill_form as fill_form_impl

        if not isinstance(fields, list):
            return ExecutionResult(
                success=False,
                message="fields 须为数组",
                verified=False,
                action="fill_form",
            )
        ok, msg = fill_form_impl(fields, app=app, submit=submit)
        return from_tuple(ok, msg, verified=ok, action="fill_form", target=app[:40] or "form")

    def click_menu_item(
        self,
        path: str,
        *,
        app: str = "",
        separator: str = ">",
    ) -> ExecutionResult:
        from ...tools.atomic_gui_ops import click_menu_item as click_menu_item_impl

        ok, msg = click_menu_item_impl(path, app=app, separator=separator)
        return from_tuple(ok, msg, verified=ok, action="click_menu_item", target=path[:40])
