"""Windows 窗口管理"""

from __future__ import annotations

import logging
import os
import re

from .winops import focus_window_by_title, powershell

logger = logging.getLogger(__name__)

_NO_WINDOW = 0x08000000


def list_windows(limit: int = 40) -> tuple[bool, str]:
    limit = max(5, min(int(limit), 80))
    script = f"""
$rows = Get-Process -ErrorAction SilentlyContinue |
  Where-Object {{ $_.MainWindowHandle -ne [IntPtr]::Zero -and $_.MainWindowTitle }} |
  Sort-Object MainWindowTitle |
  Select-Object -First {limit} ProcessName, Id, MainWindowTitle
if (-not $rows) {{ Write-Output "（无可见窗口）"; exit 0 }}
foreach ($r in $rows) {{
  Write-Output ("{{0}} | PID {{1}} | {{2}}" -f $r.ProcessName, $r.Id, $r.MainWindowTitle)
}}
"""
    try:
        out = powershell(script, timeout=15)
        if not out:
            return True, "当前没有带标题的可见窗口"
        lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
        return True, "可见窗口：\n" + "\n".join(lines)
    except Exception as e:
        return False, f"列出窗口失败: {e}"


def window_title_visible(title: str) -> bool:
    """检查是否存在标题包含关键词的可见窗口。"""
    title = (title or "").strip()
    if not title:
        return False
    safe = title.replace("'", "''")
    script = f"""
$needle = '{safe}'.ToLower()
$hit = Get-Process -ErrorAction SilentlyContinue |
  Where-Object {{ $_.MainWindowHandle -ne [IntPtr]::Zero -and $_.MainWindowTitle -and $_.MainWindowTitle.ToLower().Contains($needle) }} |
  Select-Object -First 1
if ($hit) {{ exit 0 }} else {{ exit 1 }}
"""
    try:
        import subprocess

        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return r.returncode == 0
    except Exception:
        return False


def focus_window(title: str) -> tuple[bool, str]:
    return focus_window_by_title(title, timeout=12)


def close_window(title: str, force: bool = False) -> tuple[bool, str]:
    title = (title or "").strip()
    if not title:
        return False, "请提供要关闭的窗口标题关键词"
    safe = title.replace("'", "''")
    # 第一阶段：PostMessage(WM_CLOSE) 优雅关闭
    script = f"""
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class WinClose {{
  [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr hWnd, uint Msg, IntPtr wParam, IntPtr lParam);
}}
"@
$needle = '{safe}'.ToLower()
$targets = Get-Process -ErrorAction SilentlyContinue |
  Where-Object {{ $_.MainWindowHandle -ne [IntPtr]::Zero -and $_.MainWindowTitle -and $_.MainWindowTitle.ToLower().Contains($needle) }}
if (-not $targets) {{ Write-Error "NOT_FOUND"; exit 1 }}
$pids = @()
$names = @()
foreach ($t in $targets) {{
  [void][WinClose]::PostMessage($t.MainWindowHandle, 0x0010, [IntPtr]::Zero, [IntPtr]::Zero)
  $pids += $t.Id
  $names += $t.MainWindowTitle
}}
# 等待最多 1.5s 看 WM_CLOSE 是否生效（应用可能弹"是否保存"对话框阻塞）
Start-Sleep -Milliseconds 1500
$stillAlive = @()
foreach ($pid in $pids) {{
  $p = Get-Process -Id $pid -ErrorAction SilentlyContinue
  if ($p -and $p.MainWindowHandle -ne [IntPtr]::Zero) {{ $stillAlive += $pid }}
}}
if ($stillAlive.Count -gt 0) {{
  Write-Output ("STILL_ALIVE:" + ($stillAlive -join ","))
}} else {{
  Write-Output ("CLOSED:" + ($names -join "; "))
}}
"""
    out = powershell(script, timeout=15)
    if "NOT_FOUND" in out or not out:
        return False, f"没找到标题含「{title}」的窗口"

    # WM_CLOSE 成功
    if "CLOSED:" in out:
        names = out.split("CLOSED:", 1)[1].strip()
        return True, f"已关闭窗口: {names}"

    # WM_CLOSE 未生效（应用弹对话框阻塞）
    if "STILL_ALIVE" in out:
        if not force:
            return False, "窗口优雅关闭未生效（可能弹了关闭确认框或有未保存修改），已取消强制关闭；如确实需要关闭，请开启强制关闭"

        pids_str = out.split("STILL_ALIVE:", 1)[1].strip() if "STILL_ALIVE" in out else ""
        if pids_str:
            pids = [p.strip() for p in pids_str.split(",") if p.strip()]
            import subprocess as _sp
            r = _sp.run(
                ["taskkill", "/F", "/T"] + [a for p in pids for a in ("/PID", p)],
                capture_output=True, text=True, timeout=10,
                encoding="utf-8", errors="replace",
                creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0),
            )
            if r.returncode == 0:
                return True, f"已强制关闭 {len(pids)} 个窗口（WM_CLOSE 被阻塞，已用 taskkill）"
            return False, f"强制关闭失败: {r.stderr.strip() or r.stdout.strip()}"

    return True, out.splitlines()[-1].strip()


_CLOSE_ALL_PROTECTED = (
    "System",
    "Registry",
    "smss",
    "csrss",
    "wininit",
    "services",
    "lsass",
    "svchost",
    "dwm",
    "explorer",
    "fontdrvhost",
    "sihost",
    "taskhostw",
    "ShellExperienceHost",
    "SearchHost",
    "StartMenuExperienceHost",
    "TextInputHost",
    "RuntimeBroker",
    "ApplicationFrameHost",
    "SystemSettings",
    "LockApp",
    "SecurityHealthSystray",
)

# 窗口标题默认保留（IDE / 助手控制台）
_DEFAULT_KEEP_TITLE_KEYWORDS = (
    "cursor",
    "visual studio code",
    "克克斯",
    "keks",
    "语音助手",
    "assistant-x",
    "科科斯",
)


def _build_close_all_guard_ps(self_pid: int) -> str:
    """生成 PowerShell：收集助手进程树 + 命令行匹配的 PID。"""
    return f"""
$selfPid = {self_pid}
$guard = @($selfPid)
$cmdPat = 'kekesi|assistant-x-openclaw|main\\.py|cosyvoice|sherpa-onnx|openwakeword|playwright'
$allProcs = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue
foreach ($wp in $allProcs) {{
  if ($wp.CommandLine -and ($wp.CommandLine -match $cmdPat)) {{
    if ($guard -notcontains $wp.ProcessId) {{ $guard += $wp.ProcessId }}
  }}
}}
$me = $allProcs | Where-Object {{ $_.ProcessId -eq $selfPid }} | Select-Object -First 1
if ($me -and $me.ParentProcessId -gt 0 -and ($guard -notcontains $me.ParentProcessId)) {{
  $guard += $me.ParentProcessId
}}
$added = $true
while ($added) {{
  $added = $false
  foreach ($wp in $allProcs) {{
    if (($guard -contains $wp.ParentProcessId) -and ($guard -notcontains $wp.ProcessId)) {{
      $guard += $wp.ProcessId
      $added = $true
    }}
  }}
}}
"""


def _title_keep_filter(keep_title: str) -> str:
    keys = [k.strip().lower() for k in _DEFAULT_KEEP_TITLE_KEYWORDS]
    extra = (keep_title or "").strip().lower()
    if extra:
        keys.append(extra.replace("'", "''"))
    if not keys:
        return ""
    checks = " -and ".join(
        f"(-not $_.MainWindowTitle.ToLower().Contains('{k.replace(chr(39), chr(39)*2)}'))"
        for k in keys
    )
    return f" -and ({checks})"


def close_all_apps(force: bool = False, keep_title: str = "") -> tuple[bool, str]:
    """关闭所有带窗口的用户程序。force=True 时强杀进程（需用户已口头确认）。"""
    self_pid = os.getpid()
    protected = ",".join(f"'{n}'" for n in _CLOSE_ALL_PROTECTED)
    guard_ps = _build_close_all_guard_ps(self_pid)
    keep_filter = _title_keep_filter(keep_title)
    guard_filter = " -and ($guard -notcontains $_.Id)"

    if not force:
        script = f"""
{guard_ps}
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class WinCloseAll {{
  [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr hWnd, uint Msg, IntPtr wParam, IntPtr lParam);
}}
"@
$protected = @({protected})
$targets = Get-Process -ErrorAction SilentlyContinue |
  Where-Object {{
    $_.MainWindowHandle -ne [IntPtr]::Zero -and $_.MainWindowTitle -and
    ($protected -notcontains $_.ProcessName){keep_filter}{guard_filter}
  }}
$count = 0
$skipped = 0
$names = @()
foreach ($p in $targets) {{
  if ([WinCloseAll]::PostMessage($p.MainWindowHandle, 0x0010, [IntPtr]::Zero, [IntPtr]::Zero)) {{
    $count++
    $names += ($p.ProcessName + ":" + $p.MainWindowTitle)
  }} else {{ $skipped++ }}
}}
Write-Output ("OK closed=" + $count + " total=" + $targets.Count + " skipped=" + $skipped + " guard=" + ($guard.Count))
if ($names.Count -gt 0 -and $names.Count -le 15) {{
  Write-Output ($names -join " | ")
}} elseif ($names.Count -gt 15) {{
  Write-Output (($names | Select-Object -First 15) -join " | ")
  Write-Output ("…等共 " + $names.Count + " 个")
}}
"""
    else:
        script = f"""
{guard_ps}
$protected = @({protected})
$targets = Get-Process -ErrorAction SilentlyContinue |
  Where-Object {{
    $_.MainWindowHandle -ne [IntPtr]::Zero -and $_.MainWindowTitle -and
    ($protected -notcontains $_.ProcessName){keep_filter}{guard_filter}
  }}
$count = 0
$names = @()
foreach ($p in $targets) {{
  try {{
    Stop-Process -Id $p.Id -Force -ErrorAction Stop
    $count++
    $names += ($p.ProcessName + ":" + $p.MainWindowTitle)
  }} catch {{ }}
}}
Write-Output ("OK killed=" + $count + " total=" + $targets.Count + " guard=" + ($guard.Count))
if ($names.Count -gt 0 -and $names.Count -le 15) {{
  Write-Output ($names -join " | ")
}} elseif ($names.Count -gt 15) {{
  Write-Output (($names | Select-Object -First 15) -join " | ")
  Write-Output ("…等共 " + $names.Count + " 个")
}}
"""

    out = powershell(script, timeout=30)
    if not out or "OK" not in out:
        return False, f"批量关闭失败: {(out or '未知错误').strip()[:200]}"

    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    summary = lines[0]
    if "closed=0" in summary and "total=0" in summary:
        return True, "没有需要关闭的应用窗口"
    if "closed=0" in summary or "killed=0" in summary:
        return False, "找到窗口但关闭失败，可尝试 force=true 强杀（需先确认）"

    verb = "强杀" if force else "关闭"
    detail = lines[1] if len(lines) > 1 else ""
    msg = f"已{verb}应用窗口"
    if "closed=" in summary:
        m = re.search(r"closed=(\d+)", summary)
        if m:
            msg = f"已请求关闭 {m.group(1)} 个应用窗口"
    elif "killed=" in summary:
        m = re.search(r"killed=(\d+)", summary)
        if m:
            msg = f"已结束 {m.group(1)} 个应用进程"
    if detail:
        msg += f"（{detail[:120]}）"
    return True, msg


def minimize_window(title: str) -> tuple[bool, str]:
    title = (title or "").strip()
    if not title:
        return False, "请提供窗口标题关键词"
    safe = title.replace("'", "''")
    script = f"""
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class WinMin {{
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
}}
"@
$needle = '{safe}'.ToLower()
$target = Get-Process -ErrorAction SilentlyContinue |
  Where-Object {{ $_.MainWindowHandle -ne [IntPtr]::Zero -and $_.MainWindowTitle -and $_.MainWindowTitle.ToLower().Contains($needle) }} |
  Select-Object -First 1
if (-not $target) {{ Write-Error "NOT_FOUND"; exit 1 }}
[void][WinMin]::ShowWindow($target.MainWindowHandle, 6)
Write-Output ("已最小化: " + $target.MainWindowTitle)
"""
    out = powershell(script, timeout=12)
    if "NOT_FOUND" in out or not out:
        return False, f"没找到标题含「{title}」的窗口"
    return True, out.splitlines()[-1].strip()
