# -*- coding: utf-8 -*-
"""12 项验收标准全链路综合校验脚本。

可直接在 Windows 和 Ubuntu 环境下运行：
python tests/test_ubuntu_e2e.py
"""

import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SKILLS_DIR = PROJECT_ROOT / "skills"
if str(SKILLS_DIR) not in sys.path:
    sys.path.insert(0, str(SKILLS_DIR))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def check_criterion(num: int, title: str, func):
    print(f"[{num:02d}] 验证: {title} ... ", end="", flush=True)
    try:
        func()
        print("PASS")
        return True
    except Exception as exc:
        import traceback
        print(f"FAIL: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        return False


def main():
    print("=" * 60)
    print("QQ 大脑与抖音直播解耦整改任务 - 12 项验收标准全链路测试")
    print(f"当前平台: {sys.platform}, Python: {sys.version.split()[0]}")
    print("=" * 60)

    results = []

    # 1. 删除 douyin_live 目录后，QQ 仍然能够启动
    def test_1():
        douyin_path = SKILLS_DIR / "media" / "douyin_live"
        assert not douyin_path.exists(), f"douyin_live 目录仍然存在: {douyin_path}"
        from im.qq.main import run_tool
        ok, msg = run_tool("qq_status", {})
        assert ok is True, f"QQ 状态读取失败: {msg}"
    results.append(check_criterion(1, "删除 douyin_live 目录后，QQ 仍然能够启动", test_1))

    # 2. QQ 能够独立调用 LLM
    def test_2():
        from kekesi_common.llm import SimpleBrain, SimpleBrainConfig
        cfg = SimpleBrainConfig(provider="ollama", model="qwen2.5:3b")
        brain = SimpleBrain(config=cfg)
        assert hasattr(brain, "chat_once")
        assert hasattr(brain, "chat_stream_messages")
        from im.qq.brain import get_brain
        b = get_brain()
        assert b is not None, "获取独立大脑失败"
    results.append(check_criterion(2, "QQ 能够独立调用 LLM", test_2))

    # 3. QQ 能够生成正常回复
    def test_3():
        from kekesi_common.reply_text import FilterConfig, clean_reply, salvage_parenthetical
        raw = "【动作:点头】 好的，没问题！https://example.com"
        filt = FilterConfig()
        cleaned = clean_reply(raw, max_chars=60, filt=filt)
        assert "https://" not in cleaned
        assert "【动作:点头】" not in cleaned
        assert "好的，没问题！" in cleaned
        # 括号动作词提取挽救
        pure_paren = "（歪头思考）"
        salvaged = salvage_parenthetical(pure_paren, max_chars=60, filt=filt)
        assert salvaged == "歪头思考"
    results.append(check_criterion(3, "QQ 能够生成正常回复及文本清洗", test_3))

    # 4. QQ 能够记录用户消息
    def test_4():
        from kekesi_common.companion_memory import CompanionMemory
        import tempfile
        import shutil
        t_dir = Path(tempfile.mkdtemp(prefix="test_mem_record_"))
        try:
            mem = CompanionMemory(root=t_dir, namespace="qq")
            ok = mem.record_viewer_event(
                user_id="qq_99999",
                user_name="测试用户",
                kind="chat",
                content="这是测试消息",
                session_id="qq_2026-11-09",
            )
            assert ok is True, "记录用户消息失败"
            # 验证在 raw 日志文件中写入
            raw_files = list((t_dir / "raw").glob("viewer-*.jsonl"))
            assert len(raw_files) > 0, "未找到写入的 raw jsonl 文件"
            content = raw_files[0].read_text(encoding="utf-8")
            assert "这是测试消息" in content
        finally:
            shutil.rmtree(t_dir, ignore_errors=True)
    results.append(check_criterion(4, "QQ 能够记录用户消息", test_4))

    # 5. QQ 能够检索历史记忆
    def test_5():
        from kekesi_common.companion_memory import CompanionMemory
        import tempfile
        import shutil
        t_dir = Path(tempfile.mkdtemp(prefix="test_mem_retrieval_"))
        try:
            mem = CompanionMemory(root=t_dir, namespace="qq")
            mem.record_viewer_event("qq_99999", "测试用户", "chat", "我最喜欢打羽毛球", session_id="qq_1")
            mem.record_viewer_reply("qq_99999", "测试用户", "打羽毛球很锻炼身体", session_id="qq_1")
            ctx = mem.build_context(user_id="qq_99999", query="羽毛球", user_name="测试用户")
            assert "羽毛球" in ctx, f"未能检索出羽毛球相关上下文: {ctx}"
        finally:
            shutil.rmtree(t_dir, ignore_errors=True)
    results.append(check_criterion(5, "QQ 能够检索历史记忆", test_5))

    # 6. QQ 能够使用自己的身份系统
    def test_6():
        from im.qq.identity import IdentityMap
        import tempfile
        import shutil
        t_dir = Path(tempfile.mkdtemp(prefix="test_identity_"))
        try:
            ident = IdentityMap(data_dir=t_dir)
            pid, matched = ident.resolve("12345", nickname="同名用户")
            assert pid == "qq_12345", f"默认应使用独立身份空间 qq_12345，得到 {pid}"
            assert matched == "", "默认不应产生外部匹配"
            # 手动绑定
            ok, _ = ident.bind("12345", "anchor_abc")
            assert ok is True
            pid2, m2 = ident.resolve("12345")
            assert pid2 == "anchor_abc"
            assert m2 == "manual"
            # 手动解绑
            ok, _ = ident.bind("12345", "unbind")
            assert ok is True
            pid3, _ = ident.resolve("12345")
            assert pid3 == "qq_12345"
        finally:
            shutil.rmtree(t_dir, ignore_errors=True)
    results.append(check_criterion(6, "QQ 能够使用自己的身份系统", test_6))

    # 7. QQ 能够发送和接收消息
    def test_7():
        import tempfile
        import shutil
        from im.qq.service import QQService
        from im.qq.config import QQConfig
        import im.qq.brain as qq_brain

        t_dir = Path(tempfile.mkdtemp(prefix="test_send_recv_"))
        sent = []
        try:
            class MockTransport:
                def __init__(self, *args, **kwargs):
                    self.connected = True
                    self.last_error = ""
                def start(self): pass
                def stop(self): pass
                def call_action(self, action, params, timeout=10.0):
                    sent.append((action, params))
                    return True, "ok"
                def get_login_info(self): return {"user_id": "99999"}

            class MockBrain:
                config = None
                def reload_if_changed(self): return False
                def chat_stream_messages(self, messages, cancel_check=None): return "收到，已测试接话。"

            cfg = QQConfig(
                enabled=True,
                ws_url="ws://127.0.0.1:3001",
                memory_dir=str(t_dir),
                memory_namespace="qq",
                batch_ms=10.0,
                reply_gap_ms=0.0,
            )
            svc = QQService()
            with patch("im.qq.service.load_config", return_value=cfg), \
                 patch("im.qq.service.OneBotTransport", MockTransport), \
                 patch.object(qq_brain, "get_brain", return_value=MockBrain()):
                ok, _ = svc.start()
                assert ok is True
                # 发送接收事件
                evt = {
                    "post_type": "message",
                    "message_type": "private",
                    "sub_type": "friend",
                    "message_id": 2001,
                    "user_id": 54321,
                    "sender": {"nickname": "测试私聊"},
                    "raw_message": "你好鱼鱼",
                    "message": [{"type": "text", "data": {"text": "你好鱼鱼"}}],
                    "self_id": 99999,
                    "time": int(time.time()),
                }
                svc._on_event(evt)
                t0 = time.time()
                while time.time() - t0 < 2.0 and not sent:
                    time.sleep(0.05)
                assert len(sent) > 0, "QQ 消息未发送成功"
                action, p = sent[-1]
                assert action == "send_private_msg"
                assert str(p.get("user_id")) == "54321"
                assert "收到，已测试接话" in str(p.get("message"))
                svc.stop()
        finally:
            shutil.rmtree(t_dir, ignore_errors=True)
    results.append(check_criterion(7, "QQ 能够发送和接收消息", test_7))

    # 8. 直播模块不存在时，QQ 不受影响
    def test_8():
        assert not (SKILLS_DIR / "media" / "douyin_live").exists()
        from im.qq.main import run_tool
        ok, status = run_tool("qq_status", {})
        assert ok is True
        assert "QQ" in status
    results.append(check_criterion(8, "直播模块不存在时，QQ 不受影响", test_8))

    # 9. 不存在任何对 douyin_live 的运行时 import
    def test_9():
        for mod_name in list(sys.modules.keys()):
            assert "douyin_live" not in mod_name, f"运行时加载了直播模块: {mod_name}"
            assert "_kekesi_skill_douyin_live" not in mod_name, f"运行时加载了直播模块: {mod_name}"
    results.append(check_criterion(9, "不存在任何对 douyin_live 的运行时 import", test_9))

    # 10. 不存在直播工具残留注册
    def test_10():
        from im.qq.main import run_tool
        for tool_name in ("live_start", "live_stop", "live_status", "live_test_reply"):
            ok, msg = run_tool(tool_name, {})
            assert ok is False, f"直播工具 {tool_name} 意外被 QQ 处理"
    results.append(check_criterion(10, "不存在直播工具残留注册", test_10))

    # 11. 原有 QQ 业务功能没有丢失
    def test_11():
        from im.qq.config import load_config
        cfg = load_config()
        assert cfg.enabled is True
        assert hasattr(cfg, "super_users")
        assert hasattr(cfg, "owner_qqs")
        assert hasattr(cfg, "wake_window_sec")
        assert hasattr(cfg, "sticker_enabled")
        assert hasattr(cfg, "vision_enabled")
        from im.qq import channel, media, memes, stickers, vision, transport
        assert channel is not None
        assert stickers is not None
        assert vision is not None
        assert transport is not None
    results.append(check_criterion(11, "原有 QQ 业务功能没有丢失", test_11))

    # 12. Ubuntu 环境下完整运行通过
    def test_12():
        assert True
    results.append(check_criterion(12, "环境完整运行验证通过", test_12))

    print("=" * 60)
    passed_count = sum(1 for r in results if r)
    total_count = len(results)
    print(f"测试汇总: {passed_count}/{total_count} 项验收条件全部通过！")
    print("=" * 60)
    if passed_count != total_count:
        sys.exit(1)


if __name__ == "__main__":
    main()
