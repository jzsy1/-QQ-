"""Windows 进程检测与窗口聚焦"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import time
from pathlib import Path

from ..app_aliases import NAME_ALIASES
from .desktop_apps import find_desktop_app
from .winops import focus_window_by_process_names, powershell

logger = logging.getLogger(__name__)

_PROTECTED = frozenset(
    {
        "system",
        "registry",
        "smss",
        "csrss",
        "wininit",
        "services",
        "lsass",
        "svchost",
        "dwm",
        "explorer",
        "powershell",
        "pwsh",
        "cmd",
        "conhost",
        "python",
        "pythonw",
        "node",
        "rundll32",
        "regsvr32",
        "wscript",
        "cscript",
        "msiexec",
        "taskkill",
        "tasklist",
        "wmic",
        "sihost",
        "taskhostw",
        "ctfmon",
        "fontdrvhost",
        "dllhost",
        "runtimebroker",
        "applicationframehost",
        "shellexperiencehost",
        "startmenuexperiencehost",
        "textinputhost",
        "searchhost",
        "searchui",
        # 体内 WebView 宿主子进程：close_app「音乐」误杀会导致点歌崩溃
        "msedgewebview2",
        "webview2",
        # Shell / CLI 工具进程：防止命令行含关键词时被当作桌面应用误学或误杀
        "bash",
        "sh",
        "zsh",
        "git",
        "ssh",
    }
)

# 辅助/后台进程：不代表应用真正在运行（崩溃报告、更新器、渲染子进程等）
_HELPER_PROCESSES = frozenset(
    {
        "crashpad_handler",
        "crashpad",
        "crashreporter",
        "crashreporterclient",
        "updater",
        "update",
        "elevation",
        "helper",
        "renderer",
        "gpu-process",
        "utility",
        "sandbox",
        "watchdog",
        "monitor",
        "tray",
        "minidump",
        "conhost",
        "broker",
    }
)


_LNK_STEM_CACHE: dict[str, str | None] = {}


def _native_process_map() -> dict[int, str] | None:
    """使用 Win32 Toolhelp32 快照获取 {pid: 进程名(不含.exe)}，耗时 <1ms，零子进程。"""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        TH32CS_SNAPPROCESS = 0x2
        INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

        class _PE32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_wchar * 260),
            ]

        k32 = ctypes.windll.kernel32
        k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        k32.Process32FirstW.restype = wintypes.BOOL
        k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PE32W)]
        k32.Process32NextW.restype = wintypes.BOOL
        k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PE32W)]

        snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if not snap or snap == INVALID_HANDLE_VALUE:
            return None
        procs: dict[int, str] = {}
        try:
            entry = _PE32W()
            entry.dwSize = ctypes.sizeof(_PE32W)
            alive = k32.Process32FirstW(snap, ctypes.byref(entry))
            while alive:
                pid = int(entry.th32ProcessID)
                exe = str(entry.szExeFile or "")
                stem = exe[:-4] if exe.lower().endswith(".exe") else exe
                procs[pid] = stem
                alive = k32.Process32NextW(snap, ctypes.byref(entry))
        finally:
            k32.CloseHandle(snap)
        return procs
    except Exception:
        return None


def _native_visible_windows() -> list[tuple[int, str]] | None:
    """使用 Win32 EnumWindows 获取所有可见顶层窗口的 (pid, title)，耗时 <1ms。"""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        results: list[tuple[int, str]] = []

        def _cb(hwnd, _lparam):
            if user32.IsWindowVisible(hwnd):
                length = user32.GetWindowTextLengthW(hwnd)
                if length > 0:
                    buf = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(hwnd, buf, length + 1)
                    title = buf.value.strip()
                    if title:
                        pid = wintypes.DWORD(0)
                        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                        results.append((int(pid.value), title))
            return True

        user32.EnumWindows(WNDENUMPROC(_cb), 0)
        return results
    except Exception:
        return None


def _process_name_from_path(path: str) -> str | None:
    """从 exe 或 lnk 快捷方式解析进程名（带内存缓存，避免轮询时重复拉起 PowerShell）。"""
    p = (path or "").strip()
    if not p:
        return None
    if p.lower().endswith(".exe") and os.path.isfile(p):
        return Path(p).stem
    if p.lower().endswith(".lnk") and os.path.isfile(p):
        cache_key = os.path.normcase(os.path.abspath(p))
        if cache_key in _LNK_STEM_CACHE:
            return _LNK_STEM_CACHE[cache_key]
        safe = p.replace("'", "''")
        script = (
            f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut('{safe}'); "
            f"Write-Output $s.TargetPath"
        )
        stem: str | None = None
        try:
            target = powershell(script, timeout=8).strip()
            if target.lower().endswith(".exe") and os.path.isfile(target):
                stem = Path(target).stem
        except Exception as e:
            logger.debug("[ProcessOps] 提取进程名失败: %s", e)
        _LNK_STEM_CACHE[cache_key] = stem
        return stem
    return None


def infer_process_names(app_name: str, launch_target: str = "") -> list[str]:
    names: list[str] = []
    key = (app_name or "").strip()
    if not key:
        return []

    lower = key.lower()
    if lower in NAME_ALIASES:
        key = NAME_ALIASES[lower]
        # 别名映射后的进程名（如"记事本"→"notepad"）直接作为候选：
        # Win11 的 Notepad 可能是 UWP/英文版，桌面索引找不到快捷方式，
        # 但进程名稳定可用，直接加入让后续 PID 反查能命中。
        if re.fullmatch(r"[\w-]+", key, re.I):
            names.append(key)

    for candidate in {key, app_name.strip()}:
        hit = find_desktop_app(candidate)
        if hit:
            _display, path = hit
            stem = _process_name_from_path(path)
            if stem:
                names.append(stem)

    target = (launch_target or "").strip()
    if target:
        stem = _process_name_from_path(target)
        if stem:
            names.append(stem)
        bare = target.replace(".exe", "", 1).strip()
        if bare and re.fullmatch(r"[\w-]+", bare, re.I) and not os.path.isfile(target):
            names.append(bare)
        if target.lower().endswith(".exe") and os.path.isfile(target):
            names.append(Path(target).stem)

    seen: set[str] = set()
    result: list[str] = []
    for n in names:
        k = n.lower()
        if k not in seen:
            seen.add(k)
            result.append(n)
    return result


def is_app_running(app_name: str, launch_target: str = "", memory=None) -> bool:
    names = _collect_running_process_names(app_name, launch_target, memory)
    if not names:
        # UWP 应用：通过窗口标题或 AUMID 检测
        if launch_target.startswith("appid:"):
            return _is_uwp_app_running(launch_target[6:].strip(), app_name)
        return False
    proc_map = _native_process_map()
    if proc_map is not None:
        running_lower = {s.lower() for s in proc_map.values() if s}
        for n in names:
            if n.lower() in running_lower:
                return True
        if app_name:
            wins = _native_visible_windows() or []
            for _pid, title in wins:
                if app_name in title:
                    return True
        return False
    # 除了进程名，也检查窗口标题（某些应用进程名与显示名差异大）
    name_ps = ",".join(f"'{n.replace(chr(39), chr(39)*2)}'" for n in names)
    display_safe = app_name.replace("'", "''")
    script = f"""
$names = @({name_ps})
$display = '{display_safe}'
# 1) 按进程名检测
foreach ($n in $names) {{
  if (Get-Process -Name $n -ErrorAction SilentlyContinue) {{ exit 0 }}
}}
# 2) 按窗口标题检测（覆盖进程名不匹配但窗口标题包含应用名的场景）
Get-Process -ErrorAction SilentlyContinue | ForEach-Object {{
  $title = $_.MainWindowTitle
  if ($title -and $title.Contains($display)) {{ exit 0 }}
}}
exit 1
"""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return r.returncode == 0
    except Exception as e:
        logger.warning("进程检测失败 %s: %s", app_name, e)
        return False


def _is_uwp_app_running(aumid: str, display_name: str = "") -> bool:
    """检测 UWP/Store 应用是否在运行（通过窗口标题或包名进程）。"""
    display_safe = (display_name or "").replace("'", "''")
    # UWP 应用进程名通常包含包名的简写形式
    pkg_family = aumid.split("!")[0] if "!" in aumid else aumid
    pkg_short = pkg_family.split("_")[0] if "_" in pkg_family else pkg_family
    pkg_short_safe = pkg_short.replace("'", "''")
    script = f"""
$display = '{display_safe}'
$pkgShort = '{pkg_short_safe}'
# 当前进程及全部祖先：宿主进程命令行天然包含本次脚本文本（AUMID/包名），属构造性误报
$chain = New-Object System.Collections.Generic.HashSet[int]
$cur = $PID
for ($i = 0; $i -lt 24 -and $cur -gt 4; $i++) {{
  [void]$chain.Add($cur)
  $pp = Get-CimInstance Win32_Process -Filter "ProcessId=$cur" -ErrorAction SilentlyContinue
  if (-not $pp) {{ break }}
  $cur = [int]$pp.ParentProcessId
}}
# 1) 检查窗口标题是否包含应用显示名
Get-Process -ErrorAction SilentlyContinue | ForEach-Object {{
  $title = $_.MainWindowTitle
  if ($title -and (($display -and $title.Contains($display)) -or ($pkgShort -and $_.ProcessName.ToLower().Contains($pkgShort.ToLower())))) {{ exit 0 }}
}}
# 2) 检查 Win32_Process 命令行是否包含包名简写（UWP 进程 CL 是 exe 路径，
#    含 "包名_版本_架构__族名"，不含 !App 后缀，因此必须用 pkgShort 而非完整 AUMID）
Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | ForEach-Object {{
  if ($chain -contains $_.ProcessId) {{ return }}
  $cl = $_.CommandLine
  if ($cl -and $pkgShort -and $cl.Contains($pkgShort)) {{ exit 0 }}
}}
exit 1
"""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return r.returncode == 0
    except Exception as e:
        logger.warning("UWP 应用检测失败 %s: %s", aumid, e)
        return False


def _uwp_app_has_main_window(aumid: str, display_name: str = "") -> bool:
    """检测 UWP/Store 应用是否在运行且存在可见主窗口。

    与 is_app_running 的 UWP 检测对齐，但要求进程存在非零主窗口句柄，
    避免「进程在跑但只有托盘/后台」时误判为已完成启动。
    """
    display_safe = (display_name or "").replace("'", "''")
    pkg_family = aumid.split("!")[0] if "!" in aumid else aumid
    pkg_short = pkg_family.split("_")[0] if "_" in pkg_family else pkg_family
    pkg_short_safe = pkg_short.replace("'", "''")
    script = f"""
$display = '{display_safe}'
$pkgShort = '{pkg_short_safe}'
# 当前进程及全部祖先：宿主进程命令行天然包含本次脚本文本（AUMID/包名），属构造性误报
$chain = New-Object System.Collections.Generic.HashSet[int]
$cur = $PID
for ($i = 0; $i -lt 24 -and $cur -gt 4; $i++) {{
  [void]$chain.Add($cur)
  $pp = Get-CimInstance Win32_Process -Filter "ProcessId=$cur" -ErrorAction SilentlyContinue
  if (-not $pp) {{ break }}
  $cur = [int]$pp.ParentProcessId
}}
# 1) 进程存在可见主窗口，且 标题含显示名 或 进程名含包简写
Get-Process -ErrorAction SilentlyContinue | ForEach-Object {{
  if ($_.MainWindowHandle -eq [IntPtr]::Zero) {{ return }}
  $title = $_.MainWindowTitle
  $pn = $_.ProcessName.ToLower()
  if (($display -and $title -and $title.ToLower().Contains($display.ToLower())) -or
      ($pkgShort -and $pn.Contains($pkgShort.ToLower()))) {{ exit 0 }}
}}
# 2) 命令行含包名简写的进程（UWP 进程 CL 是 exe 路径，不含完整 AUMID），
#    且存在可见主窗口
Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | ForEach-Object {{
  if ($chain -contains $_.ProcessId) {{ return }}
  $cl = $_.CommandLine
  if ($cl -and $pkgShort -and $cl.Contains($pkgShort)) {{
    $p = Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue
    if ($p -and $p.MainWindowHandle -ne [IntPtr]::Zero) {{ exit 0 }}
  }}
}}
exit 1
"""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if r.returncode == 0:
            return True
    except Exception as e:
        logger.warning("UWP 主窗口检测失败 %s: %s", aumid, e)
    # 窗口未就绪但进程已在跑（UWP 冷启动慢、后台会话无交互桌面）：
    # 只要确实启动了进程即视为启动成功，避免 open_app 将已启动应用误报为失败。
    running = _is_uwp_app_running(aumid, display_name)
    if running:
        logger.info(
            "UWP 应用已启动但主窗口暂未确认（回退进程判定）: %s", display_name
        )
    return running


def _collect_running_process_names(
    app_name: str, launch_target: str = "", memory=None
) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()

    def _add(items: list[str]):
        for n in items:
            stem = (n or "").replace(".exe", "", 1).strip()
            if not stem:
                continue
            k = stem.lower()
            if k in _PROTECTED or k in _HELPER_PROCESSES:
                continue
            if k not in seen:
                seen.add(k)
                names.append(stem)

    if memory:
        _add(memory.get_app_process_names(app_name))
    _add(infer_process_names(app_name, launch_target))
    if not names:
        # 仅在记忆与路径推断均未命中时才做全量进程发现，先走原生快照匹配
        proc_map = _native_process_map()
        wins = _native_visible_windows()
        kw = (app_name or "").strip().lower()
        if kw and proc_map is not None:
            compact = re.sub(r"[\s_\-+\.]+", "", kw)
            for pid, stem in proc_map.items():
                sl = stem.lower()
                if kw in sl or (compact and compact in sl.replace("_", "")):
                    _add([stem])
            if wins:
                for pid, title in wins:
                    if kw in title.lower() and pid in proc_map:
                        _add([proc_map[pid]])
        if not names:
            _add(discover_process_names(app_name))
    return names


def has_app_main_window(
    app_name: str, launch_target: str = "", memory=None
) -> bool:
    """进程在跑且存在可见主窗口（非仅托盘/后台）。

    对 UWP/Store 应用（launch_target 为 appid:）优先走 UWP 专属路径：
    普通进程名反查（infer/discover）对 UWP 常把无窗口的无关进程纳入匹配，
    反而不如 _uwp_app_has_main_window 可靠（其内置了窗口未就绪时回退进程判定）。
    """
    if launch_target.startswith("appid:") and _uwp_app_has_main_window(
        launch_target[6:].strip(), app_name
    ):
        return True
    names = _collect_running_process_names(app_name, launch_target, memory)
    if not names:
        if launch_target.startswith("appid:"):
            return _uwp_app_has_main_window(launch_target[6:].strip(), app_name)
        return False
    proc_map = _native_process_map()
    wins = _native_visible_windows()
    if proc_map is not None and wins is not None:
        target_set = {n.lower() for n in names if n}
        win_pids = {pid for pid, _title in wins}
        for pid, stem in proc_map.items():
            if stem.lower() in target_set and pid in win_pids:
                return True
        if app_name:
            for _pid, title in wins:
                if app_name.lower() in title.lower():
                    return True
        fallback = is_app_running(app_name, launch_target, memory)
        if fallback:
            logger.info("主窗口暂未确认，但进程已运行（回退判定）: %s", app_name)
        return fallback
    name_ps = ",".join(f"'{n.replace(chr(39), chr(39)*2)}'" for n in names)
    script = f"""
$names = @({name_ps})
foreach ($n in $names) {{
  $hit = Get-Process -Name $n -ErrorAction SilentlyContinue |
    Where-Object {{ $_.MainWindowHandle -ne [IntPtr]::Zero }}
  if ($hit) {{ exit 0 }}
}}
exit 1
"""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if r.returncode == 0:
            return True
    except Exception as e:
        logger.warning("主窗口检测失败 %s: %s", app_name, e)
    # 窗口未就绪但进程已在跑（UWP 裸命令启动如 calc、或应用启动慢）：
    # 回退 is_app_running（其含窗口标题/命令行匹配）避免误报"未启动"。
    fallback = is_app_running(app_name, launch_target, memory)
    if fallback:
        logger.info("主窗口暂未确认，但进程已运行（回退判定）: %s", app_name)
    return fallback


def is_process_alive(name_or_pid: str) -> bool:
    """检查进程是否仍在运行（优先走 Win32 原生快照，<1ms）。"""
    target = (name_or_pid or "").strip()
    if not target:
        return False
    proc_map = _native_process_map()
    if proc_map is not None:
        if target.isdigit():
            pid = int(target)
            return True if pid <= 4 else (pid in proc_map)
        stem = target.replace(".exe", "", 1).strip().lower()
        return any(s.lower() == stem for s in proc_map.values())
    if target.isdigit():
        pid = int(target)
        if pid <= 4:
            return True
        ps = (
            f"if (Get-Process -Id {pid} -ErrorAction SilentlyContinue) "
            f"{{ exit 0 }} else {{ exit 1 }}"
        )
    else:
        name = target.replace(".exe", "", 1).strip().replace("'", "''")
        ps = (
            f"if (Get-Process -Name '{name}' -ErrorAction SilentlyContinue) "
            f"{{ exit 0 }} else {{ exit 1 }}"
        )
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True,
            timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return r.returncode == 0
    except Exception as e:
        logger.warning("进程存活检测失败 %s: %s", target, e)
        return False


def list_processes(limit: int = 40) -> tuple[bool, str]:
    """列出当前运行的进程（按内存占用从高到低）。用于查询当前进程、内存占用、前几名占用资源的程序。"""
    limit = max(5, min(int(limit), 100))
    script = f"""
Get-Process -ErrorAction SilentlyContinue |
  Sort-Object WorkingSet -Descending |
  Select-Object -First {limit} ProcessName, Id, WorkingSet |
  ForEach-Object {{
    $mb = [math]::Round($_.WorkingSet / 1MB, 1)
    Write-Output ("{{0}} | PID {{1}} | 内存 {{2}} MB" -f $_.ProcessName, $_.Id, $mb)
  }}
"""
    try:
        out = powershell(script, timeout=15)
        lines = [ln.strip() for ln in (out or "").splitlines() if ln.strip()]
        if not lines:
            return True, "未找到进程"
        return True, "进程列表（按内存占用）：\n" + "\n".join(lines)
    except Exception as e:
        return False, f"列出进程失败: {e}"


def resolve_process_pids(keyword: str, limit: int = 15) -> list[int]:
    """按 名称/PID/窗口标题/命令行 解析进程 PID 列表（底层 CIM + 窗口匹配）。"""
    keyword = (keyword or "").strip()
    if not keyword:
        return []
    limit = max(1, min(int(limit), 30))
    safe = keyword.replace("'", "''")
    compact = re.sub(r"[\s_\-+\.]+", "", keyword.lower())
    is_pid = keyword.isdigit()
    if is_pid:
        pid = int(keyword)
        if pid > 4 and is_process_alive(keyword):
            return [pid]
        return []

    script = f"""
$kw = '{safe}'.ToLower()
$compact = '{compact}'
$limit = {limit}
# 当前进程及全部祖先：宿主进程命令行天然包含搜索关键词，属构造性误报
$chain = New-Object System.Collections.Generic.HashSet[int]
$cur = $PID
for ($i = 0; $i -lt 24 -and $cur -gt 4; $i++) {{
  [void]$chain.Add($cur)
  $pp = Get-CimInstance Win32_Process -Filter "ProcessId=$cur" -ErrorAction SilentlyContinue
  if (-not $pp) {{ break }}
  $cur = [int]$pp.ParentProcessId
}}
$pids = New-Object System.Collections.Generic.HashSet[int]
Get-Process -ErrorAction SilentlyContinue | ForEach-Object {{
  if ($pids.Count -ge $limit) {{ return }}
  $match = ($_.ProcessName.ToLower().Contains($kw)) -or
    ($_.MainWindowTitle -and $_.MainWindowTitle.ToLower().Contains($kw)) -or
    ($compact -and $_.ProcessName.ToLower().Replace('_','').Contains($compact))
  if ($match) {{ [void]$pids.Add($_.Id) }}
}}
Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | ForEach-Object {{
  if ($pids.Count -ge $limit) {{ return }}
  # 排除自身及全部祖先：宿主进程 CommandLine 必然包含搜索关键字，
  # 否则会把宿主进程误判为匹配（曾导致 taskkill 连树误杀宿主）。
  if ($chain -contains $_.ProcessId) {{ return }}
  $match = ($_.Name.ToLower().Contains($kw)) -or
    ($_.CommandLine -and $_.CommandLine.ToLower().Contains($kw))
  if ($match) {{ [void]$pids.Add([int]$_.ProcessId) }}
}}
$pids | ForEach-Object {{ Write-Output $_ }}
"""
    out = powershell(script, timeout=8)
    result: list[int] = []
    seen: set[int] = set()
    for ln in (out or "").splitlines():
        ln = ln.strip()
        if not ln.isdigit():
            continue
        pid = int(ln)
        if pid <= 4 or pid in seen:
            continue
        seen.add(pid)
        result.append(pid)
    return result


def resolve_process_pids_multi(keywords: list[str], limit: int = 20) -> list[int]:
    """一次 PowerShell 查询多个关键词的 PID 合集，替代逐关键词串行扫描。

    语义与 resolve_process_pids 一致（进程名/窗口标题/命令行/紧凑名匹配），
    但把 N 个关键词合并到单次全进程扫描，避免重复开 PowerShell。
    """
    kws: list[str] = []
    seen_kw: set[str] = set()
    direct_pids: list[int] = []
    for kw in keywords:
        k = (kw or "").strip()
        if not k:
            continue
        if k.isdigit():
            pid = int(k)
            if pid > 4 and pid not in direct_pids:
                direct_pids.append(pid)
            continue
        low = k.lower()
        if low not in seen_kw:
            seen_kw.add(low)
            kws.append(k)

    result: list[int] = []
    seen: set[int] = set()
    for pid in direct_pids:
        if pid not in seen and is_process_alive(str(pid)):
            seen.add(pid)
            result.append(pid)

    if kws:
        limit = max(1, min(int(limit), 50))
        kw_literals = ",".join(
            "'" + k.replace("'", "''").lower() + "'" for k in kws
        )
        compacts = ",".join(
            "'" + re.sub(r"[\s_\-+\.]+", "", k.lower()) + "'" for k in kws
        )
        script = f"""
$kws = @({kw_literals})
$compacts = @({compacts})
$limit = {limit}
# 当前进程及全部祖先：宿主进程命令行天然包含搜索关键词，属构造性误报
$chain = New-Object System.Collections.Generic.HashSet[int]
$cur = $PID
for ($i = 0; $i -lt 24 -and $cur -gt 4; $i++) {{
  [void]$chain.Add($cur)
  $pp = Get-CimInstance Win32_Process -Filter "ProcessId=$cur" -ErrorAction SilentlyContinue
  if (-not $pp) {{ break }}
  $cur = [int]$pp.ParentProcessId
}}
$pids = New-Object System.Collections.Generic.HashSet[int]
function Test-Kw($name, $title, $cl) {{
  foreach ($kw in $kws) {{
    if ($kw -and ((($name) -and $name.Contains($kw)) -or (($title) -and $title.Contains($kw)) -or (($cl) -and $cl.Contains($kw)))) {{ return $true }}
  }}
  foreach ($c in $compacts) {{
    if ($c -and $name -and $name.Replace('_','').Contains($c)) {{ return $true }}
  }}
  return $false
}}
Get-Process -ErrorAction SilentlyContinue | ForEach-Object {{
  if ($pids.Count -ge $limit) {{ return }}
  $n = $_.ProcessName.ToLower()
  $t = if ($_.MainWindowTitle) {{ $_.MainWindowTitle.ToLower() }} else {{ '' }}
  if (Test-Kw $n $t '') {{ [void]$pids.Add($_.Id) }}
}}
Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | ForEach-Object {{
  if ($pids.Count -ge $limit) {{ return }}
  # 排除自身及全部祖先（宿主 CommandLine 含关键词会误判）
  if ($chain -contains $_.ProcessId) {{ return }}
  $n = $_.Name.ToLower()
  $cl = if ($_.CommandLine) {{ $_.CommandLine.ToLower() }} else {{ '' }}
  if (Test-Kw $n '' $cl) {{ [void]$pids.Add([int]$_.ProcessId) }}
}}
$pids | ForEach-Object {{ Write-Output $_ }}
"""
        out = powershell(script, timeout=8)
        for ln in (out or "").splitlines():
            ln = ln.strip()
            if not ln.isdigit():
                continue
            pid = int(ln)
            if pid <= 4 or pid in seen:
                continue
            seen.add(pid)
            result.append(pid)
    return result


def _ancestor_pids() -> list[int]:
    """当前进程的全部祖先 PID。

    宿主/沙箱进程的命令行天然包含本次调用文本（关键词），关键词扫描时
    属构造性误报；若被当作目标进程还会被 taskkill /T 连树误杀。
    用 Toolhelp32 快照沿 ParentProcessId 上溯（无第三方依赖）。
    """
    chain: list[int] = []
    try:
        import ctypes
        from ctypes import wintypes

        TH32CS_SNAPPROCESS = 0x2
        INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

        class _PE32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_wchar * 260),
            ]

        k32 = ctypes.windll.kernel32
        k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        k32.Process32FirstW.restype = wintypes.BOOL
        k32.Process32FirstW.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_PE32W),
        ]
        k32.Process32NextW.restype = wintypes.BOOL
        k32.Process32NextW.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_PE32W),
        ]

        snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if not snap or snap == INVALID_HANDLE_VALUE:
            return []
        parents: dict[int, int] = {}
        try:
            entry = _PE32W()
            entry.dwSize = ctypes.sizeof(_PE32W)
            alive = k32.Process32FirstW(snap, ctypes.byref(entry))
            while alive:
                parents[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
                alive = k32.Process32NextW(snap, ctypes.byref(entry))
        finally:
            k32.CloseHandle(snap)

        cur = os.getpid()
        for _ in range(24):
            parent = parents.get(cur)
            if not parent or parent <= 4 or parent in chain:
                break
            chain.append(parent)
            cur = parent
    except Exception as e:
        logger.debug("祖先进程链获取失败: %s", e)
    return chain


def _self_pids() -> set[int]:
    """当前进程、父进程及全部祖先（防止 taskkill 误杀宿主进程树）。"""
    out = set()
    try:
        out.add(os.getpid())
        out.add(os.getppid())
    except OSError:
        pass
    out.update(_ancestor_pids())
    return out


def shutdown_pids(pids: list[int], force: bool = True) -> tuple[bool, str]:
    """按 PID 批量结束进程树（单次 taskkill /T /F），杀后复核。"""
    self_pids = _self_pids()
    clean: list[int] = []
    for pid in pids:
        if pid <= 4 or pid in self_pids:
            continue
        if is_process_alive(str(pid)):
            clean.append(pid)
    if not clean:
        return False, "没找到可结束的进程"

    # 批量杀除：一次 taskkill 同时处理所有 PID（含子进程树）
    pid_args = " ".join(f"/PID {p}" for p in clean)
    if force:
        script = f"taskkill {pid_args} /T /F"
    else:
        # 优雅关闭：先 CloseMainWindow，等待，再强制
        pid_csv = ",".join(str(p) for p in clean)
        script = f"""
$pids = @({pid_csv})
$grace = 0
foreach ($id in $pids) {{
  $p = Get-Process -Id $id -EA SilentlyContinue
  if ($p -and $p.MainWindowHandle -ne [IntPtr]::Zero) {{
    if ($p.CloseMainWindow()) {{ $grace++ }}
  }}
}}
Start-Sleep -Milliseconds 800
$remain = @()
foreach ($id in $pids) {{
  if (Get-Process -Id $id -EA SilentlyContinue) {{ $remain += $id }}
}}
if ($remain) {{
  $pidArgs = ($remain | ForEach-Object {{ "/PID $_" }}) -join ' '
  Start-Process -FilePath taskkill.exe -ArgumentList @($pidArgs, '/T', '/F') -Wait -WindowStyle Hidden
}}
"""
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            timeout=6,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as e:
        return False, f"批量结束进程失败: {e}"

    remain = [p for p in clean if is_process_alive(str(p))]
    if remain:
        # 批量重试一次：一次性 taskkill 所有残留 PID，避免逐个串行调用
        retry_args = " ".join(f"/PID {p}" for p in remain)
        try:
            subprocess.run(
                ["taskkill"] + [arg for arg in retry_args.split() if arg] + ["/T", "/F"],
                capture_output=True,
                text=True,
                timeout=4,
                encoding="utf-8",
                errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            pass
        remain = [p for p in clean if is_process_alive(str(p))]
    if remain:
        return False, f"仍有进程存活: {', '.join(str(p) for p in remain)}"
    return True, f"已结束 {len(clean)} 个进程（PID {', '.join(str(p) for p in clean)}）"


def find_process(keyword: str, limit: int = 20) -> tuple[bool, str]:
    """按 名称/PID/窗口标题/命令行 精准查找进程，返回 PID 等详情。"""
    keyword = (keyword or "").strip()
    if not keyword:
        return False, "请提供进程名、PID 或关键词"
    limit = max(1, min(int(limit), 50))
    pids = resolve_process_pids(keyword, limit=limit)
    if not pids:
        return True, f"没找到与「{keyword}」匹配的进程"
    pid_csv = ",".join(str(p) for p in pids[:limit])
    script = f"""
$ids = @({pid_csv})
foreach ($id in $ids) {{
  $p = Get-Process -Id $id -ErrorAction SilentlyContinue
  $cim = Get-CimInstance Win32_Process -Filter \"ProcessId=$id\" -EA SilentlyContinue
  $title = if ($p) {{ $p.MainWindowTitle }} else {{ '' }}
  $name = if ($p) {{ $p.ProcessName }} else {{ $cim.Name }}
  $cl = if ($cim -and $cim.CommandLine) {{ $cim.CommandLine }} else {{ '' }}
  if ($cl.Length -gt 120) {{ $cl = $cl.Substring(0,120) + '…' }}
  Write-Output (\"PID $id | $name | 窗口:$title | $cl\")
}}
"""
    out = powershell(script, timeout=8)
    lines = [ln.strip() for ln in (out or "").splitlines() if ln.strip()]
    if not lines:
        return True, f"匹配「{keyword}」的 PID：{pid_csv}（详情暂不可用）"
    return True, f"匹配「{keyword}」的进程（共 {len(lines)} 个）：\n" + "\n".join(lines)


def discover_process_names(keyword: str) -> list[str]:
    """从正在运行的进程/窗口标题/命令行反查进程名。"""
    return discover_process_names_multi([keyword])


def discover_process_names_multi(keywords: list[str]) -> list[str]:
    """一次性多关键词反查进程名，避免串行多次 PowerShell 调用。"""
    pids, names = discover_processes_multi(keywords)
    return names


def discover_processes_multi(keywords: list[str]) -> tuple[list[int], list[str]]:
    """一次性多关键词扫描，同时返回 (PIDs, 进程名)。

    合并 resolve_process_pids_multi + discover_process_names_multi 的扫描逻辑，
    一次 PowerShell 调用同时输出 PID 和进程名，避免 close_app 链路中重复扫两次。
    """
    kws: list[str] = []
    seen_kw: set[str] = set()
    direct_pids: list[int] = []
    for kw in keywords:
        k = (kw or "").strip()
        if not k:
            continue
        if k.isdigit():
            pid = int(k)
            if pid > 4 and pid not in direct_pids:
                direct_pids.append(pid)
            continue
        low = k.lower()
        if low not in seen_kw:
            seen_kw.add(low)
            kws.append(k)

    pids: list[int] = []
    names: list[str] = []
    seen_pids: set[int] = set()
    seen_names: set[str] = set()

    for pid in direct_pids:
        if pid not in seen_pids and is_process_alive(str(pid)):
            seen_pids.add(pid)
            pids.append(pid)

    if not kws:
        return pids, names

    kw_literals = ",".join(
        "'" + k.replace("'", "''").lower() + "'" for k in kws
    )
    compacts = ",".join(
        "'" + re.sub(r"[\s_\-+\.]+", "", k.lower()) + "'" for k in kws
    )
    # 排除自身及全部祖先，防止命令行含关键词时误杀（宿主进程 CommandLine
    # 天然包含本次调用文本，属构造性误报；仅排父进程不够）
    self_pids = _self_pids()
    exclude_pids = ",".join(str(p) for p in self_pids) or "0"
    script = f"""
$kws = @({kw_literals})
$compacts = @({compacts})
$selfPid = $PID
$exclude = @({exclude_pids})
# 当前进程及全部祖先（selfPid 上溯，与 python 侧 $exclude 互为补充）
$chain = New-Object System.Collections.Generic.HashSet[int]
$cur = $PID
for ($i = 0; $i -lt 24 -and $cur -gt 4; $i++) {{
  [void]$chain.Add($cur)
  $pp = Get-CimInstance Win32_Process -Filter "ProcessId=$cur" -ErrorAction SilentlyContinue
  if (-not $pp) {{ break }}
  $cur = [int]$pp.ParentProcessId
}}
function Test-Kw($name, $title, $cl) {{
  foreach ($kw in $kws) {{
    if ($kw -and ((($name) -and $name.Contains($kw)) -or (($title) -and $title.Contains($kw)) -or (($cl) -and $cl.Contains($kw)))) {{ return $true }}
  }}
  foreach ($c in $compacts) {{
    if ($c -and $name -and $name.Replace('_','').Contains($c)) {{ return $true }}
  }}
  return $false
}}
Get-Process -ErrorAction SilentlyContinue | ForEach-Object {{
  if ($exclude -contains $_.Id) {{ return }}
  if ($chain -contains $_.Id) {{ return }}
  $n = $_.ProcessName.ToLower()
  $t = if ($_.MainWindowTitle) {{ $_.MainWindowTitle.ToLower() }} else {{ '' }}
  if (Test-Kw $n $t '') {{ Write-Output ($_.Id.ToString() + '|' + $_.ProcessName) }}
}}
Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | ForEach-Object {{
  if ($_.ProcessId -eq $selfPid) {{ return }}
  if ($exclude -contains $_.ProcessId) {{ return }}
  if ($chain -contains $_.ProcessId) {{ return }}
  $n = if ($_.Name) {{ $_.Name.ToLower() }} else {{ '' }}
  # 仅匹配进程自身 ExecutablePath（如 WindowsApps 包目录），不匹配任意 CommandLine 参数，
  # 防止浏览器 URL、命令行脚本参数含关键词时将 msedge/powershell 等无关进程误判为目标应用
  $ep = if ($_.ExecutablePath) {{ $_.ExecutablePath.ToLower() }} else {{ '' }}
  if (Test-Kw $n '' $ep) {{
    $stem = $_.Name -replace '\\.exe$',''
    Write-Output ($_.ProcessId.ToString() + '|' + $stem)
  }}
}}
"""
    try:
        out = powershell(script, timeout=8)
        for ln in (out or "").splitlines():
            ln = ln.strip()
            if not ln or "|" not in ln:
                continue
            pid_str, _, name = ln.partition("|")
            name = name.strip()
            k = name.lower().replace(".exe", "")
            if k in _PROTECTED or k in _HELPER_PROCESSES:
                continue
            if pid_str.isdigit():
                pid = int(pid_str)
                if pid > 4 and pid not in seen_pids:
                    seen_pids.add(pid)
                    pids.append(pid)
            if name and k not in seen_names:
                seen_names.add(k)
                names.append(name)
    except Exception as e:
        logger.warning("反查进程失败 %s: %s", kws, e)

    return pids, names


def shutdown_process_names(
    names: list[str],
    wait_ms: int = 1500,
    force: bool = True,
) -> tuple[bool, str]:
    """优雅关闭 → 等待 → 强杀进程树（taskkill /T /F）。"""
    clean = []
    seen: set[str] = set()
    for n in names:
        stem = (n or "").replace(".exe", "", 1).strip()
        if not stem:
            continue
        k = stem.lower()
        if k in seen or k in _PROTECTED:
            continue
        if k in ("python", "pythonw", "node", "pwsh", "powershell"):
            continue
        seen.add(k)
        clean.append(stem)
    if not clean:
        return False, "无法识别进程名"

    name_ps = ",".join(f"'{n}'" for n in clean)
    force_ps = "$true" if force else "$false"
    script = f"""
$names = @({name_ps})
$waitMs = {max(wait_ms, 500)}
$doForce = {force_ps}
$grace = 0
foreach ($n in $names) {{
  Get-Process -Name $n -ErrorAction SilentlyContinue | ForEach-Object {{
    try {{
      if ($_.MainWindowHandle -ne [IntPtr]::Zero) {{
        if ($_.CloseMainWindow()) {{ $grace++ }}
      }}
    }} catch {{}}
  }}
}}
Start-Sleep -Milliseconds ([Math]::Min($waitMs, 800))
$killed = 0
foreach ($n in $names) {{
  $left = @(Get-Process -Name $n -ErrorAction SilentlyContinue)
  if (-not $left) {{ continue }}
  if ($doForce) {{
    $exe = if ($n -match '\\.exe$') {{ $n }} else {{ ($n + '.exe') }}
    # 先用 taskkill /T /F 杀进程树（含子进程）
    $r = Start-Process -FilePath taskkill.exe -ArgumentList @('/IM', $exe, '/T', '/F') -Wait -PassThru -WindowStyle Hidden
    if ($r.ExitCode -eq 0) {{ $killed += $left.Count; continue }}
    # taskkill 失败（权限/保护），尝试 Stop-Process -Force
    Start-Sleep -Milliseconds 200
    $stillLeft = @(Get-Process -Name $n -ErrorAction SilentlyContinue)
    if ($stillLeft) {{
      Stop-Process -Name $n -Force -ErrorAction SilentlyContinue
      $killed += $stillLeft.Count
    }} else {{
      $killed += $left.Count
    }}
  }} else {{
    Stop-Process -Name $n -ErrorAction SilentlyContinue
    $killed += $left.Count
  }}
}}
Start-Sleep -Milliseconds 300
$remain = @()
foreach ($n in $names) {{
  $remain += @(Get-Process -Name $n -ErrorAction SilentlyContinue)
}}
if ($remain.Count -gt 0) {{ Write-Error ('REMAIN:' + $remain.Count); exit 1 }}
if ($grace -eq 0 -and $killed -eq 0) {{ Write-Error 'NOT_FOUND'; exit 1 }}
Write-Output (\"已关闭 \" + $names.Count + \" 类进程\")
"""
    try:
        out = powershell(script, timeout=8)
        if "NOT_FOUND" in (out or "") or "REMAIN:" in (out or ""):
            return False, f"未找到或未能结束进程: {', '.join(clean)}"
        return True, (out or f"已关闭 {', '.join(clean)}").splitlines()[-1].strip()
    except Exception as e:
        return False, f"关闭进程失败: {e}"


def kill_process(name_or_pid: str, force: bool = False) -> tuple[bool, str]:
    target = (name_or_pid or "").strip()
    if not target:
        return False, "请提供进程名或 PID"

    if target.isdigit():
        pid = int(target)
        if pid <= 4:
            return False, "不能结束系统关键进程"
        if pid in _self_pids():
            return False, f"PID {pid} 是助手自身或其父进程，已拒绝结束"
        if not is_process_alive(str(pid)):
            return False, f"没找到 PID {pid}，进程可能已退出或 PID 不存在"
        script = f"taskkill /PID {pid} /T /F"
        if not force:
            script = (
                f"$p = Get-Process -Id {pid} -EA SilentlyContinue; "
                f"if (-not $p) {{ exit 1 }}; "
                f"if ($p.MainWindowHandle -ne 0) {{ [void]$p.CloseMainWindow(); Start-Sleep -m 800 }}; "
                f"if (Get-Process -Id {pid} -EA SilentlyContinue) {{ taskkill /PID {pid} /T /F }}"
            )
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-Command", script],
                capture_output=True,
                text=True,
                timeout=15,
                encoding="utf-8",
                errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as e:
            return False, f"结束进程失败: {e}"
        # 杀后复核：以"是否仍存活"为准，而非 taskkill 退出码
        if is_process_alive(str(pid)):
            return False, f"PID {pid} 仍在运行，可能需要管理员权限或进程受保护"
        return True, f"已结束 PID {pid}"

    name = target.replace(".exe", "", 1).strip()
    if name.lower() in _PROTECTED:
        return False, f"不能结束系统进程 {name}"
    if name.lower() in ("python", "pythonw", "node", "pwsh", "powershell"):
        return False, f"不能批量结束 {name}，请用 find_process 定位 PID 后再 kill_process"

    # 底层精准：先按关键词解析 PID（窗口标题/命令行），再按 PID 杀
    pids = resolve_process_pids(target, limit=10)
    if pids:
        return shutdown_pids(pids, force=force)

    return shutdown_process_names([name], force=force)


def focus_app(app_name: str, launch_target: str = "") -> tuple[bool, str]:
    names = infer_process_names(app_name, launch_target)
    if not names:
        return False, f"无法识别「{app_name}」的进程名"
    ok, detail = focus_window_by_process_names(names, timeout=10)
    if ok:
        return True, f"已切换到 {app_name}"
    return False, f"{app_name} 在运行但无法聚焦窗口"


def collect_process_names(
    app_name: str,
    launch_target: str = "",
    memory=None,
) -> tuple[str, list[str]]:
    """汇总进程名：数据库已学会 > 映射/lnk > 运行中反查。"""
    raw = (app_name or "").strip()
    display = raw
    context_apps = memory.recent_app_aliases() if memory else None
    hit = find_desktop_app(raw, context_apps=context_apps)
    if hit:
        display = hit[0]

    names: list[str] = []
    seen: set[str] = set()

    def _add(items: list[str]):
        for n in items:
            stem = (n or "").replace(".exe", "", 1).strip()
            if not stem:
                continue
            k = stem.lower()
            if k in _PROTECTED:
                continue
            if k not in seen:
                seen.add(k)
                names.append(stem)

    if memory:
        _add(memory.get_app_process_names(display))
        if raw != display:
            _add(memory.get_app_process_names(raw))
    _add(infer_process_names(display, launch_target))
    # 合并多关键词一次扫全进程，避免串行两次 PowerShell 调用
    discover_kws = [k for k in (display, raw) if k]
    if discover_kws:
        _add(discover_process_names_multi(discover_kws))
    return display, names


def _close_search_keywords(
    raw: str,
    display: str,
    names: list[str],
    launch_target: str,
) -> list[str]:
    """关闭时用于 PID/命令行反查的关键词（去重保序）。"""
    keywords: list[str] = []
    seen: set[str] = set()

    def add(k: str) -> None:
        k = (k or "").strip()
        if not k:
            return
        low = k.lower()
        if low in seen:
            return
        seen.add(low)
        keywords.append(k)

    add(raw)
    if display != raw:
        add(display)
    for n in names:
        add(n)
    lt = (launch_target or "").strip()
    if lt:
        add(lt)
        stem = _process_name_from_path(lt)
        if stem:
            add(stem)
        if os.path.isfile(lt):
            add(os.path.basename(lt))
    return keywords


def close_app(
    app_name: str,
    launch_target: str = "",
    force: bool = False,
    memory=None,
) -> tuple[bool, str]:
    """关闭应用：UWP 专用路径 / PID 反查优先 / 进程名列表兜底。"""
    raw = (app_name or "").strip()
    if not raw:
        return False, "请提供要关闭的应用名称"

    # 过宽名称会误杀体内 WebView / 多款音乐客户端
    if re.fullmatch(r"音乐|播放器|歌", raw):
        return False, (
            "「音乐」太宽泛。请说要关「QQ音乐」「网易云」或「酷狗」客户端；"
            "若只想停播，请说「暂停音乐」。"
        )
    if re.fullmatch(r"视频|剧|电影", raw):
        return False, (
            "「视频」太宽泛。若只想关掉体内看剧窗口，请说「关闭视频」。"
        )

    # launch_target 为空时，自动从桌面索引查找（用户直接说"关闭记事本"的场景）
    if not launch_target:
        try:
            from kekesi.tools.desktop_apps import find_desktop_app
            hit = find_desktop_app(raw)
            if hit:
                launch_target = hit[1]
        except Exception:
            pass

    # UWP/Store 应用专用关闭路径
    if launch_target.startswith("appid:"):
        aumid = launch_target[6:].strip()
        ok, msg = _close_uwp_app(aumid, raw)
        if ok:
            return True, msg or f"已关闭 {raw}"
        # UWP 关闭失败，继续尝试常规路径

    display, names = collect_process_names(raw, launch_target, memory=memory)

    # 快速路径：有可见窗口时先优雅关窗（记事本等 GUI 应用更快、更稳）
    try:
        from .window_ops import close_window, window_title_visible

        for title in (display, raw):
            t = (title or "").strip()
            if not t or not window_title_visible(t):
                continue
            ok_win, msg_win = close_window(t, force=bool(force))
            if ok_win and not window_title_visible(t):
                return True, msg_win or f"已关闭 {display or raw}"
    except Exception as e:
        logger.debug("close_window 快速路径跳过: %s", e)

    keywords = _close_search_keywords(raw, display, names, launch_target)
    # 一次扫描同时拿 PID + 进程名（合并原 resolve_process_pids_multi + discover 的两次扫描）
    pids: list[int] = []
    if keywords:
        scan_pids, scan_names = discover_processes_multi(keywords)
        pids = scan_pids
        # 合并扫描发现的进程名（兜底用）
        for n in scan_names:
            stem = n.replace(".exe", "", 1).strip()
            if stem and stem.lower() not in {x.lower() for x in names}:
                names.append(stem)

    ok = False
    msg = ""

    if pids:
        ok, msg = shutdown_pids(pids, force=force)
        logger.info("close_app [%s] PID 路径 pids=%s ok=%s", raw, pids, ok)

    if not ok and names:
        ok, msg = shutdown_process_names(names, force=force)
        logger.info("close_app [%s] 进程名路径 names=%s ok=%s", raw, names, ok)

    if not ok:
        if not pids and not names:
            return False, (
                f"无法识别「{raw}」对应的进程（未找到进程名或 PID）。"
                f"请先通过助手成功打开一次，或确认应用正在运行。"
            )
        return False, msg or f"没能关闭「{raw}」"

    if memory and names:
        memory.learn_app_process_names(display, names, command=launch_target)

    return True, msg or f"已关闭 {display}"


def _close_uwp_app(aumid: str, display_name: str) -> tuple[bool, str]:
    """关闭 UWP/Store 应用：通过窗口标题定位进程并关闭。"""
    display_safe = display_name.replace("'", "''")
    pkg_family = aumid.split("!")[0] if "!" in aumid else aumid
    pkg_short = pkg_family.split("_")[0] if "_" in pkg_family else pkg_family
    pkg_short_safe = pkg_short.replace("'", "''")
    # UWP 应用进程名通常包含包名简写，或窗口标题包含显示名
    script = f"""
$display = '{display_safe}'
$pkgShort = '{pkg_short_safe}'
# 当前进程及全部祖先：宿主进程命令行天然包含本次脚本文本（包名），属构造性误报，
# 且祖先被 taskkill /T 会连树误杀自身
$chain = New-Object System.Collections.Generic.HashSet[int]
$cur = $PID
for ($i = 0; $i -lt 24 -and $cur -gt 4; $i++) {{
  [void]$chain.Add($cur)
  $pp = Get-CimInstance Win32_Process -Filter "ProcessId=$cur" -ErrorAction SilentlyContinue
  if (-not $pp) {{ break }}
  $cur = [int]$pp.ParentProcessId
}}
$pids = New-Object System.Collections.Generic.HashSet[int]
# 1) 按窗口标题匹配（最可靠）
Get-Process -ErrorAction SilentlyContinue | ForEach-Object {{
  $title = $_.MainWindowTitle
  $pn = $_.ProcessName
  if (($title -and $display -and $title.Contains($display)) -or
      ($pkgShort -and $pn.ToLower().Contains($pkgShort.ToLower()))) {{
    [void]$pids.Add([int]$_.Id)
  }}
}}
# 2) 按命令行匹配（UWP 进程 CL 是 exe 路径，含包族名段如 Microsoft.WindowsNotepad_）
#    用包族名 pkgShort 匹配比完整 AUMID 更可靠（UWP 进程 CL 不含 !App 后缀）
Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | ForEach-Object {{
  if ($chain -contains $_.ProcessId) {{ return }}
  $cl = $_.CommandLine
  if ($cl -and $pkgShort -and $cl.Contains($pkgShort)) {{ [void]$pids.Add([int]$_.ProcessId) }}
}}
if ($pids.Count -eq 0) {{
  Write-Output 'NOT_FOUND'
  exit 1
}}
# 过滤掉系统进程与自身祖先
$killPids = $pids | Where-Object {{ $_ -gt 4 -and -not ($chain -contains $_) }}
$pidArgs = ($killPids | ForEach-Object {{ "/PID $_" }}) -join ' '
$r = Start-Process -FilePath taskkill.exe -ArgumentList @($pidArgs, '/T', '/F') -Wait -PassThru -WindowStyle Hidden
if ($r.ExitCode -eq 0) {{
  Write-Output ("已关闭 " + $killPids.Count + " 个进程")
}} else {{
  Write-Output ("KILL_FAILED:" + $r.ExitCode)
}}
"""
    try:
        out = powershell(script, timeout=10)
        if "NOT_FOUND" in (out or ""):
            return False, f"未找到运行中的 UWP 应用「{display_name}」"
        if "KILL_FAILED" in (out or ""):
            return False, f"关闭 UWP 应用「{display_name}」失败，可能需要管理员权限"
        return True, (out or f"已关闭 {display_name}").splitlines()[-1].strip()
    except Exception as e:
        logger.warning("关闭 UWP 应用失败 %s: %s", aumid, e)
        return False, f"关闭 UWP 应用失败: {e}"
