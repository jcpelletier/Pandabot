# Pandabot

<img width="1419" height="1562" alt="image" src="https://github.com/user-attachments/assets/0e748146-ba81-4926-83ef-c45dcc70a0e5" />

A Discord bot for the Panda home server backed by Claude (Opus for queries, Haiku for scheduled tasks). Mention it in Discord to ask questions about the server, trigger Jenkins jobs on demand, or schedule future checks. Also posts proactive alerts and a weekly digest automatically.

---

## Features

- **Natural language queries** — ask in plain English, Claude decides which tools to call
- **Conversation context** — remembers the last 10 messages so follow-up questions work naturally
- **Jenkins job triggering** — tell the bot to run a job; it triggers it and automatically schedules a follow-up notification when the build finishes
- **Task scheduler** — schedule any bot action for a future time, on a recurring basis, or to fire once a condition is met (e.g. "tell me when that build finishes"); backed by SQLite, no LLM cost at fire time
- **Jenkins failure notifications** — Jenkins POSTs to a local webhook; bot formats and forwards to Discord
- **Process_Movies alerts** — notified when Sort_Rips.py can't match a ripped file to TMDB
- **Disk space alert** — polls every 4h, posts to Discord if `/mnt/media` exceeds threshold (default 85%)
- **Service watchdog** — polls every 10min, alerts when Jellyfin or Sunshine goes down or recovers
- **Weekly digest** — every Sunday 9am Eastern: performance summary, Jellyfin additions, Jenkins health, system notes
- **App Insights telemetry** — bot queries, tool calls, scheduled task firings, and alerts are all logged to Azure Application Insights
- **Startup announcement** — posts version number to Discord on every restart

## Tools

| Tool | What it does |
|---|---|
| `get_disk_usage` | Free/used space on `/` and `/mnt/media` |
| `get_log_tail` | Last N lines of rip-video, rip-cd, jellyfin, or jenkins logs |
| `get_service_status` | systemd or Docker container status |
| `get_system_stats` | CPU load, RAM, NVIDIA GPU temp/VRAM/utilisation |
| `get_performance_history` | PCP/pmlogger time-series (up to 24h) for cpu, memory, disk, network |
| `get_jenkins_build_status` | Latest build result for one or all jobs |
| `get_jenkins_build_history` | Last N builds with timing and result |
| `get_jenkins_build_log` | Console log for a specific build |
| `trigger_jenkins_job` | Trigger a job immediately; returns estimated duration and scheduling hints |
| `query_jellyfin` | Library stats, recently added, active streams, watch history |
| `query_ripping` | Staging area contents, subtitle sidecar coverage, recent rip history (App Insights) |
| `query_media_library` | File metadata (codec, bitrate, duration, resolution) and directory listings via ffprobe |
| `manage_schedule` | Create, list, or cancel scheduled tasks (one-shot, condition-check, or recurring) |

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

After editing `.env`, run `install.sh` (or the snippet below) to publish `webhook.secret` and `webhook.port` — world-readable files that Jenkins uses instead of reading `.env` directly:

```bash
sudo bash /opt/discord-bot/install.sh
```

Or manually:

```bash
sudo bash -c '
  secret=$(grep "^WEBHOOK_SECRET=" /opt/discord-bot/.env | cut -d= -f2-)
  port=$(grep "^WEBHOOK_PORT=" /opt/discord-bot/.env | cut -d= -f2-)
  [ -n "$secret" ] && echo "$secret" > /opt/discord-bot/webhook.secret
  [ -n "$port"   ] && echo "$port"   > /opt/discord-bot/webhook.port
  chmod 644 /opt/discord-bot/webhook.secret /opt/discord-bot/webhook.port
'
```

## Requirements

**System** (handled by `install.sh`):
- Ubuntu Server 24.04
- Python 3.11+
- `ffmpeg` — for `query_media_library` file inspection (`apt install ffmpeg`)

**Optional system packages** (used if present, gracefully skipped if not):
- `pcp` + `cockpit-pcp` — for `get_performance_history` historical metrics
- `nvidia-smi` (NVIDIA driver) — for GPU stats in `get_system_stats`

**Credentials** (all go in `.env`):
- Discord bot token with **Message Content Intent** enabled
- Anthropic API key (Opus for interactive queries, Haiku for scheduled task firing)
- Jenkins API token
- Azure App Registration with Monitoring Reader on your App Insights resource — for `query_ripping: recent_rips` and bot telemetry (see SETUP.md §2)
