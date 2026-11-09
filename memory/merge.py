"""U3: merge-governance pure rules. KEKESI_PCE_P4_20261101_V2."""
MARKER = "KEKESI_PCE_P4_20261101_V2"
PENDING_THRESHOLD = 8
IDLE_SECONDS = 24 * 3600
PROMOTION_MIN_DATES = 3


def validate_batch(items):
    """A summary batch is valid only when every item cites a digest_id."""
    if not items:
        return False
    for it in items:
        if isinstance(it, dict):
            if not it.get("digest_id"):
                return False
        elif not getattr(it, "digest_id", None):
            return False
    return True


def should_trigger(pending_count, has_high_salience=False, idle_seconds=0):
    """Three triggers: 8+ pending, high-salience arrival, or 24h idle."""
    return (
        pending_count >= PENDING_THRESHOLD
        or bool(has_high_salience)
        or idle_seconds >= IDLE_SECONDS
    )


def should_promote(evidence, has_counterexample=False):
    """Promotion needs >=3 independent date/task evidences, no counterexample.

    Evidence items may be date strings or (date, task) tuples; distinct
    string forms count as independent evidences.
    """
    if has_counterexample:
        return False
    try:
        distinct = {str(e) for e in (evidence or [])}
    except Exception:
        return False
    return len(distinct) >= PROMOTION_MIN_DATES


def should_decay(kind):
    """error_corrections never decay."""
    return kind != "error_correction"
