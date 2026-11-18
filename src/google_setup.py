"""One-time Google consent for the deployed app (Gmail send + read, Calendar free/busy + events).

    .venv/bin/python -m src.google_setup

Opens a browser, saves the login to db/google_token.json (gitignored). scripts/push_env.sh then copies
it to Vercel as GOOGLE_TOKEN_JSON. Nothing is printed except where the file went.
"""
from __future__ import annotations

from pathlib import Path

from .config import ROOT, env, load_env
from .google_client import SCOPES, TOKEN_FILE


def main() -> int:
    load_env()
    secret = env("GMAIL_CLIENT_SECRET_FILE")
    if not secret or not Path(secret).exists():
        print("GMAIL_CLIENT_SECRET_FILE is missing; see NEXT_STEPS.md")
        return 1
    from google_auth_oauthlib.flow import InstalledAppFlow
    flow = InstalledAppFlow.from_client_secrets_file(secret, SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")
    if not creds.refresh_token:
        print("Google returned no refresh token; run again and approve all requested access.")
        return 1
    TOKEN_FILE.write_text(creds.to_json())
    print(f"Saved Google login to {TOKEN_FILE.relative_to(ROOT)} (scopes: Gmail send/read, Calendar).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
