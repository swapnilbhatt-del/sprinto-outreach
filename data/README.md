# data/

- `hubspot_customers.csv` — the company list from your HubSpot portal (247653502) as of 2026-11-10:
  HubSpot's sample record plus the five prospects the pipeline logged (all lifecycle Lead). Only rows
  whose `Lifecycle Stage` is customer or evangelist are excluded (config `exclude_stages`); a row
  with no stage is excluded to be safe. Re-export when the CRM changes (Companies › Export, keep
  `Company Domain Name` and `Lifecycle Stage`).
- `hubspot_customers_standin.csv` — the earlier illustrative stand-in (Chargebee, Freshworks, …).
  Not used by the pipeline; kept as an example of the layout and of a messy domain.
- `sample_domains.txt` — 10 test domains, one per line, optionally `domain, Company Name`. `chargebee.com` and `freshworks.com` appear in the
  stand-in file only; with the real HubSpot file they are no longer excluded.
- `competitor_customers.csv` — optional; not provided.
