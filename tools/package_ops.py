"""软件包管理 — 基于 winget"""

from __future__ import annotations

import re
import shutil
import subprocess

from .winops import _NO_WINDOW


def _winget_available() -> bool:
    return shutil.which("winget") is not None


def _run_winget(args: list[str], timeout: float = 300) -> tuple[int, str]:
    try:
        r = subprocess.run(
            ["winget", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=_NO_WINDOW,
        )
        out = (r.stdout or "").strip()
        err = (r.stderr or "").strip()
        combined = out
        if err:
            combined = (combined + "\n" + err).strip() if combined else err
        return r.returncode, combined
    except subprocess.TimeoutExpired:
        return -1, "（winget 命令超时）"
    except Exception as e:
        return -1, str(e)


def list_installed_software(query: str = "", limit: int = 30) -> tuple[bool, str]:
    if not _winget_available():
        return False, "未找到 winget，请确认已安装 App Installer"

    # limit 容错：非数字/负数/None 都用默认值 30
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 30
    limit = max(5, min(limit, 60))

    args = ["list", "--accept-source-agreements", "--disable-interactivity"]
    q = ""
    if query is not None:
        q = str(query).strip()
    if q:
        args.extend(["--query", q])

    code, out = _run_winget(args, timeout=120)
    if not out:
        return False, "列出已安装软件失败"

    # winget 找不到软件时的提示，识别后直接返回"未找到"
    lower_out = out.lower()
    if (
        "找不到" in out
        and "匹配" in out
        or "no package found" in lower_out
        or "no installed package" in lower_out
    ):
        hint = f"未找到匹配「{q}」的软件" if q else "未找到已安装软件"
        return True, hint

    data: list[str] = []
    for ln in out.splitlines():
        s = ln.strip()
        if not s or s in ("\\", "|", "/", "-") or re.match(r"^[\s\\|/-]+$", s):
            continue
        if s.startswith("名称") or s.startswith("Name") or set(s) <= {"-", " "}:
            continue
        if len(s) < 4:
            continue
        # 跳过进度条行（winget 偶尔把进度输出到 stdout）
        if re.match(r"^[█▒\s]+\s*\d+%", s):
            continue
        # 跳过 winget 的"找不到"提示行
        if "找不到" in s and "匹配" in s:
            continue
        data.append(s)
        if len(data) >= limit:
            break

    if not data:
        hint = f"未找到匹配「{q}」的软件" if q else "未找到已安装软件"
        return True, hint
    header = f"已安装软件（{'含 ' + q if q else '前 ' + str(len(data)) + ' 项'}）："
    return True, header + "\n" + "\n".join(data)


def install_software(package: str, silent: bool = True) -> tuple[bool, str]:
    if not _winget_available():
        return False, "未找到 winget，请确认已安装 App Installer"

    package = (package or "").strip()
    if not package:
        return False, "请提供软件包 ID 或名称"

    args = ["install", "--accept-package-agreements", "--accept-source-agreements"]
    if silent:
        args.append("--silent")
    if "." in package or package.startswith("{"):
        args.extend(["--id", package])
    else:
        args.extend(["--name", package])

    code, out = _run_winget(args, timeout=600)
    lower = (out or "").lower()
    if code == 0 or "successfully installed" in lower or "已成功安装" in out:
        return True, f"安装完成: {package}"
    if "already installed" in lower or "已安装" in out:
        return True, f"软件已安装: {package}"
    snippet = (out or "安装失败")[:500]
    return False, f"安装失败: {snippet}"


def is_package_installed(package: str) -> bool:
    """检查 winget 是否仍能列出该软件包。"""
    package = (package or "").strip()
    if not package:
        return False
    if not _winget_available():
        return False

    args = ["list", "--accept-source-agreements", "--disable-interactivity"]
    if "." in package or package.startswith("{"):
        args.extend(["--id", package])
    else:
        args.extend(["--query", package])

    code, out = _run_winget(args, timeout=60)
    if code != 0 or not out:
        return False

    lower_out = out.lower()
    if (
        "找不到" in out and "匹配" in out
        or "no package found" in lower_out
        or "no installed package" in lower_out
    ):
        return False

    pkg_lower = package.lower()
    for ln in out.splitlines():
        s = ln.strip()
        if not s or len(s) < 4:
            continue
        if s.startswith("名称") or s.startswith("Name") or set(s) <= {"-", " "}:
            continue
        if pkg_lower in s.lower():
            return True
    return False


def uninstall_software_verified(
    package: str,
    silent: bool = True,
) -> tuple[bool, str, bool]:
    """卸载并复核；返回 (success, message, verified)。"""
    from .execution_result import ExecutionResult, can_report_success

    package = (package or "").strip()
    if not package:
        return False, "请提供软件包 ID 或名称", False

    ok, msg = uninstall_software(package, silent=silent)
    if not ok:
        return False, msg, False

    verified = not is_package_installed(package)
    result = ExecutionResult(
        success=ok and verified,
        message=msg if verified else f"{msg}，但未能确认软件已完全卸载",
        verified=verified,
        action="uninstall_software",
        target=package,
    )
    return can_report_success(result), result.message, verified


def uninstall_software(package: str, silent: bool = True) -> tuple[bool, str]:
    if not _winget_available():
        return False, "未找到 winget，请确认已安装 App Installer"

    package = (package or "").strip()
    if not package:
        return False, "请提供软件包 ID 或名称"

    args = ["uninstall", "--accept-source-agreements"]
    if silent:
        args.append("--silent")
    if "." in package or package.startswith("{"):
        args.extend(["--id", package])
    else:
        args.extend(["--name", package])

    code, out = _run_winget(args, timeout=300)
    lower = (out or "").lower()
    if code == 0 or "successfully uninstalled" in lower or "已成功卸载" in out:
        return True, f"已卸载: {package}"
    snippet = (out or "卸载失败")[:500]
    return False, f"卸载失败: {snippet}"
