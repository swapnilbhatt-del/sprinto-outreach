#!/usr/bin/env bash
# Copies the app's secrets from .env (and the Google login file) into the linked Vercel project's
# Production environment. Values are piped to the Vercel CLI and never printed.
#
#   bash scripts/push_env.sh
#
# Needs in .env: GEMINI_API_KEY, HUNTER_API_KEY, OWN_EMAIL, DATABASE_URL, APP_PASSWORD
# Optional in .env: HUBSPOT_TOKEN (update_crm escalates to you without it)
# Needs file:      db/google_token.json  (from: .venv/bin/python -m src.google_setup)
# Generates once (and saves to .env): CRON_SECRET, APP_SECRET
set -euo pipefail
cd "$(dirname "$0")/.."

[ -f .env ] || { echo "No .env here"; exit 1; }
[ -d .vercel ] || { echo "Project not linked: run 'vercel link' first"; exit 1; }

getv() { grep -E "^$1=" .env | tail -1 | cut -d= -f2- | sed -e 's/^"//' -e 's/"$//' ; }

for k in CRON_SECRET APP_SECRET APP_PASSWORD; do
  if [ -z "$(getv "$k")" ]; then
    if [ "$k" = APP_PASSWORD ]; then v="$(openssl rand -base64 12 | tr -d '/+=')"; else v="$(openssl rand -hex 24)"; fi
    printf '\n%s=%s\n' "$k" "$v" >> .env
    echo "generated $k (saved to .env)"
  fi
done

push() {  # name value
  local name="$1" value="$2"
  if [ -z "$value" ]; then echo "skip   $name (not set)"; return; fi
  vercel env rm "$name" production --yes >/dev/null 2>&1 || true
  printf '%s' "$value" | vercel env add "$name" production >/dev/null
  echo "set    $name"
}

missing=0
for k in GEMINI_API_KEY HUNTER_API_KEY OWN_EMAIL DATABASE_URL; do
  [ -n "$(getv "$k")" ] || { echo "MISSING $k in .env"; missing=1; }
done
[ -f db/google_token.json ] || { echo "MISSING db/google_token.json (run: .venv/bin/python -m src.google_setup)"; missing=1; }
[ "$missing" = 0 ] || exit 1

for k in GEMINI_API_KEY HUNTER_API_KEY OWN_EMAIL DATABASE_URL APP_PASSWORD HUBSPOT_TOKEN CRON_SECRET APP_SECRET; do
  push "$k" "$(getv "$k")"
done
push GOOGLE_TOKEN_JSON "$(cat db/google_token.json)"
push AGENT_TIMEZONE "Asia/Kolkata"
# The scheduler (GitHub Actions) needs the same CRON_SECRET and the live URL.
if command -v gh >/dev/null 2>&1; then
  printf '%s' "$(getv CRON_SECRET)" | gh secret set CRON_SECRET >/dev/null && echo "set    GitHub secret CRON_SECRET"
  [ -n "$(getv APP_URL)" ] && printf '%s' "$(getv APP_URL)" | gh secret set APP_URL >/dev/null && echo "set    GitHub secret APP_URL"
fi
echo "Done. Redeploy for the values to take effect: vercel deploy --prod"
