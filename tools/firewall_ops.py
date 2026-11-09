r"""防火墙规则控制 — 微软官方 netsh advfirewall CLI。

「别让 XX 软件联网」→ 解析应用 exe 路径 → 添加出站阻止规则：
  netsh advfirewall firewall add rule name=<kks_前缀> dir=out action=block program="<path>"
恢复联网 = 删除科科斯创建的对应规则（不动用户手工建的规则）。

设计约束：
  - 科科斯创建的规则一律 kks_ 前缀命名，list/delete 只操作前缀内的规则
  - 规则修改需要管理员权限（Windows 安全模型），非管理员明确拒绝
  - 写后读回验证（show rule name=<name> 确认存在且路径一致）
  - netsh 输出随系统语言本地化（实测本机为英文输出），解析按双语关键词行扫描

应用路径解析两级：
  1. 运行中进程（Get-Process 匹配进程名取 Path）
  2. App Paths 注册表键（HKLM/HKCU 的 App Paths 条目，官方安装约定）
"""

from __future__ import annotations

import base64
import logging
import re
import subprocess

logger = logging.getLogger(__name__)

# netsh 子进程超时（秒）：add/show/delete 都是本地操作，很快
_NETSH_TIMEOUT = 20

# 科科斯管理的规则名前缀（list/delete 只碰前缀内规则）
_KKS_RULE_PREFIX = "kks_"

# 常见进程名 → 匹配别名（用户说「微信」时进程名是 weixin/wechat）
_APP_ALIASES: dict[str, tuple[str, ...]] = {
    "微信": ("weixin", "wechat"),
    "qq": ("qq",),
    "钉钉": ("dingtalk",),
    "迅雷": ("thunder", "xl"),
    "网易云音乐": ("cloudmusic", "netease"),
    "qq音乐": ("qqmusic",),
    "酷狗": ("kugou",),
    "百度网盘": ("baidunetdisk", "baidunetdiskhost"),
    "edge": ("msedge",),
    "chrome": ("chrome",),
    "firefox": ("firefox",),
}


def _is_admin() -> bool:
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _run_cmd(args: list[str], timeout: int = _NETSH_TIMEOUT) -> str:
    """执行命令并解码输出（utf-8 严格优先，gbk 兜底——见 power_ops 同款教训）。"""
    try:
        proc = subprocess.run(args, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"命令超时（{timeout}s）") from e
    raw = proc.stdout or b""
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or b"").decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"命令退出码 {proc.returncode}: {err[:200]}")
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("gbk", errors="replace")


def _run_netsh(args: list[str], timeout: int = _NETSH_TIMEOUT) -> str:
    return _run_cmd(["netsh", "advfirewall", *args], timeout)


# ---------------------------------------------------------------------------
# 应用路径解析
# ---------------------------------------------------------------------------

_PS_FIND_PROC_PATH = r"""
$ErrorActionPreference='SilentlyContinue'
$re='^(__KEYWORDS__)$'
Get-Process |
    Where-Object { $_.Path -and ($_.ProcessName -match $re) } |
    Select-Object -ExpandProperty Path -Unique -First 5
"""


def _find_running_paths(keys: list[str]) -> list[str]:
    """查运行中进程的 exe 路径（进程名全词匹配任一关键词）。"""
    kw = "|".join(re.escape(k) for k in keys)
    script = _PS_FIND_PROC_PATH.replace("__KEYWORDS__", kw)
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            capture_output=True, timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.debug("进程路径查询失败: %s", e)
        return []
    out = (proc.stdout or b"").decode("utf-8", errors="replace")
    return [line.strip() for line in out.splitlines() if line.strip().lower().endswith(".exe")]


def _alias_keywords(app: str) -> list[str]:
    """应用名 → 进程名候选列表（别名表 + 原词）。"""
    low = app.strip().lower()
    keys = [low]
    for cn, aliases in _APP_ALIASES.items():
        if low == cn or cn in low:
            keys.extend(aliases)
    return sorted({k for k in keys if k})


def resolve_app_path(app: str) -> tuple[bool, str]:
    """解析应用 exe 路径：运行进程 → App Paths 注册表。

    返回 (ok, 路径或候选列表/错误提示)。
    """
    if not app or not str(app).strip():
        return False, "需要指定应用名，例如：微信、chrome、迅雷"
    keys = _alias_keywords(str(app))
    # 1) 运行中进程
    for p in _find_running_paths(keys):
        return True, p
    # 2) App Paths 注册表（官方安装约定）
    import winreg

    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for k in keys:
            for sub in (f"{k}.exe", k):
                try:
                    with winreg.OpenKey(
                        hive, rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{sub}"
                    ) as key:
                        val, _ = winreg.QueryValueEx(key, "")
                        val = str(val).strip().strip('"')
                        if val.lower().endswith(".exe"):
                            return True, val
                except OSError:
                    continue
    return False, (
        f"没有找到「{app}」的可执行文件（它可能没在运行，或未按标准方式安装）。"
        "可以说出完整路径，例如『屏蔽 C:\\xxx\\app.exe 联网』"
    )


def _safe_rule_name(app: str) -> str:
    """应用名 → 规则名片段（去非法字符，限长）。"""
    frag = re.sub(r"[\\/:*?\"<>|\s]+", "_", str(app).strip()).strip("_")[:40]
    return frag or "app"


# ---------------------------------------------------------------------------
# 防火墙操作
# ---------------------------------------------------------------------------

def get_firewall_status() -> tuple[bool, str]:
    """读取防火墙三个配置文件（域/专用/公用）的开关状态。

    netsh 输出双语兼容：段头 Domain/Private/Public Profile Settings（中文为
    「域/专用/公用配置文件设置」），开关行 State ON/OFF（中文「状态 启用/禁用」）。
    """
    try:
        out = _run_netsh(["show", "allprofiles"])
    except RuntimeError as e:
        return False, f"读取防火墙状态失败：{e}"
    profiles = {"域": None, "专用": None, "公用": None}
    current = None
    for raw_line in out.splitlines():
        low = raw_line.strip().lower()
        if "domain profile" in low:
            current = "域"
        elif "private profile" in low:
            current = "专用"
        elif "public profile" in low:
            current = "公用"
        if current is None:
            continue
        is_state_line = low.startswith("state") or low.startswith("状态")
        if not is_state_line:
            continue
        value = raw_line.strip().split(maxsplit=1)[-1].strip() if raw_line.strip() else ""
        profiles[current] = ("启用" in value) or ("on" in value.lower().split()) or value.upper() == "ON"
        current = None
    parts = [
        f"{k}配置：{('开启' if v else '关闭') if v is not None else '未知'}"
        for k, v in profiles.items()
    ]
    return True, "防火墙状态——" + "，".join(parts)


def list_firewall_rules() -> tuple[bool, str]:
    """列出科科斯创建的防火墙规则（kks_ 前缀），并统计全部规则数。"""
    try:
        out = _run_netsh(["firewall", "show", "rule", "name=all"])
    except RuntimeError as e:
        return False, f"读取防火墙规则失败：{e}"
    kks_rules: list[str] = []
    total = 0
    current_name = ""
    for raw_line in out.splitlines():
        line = raw_line.strip()
        m = re.match(r"(?:规则名称|Rule Name):\s*(.+)", line)
        if m:
            total += 1
            current_name = m.group(1).strip()
            if current_name.lower().startswith(_KKS_RULE_PREFIX):
                kks_rules.append(current_name)
    lines = [f"防火墙共 {total} 条规则，其中科科斯创建的 {len(kks_rules)} 条："]
    if kks_rules:
        lines.extend(f"- {r}" for r in kks_rules)
    else:
        lines.append("（还没有科科斯创建的规则。可以说『别让 xx 联网』来屏蔽应用）")
    return True, "\n".join(lines)


def _verify_rule(name: str, program: str, direction: str, action: str) -> bool:
    """写后读回：规则存在、程序路径一致。"""
    try:
        out = _run_netsh(["firewall", "show", "rule", f"name={name}"])
    except RuntimeError:
        return False
    if name not in out:
        return False
    low = out.lower()
    # 双语关键词：程序/Program 行应包含 exe 路径；方向与动作出现即认可
    prog_ok = program.lower() in low
    dir_ok = (direction == "out" and ("方向" in out or "dir" in low)) or direction != "out"
    act_ok = action in ("block", "allow")
    return prog_ok and dir_ok and act_ok


def block_app_network(app: str | None = None, direction: str | None = "out") -> tuple[bool, str]:
    """屏蔽应用联网（添加防火墙阻止规则）。direction: out=出站（缺省）/in=入站/both。"""
    if not app or not str(app).strip():
        return False, "需要指定应用名，例如『别让微信联网』"
    direction = str(direction or "out").strip().lower()
    if direction not in ("out", "in", "both"):
        return False, "direction 必须是 out（出站）/in（入站）/both"
    if not _is_admin():
        return False, (
            "修改防火墙规则需要管理员权限。请以管理员身份运行科科斯后再试"
        )

    ok, resolved = resolve_app_path(str(app).strip())
    if not ok:
        return False, resolved
    program = resolved
    frag = _safe_rule_name(str(app))
    names: list[str] = []
    dirs = ["out", "in"] if direction == "both" else [direction]
    try:
        for d in dirs:
            name = f"{_KKS_RULE_PREFIX}block_{frag}_{d}"
            _run_netsh([
                "firewall", "add", "rule",
                f"name={name}",
                f"dir={d}", "action=block",
                f"program={program}",
                "enable=yes",
            ])
            names.append(name)
    except RuntimeError as e:
        # 回滚已添加的部分规则，避免半残状态
        for n in names:
            try:
                _run_netsh(["firewall", "delete", "rule", f"name={n}"])
            except RuntimeError:
                pass
        return False, f"添加防火墙规则失败：{e}"

    if not _verify_rule(names[0], program, dirs[0], "block"):
        for n in names:
            try:
                _run_netsh(["firewall", "delete", "rule", f"name={n}"])
            except RuntimeError:
                pass
        return False, (
            f"规则已添加但读回验证未通过，已回滚。请确认程序路径是否正确：{program}"
        )
    d_cn = {"out": "出站", "in": "入站", "both": "出站+入站"}[direction]
    return True, f"已屏蔽 {app} 的{d_cn}联网（程序：{program}）。恢复可以说『允许 xx 联网』"


def allow_app_network(app: str | None = None) -> tuple[bool, str]:
    """恢复应用联网：删除科科斯创建的对应阻止规则（只删 kks_ 前缀）。"""
    if not app or not str(app).strip():
        return False, "需要指定应用名，例如『允许微信联网』"
    if not _is_admin():
        return False, (
            "修改防火墙规则需要管理员权限。请以管理员身份运行科科斯后再试"
        )
    frag = _safe_rule_name(str(app))
    deleted: list[str] = []
    for d in ("out", "in"):
        name = f"{_KKS_RULE_PREFIX}block_{frag}_{d}"
        try:
            _run_netsh(["firewall", "delete", "rule", f"name={name}"])
            deleted.append(name)
        except RuntimeError as e:
            if "没有与" in e or "no rules match" in e.lower():
                continue
            logger.debug("删除规则 %s 失败: %s", name, e)
    if not deleted:
        return False, (
            f"没有找到科科斯为「{app}」创建的屏蔽规则。"
            "可以说『查看防火墙规则』确认现有规则"
        )
    return True, f"已恢复 {app} 联网（删除了 {len(deleted)} 条屏蔽规则）"
