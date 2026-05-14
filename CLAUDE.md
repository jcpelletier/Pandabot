# CLAUDE.md — Pandabot (discord-bot)

Main Discord bot for the panda home server.
GitHub: `jcpelletier/Pandabot`
Local path: `C:\Users\genes\Downloads\PandaMigration\discord-bot`
Server path: `/opt/discord-bot/` (systemd service `discord-bot`, user `discord-bot`)

For server-wide context (hardware, other services, Cloudflare, DNS, deployment patterns)
see the parent `CLAUDE.md` at `C:\Users\genes\Downloads\PandaMigration\CLAUDE.md`.

## Dependency on pandabot-core

This bot imports shared infrastructure from `pandabot_core`:

```python
from pandabot_core.llm import usage as llm_usage
from pandabot_core.llm import provider as llm_provider
from pandabot_core.llm.loop import run_claude_loop as _run_claude_loop_core
from pandabot_core.telemetry import ai_event, ai_trace
from pandabot_core.discord_comms import keep_typing, split_message, send_with_retry, build_history, ConfirmationManager
from pandabot_core import identity, scheduler
```

**Do not re-implement** anything that pandabot-core already provides. If you need new
shared behavior, add it to pandabot-core first, then import it here.

pandabot-core local path: `C:\Users\genes\Downloads\PandaMigration\pandabot-core`
pandabot-core server path: `/opt/pandabot-core/` (on `PYTHONPATH` via systemd unit)

## What is local (not in core)

| File | Purpose |
|---|---|
| `bot.py` | Discord client, webhook server, on_message handler, scheduler polling, TTS/STT |
| `tools.py` | All tool implementations and Claude tool schema definitions |
| `scheduler.py` | **Dead code** — superseded by `pandabot_core.scheduler`; safe to delete |
| `llm_usage.py` | **Dead code** — superseded by `pandabot_core.llm.usage`; safe to delete |
| `llm_provider.py` | **Dead code** — superseded by `pandabot_core.llm.provider`; safe to delete |

## Architecture

- `bot.py` calls `pandabot_core.llm.loop.run_claude_loop()` in a thread executor to keep the asyncio event loop free
- `tools.py` exposes `TOOL_DEFINITIONS` (list of Claude tool schemas) and `execute_tool(name, args) -> str`
- `bot.py` passes both to `run_claude_loop` — the loop handles LLM ↔ tool rounds internally
- Confirmation flow: `run_claude_loop` calls `on_confirm(channel_id, tool_name, inputs)` when a destructive preview is shown; bot stores it in `ConfirmationManager`; next "yes" message executes it directly without the LLM
- Channel history (last 15 messages): `build_history(channel, before=message)` from pandabot_core
- Typing indicator: `keep_typing(message.channel)` from pandabot_core — returns a cancellable task
- Scheduler DB: `PANDABOT_DATA_DIR=/opt/discord-bot` → uses `/opt/discord-bot/scheduler.db`

## Running tests

```bash
cd "C:\Users\genes\Downloads\PandaMigration\discord-bot"
python -m pytest tests/ -v
```

The pre-commit hook runs tests automatically. `tests/conftest.py` adds pandabot-core to
`sys.path` so tests work without installing it — the path is computed relative to this repo.

## Deploying

**Staging-first rule: every change goes to staging and passes PandaQA before production.**
Never commit directly to `main` or deploy directly to production.

Branch convention:
- `staging` — work-in-progress; deploys to `discord-bot-staging` / `#pandabot-staging`
- `main` — production; only updated by `promote-to-prod.sh`

Deployment is automatic via GitHub Actions (`.github/workflows/deploy.yml`):
- Push to `staging` → Actions deploys to `discord-bot-staging` automatically
- Push to `main` → Actions deploys to production automatically (only via `promote-to-prod.sh`)

**Do NOT run the SSH deploy command manually** — pushing to GitHub is the only deploy step needed.

Workflow:
1. Make changes on the `staging` branch
2. Push — GitHub Actions deploys to staging automatically; QA validates in `#pandabot-staging`
3. After QA passes → run `promote-to-prod.sh` (merges `staging → main`, pushes, triggers Actions deploy)

```bash
# 1. Work on staging branch
git checkout staging
# ... make changes, commit ...
git push origin staging
# GitHub Actions deploys automatically — no SSH step needed

# 2. Run QA against staging (in #pandabot-qa)
#    "run smoke tests in staging"

# 3. After QA passes — promote to production
cd "C:\Users\genes\Downloads\PandaMigration\discord-bot"
./promote-to-prod.sh           # dry-run first: ./promote-to-prod.sh --dry-run
# GitHub Actions deploys production automatically on push to main
```

If you also changed pandabot-core, deploy that first (see pandabot-core CLAUDE.md).

## Systemd unit env vars added for pandabot-core

```
Environment=PYTHONPATH=/opt/pandabot-core
Environment=PANDABOT_DATA_DIR=/opt/discord-bot
```

These are set directly in `/etc/systemd/system/discord-bot.service` (not in `.env`).
`PANDABOT_DATA_DIR` points the core scheduler/usage DB to the existing `/opt/discord-bot/scheduler.db`.
