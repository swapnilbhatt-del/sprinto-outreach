# Status

Done on 2026-11-10: Gmail is set up (token saved in `db/gmail_token.json`) and the first
real draft, for Wealthy, was sent to OWN_EMAIL. The real HubSpot list replaced the stand-in.
Use `--review` for later runs.

The project is a git repository (branch `main`). `.env`, `client_secret.json`, the Gmail token,
the database and `outputs/runs/` are never committed. If a run prints `retry`, Gemini was down:
re-run the `_retry_<run_id>.txt` file it names.

# What's left for you

Everything else is built and tested. Only two things need you, because they need your Google
login or your HubSpot account.

## 1. Gmail OAuth file (about 5 minutes)

`.env` already points to `sprinto-outreach/client_secret.json`. You only need to create that file.

1. Open https://console.cloud.google.com/projectcreate and create a project (any name, e.g. `sprinto-outreach`).
2. Open https://console.cloud.google.com/apis/library/gmail.googleapis.com and click **Enable**.
3. Open https://console.cloud.google.com/auth/branding and fill in the consent screen:
   - App name: anything. Support email and developer email: your own.
   - Audience: **External**, or **Internal** if your school account offers it.
4. Open https://console.cloud.google.com/auth/audience. Under **Test users**, add the address in `OWN_EMAIL`.
5. Open https://console.cloud.google.com/auth/clients, then click **Create client**:
   - Application type: **Desktop app**.
   - Name: anything.
6. Click **Download JSON**. Rename the file to `client_secret.json` and save it in this folder (`sprinto-outreach/`).
   It is already in `.gitignore`.

Then check it. This sends nothing:

```bash
.venv/bin/python -m src.gmail_test
```

Send one test email to yourself. A browser opens once and asks you to allow "Send email on your behalf":

```bash
.venv/bin/python -m src.gmail_test --send
```

If your school (Google Workspace) account blocks creating a Cloud project or unverified apps, do
steps 1–6 with a personal Gmail account instead. Then set `OWN_EMAIL` in `.env` to that address.

## 2. HubSpot — done 2026-11-10

- The five qualified prospects are now company records in your HubSpot portal (lifecycle Lead),
  each with a one-line pipeline summary in Description: Wealthy, Infinite Uptime, Fleetx,
  Intangles, Vahan.
- `data/hubspot_customers.csv` mirrors that list. The pipeline now skips only **customer** (and
  evangelist) stages, so leads you log stay researchable. Re-export after you mark real customers.
- HubSpot's own "Set Use Case" start-guide step is not done: it launches HubSpot's AI setup agent,
  which this session's permission checker blocked. Do it yourself in the Start Guide if you want it.

## Then send the first real draft (to yourself)

One company has reached go: **Wealthy (wealthy.in)**. The run below researches it again, drafts
the email, shows it in the terminal and asks `Send this email? [y/N]`. If you answer y, it goes to
OWN_EMAIL only; the code refuses any other address.

```bash
.venv/bin/python -m src.pipeline --domains data/ready_to_send.txt --review
```

Before forwarding anything to the prospect, read the internal notes in that email:
- **⚠ NAME CHECK:** Hunter's first and last name for the CEO may be swapped (the email address
  starts with what Hunter calls the last name). Confirm the greeting on LinkedIn.
- G4 (compliance pressure) found no signal, so the hook is the Series B round, not a security trigger.

## Where things stand (2026-11-10)

| Company | Status | What stopped it |
|---|---|---|
| Wealthy | **go**, draft sent to you | CEO; check the name order |
| Infinite Uptime | **go**, draft sent to you | Managing Director, verified email |
| Fleetx | needs_review | Hunter has 4 addresses, none with a job title |
| Intangles | needs_review | Hunter has no addresses for intangles.ai |
| Vahan | needs_review | Hunter has 4 addresses, none with a job title |
| Rewaa | needs_review | Only a 2023 Series A found (its Dec 2025 Series B isn't in Bing News) |
| Logcomex | needs_review | No dated round or headcount found |
| Mozark | needs_review | Homepage is JavaScript-only, so SOC 2 status can't be read |
| Petpooja | no_go | Over 1,000 staff |

For the needs_review companies, the run files in `outputs/runs/<domain>.json` show each gate's reason.
Hunter credits used in November: about 14.5 of 25 (11 searches, 15 size lookups, 1 email check).
