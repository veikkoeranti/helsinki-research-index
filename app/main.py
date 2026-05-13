"""
Helsinki Research Index — local app
====================================

Run with:  uvicorn app.main:app --reload --port 5050
Then open: http://127.0.0.1:5050

This is the v1 skeleton. The home page lists neighbourhoods with a paper
count so you can verify the DB is wired correctly. Build out the rest
(map view, paper detail, edit flags, thematic browsing) iteratively via
Claude Code prompts.
"""

import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "index.db"

app = FastAPI(title="Helsinki Research Index")
app.mount("/static", StaticFiles(directory=ROOT / "app" / "static"), name="static")
templates = Jinja2Templates(directory=str(ROOT / "app" / "templates"))


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    """Home: list neighbourhoods + paper counts."""
    with get_conn() as conn:
        rows = conn.execute(
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
    return templates.TemplateResponse(
        request, "index.html", {"neighbourhoods": rows}
    )


@app.get("/neighbourhood/{nbhd_id}", response_class=HTMLResponse)
def neighbourhood(
    request: Request,
    nbhd_id: str,
    show_excluded: int = 0,
    year: Optional[int] = None,
    concept: Optional[str] = None,
):
    """Show one neighbourhood with the papers mapped to it."""
    # Concept filter is applied to the histogram, author/co-discussed panels,
    # and the paper list. Year filter is applied to the paper list only —
    # the histogram is its own state and shouldn't recursively re-filter itself.
    concept_clause = ""
    concept_args: list = []
    if concept:
        concept_clause = (
            "AND EXISTS (SELECT 1 FROM json_each(p.extracted_concepts_json) "
            "WHERE json_each.value = ?)"
        )
        concept_args = [concept]

    with get_conn() as conn:
        nbhd = conn.execute(
            "SELECT id, name_fi, name_sv, major_district, lat, lng, is_quarter "
            "FROM neighbourhood WHERE id = ?",
            (nbhd_id,),
        ).fetchone()
        if nbhd is None:
            raise HTTPException(status_code=404, detail="neighbourhood not found")

        # Year histogram. Concept filter applies; year filter doesn't (would
        # collapse the chart to a single bar).
        hist_rows = conn.execute(
            f"""
            SELECT p.year, COUNT(*) AS n
            FROM paper p
            JOIN paper_neighbourhood pn ON pn.paper_id = p.openalex_id
            WHERE pn.neighbourhood_id = ?
              AND pn.user_excluded = 0
              AND p.user_excluded = 0
              AND p.year IS NOT NULL
              {concept_clause}
            GROUP BY p.year
            ORDER BY p.year
            """,
            [nbhd_id] + concept_args,
        ).fetchall()

        exclude_clause = (
            "" if show_excluded
            else "AND pn.user_excluded = 0 AND p.user_excluded = 0"
        )
        # Most active authors. Uses paper.first_author only — this misses
        # contributions where the relevant author isn't the first listed.
        # When we add a per-author table this query should be replaced.
        top_authors = conn.execute(
            f"""
            SELECT p.first_author, COUNT(*) AS n
            FROM paper p
            JOIN paper_neighbourhood pn ON pn.paper_id = p.openalex_id
            WHERE pn.neighbourhood_id = ?
              AND pn.user_excluded = 0
              AND p.user_excluded = 0
              AND p.first_author IS NOT NULL
              AND p.first_author != 'Unknown'
              {concept_clause}
            GROUP BY p.first_author
            ORDER BY n DESC, p.first_author
            LIMIT 5
            """,
            [nbhd_id] + concept_args,
        ).fetchall()

        co_neighbourhoods = conn.execute(
            f"""
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
              {concept_clause}
            GROUP BY n2.id
            ORDER BY n DESC, n2.name_fi
            LIMIT 5
            """,
            [nbhd_id, nbhd_id] + concept_args,
        ).fetchall()

        # Chip list: top concepts across all non-excluded papers in this
        # neighbourhood. NOT filtered by the active concept (otherwise the
        # chip row would collapse to a single chip when one is selected).
        concept_blobs = conn.execute(
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

        year_clause = ""
        paper_params: list = [nbhd_id] + concept_args
        if year is not None:
            year_clause = "AND p.year = ?"
            paper_params.append(year)

        papers = conn.execute(
            f"""
            SELECT
              p.openalex_id, p.doi, p.title, p.abstract, p.year,
              p.first_author, p.openalex_topic, p.journal,
              p.user_excluded AS paper_excluded,
              pn.user_excluded AS mapping_excluded
            FROM paper_neighbourhood pn
            JOIN paper p ON p.openalex_id = pn.paper_id
            WHERE pn.neighbourhood_id = ?
              {exclude_clause}
              {concept_clause}
              {year_clause}
            ORDER BY p.year DESC NULLS LAST, p.title
            """,
            paper_params,
        ).fetchall()

    # Zero-fill the year range so visual density reflects actual rhythm.
    if hist_rows:
        counts_by_year = {r["year"]: r["n"] for r in hist_rows}
        y_min = min(counts_by_year)
        y_max = max(counts_by_year)
        histogram_data = [(y, counts_by_year.get(y, 0)) for y in range(y_min, y_max + 1)]
    else:
        histogram_data = []

    # Count concept frequencies across the bag of JSON arrays.
    concept_counts: Counter = Counter()
    for row in concept_blobs:
        try:
            for c in json.loads(row["extracted_concepts_json"]) or []:
                if isinstance(c, str) and c:
                    concept_counts[c] += 1
        except (json.JSONDecodeError, TypeError):
            continue
    top_concepts = concept_counts.most_common(8)

    return templates.TemplateResponse(
        request,
        "neighbourhood.html",
        {
            "nbhd": nbhd,
            "papers": papers,
            "show_excluded": bool(show_excluded),
            "histogram_data": histogram_data,
            "selected_year": year,
            "top_authors": top_authors,
            "co_neighbourhoods": co_neighbourhoods,
            "top_concepts": top_concepts,
            "selected_concept": concept,
        },
    )


def _full_openalex_id(short_id: str) -> str:
    return f"https://openalex.org/{short_id}"


def _load_mapping_row(conn: sqlite3.Connection, paper_id: str, nbhd_id: str):
    return conn.execute(
        """
        SELECT pn.neighbourhood_id, pn.source, pn.user_excluded, pn.user_added,
               n.name_fi
        FROM paper_neighbourhood pn
        JOIN neighbourhood n ON n.id = pn.neighbourhood_id
        WHERE pn.paper_id = ? AND pn.neighbourhood_id = ?
        """,
        (paper_id, nbhd_id),
    ).fetchone()


@app.get("/paper/{short_id}", response_class=HTMLResponse)
def paper_detail(request: Request, short_id: str):
    full_id = _full_openalex_id(short_id)
    with get_conn() as conn:
        paper = conn.execute(
            "SELECT * FROM paper WHERE openalex_id = ?", (full_id,)
        ).fetchone()
        if paper is None:
            raise HTTPException(status_code=404, detail="paper not found")

        mappings = conn.execute(
            """
            SELECT pn.neighbourhood_id, pn.source, pn.user_excluded, pn.user_added,
                   n.name_fi
            FROM paper_neighbourhood pn
            JOIN neighbourhood n ON n.id = pn.neighbourhood_id
            WHERE pn.paper_id = ?
            ORDER BY pn.user_excluded, n.name_fi
            """,
            (full_id,),
        ).fetchall()

        mapped_ids = {m["neighbourhood_id"] for m in mappings}
        available = conn.execute(
            "SELECT id, name_fi, is_quarter FROM neighbourhood ORDER BY name_fi"
        ).fetchall()
        available_neighbourhoods = [n for n in available if n["id"] not in mapped_ids]

    extracted_concepts = []
    if paper["extracted_concepts_json"]:
        try:
            extracted_concepts = json.loads(paper["extracted_concepts_json"])
        except json.JSONDecodeError:
            pass
    openalex_keywords = []
    if paper["openalex_keywords_json"]:
        try:
            openalex_keywords = json.loads(paper["openalex_keywords_json"])
        except json.JSONDecodeError:
            pass

    return templates.TemplateResponse(
        request,
        "paper.html",
        {
            "paper": paper,
            "short_id": short_id,
            "mappings": mappings,
            "available_neighbourhoods": available_neighbourhoods,
            "extracted_concepts": extracted_concepts,
            "openalex_keywords": openalex_keywords,
        },
    )


@app.post("/paper/{short_id}/exclude")
def paper_toggle_exclude(short_id: str):
    full_id = _full_openalex_id(short_id)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT user_excluded FROM paper WHERE openalex_id = ?", (full_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="paper not found")
        conn.execute(
            "UPDATE paper SET user_excluded = ?, updated_at = datetime('now') "
            "WHERE openalex_id = ?",
            (0 if row["user_excluded"] else 1, full_id),
        )
        conn.commit()
    return RedirectResponse(f"/paper/{short_id}", status_code=303)


@app.post("/paper/{short_id}/notes")
def paper_save_notes(short_id: str, user_notes: str = Form("")):
    full_id = _full_openalex_id(short_id)
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE paper SET user_notes = ?, updated_at = datetime('now') "
            "WHERE openalex_id = ?",
            (user_notes, full_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="paper not found")
        conn.commit()
    return RedirectResponse(f"/paper/{short_id}", status_code=303)


@app.post("/paper/{short_id}/mapping/{nbhd_id}/exclude", response_class=HTMLResponse)
def mapping_toggle_exclude(request: Request, short_id: str, nbhd_id: str):
    full_id = _full_openalex_id(short_id)
    with get_conn() as conn:
        row = _load_mapping_row(conn, full_id, nbhd_id)
        if row is None:
            raise HTTPException(status_code=404, detail="mapping not found")
        conn.execute(
            "UPDATE paper_neighbourhood SET user_excluded = ? "
            "WHERE paper_id = ? AND neighbourhood_id = ?",
            (0 if row["user_excluded"] else 1, full_id, nbhd_id),
        )
        conn.commit()
        updated = _load_mapping_row(conn, full_id, nbhd_id)

    return templates.TemplateResponse(
        request,
        "_mapping_row.html",
        {"m": updated, "short_id": short_id},
    )


@app.post("/paper/{short_id}/mapping/add")
def mapping_add(short_id: str, neighbourhood_id: str = Form(...)):
    full_id = _full_openalex_id(short_id)
    with get_conn() as conn:
        paper = conn.execute(
            "SELECT 1 FROM paper WHERE openalex_id = ?", (full_id,)
        ).fetchone()
        if paper is None:
            raise HTTPException(status_code=404, detail="paper not found")
        nbhd = conn.execute(
            "SELECT 1 FROM neighbourhood WHERE id = ?", (neighbourhood_id,)
        ).fetchone()
        if nbhd is None:
            raise HTTPException(status_code=404, detail="neighbourhood not found")
        conn.execute(
            """
            INSERT INTO paper_neighbourhood
              (paper_id, neighbourhood_id, source, user_excluded, user_added)
            VALUES (?, ?, 'manual', 0, 1)
            ON CONFLICT(paper_id, neighbourhood_id) DO UPDATE SET
              user_excluded = 0,
              user_added = 1
            """,
            (full_id, neighbourhood_id),
        )
        conn.commit()
    return RedirectResponse(f"/paper/{short_id}", status_code=303)


CITY_SCALE_VALUES = ("city", "region", "nordic", "international")
CITY_SCALE_SORTS = {
    "year_desc": "p.year DESC NULLS LAST, p.title",
    "year_asc":  "p.year ASC NULLS LAST, p.title",
    "author":    "p.first_author COLLATE NOCASE, p.year DESC",
    "scale":     "p.extracted_scale, p.year DESC",
}


@app.get("/city-scale", response_class=HTMLResponse)
def city_scale(request: Request, scale: str = "", sort: str = "year_desc"):
    """Papers above the neighbourhood scale — can't be pinned to a map."""
    if sort not in CITY_SCALE_SORTS:
        sort = "year_desc"
    scale_filter = scale if scale in CITY_SCALE_VALUES else ""

    with get_conn() as conn:
        counts = dict(conn.execute(
            f"""
            SELECT extracted_scale, COUNT(*)
            FROM paper
            WHERE user_excluded = 0
              AND is_about_helsinki = 1
              AND extracted_scale IN ({','.join('?' * len(CITY_SCALE_VALUES))})
            GROUP BY extracted_scale
            """,
            CITY_SCALE_VALUES,
        ).fetchall())

        params: list = list(CITY_SCALE_VALUES)
        extra = ""
        if scale_filter:
            extra = "AND p.extracted_scale = ?"
            params.append(scale_filter)

        papers = conn.execute(
            f"""
            SELECT
              p.openalex_id, p.doi, p.title, p.abstract, p.year,
              p.first_author, p.journal, p.openalex_topic, p.extracted_scale
            FROM paper p
            WHERE p.user_excluded = 0
              AND p.is_about_helsinki = 1
              AND p.extracted_scale IN ({','.join('?' * len(CITY_SCALE_VALUES))})
              {extra}
            ORDER BY {CITY_SCALE_SORTS[sort]}
            """,
            params,
        ).fetchall()

    return templates.TemplateResponse(
        request,
        "city_scale.html",
        {
            "papers": papers,
            "counts": counts,
            "scales": CITY_SCALE_VALUES,
            "scale_filter": scale_filter,
            "sort": sort,
        },
    )


@app.get("/topics", response_class=HTMLResponse)
def topics_index(request: Request):
    """List distinct OpenAlex topics with >= 3 Helsinki papers."""
    with get_conn() as conn:
        topics = conn.execute(
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
    return templates.TemplateResponse(
        request,
        "topics_index.html",
        {"topics": topics},
    )


@app.get("/topic/{topic_name:path}", response_class=HTMLResponse)
def topic_detail(
    request: Request,
    topic_name: str,
    year: Optional[int] = None,
):
    """Papers under one OpenAlex topic, with year histogram + author panel.

    The histogram is its own state — year filter does NOT apply to it (would
    collapse to a single bar). Authors and the paper list both honour year.
    """
    with get_conn() as conn:
        # 404 only if the topic has *no* papers at all (any year).
        total_for_topic = conn.execute(
            """
            SELECT COUNT(*) FROM paper p
            WHERE p.user_excluded = 0
              AND p.is_about_helsinki = 1
              AND p.openalex_topic = ?
            """,
            (topic_name,),
        ).fetchone()[0]
        if total_for_topic == 0:
            raise HTTPException(status_code=404, detail="topic not found")

        hist_rows = conn.execute(
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
            (topic_name,),
        ).fetchall()

        year_clause = ""
        filter_params: list = [topic_name]
        if year is not None:
            year_clause = "AND p.year = ?"
            filter_params.append(year)

        top_authors = conn.execute(
            f"""
            SELECT p.first_author, COUNT(*) AS n
            FROM paper p
            WHERE p.openalex_topic = ?
              AND p.is_about_helsinki = 1
              AND p.user_excluded = 0
              AND p.first_author IS NOT NULL
              AND p.first_author != 'Unknown'
              {year_clause}
            GROUP BY p.first_author
            ORDER BY n DESC, p.first_author
            LIMIT 5
            """,
            filter_params,
        ).fetchall()

        papers = conn.execute(
            f"""
            SELECT
              p.openalex_id, p.doi, p.title, p.abstract, p.year,
              p.first_author, p.journal, p.openalex_topic
            FROM paper p
            WHERE p.user_excluded = 0
              AND p.is_about_helsinki = 1
              AND p.openalex_topic = ?
              {year_clause}
            ORDER BY p.year DESC NULLS LAST, p.title
            """,
            filter_params,
        ).fetchall()

    if hist_rows:
        counts_by_year = {r["year"]: r["n"] for r in hist_rows}
        y_min = min(counts_by_year)
        y_max = max(counts_by_year)
        histogram_data = [(y, counts_by_year.get(y, 0)) for y in range(y_min, y_max + 1)]
    else:
        histogram_data = []

    return templates.TemplateResponse(
        request,
        "topic.html",
        {
            "topic_name": topic_name,
            "papers": papers,
            "selected_year": year,
            "total_for_topic": total_for_topic,
            "histogram_data": histogram_data,
            "top_authors": top_authors,
        },
    )


@app.get("/map", response_class=HTMLResponse)
def map_view(request: Request, show_empty: int = 0):
    """Choropleth of kaupunginosa polygons + CircleMarkers for the 14 quarters."""
    with get_conn() as conn:
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

    counts_by_id: dict[str, int] = {r["id"]: r["paper_count"] for r in rows}
    quarter_points = [
        {
            "id": r["id"],
            "name_fi": r["name_fi"],
            "lat": r["lat"],
            "lng": r["lng"],
            "paper_count": r["paper_count"],
        }
        for r in rows
        if r["is_quarter"] and (show_empty or r["paper_count"] > 0)
    ]
    return templates.TemplateResponse(
        request,
        "map.html",
        {
            "counts_by_id": counts_by_id,
            "quarter_points": quarter_points,
            "show_empty": bool(show_empty),
        },
    )


@app.get("/healthz")
def healthz():
    return {"ok": True}
