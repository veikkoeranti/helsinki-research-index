"""
estimate_corpus.py
==================

For each Helsinki neighbourhood (and a few thematic queries), hit OpenAlex
with a per_page=1 request and read the meta.count field — this gives the
total matching papers for each query without downloading any of them. Then
do a second pass that fetches actual IDs and reports the deduped union size
across the top-N most populous queries.

Run this BEFORE running the main extraction pipeline. It tells you:
  - whether the corpus is 50, 500, or 5000 papers (very different projects)
  - which neighbourhoods are over-represented and which have ~no research
  - what fraction of unique papers you lose if you sample only the top queries

No Anthropic API key needed for this — only OpenAlex (which is free).

Usage:
  pip install requests
  python estimate_corpus.py
"""

import json
import sys
import time
from collections import defaultdict
from typing import Optional

import requests


EMAIL = "your-email@example.com"     # edit
NEIGHBOURHOODS_FILE = "helsinki_neighbourhoods.json"
TOP_N_FOR_DEDUP = 20                  # how many of the most populous queries
                                      #   to fetch IDs from for the dedup pass
PER_DEDUP_QUERY = 100                 # how many IDs to fetch per query
                                      #   in the dedup pass (max 200 per OpenAlex)

OPENALEX_URL = "https://api.openalex.org/works"
COMMON_FILTER = "primary_topic.domain.id:2"  # Social Sciences only


def count_only(query: str) -> Optional[int]:
    """Get just meta.count for a query."""
    params = {
        "search": query,
        "filter": COMMON_FILTER,
        "per_page": 1,
        "select": "id",
        "mailto": EMAIL,
    }
    try:
        r = requests.get(OPENALEX_URL, params=params, timeout=30)
        r.raise_for_status()
        return r.json().get("meta", {}).get("count")
    except Exception as e:
        print(f"  ! error for {query!r}: {e}", file=sys.stderr)
        return None


def fetch_ids(query: str, n: int) -> list[str]:
    """Get up to n IDs for a query."""
    out: list[str] = []
    cursor = "*"
    while len(out) < n:
        page_size = min(200, n - len(out))
        params = {
            "search": query,
            "filter": COMMON_FILTER,
            "per_page": page_size,
            "select": "id",
            "cursor": cursor,
            "mailto": EMAIL,
        }
        try:
            r = requests.get(OPENALEX_URL, params=params, timeout=30)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            print(f"  ! error fetching IDs for {query!r}: {e}", file=sys.stderr)
            break
        results = data.get("results", [])
        if not results:
            break
        out.extend(r["id"] for r in results)
        cursor = data.get("meta", {}).get("next_cursor")
        if not cursor:
            break
        time.sleep(0.3)
    return out


def main() -> None:
    with open(NEIGHBOURHOODS_FILE) as f:
        data = json.load(f)

    queries: list[tuple[str, str]] = []   # (label, query_string)

    # Per-neighbourhood queries: Finnish name + Helsinki for disambiguation
    for n in data["neighbourhoods"]:
        if n["fi"] == "Aluemeri":  # territorial waters — skip
            continue
        queries.append((n["fi"], f'"{n["fi"]}" Helsinki'))

    # Common quarter names (sub-districts that are de facto neighbourhoods
    # in everyday speech: Kalasatama, Jätkäsaari, Kontula, etc.)
    for q in data["common_quarters"]:
        queries.append((f"{q['fi']} (quarter)", f'"{q["fi"]}" Helsinki'))

    # Thematic queries — papers about Helsinki urban issues that may not
    # name a neighbourhood in title/abstract
    queries.extend([
        ("THEME: Helsinki gentrification", "Helsinki gentrification"),
        ("THEME: Helsinki segregation", "Helsinki segregation"),
        ("THEME: Helsinki urban planning", "Helsinki urban planning"),
        ("THEME: Helsinki suburb / lähiö", "Helsinki lähiö suburb"),
        ("THEME: Helsinki housing", "Helsinki housing"),
        ("THEME: Helsinki public space", "Helsinki public space"),
        ("THEME: Helsinki neighborhood", "Helsinki neighborhood"),
        ("THEME: Helsinki neighbourhood", "Helsinki neighbourhood"),
    ])

    print(f"=== PASS 1: counts for {len(queries)} queries ===\n")
    counts: list[tuple[str, str, int]] = []
    for label, q in queries:
        c = count_only(q)
        if c is None:
            continue
        counts.append((label, q, c))
        print(f"  {label:40s} → {c:>6}")
        time.sleep(0.25)

    counts.sort(key=lambda t: -t[2])

    total_raw = sum(c for _, _, c in counts)
    print(f"\nSum of all counts (with double-counting): {total_raw}")
    print(f"(Real unique paper count will be substantially lower because of overlap.)\n")

    # Pass 2: dedup
    print(f"=== PASS 2: deduping top {TOP_N_FOR_DEDUP} queries "
          f"(fetching up to {PER_DEDUP_QUERY} IDs each) ===\n")
    union: set[str] = set()
    per_query_unique: dict[str, int] = {}

    for i, (label, q, c) in enumerate(counts[:TOP_N_FOR_DEDUP]):
        target = min(c, PER_DEDUP_QUERY)
        ids = fetch_ids(q, target)
        before = len(union)
        union.update(ids)
        added = len(union) - before
        per_query_unique[label] = added
        print(f"  [{i + 1:>2}/{TOP_N_FOR_DEDUP}] {label:40s} fetched {len(ids):>3}, "
              f"new={added:>3}, union total={len(union)}")

    print(f"\n=== Summary ===")
    print(f"Top {TOP_N_FOR_DEDUP} queries union (deduped): {len(union)} papers")
    print(f"Sum of top {TOP_N_FOR_DEDUP} raw counts (with overlap): "
          f"{sum(c for _, _, c in counts[:TOP_N_FOR_DEDUP])}")
    print(f"Overlap ratio (raw-sum / unique): "
          f"{sum(c for _, _, c in counts[:TOP_N_FOR_DEDUP]) / max(1, len(union)):.2f}x")

    # Project upward from observed overlap
    if len(union) > 0:
        observed_ratio = sum(c for _, _, c in counts[:TOP_N_FOR_DEDUP]) / len(union)
        projected_unique = int(total_raw / observed_ratio)
        print(f"\nEXTRAPOLATED total unique papers across ALL {len(counts)} queries: "
              f"~{projected_unique}")
        print("  (This is rough — overlap distribution may differ at the long tail.)")

    # Save details
    out = {
        "all_counts": [{"label": l, "query": q, "count": c} for l, q, c in counts],
        "top_queries_dedup": {
            "queries_used": [l for l, _, _ in counts[:TOP_N_FOR_DEDUP]],
            "unique_total": len(union),
            "per_query_new": per_query_unique,
        },
    }
    with open("corpus_estimate.json", "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print("\nWrote corpus_estimate.json")


if __name__ == "__main__":
    main()
