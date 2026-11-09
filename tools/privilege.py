"""Windows 11 权限掌控 — 权限检测、UAC 提权、管理员/SYSTEM 级命令执行。

设计要点：
- 进程一旦获得管理员/SYSTEM 权限，现有所有工具（run_shell、注册表、服务）
  会自动继承该权限，无需逐个改造。
- 单命令提权走 Start-Process -Verb RunAs（触发 UAC，捕获输出）。
- SYSTEM 级执行通过一次性计划任务实现（比管理员更高，免 UAC，但需先有管理员权限）。
"""

from __future__ import annotations

import base64
import ctypes
import logging
import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from .winops import powershell

_NO_WINDOW = 0x08000000

logger = logging.getLogger(__name__)  # PRIVILEGE_4_1_V1


# ----------------------------------------------------------------------------
# 权限检测
# ----------------------------------------------------------------------------
def is_admin() -> bool:
    """当前进程是否拥有管理员（已提升的）令牌。"""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _whoami() -> str:
    try:
        r = subprocess.run(
            ["whoami"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=_NO_WINDOW,
            timeout=10,
        )
        return (r.stdout or "").strip().lower()
    except Exception:
        return ""


def is_system() -> bool:
    """当前进程是否以 NT AUTHORITY\\SYSTEM 身份运行。"""
    who = _whoami()
    return who in ("nt authority\\system", "nt 权限\\system") or who.endswith("\\system")


def privilege_level() -> str:
    """返回 system / admin / user。"""
    if is_system():
        return "system"
    if is_admin():
        return "admin"
    return "user"


def get_privilege_status() -> tuple[bool, str]:
    """详细报告当前权限：账户、完整性级别、是否管理员/SYSTEM。"""
    level = privilege_level()
    script = (
        "$id = [System.Security.Principal.WindowsIdentity]::GetCurrent(); "
        "$p = New-Object System.Security.Principal.WindowsPrincipal($id); "
        "$admin = $p.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator); "
        "Write-Output ('账户: ' + $id.Name); "
        "$il = (whoami /groups | Select-String 'Mandatory Label').ToString(); "
        "Write-Output ('管理员角色: ' + $admin); "
    )
    detail = powershell(script, timeout=15)
    labels = {"system": "SYSTEM（系统最高权限）", "admin": "管理员", "user": "标准用户"}
    head = f"当前权限级别：{labels.get(level, level)}"
    if detail:
        head += "\n" + detail.strip()
    if level == "user":
        head += "\n提示：系统级操作（HKLM、系统服务、改 DNS 等）会因权限不足失败，"
        head += "可调用 elevate_self 提权，或用 run_shell_admin 单命令提权。"
    return True, head


# ----------------------------------------------------------------------------
# 提权重启自身
# ----------------------------------------------------------------------------
def _current_launch() -> tuple[str, str]:
    """返回 (可执行程序, 参数字符串) 用于重启当前进程。"""
    exe = sys.executable
    args = sys.argv
    if args and Path(args[0]).suffix.lower() == ".py":
        params = " ".join(f'"{a}"' for a in args)
    elif len(args) > 1:
        params = " ".join(f'"{a}"' for a in args[1:])
    else:
        params = ""
    return exe, params


def elevate_self() -> tuple[bool, str]:
    """以管理员身份重启当前进程（触发 UAC）。

    注意：成功后会启动一个新的管理员实例，当前实例需自行退出。
    适用于希望整个助手长期以管理员身份运行的场景。
    """
    if is_admin():
        return True, "当前已是管理员权限，无需提权"
    exe, params = _current_launch()
    try:
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1)
        if int(rc) > 32:
            return True, (
                "已弹出 UAC 并启动管理员实例。请在新窗口继续使用；"
                "当前这个非管理员实例可以关闭了。"
            )
        if int(rc) == 1223:
            return False, "你取消了 UAC 提权请求"
        return False, f"提权失败（ShellExecute 返回码 {rc}）"
    except Exception as e:
        return False, f"提权异常: {e}"


# ----------------------------------------------------------------------------
# 管理员级单命令执行（Start-Process -Verb RunAs，捕获输出）
# ----------------------------------------------------------------------------
def _encode_ps(script: str) -> str:
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


# PRIVILEGE_4_1_V1: 取消辅助。cancel_check 可能是 None、函数或
# CancellationToken.as_check()，一律先判空再调用，异常按未取消处理。
def _is_cancelled(cancel_check) -> bool:
    try:
        return bool(cancel_check and cancel_check())
    except Exception:
        return False


# PRIVILEGE_4_1_V1_2: proc 注册辅助。winops.powershell 现已暴露 on_proc，
# launcher/runner/fast-path 的 Popen 在此注册，调用后立即注销。
# 注册失败只记 warning，不掩盖执行本身；task_id 为 None 时
# process_registry 会按 _unowned 处理（4-1 口径）。
def _make_on_proc(task_id, procs):
    def _on_proc(proc):
        procs.append(proc)
        try:
            from ..core.process_registry import register

            register(proc, task_id=task_id)
        except Exception as e:
            logger.warning("提权进程注册失败: %s", e)

    return _on_proc


def _unregister_procs(task_id, procs):
    try:
        from ..core.process_registry import unregister
    except Exception:
        return
    for proc in procs:
        try:
            unregister(proc, task_id=task_id)
        except Exception:
            pass


def _cleanup_scheduled_task(task_name: str) -> None:
    # 尽力先 Stop 再 Unregister，成败不掩盖主流程；任务已不存在时
    # 两条都带 SilentlyContinue，无害。
    try:
        powershell(
            f"Stop-ScheduledTask -TaskName '{task_name}' -ErrorAction SilentlyContinue; "
            f"Unregister-ScheduledTask -TaskName '{task_name}' -Confirm:$false -ErrorAction SilentlyContinue",
            timeout=15,
        )
    except Exception:
        pass


def run_shell_admin(command: str, timeout: int = 180, *, task_id: str | None = None, cancel_check=None) -> tuple[bool, str]:  # PRIVILEGE_4_1_V1
    """以管理员权限执行一条 PowerShell 命令并返回输出。

    若进程已是管理员/SYSTEM，直接执行；否则通过 Start-Process -Verb RunAs
    提权运行（会弹 UAC），子进程把输出写入临时文件，父进程读取回传。
    """
    if not isinstance(command, str):
        command = str(command) if command is not None else ""
    command = command.strip()
    if not command:
        return False, "命令不能为空"
    if _is_cancelled(cancel_check):
        return False, "操作已取消。"
    if is_admin() or is_system():
        _fast_procs: list = []
        try:
            out = powershell(command, timeout=timeout, on_proc=_make_on_proc(task_id, _fast_procs))
        finally:
            _unregister_procs(task_id, _fast_procs)
        # PRIVILEGE_4_1_V1_2_1: 旧逻辑（取消且 out 为空）已被下述 returncode 三分取代，标记留作审计链。
        _rc = _fast_procs[-1].returncode if _fast_procs else None
        if _rc is None:
            _cancelled_after = _is_cancelled(cancel_check) and not out
        else:
            _cancelled_after = _is_cancelled(cancel_check) and _rc != 0
        if _cancelled_after:  # PRIVILEGE_4_1_V1_2_2
            return False, "操作已取消。"
        if _rc is not None and _rc != 0:
            return False, (f"执行失败（退出码 {_rc}）：\n{out}" if out else f"执行失败（退出码 {_rc}）")
        return True, out or "命令已执行（无输出）"

    out_file = Path(tempfile.gettempdir()) / f"kekesi_admin_{uuid.uuid4().hex}.txt"
    safe_out = str(out_file).replace("'", "''")
    inner = (
        "$ErrorActionPreference='Continue'; "
        "$global:LASTEXITCODE = 0; "
        "try { "
        f"$r = & {{ {command} }} 2>&1 | Out-String; "
        "$ec = if ($LASTEXITCODE -ne $null -and $LASTEXITCODE -ne 0) { [int]$LASTEXITCODE } "
        "elseif (-not $?) { 1 } else { 0 }; "
        f"Set-Content -LiteralPath '{safe_out}' -Value $r -Encoding UTF8; "
        "exit $ec "
        "} catch { "
        f"Set-Content -LiteralPath '{safe_out}' -Value $_.Exception.Message -Encoding UTF8; "
        "exit 1 "
        "}"
    )
    enc = _encode_ps(inner)
    launcher = (
        "$p = Start-Process powershell -Verb RunAs -WindowStyle Hidden -PassThru "
        f"-ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-EncodedCommand','{enc}'; "
        "$p.WaitForExit(); Write-Output ('EXIT:' + $p.ExitCode)"
    )
    _launcher_procs: list = []
    try:
        launch_out = powershell(launcher, timeout=timeout, on_proc=_make_on_proc(task_id, _launcher_procs))
    except Exception as e:
        _cleanup(out_file)  # PRIVILEGE_4_1_V1: 异常早退也要清临时文件
        return False, f"提权执行失败: {e}"
    finally:
        _unregister_procs(task_id, _launcher_procs)

    lower = (launch_out or "").lower()
    if "canceled" in lower or "cancelled" in lower or "操作已被用户取消" in (launch_out or ""):
        _cleanup(out_file)
        return False, "你取消了 UAC 提权请求"

    result = ""
    for _ in range(20):
        if _is_cancelled(cancel_check):
            _cleanup(out_file)
            return False, ("已取消等待，管理员提权命令本身可能仍在运行"
                            "（RunAs 进程不在本任务进程树内，无法按任务终止）")  # PRIVILEGE_4_1_V1_1
        if out_file.exists():
            try:
                result = out_file.read_text(encoding="utf-8", errors="replace").strip()
                break
            except Exception:
                time.sleep(0.2)
        else:
            time.sleep(0.2)
    _cleanup(out_file)

    _exit_ok = True
    for _ln in (launch_out or "").splitlines():
        _s = _ln.strip()
        if _s.startswith("EXIT:"):
            _code_str = _s[5:].strip()
            if _code_str and _code_str != "0":
                _exit_ok = False
            break
    if not result:
        if "EXIT:0" in (launch_out or "") and _exit_ok:
            return True, "命令已以管理员权限执行（无输出）"
        return False, "提权命令已运行但未捕获到输出，可能被 UAC 拒绝"
    if len(result) > 3000:
        result = result[:3000] + "\n…（输出已截断）"
    return _exit_ok, result


# ----------------------------------------------------------------------------
# SYSTEM 级单命令执行（一次性计划任务，需已有管理员权限）
# ----------------------------------------------------------------------------
def run_shell_system(command: str, timeout: int = 180, *, task_id: str | None = None, cancel_check=None) -> tuple[bool, str]:  # PRIVILEGE_4_1_V1
    """以 NT AUTHORITY\\SYSTEM 身份执行命令（系统最高权限，免 UAC）。

    通过创建一个以 SYSTEM 运行的一次性计划任务实现，运行完即删除。
    需要当前进程已是管理员（或 SYSTEM）。
    """
    if not isinstance(command, str):
        command = str(command) if command is not None else ""
    command = command.strip()
    if not command:
        return False, "命令不能为空"
    if _is_cancelled(cancel_check):
        return False, "操作已取消。"
    if is_system():
        _fast_procs: list = []
        try:
            out = powershell(command, timeout=timeout, on_proc=_make_on_proc(task_id, _fast_procs))
        finally:
            _unregister_procs(task_id, _fast_procs)
        # PRIVILEGE_4_1_V1_2_1: 旧逻辑（取消且 out 为空）已被下述 returncode 三分取代，标记留作审计链。
        _rc = _fast_procs[-1].returncode if _fast_procs else None
        if _rc is None:
            _cancelled_after = _is_cancelled(cancel_check) and not out
        else:
            _cancelled_after = _is_cancelled(cancel_check) and _rc != 0
        if _cancelled_after:  # PRIVILEGE_4_1_V1_2_2
            return False, "操作已取消。"
        if _rc is not None and _rc != 0:
            return False, (f"执行失败（退出码 {_rc}）：\n{out}" if out else f"执行失败（退出码 {_rc}）")
        return True, out or "命令已执行（无输出）"
    if not is_admin():
        return False, (
            "以 SYSTEM 身份执行需要先获得管理员权限。"
            "请先调用 elevate_self 提权，或改用 run_shell_admin。"
        )

    tag = uuid.uuid4().hex[:12]
    task_name = f"kekesi_sys_{tag}"
    ps1 = Path(tempfile.gettempdir()) / f"kekesi_sys_{tag}.ps1"
    out_file = Path(tempfile.gettempdir()) / f"kekesi_sys_{tag}.out"
    safe_out = str(out_file).replace("'", "''")
    script_body = (
        "$ErrorActionPreference='Continue'\r\n"
        "$global:LASTEXITCODE = 0\r\n"
        "try {\r\n"
        f"  $r = & {{ {command} }} 2>&1 | Out-String\r\n"
        "  $ec = if ($LASTEXITCODE -ne $null -and $LASTEXITCODE -ne 0) { [int]$LASTEXITCODE } "
        "elseif (-not $?) { 1 } else { 0 }\r\n"
        f"  Set-Content -LiteralPath '{safe_out}' -Value $r -Encoding UTF8\r\n"
        "  exit $ec\r\n"
        "} catch {\r\n"
        f"  Set-Content -LiteralPath '{safe_out}' -Value $_.Exception.Message -Encoding UTF8\r\n"
        "  exit 1\r\n"
        "}\r\n"
    )
    try:
        ps1.write_text(script_body, encoding="utf-8")
    except Exception as e:
        return False, f"无法写入临时脚本: {e}"

    safe_ps1 = str(ps1).replace("'", "''")
    runner = (
        f"$action = New-ScheduledTaskAction -Execute 'powershell.exe' "
        f"-Argument '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File \"{safe_ps1}\"'; "
        "$principal = New-ScheduledTaskPrincipal -UserId 'NT AUTHORITY\\SYSTEM' "
        "-LogonType ServiceAccount -RunLevel Highest; "
        f"Register-ScheduledTask -TaskName '{task_name}' -Action $action -Principal $principal "
        "-Force | Out-Null; "
        f"Start-ScheduledTask -TaskName '{task_name}'; "
        "Start-Sleep -Milliseconds 500; "
        f"$n=0; $lastRes = 0; while ($n -lt {max(10, timeout)}) {{ "
        f"$info = Get-ScheduledTaskInfo -TaskName '{task_name}'; "
        "$lastRes = $info.LastTaskResult; "
        "if ($lastRes -ne 267009) { break }; "
        "Start-Sleep -Seconds 1; $n++ }; "
        f"Unregister-ScheduledTask -TaskName '{task_name}' -Confirm:$false; "
        "Write-Output ('DONE:' + $lastRes)"
    )
    _runner_procs: list = []
    try:
        launch_out = powershell(runner, timeout=timeout + 10, on_proc=_make_on_proc(task_id, _runner_procs))
    except Exception as e:
        # PRIVILEGE_4_1_V1_2: launcher 异常时 runner 脚本里的 Unregister
        # 可能没执行，尽力先 Stop 再 Unregister，成败不掩盖原始失败。
        # 注意：winops 吞超时不抛异常，超时漏任务不走这里，走 DONE 缺失清理。
        _cleanup_scheduled_task(task_name)
        _cleanup(ps1)
        _cleanup(out_file)
        return False, f"SYSTEM 任务启动失败: {e}"
    finally:
        _unregister_procs(task_id, _runner_procs)

    result = ""
    for _ in range(15):
        if _is_cancelled(cancel_check):
            # PRIVILEGE_4_1_V1_2: 取消时 launcher 可能已被树杀，runner
            # 脚本里的 Unregister 没执行，尽力先 Stop 再 Unregister。
            _cleanup_scheduled_task(task_name)
            _cleanup(ps1)
            _cleanup(out_file)
            return False, ("已取消等待，SYSTEM 任务本身可能仍在运行"
                            "（计划任务不在本任务进程树内，无法按任务终止）")  # PRIVILEGE_4_1_V1_1
        if out_file.exists():
            try:
                result = out_file.read_text(encoding="utf-8", errors="replace").strip()
                break
            except Exception:
                time.sleep(0.2)
        else:
            time.sleep(0.3)
    _cleanup(ps1)
    _cleanup(out_file)
    if "DONE" not in (launch_out or ""):
        # PRIVILEGE_4_1_V1_2: winops 吞超时不抛异常，except 等不到超时；
        # DONE 缺失说明 runner 脚本没走到 Unregister，尽力清理防漏任务。
        _cleanup_scheduled_task(task_name)

    _done_ok = True
    for _ln in (launch_out or "").splitlines():
        _s = _ln.strip()
        if _s.startswith("DONE:"):
            _res_str = _s[5:].strip()
            if _res_str and _res_str != "0":
                _done_ok = False
            break
    if not result:
        if "DONE" in (launch_out or "") and _done_ok:
            return True, "命令已以 SYSTEM 权限执行（无输出）"
        return False, f"SYSTEM 任务执行异常: {(launch_out or '未知错误').strip()[:200]}"
    if len(result) > 3000:
        result = result[:3000] + "\n…（输出已截断）"
    return _done_ok, result


def _cleanup(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except Exception:
        pass
