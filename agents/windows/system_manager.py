"""SystemManager — 系统状态、硬件监控与系统级操作。"""

from __future__ import annotations

from ...tools import winops
from ...tools.execution_result import ExecutionResult, from_tuple


class SystemManager:
    """系统信息与电源/音量等系统级操作。"""

    def get_system_status(self) -> ExecutionResult:
        ok, msg = winops.get_system_status()
        return from_tuple(ok, msg, verified=ok, action="get_system_status")

    def check_system_health(self, *, auto_fix: bool = False) -> ExecutionResult:
        from ...tools.system_repair import check_system_health as health_audit

        report = health_audit(auto_fix=auto_fix)
        lines = [report.summary]
        for issue in report.issues:
            status = (
                "已修复"
                if issue.fixed
                else ("可修复" if issue.auto_fixable else "需人工")
            )
            lines.append(f"  [{issue.severity}] {issue.description} → {status}")
            if issue.fix_detail:
                lines.append(f"    详情: {issue.fix_detail}")
        return ExecutionResult(
            success=True,
            message="\n".join(lines),
            verified=True,
            action="check_system_health",
            target="",
            extra={"issue_count": len(report.issues), "auto_fix": auto_fix},
        )

    def repair_system(self) -> ExecutionResult:
        from ...tools.system_repair import repair_system

        ok, msg = repair_system()
        return from_tuple(ok, msg, verified=ok, action="repair_system")

    def clean_memory(self) -> ExecutionResult:
        from ...tools.system_cleanup import clean_memory

        ok, msg = clean_memory()
        return from_tuple(ok, msg, verified=ok, action="clean_memory")

    def clean_disk(
        self,
        *,
        items: list[str] | None = None,
        aggressive: bool = False,
    ) -> ExecutionResult:
        from ...tools.system_cleanup import clean_disk

        ok, msg = clean_disk(items=items, aggressive=aggressive)
        return from_tuple(ok, msg, verified=ok, action="clean_disk")

    def find_large_files(
        self,
        *,
        drive: str = "C",
        min_mb: int = 100,
        limit: int = 40,
        junk_only: bool = False,
    ) -> ExecutionResult:
        from ...tools.system_cleanup import find_large_files

        ok, msg = find_large_files(
            drive=drive,
            min_mb=min_mb,
            limit=limit,
            junk_only=junk_only,
        )
        return from_tuple(ok, msg, verified=ok, action="find_large_files", target=drive)

    def get_system_stats(self) -> ExecutionResult:
        from ...tools.system_cleanup import get_system_stats

        ok, msg = get_system_stats()
        return from_tuple(ok, msg, verified=ok, action="get_system_stats")

    def kill_memory_hogs(
        self,
        *,
        threshold_mb: int = 500,
        exclude: list[str] | None = None,
    ) -> ExecutionResult:
        from ...tools.system_cleanup import kill_memory_hogs

        ok, msg = kill_memory_hogs(threshold_mb=threshold_mb, exclude=exclude)
        return from_tuple(ok, msg, verified=ok, action="kill_memory_hogs")

    def get_hardware_info(self) -> ExecutionResult:
        from ...tools import hardware_monitor as hwmon

        ok, msg = hwmon.get_hardware_info()
        return from_tuple(ok, msg, verified=ok, action="get_hardware_info")

    def get_temperatures(self) -> ExecutionResult:
        from ...tools import hardware_monitor as hwmon

        ok, msg = hwmon.get_temperatures()
        return from_tuple(ok, msg, verified=ok, action="get_temperatures")

    def set_temp_threshold(
        self,
        *,
        cpu: float | None = None,
        gpu: float | None = None,
        ssd: float | None = None,
        other: float | None = None,
        alert_enabled: bool | None = None,
        interval_sec: int | None = None,
    ) -> ExecutionResult:
        from ...tools import hardware_monitor as hwmon

        ok, msg = hwmon.set_temp_threshold(
            cpu=cpu,
            gpu=gpu,
            ssd=ssd,
            other=other,
            alert_enabled=alert_enabled,
            interval_sec=interval_sec,
        )
        return from_tuple(ok, msg, verified=ok, action="set_temp_threshold")

    def get_temp_threshold(self) -> ExecutionResult:
        from ...tools import hardware_monitor as hwmon

        ok, msg = hwmon.get_temp_threshold()
        return from_tuple(ok, msg, verified=ok, action="get_temp_threshold")

    def setup_temp_sensors(self, *, download: bool = True) -> ExecutionResult:
        from ...tools import hardware_monitor as hwmon

        ok, msg = hwmon.setup_temp_sensors(download=download)
        return from_tuple(ok, msg, verified=ok, action="setup_temp_sensors")

    def system_power(self, action: str) -> ExecutionResult:
        raw = (action or "").strip()
        if not raw:
            return ExecutionResult(
                success=False,
                message="请指定电源操作",
                verified=False,
                action="system_power",
                target="",
            )
        ok, msg = winops.system_power(raw)
        return from_tuple(ok, msg, verified=ok, action="system_power", target=raw)

    def open_url(self, url: str) -> ExecutionResult:
        raw = (url or "").strip()
        if not raw:
            return ExecutionResult(
                success=False,
                message="请提供网址",
                verified=False,
                action="open_url",
                target="",
            )
        ok, msg = winops.open_url(raw)
        return from_tuple(ok, msg, verified=ok, action="open_url", target=raw[:80])

    def set_volume(self, level: int) -> ExecutionResult:
        ok, msg = winops.set_volume(int(level))
        return from_tuple(
            ok,
            msg,
            verified=ok,
            action="set_volume",
            target=str(level),
        )

    def list_monitors(self) -> ExecutionResult:
        from ...tools.display_ops import list_monitors

        ok, msg = list_monitors()
        return from_tuple(ok, msg, verified=ok, action="list_monitors")

    def get_brightness(self, monitor: str | None = None) -> ExecutionResult:
        from ...tools.display_ops import get_brightness

        raw = str(monitor or "").strip() or None
        ok, msg = get_brightness(raw)
        return from_tuple(ok, msg, verified=ok, action="get_brightness", target=raw or "")

    def set_brightness(self, level: int, monitor: str | None = None) -> ExecutionResult:
        from ...tools.display_ops import set_brightness

        raw = str(monitor or "").strip() or None
        ok, msg = set_brightness(level, raw)
        return from_tuple(ok, msg, verified=ok, action="set_brightness", target=raw or "")

    def get_wallpaper(self) -> ExecutionResult:
        from ...tools.display_ops import get_wallpaper

        ok, msg = get_wallpaper()
        return from_tuple(ok, msg, verified=ok, action="get_wallpaper")

    def set_wallpaper(self, image_path: str, style: str | None = None) -> ExecutionResult:
        from ...tools.display_ops import set_wallpaper

        raw_style = str(style or "").strip() or None
        ok, msg = set_wallpaper(image_path, raw_style)
        return from_tuple(
            ok, msg, verified=ok, action="set_wallpaper", target=str(image_path or "")
        )

    def list_power_plans(self) -> ExecutionResult:
        from ...tools.power_ops import list_power_plans

        ok, msg = list_power_plans()
        return from_tuple(ok, msg, verified=ok, action="list_power_plans")

    def set_power_plan(self, plan: str) -> ExecutionResult:
        from ...tools.power_ops import set_power_plan

        ok, msg = set_power_plan(str(plan or "").strip())
        return from_tuple(ok, msg, verified=ok, action="set_power_plan", target=str(plan or ""))

    def get_power_timeouts(self) -> ExecutionResult:
        from ...tools.power_ops import get_power_timeouts

        ok, msg = get_power_timeouts()
        return from_tuple(ok, msg, verified=ok, action="get_power_timeouts")

    def set_power_timeout(self, kind: str, minutes: int, on_battery: bool | None = None) -> ExecutionResult:
        from ...tools.power_ops import set_timeout

        ok, msg = set_timeout(str(kind or "").strip(), minutes, on_battery)
        return from_tuple(
            ok,
            msg,
            verified=ok,
            action="set_power_timeout",
            target=f"{kind}:{minutes}",
        )

    def check_updates(self) -> ExecutionResult:
        from ...tools.wupdate_ops import check_updates

        ok, msg = check_updates()
        return from_tuple(ok, msg, verified=ok, action="check_updates")

    def install_updates(self, update_filter: str | None = None) -> ExecutionResult:
        from ...tools.wupdate_ops import install_updates

        ok, msg = install_updates(str(update_filter or "").strip() or None)
        return from_tuple(
            ok, msg, verified=ok, action="install_updates", target=str(update_filter or "全部")
        )

    def get_update_history(self) -> ExecutionResult:
        from ...tools.wupdate_ops import get_update_history

        ok, msg = get_update_history()
        return from_tuple(ok, msg, verified=ok, action="get_update_history")

    def get_firewall_status(self) -> ExecutionResult:
        from ...tools.firewall_ops import get_firewall_status

        ok, msg = get_firewall_status()
        return from_tuple(ok, msg, verified=ok, action="get_firewall_status")

    def list_firewall_rules(self) -> ExecutionResult:
        from ...tools.firewall_ops import list_firewall_rules

        ok, msg = list_firewall_rules()
        return from_tuple(ok, msg, verified=ok, action="list_firewall_rules")

    def block_app_network(self, app: str, direction: str | None = None) -> ExecutionResult:
        from ...tools.firewall_ops import block_app_network

        ok, msg = block_app_network(str(app or "").strip(), str(direction or "").strip() or None)
        return from_tuple(ok, msg, verified=ok, action="block_app_network", target=str(app or ""))

    def allow_app_network(self, app: str) -> ExecutionResult:
        from ...tools.firewall_ops import allow_app_network

        ok, msg = allow_app_network(str(app or "").strip())
        return from_tuple(ok, msg, verified=ok, action="allow_app_network", target=str(app or ""))

    def list_app_volumes(self) -> ExecutionResult:
        from ...tools.audio_session_ops import list_app_volumes

        ok, msg = list_app_volumes()
        return from_tuple(ok, msg, verified=ok, action="list_app_volumes")

    def set_app_volume(self, app: str, level: int) -> ExecutionResult:
        from ...tools.audio_session_ops import set_app_volume

        raw = str(app or "").strip()
        if not raw:
            return ExecutionResult(
                success=False,
                message="请指定要调音量的应用名",
                verified=False,
                action="set_app_volume",
                target="",
            )
        ok, msg = set_app_volume(raw, level)
        return from_tuple(ok, msg, verified=ok, action="set_app_volume", target=raw)

    def mute_app(self, app: str, muted: bool = True) -> ExecutionResult:
        from ...tools.audio_session_ops import mute_app

        raw = str(app or "").strip()
        if not raw:
            return ExecutionResult(
                success=False,
                message="请指定要静音/取消静音的应用名",
                verified=False,
                action="mute_app",
                target="",
            )
        ok, msg = mute_app(raw, muted)
        return from_tuple(ok, msg, verified=ok, action="mute_app", target=raw)

    def control_music(self, action: str) -> ExecutionResult:
        raw = (action or "").strip()
        if not raw:
            return ExecutionResult(
                success=False,
                message="请指定音乐控制动作",
                verified=False,
                action="control_music",
                target="",
            )
        ok, msg = winops.control_music(raw)
        return from_tuple(ok, msg, verified=ok, action="control_music", target=raw)

    def get_current_time(self) -> ExecutionResult:
        from ...tools.system_ops import get_time_full

        ok, msg = get_time_full()
        return from_tuple(ok, msg, verified=ok, action="get_current_time")

    def get_weather(self, city: str) -> ExecutionResult:
        from ...tools.system_ops import get_weather

        raw = (city or "").strip()
        ok, msg = get_weather(raw)
        return from_tuple(ok, msg, verified=ok, action="get_weather", target=raw)

    def set_timer(self, seconds: int, message: str = "时间到") -> ExecutionResult:
        from ...tools.system_ops import set_timer

        sec = max(0, int(seconds))
        if sec < 1:
            return ExecutionResult(
                success=False,
                message="请指定倒计时秒数",
                verified=False,
                action="set_timer",
                target="",
            )
        ok, msg = set_timer(sec, message)
        return from_tuple(ok, msg, verified=ok, action="set_timer", target=str(sec))

    def take_screenshot(self) -> ExecutionResult:
        from ...tools.system_ops import take_screenshot

        ok, msg = take_screenshot()
        return from_tuple(ok, msg, verified=ok, action="take_screenshot")
