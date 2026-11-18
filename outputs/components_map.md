# Components map

The flow for each domain is: `shortlist`, then `agent_loop` (which drives `news`, `fetch`, `clean`, `extract`, `validate` and `store`), then `qualify`, `poc_hunter`, `draft_email` and `send_gmail`. `pipeline` orchestrates all of it.

---

### config/icp.yaml + src/config.py
- **Inputs:** `config/icp.yaml`, `.env`
- **Outputs:**
  - The validated ICP dict
  - The derived action whitelist (`all_actions`)
  - The news query map
- **Failure modes:**
  - A gate missing any required key (facts, sources, sufficiency, max passes, if-unanswered) raises `ConfigError` at start-up.
  - `if_unanswered` set to anything except `needs_review` raises `ConfigError`.
  - A variable that is both a gate and a weighted factor raises `ConfigError`.
  - A missing or non-positive loop cap raises `ConfigError`.

### src/shortlist.py (Step 2)
- **Inputs:**
  - The domain list
  - `data/hubspot_customers.csv` (required; column `Company Domain Name`). With `stage_column: Lifecycle Stage`, only rows in `exclude_stages` (customer, evangelist) are excluded; a row with no stage is excluded to be safe. Leads logged in HubSpot stay researchable.
  - `data/competitor_customers.csv` (optional)
  - `run.max_companies_per_run`
- **Outputs:** `(kept, removed)`. Each removed domain carries a reason (`in <file>`, `over run cap`, or `not a valid domain`).
- **Failure modes:**
  - A missing HubSpot file raises `FileNotFoundError`, because a run must never skip customer exclusion.
  - Domains are normalised: scheme, `www.`, path and case are stripped. A subsidiary on a different domain is not caught.

### src/research/fetch.py
- **Inputs:** a URL, plus allowed hosts for redirects.
- **Outputs:** `FetchResult` with `outcome` set to one of `ok`, `not_found`, `robots_blocked`, `challenge`, `error` or `off_domain`.
- **Failure modes:**
  - robots.txt disallows the URL: `robots_blocked`, and no request is made.
  - robots.txt returns 401 or 403: treated as disallow-all.
  - robots.txt can't be reached: treated as allow-all (RFC 9309).
  - CAPTCHA, Cloudflare challenge, login wall, 401, 403 or 429: `challenge`. Nothing is bypassed.
  - A redirect off the company's domain: `off_domain`.
  - A timeout (15 s) or connection error: `error`.

### src/research/clean.py
- **Inputs:** raw HTML and a character cap.
- **Outputs:**
  - Deduplicated plain text with nav, footer, scripts, cookie banners and menus removed, capped at 12,000 characters
  - `extract_links` (taken from the raw HTML, so nav links to /trust and /security still count)
- **Failure modes:**
  - JavaScript-only pages (many hosted trust centers) clean to almost nothing, so extraction returns unknown.
  - Content past the cap is lost.

### src/llm.py
- **Inputs:** `llm` block of `config/icp.yaml`; `GEMINI_API_KEY` (or `ANTHROPIC_API_KEY`).
- **Outputs:** parsed JSON matching the schema passed in. `CallBudget` counts one unit per logical call and refuses before the cap.
- **Provider:** `provider: gemini` by default.
  - Models are tried in order: `gemini-3.8-flash`, then `gemini-3.1-flash-lite`.
  - A 503 (overloaded) moves to the next model.
  - A 429 (rate limit) waits 20 seconds and retries once.
  - Any other client error fails at once.
  - The Claude client stays available with `provider: anthropic`.
- **Failure modes:**
  - Every model unavailable, truncated output, empty response or invalid JSON all raise `LLMError`. The calling step records `extract_failed`, or the planner uses its deterministic fallback.
  - This key's plan has no quota for Pro models (429), and the 2.5 models are closed to new users (404).
  - On 4 Nov 2026 every Flash model except `3.1-flash-lite` was returning 503.

### src/research/extract.py
- **Inputs:** the domain, a source label, and the cleaned text or a numbered news digest.
- **Outputs:** JSON in a fixed schema (`SCHEMA`), enforced by the provider's structured-output mode:
  - Every fact has `value`, a verbatim `quote` and an `item`.
  - A missing fact is `"unknown"`.
- **Failure modes:**
  - An API error, refusal or truncated output raises `LLMError`. The action is recorded as `extract_failed` and doesn't count as the source being checked.
  - Each call spends one unit of the company's LLM budget.

### src/research/validate.py
- **Inputs:** the raw extraction and its `Source` (a page, or numbered news items with URLs and dates).
- **Outputs:**
  - `facts`, each with `key`, `value`, `quote`, `source_url`, `source_type`, `date` and `date_basis`
  - `dropped`
- **Failure modes (each drops the fact and records why):**
  - Schema violation
  - The quote is not found in the source text after whitespace, quote-mark and case normalisation
  - The quote names a news item that isn't in the source
- **Dates:** a fact with no stated date takes the article's publish date (news) or the fetch date (page).

### src/research/news.py (free tier)
- **Inputs:** the domain, query templates, job-board URL templates, and a run-wide `Throttle`.
- **Outputs:**
  - The company name used in queries and prompts. Resolved in this order: a name you give in the input file (`domain, Name`), then Clearbit's free autocomplete API (accepted only when it returns this exact domain), then the domain stem. The trail records the name and where it came from.
  - Bing News RSS items. The publisher URL is unwrapped from Bing's redirect link, and each item keeps the publisher name, publish date and summary.
  - Job-board facts: titles matching the compliance keywords, each with its posting URL, no LLM
  - Wikidata headcount (P1128), only when the item's official website (P856) matches the domain
- **Why Bing:**
  - Google News RSS is disallowed by its robots.txt.
  - GDELT rate-limited or timed out 45 of 48 requests from this network.
  - Microsoft's terms of use for automated querying were not reviewed.
- **Failure modes:**
  - Calls are spaced at least 2 seconds apart across the whole run. An HTTP 429 is retried once, then recorded as `rate_limited`, which doesn't count as the source being checked.
  - Queries are simple phrases because Bing handles complex OR queries poorly.
  - When Clearbit has no exact match, the domain stem is used. A stem that differs from the brand finds nothing, and a generic one (e.g. `plane`) picks up other companies. Give the name in the input file to fix either.
  - Even with the right name, other companies can share it. The extract prompt tells the model to ignore them, but that is a model judgement.
  - The job-board slug is guessed from the domain stem, so a board under a different slug is missed.
  - The Wikidata API is disallowed by robots.txt, so the lookup records `robots_blocked`.
  - A reply that isn't JSON or XML is recorded as `unparseable` or `error`.

### src/research/store.py
- **Inputs:** everything above.
- **Outputs:** SQLite `db/outreach.sqlite`, with the schema in `db/schema.sql`. Tables:
  - `runs`, `companies`, `pages`, `facts`, `dropped_facts`
  - `planner_decisions`, `gate_results`, `llm_calls`
  - `hunter_cache`, `hunter_usage`
  - `contacts`, `drafts`, `sends`
- **Failure modes:**
  - The database is local and single-writer, so parallel runs on one database are not supported.
  - Deleting the database also deletes the Hunter cache and quota counter, so Hunter could be searched again.

### src/agent_loop.py (Step 4)
- **Inputs:**
  - The ICP, a domain, a fetcher and the LLM
  - Caps: `max_passes` 3, `max_pages_per_company` 8, `max_free_api_calls_per_company` 12, `max_llm_calls_per_company` 12 (1 reserved for the draft)
- **Outputs:** `LoopResult`, containing:
  - Facts, gate results, exit reason
  - Every action with its URLs and outcomes
  - Every planner decision with its reasoning (also written to the `planner_decisions` table)
  - Gate status after each pass
  - Usage against each cap
- **How a run goes:**
  - Pass 1 runs all free sources.
  - Passes 2 and 3 use the planner. It receives the unanswered gates, the sources already tried, the facts found and the allowed action ids. Its JSON schema `enum` holds only the allowed ids, and code re-checks every id.
  - Code builds every URL from config paths or links found on the homepage.
  - In passes 2–3, code puts `scrape_homepage` first whenever another scrape is chosen (logged as "added by code"). Each scrape tries at most 2 URLs. Once the homepage has been read and does not link the page, it tries only the first configured path, so 404 guesses do not use up the 8-page budget.
  - A page that loads but has under 200 characters of readable text (typically a JavaScript-rendered trust center) is recorded as `empty_page`. It does not count as checked, so it can never pass G5 or fail G4, and no LLM call is spent on it.
  - The loop exits when a gate fails (short-circuit), when all gates are answered, at the pass cap, when the LLM budget is down to its reserve, when no action is allowed, or when the planner says stop.
- **Failure modes:**
  - The loop refuses to start without every cap (`CapError`).
  - If the planner call fails or the budget is too low, a deterministic fallback picks actions by gate order, and the trail records it as `fallback`.
  - An action that would breach a cap is skipped and recorded as `skipped_cap`.
  - Hitting the page cap usually leaves G5 or G4 unknown, so the company goes to `needs_review`.

### src/qualify.py (Step 5)
- **Inputs:** validated facts, the action outcomes, and today's date.
- **Outputs:**
  - Per gate: `pass`, `fail` or `unknown`, with a reason that quotes the evidence and the fact ids
  - `decide()` returns `no_go` (first failed gate), `needs_review`, or `go`. Gates marked `scoring_only` (G4) are skipped here.
  - `weighted_score()` for go companies only; it adds a scoring-only gate's `weight` when that gate passes
- **Failure modes:**
  - None at runtime; these are pure functions.
  - The rules are deliberately conservative. Missing evidence never passes, and G4 or G5 only resolve on absence of evidence after the named sources were actually read.

### src/poc_hunter.py (Step 6)
- **Also used in Step 4 (vendor tier for G3):** `company_headcount` calls Hunter Company Enrichment once per domain, ever (cached in `hunter_company_cache`). It turns the size bucket (e.g. `251-1K`) into an `employee_count` fact dated by Hunter's `indexedAt`. It runs only after G3's free sources, and only once G1 and G5 both pass (`agent_loop.vendor_after_gates_pass`), so a company that fails those cheap gates never spends a credit. G3 scraping waits for it. A vendor size can pass G3 but cannot fail it: outside 100–1000 it leaves G3 unknown, with the figure in the reason. A search costs 1 credit and a company lookup 0.2, both counted against the 25-credit monthly cap.
- **Inputs:**
  - The domain and `HUNTER_API_KEY`
  - The quota (25 a month)
  - `contact.hunter_filters` (department `it,executive,management`, `required_field=position`)
  - The title ranking, aliases and accepted verification statuses
- **Outputs:**
  - `(hunter_json, how)`, where `how` is `cache`, `api`, `api_unfiltered`, `no_key`, `quota_exhausted` or `error`
  - `choose_contact` returns the contact, why it was chosen, and every candidate considered with its rejection reason. The title list runs CISO, Head of Compliance, Director of Security, Head of Security, VP Engineering, CTO, then the fallbacks Head of Engineering, Head of IT, Co-founder, CEO and COO (decision contact-fallback).
  - If no ranked contact has an accepted email, `verify_unverified_candidates` runs Hunter's Email Verifier on up to 2 of the best-ranked contacts whose email Hunter hadn't verified (0.5 credit each, cached per address), then chooses again. Only `valid` is accepted; `accept_all` is not.
- **Failure modes:**
  - A domain in the cache is never searched again, even if the cached result was empty.
  - The quota counter goes up before each call, so a failed call still counts.
  - The filtered search runs first. If it finds no ranked, verified contact, one unfiltered search follows, but only while quota remains, and the two result sets are merged. A company can therefore use 2 of the 25 monthly searches. Either way, one merged result is cached per domain.
  - Contacts whose `verification.status` isn't `valid` (e.g. `accept_all`) are rejected.
  - No match leaves the company at `needs_review`.

### src/draft_email.py (Step 7)
- **Inputs:**
  - The contact and why they were chosen
  - The validated facts with ids
  - The hook-eligible fact keys and Sprinto's value propositions
- **Outputs:** the draft, with:
  - Subject, body and CTA
  - Two pain points, each tied to a fact id
  - `hook_fact_id`, `hook_url`, `hook_quote` and the reasoning
- **Checks done in code, not left to the model:**
  - The hook is a hook-eligible fact
  - The first name is in the greeting
  - The body is under 120 words
  - The CTA appears word for word and the body has exactly one question
  - The body contains no links
  - Each of the two pain points maps to a real fact
- **Failure modes:**
  - No hook-eligible fact, or the model returns `hook_fact_id` -1: `needs_review`.
  - Failing the checks twice: `needs_review`.
  - The prompt forbids claims outside the facts, but code can't fully verify that, which is why the review email carries the evidence.

### src/send_gmail.py (Step 8)
- **Inputs:** `to`, subject, body, `OWN_EMAIL`, the `--dry-run` and `--review` flags, and the OAuth client file.
- **Outputs:** `(mode, message_id)`, where `mode` is `dry_run`, `declined` or `sent`.
- **Failure modes:**
  - Any recipient other than `OWN_EMAIL`, or `OWN_EMAIL` unset, raises `RecipientNotAllowed` before anything is built or printed. It is checked again on the final `To:` header, and there is an `assert` as well.
  - Missing OAuth client file raises `RuntimeError`.
  - The first live send opens a browser for OAuth consent and stores the token in `db/gmail_token.json`.

### src/pipeline.py
- **Inputs:** `--domains`, `--dry-run`, `--review` and `--limit`.
- **Outputs:**
  - `outputs/runs/<domain>.json`, the decision trail: facts with URLs, planner decisions, gates, contact and why, the email and why, and the final status
  - `outputs/runs/_summary_<run_id>.json`
  - A console summary: go, no-go, needs_review and retry counts, Hunter searches used, LLM calls
  - `outputs/runs/_retry_<run_id>.txt` when any company hit an LLM outage, ready to pass back as `--domains`
- **Status `retry`:** a company that would be needs_review, but where at least one source went unread because every LLM model was unavailable (Gemini 503 or quota), becomes `retry`: re-run it, don't review it. A go or no_go is kept, because its deciding evidence was read.
- **Failure modes:**
  - An error in one domain's send is caught and logged in that domain's trail, and the run continues.
  - An unexpected exception in one company's research is caught: that company becomes needs_review with the error as its reason, and the run continues.


### src/discover.py (target-account discovery)
- **Inputs:** the seller's `discovery.queries` (Bing News RSS, free), one LLM call to read the headlines, Clearbit's public company lookup.
- **Outputs:** candidates with name, round, verbatim quote, source URL, date, domain and status: `new`, `check_domain` (Clearbit's company of that name has a domain that isn't the name, so a person checks it), `researched`, `excluded` (HubSpot customer) or `no_domain`.
- **Failure modes:** a quote not found in its news item drops the company; a lookup error leaves `no_domain`. Discovery never researches anything by itself.

### src/leaders.py (contact fallback)
- **Inputs:** company name and domain, two free news queries (`"{company}" founder`, `"{company}" CEO`), one LLM call.
- **Outputs:** leaders whose name and role appear together in a verbatim quote, ranked by the contact title list. The pipeline then calls Hunter's Email Finder (1 credit, cached per person) for up to `leader_finder_max` of them and uses an address only if its verification is `valid`.

### src/webapp.py + src/web/index.html (Prospect Desk)
- **Inputs:** HTTP on 127.0.0.1:8765 only (requests with any other Host header get 403).
- **Endpoints:** `GET /api/seller?name=`, `POST /api/discover`, `POST /api/research` (at most `run.max_companies_per_run` domains), `GET /api/jobs/<id>`, `GET /api/accounts`, `POST /api/send` (OWN_EMAIL only, through `send_gmail.send`).
- **Behaviour:** one discovery or research job at a time in a background thread; the page polls every 2.5 s. Research always runs as a dry run; sending is a separate click per account.
- **Failure modes:** a failing company becomes needs_review with the error as its reason and the job continues; a failed job shows its error in the page.
