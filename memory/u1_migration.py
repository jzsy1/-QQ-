"""U1: bitemporal + source + confidence columns. KEKESI_PCE_P4_20261101_V2."""
import sqlite3
from pathlib import Path

MARKER = "KEKESI_PCE_P4_20261101_V2"
TABLES = ("auto_facts", "knowledge_entries", "entity_edges")
NEW_COLUMNS = [
    ("valid_from", "TEXT"),
    ("valid_to", "TEXT"),
    ("superseded_by", "INTEGER"),
    ("source_digest_ids", "TEXT"),
    ("corroboration_count", "INTEGER DEFAULT 0"),
    ("last_reinforced_at", "TEXT"),
]
CONFIDENCE_COLUMN = ("confidence", "REAL DEFAULT 0.6")
_ENSURED = set()


def _repo_root():
    return Path(__file__).resolve().parents[2]


def _candidate_db_paths():
    root = _repo_root()
    return [root / "data" / "kekesi.db", root / "kekesi" / "data" / "kekesi.db"]


def _table_columns(conn, table):
    try:
        return {row[1] for row in conn.execute("PRAGMA table_info(%s)" % table)}
    except Exception:
        return set()


def migrate_db(db_path):
    """Add missing U1 columns to an existing DB file. False when absent."""
    path = Path(db_path)
    if not path.exists():
        return False
    conn = sqlite3.connect(str(path))
    try:
        for table in TABLES:
            cols = _table_columns(conn, table)
            if not cols:
                continue
            for name, decl in list(NEW_COLUMNS) + [CONFIDENCE_COLUMN]:
                if name not in cols:
                    conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, name, decl))
        conn.commit()
        return True
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return False
    finally:
        conn.close()


def ensure_u1_columns():
    """Migrate the first existing default DB, once per process."""
    for path in _candidate_db_paths():
        key = str(path)
        if key in _ENSURED:
            return True
        if path.exists():
            ok = migrate_db(path)
            _ENSURED.add(key)
            return ok
    return False


def supersede_old(conn, table, old_id, new_id, now):
    """Supersede, never delete: old row keeps its history."""
    conn.execute(
        "UPDATE %s SET valid_to = ?, superseded_by = ? WHERE id = ?" % table,
        (now, new_id, old_id),
    )


def record_reinforcement(conn, table, row_id, now):
    conn.execute(
        "UPDATE %s SET corroboration_count = COALESCE(corroboration_count, 0) + 1, "
        "last_reinforced_at = ? WHERE id = ?" % table,
        (now, row_id),
    )


def valid_clause():
    return "valid_to IS NULL"
