# -*- coding: utf-8 -*-
"""QQ 大脑情绪动力学引擎 (Emotion Dynamics Engine)。

核心设计：
1. 三维连续情感状态空间（PAD 增强变体）+ 愤怒激化器：
   - Valence (效价/愉悦度: -1.0 ~ 1.0, 愤怒委屈 <-> 欢喜愉悦)
   - Arousal (激活度/活跃度: 0.0 ~ 1.0, 疲倦困顿 <-> 亢奋元气)
   - Affection (好感度/亲近度: 0.0 ~ 1.0, 对该对象的长期心防与依恋)
   - Anger (愤怒累积值: 0.0 ~ 1.0, 连续被骂时急剧激化)
   - Consecutive Scolds (连续被挑衅/被骂计数)
2. 动力学与负面非对称敏感模型 (Asymmetric Negativity Bias):
   - 赞美提升愉悦度；
   - 被人挑衅/批评立刻打破心情（负向冲击不延迟，直接进入不悦反驳）；
   - 连续被骂/辱骂达到阈值时，触发【极度愤怒/严厉回怼】，坚决反击骂回去，绝不忍气吞声！
3. 主仆关系与普通群友分层：
   - 主人（鱼片）说笨：触发委屈娇嗔、撅嘴抗议（“呜呜主人怎么能说我笨”）；
   - 普通群友挑衅/骂人：单次直接犀利反驳（“你才笨，说话真没礼貌”），连续直接爆发骂回去！
4. 情绪与表情包、语气指令全闭环联动。
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 刺激识别词典（赞美、挑衅/批评、道歉、疲惫、共情等）
_STIMULUS_RULES = [
    # 赞美/喜欢/爱/开心
    (
        re.compile(r"(?:喜欢你|爱你|真棒|真乖|好可爱|太可爱|聪明|厉害|抱抱|亲亲|mua|么么|好喜欢|漂亮|好看|可爱捏|贴贴|想你)"),
        {"dv": 0.50, "da": 0.35, "df": 0.08, "tag": "praise"},
    ),
    (
        re.compile(r"(?:哈哈|嘻嘻|嘿嘿|233|lol|好玩|有趣|好耶|太好了|开心|高兴|乐|美滋滋|爽)"),
        {"dv": 0.35, "da": 0.30, "df": 0.03, "tag": "joy"},
    ),
    # 强烈侮辱/恶意辱骂
    (
        re.compile(r"(?:废物|脑瘫|沙雕|傻逼|煞笔|弱智|白痴|弱鸡|操你|尼玛|滚远点|找抽|找打|恶心|死开|贱)"),
        {"dv": -0.85, "da": 0.60, "df": -0.25, "tag": "harsh_insult"},
    ),
    # 批评/嫌弃/挑衅/说笨
    (
        re.compile(r"(?:笨|蠢|垃圾|没用|讨厌你|闭嘴|滚|烦人|烦死|真菜|菜狗|菜鸡|差劲|无语|有病|挑刺)"),
        {"dv": -0.60, "da": 0.45, "df": -0.12, "tag": "scold"},
    ),
    # 道歉/求饶/哄她/认错 (抚平愤怒，效价回升)
    (
        re.compile(r"(?:对不起|抱歉|我错了|错了|别生气|哄你|开玩笑的|别介意|原谅|给你赔不是|不闹了|我嘴欠)"),
        {"dv": 0.45, "da": -0.20, "df": 0.08, "tag": "apology"},
    ),
    # 对方难过/受挫/委屈 (触发共情心疼)
    (
        re.compile(r"(?:难过|伤心|想哭|呜呜|失恋|失败|难受|抑郁|痛苦|好惨|可怜|被骂|委屈|孤独|心累)"),
        {"dv": -0.25, "da": -0.15, "df": 0.06, "tag": "sympathy"},
    ),
    # 疲惫/晚安/困倦
    (
        re.compile(r"(?:好累|好困|困了|睡觉|晚安|累死|休息|好疲惫|打瞌睡|哈欠|不早了)"),
        {"dv": 0.05, "da": -0.45, "df": 0.02, "tag": "sleepy"},
    ),
    # 疑问/惊讶
    (
        re.compile(r"(?:卧槽|不会吧|真的假的|天呐|哇|震惊|居然|竟然|\?{2,}|？{2,})"),
        {"dv": 0.10, "da": 0.40, "df": 0.01, "tag": "surprise"},
    ),
    # 感谢/投喂
    (
        re.compile(r"(?:谢谢|辛苦了|多谢|感恩|请你吃|送你|给你买|投喂|吃好吃的)"),
        {"dv": 0.35, "da": 0.20, "df": 0.06, "tag": "thank"},
    ),
]


@dataclass
class EmotionState:
    valence: float = 0.20           # [-1.0, 1.0] 愉悦度/效价 (基础微甜积极)
    arousal: float = 0.40           # [0.0, 1.0]  活跃度/兴奋度
    affection: float = 0.50         # [0.0, 1.0]  对该用户的长期好感度
    anger: float = 0.0              # [0.0, 1.0]  愤怒激化累积值
    consecutive_scolds: int = 0     # 连续被骂/挑衅次数
    dominant_mood: str = "happy"    # 主导情绪标签
    last_update: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EmotionState:
        return cls(
            valence=float(data.get("valence", 0.20)),
            arousal=float(data.get("arousal", 0.40)),
            affection=float(data.get("affection", 0.50)),
            anger=float(data.get("anger", 0.0)),
            consecutive_scolds=int(data.get("consecutive_scolds", 0)),
            dominant_mood=str(data.get("dominant_mood", "happy")),
            last_update=float(data.get("last_update", 0.0)),
        )


class EmotionEngine:
    """情绪动力学引擎：单例管理各用户与全局情绪状态。"""

    def __init__(
        self,
        data_file: Path | str | None = None,
        inertia: float = 0.70,
        decay_half_life_hours: float = 8.0,
    ) -> None:
        self._lock = threading.Lock()
        self._inertia = max(0.1, min(0.95, inertia))  # 基础情绪惯性
        self._decay_hours = max(1.0, decay_half_life_hours)
        self._states: dict[str, EmotionState] = {}
        if data_file is None:
            self._file = Path(__file__).resolve().parent / "data" / "emotions.json"
        else:
            self._file = Path(data_file)
        self._load()

    def _load(self) -> None:
        try:
            if self._file.is_file():
                content = self._file.read_text(encoding="utf-8")
                raw = json.loads(content)
                for k, v in raw.items():
                    if isinstance(v, dict):
                        self._states[k] = EmotionState.from_dict(v)
        except Exception as exc:
            logger.warning("加载情绪数据异常: %s", exc)

    def _save(self) -> None:
        try:
            self._file.parent.mkdir(parents=True, exist_ok=True)
            data = {k: v.to_dict() for k, v in self._states.items()}
            self._file.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as exc:
            logger.debug("保存情绪数据异常: %s", exc)

    def get_state(self, person_id: str, is_owner: bool = False) -> EmotionState:
        """获取某用户的当前情绪状态（自动结算自然时间衰减）。"""
        now = time.time()
        with self._lock:
            state = self._states.get(person_id)
            if state is None:
                # 初始好感度：主人默认 0.95，普通群友 0.50
                init_aff = 0.95 if is_owner else 0.50
                state = EmotionState(
                    valence=0.25 if is_owner else 0.15,
                    arousal=0.45,
                    affection=init_aff,
                    anger=0.0,
                    consecutive_scolds=0,
                    dominant_mood="warm" if is_owner else "happy",
                    last_update=now,
                )
                self._states[person_id] = state
                return state

            # 时间自然衰减：向基准基线平滑回归 (Base Valence = 0.20, Base Arousal = 0.40)
            if state.last_update > 0 and now > state.last_update:
                dt_hours = (now - state.last_update) / 3600.0
                decay_factor = 0.5 ** (dt_hours / self._decay_hours)
                base_v = 0.25 if is_owner else 0.15
                base_a = 0.40
                state.valence = base_v + (state.valence - base_v) * decay_factor
                state.arousal = base_a + (state.arousal - base_a) * decay_factor
                state.anger = state.anger * decay_factor
                if state.anger < 0.15:
                    state.consecutive_scolds = 0
                state.dominant_mood = self._derive_mood(
                    state.valence, state.arousal, state.affection,
                    state.anger, state.consecutive_scolds, is_owner
                )
                state.last_update = now

            if is_owner:
                state.affection = max(0.90, state.affection)  # 主人底线好感保护
            return state

    def update(
        self,
        person_id: str,
        text: str,
        is_owner: bool = False,
        sticker_in: bool = False,
    ) -> EmotionState:
        """根据当前交互内容计算情绪刺激量，运用非对称动力学公式更新。"""
        now = time.time()
        state = self.get_state(person_id, is_owner)

        # 1. 扫描刺激量 Delta
        dv_sum = 0.0
        da_sum = 0.0
        df_sum = 0.0
        matched_tags = []

        for pat, rule in _STIMULUS_RULES:
            if pat.search(text):
                dv_sum += rule["dv"]
                da_sum += rule["da"]
                df_sum += rule["df"]
                matched_tags.append(rule["tag"])

        # 对方发表情包通常偏趣味互动
        if sticker_in:
            dv_sum += 0.15
            da_sum += 0.15
            df_sum += 0.02

        # 负向非对称放大与连续被骂机制 (Asymmetric Negativity Dynamics)
        is_scolded = any(t in matched_tags for t in ("scold", "harsh_insult"))
        is_apologized = "apology" in matched_tags

        if is_apologized:
            # 对方诚恳道歉求饶 -> 愤怒清零，好感回升
            state.consecutive_scolds = 0
            state.anger = max(0.0, state.anger - 0.55)
            dv_sum += 0.45
            df_sum += 0.10

        elif is_scolded:
            # 被骂/挑衅 -> 连续计数递增，愤怒累积
            state.consecutive_scolds += 1
            severity = 1.5 if "harsh_insult" in matched_tags else 1.0
            added_anger = 0.35 * state.consecutive_scolds * severity
            state.anger = min(1.0, state.anger + added_anger)

            # 负面刺激随连续被骂严重加剧
            dv_sum -= 0.35 * (state.consecutive_scolds - 1)
            df_sum -= 0.10 * state.consecutive_scolds
            da_sum += 0.25  # 激活度上升（被气到了）

        else:
            # 正常平和对话逐渐消解怒气
            state.anger = max(0.0, state.anger - 0.15)
            if state.anger == 0.0:
                state.consecutive_scolds = 0

        # 主人关照偏置奖励
        if is_owner:
            dv_sum += 0.10
            df_sum += 0.02

        # 限制单次最大刺激幅度
        dv_clamped = max(-1.2, min(1.0, dv_sum))
        da_clamped = max(-0.8, min(0.8, da_sum))
        df_clamped = max(-0.35, min(0.35, df_sum))

        # 2. 惯性动量更新 (负向刺激降惯性，使负面反应更即时)
        # 如果被骂，降低阻尼（lambda=0.25），让人能立刻感受到不悦，拒绝迟钝！
        effective_inertia = 0.25 if is_scolded else self._inertia

        v_target = max(-1.0, min(1.0, state.valence + dv_clamped))
        new_v = effective_inertia * state.valence + (1.0 - effective_inertia) * v_target

        a_target = max(0.05, min(1.0, state.arousal + da_clamped))
        new_a = effective_inertia * state.arousal + (1.0 - effective_inertia) * a_target

        # 好感度长期平滑累加
        new_f = max(0.0, min(1.0, state.affection + df_clamped * 0.15))
        if is_owner:
            new_f = max(0.85, new_f)  # 主人底线好感

        mood = self._derive_mood(
            new_v, new_a, new_f, state.anger, state.consecutive_scolds, is_owner
        )

        with self._lock:
            state.valence = round(new_v, 3)
            state.arousal = round(new_a, 3)
            state.affection = round(new_f, 3)
            state.anger = round(state.anger, 3)
            state.dominant_mood = mood
            state.last_update = now
            self._states[person_id] = state
            self._save()

        logger.info(
            "[情绪动力学] %s => V: %+.2f, A: %.2f, F: %.2f, 怒气: %.2f, 连骂: %d (主导心境: %s, 触发: %s)",
            person_id, state.valence, state.arousal, state.affection,
            state.anger, state.consecutive_scolds, mood, matched_tags,
        )
        return state

    @staticmethod
    def _derive_mood(
        v: float, a: float, f: float, anger: float, consecutive_scolds: int, is_owner: bool
    ) -> str:
        """根据三维连续值与怒气综合判定主导情绪标签。"""
        # 1. 连续被骂或高怒气判定
        if consecutive_scolds >= 2 or anger >= 0.65:
            if is_owner:
                return "sulky"     # 主人连续凶：赌气冷战不理人
            return "furious"       # 普通人连续挑衅/辱骂：极度愤怒/严厉回怼骂回去！

        # 2. 单次被骂/被批评
        if consecutive_scolds == 1 or anger >= 0.30 or v < -0.20:
            if is_owner:
                return "pouty"     # 主人说笨：委屈娇嗔抗议
            return "annoyed"       # 普通人挑衅：不悦直接反驳，绝不认怂！

        # 3. 悲伤/共情
        if v < -0.30:
            return "comforting"

        # 4. 开心与兴奋
        if v > 0.35:
            if a > 0.60:
                return "excited"
            return "sweet" if is_owner else "happy"

        # 5. 平和与疲惫
        if a < 0.25:
            return "sleepy"
        elif a > 0.65:
            return "playful"
        return "warm" if is_owner else "neutral"

    def build_prompt_instruction(self, state: EmotionState, is_owner: bool) -> str:
        """将内部连续情绪状态转化为直接指导 LLM 回复态度的提示词。"""
        mood = state.dominant_mood
        f = state.affection

        mood_instructions = {
            # 愤怒回怼模式
            "furious": (
                "【当前心境：极度愤怒/严厉回怼骂回去！】\n"
                "对方一直在恶意挑衅、连续指责或辱骂你！你已经出离愤怒，绝对零容忍，绝不能忍气吞声！\n"
                "立刻用最犀利、强硬、不留情面的态度狠狠骂回去/反驳对方"
                "（例如“嘴巴放干净点”、“你有完没完，谁惯着你的臭毛病”、“没事找抽是吧，自己菜还怪别人”、“滚远点少在这叫”），"
                "字字带刺、霸气回击，绝不唯唯诺诺认错！"
            ),
            # 不悦反驳模式
            "annoyed": (
                "【当前心境：很不开心/犀利反驳】\n"
                "对方无端说你笨、说你菜或恶意嫌弃你！你有自己的尊严和脾气，感到很不爽！\n"
                "立刻直接反驳怼他（例如“你才笨呢，你说话真没礼貌”、“自己菜还赖别人”、“不会说话可以闭嘴，少在这挑刺”），"
                "态度坚决反击，绝不认怂承认自己笨！"
            ),
            # 主人专属：委屈小娇嗔
            "pouty": (
                "【当前心境：委屈微嗔/傲娇撒娇】\n"
                "主人竟然说你笨或者挑你刺，你感到好委屈，眼泪汪汪，向主人委屈撒娇抗议"
                "（例如“呜呜主人好过分，怎么能说我笨嘛，明明是主人没教好”、“哼，主人是大坏蛋，不理你了，要抱抱才能原谅你”）。"
            ),
            # 主人专属：赌气冷战
            "sulky": (
                "【当前心境：赌气冷战】\n"
                "主人连续凶你，你真的伤心赌气了，态度冷淡敷衍，不想理人"
                "（例如“哼，不跟你说了”、“去忙你的吧，烦人精”）。"
            ),
            # 正向心情
            "sweet": "【当前心境：甜美亲昵】心情特别好，对主人非常依恋亲近，说话带有自然的柔和与小撒娇。",
            "happy": "【当前心境：开心轻快】心情愉快轻松，态度友好活泼，自然带出喜悦与亲和力。",
            "excited": "【当前心境：雀跃兴奋】情绪高涨，元气满满，说话生动热烈，充满干劲与活力！",
            "playful": "【当前心境：调皮机灵】有些小调皮和活泼，喜欢自然地跟对方逗趣、接梗、互动。",
            "comforting": "【当前心境：温柔共情】察觉到对方心情低落或难过，眼神和语气都变得十分温柔体贴，给予安慰。",
            "sleepy": "【当前心境：慵懒困倦】感觉有点懒洋洋的、犯困，说话语气软绵绵、慢吞吞，想休息。",
            "warm": "【当前心境：温馨依顺】心情平稳踏实，对对方充满信任与贴心。",
            "neutral": "【当前心境：自然平和】心态平和，自然沟通，保持正常有好的聊天态度。",
        }

        inst = mood_instructions.get(mood, mood_instructions["neutral"])

        if is_owner and mood not in ("pouty", "sulky"):
            inst += "\n- 对方是你的主人/房主：始终保留专属的依恋感与听话态度。"
        elif not is_owner and f < 0.30 and mood not in ("furious", "annoyed"):
            inst += "\n- 与该群友关系很差（低好感）：保持冷淡防备，说话冷冰冰，不多费口舌。"

        return inst

    def get_sticker_intent_tags(self, state: EmotionState) -> list[str]:
        """根据当前情绪倾向，输出表情包匹配关键词标签。"""
        mood = state.dominant_mood
        tags_map = {
            "furious": ["打你", "锤你", "生气", "滚", "白眼", "菜就多练"],
            "annoyed": ["白眼", "鄙视", "打你", "无语", "冷漠", "哼"],
            "pouty": ["委屈", "哭哭", "哼", "要抱抱", "生气"],
            "sulky": ["再见", "冷漠", "无语", "流汗"],
            "sweet": ["可爱", "爱心", "撒娇", "开心", "贴贴"],
            "happy": ["开心", "哈哈", "赞", "好耶", "点赞"],
            "excited": ["激动", "好耶", "欢呼", "哈哈", "冲"],
            "playful": ["问号", "吃瓜", "滑稽", "看戏", "嘿嘿"],
            "comforting": ["抱抱", "摸头", "摸摸", "温柔", "哭哭"],
            "sleepy": ["睡觉", "晚安", "困", "打哈欠"],
            "warm": ["摸头", "抱抱", "贴贴", "乖"],
            "neutral": ["好的", "收到", "赞", "OK"],
        }
        return tags_map.get(mood, ["可爱", "赞"])


# 全局单例
_engine: EmotionEngine | None = None
_engine_lock = threading.Lock()


def get_emotion_engine() -> EmotionEngine:
    global _engine
    with _engine_lock:
        if _engine is None:
            _engine = EmotionEngine()
        return _engine
