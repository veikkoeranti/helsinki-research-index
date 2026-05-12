"""
Seed the neighbourhood table from data/helsinki_neighbourhoods.json.
Idempotent — safe to re-run.
"""

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "index.db"
SOURCE = ROOT / "data" / "helsinki_neighbourhoods.json"


def main() -> None:
    if not SOURCE.exists():
        sys.exit(f"missing {SOURCE}")

    data = json.loads(SOURCE.read_text(encoding="utf-8"))
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM neighbourhood")

    # Kaupunginosat (the 60 official boroughs)
    for n in data["neighbourhoods"]:
        cur.execute(
            "INSERT INTO neighbourhood "
            "(id, name_fi, name_sv, major_district, lat, lng, parent_id, is_quarter, notes) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL, 0, ?)",
            (
                n["id"],
                n["fi"],
                n.get("sv"),
                n["major_district"],
                n["coords"][0],
                n["coords"][1],
                n.get("notes"),
            ),
        )

    # Quarters with their own search visibility (Kalasatama, Jätkäsaari, etc.)
    for q in data.get("common_quarters", []):
        parent_id = q["parent"].split("/")[0]
        qid = f"{parent_id}.{q['fi'].lower().replace(' ', '_')}"
        cur.execute(
            "INSERT INTO neighbourhood "
            "(id, name_fi, name_sv, major_district, lat, lng, parent_id, is_quarter, notes) "
            "SELECT ?, ?, ?, major_district, ?, ?, ?, 1, ? FROM neighbourhood WHERE id=?",
            (
                qid,
                q["fi"],
                q.get("sv"),
                q["coords"][0],
                q["coords"][1],
                parent_id,
                q.get("note"),
                parent_id,
            ),
        )

    conn.commit()
    n_total = cur.execute("SELECT COUNT(*) FROM neighbourhood").fetchone()[0]
    n_quarters = cur.execute(
        "SELECT COUNT(*) FROM neighbourhood WHERE is_quarter=1"
    ).fetchone()[0]
    conn.close()
    print(f"seeded {n_total} neighbourhoods ({n_quarters} are quarters)")


if __name__ == "__main__":
    main()
