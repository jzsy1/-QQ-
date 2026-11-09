# -*- coding: utf-8 -*-
"""记忆命名空间：算法共用，业务数据物理隔离。

每个接入层只拿到自己的 root：
- QQ：skills/im/qq/memory/，namespace="qq"
- 其他：每个插件独立 memory/，namespace="<name>"

root 下的 namespace.json 会记录归属；同一个 root 被另一个命名空间打开时直接
拒绝，防止配置写错导致两边数据互相污染。迁移脚本只做“复制 + 校验 + 可回滚”，
不会删除源数据。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

__all__ = ["MemoryNamespace", "NamespaceMismatch"]

_NAMESPACE_FILE = "namespace.json"


class NamespaceMismatch(RuntimeError):
    """记忆目录已属于另一个命名空间。"""


@dataclass(frozen=True)
class MemoryNamespace:
    name: str
    root: Path

    @classmethod
    def for_plugin(cls, plugin_dir: Path | str, name: str,
                   subdir: str = "memory") -> "MemoryNamespace":
        return cls(name=str(name), root=Path(plugin_dir) / subdir)

    def ensure(self) -> Path:
        """创建目录并写入/校验归属标记，返回 root。"""
        self.root.mkdir(parents=True, exist_ok=True)
        marker = self.root / _NAMESPACE_FILE
        if marker.exists():
            try:
                data = json.loads(marker.read_text(encoding="utf-8"))
            except Exception:
                data = {}
            owner = str((data or {}).get("name") or "").strip()
            if owner and owner != self.name:
                raise NamespaceMismatch(
                    f"记忆目录 {self.root} 属于命名空间 {owner!r}，"
                    f"不能当作 {self.name!r} 使用"
                )
        else:
            marker.write_text(
                json.dumps(
                    {"name": self.name, "root": str(self.root.resolve())},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        return self.root
