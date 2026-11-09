"""扫描本机已安装应用（桌面 / 开始菜单 / 注册表 / 系统应用列表）"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_LAUNCH_SUFFIXES = (".lnk", ".exe", ".bat", ".url")
_NAME_TRIM_RE = re.compile(r"\s*[-–—]\s*(快捷方式|Shortcut)\s*$", re.I)
_CACHE: dict[str, dict] | None = None
# 索引缓存时间戳与刷新控制：过期后台刷新，旧值先用，避免阻塞对话
_CACHE_TS: float = 0.0
_CACHE_TTL = 600.0  # 索引有效期（秒），过期触发后台刷新
_refresh_lock = threading.Lock()
_refreshing = False

_SOURCE_PRIORITY = {
    "desktop": 100,
    "startmenu": 80,
    "registry": 60,
    "startapps": 40,
}


def _desktop_dirs() -> list[Path]:
    dirs: list[Path] = []
    home = Path.home()
    dirs.extend([home / "Desktop", home / "桌面"])

    public = os.environ.get("PUBLIC", r"C:\Users\Public")
    dirs.extend([Path(public) / "Desktop", Path(public) / "桌面"])

    seen: set[str] = set()
    result: list[Path] = []
    for d in dirs:
        key = str(d).lower()
        if key in seen:
            continue
        seen.add(key)
        if d.is_dir():
            result.append(d)
    return result


def _start_menu_dirs() -> list[Path]:
    dirs: list[Path] = []
    appdata = os.environ.get("APPDATA", "")
    programdata = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
    if appdata:
        dirs.append(Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
    if programdata:
        dirs.append(
            Path(programdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
        )
    return [d for d in dirs if d.is_dir()]


def _clean_display_name(stem: str) -> str:
    name = _NAME_TRIM_RE.sub("", stem).strip()
    return name or stem


def _name_keys(name: str) -> set[str]:
    raw = name.strip().lower()
    keys = {raw}
    compact = re.sub(r"[\s_\-+\.]+", "", raw)
    if compact:
        keys.add(compact)
    # 去掉常见后缀词
    for suffix in ("launcher", "setup", "卸载", "uninstall"):
        if raw.endswith(suffix):
            trimmed = raw[: -len(suffix)].strip(" -_")
            if trimmed:
                keys.add(trimmed)
                keys.add(re.sub(r"[\s_\-+\.]+", "", trimmed))
    return keys


def _put_entry(index: dict[str, dict], entry: dict):
    priority = _SOURCE_PRIORITY.get(entry.get("source", ""), 0)
    entry["priority"] = priority
    for key in _name_keys(entry["name"]):
        old = index.get(key)
        if old is None or priority >= old.get("priority", 0):
            index[key] = entry


def _scan_files_in_dir(root: Path, source: str, index: dict[str, dict], recursive: bool):
    iterator = root.rglob("*") if recursive else root.iterdir()
    try:
        for item in iterator:
            if not item.is_file():
                continue
            if item.suffix.lower() not in _LAUNCH_SUFFIXES:
                continue
            display = _clean_display_name(item.stem)
            if not display or display.lower() in ("desktop", "桌面"):
                continue
            _put_entry(
                index,
                {
                    "name": display,
                    "target": str(item.resolve()),
                    "launch": "file",
                    "source": source,
                },
            )
    except OSError as e:
        logger.warning("扫描目录失败 %s: %s", root, e)


def _scan_registry_app_paths(index: dict[str, dict]):
    if os.name != "nt":
        return
    try:
        import winreg
    except ImportError:
        return

    reg_path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(hive, reg_path) as apps_key:
                i = 0
                while True:
                    try:
                        subname = winreg.EnumKey(apps_key, i)
                    except OSError:
                        break
                    i += 1
                    try:
                        with winreg.OpenKey(apps_key, subname) as subkey:
                            exe_path, _ = winreg.QueryValueEx(subkey, "")
                        if not exe_path or not os.path.isfile(exe_path):
                            continue
                        display = _clean_display_name(Path(subname).stem)
                        _put_entry(
                            index,
                            {
                                "name": display,
                                "target": exe_path,
                                "launch": "file",
                                "source": "registry",
                            },
                        )
                    except OSError:
                        continue
        except OSError:
            continue


def _scan_start_apps(index: dict[str, dict]):
    if os.name != "nt":
        return
    # 必须设置 Console.OutputEncoding=UTF8，否则中文 Windows 下 PowerShell 默认 GBK 输出，
    # 导致 UWP 应用中文名（记事本/画图/计算器等）变成乱码，find_desktop_app 找不到。
    ps = (
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
        "Get-StartApps | Select-Object Name, AppID | "
        "ConvertTo-Json -Compress -Depth 3"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
            capture_output=True,
            text=True,
            timeout=15,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode != 0 or not result.stdout.strip():
            return
        data = json.loads(result.stdout.strip())
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            return
        for item in data:
            name = (item.get("Name") or "").strip()
            app_id = (item.get("AppID") or "").strip()
            if not name or not app_id:
                continue
            _put_entry(
                index,
                {
                    "name": name,
                    "target": app_id,
                    "launch": "appid",
                    "source": "startapps",
                },
            )
    except Exception as e:
        logger.warning("Get-StartApps 扫描失败: %s", e)


def _build_index() -> dict[str, dict]:
    index: dict[str, dict] = {}

    for desktop in _desktop_dirs():
        _scan_files_in_dir(desktop, "desktop", index, recursive=False)

    for menu_dir in _start_menu_dirs():
        _scan_files_in_dir(menu_dir, "startmenu", index, recursive=True)

    _scan_registry_app_paths(index)
    _scan_start_apps(index)

    return index


def refresh_desktop_index() -> dict[str, dict]:
    """同步刷新索引（阻塞）。供「找不到应用」时强制重扫等场景使用。"""
    global _CACHE, _CACHE_TS
    index = _build_index()
    _CACHE = index
    _CACHE_TS = time.time()
    logger.info("应用索引已刷新，共 %d 条", len(_CACHE))
    return _CACHE


def refresh_desktop_index_async() -> None:
    """后台异步刷新索引，不阻塞调用方；并发刷新自动去重。"""
    global _refreshing
    with _refresh_lock:
        if _refreshing:
            return
        _refreshing = True

    def _work():
        global _refreshing
        try:
            refresh_desktop_index()
        except Exception as e:  # noqa: BLE001
            logger.warning("后台刷新应用索引失败: %s", e)
        finally:
            _refreshing = False

    threading.Thread(target=_work, name="app-index-refresh", daemon=True).start()


def get_desktop_index() -> dict[str, dict]:
    """返回应用索引。首次同步构建；过期则返回旧值并触发后台刷新。"""
    global _CACHE, _CACHE_TS
    if _CACHE is None:
        _CACHE = _build_index()
        _CACHE_TS = time.time()
    elif time.time() - _CACHE_TS > _CACHE_TTL:
        refresh_desktop_index_async()
    return _CACHE


def list_desktop_apps(limit: int = 120) -> list[str]:
    index = get_desktop_index()
    names = sorted({v["name"] for v in index.values()})
    return names[:limit]


def _match_score(query: str, name: str) -> int:
    q = query.strip().lower()
    n = name.strip().lower()
    q_compact = re.sub(r"[\s_\-+\.]+", "", q)
    n_compact = re.sub(r"[\s_\-+\.]+", "", n)

    if q == n or q_compact == n_compact:
        return 100
    if q in n or q_compact in n_compact:
        return 80 - min(abs(len(n) - len(q)), 30)
    if n in q or n_compact in q_compact:
        return 70 - min(abs(len(n) - len(q)), 30)
    return 0


def _resolve_launch_target(entry: dict) -> str:
    launch = entry.get("launch", "file")
    target = entry.get("target", "")
    if launch == "appid":
        return f"appid:{target}"
    return target


def find_desktop_app(
    name: str,
    *,
    context_apps: list[str] | None = None,
) -> tuple[str, str] | None:
    """按名称查找本机应用，返回 (显示名, 启动目标)。支持模糊/同音 + 上下文加权。"""
    if not isinstance(name, str):
        return None
    query = name.strip()
    if not query:
        return None

    index = get_desktop_index()
    if not index:
        refresh_desktop_index()
        index = get_desktop_index()
    if not index:
        return None

    query_keys = _name_keys(query)
    best_direct: dict | None = None
    for key in query_keys:
        hit = index.get(key)
        if hit and (
            best_direct is None
            or hit.get("priority", 0) > best_direct.get("priority", 0)
        ):
            best_direct = hit
    if best_direct:
        return best_direct["name"], _resolve_launch_target(best_direct)

    best: tuple[int, dict] | None = None
    seen_names: set[str] = set()
    for entry in index.values():
        display = entry["name"]
        if display in seen_names:
            continue
        seen_names.add(display)
        score = _match_score(query, display)
        if score <= 0:
            continue
        combined = score + entry.get("priority", 0) // 10
        if best is None or combined > best[0]:
            best = (combined, entry)

    if best and best[0] >= 70:
        entry = best[1]
        return entry["name"], _resolve_launch_target(entry)

    # 模糊 / 同音：扫注册表+桌面索引中的全部显示名，结合上下文
    from ..app_match import best_app_match, query_variants

    unique_names = sorted({v["name"] for v in index.values()})
    for variant in query_variants(query):
        hit = best_app_match(
            variant,
            unique_names,
            context=context_apps,
        )
        if not hit:
            continue
        matched_name, score = hit
        for entry in index.values():
            if entry["name"] == matched_name:
                logger.info(
                    "模糊匹配应用 %r → %r score=%.2f ctx=%s",
                    query,
                    matched_name,
                    score,
                    bool(context_apps),
                )
                return matched_name, _resolve_launch_target(entry)

    if best:
        entry = best[1]
        return entry["name"], _resolve_launch_target(entry)
    return None


# 别名
find_installed_app = find_desktop_app
refresh_app_index = refresh_desktop_index
list_installed_apps = list_desktop_apps
