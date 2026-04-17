# Pandabot

<img width="1419" height="1562" alt="image" src="https://github.com/user-attachments/assets/0e748146-ba81-4926-83ef-c45dcc70a0e5" />

A Discord bot for the Panda home server backed by Claude Opus. Mention it in Discord to ask questions about the server — disk usage, service health, Jenkins build status, log tails, and system stats. Also listens for Jenkins failure webhooks and posts alerts to a configured channel.

---

## Features

- **Natural language queries** — ask in plain English, Claude decides which tools to call
- **Conversation context** — remembers the last 10 messages so follow-up questions work naturally
- **Read-only server tools** — disk usage, log tails, service status, Jenkins build history and logs, system stats (CPU/RAM/GPU)
- **Jenkins failure notifications** — Jenkins POSTs to a local webhook; bot formats and forwards to Discord with emoji status indicators
- **Adaptive thinking** — uses Claude's extended thinking for complex multi-step questions

## Available tools

| Tool | What it does |
|---|---|
| `get_disk_usage` | Free/used space on all mounted drives |
| `get_log_tail` | Last N lines of a named log file |
| `get_service_status` | systemd or Docker container status |
| `get_system_stats` | CPU, RAM, GPU usage |
| `get_jenkins_build_status` | Latest build result for a job |
| `get_jenkins_build_history` | Last N builds with duration and result |
| `get_jenkins_build_log` | Console log for a specific build |

## Example queries

```
@Panda how much disk space is left on the media drive?
@Panda did Nightly_Convert succeed last night?
@Panda why did the last rip fail?
@Panda is Jellyfin running?
@Panda give me a full health check
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
