"""Kekesi 内部接口统一契约（中心定义，纯标准库）。

命名、入口解析、字段校验、关键词合并、上限、超时、可见谓词。
registry / bridge / router / executor / agent_loop 统一调用这里的函数，
不再各自再写一份规则。

默认值全部沿用仓库版，不引入任何对接传输。
"""

import re
from dataclasses import dataclass, field

_CLEAN_RE = re.compile(r"[^a-zA-Z0-9_-]")
_SKILL_PREFIX = "skill__"
_MCP_PREFIX = "mcp__"


def sanitize_segment(name):
    """统一清洗：非字母数字下划线短横线替换为下划线。"""
    return _CLEAN_RE.sub("_", str(name or ""))


def skill_tool_name(skill_id, tool_id):
    """技能工具暴露名。

    owner 也做清洗，且 owner/tool 里原有的 "__" 先归一为 "_"，否则
    parse 时无法区分分隔符与原始下划线。工具名截断 40 字符。
    """
    # 先按 "/" 切分 owner，再逐段清洗，最后用 "__" 拼接，避免
    # 原始 owner 里的 "__" 与分隔符混淆。
    parts = str(skill_id or "").split("/")
    clean_parts = [sanitize_segment(p).replace("__", "_") for p in parts]
    owner = "__".join(clean_parts)
    tool = sanitize_segment(tool_id).replace("__", "_")[:40]
    return f"{_SKILL_PREFIX}{owner}__{tool}"


def parse_skill_tool_name(full_name):
    """解析技能工具暴露名，失败返回 None。"""
    if not str(full_name or "").startswith(_SKILL_PREFIX):
        return None
    body = str(full_name)[len(_SKILL_PREFIX):]
    parts = body.split("__")
    if len(parts) < 3:
        return None
    return "/".join(parts[:-1]), parts[-1]


def mcp_tool_name(server, tool):
    """MCP 工具暴露名：各段清洗截断 60，整体截断 64。"""
    full = f"{_MCP_PREFIX}{sanitize_segment(server)[:60]}__{sanitize_segment(tool)[:60]}"
    return full[:64]


def resolve_entry(skill_path, meta):
    """统一入口解析：meta entry 缺省 main.py，必须是相对路径且不逃出技能目录。

    返回 (entry_name, is_valid)。调用方对无效入口记 error，不注册。
    """
    from pathlib import Path

    entry_name = str((meta or {}).get("entry") or "main.py")
    if entry_name.startswith("/") or ".." in Path(entry_name).parts:
        return entry_name, False
    base = Path(skill_path)
    resolved = (base / entry_name).resolve()
    try:
        resolved.relative_to(base.resolve())
    except ValueError:
        return entry_name, False
    return entry_name, True


@dataclass
class DiscoveryLimits:
    """上限集中定义，数值全部沿用仓库版。"""

    skill_max: int = 6
    mcp_max: int = 16
    mcp_per_server_max: int = 6
    retriever_top_k: int = 20
    retriever_extra_cap: int = 12
    tier0_search_top_k: int = 12
    local_model_top_k: int = 14
    schema_description_max: int = 1000
    briefing_description_max: int = 70
    mcp_output_max: int = 4000
    tool_result_max: int = 1200
    tool_result_list_max: int = 6000


@dataclass
class Timeouts:
    """超时集中定义。per_tool_sec 只保留公共默认值，本地细表仍由 executor 维护。"""

    mcp_startup_sec: float = 60.0
    mcp_call_sec: float = 90.0
    mcp_call_min_sec: float = 5.0
    mcp_close_sec: float = 10.0
    default_tool_sec: float = 15.0
    per_tool_sec: dict = field(default_factory=lambda: {
        "open_app": 25,
        "close_app": 25,
        "run_shell": 300,
        "acquire_skill": 180,
        "uninstall_skill": 30,
        "check_updates": 320,
        "install_updates": 920,
    })
    mcp_init_sec: dict = field(default_factory=lambda: {
        "filesystem": 60,
        "playwright": 120,
        "fetch": 45,
    })
    mcp_npx_init_sec: int = 120

    def mcp_call(self, override=None):
        if override is None:
            return self.mcp_call_sec
        return max(self.mcp_call_min_sec, float(override))


def tool_keywords(skill_meta, tool_item):
    """关键词并集：工具项优先，技能级兜底，去重、小写。"""
    out = []
    seen = set()
    for source in (tool_item or {}, skill_meta or {}):
        raw = source.get("keywords") or []
        if isinstance(raw, str):
            raw = [raw]
        for kw in raw:
            key = str(kw).strip().lower()
            if key and key not in seen:
                seen.add(key)
                out.append(key)
    return out


def validate_skill_manifest(meta):
    """校验 skill.yaml，返回 (errors, warnings)，不抛异常。

    errors 非空时调用方不应注册；warnings 只记录，不阻止注册。
    """
    errors = []
    warnings = []
    meta = meta or {}
    if not str(meta.get("name") or "").strip():
        errors.append("missing name")
    tools = meta.get("tools")
    if tools is None:
        warnings.append("missing tools list")
    elif not isinstance(tools, list):
        errors.append("tools is not a list")
    else:
        for item in tools:
            if not isinstance(item, dict):
                errors.append("tool item is not a dict")
                continue
            if not str(item.get("id") or item.get("name") or "").strip():
                errors.append("tool item missing id/name")
    entry = str(meta.get("entry") or "main.py")
    if entry.startswith("/") or ".." in entry.split("/"):
        errors.append("entry escapes skill directory")
    if meta.get("workflow_name") is not None and not str(meta.get("workflow_name")).strip():
        warnings.append("workflow_name is empty")
    return errors, warnings


def is_tool_visible(name, tools_cfg, registry_disabled=False):
    """统一可见谓词。

    - registry DISABLED -> 不可见。
    - tools_cfg 明确禁用 -> 不可见（所有分支一致）。
    - tools_cfg 无法判断 -> 可见（保留 skill/mcp 工具豁免语义）。
    tools_cfg 可以是 dict（disabled 集合或 名字->enabled 映射），也可以是
    callable（本地分组开关用 is_tool_enabled 适配）；调用方也可再叠加
    registry 状态做最终判定。
    """
    if registry_disabled:
        return False
    if tools_cfg is None:
        return True
    if callable(tools_cfg):
        try:
            return bool(tools_cfg(name))
        except Exception:
            return True
    if isinstance(tools_cfg, dict):
        disabled = tools_cfg.get("disabled") or tools_cfg.get("disabled_tools") or set()
        if isinstance(disabled, (set, list, tuple)) and name in disabled:
            return False
        # 兼容“名字 -> enabled 布尔”的映射形态。
        if name in tools_cfg and tools_cfg[name] is False:
            return False
    return True


class NameRegistry:
    """暴露名冲突检测：同一暴露名只注册第一个，后者记 error。"""

    def __init__(self):
        self._owners = {}

    def claim(self, exposed, original):
        """成功返回 True；冲突返回 False 且不覆盖已有映射。"""
        if exposed in self._owners and self._owners[exposed] != original:
            return False
        self._owners[exposed] = original
        return True

    def owner_of(self, exposed):
        return self._owners.get(exposed)


if __name__ == "__main__":
    assert skill_tool_name("windows/example", "ping") == "skill__windows__example__ping"
    assert parse_skill_tool_name("skill__windows__example__ping") == ("windows/example", "ping")
    assert mcp_tool_name("filesystem", "read_file") == "mcp__filesystem__read_file"
    assert len(mcp_tool_name("a" * 80, "b" * 80)) == 64
    assert tool_keywords({"keywords": ["A"]}, {"keywords": ["b", "A"]}) == ["b", "a"]
    assert validate_skill_manifest({"name": "x", "tools": [{"id": "ping"}]}) == ([], [])
    assert is_tool_visible("skill__a__b", None) is True
    assert is_tool_visible("skill__a__b", {"disabled": {"skill__a__b"}}) is False
    reg = NameRegistry()
    assert reg.claim("mcp__fs__read", "read_file") is True
    assert reg.claim("mcp__fs__read", "read_file_v2") is False
    print("contract self-test ok")
