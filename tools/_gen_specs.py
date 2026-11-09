# -*- coding: utf-8 -*-
"""根据 _schemas_dump.json 生成 TOOL_SPECS 的 ParamSpec 代码块。

保守策略（不引入新 bug）：
- string：max_len=200，防 LLM 传超长导致下游崩
- string+enum：enum 直接复用
- integer/number：仅类型转换，不加 min/max（越界交给 handler）
- boolean：default=False（特殊语义工具手动修正为 True）
- array/object：跳过（ParamSpec 不支持，交给 handler 自行校验）
- 必填：从 schema required 取
"""
from __future__ import annotations

import json
from pathlib import Path

# 已知 default=True 的 boolean 参数（语义为"默认强制"）
_TRUE_DEFAULTS = {
    ("close_app", "force"),
    ("kill_process", "force"),
    ("grant_full_control", "recurse"),
    ("take_ownership", "recurse"),
    ("click_desktop_icon", "double"),
    ("ocr_window", "dark_enhance"),
    ("scan_desktop", "include_controls"),
    ("scan_desktop", "include_regions"),
    ("gui_action", "partial"),
    ("click_text", "partial"),
    ("ocr_find_text", "partial"),
    ("find_text_location", "partial"),
    ("hover_text", "partial"),
    ("wait_until_found", "partial"),
}

# 长文本参数：max_len 放宽（避免截断文档/代码/消息正文）
# None 表示不限长（交给 handler），数字表示该上限
_LONG_TEXT_MAXLEN: dict[tuple[str, str], int | None] = {
    ("create_document", "content"): None,        # 文档正文，可能很长
    ("write_code_file", "content"): None,        # 代码文件完整内容
    ("type_text", "text"): 2000,                 # 输入文字
    ("open_app_and_type", "text"): 2000,         # 应用内输入
    ("wechat_send_message", "message"): 2000,    # 微信消息正文
    ("wechat_reply_current", "message"): 2000,
    ("detect_emotion", "text"): 2000,            # 情绪分析文本
    ("remember", "fact"): 500,                   # 一句话记忆
    ("add_watch_rule", "question"): 500,        # 触发询问
    ("add_watch_rule", "description"): 500,
    ("schedule_action", "description"): 500,
    ("set_reminder", "description"): 500,
    ("save_workflow", "trigger_hint"): 500,
    ("run_shell", "command"): 1000,              # shell 命令可能较长
    ("run_shell_admin", "command"): 1000,
    ("run_shell_system", "command"): 1000,
    ("fill_form", "submit"): 200,
}


def py_str(s: str) -> str:
    return '"' + s.replace('"', '\\"') + '"'


def py_tuple(items) -> str:
    if not items:
        return "()"
    if len(items) == 1:
        return f'("{items[0]}",)'
    return "(" + ", ".join(py_str(i) for i in items) + ")"


def main() -> None:
    dump_path = Path(__file__).resolve().parent / "_schemas_dump.json"
    data = json.loads(dump_path.read_text(encoding="utf-8"))

    no_param = data["no_param"]
    with_param = data["with_param"]

    lines: list[str] = []
    lines.append('# -*- coding: utf-8 -*-')
    lines.append('"""Typed Executors 工具参数契约 — 由 _gen_specs.py 从 registry schema 自动生成。')
    lines.append('')
    lines.append('维护说明：增删工具或改 schema 后，重跑 `python -m kekesi.tools._gen_specs`')
    lines.append('覆盖本文件；特殊语义（default/max_len）在 _gen_specs.py 的 _TRUE_DEFAULTS /')
    lines.append('_LONG_TEXT_MAXLEN 集中维护，勿手改本文件。"""')
    lines.append('')
    lines.append('from __future__ import annotations')
    lines.append('')
    lines.append('from .typed_executors import ParamSpec')
    lines.append('')
    lines.append('TOOL_SPECS: dict[str, list[ParamSpec]] = {')
    # 无参数工具
    for n in no_param:
        lines.append(f'    {py_str(n)}: [],')
    # 有参数工具
    for item in with_param:
        tool = item["tool"]
        plist = item["params"]
        specs: list[str] = []
        for p in plist:
            name = p["name"]
            ptype = p["type"]
            required = p["required"]
            enum = p.get("enum")
            if ptype in ("array", "object"):
                # 跳过复杂类型，交给 handler
                continue
            parts: list[str] = []
            parts.append(py_str(name))
            type_map = {"integer": "integer", "number": "number", "boolean": "boolean", "string": "string"}
            parts.append(f'"{type_map.get(ptype, "string")}"')
            if required:
                parts.append("required=True")
            else:
                parts.append("required=False")
                # default
                if ptype == "boolean":
                    default = "True" if (tool, name) in _TRUE_DEFAULTS else "False"
                    parts.append(f"default={default}")
            if ptype == "string":
                # 内容类参数放宽 max_len，标识类参数保持 200
                key = (tool, name)
                if key in _LONG_TEXT_MAXLEN:
                    ml = _LONG_TEXT_MAXLEN[key]
                    if ml is not None:
                        parts.append(f"max_len={ml}")
                else:
                    parts.append("max_len=200")
            if enum:
                parts.append(f"enum={py_tuple(enum)}")
            specs.append("        ParamSpec(" + ", ".join(parts) + "),")
        if not specs:
            lines.append(f'    {py_str(tool)}: [],')
        else:
            lines.append(f'    {py_str(tool)}: [')
            lines.extend(specs)
            lines.append("    ],")

    lines.append("}")

    out_path = Path(__file__).resolve().parent / "_tool_specs.py"
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"已生成: {out_path}")
    print(f"无参数工具: {len(no_param)}, 有参数工具: {len(with_param)}")


if __name__ == "__main__":
    main()
