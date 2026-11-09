"""Windows 虚拟文件夹（shell 命名空间）— 此电脑/回收站等无 exe、未必有快捷方式。"""

from __future__ import annotations

# 显示名/俗称 → shell 路径（app_control._launch_via_shell 已支持 shell:）
_SHELL_FOLDERS: dict[str, str] = {
    "此电脑": "shell:MyComputerFolder",
    "我的电脑": "shell:MyComputerFolder",
    "my computer": "shell:MyComputerFolder",
    "computer": "shell:MyComputerFolder",
    "回收站": "shell:RecycleBinFolder",
    "垃圾箱": "shell:RecycleBinFolder",
    "recycle bin": "shell:RecycleBinFolder",
    "控制面板": "shell:ControlPanelFolder",
    "网络": "shell:NetworkPlacesFolder",
    "网络位置": "shell:NetworkPlacesFolder",
    "桌面文件夹": "shell:Desktop",
    "下载文件夹": "shell:Downloads",
    "文档文件夹": "shell:Documents",
    "图片文件夹": "shell:Pictures",
    "我的图片": "shell:Pictures",
    "我的文档": "shell:Documents",
    "我的下载": "shell:Downloads",
}


def resolve_shell_folder(name: str) -> tuple[str, str] | None:
    """按名称解析虚拟文件夹，返回 (规范显示名, shell 命令)。"""
    raw = (name or "").strip()
    if not raw:
        return None
    low = raw.lower()

    if low in _SHELL_FOLDERS:
        for k, v in _SHELL_FOLDERS.items():
            if k.lower() == low:
                return k, v

    for key, cmd in _SHELL_FOLDERS.items():
        klow = key.lower()
        if low == klow or raw == key:
            return key, cmd
        if len(key) >= 2 and (key in raw or raw in key):
            return key, cmd
    return None


def all_shell_folder_names() -> list[str]:
    return sorted(_SHELL_FOLDERS.keys())
