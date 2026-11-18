"""Persistence. SQLite locally (db/outreach.sqlite); Postgres when DATABASE_URL is set (the Vercel
deployment uses Supabase). Same methods either way; only placeholders, upserts and ids differ."""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from ..config import ROOT

SCHEMA_PATH = ROOT / "db" / "schema.sql"
DEFAULT_DB = ROOT / "db" / "outreach.sqlite"

# Primary keys, for upserts that work on both engines (INSERT ... ON CONFLICT ... DO UPDATE).
PK = {"runs": ["run_id"], "companies": ["run_id", "domain"], "gate_results": ["run_id", "domain", "gate"],
      "hunter_cache": ["domain"], "hunter_company_cache": ["domain"], "hunter_verify_cache": ["email"],
      "contacts": ["run_id", "domain"]}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def pg_connect(url: str | None = None):
    """A psycopg connection to Postgres (Supabase pooler friendly: no server-side prepared statements)."""
    import psycopg
    from psycopg.rows import dict_row
    return psycopg.connect(url or os.environ["DATABASE_URL"], row_factory=dict_row, prepare_threshold=None,
                           autocommit=False, connect_timeout=10)


class Store:
    def __init__(self, path: Path | str | None = None, run_id: str | None = None, pg_url: str | None = None):
        url = pg_url if pg_url is not None else (os.environ.get("DATABASE_URL") if path is None else None)
        self.pg = bool(url)
        if self.pg:
            self.conn = pg_connect(url)          # schema is applied by migration (db/schema_pg.sql)
        else:
            self.conn = sqlite3.connect(str(path or DEFAULT_DB))
            self.conn.row_factory = sqlite3.Row
            self.conn.executescript(SCHEMA_PATH.read_text())
        self.run_id = run_id or "adhoc"

    # --- engine helpers ---------------------------------------------------------------
    def _x(self, sql: str, params: tuple = ()):
        if self.pg:
            sql = sql.replace("?", "%s")
        return self.conn.execute(sql, params)

    def _one(self, sql: str, params: tuple = ()) -> dict | None:
        row = self._x(sql, params).fetchone()
        return dict(row) if row else None

    def _ins(self, table: str, **cols) -> int:
        keys = ", ".join(cols)
        qs = ", ".join("?" for _ in cols)
        sql = f"INSERT INTO {table} ({keys}) VALUES ({qs})"
        if self.pg:
            row = self._x(sql + " RETURNING id", tuple(cols.values())).fetchone()
            self.conn.commit()
            return row["id"]
        cur = self._x(sql, tuple(cols.values()))
        self.conn.commit()
        return cur.lastrowid

    def _upsert(self, table: str, **cols) -> None:
        keys = list(cols)
        pk = PK[table]
        sets = ", ".join(f"{k}=excluded.{k}" for k in keys if k not in pk)
        self._x(f"INSERT INTO {table} ({', '.join(keys)}) VALUES ({', '.join('?' for _ in keys)}) "
                f"ON CONFLICT ({', '.join(pk)}) DO UPDATE SET {sets}", tuple(cols.values()))
        self.conn.commit()

    # --- runs -------------------------------------------------------------
    def start_run(self, dry_run: bool) -> None:
        self._upsert("runs", run_id=self.run_id, started_at=now(), dry_run=int(dry_run))

    def finish_run(self, summary: dict) -> None:
        self._x("UPDATE runs SET finished_at=?, summary_json=? WHERE run_id=?",
                (now(), json.dumps(summary, default=str), self.run_id))
        self.conn.commit()

    # --- research ---------------------------------------------------------
    def page(self, domain, url, action, status, outcome, clean_text=None):
        return self._ins("pages", domain=domain, url=url, action=action, http_status=status,
                         outcome=outcome, clean_text=clean_text, fetched_at=now())

    def fact(self, domain, f: dict) -> int:
        return self._ins("facts", run_id=self.run_id, domain=domain, key=f["key"],
                         value_json=json.dumps(f["value"]), quote=f["quote"], source_url=f["source_url"],
                         source_type=f["source_type"], fact_date=f.get("date"), created_at=now())

    def dropped(self, domain, d: dict) -> None:
        self._ins("dropped_facts", run_id=self.run_id, domain=domain, key=d.get("key", "*"),
                  value_json=json.dumps(d.get("value")), quote=d.get("quote"), source_url=d.get("source_url"),
                  reason=d["reason"], created_at=now())

    def planner_decision(self, domain, pass_no, decided_by, actions, reasoning):
        self._ins("planner_decisions", run_id=self.run_id, domain=domain, pass_no=pass_no,
                  decided_by=decided_by, actions_json=json.dumps(actions), reasoning=reasoning, created_at=now())

    def gate_results(self, domain, results: dict) -> None:
        for gid, r in results.items():
            self._upsert("gate_results", run_id=self.run_id, domain=domain, gate=gid, status=r["status"],
                         reason=r["reason"])

    def company(self, domain, status, failing_gate=None, reason=None, score=None):
        self._upsert("companies", run_id=self.run_id, domain=domain, status=status, failing_gate=failing_gate,
                     reason=reason, score=score, updated_at=now())

    def llm_call(self, domain, log: dict):
        self._ins("llm_calls", run_id=self.run_id, domain=domain, purpose=log["purpose"], model=log["model"],
                  input_tokens=log.get("input_tokens"), output_tokens=log.get("output_tokens"),
                  ok=int(log["ok"]), created_at=now())

    # --- hunter -----------------------------------------------------------
    def hunter_cached(self, domain) -> dict | None:
        return self._one("SELECT * FROM hunter_cache WHERE domain=?", (domain,))

    def hunter_cache_put(self, domain, status, body: dict):
        self._upsert("hunter_cache", domain=domain, http_status=status, response_json=json.dumps(body),
                     fetched_at=now())

    def hunter_used(self, month: str) -> int:
        row = self._one("SELECT searches FROM hunter_usage WHERE month=?", (month,))
        return row["searches"] if row else 0

    def hunter_company_cached(self, domain) -> dict | None:
        return self._one("SELECT * FROM hunter_company_cache WHERE domain=?", (domain,))

    def hunter_company_put(self, domain, status, body: dict):
        self._upsert("hunter_company_cache", domain=domain, http_status=status, response_json=json.dumps(body),
                     fetched_at=now())

    def hunter_credits(self, month: str) -> float:
        """Hunter bills from one pool: 1 credit per search or email-finder call, 0.5 per email
        verification; company lookups are free (checked against Hunter's account API, 2026-11-17)."""
        return (self.hunter_used(month) + 0.5 * self.hunter_used(f"{month}/verify")
                + 1.0 * self.hunter_used(f"{month}/finder"))      # company lookups are free

    def hunter_verify_cached(self, email) -> dict | None:
        return self._one("SELECT * FROM hunter_verify_cache WHERE email=?", (email.lower(),))

    def hunter_verify_put(self, email, status, body: dict):
        self._upsert("hunter_verify_cache", email=email.lower(), http_status=status, response_json=json.dumps(body),
                     fetched_at=now())

    def hunter_increment(self, month: str) -> None:
        self._x("INSERT INTO hunter_usage (month, searches) VALUES (?,1) "
                "ON CONFLICT (month) DO UPDATE SET searches=hunter_usage.searches+1", (month,))
        self.conn.commit()

    def contact(self, domain, c: dict | None, reason: str):
        c = c or {}
        self._upsert("contacts", run_id=self.run_id, domain=domain, first_name=c.get("first_name"),
                     last_name=c.get("last_name"), email=c.get("email"), position=c.get("position"),
                     matched_title=c.get("matched_title"), reason=reason)

    # --- email ------------------------------------------------------------
    def draft(self, domain, status, d: dict | None, reasoning: str):
        d = d or {}
        return self._ins("drafts", run_id=self.run_id, domain=domain, status=status, subject=d.get("subject"),
                         body=d.get("body"), pain_points_json=json.dumps(d.get("pain_points")),
                         hook_fact_id=d.get("hook_fact_id"), hook_url=d.get("hook_url"),
                         reasoning=reasoning, created_at=now())

    def send(self, domain, recipient, mode, message_id=None):
        self._ins("sends", run_id=self.run_id, domain=domain, recipient=recipient, mode=mode,
                  message_id=message_id, created_at=now())
