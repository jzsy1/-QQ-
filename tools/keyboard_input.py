"""模拟键盘输入（Windows：PowerShell SendKeys，支持中文，保护剪贴板）。

设计要点：
- type_text 用单次 PowerShell 调用完成：备份剪贴板 → 设置新内容 → Ctrl+V → 恢复原剪贴板
- send_keys 用 PowerShell SendKeys（稳定可靠，无系统消息队列风险）
- 不使用 ctypes keybd_event（无间隔连续发送曾导致系统卡死）
"""

from __future__ import annotations

import base64
import platform
import subprocess
import time


def type_text(text: str, delay_sec: float = 0.3) -> tuple[bool, str]:
    """向当前焦点窗口输入文本（支持中文，保护剪贴板）。

    单次 PowerShell 调用完成：备份 → 设置 → Ctrl+V → 恢复。
    """
    if not text:
        return False, "没有可输入的内容"
    if platform.system() != "Windows":
        return False, "自动输入目前仅支持 Windows"

    if delay_sec > 0:
        time.sleep(delay_sec)

    # 用 base64 传递文本，避免转义问题
    b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")

    # 单次 PowerShell 完成全部流程：
    # 1. $old = Get-Clipboard（备份用户剪贴板）
    # 2. Set-Clipboard（设置新内容）
    # 3. SendWait('^v')（粘贴）
    # 4. Start-Sleep 等 Ctrl+V 完成
    # 5. Set-Clipboard $old（恢复用户剪贴板）
    ps = f"""
$ErrorActionPreference = 'Stop'
try {{
    $old = Get-Clipboard
    $bytes = [Convert]::FromBase64String('{b64}')
    $content = [Text.Encoding]::UTF8.GetString($bytes)
    Set-Clipboard -Value $content
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.SendKeys]::SendWait('^v')
    Start-Sleep -Milliseconds 100
    if ($old) {{ Set-Clipboard -Value $old }}
    Write-Output 'OK'
}} catch {{
    Write-Output "ERR:$($_.Exception.Message)"
}}
"""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        out = (result.stdout or "").strip()
        if out == "OK":
            return True, f"已输入 {len(text)} 个字符"
        if out.startswith("ERR:"):
            return False, f"输入失败: {out[4:]}"
        err = (result.stderr or "").strip()
        return False, f"输入失败: {err or out or 'unknown error'}"
    except Exception as e:
        return False, f"输入失败: {e}"


def send_keys(keys: str, delay_sec: float = 0.15) -> tuple[bool, str]:
    """发送快捷键（SendKeys 语法：^f=Ctrl+F, %f=Alt+F, +f=Shift+F, {ENTER}, {F1}）。

    用 PowerShell SendKeys 发送，稳定可靠。
    """
    if not keys:
        return False, "快捷键为空"
    if platform.system() != "Windows":
        return False, "快捷键目前仅支持 Windows"

    if delay_sec > 0:
        time.sleep(delay_sec)

    # 转义单引号（PowerShell 字符串）
    safe = keys.replace("'", "''")

    ps = f"""
$ErrorActionPreference = 'Stop'
try {{
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.SendKeys]::SendWait('{safe}')
    Write-Output 'OK'
}} catch {{
    Write-Output "ERR:$($_.Exception.Message)"
}}
"""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        out = (result.stdout or "").strip()
        if out == "OK":
            return True, "快捷键已发送"
        if out.startswith("ERR:"):
            return False, f"按键失败: {out[4:]}"
        err = (result.stderr or "").strip()
        return False, f"按键失败: {err or out or 'unknown error'}"
    except Exception as e:
        return False, f"按键失败: {e}"
