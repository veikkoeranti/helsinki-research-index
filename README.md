# Helsinki Research Index

A local-first tool for indexing and curating academic research about Helsinki,
mapped to neighbourhood-level granularity. The companion to the teaching tool;
different stack, different lifecycle, sharing only the visual aesthetic.

**Status:** v0 scaffold. Database schema and FastAPI skeleton are in place;
neighbourhoods are seeded. Papers are not yet ingested. Use the Claude Code
prompts in `PROMPTS.md` to build the rest iteratively.

## Architecture in one paragraph

SQLite database (single file in `data/index.db`) holds papers, neighbourhoods,
and the editable many-to-many between them. FastAPI serves Jinja2-rendered
HTML pages styled with the same CSS as the teaching tool. Leaflet draws the
map. htmx will handle in-place edits (flag, reassign) without a SPA framework.
Everything runs on `localhost:5050` — no auth, no deploy, single user.

When ready to share read-only with others, a separate static-export script
will dump the curated DB to JSON + HTML and that goes to GitHub Pages.

## Setup

```bash
# 1. Install Python deps
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. Initialise database
sqlite3 data/index.db < scripts/schema.sql
python scripts/seed_neighbourhoods.py

# 3. Run the app
uvicorn app.main:app --reload --port 5050
# → http://127.0.0.1:5050
```

Stop the server with Ctrl+C.

## Where things live

```
app/
  main.py               # FastAPI routes
  templates/            # Jinja2 templates
  static/css/app.css    # styles (copy of teaching-tool CSS)
data/
  index.db              # SQLite database — the source of truth
  helsinki_neighbourhoods.json   # canonical neighbourhood list
scripts/
  schema.sql            # database DDL
  seed_neighbourhoods.py
  ingest.py             # (to be written) load OpenAlex+extraction outputs into DB
PROMPTS.md              # Claude Code prompts for building features
```

## Data flow

```
OpenAlex API
     │
     ▼ (extract_pilot_v2.py, run separately)
extracted.json  ──►  scripts/ingest.py  ──►  index.db
                                              │
                                              ▼
                                        FastAPI app
```

The extraction pipeline (`extract_pilot_v2.py` from the previous step) stays
external — it's run periodically, output to JSON, then ingested. Keeping it
separate makes the costs visible (API calls) and the pipeline auditable.

## Editorial workflow

Every paper-to-neighbourhood mapping has two flags:
- `paper.user_excluded` — exclude paper entirely (off-topic, junk)
- `paper_neighbourhood.user_excluded` — paper is real, but not about THIS area

Both default to 0 (include). The UI surfaces both, so you can quickly clean
false positives without losing the audit trail.

## What's not built yet

See `PROMPTS.md` — that file contains ready-to-paste Claude Code prompts for
the next features in order: ingestion, neighbourhood detail view, map view,
edit controls, thematic view, city-scale paper view.
