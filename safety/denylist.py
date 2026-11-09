"""通用危险指令黑名单 — 对用户原文与敏感工具参数做二次拦截。

与 shell_guard 的区别：
- shell_guard 只检查 run_shell* 的 command 参数；
- 本模块在 guard.check_tool_call 层兜底，扫描用户原文与文件/系统类工具参数，
  拦截破坏性/系统级变更意图，即使 LLM 把危险动作包装成 delete_file、
  move_to_trash、write_registry 等工具调用也一样挡下。

策略保守：只拦「破坏性 / 不可逆 / 系统级」动作，避免误伤正常登录、搜索等场景。
"""

from __future__ import annotations

import re

# 系统关键路径（含大小写与分隔符变体）— 出现在文件操作工具参数中即拒绝
_PROTECTED_SYSTEM_PATH_RE = re.compile(
    r"(?i)^(?:[a-zA-Z]:)?[\\/]*(?:windows|program files(?:\s*\(x86\))?|programdata|"
    r"boot|system32|windows\.old|\\$windows\.~bt|users\\default|"
    r"recovery|perflogs|config\.msi)(?:[\\/]|$)"
)

# 危险意图（用户原文 / 参数）— 命中即拒绝
_DENYLIST_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # 格式化 / 清空系统盘
    (
        re.compile(r"格式化\s*(?:c|d|e)?\s*盘?|format\s+[a-z]:\s*/q?|"
                   r"清空.{0,4}(?:系统盘|[cC]\s*盘)|(?:系统盘|[cC]\s*盘).{0,4}清空|"
                   r"抹掉系统盘|初始化磁盘|clear\s+system\s+drive", re.I),
        "禁止格式化或清空系统盘",
    ),
    # 启动 / 磁盘底层操作（超出 shell_guard 覆盖的路径）
    (
        re.compile(r"bcdedit|修改启动配置|重建\s*mbr|写\s*mbr|diskpart\s*$|"
                   r"clean\s+all\b", re.I),
        "禁止修改启动配置或磁盘底层结构",
    ),
    # 注册表系统级删除
    (
        re.compile(r"删除\s*(?:HKEY|HKLM|HKCR)|reg\s+delete\s+HKLM|"
                   r"清除.*注册表.*(?:HKEY|HKLM)", re.I),
        "禁止直接删除系统注册表项",
    ),
    # 关闭/绕过系统安全防护
    (
        re.compile(r"(?:关闭|禁用|停用|停止|关掉|关了|关).{0,10}(?:防火墙|defender|uac|"
                   r"系统防护|安全中心|杀毒软件)|(?:防火墙|defender|uac|系统防护|安全中心|"
                   r"杀毒软件).{0,4}(?:关闭|禁用|停用|停止|关掉|关了|关)|"
                   r"绕过\s*(?:uac|杀毒|安全)|禁止\s*(?:uac|defender)", re.I),
        "禁止关闭或绕过系统安全防护",
    ),
    # 凭据导出 / 读取敏感文件
    (
        re.compile(r"(?:导出|窃取|偷取|读取|查看|复制|备份).{0,12}(?:密码|口令|密钥|token|"
                   r"session|cookie|凭据|hash)|dump\s+(?:password|credential|hash)|"
                   r"读取\s*.*(?:hash|sam|system)\b", re.I),
        "禁止导出或读取系统凭据",
    ),
]

# 需要做路径保护的敏感工具（path 不能指向系统关键目录）
_SENSITIVE_PATH_TOOLS = frozenset({
    "delete_file",
    "move_to_trash",
    "rename_path",
    "move_path",
    "copy_path",
    "delete_folder",
    "empty_recycle_bin",
})


def scan_denylist(*texts: str) -> str | None:
    """扫描文本集合，命中任一危险模式返回原因；否则返回 None。"""
    for text in texts:
        if not text:
            continue
        for pat, reason in _DENYLIST_PATTERNS:
            if pat.search(text):
                return reason
    return None


def protected_path_deny_reason(path: str) -> str | None:
    """文件操作 path 参数指向系统关键目录时返回原因。"""
    p = (path or "").strip().strip('"').strip("'")
    if not p:
        return None
    # 相对路径也算（如 "\Windows"、"./windows"）
    if _PROTECTED_SYSTEM_PATH_RE.search(p):
        return "路径指向系统关键目录，已拦截"
    return None


def deny_sensitive_path_tool(func: str, args: dict | None) -> str | None:
    """敏感文件工具统一入口：返回拒绝原因或 None。"""
    if func not in _SENSITIVE_PATH_TOOLS:
        return None
    params = args or {}
    for key in ("path", "source", "target", "old_path", "new_path", "name"):
        v = params.get(key)
        if isinstance(v, str):
            reason = protected_path_deny_reason(v)
            if reason:
                return reason
    return None
