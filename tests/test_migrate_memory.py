# -*- coding: utf-8 -*-
"""测试记忆数据迁移与回滚机制。"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path
import sys

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT / "skills") not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT / "skills"))

from kekesi_common.migrate_memory import MemoryMigrator
from kekesi_common.memory_db import LiveMemoryDB


class TestMemoryMigration(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="test_migration_")
        self.source_dir = Path(self.temp_dir) / "source_live_mem"
        self.target_dir = Path(self.temp_dir) / "target_qq_mem"

        # 构造源目录结构与混合数据
        for sub in ("profiles", "raw"):
            (self.source_dir / sub).mkdir(parents=True, exist_ok=True)

        # 1. 混合 profiles: 既有直播观众，又有 QQ 用户
        (self.source_dir / "profiles" / "douyin_viewer_1.json").write_text(
            json.dumps({"user_id": "douyin_viewer_1", "user_name": "观众小李"}),
            encoding="utf-8",
        )
        (self.source_dir / "profiles" / "qq_123456.json").write_text(
            json.dumps({"user_id": "qq_123456", "user_name": "QQ群友A"}),
            encoding="utf-8",
        )
        (self.source_dir / "profiles" / "qq_789012.json").write_text(
            json.dumps({"user_id": "qq_789012", "user_name": "QQ群友B"}),
            encoding="utf-8",
        )

        # 2. 混合 raw 日志
        raw_lines = [
            json.dumps({"user_id": "douyin_viewer_1", "session_id": "live_2026", "text": "主播好"}),
            json.dumps({"user_id": "qq_123456", "session_id": "qq_2026-11-09", "text": "群里的朋友们好"}),
            json.dumps({"user_id": "qq_789012", "session_id": "qq_2026-11-09", "text": "鱼鱼在吗"}),
        ]
        (self.source_dir / "raw" / "viewer-20261109.jsonl").write_text(
            "\n".join(raw_lines) + "\n", encoding="utf-8"
        )

        # 3. 混合 SQLite 数据库
        db = LiveMemoryDB(self.source_dir / "memory.db")
        db.upsert_viewer("douyin_viewer_1", "观众小李", ts=100.0)
        db.upsert_viewer("qq_123456", "QQ群友A", ts=100.0)
        db.add_fact("qq_123456", "喜欢吃苹果", category="preference", confidence=1.0)
        db.add_fact("douyin_viewer_1", "送了一架飞机", category="gift", confidence=1.0)
        db.record_event("evt_1", "qq_2026", "qq_123456", "QQ群友A", "chat", "群里的朋友们好")
        db.record_event("evt_2", "live_2026", "douyin_viewer_1", "观众小李", "chat", "主播好")

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_migration_and_rollback(self):
        migrator = MemoryMigrator(self.source_dir, self.target_dir, target_namespace="qq")
        res = migrator.migrate()

        self.assertTrue(res.success)
        self.assertEqual(res.profiles_migrated, 2)
        self.assertEqual(res.raw_lines_migrated, 2)
        self.assertEqual(res.facts_migrated, 1)
        self.assertEqual(res.events_migrated, 1)

        # 验证源目录没有被删除或更改
        self.assertTrue((self.source_dir / "profiles" / "douyin_viewer_1.json").exists())
        self.assertTrue((self.source_dir / "profiles" / "qq_123456.json").exists())

        # 验证目标目录仅含 QQ 用户数据
        self.assertFalse((self.target_dir / "profiles" / "douyin_viewer_1.json").exists())
        self.assertTrue((self.target_dir / "profiles" / "qq_123456.json").exists())
        self.assertTrue((self.target_dir / "profiles" / "qq_789012.json").exists())

        # 验证校验结果
        v_res = migrator.verify()
        self.assertTrue(v_res.passed)
        self.assertTrue(v_res.target_namespace_valid)
        self.assertEqual(v_res.target_qq_profiles, 2)

        # 测试回滚功能：先预置一个带旧数据的内容，再迁移，再回滚
        old_marker = self.target_dir / "old_flag.txt"
        old_marker.write_text("old_state", encoding="utf-8")

        res2 = migrator.migrate()
        self.assertTrue(res2.success)
        self.assertTrue(bool(res2.backup_path))
        self.assertTrue(Path(res2.backup_path).exists())

        # 执行回滚
        ok = MemoryMigrator.rollback(res2.backup_path, self.target_dir)
        self.assertTrue(ok)
        self.assertTrue(old_marker.exists())
        self.assertEqual(old_marker.read_text(encoding="utf-8"), "old_state")


if __name__ == "__main__":
    unittest.main()
