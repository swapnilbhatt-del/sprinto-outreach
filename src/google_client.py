"""Google APIs for the deployed app: Gmail (send + read replies) and Calendar (free/busy + book).

Credentials come from GOOGLE_TOKEN_JSON (on Vercel) or db/google_token.json (local), created once by
`python -m src.google_setup`. Every send still passes send_gmail.check_recipient: OWN_EMAIL only."""
from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timezone
from email.message import EmailMessage

from .config import ROOT
from .send_gmail import check_recipient

SCOPES = ["https://www.googleapis.com/auth/gmail.send",
          "https://www.googleapis.com/auth/gmail.readonly",
          "https://www.googleapis.com/auth/calendar.events",
          "https://www.googleapis.com/auth/calendar.freebusy"]
TOKEN_FILE = ROOT / "db" / "google_token.json"
AGENT_HEADER = "X-Prospect-Desk"          # marks messages the app sent, so replies can be told apart


class GoogleNotConfigured(RuntimeError):
    pass


def credentials():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    raw = os.environ.get("GOOGLE_TOKEN_JSON")
    if not raw and TOKEN_FILE.exists():
        raw = TOKEN_FILE.read_text()
    if not raw:
        raise GoogleNotConfigured("Google access isn't set up: run `python -m src.google_setup`.")
    creds = Credentials.from_authorized_user_info(json.loads(raw), SCOPES)
    if not creds.valid:
        creds.refresh(Request())          # uses the stored refresh token; nothing is written back
    return creds


def _svc(name, version):
    from googleapiclient.discovery import build
    return build(name, version, credentials=credentials(), cache_discovery=False)


def gmail_service():
    return _svc("gmail", "v1")


def send_email(to: str, subject: str, body: str, *, thread_id: str | None = None,
               reply_to_message_id: str | None = None, kind: str = "outreach", service=None) -> dict:
    """Send one plain-text email. `to` must equal OWN_EMAIL. Returns {id, threadId, rfc_id}."""
    own = os.environ.get("OWN_EMAIL")
    recipient = check_recipient(to, own)
    msg = EmailMessage()
    msg["To"] = recipient
    msg["Subject"] = subject
    msg[AGENT_HEADER] = kind
    if reply_to_message_id:
        msg["In-Reply-To"] = reply_to_message_id
        msg["References"] = reply_to_message_id
    msg.set_content(body)
    check_recipient(msg["To"], own)                           # re-check the header actually sent
    payload = {"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode()}
    if thread_id:
        payload["threadId"] = thread_id
    svc = service or gmail_service()
    sent = svc.users().messages().send(userId="me", body=payload).execute()
    full = svc.users().messages().get(userId="me", id=sent["id"], format="metadata",
                                      metadataHeaders=["Message-ID"]).execute()
    rfc = next((h["value"] for h in full.get("payload", {}).get("headers", []) if h["name"].lower() == "message-id"), None)
    return {"id": sent["id"], "threadId": sent.get("threadId"), "rfc_id": rfc}


def _plain(payload: dict) -> str:
    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", "replace")
    for part in payload.get("parts", []) or []:
        t = _plain(part)
        if t:
            return t
    return ""


def strip_quoted(text: str) -> str:
    """The new part of a reply: drop the quoted earlier message ("On ... wrote:" and "> " lines)."""
    out = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith(">") or (s.startswith("On ") and s.endswith("wrote:")):
            break
        out.append(line)
    return "\n".join(out).strip()


def thread_replies(thread_id: str, service=None) -> list[dict]:
    """Messages in the thread that the app did NOT send (no X-Prospect-Desk header), oldest first.
    Each: {id, rfc_id, at (ISO), text}. In this build the "prospect" is you, replying from your inbox."""
    svc = service or gmail_service()
    th = svc.users().threads().get(userId="me", id=thread_id, format="full").execute()
    out = []
    for m in th.get("messages", []):
        headers = {h["name"].lower(): h["value"] for h in m.get("payload", {}).get("headers", [])}
        if AGENT_HEADER.lower() in headers:
            continue
        at = datetime.fromtimestamp(int(m.get("internalDate", "0")) / 1000, timezone.utc).isoformat()
        out.append({"id": m["id"], "rfc_id": headers.get("message-id"), "at": at,
                    "text": strip_quoted(_plain(m.get("payload", {})))[:4000]})
    return out


def calendar_busy(start: datetime, end: datetime, service=None) -> list[tuple[str, str]]:
    svc = service or _svc("calendar", "v3")
    r = svc.freebusy().query(body={"timeMin": start.isoformat(), "timeMax": end.isoformat(),
                                   "items": [{"id": "primary"}]}).execute()
    return [(b["start"], b["end"]) for b in r["calendars"]["primary"].get("busy", [])]


def calendar_book(start: datetime, end: datetime, summary: str, description: str, service=None) -> str:
    """Create the event on your own calendar. No attendees are added: the prospect is simulated,
    and nothing may be sent to anyone except OWN_EMAIL."""
    svc = service or _svc("calendar", "v3")
    ev = svc.events().insert(calendarId="primary", sendUpdates="none", body={
        "summary": summary, "description": description,
        "start": {"dateTime": start.isoformat()}, "end": {"dateTime": end.isoformat()}}).execute()
    return ev["id"]
