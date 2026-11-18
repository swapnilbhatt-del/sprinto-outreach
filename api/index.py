"""Vercel entry point: every path is rewritten here (vercel.json) and served by src/server.py."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.server import Handler as handler  # noqa: E402,F401  (Vercel looks for `handler`)
