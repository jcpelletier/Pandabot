#!/bin/bash
# notify-discord.sh — called from Jenkins post-build steps to send
# a Discord notification via the bot's local webhook.
#
# Usage (from Jenkins "Execute shell" step):
#   /opt/discord-bot/notify-discord.sh "$JOB_NAME" "$BUILD_RESULT" \
#       "$BUILD_NUMBER" "$BUILD_URL" "optional extra message"
#
# The webhook secret is read from /opt/discord-bot/webhook.secret (644),
# a separate file that Jenkins can read without exposing .env API keys.

JOB_NAME="${1:-Unknown}"
STATUS="${2:-UNKNOWN}"
BUILD_NUMBER="${3:-0}"
BUILD_URL="${4:-}"
MESSAGE="${5:-}"

# Read secret from dedicated world-readable file (not .env which contains API keys)
WEBHOOK_SECRET=""
if [ -f /opt/discord-bot/webhook.secret ]; then
  WEBHOOK_SECRET=$(cat /opt/discord-bot/webhook.secret | tr -d '[:space:]')
fi

WEBHOOK_PORT=8765
if [ -f /opt/discord-bot/webhook.port ]; then
  WEBHOOK_PORT=$(cat /opt/discord-bot/webhook.port | tr -d '[:space:]')
fi

# Build JSON with Python (avoids jq dependency)
PAYLOAD=$(python3 - "$WEBHOOK_SECRET" "$JOB_NAME" "$STATUS" "$BUILD_NUMBER" "$BUILD_URL" "$MESSAGE" <<'PYEOF'
import json, sys
_, secret, job, status, build_num, url, msg = sys.argv
print(json.dumps({
    "secret":       secret,
    "job_name":     job,
    "status":       status,
    "build_number": int(build_num) if build_num.isdigit() else 0,
    "build_url":    url,
    "message":      msg,
}))
PYEOF
)

# Use host-gateway when running inside Docker (Jenkins container);
# falls back to 127.0.0.1 when running directly on the host.
WEBHOOK_HOST="127.0.0.1"
if getent hosts host-gateway >/dev/null 2>&1; then
  WEBHOOK_HOST="host-gateway"
fi

curl -sf \
  -X POST \
  -H "Content-Type: application/json" \
  -d "$PAYLOAD" \
  "http://${WEBHOOK_HOST}:${WEBHOOK_PORT}/notify" \
|| echo "WARNING: Discord notification failed (bot may be down)"
