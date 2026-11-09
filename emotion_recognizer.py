"""情感识别模块 — 识别用户情绪并调整回复语气"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_EMOTION_KEYWORDS = {
    "happy": {
        "keywords": ["开心", "高兴", "快乐", "爽", "棒", "好", "赞", "喜欢", "爱", "兴奋",
                     "哈哈", "嘻嘻", "嘿嘿", "耶", "太好了", "太棒了", "真不错", "美滋滋"],
        "intensity": 0.8,
    },
    "sad": {
        "keywords": ["难过", "伤心", "悲伤", "失望", "沮丧", "失落", "郁闷", "低落",
                     "惨", "可怜", "想哭", "不开心", "心情不好", "心烦", "烦躁"],
        "intensity": 0.8,
    },
    "angry": {
        "keywords": ["生气", "愤怒", "烦", "气死", "可恶", "讨厌", "烦死人", "受不了",
                     "火大", "发火", "暴怒", "骂人", "抱怨", "指责", "不爽"],
        "intensity": 0.85,
    },
    "tired": {
        "keywords": ["累", "疲惫", "困", "疲倦", "乏力", "没精神", "想睡觉", "休息",
                     "好累", "疲倦", "困倦", "嗜睡", "体力不支"],
        "intensity": 0.75,
    },
    "anxious": {
        "keywords": ["焦虑", "紧张", "担心", "害怕", "恐惧", "不安", "忧虑", "忐忑",
                     "心慌", "不知所措", "担心", "着急", "着急上火"],
        "intensity": 0.7,
    },
    "surprised": {
        "keywords": ["惊讶", "震惊", "意外", "没想到", "居然", "竟然", "哇", "天呐",
                     "不可思议", "出乎意料", "大吃一惊"],
        "intensity": 0.75,
    },
}

_EMOTION_CONFIG = {
    "happy": {
        "name": "开心",
        "description": "用户心情愉快",
        "response_tone": "积极热情",
        "suggestions": ["用积极的语气回应", "使用表情符号", "分享喜悦"],
    },
    "sad": {
        "name": "难过",
        "description": "用户情绪低落",
        "response_tone": "温柔安慰",
        "suggestions": ["表达理解和同情", "给予鼓励", "提供帮助"],
    },
    "angry": {
        "name": "生气",
        "description": "用户处于愤怒状态",
        "response_tone": "冷静耐心",
        "suggestions": ["保持冷静", "避免激怒用户", "提供解决方案"],
    },
    "tired": {
        "name": "疲惫",
        "description": "用户感到劳累",
        "response_tone": "轻柔体贴",
        "suggestions": ["提醒休息", "提供轻松话题", "减少复杂操作"],
    },
    "anxious": {
        "name": "焦虑",
        "description": "用户感到焦虑不安",
        "response_tone": "镇定安抚",
        "suggestions": ["给予信心", "提供明确步骤", "保持稳定语气"],
    },
    "surprised": {
        "name": "惊讶",
        "description": "用户感到意外",
        "response_tone": "热情回应",
        "suggestions": ["表达共鸣", "解释原因", "提供更多信息"],
    },
    "neutral": {
        "name": "平静",
        "description": "用户情绪平稳",
        "response_tone": "自然友好",
        "suggestions": ["正常回应", "保持中性语气"],
    },
}


def detect_emotion(text: str) -> tuple[str, float, dict]:
    """识别文本中的情感

    返回：(emotion_key, confidence, details)
    """
    text = (text or "").strip()
    if not text:
        return "neutral", 0.0, _EMOTION_CONFIG["neutral"]

    scores = {}
    for emotion, config in _EMOTION_KEYWORDS.items():
        score = 0.0
        hits = 0
        for keyword in config["keywords"]:
            if keyword in text:
                score += config["intensity"]
                hits += 1
        if score > 0:
            scores[emotion] = score

    if not scores:
        return "neutral", 0.0, _EMOTION_CONFIG["neutral"]

    max_emotion = max(scores, key=scores.get)
    max_score = min(scores[max_emotion], 1.0)

    return max_emotion, max_score, _EMOTION_CONFIG.get(max_emotion, _EMOTION_CONFIG["neutral"])


def adjust_response_tone(response: str, emotion: str, confidence: float) -> str:
    """根据情感调整回复语气"""
    if confidence < 0.3:
        return response

    adjustments = {
        "happy": [
            ("好的", "好哒~"),
            ("知道了", "收到！"),
            ("好", "好耶！"),
        ],
        "sad": [
            ("好的", "好的，别难过"),
            ("没问题", "没问题的，一切都会好起来"),
            ("知道了", "我明白了"),
        ],
        "angry": [
            ("好的", "好的，我马上处理"),
            ("知道了", "明白，我立刻解决"),
            ("哦", "好的"),
        ],
        "tired": [
            ("好的", "好的，你好好休息"),
            ("没问题", "没问题，交给我吧"),
            ("知道了", "知道啦"),
        ],
        "anxious": [
            ("好的", "好的，别担心"),
            ("没问题", "没问题，一步一步来"),
            ("知道了", "明白了，我会处理好的"),
        ],
        "surprised": [
            ("好的", "好的！"),
            ("知道了", "哇，知道了！"),
            ("哦", "哇！"),
        ],
    }

    adj_list = adjustments.get(emotion, [])
    for old, new in adj_list:
        response = response.replace(old, new)

    return response


def get_emotion_prompt(emotion: str, confidence: float) -> str:
    """生成情感提示词，用于影响 R3 的回复风格"""
    if confidence < 0.3:
        return ""

    cfg = _EMOTION_CONFIG.get(emotion)
    if not cfg:
        return ""

    return f"[情感状态] 用户当前情绪：{cfg['name']}（{cfg['description']}）。回复时请保持{cfg['response_tone']}的语气。"


def list_emotions() -> list[str]:
    """列出所有支持的情感类型"""
    return list(_EMOTION_CONFIG.keys())


def get_emotion_config(emotion: str) -> dict:
    """获取情感配置"""
    return _EMOTION_CONFIG.get(emotion, _EMOTION_CONFIG["neutral"])


def analyze_emotion(text: str) -> dict:
    """完整分析文本情感"""
    emotion, confidence, details = detect_emotion(text)
    return {
        "emotion": emotion,
        "confidence": confidence,
        "name": details["name"],
        "description": details["description"],
        "response_tone": details["response_tone"],
        "suggestions": details["suggestions"],
    }