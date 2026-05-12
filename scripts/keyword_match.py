"""
keyword_match.py
================

Searches paper abstracts for neighbourhood name mentions and inserts
paper_neighbourhood rows with source='keyword' where no mapping already
exists. This supplements (never replaces) the LLM extraction pass.

Design:
  - Word-boundary matching: "Malmi" matches "Malmi" but not "Malminkartano"
  - Matches Finnish AND Swedish names from the neighbourhood table
  - Skips names shorter than MIN_NAME_LENGTH (too many false positives)
  - Respects existing mappings: if a (paper, neighbourhood) pair already
    exists under any source, the keyword match is skipped for that pair
  - source='keyword' in paper_neighbourhood signals lower confidence;
    the app should surface these for editorial review

Usage:
    python scripts/keyword_match.py            # insert matches
    python scripts/keyword_match.py --dry-run  # preview, no DB writes
    python scripts/keyword_match.py --verbose  # show example snippets
"""

import argparse
import re
import sqlite3
import sys
from pathlib import Path
from typing import Optional


ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "index.db"

# A neighbourhood name must NOT be immediately preceded or followed
# by a letter, digit, or hyphen. This handles compound Finnish words
# (Malminkartano) and hyphenated constructions while allowing
# "Kallio," "Kallio." "Kallio)" etc.
WORD_CHARS = r'a-zäöåéA-ZÄÖÅÉ0-9\-'
BOUNDARY_BEFORE = rf'(?<![{WORD_CHARS}])'
BOUNDARY_AFTER  = rf'(?![{WORD_CHARS}])'

# Minimum name length. Short names (Dal=Laakso, Vik=Viikki) match
# too many unrelated words in English and Finnish academic prose.
MIN_NAME_LENGTH = 5

# Names to skip explicitly regardless of length — common words or
# surnames that generate false positives even at 5+ characters.
# Extend this list if the report surfaces obvious noise.
SKIP_NAMES: set[str] = {
    'Böle',    # sv for Pasila — rare but "böle" means settlement in Swedish
    'Haga',    # sv for Haaga — common Scandinavian place name
    'Forsby',  # sv for Koskela — common enough to skip
}


def make_pattern(name: str) -> Optional[re.Pattern]:
    """Return a compiled word-boundary pattern, or None if name should be skipped."""
    if name in SKIP_NAMES:
        return None
    if len(name) < MIN_NAME_LENGTH:
        return None
    escaped = re.escape(name)
    return re.compile(
        BOUNDARY_BEFORE + escaped + BOUNDARY_AFTER,
        re.IGNORECASE | re.UNICODE,
    )


def get_snippet(text: str, match: re.Match, window: int = 90) -> str:
    """Return a short context snippet around a regex match."""
    start = max(0, match.start() - window)
    end = min(len(text), match.end() + window)
    prefix = '…' if start > 0 else ''
    suffix = '…' if end < len(text) else ''
    return prefix + text[start:end].replace('\n', ' ') + suffix


def main(dry_run: bool, verbose: bool) -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # --- Load neighbourhoods and build patterns ---
    neighbourhoods = cur.execute(
        "SELECT id, name_fi, name_sv FROM neighbourhood ORDER BY id"
    ).fetchall()

    patterns: dict[str, list[tuple[str, re.Pattern]]] = {}
    n_skipped_names = 0

    for n in neighbourhoods:
        pairs = []
        for name in [n['name_fi'], n['name_sv']]:
            if not name:
                continue
            p = make_pattern(name)
            if p is not None:
                pairs.append((name, p))
            else:
                n_skipped_names += 1
        if pairs:
            patterns[n['id']] = pairs

    nbhd_names = {n['id']: n['name_fi'] for n in neighbourhoods}

    print(f"Neighbourhoods: {len(neighbourhoods)} total, "
          f"{len(patterns)} with usable patterns "
          f"({n_skipped_names} individual names skipped as too short/generic)")

    # --- Load Helsinki papers with abstracts ---
    papers = cur.execute(
        """
        SELECT openalex_id, title, abstract
        FROM paper
        WHERE is_about_helsinki = 1
          AND abstract IS NOT NULL
          AND abstract != ''
        """
    ).fetchall()
    print(f"Scanning {len(papers)} Helsinki papers…")

    # --- Load existing mappings to avoid duplicates ---
    existing: set[tuple[str, str]] = set(
        (r['paper_id'], r['neighbourhood_id'])
        for r in cur.execute(
            "SELECT paper_id, neighbourhood_id FROM paper_neighbourhood"
        ).fetchall()
    )
    print(f"Existing mappings: {len(existing)} (will skip these pairs)\n")

    # --- Match ---
    to_insert: list[tuple[str, str]] = []      # (paper_id, nbhd_id)
    snippets: list[tuple[str, str, str, str]] = []  # (name, nbhd_fi, title, snippet)

    total_mentions = 0
    already_mapped = 0
    mentions_by_nbhd: dict[str, int] = {}
    new_by_nbhd: dict[str, int] = {}

    for paper in papers:
        abstract = paper['abstract']
        pid = paper['openalex_id']

        for nbhd_id, name_patterns in patterns.items():
            for name, pattern in name_patterns:
                m = pattern.search(abstract)
                if m is None:
                    continue
                # This neighbourhood is mentioned in this abstract
                total_mentions += 1
                mentions_by_nbhd[nbhd_id] = mentions_by_nbhd.get(nbhd_id, 0) + 1

                if (pid, nbhd_id) in existing:
                    already_mapped += 1
                else:
                    to_insert.append((pid, nbhd_id))
                    new_by_nbhd[nbhd_id] = new_by_nbhd.get(nbhd_id, 0) + 1
                    existing.add((pid, nbhd_id))
                    if verbose and len(snippets) < 30:
                        snippets.append((
                            name,
                            nbhd_names.get(nbhd_id, nbhd_id),
                            paper['title'][:70],
                            get_snippet(abstract, m),
                        ))
                # Only one match needed per neighbourhood per paper
                break

    # --- Report ---
    print(f"Total abstract mentions:       {total_mentions}")
    print(f"Already had a mapping:         {already_mapped}")
    print(f"New mappings to add:           {len(to_insert)}")
    print()

    print("Top neighbourhoods — new keyword mappings vs total mentions:")
    top_nbhds = sorted(mentions_by_nbhd.items(), key=lambda x: -x[1])[:20]
    for nbhd_id, total in top_nbhds:
        new = new_by_nbhd.get(nbhd_id, 0)
        already = total - new
        print(f"  {nbhd_names.get(nbhd_id, nbhd_id):28s} "
              f"total={total:>4}  new={new:>4}  already-mapped={already:>4}")

    if verbose and snippets:
        print(f"\nSample new matches ({len(snippets)} shown):")
        for name, nbhd, title, snip in snippets:
            print(f"\n  [{nbhd} / matched '{name}']")
            print(f"  Title:   {title}")
            print(f"  Context: {snip}")

    # --- Write ---
    if dry_run:
        print(f"\n[dry-run] No changes written. "
              f"Re-run without --dry-run to insert {len(to_insert)} mappings.")
        conn.close()
        return

    cur.executemany(
        """
        INSERT OR IGNORE INTO paper_neighbourhood
          (paper_id, neighbourhood_id, source, user_excluded, user_added)
        VALUES (?, ?, 'keyword', 0, 0)
        """,
        to_insert,
    )
    conn.commit()

    actual = cur.execute(
        "SELECT COUNT(*) FROM paper_neighbourhood WHERE source='keyword'"
    ).fetchone()[0]
    print(f"\nInserted. Keyword-source mappings now in DB: {actual}")

    conn.close()
    print("Done. Review these in the app — they appear as source='keyword'.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Keyword-match neighbourhood names against paper abstracts."
    )
    parser.add_argument(
        '--dry-run', action='store_true',
        help='preview matches without writing to DB'
    )
    parser.add_argument(
        '--verbose', action='store_true',
        help='show sample context snippets for new matches'
    )
    args = parser.parse_args()
    main(dry_run=args.dry_run, verbose=args.verbose)
