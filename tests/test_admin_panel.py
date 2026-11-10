# -*- coding: utf-8 -*-
"""测试 Web 管理控制后台 API。"""

import json
import shutil
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SKILLS_DIR = PROJECT_ROOT / "skills"
if str(SKILLS_DIR) not in sys.path:
    sys.path.insert(0, str(SKILLS_DIR))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from admin_panel.server import start_admin_server_background


class TestAdminPanel(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.test_port = 18889
        cfg_file = PROJECT_ROOT / "config.yaml"
        cls.cfg_backup = cfg_file.read_text(encoding="utf-8") if cfg_file.exists() else None
        cls.server_thread = start_admin_server_background(port=cls.test_port)
        time.sleep(0.5)

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "cfg_backup", None) is not None:
            (PROJECT_ROOT / "config.yaml").write_text(cls.cfg_backup, encoding="utf-8")

    def _get(self, path: str):
        url = f"http://127.0.0.1:{self.test_port}{path}"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            data = resp.read()
            return resp.status, data

    def _post(self, path: str, payload: dict):
        url = f"http://127.0.0.1:{self.test_port}{path}"
        data_bytes = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data_bytes,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))

    def test_serve_html(self):
        status, data = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn("QQ 大脑控制后台".encode("utf-8"), data)

    def test_api_status(self):
        status, data = self._get("/api/status")
        self.assertEqual(status, 200)
        parsed = json.loads(data.decode("utf-8"))
        self.assertTrue(parsed.get("ok"))
        self.assertIn("running", parsed)

    def test_api_config(self):
        status, data = self._get("/api/config")
        self.assertEqual(status, 200)
        parsed = json.loads(data.decode("utf-8"))
        self.assertTrue(parsed.get("ok"))
        self.assertIn("brain", parsed)
        self.assertIn("qq", parsed)

    def test_api_config_persona(self):
        status, res = self._post("/api/config/persona", {
            "agent_id": "test_agent",
            "system_prompt": "测试提示词",
            "wake_words": ["测试小金鱼"],
            "owner_names": ["测试主人"],
        })
        self.assertEqual(status, 200)
        self.assertTrue(res.get("ok"))

        # 验证读取配置已生效
        status, data = self._get("/api/config")
        parsed = json.loads(data.decode("utf-8"))
        qq_cfg = parsed.get("qq", {})
        self.assertEqual(qq_cfg.get("agent_id"), "test_agent")
        self.assertEqual(qq_cfg.get("system_prompt"), "测试提示词")

    def test_api_llm_test_mock(self):
        with patch("kekesi_common.llm.SimpleBrain.chat_once", return_value={"content": "测试回复正常"}):
            status, res = self._post("/api/llm/test", {"prompt": "ping"})
            self.assertEqual(status, 200)
            self.assertTrue(res.get("ok"))
            self.assertEqual(res.get("reply"), "测试回复正常")


if __name__ == "__main__":
    unittest.main()
