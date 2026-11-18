"""SQLite persistence. One connection per pipeline run; schema applied from db/schema.sql."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from ..config import ROOT

SCHEMA_PATH = ROOT / "db" / "schema.sql"
DEFAULT_DB = ROOT / "db" / "outreach.sqlite"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path | str = DEFAULT_DB, run_id: str | None = None):
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA_PATH.read_text())
        self.run_id = run_id or "adhoc"

    def _ins(self, table: str, **cols) -> int:
        keys = ", ".join(cols)
        qs = ", ".join("?" for _ in cols)
        cur = self.conn.execute(f"INSERT INTO {table} ({keys}) VALUES ({qs})", tuple(cols.values()))
        self.conn.commit()
        return cur.lastrowid

    # --- runs -------------------------------------------------------------
    def start_run(self, dry_run: bool) -> None:
        self.conn.execute("INSERT OR REPLACE INTO runs (run_id, started_at, dry_run) VALUES (?,?,?)",
                          (self.run_id, now(), int(dry_run)))
        self.conn.commit()

    def finish_run(self, summary: dict) -> None:
        self.conn.execute("UPDATE runs SET finished_at=?, summary_json=? WHERE run_id=?",
                          (now(), json.dumps(summary), self.run_id))
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
            self.conn.execute("INSERT OR REPLACE INTO gate_results VALUES (?,?,?,?,?)",
                              (self.run_id, domain, gid, r["status"], r["reason"]))
        self.conn.commit()

    def company(self, domain, status, failing_gate=None, reason=None, score=None):
        self.conn.execute("INSERT OR REPLACE INTO companies VALUES (?,?,?,?,?,?,?)",
                          (self.run_id, domain, status, failing_gate, reason, score, now()))
        self.conn.commit()

    def llm_call(self, domain, log: dict):
        self._ins("llm_calls", run_id=self.run_id, domain=domain, purpose=log["purpose"], model=log["model"],
                  input_tokens=log.get("input_tokens"), output_tokens=log.get("output_tokens"),
                  ok=int(log["ok"]), created_at=now())

    # --- hunter -----------------------------------------------------------
    def hunter_cached(self, domain) -> dict | None:
        row = self.conn.execute("SELECT * FROM hunter_cache WHERE domain=?", (domain,)).fetchone()
        return dict(row) if row else None

    def hunter_cache_put(self, domain, status, body: dict):
        self.conn.execute("INSERT OR REPLACE INTO hunter_cache VALUES (?,?,?,?)",
                          (domain, status, json.dumps(body), now()))
        self.conn.commit()

    def hunter_used(self, month: str) -> int:
        row = self.conn.execute("SELECT searches FROM hunter_usage WHERE month=?", (month,)).fetchone()
        return row[0] if row else 0

    def hunter_company_cached(self, domain) -> dict | None:
        row = self.conn.execute("SELECT * FROM hunter_company_cache WHERE domain=?", (domain,)).fetchone()
        return dict(row) if row else None

    def hunter_company_put(self, domain, status, body: dict):
        self.conn.execute("INSERT OR REPLACE INTO hunter_company_cache VALUES (?,?,?,?)",
                          (domain, status, json.dumps(body), now()))
        self.conn.commit()

    def hunter_credits(self, month: str) -> float:
        """Hunter bills from one pool: 1 credit per search or email-finder call, 0.5 per email
        verification; company lookups are free (checked against Hunter's account API, 2026-11-17)."""
        return (self.hunter_used(month) + 0.5 * self.hunter_used(f"{month}/verify")
                + 1.0 * self.hunter_used(f"{month}/finder"))      # company lookups are free

    def hunter_verify_cached(self, email) -> dict | None:
        row = self.conn.execute("SELECT * FROM hunter_verify_cache WHERE email=?", (email.lower(),)).fetchone()
        return dict(row) if row else None

    def hunter_verify_put(self, email, status, body: dict):
        self.conn.execute("INSERT OR REPLACE INTO hunter_verify_cache VALUES (?,?,?,?)",
                          (email.lower(), status, json.dumps(body), now()))
        self.conn.commit()

    def hunter_increment(self, month: str) -> None:
        self.conn.execute("INSERT INTO hunter_usage VALUES (?,1) ON CONFLICT(month) DO UPDATE SET searches=searches+1",
                          (month,))
        self.conn.commit()

    def contact(self, domain, c: dict | None, reason: str):
        c = c or {}
        self.conn.execute("INSERT OR REPLACE INTO contacts VALUES (?,?,?,?,?,?,?,?)",
                          (self.run_id, domain, c.get("first_name"), c.get("last_name"), c.get("email"),
                           c.get("position"), c.get("matched_title"), reason))
        self.conn.commit()

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
