"""App-level data on top of Store: decision trails, the reply agent's state and event log, and the
feedback loop's ratings and lessons. Postgres (Supabase) when DATABASE_URL is set, else SQLite."""
from __future__ import annotations

import json

from .research.store import Store, now


def _load(v):
    if v is None or isinstance(v, (dict, list)):
        return v
    return json.loads(v)


class AppData(Store):
    def _j(self) -> str:
        return "?::jsonb" if self.pg else "?"

    def _jupsert(self, table: str, key_col: str, key, json_col: str, value, **extra):
        cols = [key_col, json_col, *extra, "updated_at"]
        vals = [key, json.dumps(value, default=str), *extra.values(), now()]
        ph = ["?", self._j(), *("?" for _ in extra), "?"]
        sets = ", ".join(f"{c}=excluded.{c}" for c in cols if c != key_col)
        self._x(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(ph)}) "
                f"ON CONFLICT ({key_col}) DO UPDATE SET {sets}", tuple(vals))
        self.conn.commit()

    # --- decision trails --------------------------------------------------------------
    def save_trail(self, trail: dict) -> None:
        self._jupsert("trails", "domain", trail["domain"], "trail", trail)

    def get_trail(self, domain: str) -> dict | None:
        row = self._one("SELECT trail FROM trails WHERE domain=?", (domain,))
        return _load(row["trail"]) if row else None

    def list_trails(self, slim: bool = False) -> list[dict]:
        """All decision trails. slim=True leaves out the bulky research history (facts, actions, planner
        notes), which list screens don't need; on Postgres that's dropped in the query itself."""
        if slim and self.pg:
            q = "SELECT trail - 'facts' - 'dropped_facts' - 'research' - 'planner_decisions' AS trail FROM trails"
        else:
            q = "SELECT trail FROM trails"
        return [_load(r["trail"]) for r in self._x(q).fetchall()]

    # --- reply agent ------------------------------------------------------------------
    def get_thread(self, domain: str) -> dict | None:
        row = self._one("SELECT state, status FROM agent_threads WHERE domain=?", (domain,))
        return {"state": _load(row["state"]), "status": row["status"]} if row else None

    def put_thread(self, domain: str, state: dict, status: str = "active") -> None:
        self._jupsert("agent_threads", "domain", domain, "state", state, status=status)

    def list_threads(self, status: str | None = None) -> list[dict]:
        q, p = "SELECT domain, state, status, updated_at FROM agent_threads", ()
        if status:
            q, p = q + " WHERE status=?", (status,)
        return [{"domain": r["domain"], "state": _load(r["state"]), "status": r["status"],
                 "updated_at": str(r["updated_at"])} for r in self._x(q, p).fetchall()]

    def event(self, domain: str, node: str, outcome: str, tool: str | None = None, detail: str | None = None):
        self._ins("agent_events", domain=domain, node=node, tool=tool, outcome=outcome,
                  detail=(detail or "")[:2000], created_at=now())

    def events(self, domain: str, limit: int = 50) -> list[dict]:
        rows = self._x("SELECT node, tool, outcome, detail, created_at FROM agent_events WHERE domain=? "
                       "ORDER BY id DESC LIMIT ?", (domain, limit)).fetchall()
        return [dict(r) | {"created_at": str(r["created_at"])} for r in rows]

    # --- feedback loop ----------------------------------------------------------------
    def add_feedback(self, domain: str, rating: int | None, edited_body: str | None, notes: str | None,
                     reply_status: str | None, lessons_used: list, draft_version: int = 1) -> int:
        cols = dict(domain=domain, draft_version=draft_version, rating=rating, edited_body=edited_body,
                    notes=notes, reply_status=reply_status, created_at=now())
        keys = list(cols) + ["lessons_used"]
        ph = ["?"] * len(cols) + [self._j()]
        sql = f"INSERT INTO feedback ({', '.join(keys)}) VALUES ({', '.join(ph)})"
        vals = (*cols.values(), json.dumps(lessons_used))
        if self.pg:
            fid = self._x(sql + " RETURNING id", vals).fetchone()["id"]
        else:
            fid = self._x(sql, vals).lastrowid
        self.conn.commit()
        return fid

    def feedback_rows(self, limit: int = 200) -> list[dict]:
        rows = self._x("SELECT * FROM feedback ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) | {"lessons_used": _load(r["lessons_used"]), "created_at": str(r["created_at"])} for r in rows]

    def add_lesson(self, text: str, rationale: str, evidence: list) -> int:
        sql = f"INSERT INTO lessons (text, rationale, evidence, status, created_at) VALUES (?, ?, {self._j()}, 'pending', ?)"
        vals = (text, rationale, json.dumps(evidence), now())
        if self.pg:
            lid = self._x(sql + " RETURNING id", vals).fetchone()["id"]
        else:
            lid = self._x(sql, vals).lastrowid
        self.conn.commit()
        return lid

    def lessons(self, status: str | None = None) -> list[dict]:
        q, p = "SELECT * FROM lessons", ()
        if status:
            q, p = q + " WHERE status=?", (status,)
        rows = self._x(q + " ORDER BY id", p).fetchall()
        return [dict(r) | {"evidence": _load(r["evidence"]), "created_at": str(r["created_at"]),
                           "decided_at": str(r["decided_at"]) if r["decided_at"] else None} for r in rows]

    def decide_lesson(self, lesson_id: int, status: str) -> None:
        if status not in ("approved", "rejected", "pending"):
            raise ValueError("status must be approved, rejected or pending")
        self._x("UPDATE lessons SET status=?, decided_at=? WHERE id=?", (status, now(), lesson_id))
        self.conn.commit()
