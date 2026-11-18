"""Run Prospect Desk locally:

    .venv/bin/python -m src.webapp            # then open http://127.0.0.1:8765

The same handler is deployed on Vercel (api/index.py). Locally it answers on 127.0.0.1 only and uses
SQLite unless DATABASE_URL is set.
"""
from __future__ import annotations

from http.server import ThreadingHTTPServer

from .config import load_env
from .server import Handler, account_summary, match_seller  # noqa: F401  (re-exported for tests)

HOST, PORT = "127.0.0.1", 8765


class LocalHandler(Handler):
    local_only = True


def main():
    load_env()
    srv = ThreadingHTTPServer((HOST, PORT), LocalHandler)
    print(f"Prospect Desk running at http://{HOST}:{PORT}  (Ctrl+C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
