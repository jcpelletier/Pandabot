#!/bin/bash
# notify-discord.sh — called from Jenkins post-build steps to send
# a Discord notification via the bot's local webhook.
#
# Usage (from Jenkins "Execute shell" step):
#   /opt/discord-bot/notify-discord.sh "$JOB_NAME" "$BUILD_RESULT" \
#       "$BUILD_NUMBER" "$BUILD_URL" "optional extra message"
#
# Or source the WEBHOOK_SECRET from the .env file automatically.

JOB_NAME="${1:-Unknown}"
STATUS="${2:-UNKNOWN}"
BUILD_NUMBER="${3:-0}"
BUILD_URL="${4:-}"
MESSAGE="${5:-}"

# Load the secret from the bot's .env
WEBHOOK_SECRET=""
if [ -f /opt/discord-bot/.env ]; then
  WEBHOOK_SECRET=$(grep '^WEBHOOK_SECRET=' /opt/discord-bot/.env | cut -d= -f2-)
fi

WEBHOOK_PORT=$(grep '^WEBHOOK_PORT=' /opt/discord-bot/.env 2>/dev/null | cut -d= -f2-)
WEBHOOK_PORT="${WEBHOOK_PORT:-8765}"

curl -sf \
  -X POST \
  -H "Content-Type: application/json" \
  -d "$(jq -n \
    --arg secret       "$WEBHOOK_SECRET" \
    --arg job_name     "$JOB_NAME" \
    --arg status       "$STATUS" \
    --argjson build_number "$BUILD_NUMBER" \
    --arg build_url    "$BUILD_URL" \
    --arg message      "$MESSAGE" \
    '{secret: $secret, job_name: $job_name, status: $status,
      build_number: $build_number, build_url: $build_url,
      message: $message}')" \
  "http://127.0.0.1:${WEBHOOK_PORT}/notify" \
|| echo "WARNING: Discord notification failed (bot may be down)"
