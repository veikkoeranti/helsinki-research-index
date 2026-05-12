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

import sqlite3
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
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
              p.first_author, p.openalex_topic,
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


@app.get("/healthz")
def healthz():
    return {"ok": True}
