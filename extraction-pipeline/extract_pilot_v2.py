"""
Helsinki Urban Research — extraction pilot, v2
==============================================

Changes from v1:
  - Filters by OpenAlex domain (Social Sciences = 2). Drops natural-science noise.
  - Runs multiple targeted queries (one per Helsinki neighbourhood) and merges
    the results, deduping by OpenAlex ID. This biases the sample toward papers
    that mention specific places — which is what the extraction step is being
    tested on.
  - Adds primary_topic.display_name to the output so you can spot-check that
    the domain filter is working as intended.
  - Drops the FI-institution filter. With domain + neighbourhood-name search,
    that filter was probably costing more recall than it bought precision.
    (You can re-enable it by uncommenting one line below.)

Usage:
  export ANTHROPIC_API_KEY=sk-ant-...
  pip install requests anthropic
  python extract_pilot.py
"""

import csv
import json
import os
import time
from typing import Optional

import requests
from anthropic import Anthropic


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

EMAIL = "veikko.eranti@helsinki.fi"
N_PAPERS = 30
ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"
OUTPUT_JSON = "extracted.json"
OUTPUT_CSV = "extracted_for_review.csv"

# OpenAlex domain IDs:
#   1 = Life Sciences
#   2 = Social Sciences  ← this includes humanities, history, planning, sociology, geography
#   3 = Physical Sciences
#   4 = Health Sciences
SOCIAL_SCIENCES_DOMAIN = 2

# Search queries — each is run separately, then results are deduped.
# This list is tunable; add/remove neighbourhoods as needed.
# Note: each query also gets "Helsinki" appended to disambiguate (e.g. "Kallio"
# alone hits papers about a researcher named Kallio).
NEIGHBOURHOOD_QUERIES = [
    "Kallio Helsinki",
    "Punavuori Helsinki",
    "Kontula Helsinki",
    "Mellunmäki Helsinki",
    "Vuosaari Helsinki",
    "Jätkäsaari Helsinki",
    "Kalasatama Helsinki",
    "Pasila Helsinki",
    "Itä-Helsinki neighbourhood",
    "Helsinki neighbourhood segregation",
    "Helsinki gentrification",
    "Helsinki urban planning district",
]

# Per-query result cap. With 12 queries × 10 results, we over-fetch up to ~120
# candidates, dedupe, then process the first N_PAPERS that have abstracts.
PER_QUERY = 10

OPENALEX_URL = "https://api.openalex.org/works"

# Common filter applied to every query:
#   - primary_topic.domain.id:2   → Social Sciences only
#   - authorships.institutions.country_code:FI  ← uncomment to require FI affiliation
COMMON_FILTER = f"primary_topic.domain.id:{SOCIAL_SCIENCES_DOMAIN}"
# COMMON_FILTER += ",authorships.institutions.country_code:FI"

SELECT_FIELDS = (
    "id,doi,title,publication_year,authorships,"
    "abstract_inverted_index,primary_topic,keywords,language"
)


# ---------------------------------------------------------------------------
# Extraction prompt (unchanged from v1)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are extracting structured metadata from an academic
paper about Helsinki. You are given the title and abstract.

Return a JSON object with exactly these fields:

{
  "neighbourhoods": [string, ...],   // Helsinki neighbourhoods that the paper
                                      // discusses as case sites or primary subjects.
                                      // Do NOT include places mentioned only in
                                      // passing or via cited references.
                                      // Use Finnish forms (Kallio, Punavuori,
                                      // Kontula, Jätkäsaari) not Swedish or English.
  "scale": string,                    // one of: "neighbourhood", "city",
                                      //         "region", "nordic", "international"
                                      // — the geographic scope of the analysis
  "concepts": [string, ...],          // 3 to 7 concept tags. Lowercase,
                                      // hyphenated. Use canonical forms where
                                      // possible: "gentrification",
                                      // "rent-gap", "social-mixing", "lähiö",
                                      // "production-of-space", "segregation",
                                      // "right-to-the-city", "ethnography",
                                      // "planning-narratives", "welfare-urbanism"
                                      // etc. Invent only when nothing fits.
  "discipline": string,               // short phrase: "urban sociology",
                                      // "urban geography", "planning theory",
                                      // "urban history", "ethnography", etc.
  "is_about_helsinki": boolean,       // false if Helsinki is just mentioned
                                      // in passing rather than a primary case
  "confidence_notes": string          // one short sentence flagging any
                                      // ambiguity, missing info, or doubts
}

Return ONLY the JSON object. No preamble, no markdown fences, no commentary.
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def reconstruct_abstract(inverted_index: Optional[dict]) -> Optional[str]:
    if not inverted_index:
        return None
    positioned: list[tuple[int, str]] = []
    for word, positions in inverted_index.items():
        for pos in positions:
            positioned.append((pos, word))
    positioned.sort()
    return " ".join(w for _, w in positioned)


def fetch_for_query(query: str) -> list[dict]:
    """Fetch one OpenAlex search query's results."""
    params = {
        "search": query,
        "filter": COMMON_FILTER,
        "per_page": PER_QUERY,
        "select": SELECT_FIELDS,
        "sort": "relevance_score:desc",
        "mailto": EMAIL,
    }
    r = requests.get(OPENALEX_URL, params=params, timeout=30)
    r.raise_for_status()
    return r.json().get("results", [])


def fetch_all_candidates() -> list[dict]:
    """Run every neighbourhood query, dedupe by OpenAlex ID, preserve order."""
    seen: set[str] = set()
    candidates: list[dict] = []
    for q in NEIGHBOURHOOD_QUERIES:
        print(f"  query: {q!r}", end=" → ")
        try:
            results = fetch_for_query(q)
        except requests.HTTPError as e:
            print(f"HTTP error: {e}")
            continue
        new_count = 0
        for paper in results:
            pid = paper.get("id")
            if pid and pid not in seen:
                seen.add(pid)
                candidates.append(paper)
                new_count += 1
        print(f"{len(results)} results, {new_count} new")
        time.sleep(0.4)
    return candidates


def first_author(paper: dict) -> str:
    auths = paper.get("authorships") or []
    if not auths:
        return "Unknown"
    return auths[0].get("author", {}).get("display_name", "Unknown")


def call_claude(client: Anthropic, title: str, abstract: str) -> dict:
    user_msg = f"Title: {title}\n\nAbstract: {abstract}"
    resp = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=800,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )
    text = resp.content[0].text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"_parse_error": True, "_raw": text}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit(
            "ANTHROPIC_API_KEY not set. Get one at https://console.anthropic.com/ "
            "and run:  export ANTHROPIC_API_KEY=sk-ant-..."
        )

    client = Anthropic()
    print("Fetching candidates from OpenAlex...")
    candidates = fetch_all_candidates()
    print(f"\n{len(candidates)} unique candidates after dedupe. "
          f"Processing up to {N_PAPERS}.\n")

    results: list[dict] = []
    processed = 0
    for paper in candidates:
        if processed >= N_PAPERS:
            break

        title = paper.get("title")
        abstract = reconstruct_abstract(paper.get("abstract_inverted_index"))
        if not title or not abstract:
            continue

        author = first_author(paper)
        year = paper.get("publication_year")
        topic = paper.get("primary_topic") or {}
        topic_name = topic.get("display_name") or "?"
        domain_name = (topic.get("domain") or {}).get("display_name") or "?"

        print(f"[{processed + 1:>2}/{N_PAPERS}] {author} ({year}) [{domain_name}]: "
              f"{title[:60]}{'…' if len(title) > 60 else ''}")

        extracted = call_claude(client, title, abstract)

        record = {
            "openalex_id": paper.get("id"),
            "doi": paper.get("doi"),
            "title": title,
            "first_author": author,
            "year": year,
            "language": paper.get("language"),
            "openalex_primary_topic": topic_name,
            "openalex_domain": domain_name,
            "openalex_field": (topic.get("field") or {}).get("display_name"),
            "openalex_keywords": [
                k.get("display_name") for k in (paper.get("keywords") or [])
            ],
            "abstract": abstract,
            "extracted": extracted,
        }
        results.append(record)
        processed += 1
        time.sleep(0.4)

    # ---- Write JSON ----
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print(f"\nWrote {len(results)} records to {OUTPUT_JSON}")

    # ---- Write CSV with empty columns for hand-labelling ----
    with open(OUTPUT_CSV, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "year",
            "first_author",
            "title",
            "openalex_field",
            "openalex_topic",
            "extracted_neighbourhoods",
            "your_neighbourhoods",
            "extracted_concepts",
            "your_concepts",
            "extracted_scale",
            "your_scale",
            "is_about_helsinki",
            "confidence_notes",
            "agreement_1to5",
            "doi",
        ])
        for r in results:
            e = r["extracted"] if isinstance(r["extracted"], dict) else {}
            writer.writerow([
                r["year"],
                r["first_author"],
                r["title"],
                r.get("openalex_field", ""),
                r["openalex_primary_topic"],
                "; ".join(e.get("neighbourhoods", []) or []),
                "",
                "; ".join(e.get("concepts", []) or []),
                "",
                e.get("scale", ""),
                "",
                e.get("is_about_helsinki", ""),
                e.get("confidence_notes", "") or e.get("_parse_error", ""),
                "",
                r["doi"],
            ])
    print(f"Wrote {OUTPUT_CSV} for hand review")


if __name__ == "__main__":
    main()
