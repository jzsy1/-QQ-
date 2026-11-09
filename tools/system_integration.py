"""深度融入 Windows 11 — 开机自启、服务化常驻、夺取资源所有权。

- install_autostart：注册计划任务，开机/登录即启动，并以最高权限（管理员或 SYSTEM）运行。
  这是让助手「融入系统、常驻、免每次 UAC」最稳妥的方式。
- install_system_service：注册真正的 Windows 服务（LocalSystem，auto 启动）。
- take_ownership / grant_full_control：夺取受 TrustedInstaller 保护的文件/目录所有权。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from .privilege import is_admin, run_shell_admin
from .winops import powershell

_TASK_NAME = "KekesiAssistant"


def _repo_root() -> Path:
    """g:\\kks —— kekesi 包与 assistant-x-openclaw 的共同父目录。"""
    return Path(__file__).resolve().parents[2]


def detect_launcher() -> dict:
    """侦测语音助手真正的启动方式。

    返回 {execute, argument, workdir, kind, command}：
    - kind=bat：通过 scripts/start.bat 启动（含 cwd、CosyVoice、main.py 编排，最稳）
    - kind=python：venv python + src/main.py
    - kind=current：兜底用当前进程可执行文件
    """
    # 1. 环境变量显式覆盖
    override = os.environ.get("KEKESI_AUTOSTART_CMD", "").strip()
    if override:
        return {
            "execute": "cmd.exe",
            "argument": f'/c start "Kekesi" /min {override}',
            "workdir": str(_repo_root()),
            "kind": "override",
            "command": override,
        }

    root = _repo_root()
    assistant = root / "assistant-x-openclaw"
    start_bat = assistant / "scripts" / "start.bat"
    if start_bat.is_file():
        return {
            "execute": "cmd.exe",
            "argument": f'/c start "Kekesi" /min "{start_bat}"',
            "workdir": str(assistant),
            "kind": "bat",
            "command": str(start_bat),
        }

    venv_py = assistant / "venv" / "Scripts" / "pythonw.exe"
    if not venv_py.is_file():
        venv_py = assistant / "venv" / "Scripts" / "python.exe"
    main_py = assistant / "src" / "main.py"
    if venv_py.is_file() and main_py.is_file():
        return {
            "execute": str(venv_py),
            "argument": f'"{main_py}"',
            "workdir": str(assistant),
            "kind": "python",
            "command": f'"{venv_py}" "{main_py}"',
        }

    # 兜底：当前进程
    exe = sys.executable
    return {
        "execute": exe,
        "argument": "",
        "workdir": str(Path(exe).parent),
        "kind": "current",
        "command": f'"{exe}"',
    }


def _current_user() -> str:
    domain = os.environ.get("USERDOMAIN") or os.environ.get("COMPUTERNAME") or ""
    user = os.environ.get("USERNAME") or ""
    if domain and user:
        return f"{domain}\\{user}"
    return user


def _run_privileged(script: str, timeout: int = 120, *, task_id: str | None = None, cancel_check=None) -> tuple[bool, str]:
    """需要管理员的脚本：已是管理员直接跑，否则提权执行。"""
    # THREE_LAYER_CALLER_V1: unify through run_shell_admin so admin fast-path
    # also registers proc and respects cancel_check.
    return run_shell_admin(script, timeout=timeout, task_id=task_id, cancel_check=cancel_check)


# ----------------------------------------------------------------------------
# 开机自启（计划任务）
# ----------------------------------------------------------------------------
def install_autostart(scope: str = "admin", command: str = "", *, task_id: str | None = None, cancel_check=None) -> tuple[bool, str]:
    """注册开机自启计划任务（针对语音助手主程序）。

    scope='admin'：登录时以当前用户 + 最高权限（管理员）启动，运行在交互桌面会话，
                   可正常使用麦克风/音频/界面，且免每次 UAC（推荐）。
    scope='system'：开机时以 NT AUTHORITY\\SYSTEM 在 session 0 后台启动，权限最高，
                   但无桌面/麦克风/扬声器，仅适合纯后台无人值守，语音助手不要用。
    """
    scope = (scope or "admin").strip().lower()

    # 侦测真正的启动方式（默认 start.bat），允许显式覆盖
    if command and command.strip():
        execute = "cmd.exe"
        argument = f'/c start "Kekesi" /min {command.strip()}'
        workdir = str(_repo_root())
        kind = "custom"
    else:
        info = detect_launcher()
        execute, argument, workdir, kind = (
            info["execute"],
            info["argument"],
            info["workdir"],
            info["kind"],
        )

    se = execute.replace("'", "''")
    sa = argument.replace("'", "''")
    sw = workdir.replace("'", "''")

    if scope == "system":
        trigger = "$trigger = New-ScheduledTaskTrigger -AtStartup"
        principal = (
            "$principal = New-ScheduledTaskPrincipal -UserId 'NT AUTHORITY\\SYSTEM' "
            "-LogonType ServiceAccount -RunLevel Highest"
        )
        scope_label = "开机（SYSTEM 后台）"
    else:
        user = _current_user().replace("'", "''")
        trigger = f"$trigger = New-ScheduledTaskTrigger -AtLogOn -User '{user}'"
        principal = (
            f"$principal = New-ScheduledTaskPrincipal -UserId '{user}' "
            "-LogonType Interactive -RunLevel Highest"
        )
        scope_label = "登录（当前用户 + 管理员权限）"

    arg_part = f"-Argument '{sa}' " if argument else ""
    script = (
        f"$action = New-ScheduledTaskAction -Execute '{se}' {arg_part}"
        f"-WorkingDirectory '{sw}'; "
        f"{trigger}; {principal}; "
        "$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries "
        "-DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) "
        "-StartWhenAvailable -MultipleInstances IgnoreNew; "
        f"Register-ScheduledTask -TaskName '{_TASK_NAME}' -Action $action -Trigger $trigger "
        "-Principal $principal -Settings $settings -Force | Out-Null; "
        # 注册后立即回查确认
        f"$t = Get-ScheduledTask -TaskName '{_TASK_NAME}' -ErrorAction SilentlyContinue; "
        "if ($t) { Write-Output 'OK' } else { Write-Output 'FAILED' }"
    )
    ok, out = _run_privileged(script, task_id=task_id, cancel_check=cancel_check)
    if "OK" in (out or ""):
        return True, (
            f"已设置开机自启（{scope_label}），任务名 {_TASK_NAME}，"
            f"启动方式：{kind}。下次{scope_label.split('（')[0]}时自动拉起语音助手。"
        )
    if "FAILED" in (out or ""):
        return False, "任务注册命令执行了但回查不到任务，可能权限不足或被拦截"
    if "取消" in (out or "") or "cancel" in (out or "").lower():
        return False, "你取消了 UAC 提权，未能创建自启任务"
    return False, f"设置开机自启失败: {(out or '未知错误').strip()[:200]}"


def uninstall_autostart(*, task_id: str | None = None, cancel_check=None) -> tuple[bool, str]:
    script = (
        f"if (Get-ScheduledTask -TaskName '{_TASK_NAME}' -ErrorAction SilentlyContinue) {{ "
        f"Unregister-ScheduledTask -TaskName '{_TASK_NAME}' -Confirm:$false; Write-Output 'OK' "
        "}} else { Write-Output 'NOTFOUND' }"
    )
    ok, out = _run_privileged(script, task_id=task_id, cancel_check=cancel_check)
    if "OK" in (out or ""):
        return True, "已取消开机自启"
    if "NOTFOUND" in (out or ""):
        return True, "未发现开机自启任务，无需取消"
    return False, f"取消开机自启失败: {(out or '未知错误').strip()[:200]}"


def autostart_status() -> tuple[bool, str]:
    script = (
        f"$t = Get-ScheduledTask -TaskName '{_TASK_NAME}' -ErrorAction SilentlyContinue; "
        "if ($t) { "
        "$pr = $t.Principal; "
        "Write-Output ('已启用 | 触发:' + ($t.Triggers.CimClass.CimClassName -join ',') "
        "+ ' | 账户:' + $pr.UserId + $pr.GroupId + ' | 级别:' + $pr.RunLevel) "
        "} else { Write-Output '未启用开机自启' }"
    )
    out = powershell(script, timeout=15)
    return True, (out or "无法查询").strip()


# ----------------------------------------------------------------------------
# Windows 服务化（真正的服务，LocalSystem）
# ----------------------------------------------------------------------------
def install_system_service(
    service_name: str = "KekesiService",
    command: str = "",
    display_name: str = "Kekesi 系统助手",
    *,
    task_id: str | None = None,
    cancel_check=None,
) -> tuple[bool, str]:
    """注册一个开机自启、以 LocalSystem 运行的 Windows 服务。

    注意：标准 Windows 服务要求目标程序响应服务控制管理器（SCM）。
    若 command 指向的是普通程序（如 GUI 助手），建议改用 install_autostart(scope='system')。
    """
    name = (service_name or "KekesiService").strip()
    cmd = (command or "").strip() or detect_launcher()["command"]
    safe_name = name.replace("'", "''")
    safe_cmd = cmd.replace('"', '\\"')
    safe_disp = (display_name or name).replace("'", "''")

    script = (
        f"$bin = '{safe_cmd}'; "
        f"sc.exe create '{safe_name}' binPath= \"$bin\" start= auto obj= LocalSystem "
        f"DisplayName= '{safe_disp}' | Out-Null; "
        f"sc.exe description '{safe_name}' 'Kekesi 助手系统级常驻服务' | Out-Null; "
        f"sc.exe failure '{safe_name}' reset= 0 actions= restart/5000/restart/5000/restart/5000 | Out-Null; "
        f"Start-Service -Name '{safe_name}' -ErrorAction SilentlyContinue; "
        f"(Get-Service -Name '{safe_name}').Status | ForEach-Object {{ Write-Output ('STATUS:' + $_) }}"
    )
    ok, out = _run_privileged(script, timeout=60, task_id=task_id, cancel_check=cancel_check)
    if ok and "STATUS:" in (out or ""):
        return True, f"已注册系统服务 {name}（LocalSystem，开机自启）。状态: {out.split('STATUS:')[-1].strip()}"
    if "已存在" in (out or "") or "1073" in (out or ""):
        return False, f"服务 {name} 可能已存在，可先 uninstall_system_service 再重装"
    return False, f"注册服务失败: {(out or '未知错误').strip()[:200]}"


def uninstall_system_service(service_name: str = "KekesiService", *, task_id: str | None = None, cancel_check=None) -> tuple[bool, str]:
    name = (service_name or "KekesiService").strip()
    safe_name = name.replace("'", "''")
    script = (
        f"Stop-Service -Name '{safe_name}' -Force -ErrorAction SilentlyContinue; "
        f"sc.exe delete '{safe_name}' | Out-Null; Write-Output 'OK'"
    )
    ok, out = _run_privileged(script, timeout=40, task_id=task_id, cancel_check=cancel_check)
    if "OK" in (out or ""):
        return True, f"已卸载系统服务 {name}"
    return False, f"卸载服务失败: {(out or '未知错误').strip()[:200]}"


# ----------------------------------------------------------------------------
# 夺取所有权（触及 TrustedInstaller 保护的资源）
# ----------------------------------------------------------------------------
def take_ownership(path: str, recurse: bool = True, *, task_id: str | None = None, cancel_check=None) -> tuple[bool, str]:
    """夺取文件/目录所有权并授予管理员完全控制（用于受保护的系统资源）。"""
    p = (path or "").strip()
    if not p:
        return False, "请提供路径"
    safe_p = p.replace("'", "''")
    rflag = "/r /d y" if recurse else ""
    tflag = "/t" if recurse else ""
    script = (
        f"$path = '{safe_p}'; "
        f"takeown /f \"$path\" {rflag} 2>&1 | Out-Null; "
        f"icacls \"$path\" /grant *S-1-5-32-544:F {tflag} /c 2>&1 | Out-String"
    )
    ok, out = _run_privileged(script, timeout=120, task_id=task_id, cancel_check=cancel_check)
    txt = (out or "").lower()
    if ok and ("successfully" in txt or "成功" in (out or "") or "processed" in txt):
        return True, f"已夺取「{p}」所有权并授予管理员完全控制"
    if "denied" in txt or "拒绝" in (out or ""):
        return False, "夺取所有权被拒绝，请确认已获得管理员/SYSTEM 权限"
    return True, f"夺取所有权已执行：{(out or '').strip()[:200]}"


def grant_full_control(path: str, account: str = "Administrators", recurse: bool = True, *, task_id: str | None = None, cancel_check=None) -> tuple[bool, str]:
    """对指定账户授予路径的完全控制权限。"""
    p = (path or "").strip()
    if not p:
        return False, "请提供路径"
    safe_p = p.replace("'", "''")
    safe_acc = account.replace("'", "''")
    tflag = "/t" if recurse else ""
    script = f"icacls '{safe_p}' /grant '{safe_acc}:F' {tflag} /c 2>&1 | Out-String"
    ok, out = _run_privileged(script, timeout=120, task_id=task_id, cancel_check=cancel_check)
    if ok and ("successfully" in (out or "").lower() or "成功" in (out or "")):
        return True, f"已授予 {account} 对「{p}」的完全控制"
    return True, f"授权已执行：{(out or '').strip()[:200]}"
