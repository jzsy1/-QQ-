"""代码自修复工具 — 让 Agent 能读取/修改/重载 kekesi 自身代码。

安全约束：
- 只能操作 kekesi/ 目录下的 .py 文件
- write_code_file 自动备份 .bak + ast.parse 语法校验
- reload_module 用 importlib.reload，仅对已加载的模块生效
- 核心调度文件（agent_loop/r1_executor/agent）禁止写入
"""

from __future__ import annotations

import ast
import importlib
import logging
import shutil
import sys
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_KEKESI_ROOT = Path(__file__).resolve().parent.parent

_FORBIDDEN_WRITE = {
    "agent_loop.py",
    "r1_executor.py",
    "agent.py",
    "brain.py",
    "router.py",
    "tools_policy.py",
    "admin_server.py",
    "api/server.py",
    "mcp/bridge.py",
    "daemon.py",
    "daemon_service.py",
}

# 允许写入的子目录白名单（只放 tools/ 和 memory/ 让工具实现可修）
_WRITABLE_SUBDIRS = ("tools", "memory")


def _resolve_safe_path(rel_path: str) -> Path | None:
    """把相对路径解析到 kekesi/ 内的绝对路径，越界返回 None。

    接受：
      - tools/winops.py
      - memory/learning.py
      - winops.py（等价于 tools/winops.py，兼容简写）
      - kekesi/tools/winops.py（自动去掉 kekesi/ 前缀）
    """
    if not rel_path or not isinstance(rel_path, str):
        return None
    p = rel_path.strip().replace("\\", "/").lstrip("./")
    # 去掉可能的 kekesi/ 前缀
    if p.startswith("kekesi/"):
        p = p[len("kekesi/"):]
    # 防穿越
    if ".." in p.split("/"):
        return None
    # 必须是 .py
    if not p.endswith(".py"):
        return None

    # 直接拼
    cand = _KEKESI_ROOT / p
    if cand.is_file():
        return cand

    # 简写：tools/xxx.py
    cand2 = _KEKESI_ROOT / "tools" / p
    if cand2.is_file():
        return cand2

    # 简写：memory/xxx.py
    cand3 = _KEKESI_ROOT / "memory" / p
    if cand3.is_file():
        return cand3

    return None


def _is_writable(target: Path) -> tuple[bool, str]:
    """判定目标文件是否允许写入。"""
    rel = target.relative_to(_KEKESI_ROOT).as_posix()
    name = target.name
    if name in _FORBIDDEN_WRITE:
        return False, f"核心调度文件 {name} 禁止写入（改了也无法 reload 生效）"
    # 允许 tools/ 和 memory/ 子目录
    top = rel.split("/", 1)[0]
    if top in _WRITABLE_SUBDIRS:
        return True, ""
    return False, f"仅允许修改 {_WRITABLE_SUBDIRS} 下的文件，{rel} 不在允许范围"


def list_kekesi_files() -> tuple[bool, str]:
    """列出 kekesi/ 下所有 .py 文件及行数。"""
    try:
        lines: list[str] = ["kekesi/ 下 .py 文件清单（按路径排序）：", ""]
        total = 0
        files = sorted(_KEKESI_ROOT.rglob("*.py"))
        # 排除 __pycache__
        files = [f for f in files if "__pycache__" not in f.parts]
        for f in files:
            try:
                rel = f.relative_to(_KEKESI_ROOT).as_posix()
                n = sum(1 for _ in open(f, encoding="utf-8", errors="ignore"))
            except Exception:
                continue
            writable = "可写" if _is_writable(f)[0] else "只读"
            lines.append(f"  {writable}  {n:5d} 行  {rel}")
            total += n
        lines.append("")
        lines.append(f"合计 {len(files)} 个文件，{total} 行")
        return True, "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        logger.warning("list_kekesi_files 异常: %s", e)
        return False, f"列出文件失败: {e}"


def read_code_file(rel_path: str, start_line: int = 1, end_line: int = 0) -> tuple[bool, str]:
    """读取 kekesi/ 下的 .py 文件内容，带行号。

    start_line/end_line 从 1 开始，end_line=0 表示读到末尾。
    """
    target = _resolve_safe_path(rel_path)
    if target is None:
        return False, f"文件不存在或越界：{rel_path}（仅限 kekesi/ 目录下的 .py）"

    try:
        text = target.read_text(encoding="utf-8", errors="replace")
        all_lines = text.splitlines()
        total = len(all_lines)
        s = max(1, int(start_line))
        e = int(end_line) if end_line and end_line > 0 else total
        e = min(e, total)
        if s > total:
            return False, f"start_line={s} 超出文件总行数 {total}"
        sliced = all_lines[s - 1:e]
        out = [f"文件: {target.relative_to(_KEKESI_ROOT).as_posix()}  共 {total} 行  显示 {s}-{e}"]
        out.append("")
        for i, line in enumerate(sliced, start=s):
            out.append(f"{i:5d}  {line}")
        return True, "\n".join(out)
    except Exception as e:  # noqa: BLE001
        logger.warning("read_code_file 异常: %s", e)
        return False, f"读取失败: {e}"


def write_code_file(rel_path: str, content: str, create: bool = False) -> tuple[bool, str]:
    """写入 kekesi/ 下的 .py 文件（自动备份 .bak + ast 语法校验）。

    安全约束：
    - 仅允许 tools/ 和 memory/ 子目录
    - 禁止核心调度文件
    - 写入前自动备份为 .bak（带时间戳）
    - ast.parse 校验语法，失败拒绝写入
    - create=False 时文件必须已存在
    """
    # 先尝试解析路径（已存在文件）
    target = _resolve_safe_path(rel_path)
    if target is None:
        if not create:
            return False, f"文件不存在：{rel_path}（如需新建请设 create=true，且路径必须在 tools/ 或 memory/ 下）"
        # 新建场景：手动构造路径并校验
        p = rel_path.strip().replace("\\", "/").lstrip("./")
        if ".." in p.split("/") or not p.endswith(".py"):
            return False, f"非法路径：{rel_path}"
        cand = _KEKESI_ROOT / p
        top = p.split("/", 1)[0]
        if top not in _WRITABLE_SUBDIRS:
            return False, f"仅允许在 {_WRITABLE_SUBDIRS} 下新建文件"
        target = cand
    else:
        # 已存在文件，校验可写
        ok, reason = _is_writable(target)
        if not ok:
            return False, reason

    if not content or not content.strip():
        return False, "content 为空"

    # 语法校验
    try:
        ast.parse(content)
    except SyntaxError as e:
        return False, f"语法错误，拒绝写入：{e.msg} (line {e.lineno})\n请修正后重试"

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # 备份已存在文件
        if target.exists():
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            bak = target.with_suffix(target.suffix + f".{ts}.bak")
            shutil.copy2(target, bak)
            backup_info = f"已备份原文件为 {bak.name}"
        else:
            backup_info = "新建文件"

        target.write_text(content, encoding="utf-8")
        rel = target.relative_to(_KEKESI_ROOT).as_posix()
        line_count = content.count("\n") + 1
        return True, f"写入成功：{rel}（{line_count} 行，{backup_info}）"
    except Exception as e:  # noqa: BLE001
        logger.warning("write_code_file 异常: %s", e)
        return False, f"写入失败: {e}"


def reload_module(module_name: str) -> tuple[bool, str]:
    """重载已加载的 Python 模块，让代码修改立即生效。

    module_name 支持两种形式：
    - 完整 dotted：kekesi.tools.winops
    - 简写（自动补前缀）：winops → kekesi.tools.winops
    """
    if not module_name or not isinstance(module_name, str):
        return False, "module_name 为空"

    name = module_name.strip()
    # 简写补全
    if not name.startswith("kekesi."):
        candidates = [
            f"kekesi.tools.{name}",
            f"kekesi.memory.{name}",
            f"kekesi.{name}",
        ]
        found = None
        for c in candidates:
            if c in sys.modules:
                found = c
                break
        if not found:
            return False, f"模块未加载（无法 reload 未导入的模块）：{name}，尝试过 {candidates}"
        name = found

    if name not in sys.modules:
        return False, f"模块未加载：{name}（reload 只对已 import 的模块生效）"

    try:
        mod = sys.modules[name]
        importlib.reload(mod)
        return True, f"已重载模块：{name}"
    except Exception as e:  # noqa: BLE001
        logger.warning("reload_module 异常: %s", e)
        return False, f"重载失败：{name}: {e}"


def find_tool_definition(func_name: str) -> tuple[bool, str]:
    """查找工具定义/实现位置，辅助 R3 定位 BUG。

    在 registry.py 和 tools/executor.py 中搜索 func_name。
    """
    if not func_name:
        return False, "func_name 为空"

    try:
        registry_path = _KEKESI_ROOT / "tools" / "registry.py"
        executor_path = _KEKESI_ROOT / "tools" / "executor.py"
        hits: list[str] = []

        for label, path in [("registry", registry_path), ("executor", executor_path)]:
            if not path.exists():
                continue
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            for i, line in enumerate(lines, 1):
                # 匹配 "func_name" 或 func_name 出现的位置
                if f'"{func_name}"' in line or f"'{func_name}'" in line or f"== \"{func_name}\"" in line:
                    hits.append(f"  [{label}] {path.relative_to(_KEKESI_ROOT).as_posix()}:{i}  {line.strip()[:100]}")
        if not hits:
            return False, f"未找到工具 {func_name} 的定义/调度位置"
        return True, f"工具 {func_name} 出现在以下位置：\n" + "\n".join(hits)
    except Exception as e:  # noqa: BLE001
        logger.warning("find_tool_definition 异常: %s", e)
        return False, f"查找失败: {e}"
