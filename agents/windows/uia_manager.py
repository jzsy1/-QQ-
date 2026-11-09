"""UiaManager — Windows UI Automation 控件查找与交互。"""

from __future__ import annotations

from ...tools.execution_result import ExecutionResult, from_tuple
from ...tools.uia_ops import (
    click_element,
    find_elements,
    get_element_text,
    list_controls,
    set_element_value,
)


class UiaManager:
    """UIA 自动化封装。"""

    def find_elements(
        self,
        *,
        name: str = "",
        automation_id: str = "",
        control_type: str = "",
        class_name: str = "",
        scope: str = "descendants",
        root_window_title: str = "",
        limit: int = 20,
    ) -> ExecutionResult:
        result = find_elements(
            name=name,
            automation_id=automation_id,
            control_type=control_type,
            class_name=class_name,
            scope=scope,
            root_window_title=root_window_title,
            limit=limit,
        )
        if not result.ok:
            return ExecutionResult(
                success=False,
                message=result.message,
                verified=False,
                action="uia_find_element",
                target=(name or automation_id or control_type)[:40],
            )
        if not result.elements:
            return ExecutionResult(
                success=True,
                message="未找到匹配控件",
                verified=True,
                action="uia_find_element",
                target=(name or automation_id or control_type)[:40],
            )
        lines = [el.to_line() for el in result.elements]
        return ExecutionResult(
            success=True,
            message=f"找到 {len(result.elements)} 个控件：\n" + "\n".join(lines),
            verified=True,
            action="uia_find_element",
            target=(name or automation_id or control_type)[:40],
            extra={"count": len(result.elements)},
        )

    def click_element(
        self,
        *,
        name: str = "",
        automation_id: str = "",
        control_type: str = "",
        class_name: str = "",
        root_window_title: str = "",
        button: str = "left",
        double: bool = False,
    ) -> ExecutionResult:
        ok, msg = click_element(
            name=name,
            automation_id=automation_id,
            control_type=control_type,
            class_name=class_name,
            root_window_title=root_window_title,
            button=button,
            double=double,
        )
        return from_tuple(
            ok,
            msg,
            verified=ok,
            action="uia_click_element",
            target=(name or automation_id)[:40],
        )

    def get_element_text(
        self,
        *,
        name: str = "",
        automation_id: str = "",
        control_type: str = "",
        root_window_title: str = "",
    ) -> ExecutionResult:
        ok, msg = get_element_text(
            name=name,
            automation_id=automation_id,
            control_type=control_type,
            root_window_title=root_window_title,
        )
        return from_tuple(
            ok,
            msg,
            verified=ok,
            action="uia_get_text",
            target=(name or automation_id)[:40],
        )

    def list_controls(
        self,
        *,
        root_window_title: str = "",
        scope: str = "children",
        control_type: str = "",
        limit: int = 50,
    ) -> ExecutionResult:
        ok, msg = list_controls(
            root_window_title=root_window_title,
            scope=scope,
            control_type=control_type,
            limit=limit,
        )
        return from_tuple(
            ok,
            msg,
            verified=ok,
            action="uia_list_controls",
            target=(root_window_title or control_type)[:40],
        )

    def set_element_value(
        self,
        value: str,
        *,
        name: str = "",
        automation_id: str = "",
        control_type: str = "edit",
        root_window_title: str = "",
    ) -> ExecutionResult:
        ok, msg = set_element_value(
            value=value,
            name=name,
            automation_id=automation_id,
            control_type=control_type,
            root_window_title=root_window_title,
        )
        return from_tuple(
            ok,
            msg,
            verified=ok,
            action="uia_set_value",
            target=(name or automation_id)[:40],
        )
