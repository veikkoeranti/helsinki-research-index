"""
Export editorial decisions to a human-readable JSON file.

This dumps every piece of state that a human added or changed in the
research index — paper exclusions, neighbourhood mapping exclusions,
manually added mappings, and user notes. Everything else (paper
metadata, extracted fields) is derivable from re-ingesting OpenAlex,
so isn't exported.

The output file (data/editorial_decisions.json) is meant to be
committed to git. Re-running this script on the same DB state produces
byte-identical output (decisions sorted by key), so git diffs stay
meaningful.

Usage:
    python scripts/export_decisions.py
"""

import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "index.db"
OUT_PATH = ROOT / "data" / "editorial_decisions.json"


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # 1. Papers with any human-set state
    papers = []
    rows = conn.execute(
        """
        SELECT openalex_id, user_reviewed, user_excluded, user_notes
        FROM paper
        WHERE user_reviewed = 1 OR user_excluded = 1 OR (user_notes IS NOT NULL AND user_notes != '')
        ORDER BY openalex_id
        """
    ).fetchall()
    for r in rows:
        papers.append({
            "openalex_id": r["openalex_id"],
            "user_reviewed": bool(r["user_reviewed"]),
            "user_excluded": bool(r["user_excluded"]),
            "user_notes": r["user_notes"] or "",
        })

    # 2. Paper-neighbourhood mappings with any human-set state.
    #    Includes both excluded ("not about this area") and manually added.
    mappings = []
    rows = conn.execute(
        """
        SELECT paper_id, neighbourhood_id, source, user_excluded, user_added
        FROM paper_neighbourhood
        WHERE user_excluded = 1 OR user_added = 1
        ORDER BY paper_id, neighbourhood_id
        """
    ).fetchall()
    for r in rows:
        mappings.append({
            "paper_id": r["paper_id"],
            "neighbourhood_id": r["neighbourhood_id"],
            "source": r["source"],
            "user_excluded": bool(r["user_excluded"]),
            "user_added": bool(r["user_added"]),
        })

    conn.close()

    out = {
        "schema_version": 1,
        "papers": papers,
        "mappings": mappings,
    }
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"exported {len(papers)} paper decisions and {len(mappings)} mapping decisions")
    print(f"  → {OUT_PATH}")
    print()
    print("Now commit it:")
    print("  git add data/editorial_decisions.json")
    print('  git commit -m "Update editorial decisions"')


if __name__ == "__main__":
    main()
