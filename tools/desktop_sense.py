# -*- coding: utf-8 -*-
"""桌面感知层 — 先看清环境再执行。

- list_desktop_files：桌面文件名/路径（并尽量附上图标屏幕坐标）
- list_open_windows：窗口标题/句柄/激活态/位置
- list_desktop_icons：桌面图标名 + 屏幕坐标（供 SendInput 点击）
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from .winops import desktop_dir, powershell

logger = logging.getLogger(__name__)

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# ListView
_LVM_FIRST = 0x1000
_LVM_GETITEMCOUNT = _LVM_FIRST + 4
_LVM_GETITEMRECT = _LVM_FIRST + 14
_LVM_GETITEMTEXTW = _LVM_FIRST + 115
_LVIR_BOUNDS = 0
_LVIF_TEXT = 0x0001
_PROCESS_VM_OPERATION = 0x0008
_PROCESS_VM_READ = 0x0010
_PROCESS_VM_WRITE = 0x0020
_PROCESS_QUERY_INFORMATION = 0x0400
_MEM_COMMIT = 0x1000
_MEM_RELEASE = 0x8000
_PAGE_READWRITE = 0x04
_MAX_PATH = 260

_WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _LVITEMW(ctypes.Structure):
    _fields_ = [
        ("mask", ctypes.c_uint),
        ("iItem", ctypes.c_int),
        ("iSubItem", ctypes.c_int),
        ("state", ctypes.c_uint),
        ("stateMask", ctypes.c_uint),
        ("pszText", ctypes.c_void_p),
        ("cchTextMax", ctypes.c_int),
        ("iImage", ctypes.c_int),
        ("lParam", ctypes.c_longlong),
        ("iIndent", ctypes.c_int),
        ("iGroupId", ctypes.c_int),
        ("cColumns", ctypes.c_uint),
        ("puColumns", ctypes.c_void_p),
        ("piColFmt", ctypes.c_void_p),
        ("iGroup", ctypes.c_int),
    ]


@dataclass
class DesktopIcon:
    name: str
    x: int
    y: int
    left: int
    top: int
    right: int
    bottom: int
    path: str = ""


@dataclass
class OpenWindow:
    title: str
    hwnd: int
    pid: int
    process: str
    active: bool
    left: int
    top: int
    right: int
    bottom: int
    visible: bool


def _process_name(pid: int) -> str:
    if pid <= 0:
        return ""
    try:
        import psutil  # type: ignore

        return psutil.Process(pid).name() or str(pid)
    except Exception:
        pass
    # 无 psutil：QueryFullProcessImageNameW
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return str(pid)
    try:
        buf = ctypes.create_unicode_buffer(512)
        size = wintypes.DWORD(512)
        QueryFullProcessImageNameW = kernel32.QueryFullProcessImageNameW
        QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        QueryFullProcessImageNameW.restype = wintypes.BOOL
        if QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return Path(buf.value or "").name or str(pid)
    except Exception:
        pass
    finally:
        kernel32.CloseHandle(h)
    return str(pid)


def _fmt_rows(headers: list[str], rows: list[list[str]], *, max_rows: int = 80) -> str:
    if not rows:
        return "（空）"
    lines = [" | ".join(headers)]
    for row in rows[:max_rows]:
        lines.append(" | ".join(str(c) for c in row))
    if len(rows) > max_rows:
        lines.append(f"…共 {len(rows)} 条，已截断")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 桌面文件
# ---------------------------------------------------------------------------


def _iter_desktop_paths() -> list[Path]:
    roots: list[Path] = []
    user = Path(desktop_dir())
    if user.is_dir():
        roots.append(user)
    # 公共桌面
    public = os.environ.get("PUBLIC", r"C:\Users\Public")
    pub_desktop = Path(public) / "Desktop"
    if pub_desktop.is_dir() and pub_desktop.resolve() != user.resolve():
        roots.append(pub_desktop)
    files: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        try:
            for p in sorted(root.iterdir(), key=lambda x: x.name.lower()):
                key = str(p.resolve()).lower()
                if key in seen:
                    continue
                seen.add(key)
                files.append(p)
        except OSError as e:
            logger.debug("list desktop %s failed: %s", root, e)
    return files


def _parse_ext_filter(extensions: str | list[str] | None) -> set[str] | None:
    if not extensions:
        return None
    if isinstance(extensions, str):
        parts = re.split(r"[,，\s]+", extensions.strip())
    else:
        parts = [str(x) for x in extensions]
    out = {("." + p.lower().lstrip(".")) for p in parts if p.strip()}
    # 别名
    if ".word" in out or ".docx" in out or ".doc" in out:
        out |= {".docx", ".doc"}
        out.discard(".word")
    return out or None


def _desktop_file_priority(p: Path) -> tuple[int, str]:
    """文档类靠前，快捷方式靠后 —— 避免工具结果截断后模型只看到 .lnk。"""
    suf = p.suffix.lower()
    if p.is_dir():
        return (2, p.name.lower())
    if suf in (".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt", ".pdf", ".txt", ".md"):
        return (0, p.name.lower())
    if suf in (".lnk", ".url"):
        return (3, p.name.lower())
    return (1, p.name.lower())


def list_desktop_files(
    *,
    limit: int = 80,
    extensions: str | list[str] | None = None,
    name_contains: str | None = None,
) -> tuple[bool, str]:
    """返回桌面文件；文档类优先；可按后缀/关键字过滤。"""
    limit = max(5, min(int(limit), 200))
    files = _iter_desktop_paths()
    ext_set = _parse_ext_filter(extensions)
    needle = (name_contains or "").strip().lower()
    if ext_set or needle:
        filtered: list[Path] = []
        for p in files:
            if p.is_dir():
                continue
            if ext_set and p.suffix.lower() not in ext_set:
                continue
            if needle and needle not in p.name.lower():
                continue
            filtered.append(p)
        files = filtered

    # 统计（在排序/截断前，给模型一眼看见 Word）
    by_ext: dict[str, list[str]] = {}
    for p in files:
        if p.is_dir():
            continue
        suf = p.suffix.lower() or "(无后缀)"
        by_ext.setdefault(suf, []).append(p.name)
    summary_bits: list[str] = []
    for suf in (".docx", ".doc", ".xlsx", ".xls", ".txt", ".pdf", ".md"):
        names = by_ext.get(suf) or []
        if names:
            summary_bits.append(f"{suf}×{len(names)}: " + "、".join(names[:12]))
    other_docs = [
        f"{s}×{len(ns)}"
        for s, ns in sorted(by_ext.items())
        if s not in (".docx", ".doc", ".xlsx", ".xls", ".txt", ".pdf", ".md", ".lnk", ".url")
    ]
    if other_docs:
        summary_bits.append("其它: " + ", ".join(other_docs[:8]))
    lnk_n = len(by_ext.get(".lnk") or []) + len(by_ext.get(".url") or [])
    if lnk_n:
        summary_bits.append(f"快捷方式×{lnk_n}")

    files_sorted = sorted(files, key=_desktop_file_priority)
    icons = {i.name.lower(): i for i in _enum_desktop_icons()}
    rows: list[list[str]] = []
    for p in files_sorted[:limit]:
        kind = "文件夹" if p.is_dir() else (p.suffix.lower() or "文件")
        name = p.name
        icon = icons.get(name.lower()) or icons.get(p.stem.lower())
        coord = f"({icon.x},{icon.y})" if icon else "-"
        rows.append([name, kind, str(p), coord])
    body = _fmt_rows(["名称", "类型", "路径", "图标坐标"], rows, max_rows=limit)
    root = desktop_dir()
    head = f"桌面目录：{root}\n共 {len(files)} 项（显示 {min(len(files), limit)}）"
    if summary_bits:
        head += "\n【分类摘要】" + "；".join(summary_bits)
    if ext_set:
        head += f"\n【已过滤后缀】{', '.join(sorted(ext_set))}"
    return True, f"{head}\n{body}"


# ---------------------------------------------------------------------------
# 窗口
# ---------------------------------------------------------------------------


def _enum_open_windows(*, limit: int = 60) -> list[OpenWindow]:
    limit = max(5, min(int(limit), 120))
    foreground = int(user32.GetForegroundWindow() or 0)
    results: list[OpenWindow] = []

    @_WNDENUMPROC
    def _cb(hwnd, _lparam):  # type: ignore[misc]
        if len(results) >= limit:
            return False
        if not user32.IsWindowVisible(hwnd):
            return True
        if user32.GetWindow(hwnd, 4):  # GW_OWNER
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = (buf.value or "").strip()
        if not title:
            return True
        # 跳过工具窗 / 不可见样式
        style = user32.GetWindowLongW(hwnd, -16)  # GWL_STYLE
        if style & 0x80000000:  # WS_POPUP only without caption often tray
            ex = user32.GetWindowLongW(hwnd, -20)
            if ex & 0x00000080:  # WS_EX_TOOLWINDOW
                return True
        rect = _RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return True
        if rect.right - rect.left <= 0 or rect.bottom - rect.top <= 0:
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        proc_name = _process_name(int(pid.value))
        results.append(
            OpenWindow(
                title=title,
                hwnd=int(hwnd),
                pid=int(pid.value),
                process=proc_name,
                active=int(hwnd) == foreground,
                left=int(rect.left),
                top=int(rect.top),
                right=int(rect.right),
                bottom=int(rect.bottom),
                visible=True,
            )
        )
        return True

    user32.EnumWindows(_cb, 0)
    # 激活窗排前面
    results.sort(key=lambda w: (not w.active, w.title.lower()))
    return results


def list_open_windows(*, limit: int = 40) -> tuple[bool, str]:
    """返回可见顶层窗口：标题、句柄、进程、是否激活、屏幕矩形。"""
    try:
        wins = _enum_open_windows(limit=limit)
    except Exception as e:
        logger.exception("list_open_windows")
        return False, f"列出窗口失败: {e}"
    if not wins:
        return True, "当前没有可见顶层窗口"
    rows = []
    for w in wins:
        active = "是" if w.active else "否"
        rows.append(
            [
                w.title[:60],
                hex(w.hwnd),
                w.process,
                str(w.pid),
                active,
                f"{w.left},{w.top},{w.right},{w.bottom}",
            ]
        )
    body = _fmt_rows(
        ["标题", "句柄", "进程", "PID", "激活", "left,top,right,bottom"],
        rows,
        max_rows=limit,
    )
    return True, f"可见窗口共 {len(wins)} 个：\n{body}"


# ---------------------------------------------------------------------------
# 桌面图标（SysListView32）
# ---------------------------------------------------------------------------


def _find_desktop_listview() -> int:
    """定位桌面图标 ListView（Progman / WorkerW）。"""
    progman = user32.FindWindowW("Progman", None)
    if progman:
        # 0x052C 可生成 WorkerW（部分系统需要）
        user32.SendMessageTimeoutW(progman, 0x052C, 0, 0, 0, 1000, ctypes.byref(ctypes.c_ulong()))

    def _find_listview_under(parent: int) -> int:
        defview = user32.FindWindowExW(parent, 0, "SHELLDLL_DefView", None)
        if defview:
            lv = user32.FindWindowExW(defview, 0, "SysListView32", None)
            if lv:
                return int(lv)
        return 0

    lv = _find_listview_under(int(progman or 0))
    if lv:
        return lv

    worker = 0
    while True:
        worker = user32.FindWindowExW(0, worker, "WorkerW", None)
        if not worker:
            break
        lv = _find_listview_under(int(worker))
        if lv:
            return lv
    return 0


def _read_remote_text(hwnd: int, index: int) -> str:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    access = (
        _PROCESS_VM_OPERATION
        | _PROCESS_VM_READ
        | _PROCESS_VM_WRITE
        | _PROCESS_QUERY_INFORMATION
    )
    hproc = kernel32.OpenProcess(access, False, pid.value)
    if not hproc:
        return ""
    try:
        remote_item = kernel32.VirtualAllocEx(
            hproc, None, ctypes.sizeof(_LVITEMW), _MEM_COMMIT, _PAGE_READWRITE
        )
        remote_text = kernel32.VirtualAllocEx(
            hproc, None, (_MAX_PATH + 1) * 2, _MEM_COMMIT, _PAGE_READWRITE
        )
        if not remote_item or not remote_text:
            return ""
        try:
            item = _LVITEMW()
            item.mask = _LVIF_TEXT
            item.iItem = index
            item.iSubItem = 0
            item.pszText = remote_text
            item.cchTextMax = _MAX_PATH
            written = ctypes.c_size_t(0)
            kernel32.WriteProcessMemory(
                hproc,
                remote_item,
                ctypes.byref(item),
                ctypes.sizeof(item),
                ctypes.byref(written),
            )
            user32.SendMessageW(hwnd, _LVM_GETITEMTEXTW, index, remote_item)
            buf = ctypes.create_unicode_buffer(_MAX_PATH + 1)
            kernel32.ReadProcessMemory(
                hproc,
                remote_text,
                buf,
                (_MAX_PATH + 1) * 2,
                ctypes.byref(written),
            )
            return (buf.value or "").strip()
        finally:
            if remote_item:
                kernel32.VirtualFreeEx(hproc, remote_item, 0, _MEM_RELEASE)
            if remote_text:
                kernel32.VirtualFreeEx(hproc, remote_text, 0, _MEM_RELEASE)
    finally:
        kernel32.CloseHandle(hproc)


def _read_remote_rect(hwnd: int, index: int) -> _RECT | None:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    access = (
        _PROCESS_VM_OPERATION
        | _PROCESS_VM_READ
        | _PROCESS_VM_WRITE
        | _PROCESS_QUERY_INFORMATION
    )
    hproc = kernel32.OpenProcess(access, False, pid.value)
    if not hproc:
        return None
    try:
        remote = kernel32.VirtualAllocEx(
            hproc, None, ctypes.sizeof(_RECT), _MEM_COMMIT, _PAGE_READWRITE
        )
        if not remote:
            return None
        try:
            rect = _RECT()
            rect.left = _LVIR_BOUNDS
            written = ctypes.c_size_t(0)
            kernel32.WriteProcessMemory(
                hproc, remote, ctypes.byref(rect), ctypes.sizeof(rect), ctypes.byref(written)
            )
            user32.SendMessageW(hwnd, _LVM_GETITEMRECT, index, remote)
            kernel32.ReadProcessMemory(
                hproc, remote, ctypes.byref(rect), ctypes.sizeof(rect), ctypes.byref(written)
            )
            return rect
        finally:
            kernel32.VirtualFreeEx(hproc, remote, 0, _MEM_RELEASE)
    finally:
        kernel32.CloseHandle(hproc)


def _enum_desktop_icons() -> list[DesktopIcon]:
    lv = _find_desktop_listview()
    if not lv:
        return []
    count = int(user32.SendMessageW(lv, _LVM_GETITEMCOUNT, 0, 0))
    if count <= 0 or count > 500:
        return []
    # ListView 客户区 → 屏幕
    pt = _POINT(0, 0)
    user32.ClientToScreen(lv, ctypes.byref(pt))
    origin_x, origin_y = int(pt.x), int(pt.y)

    path_by_name: dict[str, str] = {}
    for p in _iter_desktop_paths():
        path_by_name[p.name.lower()] = str(p)
        path_by_name[p.stem.lower()] = str(p)

    icons: list[DesktopIcon] = []
    for i in range(count):
        name = _read_remote_text(lv, i)
        if not name:
            continue
        rect = _read_remote_rect(lv, i)
        if not rect:
            continue
        left = origin_x + int(rect.left)
        top = origin_y + int(rect.top)
        right = origin_x + int(rect.right)
        bottom = origin_y + int(rect.bottom)
        cx = (left + right) // 2
        cy = (top + bottom) // 2
        path = path_by_name.get(name.lower(), "")
        icons.append(
            DesktopIcon(
                name=name,
                x=cx,
                y=cy,
                left=left,
                top=top,
                right=right,
                bottom=bottom,
                path=path,
            )
        )
    return icons


def list_desktop_icons(*, limit: int = 80) -> tuple[bool, str]:
    """返回桌面图标名称与屏幕中心坐标（可直接给 click_at / mouse_click）。"""
    limit = max(5, min(int(limit), 200))
    try:
        icons = _enum_desktop_icons()
    except Exception as e:
        logger.exception("list_desktop_icons")
        return False, f"读取桌面图标失败: {e}"
    if not icons:
        # 回退：至少给出文件名，坐标未知（Win11 新桌面宿主可能无 SysListView32）
        files = _iter_desktop_paths()
        if not files:
            return True, "桌面没有图标/文件（或无法读取图标列表）"
        rows = [[p.name, "-", str(p)] for p in files[:limit]]
        body = _fmt_rows(["名称", "坐标", "路径"], rows, max_rows=limit)
        return (
            True,
            "未能读取图标屏幕坐标（系统桌面宿主可能不是经典 ListView）。"
            f"以下为桌面文件名，点击请配合 read_screen/ocr：\n{body}",
        )
    rows = []
    for ic in icons[:limit]:
        rows.append([ic.name, f"({ic.x},{ic.y})", ic.path or "-"])
    body = _fmt_rows(["名称", "中心坐标(x,y)", "路径"], rows, max_rows=limit)
    return True, f"桌面图标共 {len(icons)} 个：\n{body}"


def show_desktop() -> tuple[bool, str]:
    """露出桌面图标：最小化所有窗口（非 Win+D 切换，避免来回抖）。"""
    try:
        out = powershell(
            "(New-Object -ComObject Shell.Application).MinimizeAll(); "
            "Start-Sleep -Milliseconds 450; Write-Output 'OK'",
            timeout=8,
        )
        if "OK" in (out or ""):
            return True, "已露出桌面（已最小化窗口）"
        return False, f"露出桌面失败: {(out or '')[:80]}"
    except Exception as e:
        return False, f"露出桌面失败: {e}"


def click_desktop_icon(
    name: str,
    *,
    double: bool = True,
    show_first: bool = True,
) -> tuple[bool, str]:
    """按图标名点击：可选先清桌面 → 匹配名称 → 双击/单击坐标。"""
    target = (name or "").strip()
    if not target:
        return False, "请提供桌面图标名称"
    if show_first:
        ok_d, msg_d = show_desktop()
        if not ok_d:
            return False, msg_d
        time.sleep(0.35)
    icons = _enum_desktop_icons()
    if not icons:
        return False, "读不到桌面图标坐标，请改用 open_path 按路径打开"
    needle = target.lower()

    def _norm(s: str) -> str:
        s = (s or "").strip().lower()
        for suf in (".lnk", ".url", ".exe", ".txt", ".docx", ".pdf"):
            if s.endswith(suf):
                s = s[: -len(suf)]
        return s

    n_needle = _norm(needle)
    hit = None
    for ic in icons:
        n = ic.name.lower()
        if n == needle or _norm(n) == n_needle:
            hit = ic
            break
    if hit is None:
        for ic in icons:
            if n_needle in _norm(ic.name) or needle in ic.name.lower():
                hit = ic
                break
    if hit is None:
        names = "、".join(ic.name for ic in icons[:12])
        return False, f"桌面上没找到「{target}」。可见如：{names}"
    from .mouse_ops import click, double_click

    if double:
        ok, msg = double_click(hit.x, hit.y)
    else:
        ok, msg = click(hit.x, hit.y)
    if ok:
        action = "双击" if double else "单击"
        return True, f"已{action}桌面图标「{hit.name}」@({hit.x},{hit.y})"
    return False, f"点击失败: {msg}"
