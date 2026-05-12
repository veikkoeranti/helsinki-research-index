-- Helsinki Research Index — SQLite schema
-- Run via: sqlite3 data/index.db < scripts/schema.sql

-- Drop in correct dependency order for clean re-runs
DROP TABLE IF EXISTS paper_neighbourhood;
DROP TABLE IF EXISTS paper_concept;
DROP TABLE IF EXISTS paper;
DROP TABLE IF EXISTS neighbourhood;

-- The canonical Helsinki neighbourhoods (kaupunginosat + selected quarters).
-- Populated from helsinki_neighbourhoods.json.
CREATE TABLE neighbourhood (
    id              TEXT PRIMARY KEY,        -- '11' for Kallio; '11.kalasatama' for quarters
    name_fi         TEXT NOT NULL,
    name_sv         TEXT,
    major_district  TEXT,
    lat             REAL NOT NULL,
    lng             REAL NOT NULL,
    parent_id       TEXT,                    -- for quarters: parent kaupunginosa id
    is_quarter      INTEGER NOT NULL DEFAULT 0,
    notes           TEXT
);

-- One row per paper. Source metadata immutable; user_* fields are editorial.
CREATE TABLE paper (
    openalex_id     TEXT PRIMARY KEY,        -- e.g. https://openalex.org/W1234
    doi             TEXT,
    title           TEXT NOT NULL,
    abstract        TEXT,
    year            INTEGER,
    first_author    TEXT,
    authors_json    TEXT,                    -- full authors list as JSON
    language        TEXT,
    openalex_field  TEXT,
    openalex_topic  TEXT,
    openalex_keywords_json TEXT,             -- list of strings as JSON
    journal         TEXT,                    -- venue/journal display name

    -- Extraction outputs from Haiku
    extracted_scale         TEXT,            -- neighbourhood | city | region | nordic | international
    extracted_concepts_json TEXT,            -- list as JSON
    extracted_discipline    TEXT,
    is_about_helsinki       INTEGER,         -- boolean (0/1)
    confidence_notes        TEXT,

    -- Editorial state
    user_reviewed   INTEGER NOT NULL DEFAULT 0,   -- 0=auto, 1=human-checked
    user_excluded   INTEGER NOT NULL DEFAULT 0,   -- 0=keep, 1=excluded from corpus
    user_notes      TEXT,

    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Many-to-many: a paper can be about multiple neighbourhoods, and the
-- relationship itself is editable (the false-positive flag lives here).
CREATE TABLE paper_neighbourhood (
    paper_id        TEXT NOT NULL REFERENCES paper(openalex_id) ON DELETE CASCADE,
    neighbourhood_id TEXT NOT NULL REFERENCES neighbourhood(id),
    source          TEXT NOT NULL,           -- 'extracted' | 'manual' | 'query_match'
    user_excluded   INTEGER NOT NULL DEFAULT 0,  -- 1 = "this paper is not about this area"
    user_added      INTEGER NOT NULL DEFAULT 0,  -- 1 = added manually by user
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (paper_id, neighbourhood_id)
);

CREATE INDEX idx_paper_year ON paper(year);
CREATE INDEX idx_paper_helsinki ON paper(is_about_helsinki);
CREATE INDEX idx_paper_scale ON paper(extracted_scale);
CREATE INDEX idx_pn_neighbourhood ON paper_neighbourhood(neighbourhood_id, user_excluded);
CREATE INDEX idx_pn_paper ON paper_neighbourhood(paper_id);
