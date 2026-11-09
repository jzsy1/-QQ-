"""U4: core-RAM budget split. KEKESI_PCE_P4_20261101_V2."""
MARKER = "KEKESI_PCE_P4_20261101_V2"
PROFILE_BUDGET = 250
TASK_BUDGET = 170
TOTAL_BUDGET = 420


def _truncate_lines(text, budget):
    """Keep whole lines until the budget would overflow.

    Only when a single line alone exceeds the budget is it hard-cut;
    otherwise the block would come out empty.
    """
    out = []
    used = 0
    for line in (text or "").splitlines():
        add = len(line) + (1 if out else 0)
        if used + add > budget:
            if not out:
                return line[:budget]
            break
        out.append(line)
        used += add
    return "\n".join(out)


def split_core_ram(profile_text, task_text):
    """Split the 420-char core block: profile <=250, task <=170.

    Each side is truncated independently on whole lines, so neither
    side can squeeze the other out.
    """
    return (_truncate_lines(profile_text, PROFILE_BUDGET),
            _truncate_lines(task_text, TASK_BUDGET))


def total_chars(profile_text, task_text):
    profile, task = split_core_ram(profile_text, task_text)
    return len(profile) + len(task)
