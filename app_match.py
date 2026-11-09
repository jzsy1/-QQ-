"""应用名模糊/同音匹配 — 拼音、字符相似度、上下文加权（不写死具体应用）。"""

from __future__ import annotations

import logging
import re
from difflib import SequenceMatcher

logger = logging.getLogger(__name__)

_COMPACT_RE = re.compile(r"[\s_\-+\.]+")
# 至少 2 字才做模糊/子串匹配：允许"记事"→"记事本"、"音乐"→"QQ音乐"
# 仍排除 1 字过宽匹配（如"记"→"记事本"）
_MIN_FUZZY_LEN = 2

# 同音/形似匹配最低分（find_desktop_app / find_app_row）
FUZZY_APP_MIN_SCORE = 0.76
# 上下文命中加权
CONTEXT_BOOST = 0.08


def compact_name(text: str) -> str:
    return _COMPACT_RE.sub("", (text or "").strip().lower())


def pinyin_key(text: str) -> str:
    """中文 → 拼音串；无 pypinyin 时返回空（退化为字符相似度）。"""
    text = (text or "").strip()
    if not text:
        return ""
    try:
        from pypinyin import Style, lazy_pinyin

        parts = lazy_pinyin(text, style=Style.NORMAL, errors="ignore")
        return "".join(p for p in parts if p).lower()
    except ImportError:
        return ""


def app_name_similarity(query: str, candidate: str) -> float:
    """0~1：支持子串、字符相似、同音（拼音相同）。"""
    q = (query or "").strip()
    c = (candidate or "").strip()
    if not q or not c:
        return 0.0
    if q == c:
        return 1.0

    cq, cc = compact_name(q), compact_name(c)
    if cq == cc:
        return 0.98
    if len(cq) >= _MIN_FUZZY_LEN and len(cc) >= _MIN_FUZZY_LEN:
        if cq in cc or cc in cq:
            ratio = min(len(cq), len(cc)) / max(len(cq), len(cc), 1)
            return 0.84 + 0.12 * ratio

    sm = SequenceMatcher(None, cq, cc).ratio() if cq and cc else 0.0

    pq, pc = pinyin_key(q), pinyin_key(c)
    if pq and pc:
        if pq == pc:
            return max(sm, 0.88)
        psm = SequenceMatcher(None, pq, pc).ratio()
        if psm >= 0.92:
            return max(sm, 0.86 + 0.1 * psm)

    if sm >= 0.72:
        return 0.52 + 0.45 * sm
    return sm * 0.45 if sm >= 0.58 else 0.0


def _context_boost(query: str, candidate: str, context: list[str] | None) -> float:
    if not context:
        return 0.0
    cc = compact_name(candidate)
    for ctx in context:
        if not ctx:
            continue
        if compact_name(ctx) == cc:
            return CONTEXT_BOOST
        if app_name_similarity(ctx, candidate) >= 0.88:
            return CONTEXT_BOOST
        if app_name_similarity(query, ctx) >= 0.85 and app_name_similarity(ctx, candidate) >= 0.8:
            return CONTEXT_BOOST * 0.75
    return 0.0


def best_app_match(
    query: str,
    candidates: list[str],
    *,
    context: list[str] | None = None,
    min_score: float = FUZZY_APP_MIN_SCORE,
) -> tuple[str, float] | None:
    """在候选显示名中找最佳匹配；context 为最近用过的应用名列表。"""
    q = (query or "").strip()
    if not q or not candidates:
        return None

    best_name = ""
    best_score = 0.0
    seen: set[str] = set()
    for cand in candidates:
        c = (cand or "").strip()
        if not c or c in seen:
            continue
        seen.add(c)
        score = app_name_similarity(q, c) + _context_boost(q, c, context)
        score = min(1.0, score)
        if score > best_score:
            best_score = score
            best_name = c

    if best_score >= min_score and best_name:
        return best_name, best_score
    return None


def query_variants(user_text: str) -> list[str]:
    """从用户句拆出可用于匹配的应用名候选（原句 + 去动词对象）。"""
    from .intent_utils import extract_action_core

    text = (user_text or "").strip()
    if not text:
        return []
    _, core = extract_action_core(text)
    out: list[str] = []
    seen: set[str] = set()

    def add(s: str) -> None:
        s = (s or "").strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)

    add(text)
    if core and len(core) >= 2:
        add(core)
    return out
