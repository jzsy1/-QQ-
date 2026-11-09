# -*- coding: utf-8 -*-
"""QQ 大脑解耦端到端测试套件。

覆盖验收条件：
1. QQ 独立调用 LLM
2. QQ 生成正常回复与文本清洗
3. QQ 独立身份系统（默认隔离，人工绑定生效）
4. QQ 独立记忆系统（命名空间隔离，防污染，存取检索）
5. QQ 消息收发全链路（模拟 OneBot/NapCat 消息）
6. 零运行时 douyin_live 依赖与零直播工具泄漏
"""

import json
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SKILLS_DIR = _PROJECT_ROOT / "skills"
if str(_SKILLS_DIR) not in sys.path:
    sys.path.insert(0, str(_SKILLS_DIR))
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from kekesi_common.companion_memory import CompanionMemory
from kekesi_common.namespaces import MemoryNamespace, NamespaceMismatch
from kekesi_common.reply_text import FilterConfig, clean_reply, fallback_line, salvage_parenthetical
from kekesi_common.personas import resolve_system_prompt
from kekesi_common.llm import SimpleBrain, SimpleBrainConfig
import im.qq.brain as qq_brain
import im.qq.service as qq_service
from im.qq.config import QQConfig
from im.qq.identity import IdentityMap


class DummyMockBrain:
    """模拟 LLM Brain。"""
    def __init__(self, reply_text: str = "我在呢，今天天气不错。"):
        self.reply_text = reply_text
        self.config = SimpleBrainConfig()
        self.received_messages = []

    def reload_if_changed(self):
        return False

    def chat_stream_messages(self, messages, cancel_check=None):
        self.received_messages = list(messages)
        if callable(cancel_check) and cancel_check():
            return ""
        return self.reply_text

    def chat_once(self, messages, tools=None, tool_choice=None):
        self.received_messages = list(messages)
        return {"content": self.reply_text}


class TestQQDecoupled(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="test_qq_decoupled_")
        self.mem_dir = Path(self.temp_dir) / "qq_memory"
        self.data_dir = Path(self.temp_dir) / "qq_data"

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    # 1. 验证 QQ 独立身份系统
    def test_qq_identity_isolation_and_manual_bind(self):
        ident = IdentityMap(data_dir=self.data_dir)

        # 默认使用 QQ 自身身份空间，即使传入可能同名的昵称，也绝不自动认同
        pid1, match1 = ident.resolve("10001", nickname="观众张三", card="张三")
        self.assertEqual(pid1, "qq_10001")
        self.assertEqual(match1, "")

        # 查询未绑定状态
        ok, msg = ident.bind("10001", "")
        self.assertTrue(ok)
        self.assertIn("qq_10001", msg)

        # 手动绑定
        ok, msg = ident.bind("10001", "douyin_user_999")
        self.assertTrue(ok)
        self.assertIn("已手动绑定", msg)

        # 再次解析已绑定用户
        pid2, match2 = ident.resolve("10001", nickname="任意外号")
        self.assertEqual(pid2, "douyin_user_999")
        self.assertEqual(match2, "manual")

        # 手动解绑
        ok, msg = ident.bind("10001", "unbind")
        self.assertTrue(ok)
        self.assertIn("已解绑", msg)

        # 解绑后恢复为默认 qq_10001
        pid3, match3 = ident.resolve("10001")
        self.assertEqual(pid3, "qq_10001")

    # 2. 验证 QQ 独立记忆与命名空间防污染
    def test_qq_memory_namespace_and_operations(self):
        # 初始化 QQ 命名空间记忆
        mem = CompanionMemory(root=self.mem_dir, namespace="qq")
        self.assertEqual(mem.namespace, "qq")
        self.assertTrue((self.mem_dir / "namespace.json").exists())

        # 验证防污染：用 live 命名空间打开 QQ 记忆目录必须抛出 NamespaceMismatch 异常
        with self.assertRaises(NamespaceMismatch):
            CompanionMemory(root=self.mem_dir, namespace="live")

        # 记录用户消息
        mem.record_viewer_event(
            user_id="qq_10001",
            user_name="小明",
            kind="chat",
            content="我喜欢喝奶茶",
            session_id="qq_2026-11-09",
        )

        # 记录 AI 回复
        mem.record_viewer_reply(
            user_id="qq_10001",
            user_name="小明",
            reply="奶茶好喝，但不要喝太多哦",
            session_id="qq_2026-11-09",
        )

        # 记忆检索上下文
        ctx = mem.build_context(user_id="qq_10001", query="奶茶", user_name="小明")
        self.assertIn("奶茶", ctx)
        self.assertIn("喜欢喝奶茶", ctx)

        # 验证事实记录到专用 SQLite
        self.assertIsNotNone(mem.db)
        mem.db.add_fact("qq_10001", "爱喝全糖奶茶", category="preference")
        facts = mem.db.list_facts("qq_10001")
        self.assertTrue(any("全糖奶茶" in f["fact"] for f in facts))

    # 3. 验证回复生成、清洗与提示词组装
    def test_reply_generation_and_cleaning(self):
        mock_brain = DummyMockBrain(
            reply_text="[动作:微笑] 好的主人，这是链接 https://test.com ，鱼片在呢。"
        )
        cfg = QQConfig(
            agent_id="keks",
            max_reply_chars=60,
            wake_words=("小金鱼",),
            owner_names=("鱼片",),
        )

        # 清洗测试：过滤动作词、URL、顺嘴喊错的主人昵称
        cleaned = clean_reply(
            "[动作:微笑] 好的主人，这是链接 https://test.com ，鱼片，我在呢。",
            max_chars=60,
            filt=FilterConfig(owner_names=("鱼片",)),
            is_owner=False,
        )
        self.assertNotIn("https://", cleaned)
        self.assertNotIn("[动作:微笑]", cleaned)
        self.assertNotIn("鱼片", cleaned)  # 非主人说话，自动剔除鱼片称呼

        # 普通群友测试
        batch = [{
            "user_name": "群友A",
            "text": "今天吃啥",
            "person_id": "qq_10001",
            "is_owner": False,
        }]

        with patch.object(qq_brain, "get_brain", return_value=mock_brain):
            reply, err = qq_brain.generate_reply(cfg, memory=None, batch=batch)
            self.assertEqual(err, "")
            self.assertTrue(len(reply) > 0)
            self.assertNotIn("https://", reply)

        # 主人发言测试
        mock_brain.reply_text = "主人您好，今天给您准备了鱼汤。"
        batch_owner = [{
            "user_name": "鱼片",
            "text": "鱼鱼在吗",
            "person_id": "qq_owner",
            "is_owner": True,
        }]
        with patch.object(qq_brain, "get_brain", return_value=mock_brain):
            reply_owner, err = qq_brain.generate_reply(cfg, memory=None, batch=batch_owner)
            self.assertEqual(err, "")
            self.assertIn("主人", reply_owner)

    # 4. 验证 QQService 全链路（收发消息、记忆写入、状态返回）
    def test_qq_service_full_flow(self):
        sent_messages = []

        class MockTransport:
            def __init__(self, *args, on_event=None, **kwargs):
                self.on_event = on_event
                self.connected = False
                self.last_error = ""

            def start(self):
                self.connected = True

            def stop(self):
                self.connected = False

            def call_action(self, action, params, timeout=10.0):
                sent_messages.append((action, params))
                return True, "ok"

            def get_login_info(self):
                return {"user_id": "99999"}

        cfg = QQConfig(
            enabled=True,
            ws_url="ws://127.0.0.1:3001",
            owner_qqs=("10001",),
            memory_dir=str(self.mem_dir),
            memory_namespace="qq",
            batch_ms=10.0,
            reply_gap_ms=0.0,
        )

        mock_brain = DummyMockBrain(reply_text="收到，我是鱼鱼。")

        service = qq_service.QQService()
        with patch("im.qq.service.load_config", return_value=cfg), \
             patch("im.qq.service.OneBotTransport", MockTransport), \
             patch.object(qq_brain, "get_brain", return_value=mock_brain):

            ok, msg = service.start()
            self.assertTrue(ok)
            self.assertIn("独立 QQ 大脑与记忆命名空间", msg)

            # 模拟收到群聊 @ 消息
            event_payload = {
                "post_type": "message",
                "message_type": "group",
                "sub_type": "normal",
                "message_id": 1001,
                "group_id": 88888,
                "user_id": 12345,
                "sender": {"nickname": "小红", "card": ""},
                "raw_message": "[CQ:at,qq=99999] 鱼鱼早上好",
                "message": [
                    {"type": "at", "data": {"qq": "99999"}},
                    {"type": "text", "data": {"text": " 鱼鱼早上好"}},
                ],
                "self_id": 99999,
                "time": int(time.time()),
            }

            service._on_event(event_payload)

            # 等待攒批与回复线程处理
            t0 = time.time()
            while time.time() - t0 < 2.0 and not sent_messages:
                time.sleep(0.05)

            # 验证消息已通过 Transport 发送出去
            self.assertTrue(len(sent_messages) > 0)
            action, params = sent_messages[-1]
            self.assertEqual(action, "send_group_msg")
            self.assertEqual(str(params.get("group_id")), "88888")
            self.assertIn("收到，我是鱼鱼", str(params.get("message")))

            # 验证状态输出正常
            status_text = service.status_text()
            self.assertIn("running", status_text)
            self.assertIn("回复 1", status_text)

            # 停止服务
            stop_ok, stop_msg = service.stop()
            self.assertTrue(stop_ok)

    # 5. 验证绝对无 douyin_live 运行时加载
    def test_zero_douyin_live_runtime_dependency(self):
        # 遍历已加载模块，确保无任何以 douyin_live 或 _kekesi_skill_douyin_live 命名的模块
        loaded_douyin = [
            mod for mod in sys.modules
            if "douyin_live" in mod or "_kekesi_skill_douyin_live" in mod
        ]
        self.assertEqual(loaded_douyin, [], f"发现残留的直播运行时模块: {loaded_douyin}")


if __name__ == "__main__":
    unittest.main()
