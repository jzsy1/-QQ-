"""Windows 更新控制 — 微软官方 Microsoft.Update.Session COM 组件。

调用路径：PowerShell 官方 COM 互操作（New-Object -ComObject
Microsoft.Update.Session）→ ConvertTo-Json 输出 → Python 解析。
COM 组件本身是微软开放的正门（WUAPI 2.0，文档在 learn.microsoft.com）；
走 PowerShell 而非手写 ctypes 虚表，因 IUpdateSession/ISearcher/
IDownloader/IInstaller 接口链深、虚表易错，官方互操作层更稳。

能力与边界：
  check_updates   搜索可用更新（需联网，微软服务器响应 30s~3min）
  install_updates 下载+安装（**需要管理员权限**，UAC 下会拒绝）
  get_update_history 最近 20 条安装历史（本地读取，无需联网）

结果码均为微软文档固定枚举（非业务硬编码）：
  IInstallationResult.ResultCode: 2=成功 3=成功但有错误 4=失败 5=中止
  InstallationBehavior.RebootBehavior: 0=可不重启 1=需要重启 2=可能要求重启
  IUpdateHistoryEntry.ResultCode: 2=成功 3=失败已回滚? 4=失败 5=已放弃
"""

from __future__ import annotations

import base64
import ctypes
import json
import logging
import subprocess

logger = logging.getLogger(__name__)

# 子进程超时（秒）：首次搜索微软服务器较慢；安装含下载更长
_CHECK_TIMEOUT = 300
_HISTORY_TIMEOUT = 60
_INSTALL_TIMEOUT = 900

# 微软 WUAPI 文档固定枚举
_INSTALL_RESULT_NAMES = {
    2: "成功",
    3: "成功但有错误",
    4: "失败",
    5: "已中止",
}
_REBOOT_NAMES = {
    0: "无需重启",
    1: "需要重启",
    2: "可能要求重启",
}
_HISTORY_RESULT_NAMES = {
    2: "成功",
    3: "失败（已回滚）",
    4: "失败",
    5: "已取消",
}

# 常见 WU 错误 HResult → 用户提示（微软文档公开错误码）
_WU_ERROR_HINTS = {
    -2145107924: "无法连接到更新服务器（0x8024402C），请检查网络或代理",
    -2145107933: "网络超时（0x80244022），稍后再试",
    -2147024891: "权限不足（0x80070005）：安装更新需要管理员权限运行科科斯",
}


def _is_admin() -> bool:
    """当前进程是否管理员（shell32 官方 API）。"""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _run_ps(script: str, timeout: int) -> dict:
    """执行 PowerShell 脚本并解析 JSON 输出。

    脚本必须自捕获异常并输出 {"ok": false, "error": ...}。
    用 -EncodedCommand（UTF-16LE base64）规避引号转义与编码问题。
    """
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    cmd = [
        "powershell", "-NoProfile", "-NonInteractive",
        "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"操作超时（{timeout}s）：微软服务器无响应或更新量过大") from e
    raw = proc.stdout or b""
    # PowerShell 强制 UTF-8 输出（脚本内设置 OutputEncoding）
    out = raw.decode("utf-8", errors="replace").strip()
    if not out:
        err = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"PowerShell 无输出（rc={proc.returncode}）：{err[:200]}")
    # 取最后一行 JSON（防脚本前导输出干扰）
    for line in reversed(out.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    raise RuntimeError(f"无法解析 PowerShell 输出：{out[:200]}")


_PS_CHECK = r"""
$ErrorActionPreference='Stop'
try{
  [Console]::OutputEncoding=[Text.Encoding]::UTF8
  $s=New-Object -ComObject Microsoft.Update.Session
  $searcher=$s.CreateUpdateSearcher()
  $r=$searcher.Search("IsInstalled=0 and IsHidden=0")
  $list=@()
  for($i=0;$i -lt $r.Updates.Count;$i++){
    $u=$r.Updates.Item($i)
    $cats=@()
    foreach($c in $u.Categories){ $cats+=$c.Name }
    $list+=[ordered]@{
      index=$i
      title=$u.Title
      sizeMB=[math]::Round($u.MaxDownloadSize/1MB,1)
      reboot=[int]$u.InstallationBehavior.RebootBehavior
      category=($cats -join '/')
    }
  }
  @{ok=$true; count=$list.Count; updates=$list}|ConvertTo-Json -Compress -Depth 4
}catch{
  @{ok=$false; error=$_.Exception.Message; hresult=$_.Exception.HResult}|ConvertTo-Json -Compress
}
"""

_PS_HISTORY = r"""
$ErrorActionPreference='Stop'
try{
  [Console]::OutputEncoding=[Text.Encoding]::UTF8
  $s=New-Object -ComObject Microsoft.Update.Session
  $searcher=$s.CreateUpdateSearcher()
  $h=$searcher.QueryHistory(0,20)
  $list=@()
  foreach($e in $h){
    $list+=[ordered]@{
      title=$e.Title
      date=($e.Date.ToString('yyyy-MM-dd HH:mm'))
      result=[int]$e.ResultCode
      hresult=$e.HResult
    }
  }
  @{ok=$true; count=$list.Count; history=$list}|ConvertTo-Json -Compress -Depth 4
}catch{
  @{ok=$false; error=$_.Exception.Message}|ConvertTo-Json -Compress
}
"""

_PS_INSTALL = r"""
$ErrorActionPreference='Stop'
try{
  [Console]::OutputEncoding=[Text.Encoding]::UTF8
  $Filter='__FILTER__'
  $s=New-Object -ComObject Microsoft.Update.Session
  $searcher=$s.CreateUpdateSearcher()
  $r=$searcher.Search("IsInstalled=0 and IsHidden=0")
  $coll=New-Object -ComObject Microsoft.Update.UpdateColl
  $seen=@()
  for($i=0;$i -lt $r.Updates.Count;$i++){
    $u=$r.Updates.Item($i)
    if($Filter -eq '' -or $u.Title.ToLower().Contains($Filter.ToLower())){
      [void]$coll.Add($u)
      $seen+=$u.Title
    }
  }
  if($coll.Count -eq 0){
    @{ok=$false; error='no_match'}|ConvertTo-Json -Compress
    exit
  }
  $dl=$s.CreateUpdateDownloader()
  $dl.Updates=$coll
  $dres=$dl.Download()
  $inst=$s.CreateUpdateInstaller()
  $inst.Updates=$coll
  $ires=$inst.Install()
  $detail=@()
  for($i=0;$i -lt $coll.Count;$i++){
    $ur=$ires.GetUpdateResult($i)
    $detail+=[ordered]@{title=$coll.Item($i).Title; result=[int]$ur.ResultCode; hresult=$ur.HResult}
  }
  @{ok=$true; count=$coll.Count; requested=$seen; resultCode=[int]$ires.ResultCode; rebootRequired=[bool]$ires.RebootRequired; detail=$detail}|ConvertTo-Json -Compress -Depth 4
}catch{
  @{ok=$false; error=$_.Exception.Message; hresult=$_.Exception.HResult}|ConvertTo-Json -Compress
}
"""


def check_updates() -> tuple[bool, str]:
    """搜索可用更新。需联网；微软服务器响应可能需要 1-3 分钟。"""
    try:
        data = _run_ps(_PS_CHECK, _CHECK_TIMEOUT)
    except RuntimeError as e:
        return False, f"检查更新失败：{e}"
    if not data.get("ok"):
        err = str(data.get("error", "未知错误"))
        hint = _WU_ERROR_HINTS.get(data.get("hresult"), "")
        return False, f"检查更新失败：{err}" + (f"（{hint}）" if hint else "")
    count = int(data.get("count", 0))
    if count == 0:
        return True, "系统已是最新的，没有待安装的更新"
    lines = [f"发现 {count} 个可用更新："]
    for u in data.get("updates", []):
        reboot = _REBOOT_NAMES.get(int(u.get("reboot", 0)), "")
        size = u.get("sizeMB", 0)
        size_txt = f"{size}MB" if isinstance(size, (int, float)) and size > 0 else "—"
        cat = u.get("category", "") or "其他"
        lines.append(
            f"{int(u['index']) + 1}. {u.get('title', '未知')}（{size_txt}，{cat}，{reboot}）"
        )
    lines.append("可以说『安装更新』全部安装，或『安装包含 xx 的更新』安装指定项")
    return True, "\n".join(lines)


def install_updates(update_filter: str | None = None) -> tuple[bool, str]:
    """下载并安装更新。update_filter=None 安装全部；否则按标题子串匹配。

    需要管理员权限（Windows 安全模型要求），普通权限进程会明确拒绝。
    成功后若需重启，提示用户可用『重启电脑』完成收尾。
    """
    if not _is_admin():
        return False, (
            "安装更新需要管理员权限。请以管理员身份运行科科斯后再试；"
            "或手动打开 设置→Windows 更新→安装"
        )
    filt = str(update_filter or "").strip()
    # PowerShell 单引号字符串转义：' → ''（防 LLM 传入引号破坏脚本结构）
    script = _PS_INSTALL.replace("__FILTER__", filt.replace("'", "''"))
    try:
        data = _run_ps(script, _INSTALL_TIMEOUT)
    except RuntimeError as e:
        return False, f"安装更新失败：{e}"

    if data.get("error") == "no_match":
        scope = f"标题含「{filt}」的更新" if filt else "可用更新"
        return False, f"没有找到匹配的{scope}。可以先说『检查更新』看看有哪些"
    if not data.get("ok"):
        err = str(data.get("error", "未知错误"))
        hint = _WU_ERROR_HINTS.get(data.get("hresult"), "")
        return False, f"安装更新失败：{err}" + (f"（{hint}）" if hint else "")

    count = int(data.get("count", 0))
    code = int(data.get("resultCode", 0))
    code_name = _INSTALL_RESULT_NAMES.get(code, f"未知结果码{code}")
    failed = [
        d for d in data.get("detail", [])
        if int(d.get("result", 0)) == 4
    ]
    lines = [f"已完成 {count} 个更新的安装：{code_name}"]
    if failed:
        for d in failed:
            hr = int(d.get("hresult", 0) or 0)
            lines.append(f"- 失败：{d.get('title', '')}（0x{hr & 0xFFFFFFFF:08X}）")
    if data.get("rebootRequired"):
        lines.append("需要重启完成安装，可以对我说『重启电脑』")
    return True, "\n".join(lines)


def get_update_history() -> tuple[bool, str]:
    """读取最近 20 条更新安装历史（本地 WU 数据库，无需联网）。"""
    try:
        data = _run_ps(_PS_HISTORY, _HISTORY_TIMEOUT)
    except RuntimeError as e:
        return False, f"读取更新历史失败：{e}"
    if not data.get("ok"):
        return False, f"读取更新历史失败：{data.get('error', '未知错误')}"
    hist = data.get("history", [])
    if not hist:
        return True, "更新历史为空"
    lines = ["最近更新记录："]
    for e in hist:
        name = _HISTORY_RESULT_NAMES.get(int(e.get("result", 0)), "未知")
        hr = int(e.get("hresult", 0) or 0)
        suffix = f"，错误 0x{hr & 0xFFFFFFFF:08X}" if hr not in (0, -2145124322) else ""
        lines.append(f"- [{e.get('date', '')}] {e.get('title', '未知')}（{name}{suffix}）")
    return True, "\n".join(lines)
