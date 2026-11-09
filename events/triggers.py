"""事件触发器 — 5 类条件的被动检测。

规范 PART IX：
    Scheduler：定时任务、系统事件、文件事件、应用事件、网络事件。

设计原则：
1. 零外部依赖（不引入 psutil/watchdog），用标准库 + 短 PowerShell 调用。
2. 每个触发器 check 失败 fail-soft，返回 (False, reason)，绝不抛出。
3. 触发器是无状态的检测器，状态由 Scheduler / EventRule 维护。
4. 所有阈值/路径/间隔从 rule.condition 读取，零硬编码。

触发器类型：
    scheduled  — 定时：one-shot / interval / daily
    system     — 系统：CPU/内存/磁盘阈值、电源状态
    file       — 文件：存在性、mtime/size 变化
    app        — 应用：进程运行中/未运行
    network    — 网络：主机可达、IP 变化、WiFi 连接

check 返回值约定：
    (True,  None)         — 条件满足，应触发动作
    (False, "reason...")  — 条件不满足或检测失败，不触发
"""

from __future__ import annotations

import logging
import os
import socket
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from .engine import EventRule

logger = logging.getLogger(__name__)

# PowerShell 调用的统一超时（秒），避免触发器卡死调度线程
_PS_TIMEOUT_DEFAULT = 8


def _run_powershell(script: str, timeout_sec: int = _PS_TIMEOUT_DEFAULT) -> str:
    """运行 PowerShell 短脚本，返回 stdout（失败返回空字符串）。"""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=int(max(1, timeout_sec)),
            encoding="utf-8",
            errors="replace",
        )
        return (result.stdout or "").strip()
    except Exception:
        logger.debug("powershell failed", exc_info=True)
        return ""


# ================================================================
# 触发器基类
# ================================================================


class Trigger:
    """触发器基类。子类实现 check()。"""

    event_type: str = ""

    def check(self, rule: EventRule, ctx: dict) -> tuple[bool, str | None]:
        raise NotImplementedError


# ================================================================
# 1. 定时触发器
# ================================================================


class ScheduledTrigger(Trigger):
    """定时触发器。

    condition 支持：
        mode: "one_shot" | "interval" | "daily"
        at: ISO 时间串（one_shot），如 "2026-08-12T18:30:00"
        interval_sec: 间隔秒数（interval），如 600
        time: "HH:MM" 每日触发时间（daily），如 "09:00"
        align_to_midnight: bool（interval 是否对齐到整点/半小时）

    触发后：
        one_shot 触发一次后由 Scheduler 自动 disable
        interval 触发后更新 next_run = now + interval_sec
        daily 触发后更新 next_run = 明日同一时间
    """

    event_type = "scheduled"

    def check(self, rule: EventRule, ctx: dict) -> tuple[bool, str | None]:
        cond = rule.condition or {}
        mode = str(cond.get("mode") or "one_shot").strip().lower()
        now = datetime.now()

        # next_run 未设置 → 根据模式初始化
        if not rule.next_run:
            next_run = self._compute_initial_next_run(cond, mode, now)
            if next_run is None:
                return False, "无法计算初始 next_run"
            rule.next_run = next_run.isoformat()
            return False, f"已初始化 next_run={rule.next_run}"

        try:
            due = datetime.fromisoformat(rule.next_run)
        except (ValueError, TypeError):
            return False, f"next_run 格式无效: {rule.next_run}"

        if now < due:
            return False, "未到期"

        # 到期，触发
        return True, None

    @staticmethod
    def _compute_initial_next_run(
        cond: dict, mode: str, now: datetime
    ) -> datetime | None:
        if mode == "interval":
            interval_sec = float(cond.get("interval_sec") or 60)
            return now + timedelta(seconds=interval_sec)

        if mode == "daily":
            time_str = str(cond.get("time") or "09:00").strip()
            try:
                hh, mm = time_str.split(":", 1)
                target = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
                if target <= now:
                    target += timedelta(days=1)
                return target
            except Exception:
                return None

        # one_shot
        at_str = str(cond.get("at") or "").strip()
        if not at_str:
            return None
        try:
            return datetime.fromisoformat(at_str)
        except ValueError:
            return None

    @staticmethod
    def advance_after_fire(rule: EventRule) -> None:
        """触发后更新 next_run（由 Scheduler 调用）。"""
        cond = rule.condition or {}
        mode = str(cond.get("mode") or "one_shot").strip().lower()
        now = datetime.now()

        if mode == "interval":
            interval_sec = float(cond.get("interval_sec") or 60)
            rule.next_run = (now + timedelta(seconds=interval_sec)).isoformat()
        elif mode == "daily":
            time_str = str(cond.get("time") or "09:00").strip()
            try:
                hh, mm = time_str.split(":", 1)
                target = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
                target += timedelta(days=1)
                rule.next_run = target.isoformat()
            except Exception:
                rule.enabled = False
        else:
            # one_shot：触发后禁用
            rule.enabled = False


# ================================================================
# 2. 系统事件触发器
# ================================================================


class SystemTrigger(Trigger):
    """系统事件触发器。

    condition.metric 支持：
        cpu_high       — CPU 使用率超过阈值
        memory_high    — 内存使用率超过阈值
        disk_low       — 磁盘剩余空间低于阈值（GB 或 %）
        on_battery     — 当前使用电池供电
        on_ac          — 当前使用交流电源

    condition.threshold:
        cpu_high/memory_high: 百分比 0-100
        disk_low: GB 数值或百分比（带 % 后缀）
    condition.drive: 盘符（disk_low），默认 C:
    """

    event_type = "system"

    # 缓存最近一次系统查询结果，避免短轮询内重复 PowerShell 调用
    _cache_ttl_sec = 5.0

    def check(self, rule: EventRule, ctx: dict) -> tuple[bool, str | None]:
        cond = rule.condition or {}
        metric = str(cond.get("metric") or "").strip().lower()
        if not metric:
            return False, "缺少 metric"

        snapshot = self._snapshot_system(ctx)
        if not snapshot:
            return False, "系统状态查询失败"

        threshold = cond.get("threshold")

        if metric == "cpu_high":
            cpu = snapshot.get("cpu_percent")
            if cpu is None:
                return False, "无 CPU 数据"
            thr = float(threshold) if threshold is not None else 80.0
            return (cpu >= thr, f"cpu={cpu:.1f}% thr={thr}")

        if metric == "memory_high":
            mem = snapshot.get("memory_percent")
            if mem is None:
                return False, "无内存数据"
            thr = float(threshold) if threshold is not None else 85.0
            return (mem >= thr, f"mem={mem:.1f}% thr={thr}")

        if metric == "disk_low":
            drive = str(cond.get("drive") or "C:").strip()
            info = snapshot.get("disks", {}).get(drive.upper())
            if not info:
                return False, f"无磁盘数据 {drive}"
            thr_raw = str(threshold) if threshold is not None else "10%"
            if thr_raw.endswith("%"):
                thr = float(thr_raw[:-1])
                free_percent = info.get("free_percent", 100)
                return (free_percent <= thr, f"{drive} free={free_percent:.1f}% thr={thr}%")
            else:
                thr_gb = float(thr_raw)
                free_gb = info.get("free_gb", 0)
                return (free_gb <= thr_gb, f"{drive} free={free_gb:.1f}GB thr={thr_gb}GB")

        if metric == "on_battery":
            return (snapshot.get("on_battery") is True, f"on_battery={snapshot.get('on_battery')}")

        if metric == "on_ac":
            return (snapshot.get("on_battery") is False, f"on_battery={snapshot.get('on_battery')}")

        return False, f"未知 metric: {metric}"

    @classmethod
    def _snapshot_system(cls, ctx: dict) -> dict:
        """查询系统状态，带 5 秒 TTL 缓存。"""
        cache_key = "_system_snapshot"
        cached = ctx.get(cache_key)
        now_ts = time.time()
        if cached and (now_ts - cached.get("ts", 0)) < cls._cache_ttl_sec:
            return cached.get("data", {})

        # 一次性 PowerShell 拿 CPU/内存/磁盘/电源
        script = """
$cpu = (Get-CimInstance Win32_Processor | Select-Object -First 1).LoadPercentage
$os = Get-CimInstance Win32_OperatingSystem
$memTotal = $os.TotalVisibleMemorySize
$memFree = $os.FreePhysicalMemory
$memUsedPct = if ($memTotal -gt 0) { [math]::Round(($memTotal - $memFree) / $memTotal * 100, 1) } else { 0 }
$disks = @{}
Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3' | ForEach-Object {
    $drive = $_.DeviceID
    $totalGB = [math]::Round($_.Size / 1GB, 1)
    $freeGB = [math]::Round($_.FreeSpace / 1GB, 1)
    $freePct = if ($totalGB -gt 0) { [math]::Round($freeGB / $totalGB * 100, 1) } else { 0 }
    $disks[$drive] = @{ total_gb = $totalGB; free_gb = $freeGB; free_percent = $freePct }
}
$bat = Get-CimInstance -ClassName Win32_Battery -ErrorAction SilentlyContinue
$onBattery = if ($bat -and $bat.BatteryStatus -eq 1) { $true } else { $false }
@{
    cpu_percent = $cpu
    memory_percent = $memUsedPct
    disks = $disks
    on_battery = $onBattery
} | ConvertTo-Json -Compress
"""
        out = _run_powershell(script)
        if not out:
            ctx[cache_key] = {"ts": now_ts, "data": {}}
            return {}

        try:
            import json
            data = json.loads(out)
            # 磁盘 key 标准化为大写
            disks_raw = data.get("disks") or {}
            disks = {}
            if isinstance(disks_raw, dict):
                for k, v in disks_raw.items():
                    disks[str(k).upper()] = v
            data["disks"] = disks
            ctx[cache_key] = {"ts": now_ts, "data": data}
            return data
        except Exception:
            logger.debug("system snapshot parse failed", exc_info=True)
            ctx[cache_key] = {"ts": now_ts, "data": {}}
            return {}


# ================================================================
# 3. 文件事件触发器
# ================================================================


class FileTrigger(Trigger):
    """文件事件触发器。

    condition.event 支持：
        exists       — 文件/目录出现（之前不存在，现在存在）
        not_exists   — 文件/目录消失（之前存在，现在不存在）
        modified     — mtime 变化
        size_changed — size 变化

    condition.path: 目标路径（必填）
    """

    event_type = "file"

    def check(self, rule: EventRule, ctx: dict) -> tuple[bool, str | None]:
        cond = rule.condition or {}
        event = str(cond.get("event") or "exists").strip().lower()
        path_str = str(cond.get("path") or "").strip()
        if not path_str:
            return False, "缺少 path"
        path = Path(path_str)
        state_key = f"_file_state:{path_str}"
        prev = ctx.get(state_key) or {}

        try:
            exists = path.exists()
            stat = path.stat() if exists else None
        except Exception:
            return False, f"路径访问失败: {path_str}"

        current = {
            "exists": exists,
            "mtime": stat.st_mtime if stat else None,
            "size": stat.st_size if stat else None,
        }
        ctx[state_key] = current

        try:
            if event == "exists":
                # 边沿触发：之前不存在，现在存在；首轮仅建基线不触发
                has_prev = prev.get("exists") is not None
                fired = has_prev and exists and not prev.get("exists")
                return (fired, None if fired else f"exists={exists} prev={prev.get('exists')}")

            if event == "not_exists":
                has_prev = prev.get("exists") is not None
                fired = has_prev and (not exists) and prev.get("exists")
                return (fired, None if fired else f"exists={exists} prev={prev.get('exists')}")

            if event == "modified":
                if not exists:
                    return False, "文件不存在"
                fired = (
                    prev.get("exists")
                    and prev.get("mtime") is not None
                    and current["mtime"] != prev.get("mtime")
                )
                return (fired, None if fired else "mtime 未变化")

            if event == "size_changed":
                if not exists:
                    return False, "文件不存在"
                fired = (
                    prev.get("exists")
                    and current["size"] != prev.get("size")
                )
                return (fired, None if fired else "size 未变化")

            return False, f"未知 event: {event}"
        finally:
            # 检测后状态已写入 ctx，下一次 check 会用新 prev
            pass


# ================================================================
# 4. 应用事件触发器
# ================================================================


class AppTrigger(Trigger):
    """应用事件触发器。

    condition.event 支持：
        running     — 进程正在运行（持续触发）
        not_running — 进程未运行（持续触发）
        started     — 进程刚启动（边沿：之前不存在，现在存在）
        stopped     — 进程刚退出（边沿：之前存在，现在不存在）

    condition.process: 进程名（不含扩展名），如 "notepad"、"WeChat"
    """

    event_type = "app"

    def check(self, rule: EventRule, ctx: dict) -> tuple[bool, str | None]:
        cond = rule.condition or {}
        event = str(cond.get("event") or "running").strip().lower()
        process_name = str(cond.get("process") or "").strip()
        if not process_name:
            return False, "缺少 process"

        state_key = f"_app_state:{process_name.lower()}"
        prev_running = ctx.get(state_key, {}).get("running")

        running = self._is_process_running(process_name)
        ctx[state_key] = {"running": running, "ts": time.time()}

        if event == "running":
            return (running, None if running else "未运行")

        if event == "not_running":
            return ((not running), None if not running else "运行中")

        if event == "started":
            fired = running and (prev_running is False)
            return (fired, None if fired else f"running={running} prev={prev_running}")

        if event == "stopped":
            fired = (not running) and (prev_running is True)
            return (fired, None if fired else f"running={running} prev={prev_running}")

        return False, f"未知 event: {event}"

    @staticmethod
    def _is_process_running(process_name: str) -> bool:
        """用 tasklist 检测进程是否存在（无需 psutil）。"""
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {process_name}*", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                timeout=5,
                encoding="utf-8",
                errors="replace",
            )
            out = (result.stdout or "").strip()
            if not out:
                return False
            # tasklist 找不到时返回 "信息: 没有运行的任务..."
            return not out.startswith("信息") and process_name.lower() in out.lower()
        except Exception:
            logger.debug("tasklist failed for %s", process_name, exc_info=True)
            return False


# ================================================================
# 5. 网络事件触发器
# ================================================================


class NetworkTrigger(Trigger):
    """网络事件触发器。

    condition.event 支持：
        host_reachable    — 主机可达（持续触发）
        host_unreachable  — 主机不可达（持续触发）
        ip_changed        — 本机 IP 变化（边沿）
        wifi_connected    — WiFi 已连接（持续）
        wifi_disconnected — WiFi 未连接（持续）

    condition.host: 目标主机（host_reachable/unreachable），如 "192.168.0.1"
    condition.port: 端口（默认 80）
    condition.timeout_sec: socket 超时（默认 2）
    """

    event_type = "network"

    def check(self, rule: EventRule, ctx: dict) -> tuple[bool, str | None]:
        cond = rule.condition or {}
        event = str(cond.get("event") or "host_reachable").strip().lower()

        if event in ("host_reachable", "host_unreachable"):
            host = str(cond.get("host") or "").strip()
            if not host:
                return False, "缺少 host"
            port = int(cond.get("port") or 80)
            timeout = float(cond.get("timeout_sec") or 2)
            reachable = self._check_host(host, port, timeout)
            if event == "host_reachable":
                return (reachable, None if reachable else "不可达")
            return ((not reachable), None if not reachable else "可达")

        if event == "ip_changed":
            state_key = "_net_ip_state"
            prev_ip = ctx.get(state_key)
            current_ip = self._get_local_ip()
            ctx[state_key] = current_ip
            if prev_ip is None:
                return False, f"初始 IP={current_ip}"
            fired = current_ip != prev_ip
            return (fired, None if fired else f"IP 未变化: {current_ip}")

        if event in ("wifi_connected", "wifi_disconnected"):
            connected = self._is_wifi_connected()
            if event == "wifi_connected":
                return (connected, None if connected else "WiFi 未连接")
            return ((not connected), None if not connected else "WiFi 已连接")

        return False, f"未知 event: {event}"

    @staticmethod
    def _check_host(host: str, port: int, timeout: float) -> bool:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except Exception:
            return False

    @staticmethod
    def _get_local_ip() -> str:
        """获取本机出网 IP（用于变化检测）。"""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(2)
            try:
                s.connect(("8.8.8.8", 80))
                return s.getsockname()[0]
            finally:
                s.close()
        except Exception:
            return "0.0.0.0"

    @staticmethod
    def _is_wifi_connected() -> bool:
        """检测 WiFi 是否已连接（有 SSID 关联）。"""
        out = _run_powershell(
            "(Get-NetConnectionProfile -ErrorAction SilentlyContinue | "
            "Where-Object { $_.InterfaceAlias -match 'Wi-Fi|WLAN|无线' } | "
            "Select-Object -First 1 -ExpandProperty Name) -ne $null"
        )
        return out.strip().lower() in ("true", "1", "yes")


# ================================================================
# 触发器注册表
# ================================================================


_BUILTIN_TRIGGERS: dict[str, Trigger] = {
    "scheduled": ScheduledTrigger(),
    "system": SystemTrigger(),
    "file": FileTrigger(),
    "app": AppTrigger(),
    "network": NetworkTrigger(),
}


def get_trigger(event_type: str) -> Trigger | None:
    """按 event_type 获取内置触发器实例。"""
    return _BUILTIN_TRIGGERS.get((event_type or "").strip().lower())


def list_trigger_types() -> list[str]:
    """列出所有内置触发器类型。"""
    return sorted(_BUILTIN_TRIGGERS.keys())
