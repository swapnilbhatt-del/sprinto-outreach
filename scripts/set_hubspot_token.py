"""Paste the HubSpot private-app token into a macOS dialog; check it, save it to .env and Vercel.

    .venv/bin/python scripts/set_hubspot_token.py

The token is never printed. It is checked against HubSpot (read one company) before it is saved.
"""
import subprocess
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent


def ask() -> str:
    script = ('display dialog "Paste the HubSpot access token (starts with pat-) and click OK" '
              'default answer "" with hidden answer with title "Prospect Desk: connect HubSpot" '
              'buttons {"Cancel", "OK"} default button "OK"\n'
              'text returned of result')
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def main() -> int:
    tok = ask()
    if not tok:
        print("No token entered; nothing changed.")
        return 1
    r = httpx.get("https://api.hubapi.com/crm/v3/objects/companies", params={"limit": 1},
                  headers={"Authorization": f"Bearer {tok}"}, timeout=20)
    if r.status_code != 200:
        print(f"HubSpot refused the token (HTTP {r.status_code}). Check the app has the "
              f"crm.objects.companies.read and .write scopes, then run again.")
        return 1
    env = ROOT / ".env"
    lines = [l for l in env.read_text().splitlines() if not l.startswith("HUBSPOT_TOKEN=")]
    env.write_text("\n".join(lines + [f"HUBSPOT_TOKEN={tok}"]) + "\n")
    subprocess.run(["vercel", "env", "rm", "HUBSPOT_TOKEN", "production", "--yes"], cwd=ROOT,
                   capture_output=True)
    add = subprocess.run(["vercel", "env", "add", "HUBSPOT_TOKEN", "production"], cwd=ROOT, input=tok,
                         capture_output=True, text=True)
    where = "and to Vercel" if add.returncode == 0 else "(Vercel update failed: run scripts/push_env.sh)"
    print(f"HubSpot accepted the token. Saved to .env {where}. You can tell Claude: done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
