"""应用名称别名与相近匹配 — 内置表为空，优先 data/app_aliases.json 与用户学习。"""

from __future__ import annotations

import json
from pathlib import Path

# 不在代码里写死第三方应用；别名由 app_aliases.json / apps_registry 学习补充。
# 系统应用（记事本/画图/计算器等）的中英文映射靠 desktop_apps 索引自动发现：
# Get-StartApps 返回 UWP 应用的中文显示名，App Paths 注册表提供英文 exe 名。
_BUILTIN_ALIASES: dict[str, str] = {}

_ALIASES_PATH = Path(__file__).parent / "data" / "app_aliases.json"


def load_aliases() -> dict[str, str]:
    merged = dict(_BUILTIN_ALIASES)
    if _ALIASES_PATH.exists():
        try:
            data = json.loads(_ALIASES_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for key, val in data.items():
                    if isinstance(key, str) and isinstance(val, str) and key.strip() and val.strip():
                        merged[key.strip().lower()] = val.strip()
        except (json.JSONDecodeError, OSError):
            pass
    return merged


NAME_ALIASES: dict[str, str] = load_aliases()


def reload_aliases() -> dict[str, str]:
    global NAME_ALIASES
    NAME_ALIASES = load_aliases()
    return NAME_ALIASES


def names_for_match(name: str) -> set[str]:
    raw = (name or "").strip().lower()
    if not raw:
        return set()
    keys = {raw}
    mapped = NAME_ALIASES.get(raw)
    if mapped:
        keys.add(mapped.lower())
    return keys


def app_names_match(name: str, alias: str) -> bool:
    a_keys = names_for_match(name)
    b_keys = names_for_match(alias)
    if not a_keys or not b_keys:
        return False
    if a_keys & b_keys:
        return True
    for a in a_keys:
        for b in b_keys:
            if a == b:
                return True
            shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
            # 至少 2 字才做子串匹配：允许"记事"→"记事本"、"音乐"→"QQ音乐"
            # 但仍排除 1 字过于宽泛的匹配（如"记"→"记事本"）
            if len(shorter) < 2:
                continue
            if shorter in longer:
                return True
    return False
