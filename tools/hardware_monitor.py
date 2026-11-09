"""硬件信息与温度监控 — 科科斯体内传感器服务。

体内宿主 kekesi_hw_sensor（无 GUI / 无桌面窗）通过驱动级库读取 CPU/GPU/主板/磁盘温度；
本模块经本机 HTTP 取数。完整 CPU/主板读数首次可能需一次 UAC（仍无窗口）。

兜底：Windows PDH 热区、ACPI、磁盘可靠性计数、nvidia-smi。

阈值：data/hardware/thresholds.json；报警：ThermalGuardian → notify_reminder。
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_DEFAULT_THRESHOLDS: dict[str, Any] = {
    "cpu": 85.0,
    "gpu": 85.0,
    "ssd": 70.0,
    "other": 80.0,
    "alert_enabled": False,
    "interval_sec": 30,
    "cooldown_sec": 300,
    "hysteresis_c": 5.0,
}


def _data_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "data" / "hardware"


def _thresholds_path() -> Path:
    return _data_dir() / "thresholds.json"


def _powershell(script: str, timeout: float = 20.0) -> str:
    from .winops import powershell

    return powershell(script, timeout=timeout)


# ── 阈值 ─────────────────────────────────────────────────────


def load_thresholds() -> dict[str, Any]:
    path = _thresholds_path()
    data = dict(_DEFAULT_THRESHOLDS)
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data.update(raw)
        except Exception:
            logger.debug("read thresholds failed", exc_info=True)
    return data


def save_thresholds(data: dict[str, Any]) -> None:
    d = _data_dir()
    d.mkdir(parents=True, exist_ok=True)
    merged = dict(_DEFAULT_THRESHOLDS)
    merged.update(data)
    _thresholds_path().write_text(
        json.dumps(merged, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def set_temp_threshold(
    *,
    cpu: float | None = None,
    gpu: float | None = None,
    ssd: float | None = None,
    other: float | None = None,
    alert_enabled: bool | None = None,
    interval_sec: int | None = None,
) -> tuple[bool, str]:
    """设定温度阈值；alert_enabled=True 时启动自动报警。"""
    cur = load_thresholds()
    for key, val in (("cpu", cpu), ("gpu", gpu), ("ssd", ssd), ("other", other)):
        if val is None:
            continue
        v = float(val)
        if v < 40 or v > 110:
            return False, f"{key} 阈值须在 40–110℃ 之间"
        cur[key] = v
    if alert_enabled is not None:
        cur["alert_enabled"] = bool(alert_enabled)
    if interval_sec is not None:
        cur["interval_sec"] = max(15, int(interval_sec))
    save_thresholds(cur)
    ensure_guardian()
    if cur.get("alert_enabled"):
        g = get_guardian()
        if g:
            g.kick()
    lines = [
        f"CPU {cur['cpu']:g}℃",
        f"GPU {cur['gpu']:g}℃",
        f"固态/硬盘 {cur['ssd']:g}℃",
        f"其它 {cur['other']:g}℃",
        f"自动报警={'开' if cur.get('alert_enabled') else '关'}",
        f"巡检间隔 {cur.get('interval_sec', 30)} 秒",
    ]
    return True, "温度阈值已更新：" + "，".join(lines)


def get_temp_threshold() -> tuple[bool, str]:
    cur = load_thresholds()
    lines = [
        f"CPU ≥ {cur['cpu']:g}℃",
        f"GPU ≥ {cur['gpu']:g}℃",
        f"固态/硬盘 ≥ {cur['ssd']:g}℃",
        f"其它 ≥ {cur['other']:g}℃",
        f"自动报警：{'已开启' if cur.get('alert_enabled') else '未开启'}",
        f"巡检间隔：{cur.get('interval_sec', 30)} 秒",
        f"报警冷却：{cur.get('cooldown_sec', 300)} 秒（同一传感器）",
    ]
    return True, "当前温度阈值：\n" + "\n".join(f"- {x}" for x in lines)


# ── 采集 ─────────────────────────────────────────────────────


def _classify_sensor(name: str) -> str:
    n = (name or "").lower()
    if any(k in n for k in ("nvme", "ssd", "hdd", "disk", "drive", "storage", "固态", "硬盘")):
        return "ssd"
    if any(
        k in n
        for k in (
            "gpu",
            "radeon",
            "nvidia",
            "geforce",
            "rtx",
            "rx ",
            "hot spot",
            "hotspot",
            "vram",
            "edge",
            "junction",
        )
    ):
        return "gpu"
    if any(
        k in n
        for k in (
            "cpu",
            "package",
            "core #",
            "core#",
            "tctl",
            "tdie",
            "ccd",
            "intel",
            "amd ryzen",
            "processor",
        )
    ):
        return "cpu"
    return "other"


def _kelvin_to_c(k: float) -> float | None:
    # PDH Thermal Zone 常给开尔文；High Precision 常为十分之一开尔文
    if k > 2000:  # e.g. 3010
        c = k / 10.0 - 273.15
    elif k > 200:  # e.g. 301
        c = k - 273.15
    else:
        c = k  # already celsius
    if c < -30 or c > 150:
        return None
    return round(c, 1)


# ── 科科斯体内传感器宿主（无窗口） ────────────────────────────

_SENSOR_PORT = 18765
_SENSOR_HOST = "127.0.0.1"


def _sensor_host_dir() -> Path:
    return _data_dir() / "sensor_host"


def _sensor_exe() -> Path | None:
    p = _sensor_host_dir() / "kekesi_hw_sensor.exe"
    return p if p.is_file() else None


def _sensor_request(path: str, timeout: float = 4.0) -> dict[str, Any] | None:
    from urllib.error import URLError
    from urllib.request import Request, urlopen

    url = f"http://{_SENSOR_HOST}:{_SENSOR_PORT}{path}"
    try:
        req = Request(url, headers={"Accept": "application/json", "User-Agent": "kekesi"})
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except (URLError, TimeoutError, OSError, json.JSONDecodeError):
        return None
    except Exception:
        return None


def _sensor_host_alive() -> bool:
    data = _sensor_request("/health", timeout=1.5)
    return bool(data and data.get("ok"))


def _spawn_sensor_host(*, elevated: bool) -> tuple[bool, str]:
    exe = _sensor_exe()
    if exe is None:
        return False, "体内传感器宿主未编译（缺少 kekesi_hw_sensor.exe）"
    cwd = str(exe.parent)
    if not elevated:
        try:
            log_path = _data_dir() / "sensor_host.log"
            log_f = open(log_path, "a", encoding="utf-8")  # noqa: SIM115
            subprocess.Popen(
                [str(exe)],
                cwd=cwd,
                stdout=log_f,
                stderr=subprocess.STDOUT,
                creationflags=_NO_WINDOW,
            )
            return True, "ok"
        except OSError as e:
            if getattr(e, "winerror", None) == 740:
                return False, "need_elevation"
            return False, str(e)
        except Exception as e:
            return False, str(e)

    import ctypes

    # SW_HIDE=0：提权启动也不弹窗
    rc = int(ctypes.windll.shell32.ShellExecuteW(None, "runas", str(exe), None, cwd, 0))
    if rc <= 32:
        return False, f"UAC 启动失败（{rc}），可能取消了提权"
    return True, "elevated"


def ensure_sensor_host(*, prefer_elevated: bool = True) -> tuple[bool, str]:
    """确保体内传感器宿主运行（无桌面窗口）。完整 CPU 读数通常需一次管理员同意。"""
    if _sensor_host_alive():
        data = _sensor_request("/temps", timeout=6.0) or {}
        n = int(data.get("count") or 0)
        if n > 0 or not prefer_elevated:
            return True, f"体内传感器已就绪（{n} 点）"
        # 已在跑但几乎无传感器：可能未提权，尝试重启为提权
        try:
            _sensor_request_close()
        except Exception:
            pass
        time.sleep(0.6)

    if _sensor_exe() is None:
        return (
            False,
            "体内传感器宿主不存在。请先编译 kekesi/hardware_sensor_host 发布到 data/hardware/sensor_host",
        )

    # 先普通启动（无窗）；不够再提权无窗
    ok, detail = _spawn_sensor_host(elevated=False)
    if detail == "need_elevation" or prefer_elevated:
        # 等一下看普通模式能否起来
        if ok:
            deadline = time.time() + 6
            while time.time() < deadline:
                if _sensor_host_alive():
                    break
                time.sleep(0.4)
            data = _sensor_request("/temps", timeout=5.0) or {}
            n = int(data.get("count") or 0)
            # GPU 通常非提权也能读；CPU 封装/主板多半要管理员
            if n >= 8:
                return True, f"体内传感器已启动（{n} 点）"
            try:
                _sensor_request_close()
            except Exception:
                pass
            time.sleep(0.5)
        ok, detail = _spawn_sensor_host(elevated=True)
        if not ok:
            return False, detail

    deadline = time.time() + 35
    while time.time() < deadline:
        if _sensor_host_alive():
            data = _sensor_request("/temps", timeout=6.0) or {}
            n = int(data.get("count") or 0)
            return True, f"体内传感器已启动（无窗口，{n} 点）"
        time.sleep(0.5)
    return False, "体内传感器启动超时（若弹出 UAC 请点「是」）"


def _sensor_request_close() -> None:
    from urllib.request import Request, urlopen

    url = f"http://{_SENSOR_HOST}:{_SENSOR_PORT}/close"
    try:
        req = Request(url, data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(req, timeout=2) as resp:
            resp.read()
    except Exception:
        # 强杀兜底
        subprocess.run(
            ["taskkill", "/F", "/IM", "kekesi_hw_sensor.exe"],
            capture_output=True,
            creationflags=_NO_WINDOW,
        )


def _kind_from_hw_type(hw_type: str, name: str) -> str:
    t = (hw_type or "").lower()
    if t == "cpu":
        return "cpu"
    if t.startswith("gpu"):
        return "gpu"
    if t == "storage":
        return "ssd"
    return _classify_sensor(f"{hw_type} {name}")


def _is_useful_temp_name(name: str) -> bool:
    n = (name or "").lower()
    if "distance to tjmax" in n:
        return False
    if "warning temperature" in n or "critical temperature" in n:
        return False
    return True


def _temps_from_inner_sensor() -> list[dict[str, Any]]:
    data = _sensor_request("/temps", timeout=8.0)
    if not data or not data.get("ok"):
        return []
    out: list[dict[str, Any]] = []
    for item in data.get("sensors") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        if not _is_useful_temp_name(name):
            continue
        try:
            c = float(item.get("celsius"))
        except (TypeError, ValueError):
            continue
        if c < -20 or c > 120:
            continue
        hw = str(item.get("hardware") or "")
        hw_type = str(item.get("hardwareType") or "")
        kind = _kind_from_hw_type(hw_type, name)
        label = f"{hw} · {name}" if hw else name
        out.append(
            {
                "name": label,
                "celsius": round(c, 1),
                "kind": kind,
                "source": "inner_sensor",
            }
        )
    return out


def _temps_from_nvidia() -> list[dict[str, Any]]:
    try:
        r = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,temperature.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
            creationflags=_NO_WINDOW,
        )
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for line in (r.stdout or "").splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2:
            continue
        try:
            c = float(parts[1])
        except ValueError:
            continue
        out.append(
            {
                "name": parts[0] or "NVIDIA GPU",
                "celsius": round(c, 1),
                "kind": "gpu",
                "source": "nvidia-smi",
            }
        )
    return out


def _parse_typeperf(counter: str) -> list[tuple[str, float]]:
    try:
        r = subprocess.run(
            ["typeperf", counter, "-sc", "1"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=12,
            creationflags=_NO_WINDOW,
        )
    except Exception:
        return []
    lines = [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()]
    if len(lines) < 2:
        return []
    header, values = lines[0], lines[1]
    # 例: ...\Thermal Zone Information(\_TZ.TZ00)\Temperature
    names = re.findall(
        r"Thermal Zone Information\(\\\\?([^)]+)\)\\(?:High Precision )?Temperature",
        header,
        flags=re.I,
    )
    if not names:
        names = re.findall(r"\\\\([^\\,\"]+)\\(?:High Precision )?Temperature", header, flags=re.I)
    if not names:
        names = [f"Zone#{i}" for i in range(10)]
    temp_vals: list[float] = []
    for m in re.finditer(r',"([\d.]+)"', values):
        temp_vals.append(float(m.group(1)))
    out: list[tuple[str, float]] = []
    for i, tv in enumerate(temp_vals):
        label = names[i] if i < len(names) else f"#{i}"
        out.append((label, tv))
    return out


def _temps_from_typeperf() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    # High Precision 优先（十分之一开尔文）
    for counter, prefer_hp in (
        (r"\Thermal Zone Information(*)\High Precision Temperature", True),
        (r"\Thermal Zone Information(*)\Temperature", False),
    ):
        for label, raw in _parse_typeperf(counter):
            key = label.lower()
            if key in seen:
                continue
            c = _kelvin_to_c(raw)
            if c is None:
                continue
            seen.add(key)
            out.append(
                {
                    "name": f"系统热区 {label}",
                    "celsius": c,
                    "kind": "other",
                    "source": "typeperf-hp" if prefer_hp else "typeperf",
                }
            )
    return out


def _temps_from_acpi() -> list[dict[str, Any]]:
    script = r"""
Get-CimInstance -Namespace root/wmi -ClassName MSAcpi_ThermalZoneTemperature -EA SilentlyContinue |
  ForEach-Object {
    [PSCustomObject]@{
      Name = $_.InstanceName
      K = $_.CurrentTemperature
    }
  } | ConvertTo-Json -Compress
"""
    try:
        raw = _powershell(script, timeout=15)
    except Exception:
        return []
    if not raw.strip():
        return []
    try:
        data = json.loads(raw)
    except Exception:
        return []
    if isinstance(data, dict):
        data = [data]
    out: list[dict[str, Any]] = []
    for item in data or []:
        if not isinstance(item, dict):
            continue
        try:
            k = float(item.get("K") or 0)
            c = (k / 10.0) - 273.15
        except (TypeError, ValueError):
            continue
        if c < -30 or c > 150:
            continue
        out.append(
            {
                "name": str(item.get("Name") or "ACPI"),
                "celsius": round(c, 1),
                "kind": "other",
                "source": "acpi",
            }
        )
    return out


def _temps_from_storage() -> list[dict[str, Any]]:
    """磁盘可靠性计数器温度（系统内置，部分盘支持）。"""
    script = r"""
$out = @()
Get-PhysicalDisk -EA SilentlyContinue | ForEach-Object {
  $d = $_
  try {
    $c = $d | Get-StorageReliabilityCounter -EA Stop
    if ($null -ne $c.Temperature) {
      $out += [PSCustomObject]@{
        Name = [string]$d.FriendlyName
        C = [double]$c.Temperature
        Media = [string]$d.MediaType
      }
    }
  } catch {}
}
$out | ConvertTo-Json -Compress
"""
    try:
        raw = _powershell(script, timeout=20)
    except Exception:
        return []
    if not raw.strip():
        return []
    try:
        data = json.loads(raw)
    except Exception:
        return []
    if isinstance(data, dict):
        data = [data]
    out: list[dict[str, Any]] = []
    for item in data or []:
        if not isinstance(item, dict):
            continue
        try:
            c = float(item.get("C"))
        except (TypeError, ValueError):
            continue
        if c < -30 or c > 150:
            continue
        name = str(item.get("Name") or "磁盘")
        media = str(item.get("Media") or "")
        out.append(
            {
                "name": f"{name}" + (f"（{media}）" if media else ""),
                "celsius": round(c, 1),
                "kind": "ssd",
                "source": "storage",
            }
        )
    return out


def collect_temperatures() -> list[dict[str, Any]]:
    """汇总温度：优先科科斯体内传感器宿主，其次系统内置兜底。"""
    ensure_sensor_host(prefer_elevated=True)
    priority = {
        "inner_sensor": 0,
        "nvidia-smi": 1,
        "storage": 2,
        "acpi": 3,
        "typeperf-hp": 4,
        "typeperf": 5,
    }
    merged: dict[str, dict[str, Any]] = {}
    batches = [_temps_from_inner_sensor()]
    # 体内宿主已有精确读数时，不再叠粗热区以免干扰摘要
    if not batches[0]:
        batches.extend(
            (
                _temps_from_nvidia(),
                _temps_from_storage(),
                _temps_from_acpi(),
                _temps_from_typeperf(),
            )
        )
    for batch in batches:
        for item in batch:
            key = f"{item.get('kind')}:{item.get('name')}".lower()
            prev = merged.get(key)
            if prev is None:
                merged[key] = item
                continue
            if priority.get(str(item["source"]), 9) < priority.get(str(prev["source"]), 9):
                merged[key] = item
    items = list(merged.values())
    items.sort(key=lambda x: (x.get("kind") or "", -(x.get("celsius") or 0)))
    return items


def _max_by_kind(temps: list[dict[str, Any]]) -> dict[str, float]:
    """摘要温度：CPU 优先 Package/Core Max；GPU 优先 Hot Spot/Core。"""
    preferred = {
        "cpu": ("package", "core max", "core average", "tctl", "tdie"),
        "gpu": ("hot spot", "hotspot", "gpu core", "edge", "junction"),
        "ssd": ("composite", "temperature"),
    }
    out: dict[str, float] = {}
    for kind, keys in preferred.items():
        cands = [t for t in temps if t.get("kind") == kind]
        if not cands:
            continue
        picked = None
        for key in keys:
            for t in cands:
                if key in str(t.get("name") or "").lower():
                    picked = t
                    break
            if picked:
                break
        if picked is None:
            picked = max(cands, key=lambda t: float(t.get("celsius") or 0))
        out[kind] = float(picked["celsius"])
    for t in temps:
        kind = str(t.get("kind") or "other")
        c = float(t.get("celsius") or 0)
        if kind not in out or c > out[kind]:
            out[kind] = c
    if temps:
        # any：用各 kind 摘要的最大值，避免 TjMax 距离等脏值
        vals = [v for k, v in out.items() if k != "any"]
        if vals:
            out["any"] = max(vals)
    return out


def get_temperatures() -> tuple[bool, str]:
    temps = collect_temperatures()
    thr = load_thresholds()
    if not temps:
        return (
            False,
            "未读到温度。可说「启动体内传感器」；完整 CPU 读数首次可能需同意一次 UAC（无窗口）。",
        )
    by = _max_by_kind(temps)
    lines = ["当前硬件温度（科科斯体内传感器，无桌面窗口）："]
    kind_label = {"cpu": "CPU", "gpu": "GPU", "ssd": "固态/硬盘", "other": "主板/其它"}
    for kind in ("cpu", "gpu", "ssd", "other"):
        if kind in by:
            limit = float(thr.get(kind) or _DEFAULT_THRESHOLDS[kind])
            flag = " ⚠超阈值" if by[kind] >= limit else ""
            lines.append(f"- {kind_label[kind]} {by[kind]:g}℃（阈值 {limit:g}℃）{flag}")
    lines.append("明细：")
    # 明细优先展示关键传感器
    important = []
    rest = []
    for t in temps:
        n = str(t.get("name") or "").lower()
        if any(
            k in n
            for k in (
                "package",
                "core max",
                "hot spot",
                "gpu core",
                "composite",
            )
        ):
            important.append(t)
        else:
            rest.append(t)
    shown = (important + rest)[:28]
    for t in shown:
        lines.append(
            f"  · [{t.get('kind')}] {t.get('name')}: {t.get('celsius'):g}℃"
        )
    return True, "\n".join(lines)


def check_temperature_alerts(
    temps: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """返回超阈值项：{kind, name, celsius, threshold}。"""
    temps = temps if temps is not None else collect_temperatures()
    thr = load_thresholds()
    alerts: list[dict[str, Any]] = []
    # 按 kind 取最高再比对，避免同 kind 多核刷屏
    best: dict[str, dict[str, Any]] = {}
    for t in temps:
        kind = str(t.get("kind") or "other")
        if kind not in best or float(t["celsius"]) > float(best[kind]["celsius"]):
            best[kind] = t
    for kind, t in best.items():
        limit = float(thr.get(kind, thr.get("other", 80)))
        if float(t["celsius"]) >= limit:
            alerts.append(
                {
                    "kind": kind,
                    "name": t.get("name"),
                    "celsius": t.get("celsius"),
                    "threshold": limit,
                }
            )
    return alerts


# ── 硬件信息 ─────────────────────────────────────────────────


def collect_hardware_info() -> dict[str, Any]:
    script = r"""
$cpu = Get-CimInstance Win32_Processor | Select-Object -First 1 Name,NumberOfCores,NumberOfLogicalProcessors,MaxClockSpeed,LoadPercentage
$os = Get-CimInstance Win32_OperatingSystem
$cs = Get-CimInstance Win32_ComputerSystem
$board = Get-CimInstance Win32_BaseBoard | Select-Object -First 1 Manufacturer,Product
$bios = Get-CimInstance Win32_BIOS | Select-Object -First 1 Manufacturer,SMBIOSBIOSVersion,ReleaseDate
$gpus = @(Get-CimInstance Win32_VideoController | Where-Object {
  $_.Name -and $_.Name -notmatch 'Oray|Idd|Remote|Basic|Microsoft'
} | Select-Object Name,AdapterRAM,DriverVersion,CurrentHorizontalResolution,CurrentVerticalResolution,CurrentRefreshRate)
$disks = @(Get-CimInstance Win32_DiskDrive | Select-Object Model,Size,InterfaceType,MediaType,SerialNumber)
$logical = @(Get-PSDrive -PSProvider FileSystem | Where-Object { $_.Used -ne $null } | ForEach-Object {
  [PSCustomObject]@{
    Name=$_.Name
    UsedGB=[math]::Round($_.Used/1GB,1)
    FreeGB=[math]::Round($_.Free/1GB,1)
    TotalGB=[math]::Round(($_.Used+$_.Free)/1GB,1)
  }
})
$memMods = @(Get-CimInstance Win32_PhysicalMemory | Select-Object BankLabel,Capacity,Speed,Manufacturer,PartNumber)
[PSCustomObject]@{
  cpu=$cpu
  totalMemGB=[math]::Round($cs.TotalPhysicalMemory/1GB,1)
  freeMemGB=[math]::Round($os.FreePhysicalMemory/1MB,1)
  board=$board
  bios=$bios
  system=@{Manufacturer=$cs.Manufacturer; Model=$cs.Model}
  gpus=$gpus
  disks=$disks
  volumes=$logical
  memoryModules=$memMods
} | ConvertTo-Json -Depth 5 -Compress
"""
    raw = _powershell(script, timeout=25)
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def get_hardware_info() -> tuple[bool, str]:
    info = collect_hardware_info()
    if not info:
        return False, "无法获取硬件信息"
    lines: list[str] = ["本机硬件信息："]
    cpu = info.get("cpu") or {}
    if isinstance(cpu, dict) and cpu.get("Name"):
        lines.append(
            f"- CPU：{cpu['Name']}｜{cpu.get('NumberOfCores')}核"
            f"{cpu.get('NumberOfLogicalProcessors')}线程｜"
            f"标频 {cpu.get('MaxClockSpeed')} MHz｜负载 {cpu.get('LoadPercentage')}%"
        )
    lines.append(
        f"- 内存：已用约 "
        f"{round(float(info.get('totalMemGB') or 0) - float(info.get('freeMemGB') or 0), 1)}"
        f" / {info.get('totalMemGB')} GB"
    )
    mods = info.get("memoryModules") or []
    if isinstance(mods, dict):
        mods = [mods]
    if mods:
        for m in mods[:8]:
            if not isinstance(m, dict):
                continue
            cap = float(m.get("Capacity") or 0) / (1024**3)
            lines.append(
                f"  · {m.get('BankLabel') or '槽'}: {cap:.0f} GB @"
                f"{m.get('Speed') or '?'} MT/s"
                f" {m.get('Manufacturer') or ''} {m.get('PartNumber') or ''}".rstrip()
            )
    board = info.get("board") or {}
    if isinstance(board, dict) and board.get("Product"):
        lines.append(f"- 主板：{board.get('Manufacturer')} {board.get('Product')}")
    sysinfo = info.get("system") or {}
    if isinstance(sysinfo, dict) and sysinfo.get("Model"):
        lines.append(f"- 机型：{sysinfo.get('Manufacturer')} {sysinfo.get('Model')}")
    gpus = info.get("gpus") or []
    if isinstance(gpus, dict):
        gpus = [gpus]
    for g in gpus:
        if not isinstance(g, dict):
            continue
        vram = g.get("AdapterRAM")
        vram_s = ""
        try:
            if vram:
                # WMI 常截断 4GB，仅作参考
                vram_s = f"｜驱动显存字段约 {round(int(vram)/(1024**3), 1)} GB"
        except Exception:
            pass
        res = ""
        if g.get("CurrentHorizontalResolution"):
            res = (
                f"｜{g.get('CurrentHorizontalResolution')}x"
                f"{g.get('CurrentVerticalResolution')}@"
                f"{g.get('CurrentRefreshRate') or '?'}Hz"
            )
        lines.append(f"- 显卡：{g.get('Name')}{vram_s}{res}")
        if g.get("DriverVersion"):
            lines.append(f"  · 驱动：{g.get('DriverVersion')}")
    disks = info.get("disks") or []
    if isinstance(disks, dict):
        disks = [disks]
    for d in disks:
        if not isinstance(d, dict):
            continue
        size = float(d.get("Size") or 0) / (1024**3)
        lines.append(
            f"- 磁盘：{d.get('Model')}｜{size:.0f} GB｜"
            f"{d.get('InterfaceType') or ''} {d.get('MediaType') or ''}".rstrip()
        )
    vols = info.get("volumes") or []
    if isinstance(vols, dict):
        vols = [vols]
    if vols:
        lines.append("- 分区：")
        for v in vols:
            if not isinstance(v, dict):
                continue
            lines.append(
                f"  · {v.get('Name')}: 已用 {v.get('UsedGB')} / "
                f"{v.get('TotalGB')} GB（剩 {v.get('FreeGB')} GB）"
            )
    # 附带温度摘要
    temps = collect_temperatures()
    if temps:
        by = _max_by_kind(temps)
        bits = []
        for k, lab in (("cpu", "CPU"), ("gpu", "GPU"), ("ssd", "盘")):
            if k in by:
                bits.append(f"{lab} {by[k]:g}℃")
        if bits:
            lines.append("- 温度：" + "，".join(bits))
    else:
        lines.append("- 温度：系统热区暂无读数")
    return True, "\n".join(lines)


def setup_temp_sensors(*, download: bool = True) -> tuple[bool, str]:  # noqa: ARG001
    """启动科科斯体内传感器宿主（无桌面窗口）。"""
    ok, msg = ensure_sensor_host(prefer_elevated=True)
    if not ok:
        return False, msg
    temps = _temps_from_inner_sensor()
    by = _max_by_kind(temps)
    bits = []
    for k, lab in (("cpu", "CPU"), ("gpu", "GPU"), ("ssd", "盘")):
        if k in by:
            bits.append(f"{lab} {by[k]:g}℃")
    extra = ("；" + "，".join(bits)) if bits else ""
    return True, f"{msg}{extra}。全程在科科斯体内，不打开桌面监控窗。"


def start_thermal_monitoring() -> None:
    """路由启动时：拉起报警守护，并尝试启动体内传感器（无窗）。"""
    ensure_guardian()
    try:
        ensure_sensor_host(prefer_elevated=True)
    except Exception:
        logger.debug("inner sensor host start failed", exc_info=True)


# ── 报警守护 ─────────────────────────────────────────────────


class ThermalGuardian:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._kick = threading.Event()
        self._lock = threading.Lock()
        self._last_alert: dict[str, float] = {}  # kind -> monotonic
        self._armed: dict[str, bool] = {}  # kind -> waiting cool-down below thr-hyst
        self._thread = threading.Thread(
            target=self._loop, name="thermal-guardian", daemon=True
        )
        self._thread.start()

    def kick(self) -> None:
        self._kick.set()

    def stop(self) -> None:
        self._stop.set()
        self._kick.set()

    def _loop(self) -> None:
        self._stop.wait(8)
        while not self._stop.is_set():
            thr = load_thresholds()
            interval = max(15, int(thr.get("interval_sec") or 30))
            try:
                if thr.get("alert_enabled"):
                    self._scan(thr)
            except Exception:
                logger.debug("thermal scan failed", exc_info=True)
            self._kick.wait(interval)
            self._kick.clear()

    def _scan(self, thr: dict[str, Any]) -> None:
        temps = collect_temperatures()
        if not temps:
            return
        alerts = check_temperature_alerts(temps)
        now = time.monotonic()
        cooldown = float(thr.get("cooldown_sec") or 300)
        hyst = float(thr.get("hysteresis_c") or 5)
        by = _max_by_kind(temps)

        # 复位武装：温度回落到阈值-回差以下
        for kind in list(self._armed):
            limit = float(thr.get(kind, thr.get("other", 80)))
            cur = by.get(kind)
            if cur is not None and cur < limit - hyst:
                self._armed.pop(kind, None)

        for a in alerts:
            kind = str(a["kind"])
            if kind in self._armed:
                continue
            last = self._last_alert.get(kind, 0)
            if now - last < cooldown:
                continue
            self._last_alert[kind] = now
            self._armed[kind] = True
            label = {"cpu": "CPU", "gpu": "GPU", "ssd": "硬盘", "other": "传感器"}.get(
                kind, kind
            )
            msg = (
                f"温度报警：{label}「{a.get('name')}」当前 {a.get('celsius'):g}℃，"
                f"已超过阈值 {a.get('threshold'):g}℃，请注意散热或降低负载。"
            )
            try:
                from ..notifications import has_reminder_callbacks, notify_reminder

                if has_reminder_callbacks():
                    notify_reminder(msg)
                else:
                    logger.warning("[Thermal] %s", msg)
            except Exception:
                logger.warning("[Thermal] %s", msg)


_guardian: ThermalGuardian | None = None
_guardian_lock = threading.Lock()


def get_guardian() -> ThermalGuardian | None:
    return _guardian


def ensure_guardian() -> ThermalGuardian:
    global _guardian
    with _guardian_lock:
        if _guardian is None:
            _guardian = ThermalGuardian()
        return _guardian
