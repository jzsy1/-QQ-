"""电源计划与睡眠/熄屏定时 — 微软官方 powercfg CLI 封装。

powercfg 是微软给电源管理开的官方命令行正门（无第三方依赖）：
  /list            列出电源计划（星号=当前活动）
  /setactive GUID  切换活动计划（普通用户权限即可）
  /getactivescheme 当前活动计划
  /change <alias> <秒>   修改当前活动计划的定时（0=从不）
  /q SCHEME_CURRENT <subgroup> <setting>  读取当前值（十六进制秒）

涉及别名均为 powercfg 官方英文别名（SUB_SLEEP/STANDBYIDLE、
SUB_VIDEO/VIDEOIDLE），规避手抄 GUID 的错漏。

定时设置遵循「读原值 → 设新值 → 读回验证」三步，供调用方还原。
powercfg 输出为本地 OEM 编码（中文系统 GBK），按编码链尝试解码。
"""

from __future__ import annotations

import logging
import re
import subprocess

logger = logging.getLogger(__name__)

# powercfg 子进程超时（秒）：powercfg 冷启动一般 <2s
_PCFG_TIMEOUT = 10

# 微软官方电源设置别名（powercfg 认英文别名，比手抄 GUID 稳）
_TIMEOUT_ALIASES = {
    "sleep": ("SUB_SLEEP", "STANDBYIDLE", "睡眠"),
    "monitor": ("SUB_VIDEO", "VIDEOIDLE", "关闭显示"),
}

# 定时上限：24 小时（分钟），0=从不
_TIMEOUT_MAX_MINUTES = 1440

# 计划行解析：Power Scheme GUID: <guid>  (名称) [*]
_PLAN_RE = re.compile(r"GUID:\s*([0-9a-fA-F-]{36})\s+\((.+?)\)\s*(\*)?", re.I)

# 交流/直流 关键词（英文输出为 AC/DC ... Index）
_AC_KEYWORDS = ("交流", "ac")
_DC_KEYWORDS = ("直流", "dc")


def _parse_index(out: str, which: str) -> int | None:
    """从 /q 输出解析 AC/DC 当前设置索引（十六进制秒）。

    中文输出行形如「当前交流电源设置索引: 0x00000e10」，
    英文输出形如「AC Power Setting Index: 0xe10」——按关键词扫描而非
    固定英文正则，规避系统语言差异。
    """
    kw = _AC_KEYWORDS if which == "ac" else _DC_KEYWORDS
    for line in out.splitlines():
        low = line.lower()
        if not any(k in low for k in kw):
            continue
        if "索引" not in line and "index" not in low:
            continue
        m = re.search(r"0x([0-9a-fA-F]+)", line)
        if m:
            return int(m.group(1), 16)
    return None


def _decode_output(raw: bytes) -> str:
    """powercfg 输出解码：不同子命令/系统可能是 UTF-8 或 OEM GBK。

    utf-8 严格解码优先（非法即失败，不会把 UTF-8 中文解码成乱码）；
    失败再按 GBK。GBK 宽松解码几乎不会失败，所以必须放后面。
    """
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("gbk", errors="replace")


def _run_powercfg(args: list[str]) -> str:
    """执行 powercfg，返回解码后的 stdout；失败抛 RuntimeError。"""
    cmd = ["powercfg", *args]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=_PCFG_TIMEOUT)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"powercfg 超时（{_PCFG_TIMEOUT}s）") from e
    if proc.returncode != 0:
        err = _decode_output(proc.stderr or proc.stdout or b"").strip()
        raise RuntimeError(f"powercfg 退出码 {proc.returncode}: {err[:200]}")
    return _decode_output(proc.stdout or b"")


def _clamp_minutes(minutes: int) -> int:
    """定时分钟数钳制：0=从不，上限 24 小时。"""
    try:
        m = int(minutes)
    except (TypeError, ValueError):
        return -1
    return max(0, min(_TIMEOUT_MAX_MINUTES, m))


def list_power_plans() -> tuple[bool, str]:
    """列出所有电源计划并标注当前活动计划。"""
    try:
        out = _run_powercfg(["/list"])
    except RuntimeError as e:
        return False, f"读取电源计划失败：{e}"
    plans = _PLAN_RE.findall(out)
    if not plans:
        return False, f"未能从 powercfg 输出解析电源计划：{out[:120]}"
    lines = [f"共 {len(plans)} 个电源计划："]
    for guid, name, star in plans:
        mark = "（当前使用）" if star else ""
        lines.append(f"- {name}{mark}（GUID: {guid}）")
    return True, "\n".join(lines)


def set_power_plan(plan: str | None = None) -> tuple[bool, str]:
    """切换电源计划。plan 支持：名称关键词（如 省电/节能/高性能/平衡）或 GUID。"""
    if not plan or not str(plan).strip():
        return False, "需要指定电源计划。可以说『切到省电模式』或先查看『列出电源计划』"
    key = str(plan).strip().lower()

    try:
        out = _run_powercfg(["/list"])
    except RuntimeError as e:
        return False, f"读取电源计划失败：{e}"
    plans = _PLAN_RE.findall(out)
    if not plans:
        return False, f"未能解析电源计划列表：{out[:120]}"

    target = None
    for guid, name, _star in plans:
        if key == guid.lower() or key in name.lower():
            target = (guid, name)
            break
    if target is None:
        names = "、".join(n for _, n, _ in plans)
        return False, f"没有找到与「{plan}」匹配的电源计划。当前有：{names}"

    guid, name = target
    try:
        _run_powercfg(["/setactive", guid])
    except RuntimeError as e:
        return False, f"切换电源计划失败：{e}"

    # 读回验证
    try:
        cur = _run_powercfg(["/getactivescheme"])
        verified = guid.lower() in cur.lower()
    except RuntimeError:
        verified = False
    if not verified:
        return False, f"已执行切换到「{name}」，但读回验证未通过（可能被策略锁定）"
    return True, f"已切换电源计划：{name}"


def _read_timeout_seconds(alias: str) -> int | None:
    """读当前活动计划指定别名的定时秒数（AC 值；读不到返回 None）。"""
    subgroup, setting, _ = _TIMEOUT_ALIASES[alias]
    try:
        out = _run_powercfg(["/q", "SCHEME_CURRENT", subgroup, setting])
    except RuntimeError as e:
        logger.debug("读取定时失败 %s: %s", alias, e)
        return None
    return _parse_index(out, "ac")


def _fmt_seconds(sec: int) -> str:
    if sec <= 0:
        return "从不"
    if sec >= 86400:
        d = sec // 86400
        h = (sec % 86400) // 3600
        return f"{d} 天 {h} 小时" if h else f"{d} 天"
    m, s = divmod(sec, 60)
    if m == 0:
        return f"{s} 秒"
    if m < 60:
        return f"{m} 分钟"
    h, mm = divmod(m, 60)
    return f"{h} 小时 {mm} 分钟" if mm else f"{h} 小时"


def get_power_timeouts() -> tuple[bool, str]:
    """读取当前计划的睡眠/关闭显示定时。"""
    parts: list[str] = []
    for alias in ("sleep", "monitor"):
        sec = _read_timeout_seconds(alias)
        _, _, cn = _TIMEOUT_ALIASES[alias]
        parts.append(f"{cn}：{_fmt_seconds(sec) if sec is not None else '读取失败'}")
    return True, "当前电源定时（" + "，".join(parts) + "）"


def set_timeout(kind: str | None = None, minutes: int | None = None, on_battery: bool | None = None) -> tuple[bool, str]:
    """设置睡眠/熄屏定时。kind: sleep=睡眠 / monitor=关闭显示；minutes 0=从不。"""
    if kind is None or str(kind).strip().lower() not in _TIMEOUT_ALIASES:
        return False, "kind 必须是 sleep（睡眠）或 monitor（关闭显示）"
    alias = str(kind).strip().lower()
    cn = _TIMEOUT_ALIASES[alias][2]
    m = _clamp_minutes(minutes if minutes is not None else -1)
    if m < 0:
        return False, f"minutes 必须是 0-{_TIMEOUT_MAX_MINUTES} 的数字（0=从不）"
    seconds = m * 60

    dc_mode = bool(on_battery)
    change_arg = f"{'standby' if alias == 'sleep' else 'monitor'}-timeout-{'dc' if dc_mode else 'ac'}"

    orig = _read_timeout_seconds(alias)
    try:
        _run_powercfg(["/change", change_arg, str(seconds)])
    except RuntimeError as e:
        return False, f"设置{cn}定时失败：{e}"

    # 读回验证（powercfg 应用有短暂延迟，轮询最多 ~3s）
    import time

    verified = False
    for _ in range(6):
        time.sleep(0.5)
        now = _read_timeout_seconds(alias)
        if now == seconds:
            verified = True
            break
    scope = "用电池时" if dc_mode else "接通电源时"
    if verified:
        return True, f"已把{cn}定时（{scope}）设为 {_fmt_seconds(seconds)}"

    # 失败分类：读回值区分「没写进去」与「被后台软件覆盖」
    now = _read_timeout_seconds(alias)
    if now == orig:
        hint = (
            f"{cn}定时（{scope}）设置未生效：读回值与设置前一致"
            f"（{_fmt_seconds(orig) if orig is not None else '未知'}），"
            "可能被组策略或安全软件锁定"
        )
    elif now is not None:
        hint = (
            f"{cn}定时（{scope}）写入被后台软件覆盖：目标 {_fmt_seconds(seconds)}，"
            f"实际落盘 {_fmt_seconds(now)}。检测到竞争写入（常见于主板厂商工具如"
            " ASUS DriverHub、显卡驱动配套软件的电源管理功能），"
            "请在那类软件里修改电源设置或退出后再试"
        )
    else:
        hint = f"{cn}定时（{scope}）设置后读取失败，无法确认是否生效"
    return False, hint
