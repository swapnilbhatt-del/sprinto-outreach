"""Gate logic (Step 5). Pure functions over validated facts + which actions ran. No I/O, no LLM."""
from __future__ import annotations

from datetime import date

CHECKED = ("ok", "not_found")         # outcomes that let us conclude something about a source


def _months_ago(d: str | None, today: date) -> float | None:
    if not d:
        return None
    y, m, dd = (int(x) for x in d.split("-"))
    return (today - date(y, m, dd)).days / 30.44


def _facts(facts, key):
    return [f for f in facts if f["key"] == key]


def _ids(fs):
    return [f["id"] for f in fs if "id" in f]


def _outcome(done: dict, action: str) -> str | None:
    return (done.get(action) or {}).get("outcome")


def gate_g1(gate, facts, done, today):
    soft = _facts(facts, "is_software_product")
    b2b = _facts(facts, "sells_to_businesses")
    for key, fs in (("is_software_product", soft), ("sells_to_businesses", b2b)):
        nos = [f for f in fs if f["value"]["value"] == "no"]
        if nos and not any(f["value"]["value"] == "yes" for f in fs):
            return "fail", f"{key}=no: \"{nos[0]['quote']}\"", _ids(nos)
    ys = [f for f in soft if f["value"]["value"] == "yes"]
    yb = [f for f in b2b if f["value"]["value"] == "yes"]
    if ys and yb:
        return "pass", f"software: \"{ys[0]['quote']}\"; B2B: \"{yb[0]['quote']}\"", _ids([ys[0], yb[0]])
    return "unknown", f"software={'yes' if ys else 'unknown'}, b2b={'yes' if yb else 'unknown'}", []


EVENT_DATED = ("stated", "published")   # a page's fetch date is not the date of a round or a hire


def gate_g2(gate, facts, done, today):
    fs = [f for f in _facts(facts, "funding_round") if f.get("date") and f.get("date_basis", "stated") in EVENT_DATED]
    if not fs:
        n = len(_facts(facts, "funding_round"))
        return "unknown", f"no dated funding round found ({n} undated)", []
    latest = max(fs, key=lambda f: f["date"])
    rnd, age = latest["value"]["value"], _months_ago(latest["date"], today)
    cite = f"{rnd} on {latest['date']}: \"{latest['quote']}\""
    if rnd in gate["fail_rounds"]:
        return "fail", f"latest round is {cite}", _ids([latest])
    if rnd in ("seed", "pre_seed"):
        if age is not None and age <= gate["seed_fail_within_months"]:
            return "fail", f"latest round is {cite} (seed-stage)", _ids([latest])
        return "unknown", f"latest found round is an old seed round ({cite})", _ids([latest])
    if rnd in gate["pass_rounds"]:
        if age is not None and age <= gate["freshness_months"]:
            return "pass", cite, _ids([latest])
        return "unknown", f"{cite} is older than {gate['freshness_months']} months", _ids([latest])
    return "unknown", f"unrecognised round {rnd}", []


def gate_g3(gate, facts, done, today):
    fs = [f for f in _facts(facts, "employee_count") if f.get("date")
          and _months_ago(f["date"], today) <= gate["freshness_months"]]
    if not fs:
        return "unknown", "no headcount within freshness window", []
    latest = max(fs, key=lambda f: f["date"])
    lo, hi = latest["value"]["min"], latest["value"]["max"]
    cite = f"{lo}-{hi} ({latest['date']}): \"{latest['quote']}\""
    if latest.get("source_type") == "vendor" and (hi < gate["min"] or lo > gate["max"]):
        # A vendor's size estimate may pass G3 but never rejects a company on its own.
        side = f"below {gate['min']}" if hi < gate["min"] else f"above {gate['max']}"
        return "unknown", f"only a vendor estimate says {side}; review: {cite}", _ids([latest])
    if hi < gate["min"]:
        return "fail", f"below {gate['min']}: {cite}", _ids([latest])
    if lo > gate["max"]:
        return "fail", f"above {gate['max']}: {cite}", _ids([latest])
    if lo >= gate["min"] and hi <= gate["max"]:
        return "pass", cite, _ids([latest])
    return "unknown", f"range straddles a boundary: {cite}", _ids([latest])


def gate_g4(gate, facts, done, today):
    jobs = _facts(facts, "compliance_job_titles")
    if jobs:
        return "pass", f"(a) open role \"{jobs[0]['value']['value']}\" at {jobs[0]['source_url']}", _ids(jobs[:3])
    trust = [f for f in _facts(facts, "soc2_iso_status")
             if f["value"]["value"] in gate["any_of"]["b_trust_page"]["qualifying_values"]]
    if trust:
        return "pass", f"(b) \"{trust[0]['quote']}\" at {trust[0]['source_url']}", _ids(trust[:1])
    window = gate["any_of"]["c_leadership_change"]["window_months"]
    lead = [f for f in _facts(facts, "security_leader_change")
            if f.get("date") and f.get("date_basis", "stated") in EVENT_DATED and _months_ago(f["date"], today) <= window]
    if lead:
        return "pass", f"(c) {lead[0]['value']['role']} change on {lead[0]['date']}: \"{lead[0]['quote']}\"", _ids(lead[:1])
    checked = {
        "job boards": _outcome(done, "job_board_lookup") is not None,
        "careers page": _outcome(done, "scrape_careers") in CHECKED,
        "trust page": _outcome(done, "scrape_trust") in CHECKED,
        "security page": _outcome(done, "scrape_security") in CHECKED,
        "CISO news": _outcome(done, "news:g4_ciso") == "ok",
        "head-of news": _outcome(done, "news:g4_head") == "ok",
    }
    if all(checked.values()):
        return "fail", "all of (a) job boards + careers, (b) trust + security, (c) news checked; none met", []
    missing = [k for k, v in checked.items() if not v]
    return "unknown", f"no evidence yet; not checked: {', '.join(missing)}", []


def gate_g5(gate, facts, done, today):
    att = [f for f in _facts(facts, "soc2_iso_status") if f["value"]["value"] in gate["fail_values"]]
    if att:
        return "fail", f"attestation claimed: \"{att[0]['quote']}\" at {att[0]['source_url']}", _ids(att[:1])
    home = _outcome(done, "scrape_homepage") == "ok"
    t, s = _outcome(done, "scrape_trust"), _outcome(done, "scrape_security")
    trust_checked = t == "ok" or s == "ok" or (t == "not_found" and s == "not_found")
    news = all(_outcome(done, f"news:{qid}") == "ok" for qid in gate.get("news_queries", {}))
    if home and trust_checked and news:
        return "pass", "homepage, trust/security and news checked; no SOC 2 Type II / ISO 27001 claim", []
    missing = [n for n, ok in (("homepage", home), ("trust/security", trust_checked), ("news", news)) if not ok]
    return "unknown", f"no attestation found yet; not checked: {', '.join(missing)}", []


GATE_FNS = {"G1": gate_g1, "G2": gate_g2, "G3": gate_g3, "G4": gate_g4, "G5": gate_g5}


def evaluate_gates(icp: dict, facts: list[dict], done: dict, today: date | None = None) -> dict:
    today = today or date.today()
    out = {}
    for gid in icp["gate_order"]:
        status, reason, ev = GATE_FNS[gid](icp["gates"][gid], facts, done, today)
        out[gid] = {"status": status, "reason": reason, "evidence_fact_ids": ev}
    return out


def deciding_gates(icp: dict) -> list[str]:
    """Gates that can reject a company. A gate marked `scoring_only` only adds points (see weighted_score)."""
    return [g for g in icp["gate_order"] if not icp["gates"][g].get("scoring_only")]


def first_failure(icp: dict, results: dict) -> str | None:
    return next((g for g in deciding_gates(icp) if results[g]["status"] == "fail"), None)


def decide(icp: dict, results: dict) -> tuple[str, str | None, str]:
    """(status, failing_gate, reason). Any fail -> no_go; else any unknown -> needs_review; else go."""
    failed = first_failure(icp, results)
    if failed:
        return "no_go", failed, f"{failed} failed: {results[failed]['reason']}"
    unknown = [g for g in deciding_gates(icp) if results[g]["status"] == "unknown"]
    if unknown:
        return "needs_review", None, "unanswered after loop: " + "; ".join(f"{g} ({results[g]['reason']})" for g in unknown)
    return "go", None, "all gates passed"


def weighted_score(icp: dict, facts: list[dict], today: date | None = None,
                   gates: dict | None = None) -> tuple[int, dict]:
    today = today or date.today()
    total, detail = 0, {}
    for gid in icp["gate_order"]:                     # scoring-only gates: points when they pass
        g = icp["gates"][gid]
        if g.get("scoring_only"):
            r = (gates or {}).get(gid) or {}
            pts = g["weight"] if r.get("status") == "pass" else 0
            total += pts
            detail[gid] = {"name": g["name"], "points": pts, "fact_ids": r.get("evidence_fact_ids", []),
                           "status": r.get("status", "unknown")}
    for wid, w in icp["weighted"].items():
        fs = _facts(facts, w["fact_key"])
        hit = None
        if wid == "W3":
            hit = next((f for f in fs if f.get("date") and _months_ago(f["date"], today) <= w["within_months"]
                        and f["value"]["value"].startswith("series")), None)
        elif "value" in w:
            hit = next((f for f in fs if f["value"]["value"] == w["value"]), None)
        else:
            hit = next((f for f in fs if f["value"]["value"] == "yes"), None)
        pts = w["weight"] if hit else 0
        total += pts
        detail[wid] = {"name": w["name"], "points": pts, "fact_id": hit.get("id") if hit else None}
    return total, detail
