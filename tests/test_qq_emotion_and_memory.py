# -*- coding: utf-8 -*-
import unittest
import time
from pathlib import Path
import sys

# Ensure skills is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
SKILLS_DIR = PROJECT_ROOT / "skills"
if str(SKILLS_DIR) not in sys.path:
    sys.path.insert(0, str(SKILLS_DIR))

from im.qq.emotion import EmotionEngine, EmotionState
from im.qq.brain import build_messages, _qq_rules


class TestEmotionEngine(unittest.TestCase):
    def setUp(self):
        self.tmp_file = PROJECT_ROOT / "tests" / "tmp_emotions.json"
        if self.tmp_file.exists():
            self.tmp_file.unlink()
        self.engine = EmotionEngine(data_file=self.tmp_file, inertia=0.75, decay_half_life_hours=4.0)

    def tearDown(self):
        if self.tmp_file.exists():
            try:
                self.tmp_file.unlink()
            except Exception:
                pass

    def test_initial_state(self):
        # 普通用户
        st = self.engine.get_state("user_100", is_owner=False)
        self.assertAlmostEqual(st.valence, 0.15, places=2)
        self.assertAlmostEqual(st.affection, 0.50, places=2)

        # 主人身份特权
        st_owner = self.engine.get_state("owner_1", is_owner=True)
        self.assertAlmostEqual(st_owner.valence, 0.25, places=2)
        self.assertAlmostEqual(st_owner.affection, 0.95, places=2)

    def test_praise_and_joy_momentum(self):
        # 受到赞美与撒娇，效价 V 和激活度 A 显著提升
        st = self.engine.update("user_100", "你真可爱呀，好喜欢你！么么哒")
        self.assertGreater(st.valence, 0.15)
        self.assertGreater(st.arousal, 0.45)
        self.assertIn("当前心境", self.engine.build_prompt_instruction(st, is_owner=False))

    def test_scold_and_furious_retaliation(self):
        # 1. 单次挑衅 -> 触发不悦反驳 (annoyed)
        st_scold = self.engine.update("user_100", "你好笨啊真菜")
        self.assertEqual(st_scold.dominant_mood, "annoyed")
        self.assertLess(st_scold.valence, 0.0)

        # 2. 连续被骂 -> 激化触发愤怒回怼 (furious)
        st_furious = self.engine.update("user_100", "废物滚远点")
        self.assertEqual(st_furious.dominant_mood, "furious")
        self.assertGreaterEqual(st_furious.consecutive_scolds, 2)
        inst = self.engine.build_prompt_instruction(st_furious, is_owner=False)
        self.assertIn("骂回去", inst)

        # 3. 道歉 -> 抚平怒气
        st_apology = self.engine.update("user_100", "对不起我错了")
        self.assertEqual(st_apology.consecutive_scolds, 0)

    def test_time_decay_to_baseline(self):
        # 情绪兴奋后，时间自然流逝应缓慢回归基线
        self.engine.update("user_100", "太喜欢你了！！么么么么")
        # 模拟过去 24 小时
        with self.engine._lock:
            self.engine._states["user_100"].last_update = time.time() - 86400.0

        st_decayed = self.engine.get_state("user_100")
        self.assertAlmostEqual(st_decayed.valence, 0.15, delta=0.08)

    def test_sticker_intent_tags(self):
        st = EmotionState(dominant_mood="pouty")
        tags = self.engine.get_sticker_intent_tags(st)
        self.assertIn("哼", tags)

        st_happy = EmotionState(dominant_mood="happy")
        tags_happy = self.engine.get_sticker_intent_tags(st_happy)
        self.assertIn("开心", tags_happy)

    def test_brain_prompt_integration(self):
        inst = "【当前心境：甜美亲昵】心情特别好，语气柔和带小撒娇。"
        batch = [{"user_name": "张三", "text": "你好", "is_owner": False}]
        msgs = build_messages(
            system_prompt="你是鱼鱼。",
            batch=batch,
            max_chars=100,
            is_owner=False,
            emotion_instruction=inst,
        )
        sys_content = msgs[0]["content"]
        self.assertIn(inst, sys_content)
        self.assertIn("普通群友「张三」说：你好", msgs[1]["content"])


if __name__ == "__main__":
    unittest.main()
