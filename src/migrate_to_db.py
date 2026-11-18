"""Copy existing results into the app database, so nothing already paid for is redone.

    .venv/bin/python -m src.migrate_to_db              # into local SQLite
    DATABASE_URL=... .venv/bin/python -m src.migrate_to_db   # into Supabase (reads DATABASE_URL from .env too)

Copies: every outputs/runs/<domain>.json decision trail, and the Hunter caches and credit counters
from db/outreach.sqlite (so cached domains are never searched again). Safe to run twice.
"""
from __future__ import annotations

import json
import os
import sqlite3

from .appdata import AppData
from .config import ROOT, load_env

RUNS = ROOT / "outputs" / "runs"
SQLITE = ROOT / "db" / "outreach.sqlite"


def main() -> int:
    load_env()
    target = AppData(run_id="migration")
    where = "Postgres (DATABASE_URL)" if target.pg else "local SQLite"
    n = 0
    for f in sorted(RUNS.glob("*.json")):
        if f.name.startswith("_"):
            continue
        target.save_trail(json.loads(f.read_text()))
        n += 1
    copied = {}
    if target.pg and SQLITE.exists():
        src = sqlite3.connect(str(SQLITE))
        src.row_factory = sqlite3.Row
        for table, key in [("hunter_cache", "domain"), ("hunter_company_cache", "domain"),
                           ("hunter_verify_cache", "email")]:
            rows = src.execute(f"SELECT * FROM {table}").fetchall()
            for r in rows:
                target._upsert(table, **dict(r))
            copied[table] = len(rows)
        rows = src.execute("SELECT * FROM hunter_usage").fetchall()
        for r in rows:
            target._x("INSERT INTO hunter_usage (month, searches) VALUES (?, ?) ON CONFLICT (month) "
                      "DO UPDATE SET searches=GREATEST(hunter_usage.searches, excluded.searches)",
                      (r["month"], r["searches"]))
        target.conn.commit()
        copied["hunter_usage"] = len(rows)
    print(f"Copied {n} decision trails into {where}." + (f" Also: {copied}" if copied else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
