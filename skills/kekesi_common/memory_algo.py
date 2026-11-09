# -*- coding: utf-8 -*-
"""直播记忆核心算法库（移植自 Lumi_Nox, ForgetMeNot, KIRA）。

纯标准库实现，零外部依赖：
1. Lumi_Nox: 惰性时间衰减 (Lazy Time-Decay) + 特权身份保真 (Pinned Facts)
2. ForgetMeNot: 零向量 Jaccard 相似度去重 + 置信度渐进强化 (Confidence Nudging) + 证据溯源 (Provenance)
3. KIRA: 事实多样性聚类折叠 (Diverse Facts Clustering) + 发言人归属防洗白校验 (Speaker Attribution Guard)
"""

from __future__ import annotations

import math
import re
import time
from datetime import datetime
from typing import Any

__all__ = [
    # Lumi_Nox Decay
    "DEFAULT_HALF_LIFE_HOURS",
    "DEFAULT_PRUNE_THRESHOLD",
    "PINNED_CATEGORIES",
    "PINNED_SOURCES",
    "fact_strength",
    "is_pinned",
    "filter_active_facts",
    # ForgetMeNot Jaccard & Confidence
    "SourceKind",
    "tokenize",
    "jaccard",
    "is_similar_fact",
    "nudge_confidence",
    "promote_source_kind",
    # KIRA Diversity & Attribution
    "diverse_facts",
    "speaker_attribution_guard",
    # Prompts
    "VIEWER_FACT_EXTRACTOR_SYSTEM",
]

# ==============================================================================
# 1. Lumi_Nox: 惰性时间衰减 (Lazy Time-Decay)
# ==============================================================================
# 默认半衰期 168 小时（7 天），衰减阈值 0.1
DEFAULT_HALF_LIFE_HOURS: float = 168.0
DEFAULT_PRUNE_THRESHOLD: float = 0.1

PINNED_CATEGORIES = ("identity", "vip", "fan_medal", "guard", "honor")
PINNED_SOURCES = ("human", "manual", "system_event", "gift_event")


def fact_strength(
    created_at: float | str,
    now: float | None = None,
    half_life_hours: float = DEFAULT_HALF_LIFE_HOURS,
) -> float:
    """计算事实在当前时间的有效强度（范围 0.0 ~ 1.0）。
    
    公式：0.5 ** (age_hours / half_life_hours)
    """
    if half_life_hours is None or half_life_hours <= 0:
        return 1.0
    now_ts = time.time() if now is None else float(now)
    if isinstance(created_at, (int, float)):
        created_ts = float(created_at)
    else:
        try:
            created_ts = datetime.fromisoformat(str(created_at)).timestamp()
        except (ValueError, TypeError):
            return 1.0

    age_hours = (now_ts - created_ts) / 3600.0
    if age_hours <= 0:
        return 1.0
    return 0.5 ** (age_hours / half_life_hours)


def is_pinned(fact: dict[str, Any]) -> bool:
    """人工修正、特权身份（粉丝牌、打赏等）与明确置顶的事实不参与时间衰减。"""
    if fact.get("is_pinned"):
        return True
    source = str(fact.get("source_kind") or fact.get("source") or "")
    if source in PINNED_SOURCES:
        return True
    category = str(fact.get("category") or "")
    if category in PINNED_CATEGORIES:
        return True
    return False


def filter_active_facts(
    facts: list[dict[str, Any]],
    now: float | None = None,
    half_life_hours: float = DEFAULT_HALF_LIFE_HOURS,
    prune_threshold: float = DEFAULT_PRUNE_THRESHOLD,
) -> list[dict[str, Any]]:
    """惰性过滤：保留置顶或衰减强度 >= prune_threshold 的事实，保持原序。"""
    now_ts = time.time() if now is None else float(now)
    kept = []
    for f in facts:
        if is_pinned(f):
            kept.append(f)
            continue
        ts = f.get("created_at") or f.get("ts")
        if ts is None:
            kept.append(f)
            continue
        strength = fact_strength(ts, now=now_ts, half_life_hours=half_life_hours)
        if strength >= prune_threshold:
            kept.append(f)
    return kept


# ==============================================================================
# 2. ForgetMeNot: 零向量 Jaccard 相似度去重与置信度更新
# ==============================================================================
SourceKind = str  # "self_claim" | "reported" | "inferred"

KIND_STRENGTH: dict[str, int] = {
    "self_claim": 3,
    "reported": 2,
    "inferred": 1,
}

_JACCARD_STOPWORDS = frozenset({
    "的", "了", "在", "是", "我", "有", "和", "就", "不", "人", "都", "一", "一个",
    "上", "也", "很", "到", "说", "要", "去", "你", "会", "着", "没有", "看", "好",
    "自己", "这", "那", "他", "她", "它", "主播", "鱼鱼",
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "to", "of", "in", "on", "at", "by", "for", "with", "and", "or",
    "that", "this", "it", "they", "their", "has", "have", "had",
})


def tokenize(text: str) -> set[str]:
    """提取中英文特征词（英文分词 + 中文 2~4 字滑动窗口），过滤停用词。"""
    text = (text or "").lower()
    tokens = set()
    # 提取英文和数字连续块
    en_words = re.findall(r"[a-z0-9_]+", text)
    for w in en_words:
        if len(w) > 1 and w not in _JACCARD_STOPWORDS:
            tokens.add(w)
    # 提取中文纯文本
    cn_text = re.sub(r"[^\u4e00-\u9fff]", "", text)
    cn_len = len(cn_text)
    for n in (2, 3, 4):
        for i in range(cn_len - n + 1):
            gram = cn_text[i:i + n]
            if gram not in _JACCARD_STOPWORDS:
                tokens.add(gram)
    return tokens


def jaccard(a: set[str], b: set[str]) -> float:
    """计算两个词集的 Jaccard 相似度。"""
    if not a or not b:
        return 0.0
    overlap = len(a & b)
    union = len(a | b)
    return overlap / union if union > 0 else 0.0


def is_similar_fact(a: str, b: str, threshold: float = 0.5) -> bool:
    """无向量去重判断：子串包含或 Jaccard 相似度 >= threshold。"""
    if not a or not b:
        return False
    la, lb = a.strip().lower(), b.strip().lower()
    if la in lb or lb in la:
        return True
    tok_a, tok_b = tokenize(la), tokenize(lb)
    return jaccard(tok_a, tok_b) >= threshold


def nudge_confidence(current: float, factor: float = 0.1) -> float:
    """重新被观测到时平滑递增置信度，收敛于 1.0。
    
    公式：min(1.0, current + (1.0 - current) * factor)
    """
    c = max(0.0, min(1.0, float(current or 0.5)))
    return min(1.0, c + (1.0 - c) * factor)


def promote_source_kind(existing: str | None, incoming: str | None) -> str:
    """事实来源强度升级（self_claim > reported > inferred）。"""
    if not incoming:
        return existing or "inferred"
    if not existing:
        return incoming
    s_in = KIND_STRENGTH.get(incoming, 1)
    s_ex = KIND_STRENGTH.get(existing, 1)
    return incoming if s_in > s_ex else existing


# ==============================================================================
# 3. KIRA: 事实多样性聚类折叠 (Diverse Facts) 与归属校验
# ==============================================================================
_DIVERSE_STOPWORDS = frozenset({
    "在", "的", "了", "是", "我", "你", "他", "她", "喜欢", "平时", "经常", "觉得",
    "说", "讲", "做", "干", "想", "爱", "要", "会", "能", "可以", "经常", "一直",
    "总是", "很", "太", "非常", "特别", "真", "确实", "事实", "知道", "记得",
    "user", "viewer", "chatter", "kira", "yuyu",
})


def _extract_fact_keywords(text: str) -> set[str]:
    """提取事实的核心主题关键词用于聚类。"""
    toks = tokenize(text)
    return {t for t in toks if t not in _DIVERSE_STOPWORDS and len(t) >= 2}


def diverse_facts(
    fact_items: list[dict[str, Any]],
    username: str = "",
    limit: int = 5,
) -> list[dict[str, Any]]:
    """多样性事实选择（KIRA 算法纯 Python 移植）。
    
    将同一观众反复被记录的相似话题（如几十条吃香蕉/原神抽卡）聚类折叠为 1 条代表句，
    按频次（耐受度 Durability）排序，最新鲜的一条作为该类的代表，确保召回的多样性。
    """
    if not fact_items:
        return []
    if len(fact_items) <= limit:
        return fact_items

    # 1. 提取每个事实的核心词集
    item_tokens = []
    freq: dict[str, int] = {}
    for item in fact_items:
        txt = str(item.get("fact") or item.get("text") or "")
        toks = _extract_fact_keywords(txt)
        item_tokens.append(toks)
        for t in toks:
            freq[t] = freq.get(t, 0) + 1

    # 2. 确定每个事实的聚类主题键（出现频率最高、长度较长的特征词）
    clusters: dict[str, dict[str, Any]] = {}
    for item, toks in zip(fact_items, item_tokens):
        if toks:
            key = max(toks, key=lambda t: (freq.get(t, 0), len(t), t))
        else:
            txt = str(item.get("fact") or item.get("text") or "")
            key = txt[:8] if txt else "generic"

        c = clusters.setdefault(key, {"items": [], "max_ts": 0.0})
        c["items"].append(item)
        ts = float(item.get("created_at") or item.get("ts") or 0.0)
        if ts > c["max_ts"]:
            c["max_ts"] = ts

    # 3. 前缀合并（如“原神”和“原神抽卡”折叠为一个聚类）
    sorted_keys = sorted(clusters.keys(), key=len)
    canon: dict[str, str] = {}
    for k in sorted_keys:
        target = k
        for shorter in sorted_keys:
            if shorter == k:
                break
            if len(shorter) >= 2 and k.startswith(shorter):
                target = canon.get(shorter, shorter)
                break
        canon[k] = target

    merged_clusters: dict[str, dict[str, Any]] = {}
    for k, c in clusters.items():
        ckey = canon.get(k, k)
        m = merged_clusters.setdefault(ckey, {"items": [], "max_ts": 0.0})
        m["items"].extend(c["items"])
        if c["max_ts"] > m["max_ts"]:
            m["max_ts"] = c["max_ts"]

    # 4. 耐受度优先（聚类元素个数）+ 最新鲜度打破平局
    ranked = sorted(
        merged_clusters.values(),
        key=lambda c: (len(c["items"]), c["max_ts"]),
        reverse=True,
    )

    # 5. 从每个胜出聚类中选取最新鲜的一条代表事实
    out: list[dict[str, Any]] = []
    for c in ranked[:limit]:
        best_item = max(
            c["items"],
            key=lambda it: float(it.get("created_at") or it.get("ts") or 0.0),
        )
        out.append(best_item)
    return out


def speaker_attribution_guard(
    fact_text: str,
    user_text: str,
    context_user_texts: list[str] | None = None,
) -> bool:
    """发言人归属防洗白校验（移植自 KIRA memory_extractor.py）。
    
    检查提取出的事实文本中的关键词，是否确实出现在该观众本轮或前几轮发言中。
    防止 AI 自身的调侃或幻觉（例如 AI 脑补观众去吃烧烤）被回写沉淀为观众的真实档案。
    """
    if not fact_text or not user_text:
        return False
    corpus = user_text.lower()
    if context_user_texts:
        for ct in context_user_texts:
            if ct:
                corpus += " " + ct.lower()

    # 提取事实中的关键特征词（去掉单字和常见虚词）
    fact_tokens = tokenize(fact_text)
    meaningful = [t for t in fact_tokens if t not in _JACCARD_STOPWORDS and len(t) >= 2]
    if not meaningful:
        # 如果事实非常短且没有抓到多字关键词，至少要求其主要字符在发言里
        clean_fact = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", fact_text)
        return any(c in corpus for c in clean_fact if c not in _JACCARD_STOPWORDS)

    # 至少有一个关键核心特征词真实出自该观众本人口中
    return any(t in corpus for t in meaningful)


# ==============================================================================
# 4. Lumi_Nox: 事实提取标准 Prompt 模板
# ==============================================================================
VIEWER_FACT_EXTRACTOR_SYSTEM = """你是 AI 直播记忆系统的观众事实提炼器。
目标：只从单条观众发言里提炼“说一次就值得记住”的长期稳定事实。

## 严格自检规则（每条都必须通过）：
1. 稳定性：这条信息一个月后还成立吗？（成立：在杭州做UI、养了两只英短；不成立：今天好困、刚吃了泡面、来看直播了）
2. 可识别性：下次这个观众再来，靠这条信息能认出TA吗？（认出：喜欢被叫做猫猫、在打只狼；不认出：点赞了、哈哈）
3. 自我披露：这是观众自己透露的关于TA自己的信息吗？（是：我是程序员；否：主播今天好漂亮、原神真好玩）
三个问题任何一个答“否” -> should_store=false。

## 类别：
- identity: 称呼、名字、身份标识
- background: 职业、居住地、专业领域
- preference: 长期喜好、口味、对主播的稳定看法
- project: 正在做的事情、计划、作品

## 输出格式（必须为严格纯 JSON，无任何额外文字）：
{
  "should_store": true,
  "category": "background",
  "fact": "在杭州上班做前端开发",
  "stability_reason": "明确交代职业与城市，属于稳定属性",
  "confidence": 0.85
}
如果想不出 stability_reason，必须输出：{"should_store": false}
"""
