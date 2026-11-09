# -*- coding: utf-8 -*-
"""QQ 独立身份管理。

原则：
1. QQ 默认使用自己的身份空间：person_id = "qq_<QQ号>"，绝不自动与抖音观众或任何第三方平台合并。
2. 保留明确的人工跨平台绑定能力（超管 /bind 写入 identity.json，matched_by="manual"）。
3. 不再扫描任何直播观众 profiles，也不进行模糊同名匹配，防止记忆污染。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = ["IdentityMap"]


class IdentityMap:
    def __init__(self, data_dir: Path | str, profiles_dir: Path | str | None = None,
                 profile_ttl_sec: float = 4 * 86400.0):
        self._data_dir = Path(data_dir)
        self._path = self._data_dir / "identity.json"
        self._lock = threading.RLock()
        self._reg: dict[str, dict] = {}
        self._load()

    # -- 对外 ------------------------------------------------------------
    def resolve(self, qq_id: str, nickname: str = "",
                card: str = "") -> tuple[str, str]:
        """返回 (person_id, matched_by)。person_id 用作记忆的 user_id。

        规则：
        - 仅当存在明确的人工手动绑定时，返回绑定的 anchor（matched_by="manual"）；
        - 默认完全隔离在 QQ 独立身份空间：person_id = f"qq_{qq_id}"。
        """
        qq_id = str(qq_id or "").strip()
        if not qq_id:
            return "", ""
        with self._lock:
            entry = self._reg.get(qq_id)
            if entry and entry.get("anchor"):
                matched_by = str(entry.get("matched_by") or "manual")
                return str(entry["anchor"]), matched_by
            return f"qq_{qq_id}", ""

    def bind(self, qq_id: str, anchor: str = "") -> tuple[bool, str]:
        """人工手动绑定/解绑/查询接口。"""
        qq_id = str(qq_id or "").strip()
        anchor = str(anchor or "").strip()
        if not qq_id:
            return False, "缺少 QQ 号"

        if anchor == "unbind":
            with self._lock:
                old = self._reg.pop(qq_id, None)
                self._save()
            return True, (
                f"已解绑 QQ {qq_id}"
                + (f"（原绑定 {old.get('anchor')}）" if old else "（原本就没有绑定）")
            )

        if not anchor:
            with self._lock:
                entry = self._reg.get(qq_id)
            if entry and entry.get("anchor"):
                return True, (
                    f"QQ {qq_id} 绑定在身份 {entry.get('anchor')}"
                    f"（{entry.get('matched_by', 'manual')}）"
                )
            return True, f"QQ {qq_id} 目前没有绑定，使用独立身份 qq_{qq_id}"

        with self._lock:
            self._reg[qq_id] = {
                "anchor": anchor,
                "matched_by": "manual",
                "ts": time.time(),
            }
            self._save()
        return True, f"QQ {qq_id} 已手动绑定到外部身份 {anchor}。"

    def bindings(self) -> dict:
        with self._lock:
            return dict(self._reg)

    # -- 内部持久化 ------------------------------------------------------
    def _load(self) -> None:
        try:
            if not self._path.exists():
                return
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._reg = {
                    str(k): v for k, v in data.items() if isinstance(v, dict)
                }
        except Exception:
            logger.warning("读取 identity.json 失败，忽略旧缓存", exc_info=True)

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(self._reg, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self._path)
        except OSError:
            logger.warning("identity.json 写入失败", exc_info=True)
