# Sprinto outreach pipeline

This pipeline takes a list of prospect domains and works through each one. For every company it:
1. Qualifies it against Sprinto's ICP, using an agent loop with hard caps.
2. Picks a decision-maker through Hunter.
3. Drafts an email built only on facts it has verified.
4. Sends that draft to **your own address only** for review.

Every step leaves a decision trail in `outputs/runs/<domain>.json`.

**Prospect Desk** (`src/webapp.py`) puts this in a local web app: type the company you sell for,
find target accounts in funding news or paste your own, and see each account's decision, the people
to contact, their verified emails and a drafted email. See [Prospect Desk](#prospect-desk-web-app) below.

Results of the November 2026 runs (28 companies, 3 qualified) are summarised in the
[run report](https://claude.ai/artifact/MPaEzXSJB5gdMw837eJZDJ).

## Setup

```bash
/opt/homebrew/bin/python3.12 -m venv .venv
```

```bash
.venv/bin/pip install -r requirements.txt
```

```bash
cp .env.example .env
```

Then fill in `.env`:

| Variable | What it's for |
|---|---|
| `GEMINI_API_KEY` | LLM calls for extraction, planning and drafting (default provider) |
| `ANTHROPIC_API_KEY` | Only if you set `llm.provider: anthropic` in `config/icp.yaml` |
| `HUNTER_API_KEY` | Hunter contact search, company size and email verification. They share 25 credits a month (search 1, size 0.2, email check 0.5), and the pipeline enforces that. |
| `OWN_EMAIL` | The only address the pipeline can ever send to |
| `GMAIL_CLIENT_SECRET_FILE` | Google Cloud OAuth client (Desktop app) with the Gmail API enabled. The first live send opens a browser for consent (scope `gmail.send`). Step-by-step setup is in `NEXT_STEPS.md`; check it with `.venv/bin/python -m src.gmail_test`. |

Replace `data/hubspot_customers.csv` with your real HubSpot company export. The `Company Domain Name` column is used. The file shipped here is a stand-in.

## Run

```bash
.venv/bin/python -m src.pipeline --domains data/sample_domains.txt --dry-run
```

| Flag | Effect |
|---|---|
| `--dry-run` | Print each email instead of sending it |
| `--review` | Ask `Send this email? [y/N]` in the terminal before each send |
| *(neither)* | Send automatically to `OWN_EMAIL` |
| `--limit N` | Override the per-run cap (default 10) |

The domains file has one domain per line, optionally followed by the company name: `hevodata.com, Hevo Data`. Give a name when the brand differs from the domain. Without one, the name comes from Clearbit (only on an exact domain match) or the domain itself.

## Prospect Desk (web app)

```bash
.venv/bin/python -m src.webapp
```

Then open http://127.0.0.1:8765. It runs only on this Mac: the API keys stay in `.env`, and contact
emails never leave the computer (the server answers only on localhost).

1. **Who are you selling for?** Type the company you represent. Only Sprinto is set up (the `seller`
   block in `config/icp.yaml`); other names get a clear "not set up yet" message.
2. **Choose target accounts.** "Find companies in funding news" reads free Bing News for recent
   Series A–C raises, keeps only companies a headline names as the raiser (quoted), and looks up
   their website. A website whose domain doesn't match the name is marked "check it" and left
   unticked. You can also paste domains. "Research" runs the full pipeline on each one, without sending.
3. **Accounts and people.** Every researched account with its status and the reason for each gate.
   For qualified accounts: the people to target (name, title, email, verification, why chosen), the
   drafted email with its source, a copy button, and "Send to my inbox" (OWN_EMAIL only).

Hunter's live credit balance is shown at the top and caps every run.

## Test

```bash
.venv/bin/python -m pytest -q
```

## Where things are

- **ICP, gates, weights, caps, title list, whitelist:** `config/icp.yaml`. The `decisions` block in that file records why each value was chosen.
- **What settles each variable:** `outputs/evidence_map.md`
- **Every module's inputs, outputs and failure modes:** `outputs/components_map.md`
- **Database:** `db/outreach.sqlite`. It holds every page, fact, dropped fact, planner decision, LLM call, Hunter response, draft and send.

## Guarantees and where they are enforced

| Rule | Where | Test |
|---|---|---|
| Any failed gate means no_go, with a short-circuit | `qualify.decide`, `agent_loop.run` | `test_qualify.py` |
| An unknown gate means needs_review, never go | `qualify.decide` | `test_needs_review_when_a_gate_is_unknown` |
| An LLM outage means retry, not needs_review | `pipeline.process_domain` | `test_llm_outage_gives_retry_not_needs_review` |
| The loop cannot run without caps and stops at them | `AgentLoop.__init__`, `Counter`, `CallBudget` | `test_agent_loop.py` |
| The planner can only pick whitelisted actions, and code builds every URL | `_allowed_actions`, schema `enum`, re-check in `_plan` | `test_planner_cannot_invent_actions_or_urls` |
| Free sources run before scraping, for each variable | `_seed_actions`, `_free_exhausted` | `test_no_scraping_before_free_sources` |
| Facts without a quote found in the source are dropped | `validate.validate` | `test_validate_drops_facts_without_supporting_quote` |
| A domain is never searched on Hunter twice, and searches stop at 25 a month | `poc_hunter.domain_search` | `test_hunter_*` |
| Email goes only to `OWN_EMAIL` | `send_gmail.check_recipient` (explicit raise + assert, checked twice) | `test_send_refuses_other_recipients` |
| robots.txt is respected; CAPTCHAs and logins are never bypassed | `fetch.Fetcher` | none (network-dependent) |
| News comes from Bing News RSS (allowed by robots.txt), with publisher URLs as sources and at least 2 seconds between calls | `news.news_search`, `news.Throttle` | `test_news.py` |
