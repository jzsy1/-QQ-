"""U2: hybrid retrieval fusion (FTS + vector -> RRF). KEKESI_PCE_P4_20261101_V2."""
import math
import time

MARKER = "KEKESI_PCE_P4_20261101_V2"
RRF_K = 60
POOL_BUDGETS = {"correction": 200, "fact": 300, "knowledge": 250, "pattern": 150}


def _rrf(rank):
    return 1.0 / (RRF_K + rank)


def _time_decay(item, now):
    ts = item.get("created_at_ts") or item.get("timestamp")
    if not ts:
        raw_dt = item.get("updated_at") or item.get("created_at")
        if raw_dt:
            try:
                from datetime import datetime
                ts = datetime.fromisoformat(str(raw_dt).strip().replace("Z", "+00:00")).timestamp()
            except Exception:
                ts = None
    if not ts:
        return 1.0
    try:
        age_days = max(0.0, (now - float(ts)) / 86400.0)
    except Exception:
        return 1.0
    return math.exp(-age_days / 30.0)


def _fuse(fts_hits, vec_hits):
    scores = {}
    order = {}
    for rank, hit in enumerate(fts_hits):
        key = hit.get("id")
        if key is None:
            continue
        scores[key] = scores.get(key, 0.0) + _rrf(rank)
        order.setdefault(key, hit)
    for rank, hit in enumerate(vec_hits):
        key = hit.get("id")
        if key is None:
            continue
        scores[key] = scores.get(key, 0.0) + _rrf(rank)
        order.setdefault(key, hit)
    return scores, order


def hybrid_search(query, fts_search, vec_search=None, top_fts=20, top_vec=20):
    """Fuse FTS Top-N and vector Top-N with RRF, then weight and budget.

    fts_search / vec_search are caller callbacks (query, limit) -> list
    of dicts with at least id/text/pool keys and optional confidence /
    created_at_ts / error_weight. Vector failure falls back to FTS-only.
    Whole items only, never truncated mid-item; per-pool budgets sum
    to at most 900 chars. No table structure is hardcoded here.
    """
    try:
        fts_hits = list(fts_search(query, top_fts)) if fts_search else []
    except Exception:
        fts_hits = []
    vec_hits = []
    if vec_search is not None:
        try:
            vec_hits = list(vec_search(query, top_vec))
        except Exception:
            vec_hits = []
    scores, order = _fuse(fts_hits, vec_hits)
    now = time.time()
    ranked = []
    for key, base in scores.items():
        item = dict(order[key])
        weight = float(item.get("confidence", 0.6) or 0.6)
        if item.get("pool") == "correction":
            weight *= float(item.get("error_weight", 2.0) or 2.0)
        item["_score"] = base * weight * _time_decay(item, now)
        ranked.append(item)
    ranked.sort(key=lambda it: it["_score"], reverse=True)
    out = []
    used = {pool: 0 for pool in POOL_BUDGETS}
    for item in ranked:
        pool = item.get("pool") or "fact"
        budget = POOL_BUDGETS.get(pool, POOL_BUDGETS["fact"])
        text = item.get("text") or item.get("content") or ""
        if used[pool] + len(text) > budget:
            continue
        used[pool] += len(text)
        out.append(item)
    return out
