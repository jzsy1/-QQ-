"""Shared fail-closed filtering for forget jobs across retrieval stores."""
from __future__ import annotations

import sqlite3
from pathlib import Path

_ACTIVE = ("pending", "running", "verified", "failed", "pending_vec")


def main_db_path_for_store(store_path=None):
    """Resolve the coordinator database for a store path in this application's data dir."""
    root = Path(__file__).resolve().parents[2]
    if not store_path:
        return root / "kekesi" / "data" / "kekesi.db"
    p = Path(store_path)
    sibling = p.parent / "kekesi.db"
    if p.name == "kekesi.db" or (p.exists() and not sibling.exists()):
        return p
    return sibling


class TombstoneReadError(RuntimeError):
    pass


def read_tombstones(main_db_path: str | Path) -> list[str]:
    """Read active forget keywords; errors propagate so callers can fail closed."""
    path = Path(main_db_path)
    conn = None
    try:
        conn = sqlite3.connect(str(path), timeout=2)
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_forget_jobs'"
        ).fetchone()
        if not exists:
            raise TombstoneReadError("memory_forget_jobs table is missing")
        marks = ",".join("?" for _ in _ACTIVE)
        return [str(r[0]).strip() for r in conn.execute(
            f"SELECT DISTINCT keyword FROM memory_forget_jobs WHERE status IN ({marks})", _ACTIVE
        ) if str(r[0] or "").strip()]
    except sqlite3.Error as exc:
        raise TombstoneReadError(str(exc)) from exc
    finally:
        if conn is not None:
            conn.close()


def is_tombstoned(text: str, tombstones: list[str]) -> bool:
    """Match whole normalized phrases/terms, avoiding substring collateral matches."""
    import re
    haystack = (text or "").casefold()
    for raw in tombstones:
        needle = raw.casefold().strip()
        if not needle:
            continue
        if re.search(r"[\u3400-\u9fff]", needle):
            # Chinese names/phrases normally have no whitespace token boundaries.
            # Require at least two chars for substring matching to avoid single-char collateral.
            if len(needle) == 1:
                if haystack.strip(" ，,。！？!?；;:：\t\r\n") == needle:
                    return True
                continue
            if needle in haystack:
                return True
        elif re.fullmatch(r"[\w -]+", needle):
            pattern = r"(?<!\w)" + re.escape(needle) + r"(?!\w)"
            if re.search(pattern, haystack):
                return True
        elif needle in haystack:
            return True
    return False


def filter_rows(rows, main_db_path, text_of):
    """Return filtered rows; fail closed (empty) if tombstone state is unreadable."""
    try:
        tombstones = read_tombstones(main_db_path)
    except TombstoneReadError:
        return []
    return [row for row in rows if not is_tombstoned(text_of(row), tombstones)]
