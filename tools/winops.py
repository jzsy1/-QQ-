"""Windows 底层操作 — 借鉴 HoloJarvis winops.py"""

from __future__ import annotations

import ctypes
import os
import subprocess
import time
from pathlib import Path

_NO_WINDOW = 0x08000000

_VK = {
    "media_play_pause": 0xB3,
    "media_next": 0xB0,
    "media_prev": 0xB1,
    "volume_mute": 0xAD,
    "volume_down": 0xAE,
    "volume_up": 0xAF,
}
_KEYEVENTF_KEYUP = 0x0002

_WIN_FOCUS_TYPES = '''
Add-Type @"
using System;
using System.Runtime.InteropServices;
public struct WRECT { public int Left, Top, Right, Bottom; }
public class WinFocus {
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out WRECT r);
}
"@
'''

_WIN_FOCUS_FOOTER = '''
if (-not $best) { Write-Error "NOT_FOUND"; exit 1 }
[WinFocus]::ShowWindow($best.MainWindowHandle, 9) | Out-Null
[WinFocus]::SetForegroundWindow($best.MainWindowHandle) | Out-Null
'''


def focus_window_by_title(title: str, timeout: float = 12) -> tuple[bool, str]:
    """按窗口标题关键词聚焦面积最大的匹配窗口。"""
    title = (title or "").strip()
    if not title:
        return False, "请提供窗口标题关键词"
    safe = title.replace("'", "''")
    script = (
        _WIN_FOCUS_TYPES
        + f"""
$needle = '{safe}'.ToLower()
$best = $null
$bestArea = 0
Get-Process -ErrorAction SilentlyContinue |
  Where-Object {{ $_.MainWindowHandle -ne [IntPtr]::Zero }} |
  ForEach-Object {{
    if ($_.MainWindowTitle.ToLower().Contains($needle)) {{
      $rect = New-Object WRECT
      if ([WinFocus]::GetWindowRect($_.MainWindowHandle, [ref]$rect)) {{
        $area = ($rect.Right - $rect.Left) * ($rect.Bottom - $rect.Top)
        if ($area -gt $bestArea) {{ $bestArea = $area; $best = $_ }}
      }}
    }}
  }}
"""
        + _WIN_FOCUS_FOOTER
        + 'Write-Output ("已聚焦: " + $best.MainWindowTitle + " (PID " + $best.Id + ")")'
    )
    out = powershell(script, timeout=timeout)
    if "NOT_FOUND" in out or not out:
        return False, f"没找到标题含「{title}」的窗口"
    return True, out.splitlines()[-1].strip()


def focus_window_by_process_names(
    names: list[str], timeout: float = 10
) -> tuple[bool, str]:
    """按进程名列表聚焦面积最大的可见窗口。"""
    if not names:
        return False, "未提供进程名"
    names_literal = ",".join(f"'{n.replace(chr(39), chr(39)*2)}'" for n in names)
    script = (
        _WIN_FOCUS_TYPES
        + f"""
$names = @({names_literal})
$best = $null
$bestArea = 0
foreach ($name in $names) {{
  Get-Process -Name $name -ErrorAction SilentlyContinue |
    Where-Object {{ $_.MainWindowHandle -ne [IntPtr]::Zero }} |
    ForEach-Object {{
      $rect = New-Object WRECT
      if ([WinFocus]::GetWindowRect($_.MainWindowHandle, [ref]$rect)) {{
        $area = ($rect.Right - $rect.Left) * ($rect.Bottom - $rect.Top)
        if ($area -gt $bestArea) {{
          $bestArea = $area
          $best = $_
        }}
      }}
    }}
}}
"""
        + _WIN_FOCUS_FOOTER
        + 'Write-Output $best.ProcessName'
    )
    out = powershell(script, timeout=timeout)
    if not out or "NOT_FOUND" in out:
        return False, "应用在运行但无法聚焦窗口"
    return True, out.splitlines()[-1].strip()


def desktop_dir() -> str:
    home = Path.home()
    for name in ("Desktop", "桌面"):
        path = home / name
        if path.is_dir():
            return str(path)
    return str(home / "Desktop")


def powershell(script: str, env: dict | None = None, timeout: float = 60, *, on_proc=None) -> str:  # WINOPS_PROC_CALLBACK_V1
    """执行 PowerShell 脚本并返回输出（stdout 为空则取 stderr）。

    on_proc 若提供，会在 Popen 建好后立刻以 proc 为参数调用一次，
    调用方可在此注册 proc；on_proc 抛异常时先 kill proc 再往上抛。
    on_proc 为 None 时走原 subprocess.run 路径，行为完全不变。
    超时两种路径都返回 ""（原吞超时行为，不改成抛异常）。
    """
    full = "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;" + script
    if on_proc is None:
        try:
            r = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-NoLogo",
                    "-NonInteractive",
                    "-Command",
                    full,
                ],
                capture_output=True,
                env=env,
                timeout=timeout,
                creationflags=_NO_WINDOW,
            )
        except subprocess.TimeoutExpired:
            return ""
        out = r.stdout.decode("utf-8", "ignore").strip()
        if not out:
            out = r.stderr.decode("utf-8", "ignore").strip()
        return out
    argv = [
        "powershell",
        "-NoProfile",
        "-NoLogo",
        "-NonInteractive",
        "-Command",
        full,
    ]
    proc = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        creationflags=_NO_WINDOW,
    )
    try:
        on_proc(proc)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
        try:
            proc.wait(timeout=2)
        except Exception:
            pass
        raise
    try:
        out_bytes, err_bytes = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except Exception:
            pass
        try:
            proc.wait(timeout=2)
        except Exception:
            pass
        return ""
    out = out_bytes.decode("utf-8", "ignore").strip() if out_bytes else ""
    if not out:
        out = err_bytes.decode("utf-8", "ignore").strip() if err_bytes else ""
    return out


def get_clipboard() -> str:
    """使用 Win32 原生 API 读取剪贴板，避免每秒拉起 PowerShell 子进程。"""
    CF_UNICODETEXT = 13
    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        user32.OpenClipboard.argtypes = [ctypes.c_void_p]
        user32.OpenClipboard.restype = ctypes.c_int
        user32.GetClipboardData.argtypes = [ctypes.c_uint]
        user32.GetClipboardData.restype = ctypes.c_void_p
        user32.CloseClipboard.argtypes = []
        user32.CloseClipboard.restype = ctypes.c_int
        kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalUnlock.restype = ctypes.c_int

        if not user32.OpenClipboard(None):
            return ""
        try:
            handle = user32.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                return ""
            ptr = kernel32.GlobalLock(handle)
            if not ptr:
                return ""
            try:
                return ctypes.wstring_at(ptr) or ""
            finally:
                kernel32.GlobalUnlock(handle)
        finally:
            user32.CloseClipboard()
    except Exception:
        return powershell("Get-Clipboard -Raw")


def set_clipboard(text: str) -> None:
    # 强制转为字符串（防止传入数字/None 等）
    text_str = "" if text is None else str(text)
    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002
    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        user32.OpenClipboard.argtypes = [ctypes.c_void_p]
        user32.OpenClipboard.restype = ctypes.c_int
        user32.EmptyClipboard.argtypes = []
        user32.EmptyClipboard.restype = ctypes.c_int
        user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
        user32.SetClipboardData.restype = ctypes.c_void_p
        user32.CloseClipboard.argtypes = []
        user32.CloseClipboard.restype = ctypes.c_int
        kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
        kernel32.GlobalAlloc.restype = ctypes.c_void_p
        kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalUnlock.restype = ctypes.c_int

        if user32.OpenClipboard(None):
            try:
                user32.EmptyClipboard()
                buf = ctypes.create_unicode_buffer(text_str)
                size = ctypes.sizeof(buf)
                h_mem = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
                if h_mem:
                    ptr = kernel32.GlobalLock(h_mem)
                    if ptr:
                        ctypes.memmove(ptr, buf, size)
                        kernel32.GlobalUnlock(h_mem)
                        if user32.SetClipboardData(CF_UNICODETEXT, h_mem):
                            return
            finally:
                user32.CloseClipboard()
    except Exception:
        pass
    powershell(
        "Set-Clipboard -Value $env:JV_CLIP",
        env={**os.environ, "JV_CLIP": text_str},
    )


def open_url(url: str) -> tuple[bool, str]:
    if not url or not url.strip():
        return False, "网址为空"
    url = url.strip()
    # 仅 http/https 自动补全前缀；其他 scheme（about:, file:, mailto: 等）原样传递
    if not url.startswith(("http://", "https://", "about:", "file:", "mailto:", "ftp://", "ftp://")):
        # 没有 scheme 的，默认补 https://
        if "://" not in url and not url.startswith(("www.", "localhost")):
            url = "https://" + url
        elif url.startswith("www."):
            url = "https://" + url
    try:
        os.startfile(url)  # noqa: S606
        return True, "已在浏览器打开"
    except Exception as e:
        return False, f"打开网址失败: {e}"


def set_volume(level: int) -> tuple[bool, str]:
    level = max(0, min(100, int(level)))
    try:
        user32 = ctypes.windll.user32
        for _ in range(50):
            user32.keybd_event(_VK["volume_down"], 0, 0, 0)
            user32.keybd_event(_VK["volume_down"], 0, _KEYEVENTF_KEYUP, 0)
            time.sleep(0.005)
        steps = round(level / 2)
        for _ in range(steps):
            user32.keybd_event(_VK["volume_up"], 0, 0, 0)
            user32.keybd_event(_VK["volume_up"], 0, _KEYEVENTF_KEYUP, 0)
            time.sleep(0.005)
        return True, f"音量已设为约 {level}"
    except Exception as e:
        return False, f"调节音量失败: {e}"


def control_music(action: str) -> tuple[bool, str]:
    names = {
        "play": "播放",
        "pause": "暂停",
        "playpause": "切换播放",
        "next": "下一首",
        "previous": "上一首",
    }
    try:
        user32 = ctypes.windll.user32
        if action in ("play", "pause", "playpause"):
            vk = _VK["media_play_pause"]
            user32.keybd_event(vk, 0, 0, 0)
            user32.keybd_event(vk, 0, _KEYEVENTF_KEYUP, 0)
        elif action == "next":
            vk = _VK["media_next"]
            user32.keybd_event(vk, 0, 0, 0)
            user32.keybd_event(vk, 0, _KEYEVENTF_KEYUP, 0)
        elif action == "previous":
            vk = _VK["media_prev"]
            user32.keybd_event(vk, 0, 0, 0)
            user32.keybd_event(vk, 0, _KEYEVENTF_KEYUP, 0)
        else:
            return False, f"不支持的操作: {action}"
        return True, names.get(action, "已操作") + "音乐"
    except Exception as e:
        return False, f"媒体控制失败: {e}"


def system_power(action: str) -> tuple[bool, str]:
    action = (action or "").strip().lower()
    try:
        if action == "lock":
            ctypes.windll.user32.LockWorkStation()
            return True, "已锁屏"
        if action == "sleep":
            subprocess.run(
                ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"],
                creationflags=_NO_WINDOW,
            )
            return True, "电脑准备休眠"
        if action == "logoff":
            subprocess.run(
                ["shutdown", "/l"],
                creationflags=_NO_WINDOW,
            )
            return True, "正在注销当前用户"
        if action == "shutdown":
            subprocess.run(
                ["shutdown", "/s", "/t", "60", "/c", "克克斯收到关机指令"],
                creationflags=_NO_WINDOW,
            )
            return True, "将在 60 秒后关机，可说 shutdown /a 取消"
        if action == "restart":
            subprocess.run(
                ["shutdown", "/r", "/t", "60", "/c", "克克斯收到重启指令"],
                creationflags=_NO_WINDOW,
            )
            return True, "将在 60 秒后重启，可说 shutdown /a 取消"
        return False, "不支持的操作，可用: lock, sleep, logoff, shutdown, restart"
    except Exception as e:
        return False, f"电源操作失败: {e}"


def get_system_status() -> tuple[bool, str]:
    script = """
$os = Get-CimInstance Win32_OperatingSystem
$cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
$usedMem = [math]::Round(($os.TotalVisibleMemorySize - $os.FreePhysicalMemory) / 1MB, 1)
$totalMem = [math]::Round($os.TotalVisibleMemorySize / 1MB, 1)
$disk = Get-PSDrive -Name C -ErrorAction SilentlyContinue
$diskFree = if ($disk) { [math]::Round($disk.Free / 1GB, 1) } else { 0 }
$diskUsed = if ($disk) { [math]::Round($disk.Used / 1GB, 1) } else { 0 }
$boot = (Get-Date) - $os.LastBootUpTime
Write-Output ("CPU: " + $cpu.Name)
Write-Output ("内存: " + $usedMem + " / " + $totalMem + " GB")
Write-Output ("C盘: 已用 " + $diskUsed + " GB，剩余 " + $diskFree + " GB")
Write-Output ("运行时长: " + [int]$boot.TotalHours + " 小时 " + $boot.Minutes + " 分钟")
"""
    try:
        out = powershell(script, timeout=15)
        if not out:
            return False, "无法获取系统状态"
        return True, out.strip()
    except Exception as e:
        return False, f"获取系统状态失败: {e}"


def check_system_health() -> tuple[bool, str]:
    """检查系统健康状态，返回结构化信息供自动提醒使用。
    
    返回格式：JSON {cpu_pct, memory_pct, disk_pct, issues[], warnings[]}
    - issues: 需要立即处理的问题（CPU>90%, 内存>90%, 磁盘<10GB）
    - warnings: 需要关注的警告（CPU>70%, 内存>70%, 磁盘<30GB）
    """
    script = """
$os = Get-CimInstance Win32_OperatingSystem
$cpu = (Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average
$totalMem = $os.TotalVisibleMemorySize
$freeMem = $os.FreePhysicalMemory
$memPct = [math]::Round((1 - $freeMem / $totalMem) * 100, 1)
$disk = Get-PSDrive -Name C -ErrorAction SilentlyContinue
$diskFreeGB = if ($disk) { [math]::Round($disk.Free / 1GB, 1) } else { 0 }
$diskTotalGB = if ($disk) { [math]::Round(($disk.Free + $disk.Used) / 1GB, 1) } else { 0 }
$diskPct = if ($diskTotalGB -gt 0) { [math]::Round($disk.Used / ($disk.Free + $disk.Used) * 100, 1) } else { 0 }

$issues = @()
$warnings = @()

if ($cpu -gt 90) { $issues += "CPU使用率过高(" + [math]::Round($cpu) + "%)" }
elseif ($cpu -gt 70) { $warnings += "CPU使用率偏高(" + [math]::Round($cpu) + "%)" }

if ($memPct -gt 90) { $issues += "内存占用过高(" + $memPct + "%)" }
elseif ($memPct -gt 70) { $warnings += "内存占用偏高(" + $memPct + "%)" }

if ($diskFreeGB -lt 10) { $issues += "C盘空间不足(" + $diskFreeGB + "GB剩余)" }
elseif ($diskFreeGB -lt 30) { $warnings += "C盘空间紧张(" + $diskFreeGB + "GB剩余)" }

$json = [PSCustomObject]@{
    cpu_pct = [math]::Round($cpu)
    memory_pct = $memPct
    disk_pct = $diskPct
    issues = $issues
    warnings = $warnings
} | ConvertTo-Json -Compress
Write-Output $json
"""
    try:
        out = powershell(script, timeout=15)
        if not out:
            return False, '{"cpu_pct":0,"memory_pct":0,"disk_pct":0,"issues":[],"warnings":[]}'
        return True, out.strip()
    except Exception as e:
        return False, '{"cpu_pct":0,"memory_pct":0,"disk_pct":0,"issues":[],"warnings":[]}'


def move_to_trash(path: str) -> tuple[bool, str]:
    if not path or not isinstance(path, (str, bytes, os.PathLike)):
        return False, "路径为空"
    p = os.path.expanduser(str(path))
    if not os.path.exists(p):
        return False, f"路径不存在: {path}"
    script = (
        "Add-Type -AssemblyName Microsoft.VisualBasic;"
        "$p=$env:JV_PATH;"
        "if(Test-Path -LiteralPath $p -PathType Container){"
        "[Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory("
        "$p,'OnlyErrorDialogs','SendToRecycleBin')}"
        "else{[Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile("
        "$p,'OnlyErrorDialogs','SendToRecycleBin')}"
    )
    err = powershell(script, env={**os.environ, "JV_PATH": p})
    if err and "error" in err.lower():
        return False, f"移动失败: {err}"
    return True, f"已把「{os.path.basename(p)}」移到回收站"


def empty_recycle_bin() -> tuple[bool, str]:
    """清空 Windows 回收站（不可恢复）"""
    script = (
        "Clear-RecycleBin -Force -ErrorAction Stop; "
        "Write-Output 'OK'"
    )
    err = powershell(script, timeout=120)
    if err and "error" in err.lower() and "OK" not in err:
        # 部分系统 Clear-RecycleBin 不可用，回退 COM
        fallback = (
            "$shell = New-Object -ComObject Shell.Application; "
            "$shell.Namespace(10).InvokeVerb('empty'); "
            "Write-Output 'OK'"
        )
        err2 = powershell(fallback, timeout=120)
        if err2 and "error" in err2.lower() and "OK" not in err2:
            detail = (err2 or err or "未知错误")[:120]
            return False, f"清空回收站失败: {detail}"
    return True, "回收站已清空"


def list_directory(path: str = "~") -> tuple[bool, str]:
    p = os.path.expanduser(path)
    if not os.path.isdir(p):
        return False, f"目录不存在: {path}"
    try:
        entries = []
        for name in sorted(os.listdir(p))[:200]:
            full = os.path.join(p, name)
            prefix = "📁" if os.path.isdir(full) else "📄"
            entries.append(f"{prefix} {name}")
        return True, f"{p} 共 {len(entries)} 项：\n" + "\n".join(entries)
    except Exception as e:
        return False, f"列出目录失败: {e}"
