"""Test helper: reply in an agent thread as the prospect would (the reply goes to OWN_EMAIL only).

    .venv/bin/python scripts/simulate_reply.py wealthy.in "Interested. What times work next week?"

It sends a normal message into the thread WITHOUT the app's X-Prospect-Desk header, which is exactly
what the reply agent treats as the prospect's reply. Replying by hand from Gmail does the same.
"""
import base64
import os
import sys
from email.message import EmailMessage
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.appdata import AppData  # noqa: E402
from src.config import load_env  # noqa: E402
from src.google_client import gmail_service  # noqa: E402
from src.send_gmail import check_recipient  # noqa: E402


def main() -> int:
    load_env()
    domain, text = sys.argv[1], sys.argv[2]
    th = AppData(run_id="simulate").get_thread(domain)
    if not th:
        print(f"No agent thread for {domain}")
        return 1
    s = th["state"]
    own = os.environ["OWN_EMAIL"]
    msg = EmailMessage()
    msg["To"] = check_recipient(own, own)
    msg["Subject"] = "Re: Sprinto"
    if s.get("last_rfc_id"):
        msg["In-Reply-To"] = msg["References"] = s["last_rfc_id"]
    msg.set_content(text)
    gmail_service().users().messages().send(userId="me", body={
        "raw": base64.urlsafe_b64encode(msg.as_bytes()).decode(), "threadId": s["thread_id"]}).execute()
    print(f"Replied in {domain}'s thread as the prospect: {text!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
