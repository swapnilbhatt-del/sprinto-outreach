"""One-time Gmail check: validates the OAuth client file, runs the consent flow once, and sends
one test email to OWN_EMAIL (the only allowed recipient).

    .venv/bin/python -m src.gmail_test            # checks only, sends nothing
    .venv/bin/python -m src.gmail_test --send     # consent in the browser, then one test email
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .config import ROOT, env, load_env
from .send_gmail import TOKEN_PATH, RecipientNotAllowed, check_recipient, send


def check_client_file(path: str | None) -> tuple[bool, str]:
    """Checks the file without printing any of its contents."""
    if not path:
        return False, "GMAIL_CLIENT_SECRET_FILE is not set in .env"
    p = Path(path)
    if not p.is_absolute():
        p = ROOT / p
    if not p.exists():
        return False, f"no file at {p} (download it from Google Cloud and save it there)"
    try:
        data = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return False, f"{p.name} is not valid JSON"
    if "installed" not in data:
        kind = next(iter(data), "unknown")
        return False, f"{p.name} is a '{kind}' client; create an OAuth client of type 'Desktop app'"
    if not {"client_id", "client_secret"} <= set(data["installed"]):
        return False, f"{p.name} is missing client_id or client_secret"
    return True, f"{p.name} is a Desktop-app OAuth client"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Check Gmail setup; optionally send one test email to OWN_EMAIL")
    ap.add_argument("--send", action="store_true", help="run consent and send one test email to OWN_EMAIL")
    args = ap.parse_args(argv)
    load_env()
    own = env("OWN_EMAIL")
    try:
        check_recipient(own or "", own)
        print("OK  OWN_EMAIL is set")
    except RecipientNotAllowed as e:
        print(f"ERR {e}")
        return 1
    ok, msg = check_client_file(env("GMAIL_CLIENT_SECRET_FILE"))
    print(("OK  " if ok else "ERR ") + msg)
    if not ok:
        return 1
    print("OK  token already saved (no consent needed)" if TOKEN_PATH.exists()
          else "..  first send will open a browser for consent (scope gmail.send only)")
    if not args.send:
        print("Setup looks right. Run again with --send to send one test email to OWN_EMAIL.")
        return 0
    path = env("GMAIL_CLIENT_SECRET_FILE")
    path = path if Path(path).is_absolute() else str(ROOT / path)
    mode, msg_id = send(own, "[Sprinto pipeline] Gmail test", "This is a test from the outreach pipeline. "
                        "Only OWN_EMAIL can receive mail from it.", own, client_secret_file=path)
    print(f"OK  {mode}: message id {msg_id}" if mode == "sent" else f"..  {mode}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
