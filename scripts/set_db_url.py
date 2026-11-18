"""Ask for the Supabase database password (hidden), save DATABASE_URL to .env, and test it.

    .venv/bin/python scripts/set_db_url.py

The password is typed into this prompt only; it is never printed. Special characters are encoded.
"""
import getpass
import re
import sys
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
HOST = "aws-1-ap-south-1.pooler.supabase.com"   # Supabase "Shared pooler", transaction mode
USER = "postgres.iptnyaglmlkuvdmcxghn"
PORT, DB = 6543, "postgres"


def ask_dialog() -> str:
    """A native macOS window with one hidden field, so the password never passes through a chat or log."""
    import subprocess
    script = ('display dialog "Paste your Supabase database password and click OK" '
              'default answer "" with hidden answer with title "Prospect Desk setup" '
              'buttons {"Cancel", "OK"} default button "OK"\n'
              'text returned of result')
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def main() -> int:
    pw = ask_dialog() if "--dialog" in sys.argv else getpass.getpass("Supabase database password (typing is hidden): ").strip()
    if not pw:
        print("No password entered; nothing changed.")
        return 1
    url = f"postgresql://{USER}:{quote(pw, safe='')}@{HOST}:{PORT}/{DB}"
    try:
        import psycopg
        with psycopg.connect(url, connect_timeout=10, prepare_threshold=None) as c:
            c.execute("select count(*) from trails").fetchone()
    except Exception as e:
        msg = re.sub(r"postgresql://\S+", "postgresql://***", str(e)).splitlines()[0]
        print(f"Couldn't connect: {msg}\nCheck the password (reset it on the same page if needed) and run again.")
        return 1
    env = ROOT / ".env"
    lines = [l for l in env.read_text().splitlines() if not l.startswith("DATABASE_URL=")]
    lines.append(f"DATABASE_URL={url}")
    env.write_text("\n".join(lines) + "\n")
    print("Connected to Supabase and saved DATABASE_URL to .env. You can tell Claude: done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
