# Pandabot

<img width="1419" height="1562" alt="image" src="https://github.com/user-attachments/assets/0e748146-ba81-4926-83ef-c45dcc70a0e5" />

A Discord bot for home servers backed by Claude (Haiku). Mention it in Discord to ask questions about the server, trigger Jenkins jobs on demand, or schedule future checks. Also posts proactive alerts automatically.

Designed to be portable — every panda-specific value lives in `.env`. Feature flags let you disable entire subsystems (Jellyfin, Jenkins, ripping, SMART) that aren't present on your machine.

---

## Features

- **Natural language queries** — ask in plain English, Claude decides which tools to call
- **Conversation context** — remembers the last 10 messages so follow-up questions work naturally
- **Jenkins job triggering** — tell the bot to run a job; it triggers it and automatically schedules a follow-up notification when the build finishes
- **Task scheduler** — schedule any bot action for a future time, on a recurring basis, or to fire once a condition is met (e.g. "tell me when that build finishes"); backed by SQLite, no LLM cost at fire time
- **Jenkins failure notifications** — Jenkins POSTs to a local webhook; bot formats and forwards to Discord
- **Process_Movies alerts** — notified when Sort_Rips.py can't match a ripped file to TMDB
- **Disk space alert** — polls every 4h, posts to Discord if a path exceeds threshold (default 85%)
- **Service watchdog** — polls every 10min, alerts when watched services go down or recover
- **Weekly digest** — schedule via the bot on first use (e.g. `@Panda set up a weekly digest every Sunday at 9am`)
- **App Insights telemetry** — bot queries, tool calls, scheduled task firings, and alerts logged to Azure Application Insights
- **Startup announcement** — posts version number to Discord on every restart

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

Tools are only exposed to Claude when their feature flag is enabled. Disabled tools are invisible to Claude but remain callable by the scheduler (safe for saved tasks).

## Example queries

```
@Panda how much disk space is left?
@Panda did Nightly_Convert succeed last night?
@Panda why did the last rip fail?
@Panda is anyone watching Jellyfin right now?
@Panda how many movies are in the library?
@Panda what was added to Jellyfin this week?
@Panda which movies are missing subtitles?
@Panda is anything sitting in the staging area?
@Panda show CPU usage over the last 6 hours
@Panda show me the last 3 Login_Test builds
@Panda can you test if Jellyfin login is working?
@Panda run the movie processing job
@Panda remind me at 9am tomorrow how much disk space is left
@Panda check every 30 minutes whether the staging area is clear
@Panda how big is Song of the Sea and what codec is it?
@Panda why wasn't Sonic the Hedgehog re-encoded?
@Panda list all movies added this week
@Panda set up a weekly digest every Sunday at 9am
```

---

## Setup

See [SETUP.md](SETUP.md) for full instructions covering:
1. Creating the Discord application and bot token
2. Getting a Jenkins API token
3. Running `install.sh` on the server
4. Configuring `.env`
5. Starting the systemd service
6. Wiring Jenkins post-build notifications

## Deployment

After a fresh clone, activate the version-bump pre-commit hook once:

```bash
git config core.hooksPath .githooks
```

The `VERSION` file auto-increments on every commit. The bot announces its version to Discord on every restart.

To deploy changes:

```bash
# Push changes to GitHub
git push

# Pull and restart on the server
ssh panda "sudo git -C /opt/discord-bot pull origin main && sudo systemctl restart discord-bot"
```

## Configuration

Copy `.env.example` to `.env` and fill in all values:

```bash
sudo cp /opt/discord-bot/.env.example /opt/discord-bot/.env
sudo nano /opt/discord-bot/.env
```

Generate a webhook secret with:

```bash
openssl rand -hex 24
```

### Required

| Variable | Description |
|---|---|
| `DISCORD_TOKEN` | Discord bot token |
| `DISCORD_CHANNEL_ID` | Default channel ID for notifications |
| `ANTHROPIC_API_KEY` | Claude API key |

### Feature flags

Set any of these to `false` to disable that subsystem entirely — the tools disappear from Claude's view and the system prompt adjusts automatically.

| Variable | Default | Controls |
|---|---|---|
| `ENABLE_JELLYFIN` | `true` | `query_jellyfin` tool |
| `ENABLE_JENKINS` | `true` | All Jenkins tools + triggering instructions |
| `ENABLE_RIPPING` | `true` | `query_ripping` tool + file log whitelists |
| `ENABLE_SMART` | `true` | `smart` aspect of `query_system_health` |

### Bot identity

| Variable | Default | Description |
|---|---|---|
| `BOT_NAME` | `Panda` | Name used in the system prompt and startup message |
| `BOT_EMOJI` | `🐼` | Emoji prefix on the startup announcement |
| `TZ_NAME` | `America/New_York (Eastern Time, EDT/EST)` | Timezone shown in the system prompt |
| `SERVER_DESCRIPTION` | *(empty)* | Free-form services paragraph for the system prompt. Leave blank to auto-build from feature flags. |
| `HARDWARE_DESCRIPTION` | `NVIDIA GTX 970 (4 GB VRAM), 2 TB NTFS HDD at /mnt/media` | Hardware line in the system prompt |

### Service whitelists

These control which services and logs Claude is allowed to inspect. Values are comma-separated; use `key:value` pairs for `FILE_LOGS` and `SMART_DEVICES`.

| Variable | Default | Description |
|---|---|---|
| `DOCKER_LOG_CONTAINERS` | `jellyfin,jenkins` | Docker containers for `get_log_tail` / `get_service_status` |
| `SYSTEMD_SERVICES` | `sunshine,tailscaled,cockpit,ssh` | systemd units for `get_service_status` |
| `FILE_LOGS` | `rip-video:/var/log/rip-video.log,rip-cd:/var/log/rip-cd.log` | Named file logs for `get_log_tail` |
| `JENKINS_JOBS` | `Login_Test,Process_Movies,Nightly_Convert` | Job names listed in Jenkins tool descriptions |
| `SMART_DEVICES` | `/dev/sda:SanDisk SSD PLUS (boot),/dev/sdb:Seagate ST4000DM004 (media)` | Block devices for SMART health checks |

### Alerts and proactive tasks

| Variable | Default | Description |
|---|---|---|
| `DISK_ALERT_THRESHOLD_PCT` | `85` | Alert when this filesystem exceeds this % |
| `DISK_ALERT_PATH` | `/mnt/media` | Filesystem to monitor for disk alerts |
| `WATCHDOG_SERVICES` | `jellyfin,sunshine` | Services the watchdog checks every 10 min |

## Requirements

**System** (handled by `install.sh`):
- Ubuntu Server 24.04
- Python 3.11+
- `ffmpeg` — for `query_media_library` file inspection (`apt install ffmpeg`)

**Optional system packages** (used if present, gracefully skipped if not):
- `pcp` + `cockpit-pcp` — for `get_performance_history` historical metrics
- `nvidia-smi` (NVIDIA driver) — for GPU stats in `query_system_health`
- `smartmontools` — for SMART drive health (`apt install smartmontools`)

**Credentials** (all go in `.env`):
- Discord bot token with **Message Content Intent** enabled
- Anthropic API key
- Jenkins API token (when `ENABLE_JENKINS=true`)
- Jellyfin API key (when `ENABLE_JELLYFIN=true`)
- Azure App Registration with Monitoring Reader on your App Insights resource — for `query_ripping: recent_rips` and bot telemetry (see SETUP.md §2)
