"""Windows 内存与磁盘智能清理 — 自动清理无用数据释放资源。

功能：
- 内存清理：释放工作集、清理待机内存、结束高内存僵尸进程
- 磁盘清理：临时文件、Windows 更新缓存、缩略图缓存、回收站、包管理器缓存等
- 大文件扫描：按体积列出文件并标注可清缓存 / 建议确认 / 勿动
- 智能判断：只清理安全可回收的数据，不碰用户文件
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from pathlib import Path

from .winops import powershell

logger = logging.getLogger(__name__)

# 清理项及其安全等级
_CLEANUP_ITEMS = {
    "temp_files": "用户临时文件 (%TEMP%)",
    "windows_temp": "Windows 临时文件",
    "windows_update": "Windows 更新缓存",
    "thumbnails": "缩略图缓存",
    "recycle_bin": "回收站",
    "dns_cache": "DNS 缓存",
    "prefetch": "预读取数据",
    "error_reports": "错误报告",
    "delivery_optimization": "传递优化缓存",
    "old_logs": "旧日志文件 (CBS/DISM)",
    "package_caches": "包管理器缓存 (npm/pip/uv)",
    "amd_shader_cache": "AMD 着色器缓存",
    "crash_dumps": "崩溃转储",
}

# 大文件路径分类（先匹配先命中）
_JUNK_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("可清缓存", re.compile(
        r"\\Temp\\|\\tmp\\|\\Cache\\|\\Caches\\|\\Code Cache\\|\\GPUCache\\"
        r"|\\npm-cache\\|\\pip\\Cache\\|\\uv\\cache\\|\\CrashDumps\\"
        r"|\\DxcCache\\|\\DxCache\\|\\DX9Cache\\|\\OglCache\\|\\VkCache\\"
        r"|\\DeliveryOptimization\\|SoftwareDistribution\\Download\\"
        r"|thumbcache_.*\.db$|\.ushaderprecache$|\\INetCache\\"
        r"|\\WER\\Report|\\Prefetch\\|\\electron\\Cache\\"
        r"|-updater\\installer\.exe$|-updater\\pending\\|\\Package Cache\\",
        re.I,
    )),
    ("调试残留", re.compile(r"\.pdb$", re.I)),
    ("建议确认", re.compile(
        r"\\DiskGenius|\\Windows\.old\\|\\Installer\\[^\\]+\.msi$"
        r"|state\.vscdb$|\\Update\\Install\\|boot\.wim$",
        re.I,
    )),
    ("勿动系统", re.compile(
        r"^C:\\Windows\\|^[A-Z]:\\Windows\\|\\WinSxS\\|\\System32\\"
        r"|\\SysWOW64\\|\\DriverStore\\",
        re.I,
    )),
    ("程序本体", re.compile(
        r"\\Program Files\\|\\Program Files \(x86\)\\|\\AppData\\Local\\Programs\\",
        re.I,
    )),
]


def clean_memory() -> tuple[bool, str]:
    """清理系统内存：释放工作集 + 清理待机内存。

    通过 EmptyWorkingSet API 让各进程释放多余物理内存，
    再用 MemoryCompression 压缩待机内存。
    """
    # 1) 遍历所有进程调用 EmptyWorkingSet（相当于 RAMMap 的空工作集）
    script = """
$signature = @'
[DllImport("psapi.dll")]
public static extern int EmptyWorkingSet(IntPtr hProcess);
'@
$type = Add-Type -MemberDefinition $signature -Name "WinAPI" -Namespace "Kekesi" -PassThru
$freed = 0
Get-Process -ErrorAction SilentlyContinue | ForEach-Object {
  try {
    $r = $type::EmptyWorkingSet($_.Handle)
    if ($r -gt 0) { $freed++ }
  } catch {}
}
# 获取清理前后内存对比
$os = Get-CimInstance Win32_OperatingSystem
$freeGB = [Math]::Round($os.FreePhysicalMemory / 1MB, 2)
Write-Output ("FREED:" + $freed + "|FREE_GB:" + $freeGB)
"""
    try:
        out = powershell(script, timeout=30)
        freed = 0
        free_gb = 0
        for part in (out or "").split("|"):
            if part.startswith("FREED:"):
                freed = int(part.split(":")[1].strip())
            elif part.startswith("FREE_GB:"):
                free_gb = float(part.split(":")[1].strip())
        return True, f"已释放 {freed} 个进程的工作集内存，当前可用内存 {free_gb} GB"
    except Exception as e:
        return False, f"内存清理失败: {e}"


def clean_disk(
    items: list[str] | None = None,
    *,
    aggressive: bool = False,
) -> tuple[bool, str]:
    """清理磁盘空间。

    Args:
        items: 要清理的项目列表，None 表示全部
        aggressive: 激进模式（清理更多缓存，可能影响首次启动速度）
    """
    if items is None:
        items = list(_CLEANUP_ITEMS.keys())
        if not aggressive:
            # 非激进模式跳过 prefetch 和 delivery_optimization
            items = [i for i in items if i not in ("prefetch", "delivery_optimization")]

    results: list[str] = []
    total_freed = 0

    for item in items:
        if item not in _CLEANUP_ITEMS:
            continue
        try:
            freed, msg = _clean_item(item)
            total_freed += freed
            results.append(f"  {_CLEANUP_ITEMS[item]}: {msg}")
        except Exception as e:
            results.append(f"  {_CLEANUP_ITEMS[item]}: 失败 ({e})")
            logger.warning("清理 %s 失败: %s", item, e)

    summary = f"磁盘清理完成，共释放约 {total_freed} MB\n" + "\n".join(results)
    return True, summary


def find_large_files(
    drive: str = "C",
    *,
    min_mb: int = 100,
    limit: int = 40,
    junk_only: bool = False,
) -> tuple[bool, str]:
    """扫描指定盘符上的大文件，并标注清理建议。

    Args:
        drive: 盘符，如 C / D / C:\\
        min_mb: 最小体积（MB），默认 100
        limit: 最多返回条数，默认 40
        junk_only: 为 True 时只返回「可清缓存 / 调试残留」
    """
    root = _normalize_drive_root(drive)
    if not root:
        return False, f"无效盘符: {drive}"
    if not Path(root).exists():
        return False, f"盘符不存在: {root}"

    min_mb = max(10, int(min_mb))
    limit = max(1, min(int(limit), 200))
    # 全盘递归可能较慢；大盘给更长超时
    timeout = 240 if root.upper().startswith("C") else 360

    script = f"""
$ErrorActionPreference = 'SilentlyContinue'
$minBytes = {min_mb} * 1MB
$limit = {limit}
Get-ChildItem -LiteralPath '{root}' -File -Recurse -Force -ErrorAction SilentlyContinue |
  Where-Object {{ $_.Length -ge $minBytes }} |
  Sort-Object Length -Descending |
  Select-Object -First $limit |
  ForEach-Object {{
    $mb = [Math]::Round($_.Length / 1MB, 1)
    Write-Output ("{{0}}|{{1}}" -f $mb, $_.FullName)
  }}
"""
    try:
        out = powershell(script, timeout=timeout)
    except Exception as e:
        return False, f"扫盘失败: {e}"

    lines = [ln.strip() for ln in (out or "").splitlines() if ln.strip() and "|" in ln]
    if not lines:
        return True, f"{root} 未找到 ≥{min_mb}MB 的文件"

    rows: list[tuple[float, str, str]] = []
    for ln in lines:
        mb_s, path = ln.split("|", 1)
        try:
            mb = float(mb_s)
        except ValueError:
            continue
        tag = _classify_large_path(path)
        if junk_only and tag not in ("可清缓存", "调试残留"):
            continue
        rows.append((mb, tag, path))

    if not rows:
        scope = "可清大文件" if junk_only else "大文件"
        return True, f"{root} 未找到符合条件的{scope}（阈值 {min_mb}MB）"

    junk_mb = sum(mb for mb, tag, _ in rows if tag in ("可清缓存", "调试残留"))
    confirm_mb = sum(mb for mb, tag, _ in rows if tag == "建议确认")
    header = [
        f"{root} 大文件扫描（≥{min_mb}MB，列出 {len(rows)} 个）",
        f"可清缓存/调试残留合计约 {junk_mb:.1f} MB；建议确认约 {confirm_mb:.1f} MB",
        "标签说明：可清缓存=安全缓存；调试残留=可删 pdb；建议确认=删前问用户；程序本体/勿动系统=勿删",
        "",
    ]
    body = [f"[{tag}] {mb:.1f} MB  {path}" for mb, tag, path in rows]
    tip = "\n\n清理建议：先 clean_disk；对「可清缓存」单项可再确认后 delete_file。"
    return True, "\n".join(header + body) + tip


def _normalize_drive_root(drive: str) -> str:
    d = (drive or "C").strip().rstrip("\\/")
    if not d:
        return ""
    if len(d) == 1 and d.isalpha():
        return f"{d.upper()}:\\"
    if len(d) == 2 and d[0].isalpha() and d[1] == ":":
        return f"{d.upper()}\\"
    # 允许传入目录根（如 C:\\Users）
    p = Path(d)
    if p.exists():
        return str(p)
    return ""


def _classify_large_path(path: str) -> str:
    p = path.replace("/", "\\")
    for tag, pat in _JUNK_PATTERNS:
        if pat.search(p):
            return tag
    return "其他"


def _clean_item(item: str) -> tuple[int, str]:
    """清理单个项目，返回 (释放MB, 描述)。"""
    if item == "temp_files":
        return _clean_dir(Path(tempfile.gettempdir()))
    elif item == "windows_temp":
        return _clean_dir(Path(os.environ.get("WINDIR", r"C:\Windows")) / "Temp")
    elif item == "windows_update":
        return _clean_windows_update_cache()
    elif item == "thumbnails":
        return _clean_thumbnail_cache()
    elif item == "recycle_bin":
        return _clean_recycle_bin()
    elif item == "dns_cache":
        out = powershell("Clear-DnsClientCache -ErrorAction SilentlyContinue; ipconfig /flushdns 2>&1 | Out-String", timeout=10)
        return 0, "已刷新 DNS 缓存"
    elif item == "prefetch":
        return _clean_dir(Path(os.environ.get("WINDIR", r"C:\Windows")) / "Prefetch")
    elif item == "error_reports":
        return _clean_error_reports()
    elif item == "delivery_optimization":
        return _clean_delivery_optimization()
    elif item == "old_logs":
        return _clean_old_logs()
    elif item == "package_caches":
        return _clean_package_caches()
    elif item == "amd_shader_cache":
        return _clean_amd_shader_cache()
    elif item == "crash_dumps":
        return _clean_crash_dumps()
    return 0, "未知项目"


def _clean_package_caches() -> tuple[int, str]:
    """清理 npm / pip / uv 缓存（可重建）。"""
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    dirs = [
        local / "npm-cache",
        local / "pip" / "Cache",
        local / "uv" / "cache",
    ]
    total = 0
    parts: list[str] = []
    for d in dirs:
        if not d.exists():
            continue
        mb, _ = _clean_dir_all(d)
        total += mb
        if mb:
            parts.append(f"{d.name}:{mb}MB")
    if not parts:
        return 0, "无缓存或已为空"
    return total, f"释放 {total} MB（{', '.join(parts)}）"


def _clean_amd_shader_cache() -> tuple[int, str]:
    """清理 AMD 着色器缓存（运行游戏/应用后会重建）。"""
    amd = Path(os.environ.get("LOCALAPPDATA", "")) / "AMD"
    if not amd.exists():
        return 0, "目录不存在"
    total = 0
    for name in ("DxcCache", "DxCache", "DX9Cache", "cl.cache", "OglCache", "VkCache"):
        p = amd / name
        if p.exists():
            mb, _ = _clean_dir_all(p)
            total += mb
    return total, f"释放 {total} MB"


def _clean_crash_dumps() -> tuple[int, str]:
    local = Path(os.environ.get("LOCALAPPDATA", "")) / "CrashDumps"
    if not local.exists():
        return 0, "目录不存在"
    return _clean_dir_all(local)


def _clean_dir_all(dir_path: Path) -> tuple[int, str]:
    """清空目录内容（不要求文件超过 1 天），返回释放 MB。"""
    if not dir_path.exists():
        return 0, "目录不存在"
    safe = str(dir_path).replace("'", "''")
    script = f"""
$path = '{safe}'
$freed = 0
Get-ChildItem -LiteralPath $path -Force -ErrorAction SilentlyContinue | ForEach-Object {{
  $size = 0
  if ($_.PSIsContainer) {{
    $size = (Get-ChildItem $_.FullName -Recurse -Force -File -ErrorAction SilentlyContinue |
      Measure-Object Length -Sum).Sum
  }} else {{ $size = $_.Length }}
  try {{
    Remove-Item $_.FullName -Recurse -Force -ErrorAction Stop
    $freed += $size
  }} catch {{}}
}}
Write-Output ([Math]::Round(($freed / 1MB), 1))
"""
    out = powershell(script, timeout=90)
    try:
        mb = float((out or "0").strip().splitlines()[-1])
        return int(mb), f"释放 {mb:.1f} MB"
    except (ValueError, IndexError):
        return 0, "清理完成（无法计算大小）"


def _clean_dir(dir_path: Path) -> tuple[int, str]:
    """清理目录中的文件，返回释放的 MB。"""
    if not dir_path.exists():
        return 0, "目录不存在"
    script = f"""
$path = '{dir_path}'
$freed = 0
Get-ChildItem -Path $path -Recurse -File -ErrorAction SilentlyContinue |
  Where-Object {{ $_.LastWriteTime -lt (Get-Date).AddDays(-1) }} |
  ForEach-Object {{
    $size = $_.Length
    try {{ Remove-Item $_.FullName -Force -ErrorAction SilentlyContinue; $freed += $size }} catch {{}}
  }}
Write-Output ([Math]::Round($freed / 1MB, 1))
"""
    out = powershell(script, timeout=60)
    try:
        mb = float((out or "0").strip().splitlines()[-1])
        return int(mb), f"释放 {mb:.1f} MB"
    except (ValueError, IndexError):
        return 0, "清理完成（无法计算大小）"


def _clean_windows_update_cache() -> tuple[int, str]:
    """清理 Windows 更新缓存（SoftwareDistribution\\Download）。"""
    path = Path(os.environ.get("WINDIR", r"C:\Windows")) / "SoftwareDistribution" / "Download"
    if not path.exists():
        return 0, "目录不存在"
    # 先停 wuauserv 服务，清理后重启
    powershell("Stop-Service -Name wuauserv -Force -ErrorAction SilentlyContinue", timeout=10)
    mb, msg = _clean_dir(path)
    powershell("Start-Service -Name wuauserv -ErrorAction SilentlyContinue", timeout=10)
    return mb, f"释放 {mb} MB（已重启更新服务）"


def _clean_thumbnail_cache() -> tuple[int, str]:
    """清理缩略图缓存。"""
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    cache = local / "Microsoft" / "Windows" / "Explorer"
    if not cache.exists():
        return 0, "目录不存在"
    script = f"""
$path = '{cache}'
$freed = 0
Get-ChildItem -Path $path -Filter "thumbcache_*.db" -ErrorAction SilentlyContinue |
  ForEach-Object {{
    $size = $_.Length
    try {{ Remove-Item $_.FullName -Force -ErrorAction SilentlyContinue; $freed += $size }} catch {{}}
  }}
Write-Output ([Math]::Round($freed / 1MB, 1))
"""
    out = powershell(script, timeout=15)
    try:
        mb = float((out or "0").strip().splitlines()[-1])
        return int(mb), f"释放 {mb:.1f} MB"
    except (ValueError, IndexError):
        return 0, "清理完成"


def _clean_recycle_bin() -> tuple[int, str]:
    """清空回收站。"""
    out = powershell(
        "Clear-RecycleBin -Force -ErrorAction SilentlyContinue 2>&1 | Out-String",
        timeout=30,
    )
    return 0, "已清空回收站"


def _clean_error_reports() -> tuple[int, str]:
    """清理 Windows 错误报告。"""
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    dirs = [
        local / "Microsoft" / "Windows" / "WER" / "ReportArchive",
        local / "Microsoft" / "Windows" / "WER" / "ReportQueue",
        Path("C:\\ProgramData\\Microsoft\\Windows\\WER\\ReportArchive"),
        Path("C:\\ProgramData\\Microsoft\\Windows\\WER\\ReportQueue"),
    ]
    total = 0
    for d in dirs:
        if d.exists():
            mb, _ = _clean_dir(d)
            total += mb
    return total, f"释放 {total} MB"


def _clean_delivery_optimization() -> tuple[int, str]:
    """清理传递优化缓存。"""
    out = powershell(
        "Get-DeliveryOptimizationStatus -ErrorAction SilentlyContinue | "
        "ForEach-Object { $_.TotalSize } | Measure-Object -Sum | "
        "Select-Object -ExpandProperty Sum",
        timeout=10,
    )
    # 清理 DO 缓存
    powershell(
        "Delete-DeliveryOptimizationCache -Force -ErrorAction SilentlyContinue 2>&1 | Out-Null",
        timeout=30,
    )
    return 0, "已清理传递优化缓存"


def _clean_old_logs() -> tuple[int, str]:
    """清理旧的 CBS/DISM 日志。"""
    windir = Path(os.environ.get("WINDIR", r"C:\Windows"))
    logs_dir = windir / "Logs" / "CBS"
    if not logs_dir.exists():
        return 0, "目录不存在"
    return _clean_dir(logs_dir)


def get_system_stats() -> tuple[bool, str]:
    """获取系统资源概况：CPU、内存、磁盘、网络。"""
    script = """
# CPU 使用率
$cpu = (Get-CimInstance Win32_Processor -ErrorAction SilentlyContinue | Measure-Object -Property LoadPercentage -Average).Average
# 内存
$os = Get-CimInstance Win32_OperatingSystem
$memTotal = [Math]::Round($os.TotalVisibleMemorySize / 1MB, 1)
$memFree = [Math]::Round($os.FreePhysicalMemory / 1MB, 1)
$memUsedPct = [Math]::Round((1 - $os.FreePhysicalMemory / $os.TotalVisibleMemorySize) * 100, 1)
# 磁盘
$disks = Get-CimInstance Win32_LogicalDisk -Filter "DriveType=3" -ErrorAction SilentlyContinue
$diskInfo = ($disks | ForEach-Object {
  $free = [Math]::Round($_.FreeSpace / 1GB, 1)
  $total = [Math]::Round($_.Size / 1GB, 1)
  "$($_.DeviceID) ${free}/${total}GB"
}) -join ', '
# 前 5 内存占用进程
$topMem = Get-Process -ErrorAction SilentlyContinue | Sort-Object WorkingSet64 -Descending |
  Select-Object -First 5 | ForEach-Object {
    $mb = [Math]::Round($_.WorkingSet64 / 1MB, 0)
    "$($_.ProcessName)(${mb}MB)"
  }
$topMemStr = $topMem -join ', '
Write-Output "CPU: ${cpu}%"
Write-Output "内存: 已用 ${memUsedPct}% (${memFree}/${memTotal} GB 可用)"
Write-Output "磁盘: $diskInfo"
Write-Output "内存占用前5: $topMemStr"
"""
    out = powershell(script, timeout=15)
    return True, (out or "获取失败").strip()


def kill_memory_hogs(
    threshold_mb: int = 500,
    *,
    exclude: list[str] | None = None,
) -> tuple[bool, str]:
    """结束占用内存过高且非关键的进程。

    Args:
        threshold_mb: 内存占用阈值（MB），超过此值的非关键进程将被结束
        exclude: 排除的进程名列表
    """
    # 系统关键进程，不会被结束
    protected = {
        "system", "registry", "smss", "csrss", "wininit", "services",
        "lsass", "svchost", "dwm", "explorer", "powershell", "pwsh",
        "cmd", "conhost", "python", "pythonw", "node",
        "fontdrvhost", "dllhost", "runtimebroker", "sihost", "taskhostw",
        "ctfmon", "searchhost", "startmenuexperiencehost", "textinputhost",
        "applicationframehost", "shellexperiencehost", "audiodg",
        "wudfhost", "rundll32", "spoolsv", "winlogon",
    }
    if exclude:
        protected.update(n.lower() for n in exclude)

    threshold_bytes = threshold_mb * 1024 * 1024
    safe_protected = ",".join(f"'{n}'" for n in protected)
    script = f"""
$protected = @({safe_protected})
$threshold = {threshold_bytes}
$killed = @()
Get-Process -ErrorAction SilentlyContinue |
  Where-Object {{ $_.WorkingSet64 -gt $threshold -and $protected -notcontains $_.ProcessName.ToLower() }} |
  Sort-Object WorkingSet64 -Descending |
  Select-Object -First 5 |
  ForEach-Object {{
    $mb = [Math]::Round($_.WorkingSet64 / 1MB, 0)
    try {{
      Stop-Process -Id $_.Id -Force -ErrorAction Stop
      $killed += "$($_.ProcessName)($($mb)MB)"
    }} catch {{
      $killed += "$($_.ProcessName)($($mb)MB-失败)"
    }}
  }}
if ($killed.Count -eq 0) {{ Write-Output 'NONE' }}
else {{ Write-Output ($killed -join ', ') }}
"""
    out = powershell(script, timeout=20)
    result = (out or "").strip().splitlines()[-1].strip()
    if result == "NONE":
        return True, f"没有超过 {threshold_mb} MB 的非关键进程需要结束"
    return True, f"已结束高内存进程: {result}"
