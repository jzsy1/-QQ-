"""轻量 BM25 工具检索 — 对标 ToolRegistry 渐进披露，无第三方依赖。"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter

from .registry import get_tool, get_tool_names, register_builtin_tools

_log = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"[\w\u4e00-\u9fff]+", re.UNICODE)
_K1 = 1.2
_B = 0.75
_RRF_K = 60  # RRF 融合常数（Cormack 等标准取值）


def _tokenize(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text or "") if len(t) >= 1]


_CORPUS_CACHE: tuple[int, dict[str, list[str]]] | None = None


def _tool_corpus() -> dict[str, list[str]]:
    global _CORPUS_CACHE
    register_builtin_tools()
    all_names = get_tool_names()
    if _CORPUS_CACHE is not None and _CORPUS_CACHE[0] == len(all_names):
        return _CORPUS_CACHE[1]
    out: dict[str, list[str]] = {}
    for name in all_names:
        tool = get_tool(name) or {}
        blob = " ".join(
            part
            for part in (name, tool.get("description", ""), tool.get("keywords", ""))
            if part
        )
        out[name] = _tokenize(blob)
    _CORPUS_CACHE = (len(all_names), out)
    return out


def _vec_extra_names(user_text: str, names: list[str], ranked: list[str]) -> list[str] | None:
    """向量语义补捞：BM25 零分但语义相关的工具名（按相似度降序）。

    不可用（未启用/服务失败/熔断）返回 None，调用方回退纯 BM25 零回归。
    正分工具原序不动，补捞仅从零分工具中捞（对齐渐进披露语义）。
    """
    try:
        from .tool_vec import hybrid_limits, vec_scores_for

        limits = hybrid_limits()
        if limits is None:
            return None
        min_score, top_k_vec = limits
        vec = vec_scores_for(user_text, names)
        if vec is None:
            return None
        ranked_set = set(ranked)
        scored = [
            (vec[n], n)
            for n in names
            if n not in ranked_set and vec.get(n, -1.0) >= min_score
        ]
        scored.sort(key=lambda x: (-x[0], x[1]))
        picked = [n for _, n in scored[:top_k_vec]]
        if picked:
            _log.info("[vec] 语义补捞 %d: %s", len(picked), picked)
        return picked
    except Exception as e:
        _log.warning("[vec] 语义补捞异常（回退纯 BM25）: %s", e)
        return None


def _hybrid_merge(
    user_text: str, names: list[str], ranked: list[str]
) -> tuple[list[str], dict[str, float]] | None:
    """BM25 排名与向量排名做 RRF 融合。

    返回 (融合后的全量工具名, 向量分映射)；向量不可用时返回 None，
    调用方回退纯 BM25 排序（零回归）。
    """
    try:
        from .tool_vec import vec_scores_for

        vec = vec_scores_for(user_text, names)
    except Exception as e:
        _log.warning("[vec] 混合排序异常（回退纯 BM25）: %s", e)
        return None
    if not vec:
        return None
    vec_ranked = sorted(names, key=lambda n: (-vec.get(n, -1.0), n))
    rrf: dict[str, float] = {}
    for r, n in enumerate(ranked):
        rrf[n] = rrf.get(n, 0.0) + 1.0 / (_RRF_K + r + 1)
    for r, n in enumerate(vec_ranked):
        rrf[n] = rrf.get(n, 0.0) + 1.0 / (_RRF_K + r + 1)
    ordered = sorted(names, key=lambda n: (-rrf.get(n, 0.0), -vec.get(n, -1.0), n))
    return ordered, vec


def rank_tools_by_text(
    user_text: str,
    tool_names: list[str],
    *,
    top_k: int | None = None,
    positive_only: bool = False,
) -> list[str]:
    """按与用户话的相关度重排工具名（BM25 + 向量 RRF 混合主排序）。

    positive_only=True 时只返回得分工（BM25 正分原序 + 向量补捞），
    供 tier0 渐进披露做"核心清单 + 检索补位"的合并。"""
    names = [n for n in tool_names if n]
    if len(names) <= 1:
        return names

    query = _tokenize(user_text)
    if not query:
        return names[:top_k] if top_k else names

    corpus = _tool_corpus()
    docs = [corpus.get(n, _tokenize(n)) for n in names]
    if not any(docs):
        return names[:top_k] if top_k else names

    n_docs = len(docs)
    avgdl = sum(len(d) for d in docs) / max(n_docs, 1)
    df: Counter[str] = Counter()
    for doc in docs:
        for term in set(doc):
            df[term] += 1

    q_tf = Counter(query)
    scores: list[tuple[float, str]] = []
    raw_corpus = corpus
    text_lower = (user_text or "").lower()
    for name, doc in zip(names, docs):
        dl = len(doc) or 1
        tf_doc = Counter(doc)
        score = 0.0
        blob = " ".join(raw_corpus.get(name, []))
        for term in set(doc):
            if len(term) >= 2 and term in text_lower:
                score += 4.0
        for term in query:
            if len(term) >= 2 and term in blob:
                score += 3.0
            if len(term) >= 2 and term in text_lower and term in name.lower():
                score += 2.0
        for term, qf in q_tf.items():
            if term not in tf_doc:
                continue
            idf = math.log(1 + (n_docs - df[term] + 0.5) / (df[term] + 0.5))
            tf = tf_doc[term]
            denom = tf + _K1 * (1 - _B + _B * dl / avgdl)
            score += idf * tf * (_K1 + 1) / max(denom, 1e-9) * qf
        scores.append((score, name))

    scores.sort(key=lambda x: (-x[0], x[1]))
    ranked = [name for sc, name in scores if sc > 0]
    if positive_only:
        # 向量语义补捞：BM25 零分但语义相关的工具补进剩余名额
        extra = _vec_extra_names(user_text, names, ranked)
        merged = ranked + (extra or [])
        if top_k is not None:
            return merged[:top_k]
        return merged
    # 日志：带分数的 top 结果，方便排查检索质量
    top_display = [(f"{sc:.2f}", name) for sc, name in scores[:8] if sc > 0]
    if top_display:
        _log.info("[bm25] query='%s' scored_top=%s", user_text, top_display)

    # 完整池：BM25+向量 RRF 主排序；向量不可用 → BM25 排序 + 尾部语义重排（零回归）
    hybrid = _hybrid_merge(user_text, names, ranked)
    if hybrid is not None:
        ordered, _vec = hybrid
    else:
        tail = [n for n in names if n not in ranked]
        # 尾部（零分工具）按向量相似度降序重排；BM25 全零时向量兜底整表排序
        extra = _vec_extra_names(user_text, names, ranked)
        if extra:
            extra_set = set(extra)
            tail = extra + [n for n in tail if n not in extra_set]
        ordered = ranked + tail
    if top_k is not None:
        return ordered[:top_k]
    return ordered
