# Pandabot

<img width="1419" height="1562" alt="image" src="https://github.com/user-attachments/assets/0e748146-ba81-4926-83ef-c45dcc70a0e5" />

A Discord bot for the Panda home server backed by Claude Haiku. Mention it in Discord to ask questions about the server — disk usage, service health, Jenkins build status, Jellyfin library, ripping pipeline, and performance history. Also posts proactive alerts and a weekly digest automatically.

---

## Features

- **Natural language queries** — ask in plain English, Claude decides which tools to call
- **Conversation context** — remembers the last 10 messages so follow-up questions work naturally
- **Jenkins failure notifications** — Jenkins POSTs to a local webhook; bot formats and forwards to Discord
- **Process_Movies alerts** — notified when Sort_Rips.py can't match a ripped file to TMDB
- **Disk space alert** — polls every 4h, posts to Discord if `/mnt/media` exceeds threshold (default 85%)
- **Service watchdog** — polls every 10min, alerts when Jellyfin or Sunshine goes down or recovers
- **Weekly digest** — every Sunday 9am Eastern: performance summary, Jellyfin additions, Jenkins health, system notes

## Tools

| Tool | What it does |
|---|---|
| `get_disk_usage` | Free/used space on `/` and `/mnt/media` |
| `get_log_tail` | Last N lines of rip-video, rip-cd, jellyfin, or jenkins logs |
| `get_service_status` | systemd or Docker container status |
| `get_system_stats` | CPU load, RAM, NVIDIA GPU temp/VRAM/utilisation |
| `get_performance_history` | 7-day PCP/pmlogger time-series for cpu, memory, disk, network |
| `get_jenkins_build_status` | Latest build result for one or all jobs |
| `get_jenkins_build_history` | Last N builds with timing and result |
| `get_jenkins_build_log` | Console log for a specific build |
| `query_jellyfin` | Library stats, recently added, active streams, watch history |
| `query_ripping` | Staging area contents, subtitle sidecar coverage, recent rip history (App Insights) |

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

The server runs directly from this repo. To deploy changes:

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

## Requirements

- Ubuntu Server 24.04
- Python 3.11+
- Discord bot token with **Message Content Intent** enabled
- Anthropic API key
- Jenkins API token
