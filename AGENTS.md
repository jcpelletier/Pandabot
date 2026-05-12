# AGENTS.md — Pandabot (discord-bot)

AI coding agent instructions for this repository.

## What this repo is

The main Discord bot for the panda home server (`jcpelletier/Pandabot`). A Python
discord.py bot that answers questions about server status via Claude tool-use, handles
Jenkins failure notifications, and runs scheduled tasks.

## Architecture

| File | Role |
|---|---|
| `bot.py` | Discord client, asyncio event loop, webhook server, scheduler poller |
| `tools.py` | All tool implementations and Claude tool schema definitions |
| `pandabot_core/` | **Not in this repo** — shared library on `PYTHONPATH=/opt/pandabot-core` |

**Dependency rule:** `pandabot_core` provides the LLM loop, Discord helpers, scheduler,
telemetry, and identity. Do NOT re-implement anything that `pandabot_core` already
provides. Import it; do not copy it.

## Running tests

```bash
python -m pytest tests/ -v
```

Tests must pass before submitting any PR. The pre-commit hook also runs them.

## Critical coding rules

**Typing indicator** — always use `keep_typing()` from pandabot_core, never `async with
channel.typing()` or `await channel.typing()`. Those patterns leak tasks or crash on
Discord errors.

**Tool surface** — tools are registered in `TOOL_DEFINITIONS` (list of Anthropic tool
schemas) and dispatched in `execute_tool()`. Both must be updated together when adding
or removing a tool.

**Feature flags** — tools can be gated behind env vars (`ENABLE_JELLYFIN`, `ENABLE_JENKINS`,
etc.). Follow the existing pattern in `tools.py` when adding a flagged tool.

**No arbitrary shell** — tools run specific, whitelisted commands. Do not add subprocess
calls that accept arbitrary user input.

## Files never to modify

- `.env` — credentials, not in repo
- `scheduler.db` — runtime state, not in repo
- `VERSION` — auto-incremented by pre-commit hook; never hand-edit
- `CHANGELOG.md` — maintained manually by the developer
- `tests/conftest.py` — test infrastructure; changes require careful review

## Branch and deployment

- Work happens on the `staging` branch only. **Never commit to `main`.**
- PRs should target `staging`.
- `main` is only updated by `promote-to-prod.sh` after QA validation.
- The pre-commit hook auto-increments `VERSION`; activate it once after clone:
  `git config core.hooksPath .githooks`

## Key env vars (set in `/opt/discord-bot/.env` on server)

`DISCORD_TOKEN`, `ANTHROPIC_API_KEY`, `DISCORD_CHANNEL_ID`, `JENKINS_URL`,
`JENKINS_USER`, `JENKINS_TOKEN`, `WEBHOOK_SECRET`

Full context and deployment instructions: see `CLAUDE.md` in this repo.
