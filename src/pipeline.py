"""Orchestrates steps 2-8 per domain and writes outputs/runs/<domain>.json plus a run summary.

    python -m src.pipeline --domains data/sample_domains.txt --dry-run
    python -m src.pipeline --domains data/sample_domains.txt --review
"""
from __future__ import annotations

import argparse
import json
from collections import Counter as Tally
from datetime import date, datetime, timezone
from pathlib import Path

from .agent_loop import AgentLoop
from .config import ROOT, env, load_env, load_icp
from .draft_email import draft, render_for_review
from .llm import CallBudget, make_llm
from .leaders import find_leaders, split_name
from .poc_hunter import (account_credits, choose_contact, company_headcount, domain_search, effective_quota,
                         find_email, name_order_warning, verify_email, verify_unverified_candidates)
from .qualify import decide, weighted_score
from .research.fetch import Fetcher
from .research.news import Throttle
from .appdata import AppData
from .research.store import Store
from .send_gmail import RecipientNotAllowed, send
from .shortlist import normalise, shortlist

RUNS_DIR = ROOT / "outputs" / "runs"
OUTAGE = "every model unavailable"       # GeminiJSON's error when every model was overloaded or out of quota


def llm_outages(actions: list[dict]) -> list[str]:
    """Actions whose source couldn't be read because the LLM was down, not because facts were missing."""
    return [a["action"] for a in actions if a.get("outcome") == "extract_failed" and OUTAGE in (a.get("note") or "")]


def process_domain(domain: str, icp: dict, *, fetcher, llm, store: Store, dry_run: bool, review: bool,
                   today: date, hunter_http=None, gmail_service=None, print_fn=print, news_throttle=None,
                   company_name: str | None = None, hunter_quota: float | None = None,
                   lessons: list[dict] | None = None) -> dict:
    loop_cfg = icp["agent_loop"]
    quota = hunter_quota if hunter_quota is not None else icp["contact"]["hunter_monthly_quota"]
    budget = CallBudget(loop_cfg["max_llm_calls_per_company"])
    llm.on_call = lambda log: store.llm_call(domain, log)
    trail: dict = {"domain": domain, "run_id": store.run_id, "generated_at": datetime.now(timezone.utc).isoformat()}

    # Steps 3-4: research cascade inside the capped agent loop
    loop = AgentLoop(icp, domain, fetcher, llm, store=store, llm_budget=budget, today=today,
                     news_throttle=news_throttle, company_name=company_name,
                     company_lookup=lambda d: company_headcount(store, d, env("HUNTER_API_KEY"), quota,
                                                                http=hunter_http, today=today))
    res = loop.run()
    trail["company"] = res.company
    trail["research"] = {"exit_reason": res.exit_reason, "usage": res.usage, "actions": res.actions,
                         "gate_history": res.gate_history}
    trail["planner_decisions"] = res.planner
    trail["facts"] = res.facts
    trail["dropped_facts"] = res.dropped

    # Step 5: go / no-go
    status, failing, reason = decide(icp, res.gates)
    trail["gates"] = res.gates
    store.gate_results(domain, res.gates)
    trail["qualification"] = {"status": status, "failing_gate": failing, "reason": reason}
    score = None
    if status == "go":
        score, detail = weighted_score(icp, res.facts, today, gates=res.gates)
        trail["qualification"]["weighted_score"] = {"total": score, "factors": detail}

    contact = None
    if status == "go":
        # Step 6: point of contact
        c = icp["contact"]
        key = env("HUNTER_API_KEY")
        hunter, how = domain_search(store, domain, key, quota,
                                    http=hunter_http, today=today, filters=c.get("hunter_filters"),
                                    usable=lambda b: choose_contact(b, c["title_ranking"], c["title_aliases"],
                                                                    c["accepted_verification_status"])[0] is not None)
        trail["contact"] = {"hunter_lookup": how}
        why, considered = f"Hunter lookup {how}", []
        if hunter is not None:
            contact, why, considered = choose_contact(hunter, c["title_ranking"], c["title_aliases"],
                                                      c["accepted_verification_status"])
            if contact is None and c.get("verify_unverified_max"):
                # Ranked titles whose email Hunter hasn't verified: check the best ones, then choose again.
                hunter, vlog = verify_unverified_candidates(
                    hunter, c["title_ranking"], c["title_aliases"],
                    lambda e: verify_email(store, e, key, quota, http=hunter_http, today=today),
                    c["verify_unverified_max"])
                trail["contact"]["email_verifier"] = vlog
                if vlog:
                    contact, why, considered = choose_contact(hunter, c["title_ranking"], c["title_aliases"],
                                                              c["accepted_verification_status"])
        if contact is None and c.get("leader_finder_max") and how not in ("no_key", "quota_exhausted"):
            # Fallback: leaders named in free news coverage, then Hunter's Email Finder for that exact person.
            leaders, llog = find_leaders(fetcher, llm, budget, res.company["name"], domain, news_throttle,
                                         c["title_ranking"], c["title_aliases"])
            tried = []
            for p in leaders[:c["leader_finder_max"]]:
                first, last = split_name(p["name"])
                found, fhow = find_email(store, domain, first, last, key, quota, http=hunter_http, today=today)
                status_v = ((found or {}).get("verification") or {}).get("status")
                if found and status_v in (None, "", "unknown"):
                    status_v, _ = verify_email(store, found["email"], key, quota, http=hunter_http, today=today)
                tried.append({"name": p["name"], "role": p["role"], "finder": fhow, "verification": status_v})
                if found and status_v in c["accepted_verification_status"]:
                    contact = {"first_name": first, "last_name": last, "email": found["email"],
                               "position": p["role"], "matched_title": p["matched_title"]}
                    warning = name_order_warning(first, last, found["email"])
                    if warning:
                        contact["name_warning"] = warning
                    why = (f"Hunter's domain search had no usable contact, so the leader was taken from the news: "
                           f"\"{p['quote']}\" ({p['source_url']}). Matched \"{p['matched_title']}\" on the title list. "
                           f"Hunter Email Finder returned their address (score {found.get('score')}), verification \"{status_v}\".")
                    break
            trail["contact"]["leader_fallback"] = {**llog, "tried": tried}
        trail["contact"].update({"chosen": contact, "why": why, "considered": considered})
        store.contact(domain, contact, why)
        if contact is None:
            status, reason = "needs_review", f"no contact: {why}"

    if status == "go" and contact:
        # Step 7: personalised draft
        dstatus, d, dreason = draft(llm, budget, icp, f"{res.company['name']} ({domain})", contact, res.facts,
                                    trail["contact"]["why"], lessons=lessons)
        store.draft(domain, dstatus, d, dreason)
        trail["email"] = {"status": dstatus, "reason": dreason, "draft": d}
        if dstatus != "drafted":
            status, reason = "needs_review", f"email: {dreason}"
        else:
            # Step 8: send to OWN_EMAIL only
            own = env("OWN_EMAIL")
            gate_summary = "; ".join(f"{g} {v['status']}" for g, v in res.gates.items())
            body = render_for_review(domain, contact, d, trail["contact"]["why"], gate_summary)
            subject = f"[Sprinto draft → {contact['first_name']} @ {domain}] {d['subject']}"
            try:
                mode, msg_id = send(own or "", subject, body, own, dry_run=dry_run, review=review,
                                    client_secret_file=env("GMAIL_CLIENT_SECRET_FILE"), service=gmail_service,
                                    print_fn=print_fn)
                store.send(domain, own, mode, msg_id)
                trail["send"] = {"mode": mode, "to": own, "message_id": msg_id}
            except (RecipientNotAllowed, RuntimeError) as e:
                trail["send"] = {"mode": "failed", "error": str(e)}
                print_fn(f"[{domain}] send failed: {e}")

    # needs_review means "a person should look"; if the LLM was down, re-running is the fix instead.
    # A no_go or go stands: its deciding evidence was read.
    outages = llm_outages(res.actions)
    if status == "needs_review" and (outages or OUTAGE in reason):
        status = "retry"
        reason = (f"LLM unavailable during research ({len(outages)} source(s) unread: {', '.join(outages[:4])}"
                  f"{'…' if len(outages) > 4 else ''}); re-run this company later. Was: {reason}")
    trail["final_status"] = status
    trail["final_reason"] = reason
    trail["llm_calls"] = budget.used
    store.company(domain, status, failing, reason, score)
    return trail


def write_trail(trail: dict, runs_dir: Path = RUNS_DIR) -> Path:
    runs_dir.mkdir(parents=True, exist_ok=True)
    path = runs_dir / f"{trail['domain']}.json"
    path.write_text(json.dumps(trail, indent=2, default=str))
    return path


def summarise(run_id, trails, removed, store: Store, llm_total: int, today: date) -> dict:
    counts = Tally(t["final_status"] for t in trails)
    month = today.strftime("%Y-%m")
    return {
        "run_id": run_id,
        "researched": len(trails),
        "go": counts.get("go", 0), "no_go": counts.get("no_go", 0), "needs_review": counts.get("needs_review", 0),
        "retry": counts.get("retry", 0),
        "excluded": removed,
        "hunter_searches_used_this_month": store.hunter_used(month),
        "hunter_company_lookups_this_month": store.hunter_used(f"{month}/enrich"),
        "hunter_email_checks_this_month": store.hunter_used(f"{month}/verify"),
        "hunter_credits_used_this_month": round(store.hunter_credits(month), 1),
        "hunter_calls_this_run": sum(1 for t in trails if t.get("contact", {}).get("hunter_lookup") == "api"),
        "llm_calls_total": llm_total,
        "emails": Tally(t.get("send", {}).get("mode", "none") for t in trails),
        "per_company": [{"domain": t["domain"], "status": t["final_status"], "reason": t["final_reason"],
                         "llm_calls": t["llm_calls"], "exit": t["research"]["exit_reason"]} for t in trails],
    }


def print_summary(s: dict, print_fn=print) -> None:
    print_fn("\n=== Run summary " + s["run_id"] + " ===")
    print_fn(f"go: {s['go']}   no_go: {s['no_go']}   needs_review: {s['needs_review']}   retry: {s.get('retry', 0)}   "
             f"(researched {s['researched']}, excluded {len(s['excluded'])})")
    print_fn(f"Hunter searches used this month: {s['hunter_searches_used_this_month']} "
             f"(this run: {s['hunter_calls_this_run']})   LLM calls: {s['llm_calls_total']}")
    print_fn(f"Emails: {dict(s['emails'])}")
    for e in s["excluded"]:
        print_fn(f"  excluded  {e['domain']:<22} {e['reason']}")
    for c in s["per_company"]:
        print_fn(f"  {c['status']:<12} {c['domain']:<22} {c['reason'][:150]}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Sprinto agentic outreach pipeline")
    ap.add_argument("--domains", default=str(ROOT / "data" / "sample_domains.txt"))
    ap.add_argument("--dry-run", action="store_true", help="print emails instead of sending")
    ap.add_argument("--review", action="store_true", help="confirm each send in the terminal")
    ap.add_argument("--limit", type=int, help="override run.max_companies_per_run")
    args = ap.parse_args(argv)

    load_env()
    icp = load_icp()
    if args.limit:
        icp["run"]["max_companies_per_run"] = args.limit
    today = date.today()
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    store = AppData(run_id=run_id)
    store.start_run(args.dry_run)

    names = {}                     # optional "domain, Company Name" per line
    raw = []
    for line in Path(args.domains).read_text().splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue
        dom, _, name = line.partition(",")
        raw.append(dom)
        if name.strip() and normalise(dom):
            names[normalise(dom)] = name.strip()
    kept, removed = shortlist(raw, icp)
    for r in removed:
        store.company(r["domain"], "excluded", reason=r["reason"])

    cfg = icp["agent_loop"]
    fetcher = Fetcher(cfg["user_agent"], cfg["http_timeout_seconds"])
    llm = make_llm(icp["llm"])
    throttle = Throttle(cfg.get("news_min_interval_seconds", 2))    # one per run: polite gap between news calls
    remaining, reset = account_credits(env("HUNTER_API_KEY"))
    quota = effective_quota(store, icp["contact"]["hunter_monthly_quota"], remaining, today)
    if remaining is not None:
        print(f"Hunter: {remaining:g} credits left on the account (resets {reset}); this run may use up to "
              f"{max(0.0, quota - store.hunter_credits(today.strftime('%Y-%m'))):g}.")
    trails = []
    for d in kept:
        print(f"→ {d}")
        try:
            trail = process_domain(d, icp, fetcher=fetcher, llm=llm, store=store, dry_run=args.dry_run,
                                   review=args.review, today=today, news_throttle=throttle,
                                   company_name=names.get(d), hunter_quota=quota)
        except Exception as e:             # one company's failure must not stop the run
            reason = f"pipeline error: {type(e).__name__}: {e}"
            store.company(d, "needs_review", reason=reason)
            trail = {"domain": d, "run_id": run_id, "final_status": "needs_review", "final_reason": reason,
                     "llm_calls": None, "research": {"exit_reason": "error"}}
        write_trail(trail)
        store.save_trail(trail)
        print(f"  {trail['final_status']}: {trail['final_reason'][:160]}")
        trails.append(trail)

    summary = summarise(run_id, trails, removed, store, llm.total_calls, today)
    store.finish_run(summary)
    (RUNS_DIR / f"_summary_{run_id}.json").write_text(json.dumps(summary, indent=2, default=str))
    print_summary(summary)
    retry = [t["domain"] for t in trails if t["final_status"] == "retry"]
    if retry:
        path = RUNS_DIR / f"_retry_{run_id}.txt"
        path.write_text("".join(f"{d}, {names[d]}\n" if d in names else f"{d}\n" for d in retry))
        print(f"\n{len(retry)} company(ies) hit an LLM outage. Re-run them later with:\n"
              f"  .venv/bin/python -m src.pipeline --domains {path.relative_to(ROOT)} --review")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
