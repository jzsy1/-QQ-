"""Windows 系统自修复 — 检测系统异常并自动修复。

功能：
- 系统健康检查：检测关键服务、磁盘、内存、网络异常
- 自动修复：服务崩溃自动重启、系统文件损坏自动修复
- 事件日志监控：检测关键错误并触发修复
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from .system_admin import control_service, list_services
from .winops import powershell

logger = logging.getLogger(__name__)

# 关键系统服务：这些服务崩溃会导致系统功能异常
_CRITICAL_SERVICES = [
    "Audiosrv",          # 音频
    "Dnscache",          # DNS 缓存
    "Dhcp",              # DHCP（网络）
    "Winmgmt",           # WMI
    "EventLog",          # 事件日志
    "Schedule",          # 计划任务
    "Spooler",           # 打印
    "Themes",            # 主题（桌面渲染）
    "WlanSvc",           # WLAN（笔记本）
    "WlanAutoConfig",    # WLAN 自动配置
    "Netprofm",          # 网络配置文件
    "NlaSvc",            # 网络感知
    "TrkWks",            # 分布式链接跟踪
    "SysMain",           # 系统主服务
    "BFE",               # 基础筛选引擎（防火墙）
    "MPSSVC",            # Windows 防火墙
]


@dataclass
class HealthIssue:
    """系统健康问题。"""
    category: str       # service / disk / memory / network / system_file
    severity: str       # critical / warning / info
    description: str
    auto_fixable: bool
    fixed: bool = False
    fix_detail: str = ""


@dataclass
class HealthReport:
    """系统健康报告。"""
    issues: list[HealthIssue] = field(default_factory=list)
    summary: str = ""

    @property
    def has_critical(self) -> bool:
        return any(i.severity == "critical" for i in self.issues)

    @property
    def unfixed(self) -> list[HealthIssue]:
        return [i for i in self.issues if not i.fixed]


def check_system_health(*, auto_fix: bool = False) -> HealthReport:
    """全面系统健康检查。

    Args:
        auto_fix: 是否自动修复发现的问题
    """
    report = HealthReport()

    # 1) 关键服务检查
    _check_services(report, auto_fix=auto_fix)

    # 2) 磁盘空间检查
    _check_disk_space(report)

    # 3) 内存使用检查
    _check_memory(report)

    # 4) 系统文件完整性
    _check_system_files(report, auto_fix=auto_fix)

    # 5) 最近的系统错误事件
    _check_recent_errors(report)

    # 汇总
    total = len(report.issues)
    fixed = sum(1 for i in report.issues if i.fixed)
    crit = sum(1 for i in report.issues if i.severity == "critical")
    if total == 0:
        report.summary = "系统健康，未发现异常"
    else:
        report.summary = f"发现 {total} 个问题（{crit} 个严重），已修复 {fixed} 个"

    return report


def _check_services(report: HealthReport, *, auto_fix: bool = False) -> None:
    """检查关键系统服务是否在运行。"""
    ok, out = list_services(status="running", limit=200)
    if not ok:
        report.issues.append(HealthIssue(
            category="service",
            severity="warning",
            description="无法获取服务列表",
            auto_fixable=False,
        ))
        return

    running_names: set[str] = set()
    for line in (out or "").splitlines():
        parts = line.split("|")
        if len(parts) >= 2:
            running_names.add(parts[0].strip().lower())

    for svc in _CRITICAL_SERVICES:
        if svc.lower() not in running_names:
            issue = HealthIssue(
                category="service",
                severity="critical",
                description=f"关键服务 {svc} 未运行",
                auto_fixable=True,
            )
            if auto_fix:
                ok2, msg = control_service(svc, "start")
                if ok2:
                    issue.fixed = True
                    issue.fix_detail = f"已自动启动: {msg}"
                else:
                    issue.fix_detail = f"自动启动失败: {msg}"
            report.issues.append(issue)


def _check_disk_space(report: HealthReport) -> None:
    """检查磁盘空间是否不足。"""
    script = """
Get-CimInstance Win32_LogicalDisk -Filter "DriveType=3" -ErrorAction SilentlyContinue |
  ForEach-Object {
    $freeGB = [Math]::Round($_.FreeSpace / 1GB, 1)
    $totalGB = [Math]::Round($_.Size / 1GB, 1)
    $pct = if ($_.Size -gt 0) { [Math]::Round(($_.FreeSpace / $_.Size) * 100, 1) } else { 0 }
    Write-Output ("{0}|{1}|{2}|{3}" -f $_.DeviceID, $freeGB, $totalGB, $pct)
  }
"""
    out = powershell(script, timeout=10)
    for line in (out or "").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("|")
        if len(parts) < 4:
            continue
        drive, free_gb, total_gb, pct_free = parts[0], parts[1], parts[2], parts[3]
        try:
            free = float(free_gb)
            pct = float(pct_free)
        except ValueError:
            continue
        if free < 1.0:
            report.issues.append(HealthIssue(
                category="disk",
                severity="critical",
                description=f"磁盘 {drive} 空间严重不足：剩余 {free_gb} GB / {total_gb} GB ({pct_free}%)",
                auto_fixable=True,
            ))
        elif free < 5.0 or pct < 10:
            report.issues.append(HealthIssue(
                category="disk",
                severity="warning",
                description=f"磁盘 {drive} 空间偏低：剩余 {free_gb} GB / {total_gb} GB ({pct_free}%)",
                auto_fixable=True,
            ))


def _check_memory(report: HealthReport) -> None:
    """检查内存使用情况。"""
    script = """
$os = Get-CimInstance Win32_OperatingSystem -ErrorAction SilentlyContinue
if ($os) {
  $totalGB = [Math]::Round($os.TotalVisibleMemorySize / 1MB, 1)
  $freeGB = [Math]::Round($os.FreePhysicalMemory / 1MB, 1)
  $pctFree = if ($os.TotalVisibleMemorySize -gt 0) { [Math]::Round(($os.FreePhysicalMemory / $os.TotalVisibleMemorySize) * 100, 1) } else { 0 }
  Write-Output ("{0}|{1}|{2}" -f $freeGB, $totalGB, $pctFree)
}
"""
    out = powershell(script, timeout=8)
    line = (out or "").strip()
    if not line:
        return
    parts = line.split("|")
    if len(parts) < 3:
        return
    try:
        free_gb, total_gb, pct_free = float(parts[0]), float(parts[1]), float(parts[2])
    except ValueError:
        return
    if pct_free < 5:
        report.issues.append(HealthIssue(
            category="memory",
            severity="critical",
            description=f"内存严重不足：剩余 {free_gb} GB / {total_gb} GB ({pct_free}%)",
            auto_fixable=True,
        ))
    elif pct_free < 15:
        report.issues.append(HealthIssue(
            category="memory",
            severity="warning",
            description=f"内存偏低：剩余 {free_gb} GB / {total_gb} GB ({pct_free}%)",
            auto_fixable=True,
        ))


def _check_system_files(report: HealthReport, *, auto_fix: bool = False) -> None:
    """检查系统文件完整性（快速扫描）。"""
    # 用 sfc /verifyonly 做快速检查（不修复），比 /scannow 快得多
    # 注意：sfc 需要管理员权限
    script = """
$sfcLog = "$env:TEMP\\kekesi_sfc_verify.txt"
$proc = Start-Process -FilePath sfc.exe -ArgumentList '/verifyonly' -Wait -PassThru -WindowStyle Hidden -RedirectStandardOutput $sfcLog -ErrorAction SilentlyContinue
if (Test-Path $sfcLog) {
  $content = Get-Content $sfcLog -Raw -ErrorAction SilentlyContinue
  if ($content -match 'found integrity violations') {
    Write-Output "VIOLATIONS"
  } elseif ($content -match 'did not find any integrity violations') {
    Write-Output "OK"
  } else {
    Write-Output "UNKNOWN"
  }
} else {
  Write-Output "NO_LOG"
}
"""
    try:
        out = powershell(script, timeout=120)
        if "VIOLATIONS" in (out or ""):
            issue = HealthIssue(
                category="system_file",
                severity="critical",
                description="系统文件完整性检查发现损坏",
                auto_fixable=True,
            )
            if auto_fix:
                fix_out = powershell(
                    "sfc /scannow 2>&1 | Select-Object -Last 3 | Out-String",
                    timeout=300,
                )
                if "successfully" in (fix_out or "").lower() or "成功" in (fix_out or ""):
                    issue.fixed = True
                    issue.fix_detail = "sfc /scannow 已修复系统文件"
                else:
                    issue.fix_detail = f"sfc 修复结果: {(fix_out or '').strip()[:200]}"
            report.issues.append(issue)
    except Exception as e:
        logger.warning("系统文件检查失败: %s", e)


def _check_recent_errors(report: HealthReport) -> None:
    """检查最近 1 小时的系统错误事件。"""
    script = """
Get-WinEvent -FilterHashtable @{
  LogName='System'; Level=2; StartTime=(Get-Date).AddHours(-1)
} -MaxEvents 5 -ErrorAction SilentlyContinue |
  ForEach-Object {
    Write-Output ("{0}|{1}|{2}" -f $_.TimeCreated.ToString('HH:mm:ss'), $_.Id, ($_.Message -replace '[\\r\\n]+',' ' -replace '\\s+',' ').Substring(0, [Math]::Min(120, $_.Message.Length)))
  }
"""
    out = powershell(script, timeout=10)
    for line in (out or "").splitlines():
        line = line.strip()
        if not line:
            continue
        report.issues.append(HealthIssue(
            category="event_log",
            severity="warning",
            description=f"系统错误事件: {line}",
            auto_fixable=False,
        ))


def repair_system() -> tuple[bool, str]:
    """一键修复系统：运行健康检查并自动修复所有可修复的问题。"""
    report = check_system_health(auto_fix=True)
    lines = [report.summary]
    for issue in report.issues:
        status = "已修复" if issue.fixed else ("可修复但未执行" if issue.auto_fixable else "无法自动修复")
        lines.append(f"  [{issue.severity}] {issue.description} → {status}")
        if issue.fix_detail:
            lines.append(f"    详情: {issue.fix_detail}")
    return True, "\n".join(lines)
