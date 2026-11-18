"""Step 4: the research agent loop.

Pass 1 runs every free source (news, job boards, Wikidata, press page) deterministically.
Passes 2..MAX_PASSES ask a planner LLM to pick from a whitelist of actions; code builds every URL.
Hard caps on passes, pages, free API calls and LLM calls are checked before each spend.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from urllib.parse import urlparse

from .config import all_actions, news_queries
from .llm import BudgetExceeded, CallBudget, JsonLLM, LLMError
from .qualify import evaluate_gates, first_failure
from .research.clean import attestation_links, clean_html, extract_links
from .research.extract import extract_facts
from .research.fetch import FetchResult
from .research.news import (Throttle, job_board_lookup, news_digest, news_search, resolve_company_name,
                            wikidata_employees)
from .research.validate import Source, SourceItem, validate

PLANNER_SYSTEM = """You plan the next research step for qualifying ONE company against an ICP.
You may only choose action ids from the `allowed_actions` list you are given. You never write URLs or search queries.
Pick the fewest actions most likely to settle the unanswered gates, preferring actions that serve several gates.
If nothing on the list is likely to help, return an empty list and set stop=true.
Explain your reasoning in one or two sentences."""


class CapError(ValueError):
    pass


@dataclass
class Counter:
    limit: int
    used: int = 0

    def take(self) -> bool:
        if self.used >= self.limit:
            return False
        self.used += 1
        return True

    @property
    def remaining(self) -> int:
        return self.limit - self.used


@dataclass
class LoopResult:
    domain: str
    facts: list[dict]
    gates: dict
    exit_reason: str
    passes: int
    actions: list[dict] = field(default_factory=list)
    planner: list[dict] = field(default_factory=list)
    dropped: list[dict] = field(default_factory=list)
    gate_history: list[dict] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    company: dict = field(default_factory=dict)


class AgentLoop:
    def __init__(self, icp: dict, domain: str, fetcher, llm: JsonLLM, store=None,
                 llm_budget: CallBudget | None = None, today: date | None = None, news_throttle=None,
                 company_name: str | None = None, company_lookup=None):
        caps = icp.get("agent_loop") or {}
        for k in ("max_passes", "max_pages_per_company", "max_llm_calls_per_company",
                  "max_free_api_calls_per_company"):
            if not isinstance(caps.get(k), int) or caps[k] <= 0:
                raise CapError(f"refusing to run without agent_loop.{k}")
        self.icp, self.cfg, self.domain = icp, caps, domain
        self.fetcher, self.llm, self.store = fetcher, llm, store
        self.company_lookup = company_lookup     # domain -> (facts, outcome); the paid vendor tier
        self.today = today or date.today()
        self.pages = Counter(caps["max_pages_per_company"])
        self.api = Counter(caps["max_free_api_calls_per_company"])
        self.llm_budget = llm_budget or CallBudget(caps["max_llm_calls_per_company"])
        self.reserve = caps.get("llm_calls_reserved_for_draft", 0)
        self.actions = all_actions(icp)
        self.queries = news_queries(icp)
        self.facts: list[dict] = []
        self.done: dict[str, dict] = {}
        self.gate_passes = {g: 0 for g in icp["gate_order"]}
        self.homepage_links: list[tuple[str, str]] = []
        self.homepage_read = False     # once True, a page the homepage doesn't link to gets one guess, not two
        self.news_throttle = news_throttle or Throttle(caps.get("news_min_interval_seconds", 2))
        self.result = LoopResult(domain, self.facts, {}, "", 0)
        self._next_id = 1
        self.name_override = company_name
        self.company, self.company_name_source = None, None

    # ------------------------------------------------------------------ run
    def run(self) -> LoopResult:
        r = self.result
        self.company, self.company_name_source = resolve_company_name(
            self.fetcher, self.domain, self.name_override, self.api.take)
        r.company = {"name": self.company, "source": self.company_name_source}
        gates = evaluate_gates(self.icp, self.facts, self.done, self.today)
        for pass_no in range(1, self.cfg["max_passes"] + 1):
            if pass_no == 1:
                chosen = self._seed_actions(gates)
                decided_by, reasoning = "seed", "Pass 1 runs every free source for every gate before any scraping."
            else:
                allowed = self._allowed_actions(gates)
                if not allowed:
                    r.exit_reason = "no_allowed_actions"
                    break
                chosen, decided_by, reasoning = self._plan(pass_no, gates, allowed)
                if not chosen:
                    r.exit_reason = "planner_stop"
                    break
                chosen = self._vendor_first(self._homepage_first(chosen), allowed)
            self._log_decision(pass_no, decided_by, chosen, reasoning)
            self._execute(pass_no, chosen)
            r.passes = pass_no
            gates = evaluate_gates(self.icp, self.facts, self.done, self.today)
            r.gate_history.append({"pass": pass_no, "gates": {g: v["status"] for g, v in gates.items()}})
            failed = first_failure(self.icp, gates)
            if failed:
                r.exit_reason = f"short_circuit:{failed}"
                break
            if all(v["status"] != "unknown" for v in gates.values()):
                r.exit_reason = "all_gates_answered"
                break
            if self.llm_budget.remaining <= self.reserve:
                r.exit_reason = "llm_cap"
                break
        else:
            r.exit_reason = "max_passes"
        r.gates = gates
        r.usage = {"passes": r.passes, "pages": self.pages.used, "free_api_calls": self.api.used,
                   "llm_calls": self.llm_budget.used,
                   "caps": {k: self.cfg[k] for k in ("max_passes", "max_pages_per_company",
                                                     "max_free_api_calls_per_company", "max_llm_calls_per_company")}}
        return r

    # ------------------------------------------------------- action choice
    def _unanswered(self, gates) -> list[str]:
        return [g for g in self.icp["gate_order"] if gates[g]["status"] == "unknown"
                and self.gate_passes[g] < self.icp["gates"][g]["max_passes"]]

    def _free_exhausted(self, gid: str) -> bool:
        return all(a in self.done for a in self.icp["gates"][gid]["sources"].get("free", []))

    def _vendor_exhausted(self, gid: str) -> bool:
        return all(a in self.done for a in self.icp["gates"][gid]["sources"].get("vendor", []))

    def _affordable(self, aid: str) -> bool:
        if self.actions[aid]["tier"] == "vendor":
            return True                    # no pages or LLM calls; the credit quota is checked inside
        spare_llm = self.llm_budget.remaining - self.reserve
        if aid.startswith("news:") or aid in ("job_board_lookup", "wikidata_lookup"):
            return self.api.remaining > 0 and (spare_llm > 0 or not aid.startswith("news:"))
        return self.pages.remaining > 0 and spare_llm > 0

    def _vendor_first(self, chosen: list[dict], allowed: list[dict]) -> list[dict]:
        """Free -> vendor -> scrape: any allowed vendor action runs before the planner's choices.
        It costs no pages or LLM calls, and it unlocks scraping for its gate."""
        picked = {c["action"] for c in chosen}
        extra = [{"action": a["action"], "gate": a["serves"][0],
                  "why": "added by code: vendor source runs after free sources, before scraping"}
                 for a in allowed if a["tier"] == "vendor" and a["action"] not in picked]
        return extra + chosen

    def _homepage_first(self, chosen: list[dict]) -> list[dict]:
        """Before any other scrape, read the homepage: its links tell _candidates which page URLs
        exist, so missing pages cost one guess instead of two. Free sources are already done here."""
        scrapes = [c for c in chosen if c["action"].startswith("scrape_")]
        if not scrapes or "scrape_homepage" in self.done:
            return chosen
        home = [c for c in chosen if c["action"] == "scrape_homepage"]
        rest = [c for c in chosen if c["action"] != "scrape_homepage"]
        if not home:
            if not self._affordable("scrape_homepage"):
                return chosen
            home = [{"action": "scrape_homepage", "gate": "G5",
                     "why": "added by code: read the homepage first so its links pick the page URLs"}]
        return home + rest

    def _seed_actions(self, gates) -> list[dict]:
        unanswered = self._unanswered(gates)
        out = []
        for aid, a in self.actions.items():
            serves = [g for g in a["serves"] if g in unanswered]
            if a["tier"] == "free" and serves:
                out.append({"action": aid, "gate": serves[0], "why": "free source"})
        return out

    def _allowed_actions(self, gates) -> list[dict]:
        """Whitelist filtered by: not yet run, serves an unanswered gate, free-first rule, caps."""
        unanswered = self._unanswered(gates)
        out = []
        for aid, a in self.actions.items():
            if aid in self.done or not self._affordable(aid):
                continue
            serves = [g for g in a["serves"] if g in unanswered]
            if a["tier"] == "vendor":
                # Paid credits: only spend them once the cheap gates already pass (a company that
                # fails G1 or G5 never needs its size). Because that hold can delay it past the gate's
                # max_passes, a vendor lookup ignores max_passes: it runs once and its credits are capped.
                if any(gates[g]["status"] != "pass" for g in self.cfg.get("vendor_after_gates_pass", [])):
                    continue
                serves = [g for g in a["serves"] if gates[g]["status"] == "unknown" and self._free_exhausted(g)]
            if a["tier"] == "scrape":
                serves = [g for g in serves if self._free_exhausted(g) and self._vendor_exhausted(g)]
            if serves:
                out.append({"action": aid, "serves": serves, "tier": a["tier"]})
        return out

    def _plan(self, pass_no, gates, allowed) -> tuple[list[dict], str, str]:
        n = self.cfg.get("max_actions_per_pass", 3)
        ids = [a["action"] for a in allowed]
        unanswered = sorted({g for a in allowed for g in a["serves"]})
        fallback = sorted(allowed, key=lambda a: min(self.icp["gate_order"].index(g) for g in a["serves"]))[:n]
        fallback = [{"action": a["action"], "gate": a["serves"][0], "why": "deterministic fallback"} for a in fallback]
        if self.llm_budget.remaining - self.reserve < 2:          # planner + at least one extract
            return fallback, "fallback", "LLM budget too low for a planner call; picked by gate order."
        schema = {
            "type": "object",
            "properties": {
                "reasoning": {"type": "string"},
                "stop": {"type": "boolean"},
                "actions": {"type": "array", "items": {
                    "type": "object",
                    "properties": {"action": {"type": "string", "enum": ids},
                                   "gate": {"type": "string", "enum": unanswered},
                                   "why": {"type": "string"}},
                    "required": ["action", "gate", "why"], "additionalProperties": False}},
            },
            "required": ["reasoning", "stop", "actions"], "additionalProperties": False,
        }
        user = self._planner_prompt(pass_no, gates, allowed, n)
        try:
            out = self.llm.call_json("planner", PLANNER_SYSTEM, user, schema, self.llm_budget, max_tokens=2000)
        except (LLMError, BudgetExceeded) as e:
            return fallback, "fallback", f"planner call failed ({e}); picked by gate order."
        chosen, seen = [], set()
        for a in out.get("actions", []):
            if a.get("action") in ids and a["action"] not in seen:     # re-check whitelist in code
                seen.add(a["action"])
                chosen.append({"action": a["action"], "gate": a.get("gate"), "why": a.get("why", "")})
        if out.get("stop") and not chosen:
            return [], "planner", out.get("reasoning", "")
        return chosen[:n] or fallback, "planner" if chosen else "fallback", out.get("reasoning", "")

    def _planner_prompt(self, pass_no, gates, allowed, n) -> str:
        lines = [f"Company: {self.company} ({self.domain})", f"Pass {pass_no} of {self.cfg['max_passes']}.",
                 f"Choose at most {n} actions.", "", "Unanswered gates:"]
        for g in self.icp["gate_order"]:
            if gates[g]["status"] == "unknown":
                gate = self.icp["gates"][g]
                lines.append(f"- {g} {gate['name']}: {gates[g]['reason']}. Sufficient: {gate['sufficient_answer']}")
        lines += ["", "Sources already tried:"]
        lines += [f"- {aid}: {d['outcome']}" for aid, d in self.done.items()] or ["- none"]
        lines += ["", "Facts found so far:"]
        lines += [f"- {f['key']}={f['value'].get('value')} ({f['source_type']})" for f in self.facts] or ["- none"]
        lines += ["", f"Budget left: pages {self.pages.remaining}, LLM calls {self.llm_budget.remaining - self.reserve}",
                  "", "allowed_actions:"]
        lines += [f"- {a['action']} (serves {', '.join(a['serves'])})" for a in allowed]
        return "\n".join(lines)

    def _log_decision(self, pass_no, decided_by, chosen, reasoning):
        entry = {"pass": pass_no, "decided_by": decided_by, "actions": chosen, "reasoning": reasoning}
        self.result.planner.append(entry)
        if self.store:
            self.store.planner_decision(self.domain, pass_no, decided_by, chosen, reasoning)

    # ------------------------------------------------------------ execution
    def _execute(self, pass_no, chosen):
        served = set()
        news_ids = [c["action"] for c in chosen if c["action"].startswith("news:")]
        for c in chosen:
            aid = c["action"]
            served.update(self.actions[aid]["serves"])
            if aid.startswith("news:"):
                continue
            if aid == "job_board_lookup":
                self._job_boards(pass_no)
            elif aid == "wikidata_lookup":
                self._wikidata(pass_no)
            elif aid == "hunter_company_lookup":
                self._company_lookup(pass_no)
            else:
                self._scrape(pass_no, aid)
        if news_ids:
            self._news(pass_no, news_ids)
        for g in served:
            if g in self.gate_passes:
                self.gate_passes[g] += 1

    def _add_facts(self, facts, dropped) -> list[int]:
        ids = []
        for f in facts:
            if any(x["key"] == f["key"] and x["value"] == f["value"] and x["source_url"] == f["source_url"]
                   for x in self.facts):
                continue
            f["id"] = self.store.fact(self.domain, f) if self.store else self._next_id
            self._next_id += 1
            self.facts.append(f)
            ids.append(f["id"])
        for d in dropped:
            self.result.dropped.append(d)
            if self.store:
                self.store.dropped(self.domain, d)
        return ids

    def _record(self, pass_no, aid, outcome, attempts, fact_ids, note=None):
        self.done[aid] = {"outcome": outcome, "pass": pass_no}
        self.result.actions.append({"pass": pass_no, "action": aid, "outcome": outcome,
                                    "attempts": attempts, "fact_ids": fact_ids, **({"note": note} if note else {})})

    def _extract(self, label, source: Source):
        if self.llm_budget.remaining - self.reserve <= 0:
            return None, "skipped_llm_cap"
        try:
            raw = extract_facts(self.llm, self.llm_budget, f"{self.company} ({self.domain})", label,
                                source.render())
        except (LLMError, BudgetExceeded) as e:
            return None, f"extract_failed: {e}"
        return validate(raw, source), None

    def _news(self, pass_no, news_ids):
        name = self.company
        results, attempts = {}, {}
        for aid in news_ids:
            qid = aid.split(":", 1)[1]
            query = self.queries[qid][1].format(company=name)
            if not self.api.take():
                attempts[aid] = [{"query": query, "outcome": "skipped_cap"}]
                continue
            items, outcome = news_search(self.fetcher, query, self.cfg.get("news_items_per_query", 8),
                                         self.news_throttle)
            results[aid] = items
            attempts[aid] = [{"query": query, "outcome": outcome, "items": len(items)}]
        digest = news_digest(results)
        fact_ids, err = [], None
        if digest.items:
            out, err = self._extract("News search results (numbered list)", digest)
            if out:
                fact_ids = self._add_facts(*out)
        for aid in news_ids:
            a = attempts[aid][0]
            outcome = a["outcome"] if a["outcome"] != "ok" or not err else err.split(":")[0]
            self._record(pass_no, aid, outcome, attempts[aid], fact_ids, err)

    def _job_boards(self, pass_no):
        cfg = self.icp["actions"]["job_board_lookup"]
        kws = self.icp["gates"]["G4"]["any_of"]["a_job_posting"]["title_keywords"]
        facts, attempts = job_board_lookup(self.fetcher, self.domain, cfg["url_templates"], kws, self.api.take)
        ids = self._add_facts(facts, [])
        outcome = "ok" if any(a["outcome"] == "ok" for a in attempts) else "not_found"
        self._record(pass_no, "job_board_lookup", outcome, attempts, ids)

    def _wikidata(self, pass_no):
        if not (self.api.take() and self.api.take()):
            self._record(pass_no, "wikidata_lookup", "skipped_cap", [], [])
            return
        facts, outcome = wikidata_employees(self.fetcher, self.domain)
        self._record(pass_no, "wikidata_lookup", outcome, [], self._add_facts(facts, []))

    def _company_lookup(self, pass_no):
        if self.company_lookup is None:
            self._record(pass_no, "hunter_company_lookup", "not_configured", [], [])
            return
        facts, outcome = self.company_lookup(self.domain)
        self._record(pass_no, "hunter_company_lookup", outcome, [], self._add_facts(facts, []))

    def _candidates(self, aid) -> list[tuple[str, tuple[str, ...]]]:
        a = self.icp["actions"][aid]
        own = (self.domain,)
        trust_hosts = tuple(self.icp.get("trust_center_hosts", []))
        # Homepage links count only if the last path segment is exactly one of the configured page
        # names (e.g. /about, /company/careers) at depth <= 2, or the host is a trust subdomain or
        # a known hosted trust center. Shortest path first, so /trust beats /trust/ai.
        slugs = {p.strip("/").split("/")[-1] for p in a.get("paths", []) if p.strip("/")}
        kws = [k.lower() for k in a.get("link_keywords", [])]
        linked = []
        for href, anchor in self.homepage_links:
            host = urlparse(href).netloc.lower().split(":")[0]
            segs = [s for s in urlparse(href).path.lower().split("/") if s]
            www = host.removeprefix("www.")
            hay = f"{'/'.join(segs)} {(anchor or '').lower()}"
            hit = next((i for i, k in enumerate(kws) if k in hay), None)
            if www == self.domain and segs and len(segs) <= 2 and segs[-1] in slugs:
                linked.append((len(segs), href, own))
            elif www == self.domain and segs and len(segs) <= 5 and hit is not None:
                # A link that names a certificate or security page. Certificate words (iso, soc, certificate)
                # go before everything, e.g. /en/document/certificate/iso-27001; generic words like
                # "security" or "trust" go after exact page names such as /trust.
                strong = any(w in kws[hit] for w in ("iso", "soc", "certif"))
                linked.append(((hit - 100) if strong else (10 + hit), href, own))
            elif any(host == f"{s}.{self.domain}" for s in a.get("subdomains", [])) and len(segs) <= 1:
                linked.append((len(segs), href, own))
            elif host in trust_hosts and aid == "scrape_trust":
                linked.append((len(segs), href, (host,)))
        urls: list[tuple[str, tuple[str, ...]]] = [(h, hosts) for _, h, hosts in sorted(linked, key=lambda x: x[0])]
        urls += [(f"https://{self.domain}{p}", own) for p in a.get("paths", [])]
        urls += [(f"https://{s}.{self.domain}/", own) for s in a.get("subdomains", [])]
        limit = self.cfg.get("max_candidate_urls_per_action", 2)
        if self.homepage_read and not linked and aid != "scrape_homepage":
            limit = 1                  # the homepage doesn't link it: try the most likely path only
        seen, out = set(), []
        for u, hosts in urls:
            key = u.rstrip("/")
            if key not in seen:
                seen.add(key)
                out.append((u, hosts))
        return out[:limit]

    def _scrape(self, pass_no, aid):
        attempts, page = [], None
        for url, hosts in self._candidates(aid):
            if not self.pages.take():
                attempts.append({"url": url, "outcome": "skipped_cap"})
                break
            res: FetchResult = self.fetcher.get(url, allowed_hosts=hosts)
            attempts.append({"url": url, "outcome": res.outcome, "status": res.status})
            if self.store:
                self.store.page(self.domain, url, aid, res.status, res.outcome)
            if res.ok:
                page = res
                break
        if page is None:
            outcomes = {a["outcome"] for a in attempts}
            outcome = "not_found" if outcomes == {"not_found"} else (sorted(outcomes - {"not_found"}) or ["error"])[0]
            self._record(pass_no, aid, outcome, attempts, [])
            return
        if aid == "scrape_homepage":
            self.homepage_links = extract_links(page.text, page.final_url or page.url)
            self.homepage_read = True
        text = clean_html(page.text, self.cfg.get("max_clean_text_chars", 12000))
        url = page.final_url or page.url
        badges = attestation_links(page.text, url, self.domain)
        if badges and len(text) >= self.cfg.get("min_page_text_chars", 200):
            # Cleaning drops footers, where companies often link their certificates
            # ("ISO Certificate 27001-2022"). Keep those link texts so they can be quoted as evidence.
            text += "\n\nCertificate and security links on this page:\n" + "\n".join(badges)
        if len(text) < self.cfg.get("min_page_text_chars", 200):
            # A JavaScript-rendered shell (common for hosted trust centers) has no readable text.
            # It is not evidence of absence, so it must not count as "checked".
            if self.store:
                self.store.page(self.domain, url, aid, page.status, "empty_page", text)
            self._record(pass_no, aid, "empty_page", attempts, [],
                         f"empty_page: only {len(text)} chars of text (likely rendered by JavaScript)")
            return
        if self.store:
            self.store.page(self.domain, url, aid, page.status, "cleaned", text)
        src = Source(label=url, items={"none": SourceItem(url=url, text=text, source_type="page",
                                                          date=self.today.isoformat())})
        out, err = self._extract(f"{aid} page {url}", src)
        if out is None:
            self._record(pass_no, aid, err.split(":")[0], attempts, [], err)
            return
        self._record(pass_no, aid, "ok", attempts, self._add_facts(*out))
