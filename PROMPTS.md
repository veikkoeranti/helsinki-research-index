# Claude Code prompts — Helsinki Research Index

These are sequenced prompts. Run them one at a time in Claude Code from the
project root, review the changes, commit if good, then move on. Don't paste
all of them at once — each builds on the previous.

Before each prompt: make sure `npm run dev` isn't running (this project uses
uvicorn, not Astro) and that you have run `python scripts/seed_neighbourhoods.py`
at least once.

---

## Prompt 1 — Ingestion script

> Write `scripts/ingest.py`. It should read a JSON file produced by
> `extract_pilot_v2.py` (the `extracted.json` format with fields like
> `openalex_id`, `title`, `abstract`, `extracted.neighbourhoods`, etc.)
> and load each record into the `paper` table. Then, for each value in
> the paper's `extracted.neighbourhoods` list, resolve it to a
> `neighbourhood.id` by case-insensitive match against `name_fi` and
> `name_sv`, and insert into `paper_neighbourhood` with `source='extracted'`.
>
> Skip papers where the abstract is empty, and skip neighbourhood values
> that don't resolve (log them to stderr so we can fix the gazetteer).
>
> The script should be idempotent — re-running it with the same input
> should produce the same DB state, not duplicates. Use INSERT ... ON CONFLICT(openalex_id) DO UPDATE for papers — never INSERT OR REPLACE, because the CASCADE on paper_neighbourhood would silently wipe editorial flags every re-ingest. Use INSERT OR IGNORE for mappings (where the CASCADE issue doesn't apply, since mappings are leaf rows).
>
> Take the input path as a command-line argument. Print a summary at the
> end: papers inserted, mappings inserted, neighbourhood names that
> failed to resolve (with counts).
>
> After writing it, run it against `data/extracted.json` (place a sample
> there first — copy `extracted.json` from the extraction pipeline) and
> show me the summary. Don't commit yet; I'll review.

---

## Prompt 2 — Neighbourhood detail page

> Add a route `GET /neighbourhood/{id}` to `app/main.py` and a template
> `app/templates/neighbourhood.html`.
>
>When running scripts that modify data/index.db or other files in data/, do not use a worktree — work in the project's actual directory. The database is runtime state, not source code, and worktree copies of it create confusion. If you've already done work in a worktree that modified data files, merge those data files back to the project root before finishing.

> The page should show:
> - The neighbourhood name (Finnish + Swedish), major district, and a
>   small Leaflet map centred on its coordinates with a single pin
> - A list of all papers mapped to it, ordered by year descending
> - For each paper: title (linked to the OpenAlex page or DOI if present),
>   first author, year, primary topic from OpenAlex, the first ~250
>   characters of the abstract with "read more" link to a detail page
>   (which we'll build next)
> - Honour the user_excluded flags: by default hide papers where
>   either `paper.user_excluded=1` or the per-mapping `user_excluded=1`,
>   but include a "show excluded" toggle in the URL (`?show_excluded=1`)
>
> Style consistently with the existing index page. Use the same CSS classes
> (article-header, eyebrow, lede, matrix). Don't introduce new CSS.
>
> Update the index page's neighbourhood links so they point to `/neighbourhood/{id}`
> (they already do, but verify the route works end-to-end).
>
> Test by curling `/neighbourhood/11` (Kallio) and showing me the first
> 20 lines of HTML output.

---

## Prompt 3 — Paper detail page with edit controls

> Add `GET /paper/{openalex_id}` returning a template `paper.html` and
> two POST routes for edits:
> - `POST /paper/{openalex_id}/exclude` — toggles `paper.user_excluded`
> - `POST /paper/{openalex_id}/mapping/{neighbourhood_id}/exclude` —
>   toggles `paper_neighbourhood.user_excluded` for that pair
>
> The paper page shows all metadata, full abstract, OpenAlex link, DOI link,
> extracted concepts, all currently-mapped neighbourhoods (each with a
> checkbox "this paper IS about this area" — checked when not excluded —
> that posts to the exclude route), and a free-text `user_notes` field.
>
> Use htmx for the checkboxes (`hx-post`, `hx-swap="outerHTML"`) so the
> click toggles state without reloading. The endpoint should return the
> updated checkbox fragment.
>
> Also add an "Add another neighbourhood" form: select element listing
> all neighbourhoods, posting to `POST /paper/{openalex_id}/mapping/add`
> which inserts a new row with `source='manual'`, `user_added=1`.
>
> After implementing, walk me through testing it manually: which URL to
> open, what to click, what to verify in the database.

---

## Prompt 4 — Map view of all neighbourhoods

> Add a route `GET /map` and template `map.html` that shows a single
> Leaflet map of Helsinki with one pin per neighbourhood. Pin size or
> opacity should reflect paper count (after exclusions).
>
> Clicking a pin opens a popup with: neighbourhood name, paper count,
> and a link to its detail page.
>
> Use Leaflet's `CircleMarker` with radius scaled from paper count
> (e.g. radius = 4 + sqrt(count)), and fill colour from the existing
> `--color-accent` CSS variable. OpenStreetMap tiles.
>
> The map should NOT show neighbourhoods with zero papers (we don't want
> dozens of empty pins). Add a query parameter `?show_empty=1` to override.
>
> Use the existing nav link in `base.html` (it already points to `/map`).

---

## Prompt 5 — Thematic view via OpenAlex topics

> Build `GET /topics` and `GET /topic/{topic_name}` routes. They should
> use the OpenAlex topic tags already stored in `paper.openalex_topic`
> (no extraction needed — use what's there).
>
> `/topics` lists all distinct topics that appear on at least one
> non-excluded paper, with the count of papers for each, ordered by count
> descending. Top 5–10 most common topics shown more prominently.
>
> `/topic/{topic_name}` shows all papers under that topic, plus a Leaflet
> map showing which neighbourhoods those papers are about (pins sized by
> paper count for THIS topic, not the global count).
>
> Update `base.html` nav to add a "Topics" link.
>
> Push back if the topic field is too sparse or too noisy for this to
> work — we may need to fall back to extracted concepts instead.

---

## Prompt 6 — City-scale papers

> Add a route `GET /city-scale` showing all papers where
> `extracted_scale IN ('city', 'region', 'nordic', 'international')` AND
> `is_about_helsinki = 1` AND `user_excluded = 0`.
>
> These are the papers that can't be mapped to a neighbourhood. Show them
> as a sortable/filterable list (by year, author, scale). No map — that's
> the whole point.
>
> Update `base.html` nav to add a "City-scale" link.

---

## Prompt 7 — Index page improvements

> The home page currently lists 74 neighbourhoods alphabetically with
> paper counts. Improve it:
>
> 1. Sort by paper count descending by default; add `?sort=name` to switch
> 2. Group by major district (with collapsible sections, using `<details>`)
> 3. Add a top-of-page summary: total papers in index, total reviewed,
>    total excluded, total mappings flagged false-positive
> 4. Visual: thin horizontal bar in each row whose width reflects paper
>    count relative to the largest. CSS only, no JS.

---

## Things NOT to do yet (defer to later)

- Authentication / user management — keep single-user
- Deployment / static export to GitHub Pages — build the working app first
- Hosted version — localhost only for now
- Adding new database tables without asking
- Concept extraction beyond what OpenAlex provides — defer until corpus is curated
- Bilingual UI — English only for now (the underlying data is already bilingual)
