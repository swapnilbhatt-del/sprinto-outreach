"""Builds the shareable run report from outputs/runs/*.json.

    .venv/bin/python outputs/report/build.py      # writes outputs/report/pipeline-report.html

The page never contains contact email addresses. Publish it with the Artifact tool, or open it locally.
"""
import glob
import html
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
OUT = os.path.join(HERE, "pipeline-report.html")
DRAFT_DOMAINS = ["wealthy.in", "infinite-uptime.com", "fleetx.ai"]

# One plain-English line per company; falls back to the pipeline's own reason.
WHY = {
    "wealthy.in": "Fintech for mutual-fund distributors. Series B Nov 2025, 400+ staff, no SOC 2/ISO claim. Contact: CEO (check name order).",
    "infinite-uptime.com": "Industrial predictive-maintenance platform. Series C Mar 2025, 251–1K staff, no SOC 2/ISO claim. Contact: Managing Director.",
    "fleetx.ai": "Fleet-management SaaS. Series C May 2025, 251–1K staff, no SOC 2/ISO claim. Contact: a founder named in inc42 coverage, email found and verified by Hunter.",
    "intangles.ai": "Passes the fit gates. News names the CEO and COO, but Hunter's Email Finder has no address for either.",
    "vahan.co": "SOC 2 status and headcount unanswered on the latest run.",
    "getpliant.com": "Its own site links an ISO 27001:2022 certificate, so it fails G5 (caught after the 17 Nov fix).",
    "rewaa.com": "Only a 2023 Series A found; the Dec 2025 Series B isn't in Bing News.",
    "logcomex.com": "No dated funding round or headcount found.",
    "mozark.ai": "Homepage is built by JavaScript, so its SOC 2 status can't be read.",
    "omilia.com": "Trust page is JavaScript-only. Manual check: its product site (ocp.ai) claims SOC 2 Type II and ISO 27001, so it would fail G5.",
    "autorabit.com": "Behind a Cloudflare challenge (not bypassed); last round found is 2022.",
    "conductorone.com": "Site redirects off-domain; Hunter's 51–250 range straddles the 100 floor.",
    "kaartech.com": "Domain doesn't resolve; Hunter's 1K–5K range straddles the 1,000 ceiling.",
    "spyne.ai": "Hunter estimates 11–50 staff; a vendor estimate alone can't reject, so it's left for review.",
    "zenskar.com": "No headcount found within 24 months.",
    "zomato.com": "Consumer company; site fetch failed in the first test run.",
    "bureau.id": "Rejected on G4 under the old rule (no compliance signal). Would need a re-run now that G4 only scores.",
    "easebuzz.in": "Claims SOC 2 Type II, so it fails G5.",
    "gnani.ai": "Claims SOC 2 and ISO 27001, so it fails G5.",
    "respond.io": "Announced ISO 27001:2022, so it fails G5.",
    "hasura.io": "Security page says SOC 2 Type II.",
    "hevodata.com": "SOC 2 Type II on its site.",
    "spotdraft.com": "Homepage lists SOC 2 Type II.",
    "petpooja.com": "1,500+ employees, above the 1,000 ceiling.",
    "atlassian.com": "Public company.",
    "posthog.com": "Series E (2025), past Series C.",
    "unifyapps.com": "Latest round reported as Series D+ (2025).",
    "zluri.com": "Acquired in Oct 2026.",
}
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def e(x):
    return html.escape(str(x))


def main():
    rows = []
    for f in sorted(glob.glob(os.path.join(ROOT, "outputs/runs/*.json"))):
        if not os.path.basename(f).startswith("_"):
            rows.append(json.load(open(f)))
    order = {"go": 0, "needs_review": 1, "retry": 2, "no_go": 3}
    rows.sort(key=lambda t: (order.get(t["final_status"], 9), t["company"]["name"].lower()))
    counts = {k: sum(1 for t in rows if t["final_status"] == k) for k in order}
    label = {"go": "Go", "needs_review": "Review", "no_go": "No-go", "retry": "Retry"}
    mark = {"pass": "✓", "fail": "✕", "unknown": "?"}
    trs = []
    for t in rows:
        d, st, g = t["domain"], t["final_status"], t.get("gates", {})
        r = t["run_id"]
        date = f"{r[:4]}-{r[4:6]}-{r[6:8]}"
        chips = "".join(
            f'<span class="g g-{g.get(k, {}).get("status", "unknown")}" title="{k}: {g.get(k, {}).get("status", "unknown")}">'
            f'{k}<b>{mark[g.get(k, {}).get("status", "unknown")]}</b></span>' for k in ["G1", "G5", "G3", "G2", "G4"])
        trs.append(f'<tr><td class="co"><strong>{e(t["company"]["name"])}</strong><span class="dom">{e(d)}</span></td>'
                   f'<td><span class="pill p-{st}">{label[st]}</span></td><td class="gates">{chips}</td>'
                   f'<td class="why">{e(WHY.get(d, t["final_reason"][:140]))}</td>'
                   f'<td class="num">{date}{"<sup>*</sup>" if date < "2026-11-10" else ""}</td></tr>')
    drafts = []
    for dom in DRAFT_DOMAINS:
        t = json.load(open(os.path.join(ROOT, f"outputs/runs/{dom}.json")))
        dr, c = t["email"]["draft"], t["contact"]["chosen"]
        drafts.append(f'<article class="draft"><header><span class="pill p-go">Go</span><h3>{e(t["company"]["name"])}</h3>'
                      f'<span class="meta">to {e(c["position"])} · draft, never sent to the prospect</span></header>'
                      f'<p class="subj"><span>Subject</span>{e(dr["subject"])}</p><p class="body">{e(dr["body"])}</p>'
                      f'<p class="hook"><span>Hook source</span><a href="{e(dr["hook_url"])}" target="_blank" rel="noopener">'
                      f'{e(dr["hook_quote"][:160])}</a></p></article>')
    page = (open(os.path.join(HERE, "template.html")).read()
            .replace("{{ROWS}}", "\n".join(trs)).replace("{{DRAFTS}}", "".join(drafts))
            .replace("{{N}}", str(len(rows))).replace("{{GO}}", str(counts["go"]))
            .replace("{{REV}}", str(counts["needs_review"] + counts["retry"])).replace("{{NOGO}}", str(counts["no_go"])))
    import base64
    for name in ("overview", "agent", "learning"):          # screenshots, embedded so the page is self-contained
        img = os.path.join(HERE, "img", f"{name}.jpg")
        if os.path.exists(img):
            page = page.replace("{{IMG:%s}}" % name, "data:image/jpeg;base64," + base64.b64encode(open(img, "rb").read()).decode())
    leaked = set(EMAIL.findall(page))
    if leaked:
        raise SystemExit(f"refusing to write: page contains email addresses {sorted(leaked)}")
    open(OUT, "w").write(page)
    print(f"wrote {OUT}: {len(rows)} companies, {counts}")


if __name__ == "__main__":
    main()
