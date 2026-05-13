"""
Render the Helsinki Research Index as a static site into docs/ for GitHub Pages.

Read-only: no edit controls, no htmx, no filter wiring. Templates check the
`static_build` and `read_only` flags to suppress those bits. Filter chips
render as plain pills; histogram bars render without anchors. The dynamic
?year= / ?concept= / ?topic= filter chips would 404 on a static host, so
they're deliberately neutered.

Usage:
    python scripts/build_static.py

No flags. Reads data/index.db relative to the project root. Optional
environment variable CNAME controls whether a docs/CNAME file is written.

We duplicate the query shapes from app/main.py rather than refactoring the
route handlers — the route handlers interleave SQL with filter params
that the static build doesn't need. Keep these queries in sync if you
change the route handlers (the diff is small; the templates are the
authoritative consumer either way).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
import time
import traceback
from collections import Counter
from pathlib import Path
from typing import Any, Optional

# Make `app.template_helpers` importable when running this script directly.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.template_helpers import slugify_topic, topic_url_static

DB_PATH = ROOT / "data" / "index.db"
TEMPLATES_DIR = ROOT / "app" / "templates"
STATIC_SRC = ROOT / "app" / "static"
OUT = ROOT / "docs"


# ---------------------------------------------------------------------------
# Jinja env
# ---------------------------------------------------------------------------

def make_env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
    )
    env.globals["topic_url"] = topic_url_static
    env.globals["static_build"] = True
    # Templates check `request` (FastAPI passes one) to decide URL building
    # for {% url_for %} — we don't use that, so leave it unset.
    return env


# ---------------------------------------------------------------------------
# DB helpers — keep these in lockstep with app/main.py route queries.
# ---------------------------------------------------------------------------

def open_db() -> sqlite3.Connection:
    if not DB_PATH.exists():
        sys.exit(f"missing db: {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_index_neighbourhoods(conn: sqlite3.Connection):
    return conn.execute(
        """
        SELECT
          n.id, n.name_fi, n.name_sv, n.major_district, n.is_quarter,
          COUNT(DISTINCT CASE
            WHEN pn.user_excluded = 0 AND p.user_excluded = 0
            THEN pn.paper_id
          END) AS paper_count
        FROM neighbourhood n
        LEFT JOIN paper_neighbourhood pn ON pn.neighbourhood_id = n.id
        LEFT JOIN paper p ON p.openalex_id = pn.paper_id
        GROUP BY n.id
        ORDER BY paper_count DESC, n.name_fi
        """
    ).fetchall()


def fetch_neighbourhood_meta(conn: sqlite3.Connection, nbhd_id: str):
    return conn.execute(
        "SELECT id, name_fi, name_sv, major_district, lat, lng, is_quarter "
        "FROM neighbourhood WHERE id = ?",
        (nbhd_id,),
    ).fetchone()


def fetch_neighbourhood_papers(conn: sqlite3.Connection, nbhd_id: str):
    return conn.execute(
        """
        SELECT
          p.openalex_id, p.doi, p.title, p.abstract, p.year,
          p.first_author, p.openalex_topic, p.journal,
          p.user_excluded AS paper_excluded,
          pn.user_excluded AS mapping_excluded
        FROM paper_neighbourhood pn
        JOIN paper p ON p.openalex_id = pn.paper_id
        WHERE pn.neighbourhood_id = ?
          AND pn.user_excluded = 0
          AND p.user_excluded = 0
        ORDER BY p.year DESC NULLS LAST, p.title
        """,
        (nbhd_id,),
    ).fetchall()


def fetch_histogram(conn: sqlite3.Connection, nbhd_id: str):
    rows = conn.execute(
        """
        SELECT p.year, COUNT(*) AS n
        FROM paper p
        JOIN paper_neighbourhood pn ON pn.paper_id = p.openalex_id
        WHERE pn.neighbourhood_id = ?
          AND pn.user_excluded = 0
          AND p.user_excluded = 0
          AND p.year IS NOT NULL
        GROUP BY p.year
        ORDER BY p.year
        """,
        (nbhd_id,),
    ).fetchall()
    return _zero_fill_years(rows)


def fetch_top_authors(conn: sqlite3.Connection, nbhd_id: str):
    return conn.execute(
        """
        SELECT p.first_author, COUNT(*) AS n
        FROM paper p
        JOIN paper_neighbourhood pn ON pn.paper_id = p.openalex_id
        WHERE pn.neighbourhood_id = ?
          AND pn.user_excluded = 0
          AND p.user_excluded = 0
          AND p.first_author IS NOT NULL
          AND p.first_author != 'Unknown'
        GROUP BY p.first_author
        ORDER BY n DESC, p.first_author
        LIMIT 5
        """,
        (nbhd_id,),
    ).fetchall()


def fetch_co_neighbourhoods(conn: sqlite3.Connection, nbhd_id: str):
    return conn.execute(
        """
        SELECT n2.id, n2.name_fi, COUNT(DISTINCT pn1.paper_id) AS n
        FROM paper_neighbourhood pn1
        JOIN paper_neighbourhood pn2 ON pn2.paper_id = pn1.paper_id
        JOIN neighbourhood n2 ON n2.id = pn2.neighbourhood_id
        JOIN paper p ON p.openalex_id = pn1.paper_id
        WHERE pn1.neighbourhood_id = ?
          AND pn2.neighbourhood_id != ?
          AND pn1.user_excluded = 0
          AND pn2.user_excluded = 0
          AND p.user_excluded = 0
        GROUP BY n2.id
        ORDER BY n DESC, n2.name_fi
        LIMIT 5
        """,
        (nbhd_id, nbhd_id),
    ).fetchall()


def fetch_top_concepts(conn: sqlite3.Connection, nbhd_id: str):
    blobs = conn.execute(
        """
        SELECT p.extracted_concepts_json
        FROM paper p
        JOIN paper_neighbourhood pn ON pn.paper_id = p.openalex_id
        WHERE pn.neighbourhood_id = ?
          AND pn.user_excluded = 0
          AND p.user_excluded = 0
          AND p.extracted_concepts_json IS NOT NULL
        """,
        (nbhd_id,),
    ).fetchall()
    counts: Counter = Counter()
    for row in blobs:
        try:
            for c in json.loads(row["extracted_concepts_json"]) or []:
                if isinstance(c, str) and c:
                    counts[c] += 1
        except (json.JSONDecodeError, TypeError):
            continue
    return counts.most_common(8)


def fetch_papers_for_export(conn: sqlite3.Connection):
    """Every is_about_helsinki, non-excluded paper — used for per-paper pages."""
    return conn.execute(
        """
        SELECT * FROM paper
        WHERE user_excluded = 0 AND is_about_helsinki = 1
        ORDER BY openalex_id
        """
    ).fetchall()


def fetch_paper_mappings(conn: sqlite3.Connection, paper_id: str):
    return conn.execute(
        """
        SELECT pn.neighbourhood_id, pn.source, pn.user_excluded, pn.user_added,
               n.name_fi
        FROM paper_neighbourhood pn
        JOIN neighbourhood n ON n.id = pn.neighbourhood_id
        WHERE pn.paper_id = ?
        ORDER BY pn.user_excluded, n.name_fi
        """,
        (paper_id,),
    ).fetchall()


def fetch_topics_index(conn: sqlite3.Connection):
    return conn.execute(
        """
        SELECT p.openalex_topic AS topic, COUNT(*) AS paper_count
        FROM paper p
        WHERE p.user_excluded = 0
          AND p.is_about_helsinki = 1
          AND p.openalex_topic IS NOT NULL
          AND p.openalex_topic != ''
        GROUP BY p.openalex_topic
        HAVING paper_count >= 3
        ORDER BY paper_count DESC, p.openalex_topic
        """
    ).fetchall()


def fetch_topic_papers(conn: sqlite3.Connection, topic: str):
    return conn.execute(
        """
        SELECT
          p.openalex_id, p.doi, p.title, p.abstract, p.year,
          p.first_author, p.journal, p.openalex_topic
        FROM paper p
        WHERE p.user_excluded = 0
          AND p.is_about_helsinki = 1
          AND p.openalex_topic = ?
        ORDER BY p.year DESC NULLS LAST, p.title
        """,
        (topic,),
    ).fetchall()


def fetch_topic_histogram(conn: sqlite3.Connection, topic: str):
    rows = conn.execute(
        """
        SELECT p.year, COUNT(*) AS n
        FROM paper p
        WHERE p.openalex_topic = ?
          AND p.is_about_helsinki = 1
          AND p.user_excluded = 0
          AND p.year IS NOT NULL
        GROUP BY p.year
        ORDER BY p.year
        """,
        (topic,),
    ).fetchall()
    return _zero_fill_years(rows)


def fetch_topic_top_authors(conn: sqlite3.Connection, topic: str):
    return conn.execute(
        """
        SELECT p.first_author, COUNT(*) AS n
        FROM paper p
        WHERE p.openalex_topic = ?
          AND p.is_about_helsinki = 1
          AND p.user_excluded = 0
          AND p.first_author IS NOT NULL
          AND p.first_author != 'Unknown'
        GROUP BY p.first_author
        ORDER BY n DESC, p.first_author
        LIMIT 5
        """,
        (topic,),
    ).fetchall()


def fetch_topic_nbhd_points(conn: sqlite3.Connection, topic: str):
    return conn.execute(
        """
        SELECT n.id, n.name_fi, n.lat, n.lng,
               COUNT(DISTINCT pn.paper_id) AS n_papers
        FROM neighbourhood n
        JOIN paper_neighbourhood pn ON pn.neighbourhood_id = n.id
        JOIN paper p ON p.openalex_id = pn.paper_id
        WHERE p.openalex_topic = ?
          AND p.is_about_helsinki = 1
          AND p.user_excluded = 0
          AND pn.user_excluded = 0
        GROUP BY n.id
        HAVING n_papers > 0
        ORDER BY n_papers DESC
        """,
        (topic,),
    ).fetchall()


def _zero_fill_years(rows):
    if not rows:
        return []
    by_year = {r["year"]: r["n"] for r in rows}
    y_min, y_max = min(by_year), max(by_year)
    return [(y, by_year.get(y, 0)) for y in range(y_min, y_max + 1)]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def short_id(openalex_id: str) -> str:
    return openalex_id.rsplit("/", 1)[-1]


def write_page(path: Path, html: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")


def render_safe(env: Environment, name: str, ctx: dict, errors: list) -> Optional[str]:
    """Render a template; on any error, log and return None instead of aborting."""
    try:
        return env.get_template(name).render(**ctx)
    except Exception as exc:
        errors.append(f"{name}: {exc}")
        traceback.print_exc()
        return None


def build_index(env, conn, errors: list) -> int:
    rows = fetch_index_neighbourhoods(conn)
    html = render_safe(env, "index.html",
                       {"neighbourhoods": rows, "read_only": True}, errors)
    if html is None:
        return 0
    write_page(OUT / "index.html", html)
    return 1


def build_neighbourhood_pages(env, conn, errors: list) -> int:
    written = 0
    rows = conn.execute(
        """
        SELECT DISTINCT n.id
        FROM neighbourhood n
        JOIN paper_neighbourhood pn ON pn.neighbourhood_id = n.id
        JOIN paper p ON p.openalex_id = pn.paper_id
        WHERE pn.user_excluded = 0 AND p.user_excluded = 0
        """
    ).fetchall()
    for row in rows:
        nbhd_id = row["id"]
        nbhd = fetch_neighbourhood_meta(conn, nbhd_id)
        if nbhd is None:
            continue
        ctx = {
            "nbhd": nbhd,
            "papers": fetch_neighbourhood_papers(conn, nbhd_id),
            "show_excluded": False,
            "histogram_data": fetch_histogram(conn, nbhd_id),
            "selected_year": None,
            "top_authors": fetch_top_authors(conn, nbhd_id),
            "co_neighbourhoods": fetch_co_neighbourhoods(conn, nbhd_id),
            "top_concepts": fetch_top_concepts(conn, nbhd_id),
            "selected_concept": None,
            "selected_topic": None,
            "read_only": True,
        }
        html = render_safe(env, "neighbourhood.html", ctx, errors)
        if html is None:
            continue
        write_page(OUT / "neighbourhood" / nbhd_id / "index.html", html)
        written += 1
    return written


def build_paper_pages(env, conn, errors: list) -> int:
    """One page per is_about_helsinki paper. No edit controls."""
    written = 0
    papers = fetch_papers_for_export(conn)
    # Read neighbourhood list once for the "add" select control which the
    # template renders; in read_only mode the form is hidden but the
    # context variable is still iterated, so pass an empty list.
    for p in papers:
        sid = short_id(p["openalex_id"])
        mappings = fetch_paper_mappings(conn, p["openalex_id"])

        extracted_concepts: list = []
        if p["extracted_concepts_json"]:
            try:
                extracted_concepts = json.loads(p["extracted_concepts_json"]) or []
            except (json.JSONDecodeError, TypeError):
                pass
        openalex_keywords: list = []
        if p["openalex_keywords_json"]:
            try:
                openalex_keywords = json.loads(p["openalex_keywords_json"]) or []
            except (json.JSONDecodeError, TypeError):
                pass

        ctx = {
            "paper": p,
            "short_id": sid,
            "mappings": mappings,
            "available_neighbourhoods": [],  # form hidden in read_only
            "extracted_concepts": extracted_concepts,
            "openalex_keywords": openalex_keywords,
            "read_only": True,
        }
        html = render_safe(env, "paper.html", ctx, errors)
        if html is None:
            continue
        write_page(OUT / "paper" / sid / "index.html", html)
        written += 1
    return written


def build_topics_pages(env, conn, errors: list) -> tuple[int, dict]:
    topics = fetch_topics_index(conn)
    html = render_safe(env, "topics_index.html",
                       {"topics": topics, "read_only": True}, errors)
    written = 0
    if html is not None:
        write_page(OUT / "topics" / "index.html", html)
        written += 1

    slug_map: dict = {}
    seen_slugs: set = set()
    for t in topics:
        name = t["topic"]
        slug = slugify_topic(name)
        # Disambiguate slug collisions (rare): append a numeric suffix.
        base = slug
        i = 2
        while slug in seen_slugs:
            slug = f"{base}-{i}"
            i += 1
        seen_slugs.add(slug)
        slug_map[slug] = name

        total = t["paper_count"]
        ctx = {
            "topic_name": name,
            "papers": fetch_topic_papers(conn, name),
            "selected_year": None,
            "total_for_topic": total,
            "histogram_data": fetch_topic_histogram(conn, name),
            "top_authors": fetch_topic_top_authors(conn, name),
            "nbhd_points": [
                {
                    "id": r["id"],
                    "name_fi": r["name_fi"],
                    "lat": r["lat"],
                    "lng": r["lng"],
                    "n_papers": r["n_papers"],
                }
                for r in fetch_topic_nbhd_points(conn, name)
            ],
            "read_only": True,
        }
        page = render_safe(env, "topic.html", ctx, errors)
        if page is None:
            continue
        write_page(OUT / "topic" / slug / "index.html", page)
        written += 1

    return written, slug_map


def build_map_page(env, conn, errors: list) -> int:
    # Same shape as the /map route output, but we'll let the template's
    # existing fetch() of /static/geo/helsinki_neighbourhoods.geojson work
    # by virtue of copying app/static. counts_by_id + quarter_points come
    # from the same SQL.
    rows = conn.execute(
        """
        SELECT
          n.id, n.name_fi, n.lat, n.lng, n.is_quarter,
          COUNT(DISTINCT CASE
            WHEN pn.user_excluded = 0 AND p.user_excluded = 0
            THEN pn.paper_id
          END) AS paper_count
        FROM neighbourhood n
        LEFT JOIN paper_neighbourhood pn ON pn.neighbourhood_id = n.id
        LEFT JOIN paper p ON p.openalex_id = pn.paper_id
        GROUP BY n.id
        """
    ).fetchall()
    counts_by_id = {r["id"]: r["paper_count"] for r in rows}
    quarter_points = [
        {
            "id": r["id"], "name_fi": r["name_fi"],
            "lat": r["lat"], "lng": r["lng"],
            "paper_count": r["paper_count"],
        }
        for r in rows
        if r["is_quarter"] and r["paper_count"] > 0
    ]
    ctx = {
        "counts_by_id": counts_by_id,
        "quarter_points": quarter_points,
        "show_empty": False,
        "read_only": True,
    }
    html = render_safe(env, "map.html", ctx, errors)
    if html is None:
        return 0
    write_page(OUT / "map" / "index.html", html)
    return 1


def build_404(env, errors: list) -> int:
    # Minimal 404 — write a tiny inline page rather than adding a template.
    html = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Not found — Helsinki Research Index</title>
  <link rel="stylesheet" href="/static/css/app.css">
</head>
<body>
  <header class="site-header">
    <div class="shell">
      <a href="/" class="site-title">Helsinki <em>Research Index</em></a>
    </div>
  </header>
  <main class="shell">
    <header class="article-header">
      <p class="eyebrow">404</p>
      <h1>Page <em>not found</em></h1>
      <p class="lede">
        That URL doesn't exist in the static export.
        <a href="/">Back to the neighbourhood list</a>.
      </p>
    </header>
  </main>
</body>
</html>
"""
    write_page(OUT / "404.html", html)
    return 1


def build_places_json(conn: sqlite3.Connection) -> int:
    rows = conn.execute(
        """
        SELECT
          n.id, n.name_fi, n.lat, n.lng,
          COUNT(DISTINCT CASE
            WHEN pn.user_excluded = 0 AND p.user_excluded = 0
            THEN pn.paper_id
          END) AS paper_count
        FROM neighbourhood n
        LEFT JOIN paper_neighbourhood pn ON pn.neighbourhood_id = n.id
        LEFT JOIN paper p ON p.openalex_id = pn.paper_id
        GROUP BY n.id
        HAVING paper_count > 0
        ORDER BY paper_count DESC
        """
    ).fetchall()
    payload = [
        {"id": r["id"], "name_fi": r["name_fi"],
         "lat": r["lat"], "lng": r["lng"], "paper_count": r["paper_count"]}
        for r in rows
    ]
    write_page(OUT / "data" / "places.json",
               json.dumps(payload, ensure_ascii=False, indent=2))
    return len(payload)


# ---------------------------------------------------------------------------
# Base-URL rewriting
# ---------------------------------------------------------------------------

# Every absolute path the templates emit starts with one of these prefixes.
# Order doesn't matter — the trailing \b in the regex disambiguates
# `/topic` from `/topics` etc.
ROUTE_PREFIXES = (
    "static", "neighbourhood", "topic", "topics", "paper", "papers",
    "map", "city-scale", "data",
)


def normalize_base(base: str) -> str:
    """Return a base prefix without trailing slash, or '/' to mean no prefix."""
    if not base or base == "/":
        return "/"
    if not base.startswith("/"):
        base = "/" + base
    base = base.rstrip("/")
    return base or "/"


def rewrite_paths(content: str, base: str) -> str:
    """Prepend `base` to every absolute-path URL we know we emit.

    Matches `/<known-prefix>...` immediately preceded by `"`, `'`, `=`, or
    `(`, which covers: HTML attribute values (href, src), CSS url(), and
    inline-JS string literals. Also handles bare `href="/"` and `src="/"`.
    """
    if base == "/":
        return content
    pattern = re.compile(
        r"(?<=[\"'=(])/(" + "|".join(ROUTE_PREFIXES) + r")\b"
    )
    content = pattern.sub(lambda m: f"{base}/{m.group(1)}", content)
    # Bare-root href/src — keep limited to those attributes so we don't
    # rewrite stray "/" in JS / CSS string literals.
    content = re.sub(r'(href|src)="/"', rf'\1="{base}/"', content)
    return content


def rewrite_output_tree(out_root: Path, base: str) -> int:
    """Apply rewrite_paths to every HTML/CSS file under docs/. Returns count."""
    if base == "/":
        return 0
    n = 0
    for p in out_root.rglob("*"):
        if not p.is_file() or p.suffix not in (".html", ".css"):
            continue
        text = p.read_text(encoding="utf-8")
        rewritten = rewrite_paths(text, base)
        if rewritten != text:
            p.write_text(rewritten, encoding="utf-8")
            n += 1
    return n


def copy_static() -> int:
    dst = OUT / "static"
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(
        STATIC_SRC, dst,
        ignore=shutil.ignore_patterns(".DS_Store", "*.swp", "__pycache__"),
    )
    return sum(1 for _ in dst.rglob("*") if _.is_file())


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Render the Helsinki Research Index to docs/ for static hosting."
    )
    parser.add_argument(
        "--base-url",
        default="/",
        help=(
            "Subpath under which the site will be hosted (e.g. /helsinki-papers/). "
            "Default '/' (site lives at the host root). Rewrites every absolute "
            "URL we emit to be prefixed with this path."
        ),
    )
    args = parser.parse_args()
    base = normalize_base(args.base_url)

    t0 = time.time()
    OUT.mkdir(parents=True, exist_ok=True)
    env = make_env()
    errors: list[str] = []

    with open_db() as conn:
        n_index = build_index(env, conn, errors)
        n_nbhd = build_neighbourhood_pages(env, conn, errors)
        print(f"  neighbourhoods:      {n_nbhd}")
        n_papers = build_paper_pages(env, conn, errors)
        print(f"  papers:              {n_papers}")
        n_topic_pages, slug_map = build_topics_pages(env, conn, errors)
        print(f"  topics (incl index): {n_topic_pages}")
        n_map = build_map_page(env, conn, errors)
        n_places = build_places_json(conn)
        print(f"  places.json rows:    {n_places}")

    # topic_slugs.json sidecar
    write_page(OUT / "data" / "topic_slugs.json",
               json.dumps(slug_map, ensure_ascii=False, indent=2))

    n_404 = build_404(env, errors)
    n_static_files = copy_static()
    print(f"  static files copied: {n_static_files}")

    # GitHub Pages housekeeping.
    (OUT / ".nojekyll").write_text("")
    cname = os.environ.get("CNAME")
    if cname:
        (OUT / "CNAME").write_text(cname.strip() + "\n")
        print(f"  CNAME written: {cname.strip()}")

    # Base-URL rewrite, after everything is on disk.
    if base != "/":
        n_rewritten = rewrite_output_tree(OUT, base)
        print(f"  rewritten with base {base}/: {n_rewritten} files")

    pages_total = n_index + n_nbhd + n_papers + n_topic_pages + n_map + n_404
    elapsed = time.time() - t0
    print()
    print(f"pages generated: {pages_total}")
    print(f"elapsed:         {elapsed:.2f}s")
    if errors:
        print(f"errors:          {len(errors)}")
        for e in errors[:10]:
            print(f"  - {e}")
        if len(errors) > 10:
            print(f"  ... and {len(errors) - 10} more")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
