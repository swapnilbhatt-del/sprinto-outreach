"""Step 8: send via Gmail API (scope gmail.send). The only possible recipient is OWN_EMAIL."""
from __future__ import annotations

import base64
from email.message import EmailMessage
from pathlib import Path
from typing import Callable

from .config import ROOT

SCOPES = ["https://www.googleapis.com/auth/gmail.send"]
TOKEN_PATH = ROOT / "db" / "gmail_token.json"


class RecipientNotAllowed(RuntimeError):
    pass


def check_recipient(to: str, own_email: str | None) -> str:
    """Raise unless `to` is exactly OWN_EMAIL. Explicit raise as well as assert: asserts vanish under -O."""
    if not own_email or "@" not in own_email:
        raise RecipientNotAllowed("OWN_EMAIL is not set")
    if (to or "").strip().lower() != own_email.strip().lower():
        raise RecipientNotAllowed(f"refusing to send to {to!r}: only OWN_EMAIL is allowed")
    assert to.strip().lower() == own_email.strip().lower()
    return own_email.strip()


def _gmail_service(client_secret_file: str):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(client_secret_file, SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_PATH.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def send(to: str, subject: str, body: str, own_email: str | None, *, dry_run: bool = False,
         review: bool = False, client_secret_file: str | None = None,
         input_fn: Callable[[str], str] = input, print_fn: Callable[[str], None] = print,
         service=None) -> tuple[str, str | None]:
    """Returns (mode, message_id). mode: dry_run | declined | sent."""
    recipient = check_recipient(to, own_email)     # first line of defence, before anything else happens
    msg = EmailMessage()
    msg["To"] = recipient
    msg["Subject"] = subject
    msg.set_content(body)

    if dry_run:
        print_fn(f"[dry-run] To: {recipient}\nSubject: {subject}\n\n{body}")
        return "dry_run", None
    if review:
        print_fn(f"To: {recipient}\nSubject: {subject}\n\n{body}")
        if input_fn("Send this email? [y/N] ").strip().lower() not in ("y", "yes"):
            return "declined", None
    if service is None:
        if not client_secret_file or not Path(client_secret_file).exists():
            raise RuntimeError("GMAIL_CLIENT_SECRET_FILE is not set or missing")
        service = _gmail_service(client_secret_file)
    check_recipient(msg["To"], own_email)          # re-check the header actually being sent
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    sent = service.users().messages().send(userId="me", body={"raw": raw}).execute()
    return "sent", sent.get("id")
