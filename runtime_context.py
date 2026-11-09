"""运行环境上下文 — Loop / Legacy 共用（按意图轻量注入，避免每轮塞满）。"""

from __future__ import annotations

import platform

from .intent_utils import extract_im_contact
from .tools.desktop_apps import list_desktop_apps


def os_hint() -> str:
    if platform.system() == "Windows":
        base = "[运行环境] Windows；run_shell 用 PowerShell。"
        try:
            from .tools.privilege import privilege_level

            level = privilege_level()
            labels = {
                "system": "SYSTEM",
                "admin": "管理员",
                "user": "标准用户",
            }
            base += f" 权限：{labels.get(level, level)}"
        except Exception:
            pass
        return base
    return "[运行环境] Unix；run_shell 用 bash/zsh。"


def build_extra_context(
    user_text: str,
    *,
    apps_from_rules: dict | None = None,
    mcp_names: list[str] | None = None,
    strong_model: bool = False,
) -> str:
    parts: list[str] = []
    t = (user_text or "").strip()
    if not t:
        return ""

    app_limit = 8 if strong_model else 12
    app_verbs = ("打开", "启动", "运行", "关闭", "关掉", "退出")

    if any(k in t for k in app_verbs):
        desktop_apps = list_desktop_apps(limit=app_limit)
        if desktop_apps:
            parts.append(
                "常见软件：" + "、".join(desktop_apps[:app_limit])
            )

    if any(k in t for k in ("微信", "发消息", "回微信", "聊天", "联系人", "QQ")):
        im_hint = (
            "IM：open_app→read_screen→搜索框/Ctrl+F→短关键词→选第一项→"
            "确认标题后再输入发送；勿侧栏点名。"
        )
        contact_key = extract_im_contact(t)
        if contact_key:
            im_hint += f" 本句关键词：「{contact_key}」。"
        parts.append(im_hint)

    rules_apps = apps_from_rules or {}
    if rules_apps and any(k in t for k in ("打开", "启动", "运行")):
        names = list(rules_apps.keys())[:8]
        parts.append("内置启动项：" + "、".join(names))

    # MCP：显式提到扩展时提示；相关意图时也简短带一句，避免模型不知道有扩展工具
    if mcp_names:
        tl = t.lower()
        explicit = any(k in tl for k in ("mcp", "扩展", "插件"))
        related = any(
            k in t
            for k in (
                "浏览器", "网页", "抓取", "playwright", "文件系统",
                "Downloads", "Documents", "github", "仓库",
            )
        )
        if explicit or related:
            parts.append(
                "MCP："
                + "、".join(list(mcp_names)[:12])
                + ("…" if len(mcp_names) > 12 else "")
                + "（可用 mcp__服务器__工具 调用）"
            )

    # 权限/环境仅在 shell、提权、系统操作时附带
    if any(
        k in t
        for k in (
            "命令", "shell", "powershell", "管理员", "提权", "权限",
            "注册表", "服务", "系统",
        )
    ):
        parts.append(os_hint())

    return "\n".join(parts)
