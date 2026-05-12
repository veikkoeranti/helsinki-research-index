"""
Helsinki Urban Research — extraction pilot
==========================================

Pulls ~30 papers from OpenAlex matching Helsinki urban research criteria,
reconstructs abstracts from OpenAlex's inverted-index format, and uses
Claude Haiku to extract neighbourhood mentions and concept tags. Outputs:

  - extracted.json   : full structured records (machine-readable)
  - extracted_for_review.csv : flat table with empty columns for you to
                                hand-label, then compute agreement.

Usage:
  export ANTHROPIC_API_KEY=sk-ant-...
  pip install requests anthropic
  python extract_pilot.py

You will need an API key from https://console.anthropic.com/ (separate
from a Claude.ai Pro/Max subscription — the API is pay-as-you-go and
needs its own credit balance, $5 minimum). Cost for 30 papers on
Haiku 4.5 is well under USD 1.

Notes:
  - OpenAlex is free, no key required. Set EMAIL below to the address
    you want to use for OpenAlex's "polite pool" priority queue.
  - The script over-fetches (50 candidates) and processes the first
    30 that have a usable abstract.
"""

import csv
import json
import os
import time
from typing import Any, Optional

import requests
from anthropic import Anthropic


# ---------------------------------------------------------------------------
# Configuration — edit these
# ---------------------------------------------------------------------------

EMAIL = "veikko.eranti@helsinki.fi"          # OpenAlex polite-pool identifier
N_PAPERS = 30                                # how many to extract
ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"
OUTPUT_JSON = "extracted.json"
OUTPUT_CSV = "extracted_for_review.csv"

# OpenAlex query — tune as needed.
#   - search="Helsinki urban" biases toward urban-themed papers via relevance
#   - country filter limits to Finnish-affiliated work
#   - You can swap this to search="Kallio" or "Kontula" later for targeted runs
OPENALEX_PARAMS = {
    "search": "Helsinki urban",
    "filter": "authorships.institutions.country_code:FI",
    "per_page": 50,
    "select": (
        "id,doi,title,publication_year,authorships,"
        "abstract_inverted_index,primary_topic,keywords,language"
    ),
    "sort": "relevance_score:desc",
}

OPENALEX_URL = "https://api.openalex.org/works"


# ---------------------------------------------------------------------------
# Extraction prompt
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
    """OpenAlex stores abstracts as {word: [positions]}. Rebuild the prose."""
    if not inverted_index:
        return None
    positioned: list[tuple[int, str]] = []
    for word, positions in inverted_index.items():
        for pos in positions:
            positioned.append((pos, word))
    positioned.sort()
    return " ".join(w for _, w in positioned)


def fetch_candidates() -> list[dict]:
    params = dict(OPENALEX_PARAMS, mailto=EMAIL)
    r = requests.get(OPENALEX_URL, params=params, timeout=30)
    r.raise_for_status()
    return r.json().get("results", [])


def first_author(paper: dict) -> str:
    auths = paper.get("authorships") or []
    if not auths:
        return "Unknown"
    return auths[0].get("author", {}).get("display_name", "Unknown")


def call_claude(client: Anthropic, title: str, abstract: str) -> dict:
    """Single Haiku call. Returns parsed JSON, or an error stub."""
    user_msg = f"Title: {title}\n\nAbstract: {abstract}"
    resp = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=800,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )
    text = resp.content[0].text.strip()
    # Tolerate markdown fences if the model adds them
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
    print(f"Fetching candidates from OpenAlex...")
    candidates = fetch_candidates()
    print(f"Got {len(candidates)} candidates. Processing up to {N_PAPERS}.\n")

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
        print(f"[{processed + 1:>2}/{N_PAPERS}] {author} ({year}): "
              f"{title[:70]}{'…' if len(title) > 70 else ''}")

        extracted = call_claude(client, title, abstract)

        topic = paper.get("primary_topic") or {}
        record = {
            "openalex_id": paper.get("id"),
            "doi": paper.get("doi"),
            "title": title,
            "first_author": author,
            "year": year,
            "language": paper.get("language"),
            "openalex_primary_topic": topic.get("display_name"),
            "openalex_keywords": [
                k.get("display_name") for k in (paper.get("keywords") or [])
            ],
            "abstract": abstract,
            "extracted": extracted,
        }
        results.append(record)
        processed += 1
        time.sleep(0.4)  # Be polite to both APIs

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
            "extracted_neighbourhoods",
            "your_neighbourhoods",          # to be filled in by you
            "extracted_concepts",
            "your_concepts",                # to be filled in by you
            "extracted_scale",
            "your_scale",                   # to be filled in by you
            "is_about_helsinki",
            "confidence_notes",
            "openalex_topic",
            "openalex_keywords",
            "agreement_1to5",               # your overall verdict
            "doi",
        ])
        for r in results:
            e = r["extracted"] if isinstance(r["extracted"], dict) else {}
            writer.writerow([
                r["year"],
                r["first_author"],
                r["title"],
                "; ".join(e.get("neighbourhoods", []) or []),
                "",
                "; ".join(e.get("concepts", []) or []),
                "",
                e.get("scale", ""),
                "",
                e.get("is_about_helsinki", ""),
                e.get("confidence_notes", "") or e.get("_parse_error", ""),
                r["openalex_primary_topic"],
                "; ".join(r["openalex_keywords"] or []),
                "",
                r["doi"],
            ])
    print(f"Wrote {OUTPUT_CSV} for hand review")


if __name__ == "__main__":
    main()
