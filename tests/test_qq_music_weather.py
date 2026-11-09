# -*- coding: utf-8 -*-
"""测试 QQ 大脑点歌与天气查询扩展功能。"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SKILLS_DIR = PROJECT_ROOT / "skills"
if str(SKILLS_DIR) not in sys.path:
    sys.path.insert(0, str(SKILLS_DIR))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from im.qq import music as music_mod
from im.qq import weather as weather_mod
from im.qq.service import QQService
from im.qq.config import QQConfig


def test_music_intent():
    """测试音乐点歌意图提取。"""
    cases = [
        ("我要听晴天", True, "晴天"),
        ("我要听 周杰伦 稻香", True, "周杰伦 稻香"),
        ("点歌 起风了", True, "起风了"),
        ("来首海阔天空吧", True, "海阔天空"),
        ("我想听歌", True, ""),
        ("点歌", True, ""),
        ("今天吃饭了吗", False, ""),
    ]
    for text, expected_hit, expected_query in cases:
        hit, query = music_mod.is_music_intent(text)
        assert hit == expected_hit, f"Intent hit mismatch for '{text}': got {hit}, expected {expected_hit}"
        if expected_hit:
            assert query == expected_query, f"Query mismatch for '{text}': got {query}, expected {expected_query}"
    print("[PASS] test_music_intent")


def test_music_search():
    """测试歌曲检索能力。"""
    song = music_mod.search_song("晴天 周杰伦")
    assert song is not None, "Search song should return result"
    assert "晴天" in song["title"] or "周杰伦" in song["singer"]
    assert song["platform"] in ("163", "qq")
    assert song["id"]
    assert "http" in song["page_url"]
    print(f"[PASS] test_music_search: {song['title']} - {song['singer']} ({song['platform']})")


def test_weather_intent():
    """测试天气意图提取。"""
    cases = [
        ("查天气", True, ""),
        ("北京天气", True, "北京"),
        ("上海天气怎么样", True, "上海"),
        ("查一下深圳的天气预报", True, "深圳"),
        ("广州今天热不热", True, "广州"),
        ("明天成都下雨吗", True, "成都"),
        ("今天星期几", False, ""),
    ]
    for text, expected_hit, expected_city in cases:
        hit, city = weather_mod.is_weather_intent(text)
        assert hit == expected_hit, f"Weather intent mismatch for '{text}': got {hit}, expected {expected_hit}"
        if expected_hit:
            assert city == expected_city, f"City mismatch for '{text}': got {city}, expected {expected_city}"
    print("[PASS] test_weather_intent")


def test_weather_report():
    """测试天气播报文本生成。"""
    report = weather_mod.get_weather_report("上海")
    assert "上海天气预报" in report
    assert "当前天气" in report
    assert "气温" in report
    assert "贴心建议" in report
    print("[PASS] test_weather_report")


class DummyTransport:
    def __init__(self):
        self.sent_actions = []

    def call_action(self, action, params, timeout=10.0):
        self.sent_actions.append((action, params))
        return True, "ok"


class DummyMemory:
    def record_viewer_reply(self, *args, **kwargs):
        pass


def test_service_dispatch_weather():
    """测试 QQService 接收到天气意图时的快路径直接响应。"""
    svc = QQService()
    svc._cfg = QQConfig(enabled=True, reply_gap_ms=0, sticker_enabled=False)
    dummy_t = DummyTransport()
    svc._transport = dummy_t
    svc._memory = DummyMemory()

    batch = [
        {
            "chat_type": "private",
            "chat_id": "123456",
            "text": "北京天气怎么样",
            "person_id": "p_123",
            "user_name": "张三",
        }
    ]
    svc._respond("private_123456", batch)

    assert len(dummy_t.sent_actions) >= 1
    action, params = dummy_t.sent_actions[0]
    assert action == "send_private_msg"
    assert "北京天气预报" in params["message"]
    print("[PASS] test_service_dispatch_weather")


def test_service_dispatch_music():
    """测试 QQService 接收到点歌意图时的卡片响应（成功时只发一张卡片，不发冗余文字链接）。"""
    svc = QQService()
    svc._cfg = QQConfig(enabled=True, reply_gap_ms=0, sticker_enabled=False)
    dummy_t = DummyTransport()
    svc._transport = dummy_t
    svc._memory = DummyMemory()

    batch = [
        {
            "chat_type": "group",
            "chat_id": "999999",
            "text": "我要听晴天",
            "person_id": "p_123",
            "user_name": "李四",
        }
    ]
    svc._respond("group_999999", batch)

    # 成功时只发送一张卡片（原生 json 格式的 Ark 结构化卡片或 music 卡片），绝不重复发送带有冗余链接的文本消息
    assert len(dummy_t.sent_actions) == 1
    action, params = dummy_t.sent_actions[0]
    assert action == "send_group_msg"
    msg_list = params["message"]
    assert isinstance(msg_list, list)
    card_type = msg_list[0].get("type")
    assert card_type in ("json", "music")
    print(f"[PASS] test_service_dispatch_music: 单一卡片极简发送成功 (type={card_type})")



if __name__ == "__main__":
    test_music_intent()
    test_music_search()
    test_weather_intent()
    test_weather_report()
    test_service_dispatch_weather()
    test_service_dispatch_music()
    print("\nALL MUSIC & WEATHER TESTS PASSED!")
