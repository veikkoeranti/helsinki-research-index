"""
Helsinki Urban Research — full corpus extraction
================================================

Hardened version of extract_pilot_v2.py for running on the full corpus.

Differences from the pilot:
  - Streams results to JSONL (one JSON object per line) as they're produced.
    Safe to Ctrl+C; you don't lose progress.
  - On startup, reads the output file and skips papers already processed.
    Safe to restart; you don't pay for the same paper twice.
  - Paginates each OpenAlex query (up to MAX_PER_QUERY candidates per query).
  - Logs each step plainly so you can watch progress and notice problems early.
  - Writes a sidecar progress log with timestamps for later analysis.

Output:
  - data/extracted.jsonl  (line-delimited JSON; each line is one paper's record)
  - data/extraction.log   (timestamps + summary stats per query)

After completion, convert the JSONL to a list-JSON if your ingest.py expects
that format:
  python -c "import json; print(json.dumps([json.loads(l) for l in open('data/extracted.jsonl')], indent=2))" > data/extracted.json

Usage:
  export ANTHROPIC_API_KEY=sk-
  python extraction-pipeline/extract_full.py
"""

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests
from anthropic import Anthropic


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

EMAIL = "veikko.eranti@helsinki.fi"      # edit
ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_JSONL = ROOT / "data" / "extracted.jsonl"
LOG_PATH = ROOT / "data" / "extraction.log"
NEIGHBOURHOODS_FILE = ROOT / "data" / "helsinki_neighbourhoods.json"

# Per-query cap. At most this many candidates pulled per query, paginated.
# 200 is generous — almost no Helsinki neighbourhood has more than that.
MAX_PER_QUERY = 200
PER_PAGE = 100                        # OpenAlex standard page size

OPENALEX_URL = "https://api.openalex.org/works"
COMMON_FILTER = "primary_topic.domain.id:2"  # Social Sciences

SELECT_FIELDS = (
    "id,doi,title,publication_year,authorships,"
    "abstract_inverted_index,primary_topic,keywords,language,"
    "primary_location"                # for journal/venue
)

# Hard cap on total papers to process this run — safety against runaway costs.
# Set to None to disable.
MAX_TOTAL = None


# ---------------------------------------------------------------------------
# Extraction prompt (same as v2)
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
  "concepts": [string, ...],          // 3 to 7 concept tags. Lowercase, hyphenated.
                                      // Canonical forms preferred: "gentrification",
                                      // "rent-gap", "social-mixing", "lähiö",
                                      // "production-of-space", "segregation",
                                      // "right-to-the-city", "ethnography",
                                      // "planning-narratives", "welfare-urbanism"
  "discipline": string,
  "is_about_helsinki": boolean,
  "confidence_notes": string
}

Return ONLY the JSON object. No preamble, no markdown fences, no commentary.
"""


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

def build_queries() -> list[tuple[str, str]]:
    """Return (label, query) pairs for every neighbourhood + quarter + theme."""
    data = json.loads(NEIGHBOURHOODS_FILE.read_text(encoding="utf-8"))
    queries: list[tuple[str, str]] = []

    for n in data["neighbourhoods"]:
        if n["fi"] == "Aluemeri":
            continue
        queries.append((n["fi"], f'"{n["fi"]}" Helsinki'))

    for q in data.get("common_quarters", []):
        queries.append((f"{q['fi']} (quarter)", f'"{q["fi"]}" Helsinki'))

    queries.extend([
        ("THEME: gentrification", "Helsinki gentrification"),
        ("THEME: segregation", "Helsinki segregation"),
        ("THEME: urban planning", "Helsinki urban planning"),
        ("THEME: lähiö", "Helsinki lähiö suburb"),
        ("THEME: housing", "Helsinki housing"),
        ("THEME: public space", "Helsinki public space"),
        ("THEME: neighborhood", "Helsinki neighborhood"),
        ("THEME: neighbourhood", "Helsinki neighbourhood"),
    ])
    return queries


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def log(msg: str) -> None:
    ts = datetime.now().isoformat(timespec="seconds")
    line = f"{ts}  {msg}"
    print(line)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def reconstruct_abstract(inverted_index: Optional[dict]) -> Optional[str]:
    if not inverted_index:
        return None
    positioned: list[tuple[int, str]] = []
    for word, positions in inverted_index.items():
        for pos in positions:
            positioned.append((pos, word))
    positioned.sort()
    return " ".join(w for _, w in positioned)


def first_author(paper: dict) -> str:
    auths = paper.get("authorships") or []
    if not auths:
        return "Unknown"
    return auths[0].get("author", {}).get("display_name", "Unknown")


def journal(paper: dict) -> Optional[str]:
    pl = paper.get("primary_location") or {}
    src = pl.get("source") or {}
    return src.get("display_name")


def load_already_processed() -> set[str]:
    """Read existing JSONL and return set of openalex_ids already done."""
    done: set[str] = set()
    if not OUTPUT_JSONL.exists():
        return done
    with OUTPUT_JSONL.open(encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
                if rec.get("openalex_id"):
                    done.add(rec["openalex_id"])
            except json.JSONDecodeError:
                continue
    return done


def fetch_candidates(query: str, already_seen: set[str]) -> list[dict]:
    """Paginate a single OpenAlex query up to MAX_PER_QUERY. Dedupes against already_seen."""
    out: list[dict] = []
    cursor = "*"
    while len(out) < MAX_PER_QUERY:
        page_size = min(PER_PAGE, MAX_PER_QUERY - len(out))
        params = {
            "search": query,
            "filter": COMMON_FILTER,
            "per_page": page_size,
            "select": SELECT_FIELDS,
            "cursor": cursor,
            "mailto": EMAIL,
        }
        try:
            r = requests.get(OPENALEX_URL, params=params, timeout=60)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            log(f"  ! OpenAlex error for {query!r}: {e}")
            break
        results = data.get("results", [])
        if not results:
            break
        for p in results:
            if p.get("id") and p["id"] not in already_seen:
                out.append(p)
                already_seen.add(p["id"])
        cursor = data.get("meta", {}).get("next_cursor")
        if not cursor:
            break
        time.sleep(0.3)
    return out


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


def append_record(record: dict) -> None:
    with OUTPUT_JSONL.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY not set. Get one at https://console.anthropic.com/")

    OUTPUT_JSONL.parent.mkdir(parents=True, exist_ok=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

    already_done = load_already_processed()
    log(f"=== run start. Already processed: {len(already_done)} ===")

    client = Anthropic()
    queries = build_queries()

    seen_during_fetch: set[str] = set(already_done)  # avoid re-asking OpenAlex twice
    candidates_by_query: list[tuple[str, list[dict]]] = []

    # Phase 1: fetch all candidates first. This is cheap (no LLM calls) and
    # lets us see the total scale before paying for extraction.
    log("Phase 1: fetching candidates from OpenAlex...")
    for label, q in queries:
        cands = fetch_candidates(q, seen_during_fetch)
        candidates_by_query.append((label, cands))
        log(f"  {label[:40]:40s} → {len(cands):>3} new candidates")

    total_new = sum(len(c) for _, c in candidates_by_query)
    log(f"Total NEW candidates to process: {total_new}")
    if MAX_TOTAL and total_new > MAX_TOTAL:
        log(f"  ! capped at {MAX_TOTAL} (MAX_TOTAL setting)")
        total_new = MAX_TOTAL

    # Phase 2: extract. Each paper gets its own append-to-disk write so a
    # Ctrl+C never loses more than one paper of progress.
    log(f"Phase 2: extracting via {ANTHROPIC_MODEL}...")
    processed_this_run = 0
    api_errors = 0
    already_processed = load_already_processed()

    for label, cands in candidates_by_query:
        for paper in cands:
            if MAX_TOTAL and processed_this_run >= MAX_TOTAL:
                log("Hit MAX_TOTAL, stopping")
                break

            paper_id = paper.get("id")
            if paper_id in already_processed:
                continue

            title = paper.get("title")
            abstract = reconstruct_abstract(paper.get("abstract_inverted_index"))
            if not title or not abstract:
                continue

            try:
                extracted = call_claude(client, title, abstract)
            except Exception as e:
                api_errors += 1
                log(f"  ! Claude error on {paper.get('id')}: {e}")
                if api_errors > 10:
                    log("  ! too many API errors, stopping")
                    return
                time.sleep(5)
                continue

            topic = paper.get("primary_topic") or {}
            record = {
                "openalex_id": paper.get("id"),
                "doi": paper.get("doi"),
                "title": title,
                "first_author": first_author(paper),
                "year": paper.get("publication_year"),
                "language": paper.get("language"),
                "journal": journal(paper),
                "openalex_primary_topic": topic.get("display_name"),
                "openalex_domain": (topic.get("domain") or {}).get("display_name"),
                "openalex_field": (topic.get("field") or {}).get("display_name"),
                "openalex_keywords": [
                    k.get("display_name") for k in (paper.get("keywords") or [])
                ],
                "abstract": abstract,
                "extracted": extracted,
                "query_source": label,
            }
            append_record(record)
            if paper_id:
                already_processed.add(paper_id)
            processed_this_run += 1

            if processed_this_run % 25 == 0:
                log(f"  ... {processed_this_run} processed this run")

            time.sleep(0.2)
        if MAX_TOTAL and processed_this_run >= MAX_TOTAL:
            break

    log(f"=== run complete. Processed this run: {processed_this_run}. "
        f"API errors: {api_errors}. Total in output: "
        f"{len(load_already_processed())} ===")


if __name__ == "__main__":
    main()
