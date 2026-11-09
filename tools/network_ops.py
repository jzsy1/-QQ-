"""网络与 Wi-Fi 管理"""

from __future__ import annotations

import os
import tempfile
import xml.sax.saxutils

from .privilege import is_admin, is_system, run_shell_admin
from .winops import powershell


def get_network_info() -> tuple[bool, str]:
    script = """
Get-NetIPConfiguration -ErrorAction SilentlyContinue |
  Where-Object { $_.IPv4Address } |
  ForEach-Object {
    $alias = $_.InterfaceAlias
    $ip = ($_.IPv4Address | Select-Object -First 1).IPAddress
    $gwObj = $_.IPv4DefaultGateway | Select-Object -First 1
    $gw = if ($gwObj) { $gwObj.NextHop } else { '无' }
    $dnsArr = Get-DnsClientServerAddress -InterfaceAlias $alias -AddressFamily IPv4 -ErrorAction SilentlyContinue |
      Select-Object -ExpandProperty ServerAddresses
    $dns = if ($dnsArr) { ($dnsArr -join ', ') } else { '无' }
    Write-Output ("接口: " + $alias)
    Write-Output ("  IP: " + $ip)
    Write-Output ("  网关: " + $gw)
    Write-Output ("  DNS: " + $dns)
    Write-Output ""
  }
"""
    out = powershell(script, timeout=20)
    if not out or out.strip() == "":
        return False, "无法获取网络信息"
    if len(out) > 2500:
        out = out[:2500] + "\n…（已截断）"
    return True, out.strip()


def set_dns(interface_alias: str, primary: str, secondary: str = "", *, task_id: str | None = None, cancel_check=None) -> tuple[bool, str]:
    alias = (interface_alias or "").strip()
    primary = (primary or "").strip()
    secondary = (secondary or "").strip()
    if not alias or not primary:
        return False, "请提供网卡名称和主 DNS"

    safe_alias = alias.replace("'", "''")
    safe_pri = primary.replace("'", "''")
    if secondary:
        safe_sec = secondary.replace("'", "''")
        addr_expr = f"@('{safe_pri}','{safe_sec}')"
    else:
        addr_expr = f"@('{safe_pri}')"

    script = (
        f"Set-DnsClientServerAddress -InterfaceAlias '{safe_alias}' "
        f"-ServerAddresses {addr_expr} -ErrorAction Stop; Write-Output 'OK'"
    )
    out = powershell(script, timeout=20)
    if "OK" in (out or ""):
        dns = primary + (f", {secondary}" if secondary else "")
        return True, f"已将 {alias} 的 DNS 设为 {dns}"
    lower = (out or "").lower()
    if ("access is denied" in lower or "拒绝访问" in (out or "")) and not (
        is_admin() or is_system()
    ):
        ok, eout = run_shell_admin(script, timeout=40, task_id=task_id, cancel_check=cancel_check)
        if ok:
            dns = primary + (f", {secondary}" if secondary else "")
            return True, f"（管理员）已将 {alias} 的 DNS 设为 {dns}"
        return False, f"设置 DNS 需要管理员权限: {(eout or out or '').strip()[:160]}"
    if "access is denied" in lower or "拒绝访问" in (out or ""):
        return False, "设置 DNS 需要管理员权限"
    return False, f"设置 DNS 失败: {(out or '未知错误').strip()}"


def get_bluetooth_status() -> tuple[bool, str]:
    """查询本机蓝牙适配器的开关状态。"""
    script = """
Get-PnpDevice -Class Bluetooth -ErrorAction SilentlyContinue |
  Where-Object { $_.InstanceId -like 'BTHENUM*' -or $_.InstanceId -like 'BTHLEENUM*' -or $_.FriendlyName -match 'Bluetooth|蓝牙' } |
  Select-Object -First 6 FriendlyName, Status |
  ForEach-Object { Write-Output ("{0} | 状态 {1}" -f $_.FriendlyName, $_.Status) }
"""
    out = powershell(script, timeout=20)
    lines = [ln.strip() for ln in (out or "").splitlines() if ln.strip()]
    if not lines:
        # 有些机器蓝牙不在 BTHENUM 下：回退查适配器是否在设备里
        fallback = powershell(
            """
Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue |
  Where-Object { ($_.Class -eq 'Bluetooth') -or ($_.FriendlyName -match 'Bluetooth|蓝牙') } |
  Select-Object -ExpandProperty Status -First 1
""",
            timeout=20,
        )
        st = (fallback or "").strip()
        if st:
            return True, f"蓝牙状态：{st}"
        return True, "未检测到本机蓝牙适配器（可能硬件不支持或驱动未安装）"
    # 任一适配器 OK 即视为蓝牙开启
    if any("OK" in ln or "Started" in ln or "正常" in ln for ln in lines):
        return True, "蓝牙当前为开启状态。\n" + "\n".join(lines)
    if any("Unknown" in ln or "停止" in ln or "Disabled" in ln or "Error" in ln for ln in lines):
        return True, "蓝牙当前可能已关闭或不可用。\n" + "\n".join(lines)
    return True, "\n".join(lines)


def _bluetooth_pnp_ids() -> list[str]:
    """返回蓝牙适配器的 InstanceId 列表（用于开关）。"""
    out = powershell(
        """
Get-PnpDevice -Class Bluetooth -ErrorAction SilentlyContinue |
  Where-Object { $_.InstanceId -like 'BTHENUM*' -or $_.InstanceId -like 'BTHLEENUM*' } |
  Select-Object -ExpandProperty InstanceId -First 6
""",
        timeout=20,
    )
    return [ln.strip() for ln in (out or "").splitlines() if ln.strip()]


def set_bluetooth(on: bool | None = None) -> tuple[bool, str]:
    """开启/关闭本机蓝牙适配器（需要管理员权限）。"""
    ids = _bluetooth_pnp_ids()
    if not ids:
        return False, "未检测到可操作的蓝牙适配器（可能硬件不支持）"
    verb = "启用" if on else "禁用"
    cmd = "Enable-PnpDevice" if on else "Disable-PnpDevice"
    safe = ",".join(f"'{i.replace(chr(39), chr(39)*2)}'" for i in ids)
    script = f"""
$ids = @({safe})
$ok = $false
foreach ($id in $ids) {{
  try {{ {cmd} -InstanceId $id -Confirm:$false -ErrorAction Stop; $ok = $true }}
  catch {{}}
}}
if ($ok) {{ Write-Output 'DONE' }} else {{ Write-Output 'NO_OP'; exit 1 }}
"""
    out = powershell(script, timeout=30)
    if "DONE" in (out or ""):
        return True, f"已{verb}蓝牙适配器"
    return False, f"{verb}蓝牙失败，可能需要管理员权限：{(out or '未知错误').strip()[:160]}"


def list_wifi_networks() -> tuple[bool, str]:
    script = """
$out = netsh wlan show networks mode=bssid 2>&1 | Out-String
Write-Output $out.Trim()
"""
    out = powershell(script, timeout=25)
    if not out:
        return False, "无法扫描 Wi-Fi 网络"
    lines = []
    for ln in out.splitlines():
        s = ln.strip()
        if not s:
            continue
        if any(k in s for k in ("SSID", "信号", "Signal", "BSSID", "身份验证", "Authentication")):
            lines.append(s)
    if not lines:
        lines = [ln.strip() for ln in out.splitlines() if ln.strip()][:40]
    text = "\n".join(lines[:40])
    if len(text) > 2000:
        text = text[:2000] + "\n…（已截断）"
    return True, text or out[:2000]


def list_wifi_profiles() -> tuple[bool, str]:
    script = """
netsh wlan show profiles 2>&1 |
  Select-String 'All User Profile|所有用户配置文件' |
  ForEach-Object { ($_.Line -split ':', 2)[1].Trim() }
"""
    out = powershell(script, timeout=15)
    lines = [ln.strip() for ln in (out or "").splitlines() if ln.strip()]
    if not lines:
        return True, "未找到已保存的 Wi-Fi 配置文件"
    return True, "已保存 Wi-Fi：\n" + "\n".join(lines)


def connect_wifi(ssid: str, password: str = "") -> tuple[bool, str]:
    ssid = (ssid or "").strip()
    if not ssid:
        return False, "请提供 Wi-Fi 名称（SSID）"

    safe_ssid = ssid.replace("'", "''")
    check = powershell(
        f"netsh wlan show profile name='{safe_ssid}' 2>&1 | Out-String",
        timeout=10,
    )
    # 检查 wlan 服务是否可用（服务未运行时所有 wlan 命令都会失败）
    lower_check = (check or "").lower()
    if "not running" in lower_check or "wlansvc" in lower_check or "服务没有运行" in (check or ""):
        return False, "Wi-Fi 服务（wlansvc）未运行，无法操作 Wi-Fi"
    profile_exists = bool(
        check
        and "找不到" not in check
        and "not found" not in lower_check
    )

    if not profile_exists:
        pwd = (password or "").strip()
        if not pwd:
            return False, f"未找到「{ssid}」的配置文件，请提供密码以创建"
        esc_ssid = xml.sax.saxutils.escape(ssid)
        esc_pwd = xml.sax.saxutils.escape(pwd)
        profile_xml = f"""<?xml version="1.0"?>
<WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">
  <name>{esc_ssid}</name>
  <SSIDConfig><SSID><name>{esc_ssid}</name></SSID></SSIDConfig>
  <connectionType>ESS</connectionType>
  <connectionMode>auto</connectionMode>
  <MSM><security>
    <authEncryption><authentication>WPA2PSK</authentication><encryption>AES</encryption></authEncryption>
    <sharedKey><keyType>passPhrase</keyType><protected>false</protected><keyMaterial>{esc_pwd}</keyMaterial></sharedKey>
  </security></MSM>
</WLANProfile>"""
        tmp_path = ""
        try:
            fd, tmp_path = tempfile.mkstemp(suffix=".xml", prefix="kekesi_wifi_")
            os.close(fd)
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(profile_xml)
            add_out = powershell(
                f'netsh wlan add profile filename="{tmp_path}" user=all 2>&1 | Out-String',
                timeout=15,
            )
            if add_out and ("error" in add_out.lower() or "错误" in add_out):
                return False, f"创建 Wi-Fi 配置失败: {add_out.strip()[:300]}"
        finally:
            if tmp_path:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    connect_out = powershell(
        f"netsh wlan connect name='{safe_ssid}' ssid='{safe_ssid}' 2>&1 | Out-String",
        timeout=20,
    )
    lower = (connect_out or "").lower()
    if "已成功完成连接请求" in connect_out or "connection request was completed successfully" in lower:
        return True, f"已连接 Wi-Fi: {ssid}"
    if "已连接" in connect_out or "connected" in lower:
        return True, f"已连接 Wi-Fi: {ssid}"
    return False, f"连接失败: {(connect_out or '未知错误').strip()[:300]}"
