"""UI Automation (UIA) — 按名称/类型查找控件并交互。

使用 .NET UIAutomationClient（System.Windows.Automation）通过 PowerShell 调用。
这是 Windows 内置的无障碍框架，能精确识别大多数 Win32/WPF/WinForms/UWP 控件，
比坐标点击更稳定，且不依赖 OCR。

核心能力：
- find_element：按名称/自动化ID/控件类型查找元素，返回坐标
- click_element：查找并点击元素
- get_element_text：获取元素文本
- list_controls：列出窗口下的控件树
- invoke / toggle / select：调用控件模式
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .winops import powershell

logger = logging.getLogger(__name__)

# UIA 控件类型（常用）
_CONTROL_TYPES = {
    "button", "checkbox", "combobox", "edit", "hyperlink",
    "image", "list", "listitem", "menu", "menubar", "menuitem",
    "progressbar", "radiobutton", "scrollbar", "slider", "tab",
    "tabitem", "text", "toolbar", "tooltip", "tree", "treeitem",
    "window", "pane", "group", "datagram", "dataitem",
    "document", "splitbutton", "thumb", "custom",
}

# UIA 树遍历范围
_TREE_SCOPE = {
    "self": "Self",
    "children": "Children",
    "descendants": "Descendants",
    "subtree": "Subtree",
}


@dataclass
class UIAElement:
    """UIA 元素信息。"""
    name: str = ""
    automation_id: str = ""
    control_type: str = ""
    class_name: str = ""
    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0
    is_enabled: bool = True
    is_offscreen: bool = False
    text: str = ""

    @property
    def center(self) -> tuple[int, int]:
        return self.x + self.width // 2, self.y + self.height // 2

    def to_line(self) -> str:
        parts = [f"type={self.control_type}", f"name={self.name}"]
        if self.automation_id:
            parts.append(f"id={self.automation_id}")
        parts.append(f"pos=({self.x},{self.y},{self.width}x{self.height})")
        if self.text and self.text != self.name:
            parts.append(f"text={self.text[:40]}")
        return " | ".join(parts)


@dataclass
class UIASearchResult:
    ok: bool
    message: str
    elements: list[UIAElement] = field(default_factory=list)


# 共用的 UIA 类型加载脚本（PowerShell）
_UIA_PREAMBLE = """
Add-Type -AssemblyName 'UIAutomationClient, Version=4.0.0.0, Culture=neutral, PublicKeyToken=31bf3856ad364e35' -ErrorAction Stop
Add-Type -AssemblyName 'UIAutomationTypes, Version=4.0.0.0, Culture=neutral, PublicKeyToken=31bf3856ad364e35' -ErrorAction Stop
$SWA = 'System.Windows.Automation'
"""


def _escape_ps_string(s: str) -> str:
    """转义 PowerShell 单引号字符串。"""
    return (s or "").replace("'", "''")


def _build_condition_ps(
    name: str = "",
    automation_id: str = "",
    control_type: str = "",
    class_name: str = "",
    partial: bool = True,
) -> str:
    """构建 UIA 查找条件的 PowerShell 片段。"""
    parts: list[str] = []
    if name:
        safe = _escape_ps_string(name)
        if partial:
            # 注意：PropertyCondition 对字符串是精确匹配，IgnoreCase 仅让比较忽略大小写，
            # 并不做子串匹配。真正的子串匹配由 find_elements 在取回元素后用 IndexOf 完成。
            parts.append(
                f"new-object System.Windows.Automation.PropertyCondition("
                f"[System.Windows.Automation.AutomationElement]::NameProperty, "
                f"'{safe}', [System.Windows.Automation.ConditionFlags]::IgnoreCase)"
            )
        else:
            parts.append(
                f"new-object System.Windows.Automation.PropertyCondition("
                f"[System.Windows.Automation.AutomationElement]::NameProperty, '{safe}')"
            )
    if automation_id:
        safe = _escape_ps_string(automation_id)
        parts.append(
            f"new-object System.Windows.Automation.PropertyCondition("
            f"[System.Windows.Automation.AutomationElement]::AutomationIdProperty, '{safe}')"
        )
    if control_type:
        cts = [c.strip() for c in control_type.split(",") if c.strip()]
        if len(cts) == 1:
            ct = cts[0][0].upper() + cts[0][1:]
            parts.append(
                f"new-object System.Windows.Automation.PropertyCondition("
                f"[System.Windows.Automation.AutomationElement]::ControlTypeProperty, "
                f"[System.Windows.Automation.ControlType]::{ct})"
            )
        elif len(cts) > 1:
            ct_conds = ", ".join(
                f"(new-object System.Windows.Automation.PropertyCondition("
                f"[System.Windows.Automation.AutomationElement]::ControlTypeProperty, "
                f"[System.Windows.Automation.ControlType]::{c[0].upper() + c[1:]}))"
                for c in cts
            )
            parts.append(
                f"new-object System.Windows.Automation.OrCondition({ct_conds})"
            )
    if class_name:
        safe = _escape_ps_string(class_name)
        parts.append(
            f"new-object System.Windows.Automation.PropertyCondition("
            f"[System.Windows.Automation.AutomationElement]::ClassNameProperty, '{safe}')"
        )

    if not parts:
        return "[System.Windows.Automation.Condition]::TrueCondition"
    if len(parts) == 1:
        return parts[0]
    return "new-object System.Windows.Automation.AndCondition(" + ", ".join(parts) + ")"


def _format_element_ps(var: str = "$e") -> str:
    """生成 PowerShell 片段：将元素格式化为字符串行。"""
    return f"""
      try {{
        $r = $e.GetCurrentPropertyValue([System.Windows.Automation.AutomationElement]::BoundingRectangleProperty)
        $x = [int]$r.X; $y = [int]$r.Y; $w = [int]$r.Width; $h = [int]$r.Height
        $cx = $x + [int]($w/2); $cy = $y + [int]($h/2)
        $nm = try {{ $e.GetCurrentPropertyValue([System.Windows.Automation.AutomationElement]::NameProperty) }} catch {{ '' }}
        $aid = try {{ $e.GetCurrentPropertyValue([System.Windows.Automation.AutomationElement]::AutomationIdProperty) }} catch {{ '' }}
        $ctObj = try {{ $e.GetCurrentPropertyValue([System.Windows.Automation.AutomationElement]::ControlTypeProperty) }} catch {{ $null }}
        $ct = if ($ctObj) {{ $ctObj.ProgrammaticName -replace 'ControlType\\.','' }} else {{ '' }}
        $cn = try {{ $e.GetCurrentPropertyValue([System.Windows.Automation.AutomationElement]::ClassNameProperty) }} catch {{ '' }}
        $en = try {{ $e.GetCurrentPropertyValue([System.Windows.Automation.AutomationElement]::IsEnabledProperty) }} catch {{ 'True' }}
        $of = try {{ $e.GetCurrentPropertyValue([System.Windows.Automation.AutomationElement]::IsOffscreenProperty) }} catch {{ 'False' }}
        $tx = ''
        try {{
          $pat = $e.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)
          if ($pat) {{ $tx = $pat.Current.Value }}
        }} catch {{}}
        Write-Output ("TYPE={{0}}|NAME={{1}}|AID={{2}}|CLS={{3}}|RECT={{4}},{{5}},{{6}},{{7}}|EN={{8}}|OFF={{9}}|TEXT={{10}}" -f $ct, $nm, $aid, $cn, $x, $y, $w, $h, $en, $of, $tx)
      }} catch {{
        Write-Output ("ERR|" + $_.Exception.Message)
      }}
"""


def _parse_element_line(line: str) -> UIAElement | None:
    """解析 PowerShell 输出的元素行。"""
    line = line.strip()
    if not line or line.startswith("ERR|"):
        return None
    parts = line.split("|")
    el = UIAElement()
    for p in parts:
        if "=" not in p:
            continue
        k, v = p.split("=", 1)
        k = k.strip().upper()
        v = v
        if k == "TYPE":
            el.control_type = v
        elif k == "NAME":
            el.name = v
        elif k == "AID":
            el.automation_id = v
        elif k == "CLS":
            el.class_name = v
        elif k == "RECT":
            try:
                x, y, w, h = v.split(",")
                el.x, el.y = int(x), int(y)
                el.width, el.height = int(w), int(h)
            except (ValueError, IndexError):
                pass
        elif k == "EN":
            el.is_enabled = v.lower() == "true"
        elif k == "OFF":
            el.is_offscreen = v.lower() == "true"
        elif k == "TEXT":
            el.text = v
    return el


def find_elements(
    name: str = "",
    automation_id: str = "",
    control_type: str = "",
    class_name: str = "",
    *,
    scope: str = "descendants",
    root_window_title: str = "",
    limit: int = 20,
    timeout: float = 8,
) -> UIASearchResult:
    """按条件查找 UIA 元素。

    Args:
        name: 元素名称（支持子串匹配，忽略大小写）
        automation_id: 自动化 ID（精确匹配）
        control_type: 控件类型（如 Button/Edit/List）
        class_name: 类名
        scope: 搜索范围 self/children/descendants/subtree
        root_window_title: 限定根窗口标题（不传则从桌面根搜索）
        limit: 最多返回的元素数
        timeout: PowerShell 超时秒数
    """
    if not any([name, automation_id, control_type, class_name]):
        return UIASearchResult(False, "至少提供一个查找条件（name/automation_id/control_type/class_name）")

    scope = (scope or "descendants").lower()
    if scope not in _TREE_SCOPE:
        return UIASearchResult(False, f"不支持的 scope: {scope}")
    scope_ps = f"[System.Windows.Automation.TreeScope]::{_TREE_SCOPE[scope]}"

    condition = _build_condition_ps(name, automation_id, control_type, class_name)
    # 仅按 name 查找且需要子串语义时，UIA 原生 PropertyCondition 无法子串匹配，
    # 退化为"取回全部元素 + 强制忽略大小写的包含过滤"，以保"名称包含即命中"的语义。
    use_substring = bool(name) and not (automation_id or control_type or class_name)
    limit = max(1, min(int(limit), 100))

    # 根元素：桌面或指定窗口
    if root_window_title:
        safe_title = _escape_ps_string(root_window_title)
        root_ps = (
            f"$rootCond = new-object System.Windows.Automation.PropertyCondition("
            f"[System.Windows.Automation.AutomationElement]::NameProperty, "
            f"'{safe_title}', [System.Windows.Automation.ConditionFlags]::IgnoreCase); "
            f"$root = [System.Windows.Automation.AutomationElement]::FindFirst("
            f"[System.Windows.Automation.TreeScope]::Children, $rootCond); "
            "if (-not $root) { Write-Output 'ROOT_NOT_FOUND'; exit 0 }"
        )
    else:
        root_ps = "$root = [System.Windows.Automation.AutomationElement]::RootElement"

    if use_substring:
        safe_name = _escape_ps_string(name)
        find_ps = (
            f"$search = '{safe_name}'\n"
            f"$found = $root.FindAll({scope_ps}, "
            "[System.Windows.Automation.AutomationElement]::TrueCondition)\n"
            "if (-not $found -or $found.Count -eq 0) { Write-Output 'NONE'; exit 0 }\n"
            "$found = @($found | Where-Object { $_.Current.Name -and "
            "$_.Current.Name.IndexOf($search, "
            "[System.StringComparison]::OrdinalIgnoreCase) -ge 0 })\n"
            "if ($found.Count -eq 0) { Write-Output 'NONE'; exit 0 }\n"
        )
    else:
        find_ps = (
            f"$cond = {condition}\n"
            f"$found = $root.FindAll({scope_ps}, $cond)\n"
            "if (-not $found -or $found.Count -eq 0) { Write-Output 'NONE'; exit 0 }\n"
        )

    script = f"""
{_UIA_PREAMBLE}
{root_ps}
{find_ps}
$count = 0
foreach ($e in $found) {{
  if ($count -ge {limit}) {{ break }}
{_format_element_ps("$e")}
  $count++
}}
"""
    try:
        out = powershell(script, timeout=timeout)
    except Exception as e:
        return UIASearchResult(False, f"UIA 查询失败: {e}")

    out = (out or "").strip()
    if not out:
        return UIASearchResult(False, "UIA 查询无输出")
    if out == "NONE":
        return UIASearchResult(True, "未找到匹配的控件", [])
    if out == "ROOT_NOT_FOUND":
        return UIASearchResult(False, f"未找到标题含「{root_window_title}」的根窗口")

    elements: list[UIAElement] = []
    for line in out.splitlines():
        el = _parse_element_line(line)
        if el:
            elements.append(el)

    if not elements:
        return UIASearchResult(False, "解析元素失败")
    return UIASearchResult(True, f"找到 {len(elements)} 个匹配控件", elements)


def click_element(
    name: str = "",
    automation_id: str = "",
    control_type: str = "",
    class_name: str = "",
    *,
    root_window_title: str = "",
    button: str = "left",
    double: bool = False,
) -> tuple[bool, str]:
    """查找并点击 UIA 元素。

    优先尝试 InvokePattern（最可靠），失败则回退到坐标点击。
    """
    result = find_elements(
        name=name, automation_id=automation_id,
        control_type=control_type, class_name=class_name,
        root_window_title=root_window_title,
        limit=1, timeout=8,
    )
    if not result.ok or not result.elements:
        return False, result.message

    el = result.elements[0]
    if el.is_offscreen or el.width <= 0 or el.height <= 0:
        return False, f"控件「{el.name or el.control_type}」不可见或尺寸为 0"

    # 先尝试 InvokePattern（程序化点击，不依赖坐标）
    invoke_ok, invoke_msg = _try_invoke_pattern(
        name=name, automation_id=automation_id,
        control_type=control_type, class_name=class_name,
        root_window_title=root_window_title,
    )
    if invoke_ok:
        return True, f"已通过 InvokePattern 激活「{el.name or el.control_type}」"

    cx, cy = el.center
    # 坐标回退前检查遮挡
    try:
        from .occlusion_ops import check_point_occlusion

        occ = check_point_occlusion(cx, cy, expected_title=root_window_title or "")
        if occ.occluded and root_window_title:
            return False, f"控件中心被遮挡，放弃点击：{occ.message}"
    except Exception:
        pass

    # 物理点击优先（真实生效），失败再用虚拟点击兜底（PostMessage 常被现代应用忽略）
    from .virtual_mouse import virtual_click
    from .mouse_ops import click as mouse_click, double_click, is_click_blocked_by_uipi

    blocked = is_click_blocked_by_uipi(cx, cy)
    if blocked:
        return False, blocked
    if double:
        ok, msg = double_click(cx, cy)
    else:
        ok, msg = mouse_click(cx, cy, button=button)
    if ok:
        return True, f"已点击「{el.name or el.control_type}」于 ({cx}, {cy})"
    vok, vmsg = virtual_click(cx, cy, button=button, double=double)
    if vok:
        return True, f"已虚拟点击「{el.name or el.control_type}」于 ({cx}, {cy})"
    return False, msg


def _try_invoke_pattern(
    name: str = "",
    automation_id: str = "",
    control_type: str = "",
    class_name: str = "",
    *,
    root_window_title: str = "",
) -> tuple[bool, str]:
    """尝试通过 InvokePattern 程序化激活控件。"""
    condition = _build_condition_ps(name, automation_id, control_type, class_name)
    if root_window_title:
        safe_title = _escape_ps_string(root_window_title)
        root_ps = (
            f"$rootCond = new-object System.Windows.Automation.PropertyCondition("
            f"[System.Windows.Automation.AutomationElement]::NameProperty, "
            f"'{safe_title}', [System.Windows.Automation.ConditionFlags]::IgnoreCase); "
            f"$root = [System.Windows.Automation.AutomationElement]::FindFirst("
            f"[System.Windows.Automation.TreeScope]::Children, $rootCond); "
            "if (-not $root) { exit 1 }"
        )
    else:
        root_ps = "$root = [System.Windows.Automation.AutomationElement]::RootElement"

    script = f"""
{_UIA_PREAMBLE}
{root_ps}
$cond = {condition}
$e = $root.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $cond)
if (-not $e) {{ Write-Output 'NOT_FOUND'; exit 0 }}
try {{
  $pat = $e.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
  if ($pat) {{
    $pat.Invoke()
    Write-Output 'OK'
  }} else {{
    Write-Output 'NO_PATTERN'
  }}
}} catch {{
  Write-Output ('ERR|' + $_.Exception.Message)
}}
"""
    try:
        out = powershell(script, timeout=10)
        out = (out or "").strip()
        if out == "OK":
            return True, "InvokePattern 已调用"
        return False, out
    except Exception as e:
        return False, f"InvokePattern 失败: {e}"


def get_element_text(
    name: str = "",
    automation_id: str = "",
    control_type: str = "",
    *,
    root_window_title: str = "",
) -> tuple[bool, str]:
    """获取控件的文本内容。"""
    result = find_elements(
        name=name, automation_id=automation_id,
        control_type=control_type,
        root_window_title=root_window_title,
        limit=1, timeout=8,
    )
    if not result.ok or not result.elements:
        return False, result.message
    el = result.elements[0]
    text = el.text or el.name
    if not text:
        return True, "（控件无文本）"
    return True, text


def list_controls(
    root_window_title: str = "",
    *,
    scope: str = "children",
    control_type: str = "",
    limit: int = 50,
) -> tuple[bool, str]:
    """列出窗口下的控件。

    Args:
        root_window_title: 窗口标题关键词（不传则列桌面顶层窗口）
        scope: children/descendants
        control_type: 只列指定类型
        limit: 最多返回数
    """
    scope = (scope or "children").lower()
    if scope not in _TREE_SCOPE:
        return False, f"不支持的 scope: {scope}"
    scope_ps = f"[System.Windows.Automation.TreeScope]::{_TREE_SCOPE[scope]}"
    limit = max(1, min(int(limit), 200))

    if root_window_title:
        safe_title = _escape_ps_string(root_window_title)
        root_ps = (
            f"$rootCond = new-object System.Windows.Automation.PropertyCondition("
            f"[System.Windows.Automation.AutomationElement]::NameProperty, "
            f"'{safe_title}', [System.Windows.Automation.ConditionFlags]::IgnoreCase); "
            f"$root = [System.Windows.Automation.AutomationElement]::FindFirst("
            f"[System.Windows.Automation.TreeScope]::Children, $rootCond); "
            "if (-not $root) { Write-Output 'ROOT_NOT_FOUND'; exit 0 }"
        )
    else:
        root_ps = "$root = [System.Windows.Automation.AutomationElement]::RootElement"

    if control_type:
        ct = control_type.strip().capitalize()
        cond_ps = (
            f"$cond = new-object System.Windows.Automation.PropertyCondition("
            f"[System.Windows.Automation.AutomationElement]::ControlTypeProperty, "
            f"[System.Windows.Automation.ControlType]::{ct})"
        )
    else:
        cond_ps = "$cond = [System.Windows.Automation.Condition]::TrueCondition"

    script = f"""
{_UIA_PREAMBLE}
{root_ps}
{cond_ps}
$found = $root.FindAll({scope_ps}, $cond)
if (-not $found -or $found.Count -eq 0) {{
  Write-Output 'NONE'
  exit 0
}}
$count = 0
foreach ($e in $found) {{
  if ($count -ge {limit}) {{ break }}
{_format_element_ps("$e")}
  $count++
}}
"""
    try:
        out = powershell(script, timeout=20)
    except Exception as e:
        return False, f"UIA 列出控件失败: {e}"

    out = (out or "").strip()
    if not out:
        return False, "UIA 查询无输出"
    if out == "NONE":
        return True, "未找到控件"
    if out == "ROOT_NOT_FOUND":
        return False, f"未找到标题含「{root_window_title}」的根窗口"

    elements: list[UIAElement] = []
    for line in out.splitlines():
        el = _parse_element_line(line)
        if el:
            elements.append(el)

    if not elements:
        return False, "解析元素失败"
    lines = [el.to_line() for el in elements]
    return True, f"找到 {len(elements)} 个控件：\n" + "\n".join(lines)


def set_element_value(
    value: str,
    name: str = "",
    automation_id: str = "",
    control_type: str = "edit",
    *,
    root_window_title: str = "",
) -> tuple[bool, str]:
    """设置编辑框的值（ValuePattern）。"""
    condition = _build_condition_ps(name, automation_id, control_type)
    if root_window_title:
        safe_title = _escape_ps_string(root_window_title)
        root_ps = (
            f"$rootCond = new-object System.Windows.Automation.PropertyCondition("
            f"[System.Windows.Automation.AutomationElement]::NameProperty, "
            f"'{safe_title}', [System.Windows.Automation.ConditionFlags]::IgnoreCase); "
            f"$root = [System.Windows.Automation.AutomationElement]::FindFirst("
            f"[System.Windows.Automation.TreeScope]::Children, $rootCond); "
            "if (-not $root) { Write-Output 'ROOT_NOT_FOUND'; exit 0 }"
        )
    else:
        root_ps = "$root = [System.Windows.Automation.AutomationElement]::RootElement"

    # 用环境变量传值，避免转义问题
    import os
    env = {"KEKESI_UIA_VALUE": value or ""}
    script = f"""
{_UIA_PREAMBLE}
{root_ps}
$cond = {condition}
$e = $root.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $cond)
if (-not $e) {{ Write-Output 'NOT_FOUND'; exit 0 }}
try {{
  $pat = $e.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)
  if ($pat) {{
    $pat.SetValue($env:KEKESI_UIA_VALUE)
    Write-Output 'OK'
  }} else {{
    Write-Output 'NO_PATTERN'
  }}
}} catch {{
  Write-Output ('ERR|' + $_.Exception.Message)
}}
"""
    try:
        out = powershell(script, env=env, timeout=10)
        out = (out or "").strip()
        if out == "OK":
            return True, f"已设置值: {value[:40]}"
        if out == "NO_PATTERN":
            return False, "控件不支持 ValuePattern（可能不是编辑框）"
        if out == "NOT_FOUND":
            return False, "未找到匹配的编辑框"
        return False, out
    except Exception as e:
        return False, f"设置值失败: {e}"
