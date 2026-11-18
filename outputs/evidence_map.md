# Evidence map

Generated from `config/icp.yaml` (version 2, confirmed 2026-11-04). One row per ICP variable.

**Decision rule.** Gates are checked in this order: G1, G5, G3, G2, G4. G4 is scoring-only (decision G4-scoring, chosen 2026-11-10): it is still researched, adds 3 points when met, and never makes a company `no_go` or `needs_review`. Among G1, G5, G3 and G2, the first failed gate makes the company `no_go` and stops its research. If no gate failed but one is still unknown when the loop exits, the status is `needs_review`. The status is `go` only when G1, G5, G3 and G2 all pass. Weighted factors only rank `go` companies.

**Funding facts.** A funding-round fact is kept only if its quote names the round ("Series B", "acquired", "IPO"); a quote like "raised $30 million" can't set the round.

**Source order.** Pass 1 runs every free source: Bing News RSS (Google News RSS is disallowed by its robots.txt, and GDELT rate-limited this network), the Greenhouse, Lever and Ashby job-board APIs, Wikidata and the press page. A scrape action for a gate is allowed only after every free source listed for that gate has run. Hunter is used for the contact and, as a vendor source for G3, for company size.

| Variable | Fact that settles it | Where it lives | What counts as sufficient | Max passes | If unanswered | Gate or weighted |
|---|---|---|---|---|---|---|
| **G1** SaaS business | Sells internet-delivered software; buyers are businesses | Scrape: homepage, about. No free source states this reliably | **Pass:** a quote showing software and a quote showing business buyers. **Fail:** a quote showing it's not software, or consumer-only | 2 | needs_review | Gate |
| **G2** Funding Series A–C | Latest priced round and its date | Free: Bing News (`"{co}" raises`, `"{co}" funding round`, `{co} series round`, `"{co}" acquired`), press page. Scrape: about | **Pass:** the latest round dated by the article (a page's fetch date never counts) is Series A, B or C and is within 36 months. **Fail:** the latest dated round is Series D or later, an IPO or an acquisition, or a seed round under 18 months old. No round found counts as unknown | 2 | needs_review | Gate |
| **G3** Employees 100–1000 | A stated headcount number or range | Free: Wikidata P1128, Bing News, press page. Vendor: Hunter company size bucket (0.2 credit, cached per domain, run only once G1 and G5 pass; it can pass G3 but never fails it alone, so a vendor figure outside 100–1000 means needs_review). Scrape: about, careers | **Pass:** the latest figure within 24 months lies entirely inside 100–1000. **Fail:** entirely below 100 (short-circuit) or entirely above 1000. A range that straddles a boundary counts as unknown | 2 | needs_review | Gate |
| **G4** Compliance pressure (any of a/b/c) | (a) Open compliance, GRC or security role. (b) Trust or security page says SOC 2 or ISO 27001 is in progress, planned or customer-requested. (c) New security or compliance leader within 12 months | (a) Free: job-board APIs. Scrape: careers. (b) Scrape: trust, security. (c) Free: Bing News (`"{co}" CISO`, `"{co}" "head of security"`), press page | **Pass:** any one met, with a quote and URL. **Fail:** all of these were checked and none was met: job boards, careers page, trust page, security page and both news queries (shown as 0 points) | 3 | 0 points | Weighted, +3 (was a gate until 2026-11-10) |
| **G5** No SOC 2 Type II / ISO 27001 | No claim of an attestation on the site or in the news | Free: Bing News (`"{co}" "SOC 2"`, `"{co}" "ISO 27001"`). Scrape: homepage, trust, security | **Fail:** any quote claiming SOC 2 Type II or ISO 27001 was achieved. **Pass:** homepage read, trust or security page read (or both returned 404), both news queries run, nothing found. A Type I report alone still passes | 3 | needs_review | Gate |
| W1 Upmarket motion | Enterprise plan, "contact sales", enterprise logos | Any page read | Quote + URL | within loop caps | scores 0 | Weighted (3) |
| W2 Regulated vertical | Fintech, healthtech, insurance, HR/payroll, or a PII-heavy product | Any page read | Quote + URL | within loop caps | scores 0 | Weighted (3) |
| W3 Recent funding | Series round within 12 months | Reuses G2 evidence | Dated article | within loop caps | scores 0 | Weighted (2) |
| W4 EU / GDPR exposure | EU office, EU customers, or a GDPR mention | Any page read | Quote + URL | within loop caps | scores 0 | Weighted (1) |
| W5 SOC 2 Type I only | Type I without Type II | Trust or security page | Quote + URL | within loop caps | scores 0 | Weighted (1) |

**Known gaps in the free tier:** Bing News searches on the company name: the name given in the input file, else Clearbit's exact-domain match, else the domain stem. A stem fallback can miss a company (`hevodata` vs "Hevo Data") or pick up unrelated ones, and the extract prompt has to filter those out. The Wikidata API is disallowed by robots.txt for this crawler, so that lookup is recorded as `robots_blocked` and G3 falls back to pages.

**Loop caps per company:** 3 passes, 8 pages, 15 free API calls (1 name lookup, 8 news queries, up to 3 job-board probes, 2 Wikidata calls), and 12 LLM calls, with 1 kept back for the draft. **Run cap:** 10 companies.

Every fact needs a verbatim quote that `validate.py` finds in the source text. Facts without one are dropped, logged in `dropped_facts`, and the variable they would have settled stays unknown.
