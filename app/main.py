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

from fastapi import FastAPI, Request
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


@app.get("/healthz")
def healthz():
    return {"ok": True}
