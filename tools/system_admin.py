"""Windows 系统管理 — 注册表、服务、计划任务"""

from __future__ import annotations

import os
import re

from .privilege import is_admin, is_system, run_shell_admin
from .winops import powershell

_HIVE_RE = re.compile(r"^(HKCU|HKLM|HKCR|HKU|HKCC):\\", re.I)
# HKCU 任何权限都可写；其它 hive（HKLM/HKCR/HKU/HKCC）需管理员/SYSTEM
_USER_HIVE = ("HKCU:", "HKEY_CURRENT_USER\\")


def _normalize_reg_path(path: str) -> str:
    p = (path or "").strip()
    BS = chr(92)  # 反斜杠，避免 Edit 工具转义问题
    # 标准化各种写法为 PowerShell 风格 HKCU:\\Path
    _PREFIX_MAP = [
        ("HKEY_CURRENT_USER", "HKCU"),
        ("HKEY_LOCAL_MACHINE", "HKLM"),
        ("HKEY_CLASSES_ROOT", "HKCR"),
        ("HKEY_USERS", "HKU"),
        ("HKEY_CURRENT_CONFIG", "HKCC"),
        ("HKCU", "HKCU"),
        ("HKLM", "HKLM"),
        ("HKCR", "HKCR"),
        ("HKU", "HKU"),
        ("HKCC", "HKCC"),
    ]
    upper = p.upper()
    matched = False
    for long, short in _PREFIX_MAP:
        prefix_long = long + BS
        prefix_short = short + ":" + BS
        # 已是 PowerShell 风格 HKCU:\\
        if upper.startswith(prefix_short):
            matched = True
            break
        # HKEY_CURRENT_USER\\ 或 HKCU\\（单/多反斜杠）
        if upper.startswith(prefix_long):
            p = short + ":" + BS + p[len(prefix_long):]
            matched = True
            break
    # 仍无 hive 前缀，默认 HKCU
    if not matched:
        p = "HKCU:" + BS + p.lstrip(BS)
    return p


def read_registry(path: str, name: str = "") -> tuple[bool, str]:
    path = _normalize_reg_path(path)
    name = (name or "").strip()
    safe_path = path.replace("'", "''")
    if name:
        safe_name = name.replace("'", "''")
        script = (
            f"try {{ "
            f"$v = Get-ItemProperty -LiteralPath '{safe_path}' "
            f"-Name '{safe_name}' -ErrorAction Stop | Select-Object -ExpandProperty '{safe_name}'; "
            f"Write-Output ($v | Out-String).Trim() "
            f"}} catch {{ Write-Error $_.Exception.Message; exit 1 }}"
        )
    else:
        script = (
            f"try {{ "
            f"Get-ItemProperty -LiteralPath '{safe_path}' -ErrorAction Stop | "
            f"Format-List | Out-String -Width 200 "
            f"}} catch {{ Write-Error $_.Exception.Message; exit 1 }}"
        )
    out = powershell(script, timeout=15)
    lower = (out or "").lower()
    if "does not exist" in lower or "找不到" in lower or "cannot find" in lower:
        return False, f"读取注册表失败: {out.strip()}"
    if not out:
        return True, "（键存在但值为空，或无属性）"
    if len(out) > 2000:
        out = out[:2000] + "\n…（已截断）"
    return True, out.strip()


def write_registry(path: str, name: str, value: str, value_type: str = "String", *, task_id: str | None = None, cancel_check=None) -> tuple[bool, str]:
    path = _normalize_reg_path(path)
    name = (name or "").strip()
    if not name:
        return False, "请提供注册表值名称"

    allowed_types = {"String", "ExpandString", "DWord", "QWord", "MultiString"}
    vtype = (value_type or "String").strip()
    if vtype not in allowed_types:
        return False, f"不支持的类型: {vtype}"

    is_user_hive = any(path.upper().startswith(h.upper()) for h in _USER_HIVE)
    need_admin = not is_user_hive

    safe_path = path.replace("'", "''")
    safe_name = name.replace("'", "''")
    # 把值经环境变量传入，避免命令注入；提权路径无法继承环境变量，故用单引号转义后内联
    safe_value = (value or "").replace("'", "''")
    if need_admin and not (is_admin() or is_system()):
        # 需要管理员且当前不是 → 走提权执行（会弹 UAC）
        script = (
            f"$parent = Split-Path -LiteralPath '{safe_path}'; "
            f"if ($parent) {{ New-Item -Path $parent -Force | Out-Null }}; "
            f"New-Item -Path '{safe_path}' -Force | Out-Null; "
            f"Set-ItemProperty -LiteralPath '{safe_path}' -Name '{safe_name}' "
            f"-Value '{safe_value}' -Type {vtype}; Write-Output 'OK'"
        )
        ok, out = run_shell_admin(script, timeout=60, task_id=task_id, cancel_check=cancel_check)
        if "OK" in (out or ""):
            return True, f"已（管理员）写入 {path}\\{name}"
        return False, (out or "写入失败，可能 UAC 被拒绝").strip()[:200]

    env = {**os.environ, "JV_REG_VAL": value}
    script = (
        f"$parent = Split-Path -LiteralPath '{safe_path}'; "
        f"if ($parent) {{ New-Item -Path $parent -Force | Out-Null }}; "
        f"New-Item -Path '{safe_path}' -Force | Out-Null; "
        f"Set-ItemProperty -LiteralPath '{safe_path}' -Name '{safe_name}' "
        f"-Value $env:JV_REG_VAL -Type {vtype}; "
        f"Write-Output 'OK'"
    )
    out = powershell(script, env=env, timeout=15)
    if "OK" in (out or ""):
        return True, f"已写入 {path}\\{name}"
    lower = (out or "").lower()
    if "denied" in lower or "拒绝" in (out or ""):
        return False, f"写入 {path} 被拒绝，需要管理员权限（可先 elevate_self）"
    return False, (out or "写入失败").strip()


def list_services(status: str = "all", limit: int = 40) -> tuple[bool, str]:
    limit = max(5, min(int(limit), 80))
    status = (status or "all").strip().lower()
    filter_clause = ""
    if status == "running":
        filter_clause = " | Where-Object { $_.Status -eq 'Running' }"
    elif status == "stopped":
        filter_clause = " | Where-Object { $_.Status -eq 'Stopped' }"

    script = f"""
Get-Service -ErrorAction SilentlyContinue{filter_clause} |
  Sort-Object Status, DisplayName |
  Select-Object -First {limit} Name, Status, DisplayName |
  ForEach-Object {{ Write-Output ("{{0}} | {{1}} | {{2}}" -f $_.Name, $_.Status, $_.DisplayName) }}
"""
    out = powershell(script, timeout=20)
    lines = [ln.strip() for ln in (out or "").splitlines() if ln.strip()]
    if not lines:
        return True, "未找到匹配的服务"
    return True, "服务列表：\n" + "\n".join(lines)


def control_service(name: str, action: str, *, task_id: str | None = None, cancel_check=None) -> tuple[bool, str]:
    name = (name or "").strip()
    action = (action or "").strip().lower()
    if not name:
        return False, "请提供服务名称"
    if action not in ("start", "stop", "restart"):
        return False, "action 必须是 start、stop 或 restart"

    # 仅拦截会瞬间使系统崩溃/掉出会话的核心服务，其余交由口头确认把关
    fatal = {"rpcss", "dcomlaunch", "lsass", "plugplay", "power"}
    if name.lower() in fatal and action in ("stop", "restart"):
        return False, f"{name} 是系统核心服务，停止会导致系统立即崩溃，已拒绝"

    safe_name = name.replace("'", "''")
    if action == "start":
        cmd = f"Start-Service -Name '{safe_name}' -ErrorAction Stop; Write-Output OK"
    elif action == "stop":
        cmd = f"Stop-Service -Name '{safe_name}' -Force -ErrorAction Stop; Write-Output OK"
    else:
        cmd = f"Restart-Service -Name '{safe_name}' -Force -ErrorAction Stop; Write-Output OK"

    out = powershell(cmd, timeout=30)
    if "OK" in (out or ""):
        labels = {"start": "已启动", "stop": "已停止", "restart": "已重启"}
        return True, f"{labels[action]}服务 {name}"
    lower = (out or "").lower()
    # 权限不足时自动提权重试
    if ("denied" in lower or "拒绝" in (out or "") or "cannot open" in lower) and not (
        is_admin() or is_system()
    ):
        ok, eout = run_shell_admin(cmd, timeout=40, task_id=task_id, cancel_check=cancel_check)
        if "OK" in (eout or ""):
            labels = {"start": "已启动", "stop": "已停止", "restart": "已重启"}
            return True, f"（管理员）{labels[action]}服务 {name}"
        return False, f"服务操作失败: {(eout or '未知错误').strip()[:160]}"
    return False, f"服务操作失败: {(out or '未知错误').strip()}"


def list_scheduled_tasks(limit: int = 30) -> tuple[bool, str]:
    limit = max(5, min(int(limit), 60))
    script = f"""
Get-ScheduledTask -ErrorAction SilentlyContinue |
  Select-Object -First {limit} TaskName, State, TaskPath |
  ForEach-Object {{ Write-Output ("{{0}} | {{1}} | {{2}}" -f $_.TaskName, $_.State, $_.TaskPath) }}
"""
    out = powershell(script, timeout=20)
    lines = [ln.strip() for ln in (out or "").splitlines() if ln.strip()]
    if not lines:
        return True, "未找到计划任务"
    return True, "计划任务：\n" + "\n".join(lines)


def run_scheduled_task(task_name: str) -> tuple[bool, str]:
    task_name = (task_name or "").strip()
    if not task_name:
        return False, "请提供计划任务名称"
    safe = task_name.replace("'", "''")
    script = f"Start-ScheduledTask -TaskName '{safe}' -ErrorAction Stop; Write-Output 'OK'"
    out = powershell(script, timeout=30)
    if "OK" in (out or ""):
        return True, f"已触发计划任务: {task_name}"
    return False, (out or "触发失败").strip()
