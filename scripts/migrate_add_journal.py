"""
Migration: add `journal` column to the `paper` table.

Idempotent — checks existing columns before altering. Run once:
    python scripts/migrate_add_journal.py
"""

import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "index.db"


def main() -> None:
    if not DB_PATH.exists():
        sys.exit(f"missing db: {DB_PATH}")

    conn = sqlite3.connect(DB_PATH)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(paper)")}
    if "journal" in cols:
        print("journal column already present — nothing to do")
        return
    conn.execute("ALTER TABLE paper ADD COLUMN journal TEXT")
    conn.commit()
    print("added paper.journal (TEXT, NULL for existing rows)")


if __name__ == "__main__":
    main()
