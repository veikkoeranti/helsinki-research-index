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
from pathlib import Path

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
def neighbourhood(request: Request, nbhd_id: str, show_excluded: int = 0):
    """Show one neighbourhood with the papers mapped to it."""
    with get_conn() as conn:
        nbhd = conn.execute(
            "SELECT id, name_fi, name_sv, major_district, lat, lng, is_quarter "
            "FROM neighbourhood WHERE id = ?",
            (nbhd_id,),
        ).fetchone()
        if nbhd is None:
            raise HTTPException(status_code=404, detail="neighbourhood not found")

        exclude_clause = (
            "" if show_excluded
            else "AND pn.user_excluded = 0 AND p.user_excluded = 0"
        )
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
            ORDER BY p.year DESC NULLS LAST, p.title
            """,
            (nbhd_id,),
        ).fetchall()

    return templates.TemplateResponse(
        request,
        "neighbourhood.html",
        {
            "nbhd": nbhd,
            "papers": papers,
            "show_excluded": bool(show_excluded),
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


@app.get("/healthz")
def healthz():
    return {"ok": True}
