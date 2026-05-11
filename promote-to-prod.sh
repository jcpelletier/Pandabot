#!/usr/bin/env bash
# Promotes the commit currently running in discord-bot-staging to production.
# Usage: ./promote-to-prod.sh [--dry-run]
set -euo pipefail

SSH="wsl ssh -i ~/.ssh/id_ed25519 genesis@192.168.1.100"
DRY_RUN=false
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=true

STAGING_COMMIT=$($SSH "sudo git -C /opt/discord-bot-staging rev-parse HEAD")
STAGING_VERSION=$($SSH "cat /opt/discord-bot-staging/VERSION 2>/dev/null || echo unknown")
PROD_COMMIT=$($SSH "sudo git -C /opt/discord-bot rev-parse HEAD")

echo "Staging : $STAGING_COMMIT (v$STAGING_VERSION)"
echo "Prod    : $PROD_COMMIT"

if [[ "$STAGING_COMMIT" == "$PROD_COMMIT" ]]; then
    echo "Already up to date — staging and production are on the same commit."
    exit 1
fi

if $DRY_RUN; then
    echo "--dry-run: no changes made."
    exit 0
fi

echo "Promoting staging → production…"
$SSH "sudo git -C /opt/discord-bot fetch origin && \
      sudo git -C /opt/discord-bot checkout $STAGING_COMMIT && \
      sudo chown discord-bot:discord-bot /opt/discord-bot/scheduler.db && \
      sudo systemctl restart discord-bot"

echo "Done. Production is now at $STAGING_COMMIT (v$STAGING_VERSION)."
