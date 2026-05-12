"""
Re-apply editorial decisions from data/editorial_decisions.json to the DB.

Run this AFTER `scripts/ingest.py` to restore your human review state.
Idempotent: re-running on the same input produces the same DB state.

Workflow when re-building from scratch:
    rm data/index.db
    sqlite3 data/index.db < scripts/schema.sql
    python scripts/seed_neighbourhoods.py
    python scripts/ingest.py data/extracted.json
    python scripts/import_decisions.py

Usage:
    python scripts/import_decisions.py
"""

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "index.db"
IN_PATH = ROOT / "data" / "editorial_decisions.json"


def main() -> None:
    if not IN_PATH.exists():
        print(f"no decisions file at {IN_PATH} — nothing to import")
        return

    data = json.loads(IN_PATH.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1:
        sys.exit(f"unsupported schema_version: {data.get('schema_version')}")

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # Apply paper-level decisions
    paper_count = 0
    paper_missing = 0
    for p in data.get("papers", []):
        result = cur.execute(
            """
            UPDATE paper
            SET user_reviewed = ?, user_excluded = ?, user_notes = ?,
                updated_at = datetime('now')
            WHERE openalex_id = ?
            """,
            (
                int(p["user_reviewed"]),
                int(p["user_excluded"]),
                p.get("user_notes") or None,
                p["openalex_id"],
            ),
        )
        if result.rowcount == 1:
            paper_count += 1
        else:
            paper_missing += 1
            print(f"  ! paper not in DB: {p['openalex_id']}", file=sys.stderr)

    # Apply mapping-level decisions. For "user_added" rows the mapping
    # may not exist in the DB yet (the auto-ingest wouldn't have created
    # it), so we INSERT-or-UPDATE.
    mapping_count = 0
    mapping_inserted = 0
    for m in data.get("mappings", []):
        # Check whether the mapping already exists
        existing = cur.execute(
            "SELECT 1 FROM paper_neighbourhood WHERE paper_id = ? AND neighbourhood_id = ?",
            (m["paper_id"], m["neighbourhood_id"]),
        ).fetchone()

        if existing:
            cur.execute(
                """
                UPDATE paper_neighbourhood
                SET user_excluded = ?, user_added = ?, source = ?
                WHERE paper_id = ? AND neighbourhood_id = ?
                """,
                (
                    int(m["user_excluded"]),
                    int(m["user_added"]),
                    m["source"],
                    m["paper_id"],
                    m["neighbourhood_id"],
                ),
            )
            mapping_count += 1
        else:
            # Need to verify the paper exists before inserting
            paper_exists = cur.execute(
                "SELECT 1 FROM paper WHERE openalex_id = ?", (m["paper_id"],)
            ).fetchone()
            if not paper_exists:
                print(f"  ! mapping refers to missing paper: {m['paper_id']}", file=sys.stderr)
                continue
            cur.execute(
                """
                INSERT INTO paper_neighbourhood
                (paper_id, neighbourhood_id, source, user_excluded, user_added)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    m["paper_id"],
                    m["neighbourhood_id"],
                    m["source"],
                    int(m["user_excluded"]),
                    int(m["user_added"]),
                ),
            )
            mapping_inserted += 1

    conn.commit()
    conn.close()

    print(f"imported decisions:")
    print(f"  papers updated:       {paper_count}")
    print(f"  papers missing in DB: {paper_missing}")
    print(f"  mappings updated:     {mapping_count}")
    print(f"  mappings inserted:    {mapping_inserted}")


if __name__ == "__main__":
    main()
