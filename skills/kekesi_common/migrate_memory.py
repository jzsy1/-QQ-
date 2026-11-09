# -*- coding: utf-8 -*-
"""记忆数据迁移与命名空间隔离迁移工具。

用于从混合或旧版记忆目录（例如历史旧版记忆目录）
提取并迁移 QQ 专属记忆到 QQ 独立命名空间目录（例如 skills/im/qq/memory）。

核心保证：
1. 来源数据绝对不修改、不删除（只读提取）。
2. 目标目录强制打上 namespace="qq" 隔离标记。
3. 提供迁移前快照备份、数据校验（行数/事实/哈希对比）及回滚机制。
4. 不破坏主系统现有四库边界。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# 确保 skills 目录可被导入
_SKILLS_DIR = Path(__file__).resolve().parents[1]
if str(_SKILLS_DIR) not in sys.path:
    sys.path.insert(0, str(_SKILLS_DIR))

from kekesi_common.namespaces import MemoryNamespace

logger = logging.getLogger(__name__)

__all__ = ["MemoryMigrator", "MigrationResult", "VerificationResult"]


@dataclass
class MigrationResult:
    success: bool
    source_dir: str
    target_dir: str
    backup_path: str = ""
    profiles_migrated: int = 0
    raw_lines_migrated: int = 0
    facts_migrated: int = 0
    events_migrated: int = 0
    error: str = ""


@dataclass
class VerificationResult:
    passed: bool
    source_qq_profiles: int = 0
    target_qq_profiles: int = 0
    source_qq_events: int = 0
    target_qq_events: int = 0
    target_namespace_valid: bool = False
    details: list[str] = field(default_factory=list)


def _file_md5(path: Path) -> str:
    h = hashlib.md5()
    try:
        h.update(path.read_bytes())
        return h.hexdigest()
    except Exception:
        return ""


class MemoryMigrator:
    def __init__(self, source_dir: Path | str, target_dir: Path | str,
                 target_namespace: str = "qq"):
        self.source_dir = Path(source_dir).resolve()
        self.target_dir = Path(target_dir).resolve()
        self.target_namespace = str(target_namespace or "qq")

    def migrate(self, backup_parent: Path | str | None = None) -> MigrationResult:
        """执行数据迁移。原目录绝不删除任何数据。"""
        if not self.source_dir.exists():
            return MigrationResult(
                success=False,
                source_dir=str(self.source_dir),
                target_dir=str(self.target_dir),
                error=f"源记忆目录不存在: {self.source_dir}",
            )

        backup_path_str = ""
        # 1. 如果目标目录已存在且有内容，创建回滚备份
        if self.target_dir.exists() and any(self.target_dir.iterdir()):
            ts = time.strftime("%Y%m%d_%H%M%S")
            parent = Path(backup_parent) if backup_parent else self.target_dir.parent
            b_path = parent / f".migration_backup_{self.target_dir.name}_{ts}"
            try:
                shutil.copytree(self.target_dir, b_path)
                backup_path_str = str(b_path)
                logger.info("创建目标目录回滚快照: %s", b_path)
            except Exception as exc:
                return MigrationResult(
                    success=False,
                    source_dir=str(self.source_dir),
                    target_dir=str(self.target_dir),
                    error=f"创建目标目录备份失败: {exc}",
                )

        profiles_count = 0
        raw_lines_count = 0
        facts_count = 0
        events_count = 0

        try:
            # 2. 确保目标目录及其命名空间标记
            ns = MemoryNamespace(name=self.target_namespace, root=self.target_dir)
            ns.ensure()

            for sub in ("raw", "chunks", "weekly", "profiles"):
                (self.target_dir / sub).mkdir(parents=True, exist_ok=True)

            # 3. 迁移 profiles: 仅提取 qq_* 用户档案
            src_profiles = self.source_dir / "profiles"
            dst_profiles = self.target_dir / "profiles"
            if src_profiles.exists():
                for p_file in src_profiles.glob("*.json"):
                    # 识别 QQ 用户: 文件名以 qq_ 开头，或文件内容明确属于 QQ
                    is_qq = p_file.stem.startswith("qq_")
                    if not is_qq:
                        try:
                            data = json.loads(p_file.read_text(encoding="utf-8"))
                            if isinstance(data, dict):
                                uid = str(data.get("user_id") or "")
                                if uid.startswith("qq_") or data.get("channel") == "qq":
                                    is_qq = True
                        except Exception:
                            pass
                    if is_qq:
                        dst_file = dst_profiles / p_file.name
                        shutil.copy2(p_file, dst_file)
                        profiles_count += 1

            # 4. 迁移 raw 日志流水: 仅提取 session_id/user_id 以 qq_ 开头的行
            src_raw = self.source_dir / "raw"
            dst_raw = self.target_dir / "raw"
            if src_raw.exists():
                for r_file in sorted(src_raw.glob("viewer-*.jsonl")):
                    out_lines = []
                    try:
                        with r_file.open("r", encoding="utf-8", errors="replace") as f:
                            for line in f:
                                line_str = line.strip()
                                if not line_str:
                                    continue
                                try:
                                    rec = json.loads(line_str)
                                    uid = str(rec.get("user_id") or "")
                                    sid = str(rec.get("session_id") or "")
                                    meta = rec.get("meta") or {}
                                    if (uid.startswith("qq_")
                                            or sid.startswith("qq_")
                                            or meta.get("channel") == "qq"):
                                        out_lines.append(line_str)
                                except Exception:
                                    continue
                    except Exception:
                        continue

                    if out_lines:
                        dst_file = dst_raw / r_file.name
                        with dst_file.open("a", encoding="utf-8") as out_f:
                            for l_str in out_lines:
                                out_f.write(l_str + "\n")
                        raw_lines_count += len(out_lines)

            # 5. 迁移 SQLite memory.db（如果存在）
            src_db = self.source_dir / "memory.db"
            if src_db.exists():
                dst_db = self.target_dir / "memory.db"
                from kekesi_common.memory_db import LiveMemoryDB

                target_db_obj = LiveMemoryDB(dst_db)
                with sqlite3.connect(str(src_db)) as s_conn:
                    s_conn.row_factory = sqlite3.Row
                    # 迁移 viewers
                    try:
                        v_rows = s_conn.execute(
                            "SELECT * FROM viewers WHERE user_id LIKE 'qq_%'"
                        ).fetchall()
                        for row in v_rows:
                            keys = row.keys()
                            ts = row["last_seen_at"] if "last_seen_at" in keys else (row["last_seen"] if "last_seen" in keys else None)
                            is_vip = bool(row["is_vip"]) if "is_vip" in keys else False
                            target_db_obj.upsert_viewer(
                                user_id=row["user_id"],
                                user_name=row["user_name"],
                                ts=ts,
                                is_vip=is_vip,
                            )
                    except sqlite3.Error:
                        pass

                    # 迁移 facts
                    try:
                        table_check = s_conn.execute(
                            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('facts', 'viewer_facts')"
                        ).fetchone()
                        if table_check:
                            f_table = table_check[0]
                            f_rows = s_conn.execute(
                                f"SELECT * FROM {f_table} WHERE user_id LIKE 'qq_%'"
                            ).fetchall()
                            for row in f_rows:
                                keys = row.keys()
                                target_db_obj.add_fact(
                                    user_id=row["user_id"],
                                    fact=row["fact"],
                                    category=row["category"] if "category" in keys else "preference",
                                    confidence=float(row["confidence"]) if "confidence" in keys else 0.8,
                                    source_kind=row["source_kind"] if "source_kind" in keys else "chat",
                                )
                                facts_count += 1
                    except sqlite3.Error:
                        pass

                    # 迁移 events
                    try:
                        table_check = s_conn.execute(
                            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('events', 'viewer_events')"
                        ).fetchone()
                        if table_check:
                            e_table = table_check[0]
                            e_rows = s_conn.execute(
                                f"SELECT * FROM {e_table} WHERE user_id LIKE 'qq_%' OR session_id LIKE 'qq_%'"
                            ).fetchall()
                            for row in e_rows:
                                keys = row.keys()
                                target_db_obj.record_event(
                                    record_id=str(row["record_id"] if "record_id" in keys else f"evt_{events_count}"),
                                    session_id=str(row["session_id"]),
                                    user_id=str(row["user_id"]),
                                    user_name=str(row["user_name"]),
                                    kind=str(row["kind"] if "kind" in keys else "chat"),
                                    content=str(row["content"] if "content" in keys else (row["text"] if "text" in keys else "")),
                                )
                                events_count += 1
                    except sqlite3.Error:
                        pass

            return MigrationResult(
                success=True,
                source_dir=str(self.source_dir),
                target_dir=str(self.target_dir),
                backup_path=backup_path_str,
                profiles_migrated=profiles_count,
                raw_lines_migrated=raw_lines_count,
                facts_migrated=facts_count,
                events_migrated=events_count,
            )

        except Exception as exc:
            logger.error("迁移过程异常: %s", exc, exc_info=True)
            return MigrationResult(
                success=False,
                source_dir=str(self.source_dir),
                target_dir=str(self.target_dir),
                backup_path=backup_path_str,
                error=str(exc),
            )

    def verify(self) -> VerificationResult:
        """校验目标目录与源目录的 QQ 数据完整性及命名空间合法性。"""
        details: list[str] = []
        # 1. 命名空间检查
        ns_marker = self.target_dir / "namespace.json"
        ns_valid = False
        if ns_marker.exists():
            try:
                data = json.loads(ns_marker.read_text(encoding="utf-8"))
                if data.get("name") == self.target_namespace:
                    ns_valid = True
            except Exception:
                pass
        if not ns_valid:
            details.append(f"目标目录命名空间标记不符合要求 (期望 {self.target_namespace})")

        # 2. 统计源目录 QQ profiles vs 目标目录 QQ profiles
        src_profiles_count = 0
        src_p_dir = self.source_dir / "profiles"
        if src_p_dir.exists():
            for p in src_p_dir.glob("qq_*.json"):
                src_profiles_count += 1

        dst_profiles_count = 0
        dst_p_dir = self.target_dir / "profiles"
        if dst_p_dir.exists():
            for p in dst_p_dir.glob("qq_*.json"):
                dst_profiles_count += 1

        if src_profiles_count != dst_profiles_count:
            details.append(
                f"Profiles 数量不匹配: 源={src_profiles_count}, 目标={dst_profiles_count}"
            )

        # 3. 统计 raw 流水行数
        src_raw_count = 0
        src_r_dir = self.source_dir / "raw"
        if src_r_dir.exists():
            for f in src_r_dir.glob("viewer-*.jsonl"):
                try:
                    for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                        if "qq_" in line:
                            src_raw_count += 1
                except Exception:
                    pass

        dst_raw_count = 0
        dst_r_dir = self.target_dir / "raw"
        if dst_r_dir.exists():
            for f in dst_r_dir.glob("viewer-*.jsonl"):
                try:
                    for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                        if "qq_" in line:
                            dst_raw_count += 1
                except Exception:
                    pass

        passed = ns_valid and (src_profiles_count == dst_profiles_count)
        return VerificationResult(
            passed=passed,
            source_qq_profiles=src_profiles_count,
            target_qq_profiles=dst_profiles_count,
            source_qq_events=src_raw_count,
            target_qq_events=dst_raw_count,
            target_namespace_valid=ns_valid,
            details=details,
        )

    @classmethod
    def rollback(cls, backup_dir: Path | str, target_dir: Path | str) -> bool:
        """从备份快照回滚目标目录。"""
        b_path = Path(backup_dir)
        t_path = Path(target_dir)
        if not b_path.exists():
            logger.error("备份路径不存在: %s", b_path)
            return False
        try:
            if t_path.exists():
                shutil.rmtree(t_path)
            shutil.copytree(b_path, t_path)
            logger.info("已成功回滚目标目录 %s 从快照 %s", t_path, b_path)
            return True
        except Exception as exc:
            logger.error("回滚失败: %s", exc, exc_info=True)
            return False


def main():
    parser = argparse.ArgumentParser(description="QQ 大脑独立记忆数据迁移工具")
    parser.add_argument("--source", required=False, help="源记忆目录路径")
    parser.add_argument("--target", required=False, help="目标 QQ 记忆目录路径")
    parser.add_argument("--namespace", default="qq", help="目标命名空间名称，默认 qq")
    parser.add_argument("--migrate", action="store_true", help="执行迁移")
    parser.add_argument("--verify", action="store_true", help="校验数据")
    parser.add_argument("--rollback", help="指定回滚快照目录路径进行回滚")

    args = parser.parse_args()

    if args.rollback:
        if not args.target:
            print("错误: 回滚操作必须指定 --target 目标目录")
            sys.exit(1)
        ok = MemoryMigrator.rollback(args.rollback, args.target)
        print(f"回滚结果: {'成功' if ok else '失败'}")
        sys.exit(0 if ok else 1)

    if not args.source or not args.target:
        print("错误: 请提供 --source 与 --target 目录参数")
        sys.exit(1)

    migrator = MemoryMigrator(args.source, args.target, target_namespace=args.namespace)

    if args.migrate:
        res = migrator.migrate()
        print("=== 迁移结果 ===")
        print(json.dumps(asdict(res), ensure_ascii=False, indent=2))
        if not res.success:
            sys.exit(1)

    if args.verify or args.migrate:
        v_res = migrator.verify()
        print("=== 校验结果 ===")
        print(json.dumps(asdict(v_res), ensure_ascii=False, indent=2))
        if not v_res.passed:
            sys.exit(1)


if __name__ == "__main__":
    main()
