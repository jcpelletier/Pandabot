# Pandabot

<img width="1419" height="1562" alt="image" src="https://github.com/user-attachments/assets/0e748146-ba81-4926-83ef-c45dcc70a0e5" />

A Discord bot for home servers backed by Claude (Haiku). Mention it in Discord to ask questions about your server, trigger CI jobs on demand, or schedule future checks. Also posts proactive alerts automatically.

Every server-specific value lives in `.env` — feature flags let you disable entire subsystems (Jellyfin, Jenkins, disc ripping, SMART health) that aren't present on your machine. No code changes needed for most setups.

---

## Features

- **Natural language queries** — ask in plain English, Claude decides which tools to call
- **Conversation context** — remembers the last 10 messages so follow-up questions work naturally
- **Jenkins job triggering** — tell the bot to run a job; it triggers it and automatically schedules a follow-up notification when the build finishes
- **Task scheduler** — schedule any bot action for a future time, on a recurring basis, or to fire once a condition is met; backed by SQLite, no LLM cost at fire time
- **Jenkins failure notifications** — Jenkins POSTs to a local webhook; bot formats and forwards to Discord
- **Disk space alert** — polls every 4h, posts to Discord if a configured path exceeds threshold
- **Service watchdog** — polls every 10min, alerts when watched services go down or recover
- **App Insights telemetry** — queries, tool calls, scheduled task firings, and alerts logged to Azure Application Insights (optional)
- **Startup announcement** — posts version number to Discord on every restart

---

## Tools

| Tool | What it does |
|---|---|
| `get_log_tail` | Last N lines of configured service logs (file or Docker) |
| `get_service_status` | systemd or Docker container status |
| `get_performance_history` | PCP/pmlogger time-series (up to 168h / 1 week) for cpu, memory, disk, network |
| `get_jenkins_build_status` | Latest build result for one or all jobs |
| `get_jenkins_build_history` | Last N builds or all builds in a time window with pass/fail summary |
| `get_jenkins_build_log` | Console log for a specific build |
| `trigger_jenkins_job` | Trigger a job immediately; returns estimated duration and scheduling hints |
| `query_storage` | Disk usage, per-folder breakdown, or top-N largest files |
| `query_system_health` | CPU/RAM/GPU stats, failed systemd units, available updates, top processes, SMART drive health |
| `query_network` | Tailscale peer status, external IP, listening TCP ports |
| `query_jellyfin` | Library stats, recently added, weekly additions, active streams, watch history |
| `query_ripping` | Staging area contents, subtitle sidecar coverage, recent rip history (App Insights) |
| `query_media_library` | File metadata (codec, bitrate, duration, resolution) and directory listings via ffprobe |
| `manage_schedule` | Create, list, or cancel scheduled tasks (one-shot, condition-check, or recurring) |

Tools are only exposed to Claude when their feature flag is enabled — disabled tools are invisible to Claude but still callable by the scheduler (safe for saved tasks).

---

## Adapting for your setup

### What you can configure without touching code

Everything in the table below is controlled by `.env`. The defaults match the original panda server; change only what differs on your machine.

| What you want to change | Env var(s) |
|---|---|
| Disable Jellyfin (not installed) | `ENABLE_JELLYFIN=false` |
| Disable Jenkins | `ENABLE_JENKINS=false` |
| Disable disc ripping tools | `ENABLE_RIPPING=false` |
| Disable SMART drive health | `ENABLE_SMART=false` |
| Different Docker containers to monitor | `DOCKER_LOG_CONTAINERS=myapp,nginx` |
| Different systemd services to monitor | `SYSTEMD_SERVICES=myservice,ssh` |
| Different file logs to tail | `FILE_LOGS=myapp:/var/log/myapp.log` |
| Different Jenkins job names | `JENKINS_JOBS=Build,Test,Deploy` |
| Different SMART devices | `SMART_DEVICES=/dev/sda:My SSD,/dev/sdb:My HDD` |
| Different media library paths | `MEDIA_PATH=`, `STAGING_PATH=` |
| Bot name and emoji | `BOT_NAME=`, `BOT_EMOJI=` |
| Server timezone | `TZ_NAME=` |
| Hardware description in system prompt | `HARDWARE_DESCRIPTION=` |
| Custom server description in system prompt | `SERVER_DESCRIPTION=` (leave blank to auto-build from flags) |
| Disk alert path and threshold | `DISK_ALERT_PATH=`, `DISK_ALERT_THRESHOLD_PCT=` |
| Services the watchdog monitors | `WATCHDOG_SERVICES=` |

### What requires code changes

These are genuine code-level dependencies that `.env` can't paper over:

| Scenario | What to change |
|---|---|
| Replace Jellyfin with Plex or another media server | Rewrite `query_jellyfin()` in `tools.py` against the new API |
| Replace Jenkins with GitHub Actions, Gitea, etc. | Rewrite the Jenkins tool functions in `tools.py` |
| Add a new proactive alert type | Add a new `async def task_*()` in `bot.py` and wire it in `main()` |
| Add a new tool entirely | Add the function to `tools.py` and register it in `_build_tool_definitions()` and `execute_tool()` |
| Change the ripping pipeline (not MakeMKV/abcde) | Update `query_ripping()` in `tools.py` to match your pipeline's structure |

For most home server setups — with some combination of Docker services, systemd units, a media library, and a CI system — everything is configurable without code.

---

## Quick start

### 1. Create the Discord bot

1. [Discord Developer Portal](https://discord.com/developers/applications) → **New Application**
2. **Bot** → **Add Bot** → enable **Message Content Intent**
3. Copy the **Token** → this becomes `DISCORD_TOKEN` in `.env`
4. **OAuth2 → URL Generator** → scopes: `bot` → permissions: `Send Messages`, `Read Message History`, `View Channels` → invite to your server
5. In Discord: **Settings → Advanced → Developer Mode** → right-click your channel → **Copy Channel ID** → `DISCORD_CHANNEL_ID`

### 2. Get an Anthropic API key

Create a key at [console.anthropic.com](https://console.anthropic.com) → `ANTHROPIC_API_KEY`

### 3. Install on the server

```bash
ssh yourserver
curl -fsSL https://raw.githubusercontent.com/jcpelletier/Pandabot/main/install.sh | sudo bash
```

This creates the `discord-bot` system user, sets up a Python venv at `/opt/discord-bot/`, and installs the systemd unit.

### 4. Configure `.env`

```bash
sudo nano /opt/discord-bot/.env
```

**Always required:**
```bash
DISCORD_TOKEN=...
DISCORD_CHANNEL_ID=...
ANTHROPIC_API_KEY=...
WEBHOOK_SECRET=$(openssl rand -hex 24)   # paste the output
```

**Disable features you don't have** (all default to `true`):
```bash
ENABLE_JELLYFIN=false   # no Jellyfin
ENABLE_JENKINS=false    # no Jenkins
ENABLE_RIPPING=false    # no disc ripping pipeline
ENABLE_SMART=false      # don't want SMART drive checks
```

**Adjust whitelists** to match your services (shown with their defaults):
```bash
DOCKER_LOG_CONTAINERS=jellyfin,jenkins
SYSTEMD_SERVICES=sunshine,tailscaled,cockpit,ssh
JENKINS_JOBS=Login_Test,Process_Movies,Nightly_Convert
SMART_DEVICES=/dev/sda:Boot SSD,/dev/sdb:Media HDD
```

**Name the bot:**
```bash
BOT_NAME=Panda
BOT_EMOJI=🐼
TZ_NAME=America/New_York (Eastern Time, EDT/EST)
HARDWARE_DESCRIPTION=NVIDIA GTX 970 (4 GB VRAM), 2 TB HDD at /mnt/media
```

Optional: fill in `JELLYFIN_URL` / `JELLYFIN_API_KEY`, `JENKINS_URL` / `JENKINS_USER` / `JENKINS_TOKEN`, and Azure App Insights vars if you use those features.

### 5. Start the bot

```bash
sudo systemctl enable --now discord-bot
sudo journalctl -fu discord-bot
```

You should see:
```
Webhook server listening on 127.0.0.1:8765/notify
Logged in as Panda#1234 (id=...)
```

And a startup message in Discord.

### 6. Optional: wire Jenkins notifications

See [SETUP.md §7](SETUP.md#7-wire-jenkins-notifications) for how to POST build results to the bot's webhook from Jenkins pipelines or freestyle jobs.

### 7. Set up a weekly digest (optional)

The bot doesn't run a weekly digest automatically — create one via Discord on first use:

```
@Panda set up a recurring digest every Sunday at 9am. Include CPU and memory 
for the past week, Jellyfin additions this week, Jenkins job health for the 
past 7 days, disk usage, and any failed systemd units or pending updates.
```

---

## Example queries

**Works on any setup:**
```
@Panda how much disk space is left?
@Panda is the server under heavy load?
@Panda show CPU usage over the last 6 hours
@Panda are there any failed systemd units?
@Panda are there any pending updates?
@Panda what's listening on the network?
@Panda remind me at 9am tomorrow how much disk space is left
@Panda check disk space every day at 8am
```

**Requires `ENABLE_JENKINS=true`:**
```
@Panda did last night's conversion job succeed?
@Panda why did the last build fail?
@Panda show me the last 5 builds of Login_Test
@Panda run the movie processing job
```

**Requires `ENABLE_JELLYFIN=true`:**
```
@Panda is anyone watching right now?
@Panda how many movies are in the library?
@Panda what was added this week?
```

**Requires `ENABLE_RIPPING=true`:**
```
@Panda why did the last rip fail?
@Panda is anything sitting in the staging area?
@Panda which movies are missing subtitles?
```

**Requires `ENABLE_SMART=true`:**
```
@Panda check drive health
@Panda how old is the media drive?
```

**Media library (`MEDIA_PATH`):**
```
@Panda how big is Blade Runner 2049 and what codec is it?
@Panda list all movies added this week
@Panda why wasn't this file re-encoded?
```

---

## Deployment

After a fresh clone, activate the version-bump pre-commit hook once:

```bash
git config core.hooksPath .githooks
```

The `VERSION` file auto-increments on every commit. The bot announces its version to Discord on every restart.

To deploy changes to the server:

```bash
# Push from local repo
git push

# Pull and restart on the server
ssh yourserver "sudo git -C /opt/discord-bot pull origin main && sudo systemctl restart discord-bot"
```

---

## Requirements

**System** (handled by `install.sh`):
- Ubuntu Server 24.04
- Python 3.11+
- `ffmpeg` — for `query_media_library` (`apt install ffmpeg`)

**Optional system packages** (used if present, gracefully skipped if missing):
- `pcp` + `cockpit-pcp` — for `get_performance_history` historical metrics
- `nvidia-smi` (NVIDIA driver) — for GPU stats in `query_system_health`
- `smartmontools` — for SMART health checks (`apt install smartmontools`; also requires `setcap` — handled by `install.sh`)

**Credentials:**
- Discord bot token with **Message Content Intent** enabled
- Anthropic API key
- Jenkins API token (when `ENABLE_JENKINS=true`) — see SETUP.md §3
- Jellyfin API key (when `ENABLE_JELLYFIN=true`)
- Azure App Registration with Monitoring Reader on your App Insights resource — for `query_ripping: recent_rips` and telemetry (optional; see SETUP.md §2)
