-- SQLite schema for the Sprinto outreach pipeline. Applied idempotently by src/research/store.py.

CREATE TABLE IF NOT EXISTS runs (
    run_id       TEXT PRIMARY KEY,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    dry_run      INTEGER NOT NULL,
    summary_json TEXT
);

CREATE TABLE IF NOT EXISTS companies (
    run_id       TEXT NOT NULL,
    domain       TEXT NOT NULL,
    status       TEXT NOT NULL,          -- go | no_go | needs_review | retry | excluded
    failing_gate TEXT,
    reason       TEXT,
    score        INTEGER,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (run_id, domain)
);

CREATE TABLE IF NOT EXISTS pages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    domain      TEXT NOT NULL,
    url         TEXT NOT NULL,
    action      TEXT NOT NULL,
    http_status INTEGER,
    outcome     TEXT NOT NULL,           -- ok | not_found | robots_blocked | challenge | error | off_domain
    clean_text  TEXT,
    fetched_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS facts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL,
    domain      TEXT NOT NULL,
    key         TEXT NOT NULL,           -- extract.py schema field
    value_json  TEXT NOT NULL,
    quote       TEXT NOT NULL,
    source_url  TEXT NOT NULL,
    source_type TEXT NOT NULL,           -- news | job_board | wikidata | page
    fact_date   TEXT,                    -- YYYY-MM-DD when known
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dropped_facts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL,
    domain      TEXT NOT NULL,
    key         TEXT NOT NULL,
    value_json  TEXT,
    quote       TEXT,
    source_url  TEXT,
    reason      TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS planner_decisions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT NOT NULL,
    domain       TEXT NOT NULL,
    pass_no      INTEGER NOT NULL,
    decided_by   TEXT NOT NULL,          -- seed | planner | fallback
    actions_json TEXT NOT NULL,
    reasoning    TEXT,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gate_results (
    run_id  TEXT NOT NULL,
    domain  TEXT NOT NULL,
    gate    TEXT NOT NULL,
    status  TEXT NOT NULL,               -- pass | fail | unknown
    reason  TEXT NOT NULL,
    PRIMARY KEY (run_id, domain, gate)
);

CREATE TABLE IF NOT EXISTS llm_calls (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT,
    domain        TEXT,
    purpose       TEXT NOT NULL,         -- extract | planner | draft
    model         TEXT NOT NULL,
    input_tokens  INTEGER,
    output_tokens INTEGER,
    ok            INTEGER NOT NULL,
    created_at    TEXT NOT NULL
);

-- Hunter: one row per domain, ever. A cached domain is never searched again.
CREATE TABLE IF NOT EXISTS hunter_cache (
    domain        TEXT PRIMARY KEY,
    http_status   INTEGER NOT NULL,
    response_json TEXT NOT NULL,
    fetched_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS hunter_company_cache (
    domain        TEXT PRIMARY KEY,          -- Hunter Company Enrichment, one call per domain ever
    http_status   INTEGER NOT NULL,
    response_json TEXT NOT NULL,
    fetched_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS hunter_usage (
    month    TEXT PRIMARY KEY,           -- YYYY-MM (searches) or YYYY-MM/enrich (company lookups)
    searches INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS contacts (
    run_id      TEXT NOT NULL,
    domain      TEXT NOT NULL,
    first_name  TEXT,
    last_name   TEXT,
    email       TEXT,
    position    TEXT,
    matched_title TEXT,
    reason      TEXT NOT NULL,
    PRIMARY KEY (run_id, domain)
);

CREATE TABLE IF NOT EXISTS drafts (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id           TEXT NOT NULL,
    domain           TEXT NOT NULL,
    status           TEXT NOT NULL,      -- drafted | needs_review
    subject          TEXT,
    body             TEXT,
    pain_points_json TEXT,
    hook_fact_id     INTEGER,
    hook_url         TEXT,
    reasoning        TEXT,
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sends (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     TEXT NOT NULL,
    domain     TEXT NOT NULL,
    recipient  TEXT NOT NULL,
    mode       TEXT NOT NULL,            -- sent | dry_run | declined
    message_id TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS hunter_verify_cache (
    email         TEXT PRIMARY KEY,          -- Hunter Email Verifier, one call per address ever
    http_status   INTEGER NOT NULL,
    response_json TEXT NOT NULL,
    fetched_at    TEXT NOT NULL
);

-- App data (same tables as db/schema_pg.sql; JSON stored as text here).
CREATE TABLE IF NOT EXISTS trails (domain TEXT PRIMARY KEY, trail TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS agent_threads (domain TEXT PRIMARY KEY, state TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active', updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS agent_events (id INTEGER PRIMARY KEY AUTOINCREMENT, domain TEXT NOT NULL, node TEXT NOT NULL, tool TEXT, outcome TEXT NOT NULL, detail TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS feedback (id INTEGER PRIMARY KEY AUTOINCREMENT, domain TEXT NOT NULL, draft_version INTEGER NOT NULL DEFAULT 1, rating INTEGER, edited_body TEXT, notes TEXT, reply_status TEXT, lessons_used TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS lessons (id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL, rationale TEXT, evidence TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL, decided_at TEXT);
