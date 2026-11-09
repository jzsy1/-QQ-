"""环境变量读写（用户级写入）"""

from __future__ import annotations

import os
import re

from .winops import powershell

_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _valid_name(name: str) -> bool:
    return bool(_NAME_RE.match((name or "").strip()))


def read_env_var(name: str) -> tuple[bool, str]:
    name = (name or "").strip()
    if not _valid_name(name):
        return False, "环境变量名无效，仅允许字母、数字和下划线"

    safe = name.replace("'", "''")
    script = f"""
$u = [Environment]::GetEnvironmentVariable('{safe}', 'User')
$p = [Environment]::GetEnvironmentVariable('{safe}', 'Process')
$m = [Environment]::GetEnvironmentVariable('{safe}', 'Machine')
$lines = @()
if ($null -ne $u -and $u -ne '') {{ $lines += \"User: $u\" }}
if ($null -ne $p -and $p -ne '') {{ $lines += \"Process: $p\" }}
if ($null -ne $m -and $m -ne '') {{ $lines += \"Machine: $m\" }}
if ($lines.Count -eq 0) {{ Write-Output '(未设置)' }} else {{ $lines | ForEach-Object {{ Write-Output $_ }} }}
"""
    out = powershell(script, timeout=10)
    return True, out.strip() or "(未设置)"


def set_env_var(name: str, value: str) -> tuple[bool, str]:
    name = (name or "").strip()
    if not _valid_name(name):
        return False, "环境变量名无效"
    if not name:
        return False, "请提供环境变量名"

    # 强制转为字符串（防止传入 None/数字等）
    if value is None:
        value = ""
    else:
        value = str(value)

    safe = name.replace("'", "''")
    env = {**os.environ, "JV_ENV_VAL": value}
    script = (
        f"[Environment]::SetEnvironmentVariable('{safe}', $env:JV_ENV_VAL, 'User'); "
        f"Write-Output 'OK'"
    )
    out = powershell(script, env=env, timeout=10)
    if "OK" in (out or ""):
        preview = value if len(value) <= 60 else value[:60] + "…"
        return True, f"已设置用户环境变量 {name}={preview}"
    return False, (out or "设置失败").strip()


def list_env_vars(prefix: str = "", limit: int = 40) -> tuple[bool, str]:
    limit = max(5, min(int(limit), 100))
    prefix = (prefix or "").strip()
    safe_prefix = prefix.replace("'", "''")
    # 用 JSON 输出避免多行值污染行数统计；limit 作用于变量数而非行数
    script = f"""
$vars = [Environment]::GetEnvironmentVariables('User')
$prefix = '{safe_prefix}'
$items = $vars.GetEnumerator() |
  Where-Object {{ -not $prefix -or $_.Key -like ($prefix + '*') }} |
  Sort-Object Key |
  Select-Object -First {limit}
if (-not $items) {{ Write-Output '[]' ; exit 0 }}
$items | ForEach-Object {{
  $v = [string]$_.Value
  if ($v.Length -gt 80) {{ $v = $v.Substring(0, 80) + '…' }}
  # 用 base64 包裹 value，避免换行污染输出
  $bytes = [Text.Encoding]::UTF8.GetBytes($v)
  $b64 = [Convert]::ToBase64String($bytes)
  Write-Output ($_.Key + '\t' + $b64)
}}
"""
    out = powershell(script, timeout=15)
    raw_lines = [ln.strip() for ln in (out or "").splitlines() if ln.strip()]
    if not raw_lines or raw_lines == ["[]"]:
        return True, "未找到匹配的环境变量"
    import base64 as _b64
    decoded = []
    for ln in raw_lines:
        if "\t" in ln:
            k, b64v = ln.split("\t", 1)
            try:
                v = _b64.b64decode(b64v).decode("utf-8")
            except Exception:
                v = b64v
            decoded.append(f"{k}={v}")
    if not decoded:
        return True, "未找到匹配的环境变量"
    return True, "用户环境变量：\n" + "\n".join(decoded)
