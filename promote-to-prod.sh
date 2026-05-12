#!/usr/bin/env bash
# Promotes the staging branch to production (main) and deploys.
# Usage: ./promote-to-prod.sh [--dry-run]
set -euo pipefail

SSH="wsl ssh -i ~/.ssh/id_ed25519 genesis@192.168.1.100"
DRY_RUN=false
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=true

git fetch origin

STAGING_COMMIT=$(git rev-parse origin/staging)
MAIN_COMMIT=$(git rev-parse origin/main)
STAGING_VERSION=$(git show origin/staging:VERSION 2>/dev/null || echo "unknown")

echo "Staging : $STAGING_COMMIT (v$STAGING_VERSION)"
echo "Main    : $MAIN_COMMIT"

if [[ "$STAGING_COMMIT" == "$MAIN_COMMIT" ]]; then
    echo "Already up to date — staging and main are on the same commit."
    exit 1
fi

if $DRY_RUN; then
    echo "--dry-run: no changes made."
    exit 0
fi

echo "Merging staging → main…"
PREV_BRANCH=$(git symbolic-ref --short HEAD 2>/dev/null || echo "detached")
git checkout main
git merge --ff-only origin/staging
git push origin main
[[ "$PREV_BRANCH" != "detached" ]] && git checkout "$PREV_BRANCH"

echo "Deploying to production…"
$SSH "sudo git -C /opt/discord-bot pull origin main && \
      sudo chown discord-bot:discord-bot /opt/discord-bot/scheduler.db && \
      sudo systemctl restart discord-bot"

echo "Done. Production is now at $STAGING_COMMIT (v$STAGING_VERSION)."
