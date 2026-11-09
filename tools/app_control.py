"""应用启动 — 多策略 Shell 启动，等同用户手动打开"""

from __future__ import annotations

import logging
import os
import platform
import subprocess
import time

from .keyboard_input import type_text

logger = logging.getLogger(__name__)


def _launch_file(path: str) -> None:
    """对快捷方式/可执行文件依次尝试多种 Windows 启动方式。"""
    errors: list[str] = []
    attempts = [
        ("startfile", lambda: os.startfile(path)),
        (
            "explorer",
            lambda: subprocess.Popen(
                ["explorer.exe", path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ),
        ),
        (
            "cmd_start",
            lambda: subprocess.Popen(
                ["cmd", "/c", "start", "", path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ),
        ),
    ]
    for label, fn in attempts:
        try:
            fn()
            logger.info("应用已启动 [%s]: %s", label, path)
            return
        except Exception as e:
            errors.append(f"{label}:{e}")
    raise OSError("; ".join(errors))


def _launch_via_shell(command: str) -> None:
    if platform.system() != "Windows":
        raise OSError("仅支持 Windows")

    lower = command.lower()
    if command.startswith("appid:"):
        app_id = command[6:].strip()
        target = f"shell:AppsFolder\\{app_id}"
        try:
            os.startfile(target)
        except OSError:
            subprocess.Popen(
                ["explorer.exe", target],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        logger.info("应用已启动 [appid]: %s", app_id[:60])
        return

    if lower.startswith("shell:"):
        os.startfile(command)
        return

    if lower.startswith(("http://", "https://", "microsoft-edge:")):
        os.startfile(command)
        return

    if os.path.isfile(command):
        _launch_file(command)
        return

    if lower.endswith((".lnk", ".url", ".exe", ".bat", ".cmd")):
        _launch_file(command)
        return

    subprocess.Popen(
        ["cmd", "/c", "start", "", command],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    logger.info("应用已启动 [cmd_start]: %s", command)


def launch_app(command: str) -> tuple[bool, str]:
    command = (command or "").strip()
    if not command:
        return False, "启动命令为空"

    try:
        _launch_via_shell(command)
        if command.startswith("appid:"):
            display = command[6:].split("!")[-1][:40]
        elif os.path.isfile(command):
            display = os.path.splitext(os.path.basename(command))[0]
        else:
            display = command
        return True, f"已启动 {display}"
    except Exception as e:
        logger.error("启动失败 %s: %s", command, e)
        try:
            subprocess.Popen(
                [
                    "powershell",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    f'Start-Process -FilePath "{command}"',
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True, f"已启动 {command}"
        except Exception as e2:
            return False, f"启动失败: {e}; {e2}"


def launch_app_and_type(
    command: str,
    content: str,
    wait_sec: float = 1.5,
    app_name: str = "",
) -> tuple[bool, str]:
    ok, msg = launch_app(command)
    if not ok:
        return False, msg

    # 等待应用窗口出现并主动聚焦（而非固定 sleep）
    focused = False
    if app_name:
        from .process_ops import infer_process_names
        from .winops import focus_window_by_process_names

        names = infer_process_names(app_name, command)
        # 轻量轮询：用已知的进程名快速检查窗口是否出现（最多 15 秒）
        if names:
            name_ps = ",".join(
                f"'{n.replace(chr(39), chr(39)*2)}'" for n in names
            )
            # 检查进程存在且有主窗口（MainWindowHandle 非零即可，不要求标题）
            check_script = (
                f"$names = @({name_ps});"
                "foreach ($n in $names) {"
                " $p = Get-Process -Name $n -ErrorAction SilentlyContinue |"
                " Where-Object { $_.MainWindowHandle -ne [IntPtr]::Zero }"
                " if ($p) { exit 0 }"
                "}; exit 1"
            )
            # 进程存在检查（无窗口也算，用于慢启动应用的兜底聚焦）
            proc_check = (
                f"$names = @({name_ps});"
                "foreach ($n in $names) {"
                " if (Get-Process -Name $n -ErrorAction SilentlyContinue) { exit 0 }"
                "}; exit 1"
            )
            for i in range(30):  # 最多 ~9 秒，但间隔更短
                if i > 0:  # 首次不 sleep，立即检查
                    time.sleep(0.3)
                try:
                    r = subprocess.run(
                        ["powershell", "-NoProfile", "-NoLogo", "-Command", check_script],
                        capture_output=True,
                        timeout=2,
                        creationflags=getattr(
                            subprocess, "CREATE_NO_WINDOW", 0
                        ),
                    )
                    if r.returncode == 0:
                        # 窗口已出现，聚焦
                        fok, _ = focus_window_by_process_names(
                            names, timeout=3
                        )
                        if fok:
                            focused = True
                            time.sleep(0.2)  # 聚焦后等待 UI 就绪
                            break
                except Exception:
                    pass
            # 兜底：进程存在但窗口未就绪，也尝试聚焦
            if not focused:
                try:
                    r2 = subprocess.run(
                        ["powershell", "-NoProfile", "-NoLogo", "-Command", proc_check],
                        capture_output=True,
                        timeout=2,
                        creationflags=getattr(
                            subprocess, "CREATE_NO_WINDOW", 0
                        ),
                    )
                    if r2.returncode == 0:
                        fok, _ = focus_window_by_process_names(
                            names, timeout=3
                        )
                        if fok:
                            focused = True
                            time.sleep(0.3)
                except Exception:
                    pass
    else:
        time.sleep(wait_sec)

    if not focused:
        time.sleep(0.5)

    ok2, msg2 = type_text(content, delay_sec=0.2)
    if not ok2:
        return False, f"{msg}，但输入失败：{msg2}"
    return True, f"{msg}，{msg2}"
