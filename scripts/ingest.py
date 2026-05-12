"""
Ingest extraction-pipeline output into the index DB.

Reads a JSON array of records produced by extract_pilot_v2.py and loads
each into the `paper` table, then resolves extracted.neighbourhoods to
`neighbourhood.id` (case-insensitive vs name_fi / name_sv) and inserts
into `paper_neighbourhood` with source='extracted'.

Idempotent: re-running with the same input produces the same row set.
"""

import argparse
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "index.db"


# UPSERT, not INSERT OR REPLACE: REPLACE deletes the paper row, which
# cascades to paper_neighbourhood and wipes editorial user_excluded flags.
PAPER_INSERT = """
INSERT INTO paper (
    openalex_id, doi, title, abstract, year, first_author,
    authors_json, language, openalex_field, openalex_topic,
    openalex_keywords_json,
    extracted_scale, extracted_concepts_json, extracted_discipline,
    is_about_helsinki, confidence_notes
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(openalex_id) DO UPDATE SET
    doi=excluded.doi,
    title=excluded.title,
    abstract=excluded.abstract,
    year=excluded.year,
    first_author=excluded.first_author,
    authors_json=excluded.authors_json,
    language=excluded.language,
    openalex_field=excluded.openalex_field,
    openalex_topic=excluded.openalex_topic,
    openalex_keywords_json=excluded.openalex_keywords_json,
    extracted_scale=excluded.extracted_scale,
    extracted_concepts_json=excluded.extracted_concepts_json,
    extracted_discipline=excluded.extracted_discipline,
    is_about_helsinki=excluded.is_about_helsinki,
    confidence_notes=excluded.confidence_notes,
    updated_at=datetime('now')
"""


def load_neighbourhood_index(cur: sqlite3.Cursor) -> dict[str, str]:
    """Map lowercased name_fi / name_sv -> neighbourhood.id."""
    idx: dict[str, str] = {}
    for row_id, name_fi, name_sv in cur.execute(
        "SELECT id, name_fi, name_sv FROM neighbourhood"
    ):
        if name_fi:
            idx.setdefault(name_fi.lower(), row_id)
        if name_sv:
            idx.setdefault(name_sv.lower(), row_id)
    return idx


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="path to extracted.json")
    args = parser.parse_args()

    if not args.input.exists():
        sys.exit(f"missing input: {args.input}")
    if not DB_PATH.exists():
        sys.exit(f"missing db: {DB_PATH} (run schema.sql + seed_neighbourhoods.py first)")

    records = json.loads(args.input.read_text(encoding="utf-8"))
    if not isinstance(records, list):
        sys.exit("expected a JSON array at the top level")

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    cur = conn.cursor()

    nbhd_index = load_neighbourhood_index(cur)

    papers_seen = 0
    papers_inserted = 0
    papers_skipped_no_abstract = 0
    mappings_inserted = 0
    unresolved: Counter[str] = Counter()

    for rec in records:
        papers_seen += 1
        abstract = (rec.get("abstract") or "").strip()
        if not abstract:
            papers_skipped_no_abstract += 1
            continue

        extracted = rec.get("extracted") or {}
        openalex_id = rec["openalex_id"]

        cur.execute(
            PAPER_INSERT,
            (
                openalex_id,
                rec.get("doi"),
                rec.get("title") or "",
                abstract,
                rec.get("year"),
                rec.get("first_author"),
                json.dumps(rec["authors"]) if rec.get("authors") is not None else None,
                rec.get("language"),
                rec.get("openalex_field"),
                rec.get("openalex_primary_topic") or rec.get("openalex_topic"),
                json.dumps(rec.get("openalex_keywords")) if rec.get("openalex_keywords") is not None else None,
                extracted.get("scale"),
                json.dumps(extracted.get("concepts")) if extracted.get("concepts") is not None else None,
                extracted.get("discipline"),
                1 if extracted.get("is_about_helsinki") else 0 if "is_about_helsinki" in extracted else None,
                extracted.get("confidence_notes"),
            ),
        )
        papers_inserted += 1

        for raw_name in extracted.get("neighbourhoods") or []:
            name = (raw_name or "").strip()
            if not name:
                continue
            nbhd_id = nbhd_index.get(name.lower())
            if nbhd_id is None:
                unresolved[name] += 1
                continue
            cur.execute(
                "INSERT OR IGNORE INTO paper_neighbourhood "
                "(paper_id, neighbourhood_id, source) VALUES (?, ?, 'extracted')",
                (openalex_id, nbhd_id),
            )
            mappings_inserted += cur.rowcount

    conn.commit()
    conn.close()

    for name, count in sorted(unresolved.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"unresolved neighbourhood: {name!r} ({count}x)", file=sys.stderr)

    print(f"records seen:           {papers_seen}")
    print(f"papers upserted:        {papers_inserted}")
    print(f"papers skipped (empty): {papers_skipped_no_abstract}")
    print(f"mappings inserted:      {mappings_inserted}")
    print(f"unresolved names:       {len(unresolved)} ({sum(unresolved.values())} occurrences)")


if __name__ == "__main__":
    main()
