"""Local web app: type the company you represent, find target accounts, qualify them, see the people.

    .venv/bin/python -m src.webapp            # then open http://127.0.0.1:8765

Runs only on this machine (bound to 127.0.0.1): the API keys stay in .env and contact emails never
leave the computer. Research runs one job at a time in a background thread; the page polls for
progress. Drafts are shown in the page; "Send to my inbox" mails one to OWN_EMAIL, nobody else.
"""
from __future__ import annotations

import json
import threading
import traceback
import uuid
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .config import ROOT, env, load_env, load_icp
from .discover import discover
from .draft_email import render_for_review
from .llm import make_llm
from .pipeline import RUNS_DIR, process_domain, write_trail
from .poc_hunter import account_credits, effective_quota
from .research.fetch import Fetcher
from .research.news import Throttle
from .research.store import Store
from .send_gmail import RecipientNotAllowed, send
from .shortlist import load_exclusions, normalise

HOST, PORT = "127.0.0.1", 8765
PAGE = Path(__file__).parent / "web" / "index.html"

JOBS: dict[str, dict] = {}
WORK_LOCK = threading.Lock()                 # one research or discovery job at a time


# ------------------------------------------------------------------ data helpers
def match_seller(icp: dict, name: str) -> dict | None:
    s = icp.get("seller") or {}
    q = (name or "").strip().lower()
    names = {s.get("name", "").lower(), *(a.lower() for a in s.get("aliases", []))}
    return s if q and q in names else None


def _people(t: dict) -> list[dict]:
    """The chosen contact first, then other ranked people Hunter or the news fallback surfaced."""
    c = t.get("contact") or {}
    out, seen = [], set()
    ch = c.get("chosen")
    if ch:
        out.append({"name": f"{ch.get('first_name', '')} {ch.get('last_name') or ''}".strip(),
                    "title": ch.get("position"), "email": ch.get("email"), "role": "primary",
                    "verification": "valid", "why": c.get("why"), "warning": ch.get("name_warning")})
        seen.add((ch.get("email") or "").lower())
    for r in c.get("considered") or []:
        e = (r.get("email") or "").lower()
        if not e or e in seen or r.get("rank") is None:
            continue
        seen.add(e)
        out.append({"name": r.get("name"), "title": r.get("position"), "email": r.get("email"), "role": "other",
                    "verification": r.get("verification"), "why": r.get("rejected") or "ranked title"})
    for tr in (c.get("leader_fallback") or {}).get("tried", []):
        if not any(p["name"] == tr["name"] for p in out):
            out.append({"name": tr["name"], "title": tr["role"], "email": None, "role": "named in news",
                        "verification": tr.get("verification"), "why": f"Email Finder: {tr.get('finder')}"})
    return out


def account_summary(t: dict) -> dict:
    run = t.get("run_id") or ""
    d = (t.get("email") or {}).get("draft")
    return {
        "domain": t["domain"], "name": (t.get("company") or {}).get("name") or t["domain"],
        "status": t.get("final_status"), "reason": t.get("final_reason"),
        "run_date": f"{run[:4]}-{run[4:6]}-{run[6:8]}" if len(run) >= 8 else None,
        "gates": {g: {"status": v.get("status"), "reason": v.get("reason")} for g, v in (t.get("gates") or {}).items()},
        "score": ((t.get("qualification") or {}).get("weighted_score") or {}).get("total"),
        "people": _people(t),
        "draft": {"subject": d["subject"], "body": d["body"], "hook_url": d.get("hook_url"),
                  "hook_quote": d.get("hook_quote")} if d else None,
        "sent": (t.get("send") or {}).get("mode"),
    }


def load_accounts() -> list[dict]:
    out = []
    for f in sorted(RUNS_DIR.glob("*.json")):
        if f.name.startswith("_"):
            continue
        try:
            out.append(account_summary(json.loads(f.read_text())))
        except (json.JSONDecodeError, KeyError):
            continue
    order = {"go": 0, "needs_review": 1, "retry": 2, "no_go": 3}
    out.sort(key=lambda a: (order.get(a["status"], 9), -(a["score"] or 0), a["name"].lower()))
    return out


# ------------------------------------------------------------------ jobs
def _new_job(kind: str) -> dict:
    job = {"id": uuid.uuid4().hex[:10], "kind": kind, "state": "queued", "log": [], "items": [],
           "result": None, "error": None, "started": datetime.now().isoformat(timespec="seconds")}
    JOBS[job["id"]] = job
    return job


def _log(job, msg):
    job["log"].append(msg)
    job["log"] = job["log"][-40:]


def _run_discover(job, icp):
    with WORK_LOCK:
        job["state"] = "running"
        try:
            cfg = icp["agent_loop"]
            excl = load_exclusions(icp["run"]["shortlist_exclusions"])
            researched = {f.stem for f in RUNS_DIR.glob("*.json") if not f.name.startswith("_")}
            job["result"] = discover(Fetcher(cfg["user_agent"], cfg["http_timeout_seconds"]), make_llm(icp["llm"]),
                                     icp["seller"], cfg["user_agent"], excl, researched,
                                     throttle=Throttle(cfg.get("news_min_interval_seconds", 2)),
                                     progress=lambda m: _log(job, m))
            job["state"] = "done"
        except Exception as e:                                  # surface, don't crash the server
            job["state"], job["error"] = "failed", f"{type(e).__name__}: {e}"
            traceback.print_exc()


def _run_research(job, icp, targets: list[tuple[str, str | None]]):
    with WORK_LOCK:
        job["state"] = "running"
        try:
            today = date.today()
            run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
            store = Store(run_id=run_id)
            store.start_run(True)
            cfg = icp["agent_loop"]
            fetcher = Fetcher(cfg["user_agent"], cfg["http_timeout_seconds"])
            llm = make_llm(icp["llm"])
            throttle = Throttle(cfg.get("news_min_interval_seconds", 2))
            remaining, reset = account_credits(env("HUNTER_API_KEY"))
            quota = effective_quota(store, icp["contact"]["hunter_monthly_quota"], remaining, today)
            if remaining is not None:
                _log(job, f"Hunter: {remaining:g} credits left (resets {reset})")
            excl = load_exclusions(icp["run"]["shortlist_exclusions"])
            for dom, name in targets:
                item = {"domain": dom, "name": name or dom, "state": "waiting"}
                job["items"].append(item)
            for item in job["items"]:
                if item["domain"] in excl:
                    item.update(state="skipped", status="excluded", reason=f"In {excl[item['domain']]}")
                    continue
                item["state"] = "researching"
                _log(job, f"Researching {item['name']}")
                try:
                    trail = process_domain(item["domain"], icp, fetcher=fetcher, llm=llm, store=store, dry_run=True,
                                           review=False, today=today, news_throttle=throttle,
                                           company_name=item["name"] if item["name"] != item["domain"] else None,
                                           hunter_quota=quota, print_fn=lambda *_: None)
                except Exception as e:
                    trail = {"domain": item["domain"], "run_id": run_id, "final_status": "needs_review",
                             "final_reason": f"pipeline error: {type(e).__name__}: {e}", "llm_calls": None,
                             "research": {"exit_reason": "error"}}
                trail.pop("send", None)                         # app runs draft only; sending is a separate click
                write_trail(trail)
                item.update(state="done", status=trail["final_status"], reason=trail["final_reason"][:200])
            job["state"] = "done"
        except Exception as e:
            job["state"], job["error"] = "failed", f"{type(e).__name__}: {e}"
            traceback.print_exc()


def send_draft(domain: str) -> dict:
    path = RUNS_DIR / f"{domain}.json"
    t = json.loads(path.read_text())
    d, c = (t.get("email") or {}).get("draft"), (t.get("contact") or {}).get("chosen")
    if not d or not c:
        return {"ok": False, "error": "This account has no drafted email."}
    own = env("OWN_EMAIL")
    gates = "; ".join(f"{g} {v['status']}" for g, v in t["gates"].items())
    body = render_for_review(domain, c, d, t["contact"]["why"], gates)
    subject = f"[Sprinto draft → {c['first_name']} @ {domain}] {d['subject']}"
    try:
        mode, msg_id = send(own or "", subject, body, own, client_secret_file=env("GMAIL_CLIENT_SECRET_FILE"))
    except (RecipientNotAllowed, RuntimeError) as e:
        return {"ok": False, "error": str(e)}
    t["send"] = {"mode": mode, "to": own, "message_id": msg_id}
    path.write_text(json.dumps(t, indent=2, default=str))
    return {"ok": True, "mode": mode}


# ------------------------------------------------------------------ HTTP
class Handler(BaseHTTPRequestHandler):
    icp: dict = {}

    def log_message(self, fmt, *args):                          # quiet; errors still print
        pass

    def _json(self, code, obj):
        body = json.dumps(obj, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return {}

    def _local_only(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
            self._json(403, {"error": "This app only answers on localhost."})
            return False
        return True

    def do_GET(self):
        if not self._local_only():
            return
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            body = PAGE.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif u.path == "/api/seller":
            name = parse_qs(u.query).get("name", [""])[0]
            s = match_seller(self.icp, name)
            if not s:
                return self._json(404, {"error": f"“{name}” isn't set up yet. Only {self.icp['seller']['name']} "
                                                 f"has a researched ideal-customer profile so far."})
            gates = [{"id": g, "name": self.icp["gates"][g]["name"],
                      "scoring_only": bool(self.icp["gates"][g].get("scoring_only"))} for g in self.icp["gate_order"]]
            remaining, reset = account_credits(env("HUNTER_API_KEY"))
            self._json(200, {"name": s["name"], "domain": s["domain"], "pitch": s["pitch"], "gates": gates,
                             "hunter": {"remaining": remaining, "reset": reset}})
        elif u.path == "/api/accounts":
            self._json(200, {"accounts": load_accounts()})
        elif u.path.startswith("/api/jobs/"):
            job = JOBS.get(u.path.rsplit("/", 1)[-1])
            self._json(200, job) if job else self._json(404, {"error": "No such job."})
        else:
            self._json(404, {"error": "Not found."})

    def do_POST(self):
        if not self._local_only():
            return
        u, b = urlparse(self.path), self._body()
        if u.path in ("/api/discover", "/api/research") and WORK_LOCK.locked():
            return self._json(409, {"error": "A search is already running. Wait for it to finish."})
        if u.path == "/api/discover":
            job = _new_job("discover")
            threading.Thread(target=_run_discover, args=(job, self.icp), daemon=True).start()
            self._json(202, {"job": job["id"]})
        elif u.path == "/api/research":
            targets, seen = [], set()
            for t in b.get("targets") or []:
                dom = normalise(t.get("domain") if isinstance(t, dict) else str(t))
                if dom and dom not in seen:
                    seen.add(dom)
                    targets.append((dom, (t.get("name") if isinstance(t, dict) else None) or None))
            if not targets:
                return self._json(400, {"error": "Give at least one valid domain, like acme.com."})
            limit = self.icp["run"]["max_companies_per_run"]
            if len(targets) > limit:
                return self._json(400, {"error": f"At most {limit} companies per run (you gave {len(targets)})."})
            job = _new_job("research")
            threading.Thread(target=_run_research, args=(job, self.icp, targets), daemon=True).start()
            self._json(202, {"job": job["id"]})
        elif u.path == "/api/send":
            dom = normalise(b.get("domain") or "")
            if not dom or not (RUNS_DIR / f"{dom}.json").exists():
                return self._json(404, {"error": "Unknown account."})
            self._json(200, send_draft(dom))
        else:
            self._json(404, {"error": "Not found."})


def main():
    load_env()
    Handler.icp = load_icp()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Prospecting app running at http://{HOST}:{PORT}  (Ctrl+C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
