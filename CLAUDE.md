# Helsinki Research Index — project notes for Claude Code

## What this is
A local-first FastAPI + SQLite app for indexing and curating academic research
about Helsinki. Runs on localhost only; single user (the instructor); editable.
Different project from the teaching tool (helsinki-urban-sociology) — keep
them separate.

## Stack rules
- Python 3.11+, FastAPI, SQLite, Jinja2 templates, htmx for interactivity,
  Leaflet for the map. Do NOT introduce React, Vue, Tailwind, or any other
  frontend framework.
- All CSS lives in `app/static/css/app.css` (a copy of the teaching tool's
  CSS — keep visual consistency).
- All HTML is server-rendered via Jinja2. No client-side templating.

## Data model
- `paper` — one row per paper. Source metadata + extraction outputs + two
  user-editable fields (`user_reviewed`, `user_excluded`, `user_notes`).
- `neighbourhood` — the 60 kaupunginosat plus 14 named quarters. Seeded from
  `data/helsinki_neighbourhoods.json`. Do NOT change without asking.
- `paper_neighbourhood` — many-to-many; the per-mapping `user_excluded` flag
  is how false-positive area assignments get cleaned without deleting data.

## False-positive context (important)
The extraction pipeline produces lots of mappings via full-text OpenAlex
search. Many of these are spurious (e.g. papers by an author named "Laakso"
matching the neighbourhood Laakso). The editorial workflow is designed
around this: every mapping is provisional until reviewed. Never auto-delete
mappings; always set `user_excluded = 1` instead.

## Database operations
- The DB file is `data/index.db`. Treat it as the source of truth.
- For schema changes, EDIT `scripts/schema.sql` and write a small migration
  script. Never edit the DB ad hoc.
- Run `sqlite3 data/index.db` for inspection.

## Conventions
- After any significant change, run the app (`uvicorn app.main:app --port 5050`)
  and curl the affected route to confirm it still responds 200.
- Don't `git commit` automatically. Show diffs; let the user commit.
- Keep functions small. Keep templates simple. Server-side rendering means
  you can debug everything with view-source.
- Use htmx attributes (`hx-post`, `hx-target`, `hx-swap`) for in-place edits
  rather than full-page reloads or JavaScript fetch().

## Things to ask before doing
- Adding new database tables or columns
- Restructuring the schema
- Changing the visual design or the CSS file
- Adding new dependencies to requirements.txt
- Anything that would touch the deploy/export pipeline (when that exists)

When running scripts that modify data/index.db or other files in data/, do not use a worktree — work in the project's actual directory. The database is runtime state, not source code, and worktree copies of it create confusion. If you've already done work in a worktree that modified data files, merge those data files back to the project root before finishing.

