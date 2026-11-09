"""NetworkManager — WiFi / DNS / IP 管理。"""

from __future__ import annotations

from ...tools.execution_result import ExecutionResult, from_tuple
from ...tools import network_ops


class NetworkManager:
    """网络操作封装。"""

    def get_ip(self) -> ExecutionResult:
        ok, msg = network_ops.get_network_info()
        return from_tuple(ok, msg, verified=ok, action="get_network_info")

    def list_wifi(self) -> ExecutionResult:
        ok, msg = network_ops.list_wifi_networks()
        return from_tuple(ok, msg, verified=ok, action="list_wifi_networks")

    def list_profiles(self) -> ExecutionResult:
        ok, msg = network_ops.list_wifi_profiles()
        return from_tuple(ok, msg, verified=ok, action="list_wifi_profiles")

    def connect_wifi(self, ssid: str, password: str = "") -> ExecutionResult:
        ok, msg = network_ops.connect_wifi(ssid, password)
        return from_tuple(ok, msg, verified=ok, action="connect_wifi", target=ssid)

    def disconnect_wifi(self, ssid: str = "") -> ExecutionResult:
        from ...tools.winops import powershell

        safe = (ssid or "").strip().replace("'", "''")
        if safe:
            cmd = f"netsh wlan disconnect interface=* 2>&1; netsh wlan connect name='{safe}' 2>&1"
        else:
            cmd = "netsh wlan disconnect 2>&1"
        out = powershell(cmd, timeout=15)
        ok = bool(out) and "error" not in (out or "").lower()
        return from_tuple(ok, out or "已断开", verified=ok, action="disconnect_wifi", target=ssid)

    def set_dns(self, interface: str, primary: str, secondary: str = "", *, task_id: str | None = None, cancel_check=None) -> ExecutionResult:
        ok, msg = network_ops.set_dns(interface, primary, secondary, task_id=task_id, cancel_check=cancel_check)
        return from_tuple(ok, msg, verified=ok, action="set_dns", target=interface)

    def get_network_info(self) -> ExecutionResult:
        return self.get_ip()

    def bluetooth_status(self) -> ExecutionResult:
        ok, msg = network_ops.get_bluetooth_status()
        return from_tuple(ok, msg, verified=ok, action="get_bluetooth_status")

    def set_bluetooth(self, on: bool) -> ExecutionResult:
        ok, msg = network_ops.set_bluetooth(on=on)
        return from_tuple(ok, msg, verified=ok, action="set_bluetooth",
                          target="on" if on else "off")
