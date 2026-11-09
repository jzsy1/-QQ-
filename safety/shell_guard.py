"""Shell 命令安全解析 — 拦截高危破坏性命令。"""

from __future__ import annotations

import re

# 绝对禁止（即使口头确认也不经 shell 直跑；应走专用工具）
_BLOCK_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"format\s+[a-z]:", re.I), "禁止格式化磁盘"),
    (re.compile(r"\b(rm\s+-rf\s+/|del\s+/s\s+/q\s+[cC]:\\)", re.I), "禁止递归清空系统盘"),
    (re.compile(r"Remove-Item\s+.*-[Rr]ecurse.*[cC]:\\", re.I), "禁止递归删除系统盘"),
    (re.compile(r"diskpart", re.I), "禁止 diskpart，请用专用工具"),
    (re.compile(r"bcdedit", re.I), "禁止修改启动配置"),
    (re.compile(r"reg\s+delete\s+HKLM", re.I), "禁止直接删除 HKLM，请用 write_registry 等工具"),
    (re.compile(r"Shutdown\s+/s|/r\b|Stop-Computer|Restart-Computer", re.I), "关机/重启请用 system_power 工具"),
    (re.compile(r"Invoke-Expression\s*\(|IEX\s*\(|DownloadString\s*\(", re.I), "禁止远程下载执行脚本"),
    (re.compile(r"curl\s+.+\|\s*(ba)?sh", re.I), "禁止管道执行远程脚本"),
]

# 只读/查询类命令：直接放行，无需确认（降低日常啰嗦度）
_READONLY_RE = re.compile(
    r"^\s*(?:"
    r"Get-\w+|Select-\w+|Measure-\w+|Test-Path|Test-Connection|Test-NetConnection|"
    r"Resolve-DnsName|Write-Output|Write-Host|echo|"
    r"dir|ls|type|cat|where(?:\.exe)?|findstr|"
    r"ipconfig|ping|tracert|nslookup|netstat|arp|route\s+print|"
    r"systeminfo|hostname|whoami|ver|tasklist|driverquery|"
    r"wmic\s+\w+\s+(?:get|list)|"
    r"git\s+(?:status|log|diff|branch|show|remote)|"
    r"python\s+--version|node\s+--version|npm\s+(?:ls|list|view)"
    r")\b[^|;&]*$",
    re.I,
)

# 需口头确认（与 guard S3 叠加）
_CONFIRM_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"Remove-Item|rm\s+-|del\s+/|rmdir", re.I), "删除类命令"),
    (re.compile(r"Stop-Process|taskkill|kill\s+", re.I), "结束进程类命令"),
    (re.compile(r"net\s+user|Set-LocalUser|Disable-LocalUser", re.I), "用户账户类命令"),
    (re.compile(r"Set-Service|Stop-Service|sc\s+delete", re.I), "服务变更类命令"),
    (re.compile(r"New-NetFirewallRule|Set-NetFirewallProfile|netsh\s+advfirewall", re.I), "防火墙变更"),
    (re.compile(r"cipher\s+/w|Clear-RecycleBin", re.I), "不可恢复清理"),
]


def is_readonly_command(command: str) -> bool:
    """纯查询类命令（无管道改写、无危险动词）。"""
    cmd = (command or "").strip()
    if not cmd:
        return False
    return bool(_READONLY_RE.match(cmd))


def check_shell_command(command: str) -> tuple[bool, str, bool]:
    """返回 (允许继续, 原因, 是否仍需口头确认)。

    允许=False 表示硬拦截；允许=True 且 needs_confirm=True 表示交给 guard 确认词。
    只读命令直接 (True, "", False)。
    """
    cmd = (command or "").strip()
    if not cmd:
        return False, "命令不能为空", False

    for pat, reason in _BLOCK_PATTERNS:
        if pat.search(cmd):
            return False, f"Shell 安全闸门拦截：{reason}", False

    if is_readonly_command(cmd):
        return True, "", False

    for pat, reason in _CONFIRM_PATTERNS:
        if pat.search(cmd):
            return True, reason, True

    return True, "", False
