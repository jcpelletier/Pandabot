# Panda — Feature Roadmap

This document tracks planned tools and capabilities for Panda.

## Model

**`claude-haiku-4-5`** — fast and cheap for a home server assistant. Haiku handles
tool use reliably up to ~12 tools. The consolidated tool design below stays within
that limit regardless of how many features are added.

---

## Tool surface design

Haiku's tool selection degrades noticeably above ~12 tools. The strategy is
**one tool per domain, with a `type` parameter** to select the specific query
within that domain. This keeps the total tool count flat as new capabilities
are added.

### Current tools (8) — keep as-is

| Tool | Notes |
|---|---|
| `get_log_tail` | Already narrow, stays separate |
| `get_service_status` | Already narrow, stays separate |
| `get_performance_history` | Already narrow, stays separate |
| `get_jenkins_build_status` | |
| `get_jenkins_build_history` | |
| `get_jenkins_build_log` | |
| `get_disk_usage` | → fold into `query_storage` when that tool is built |
| `get_system_stats` | → fold into `query_system_health` when that tool is built |

### Target tool list (10 total once roadmap is complete)

| # | Tool | Replaces / covers |
|---|---|---|
| 1 | `get_log_tail` | unchanged |
| 2 | `get_service_status` | unchanged |
| 3 | `get_performance_history` | unchanged |
| 4 | `query_jenkins(type, job_name, ...)` | status + history + log merged |
| 5 | `query_storage(type)` | disk_usage + media breakdown + largest files |
| 6 | `query_system_health(aspect)` | system_stats + temps + SMART + failed + updates + top procs |
| 7 | `query_jellyfin(type)` | library stats + recent + streams + history |
| 8 | `query_ripping(type)` | staging + queue + recent rips + subtitle status |
| 9 | `query_network(type)` | Tailscale + external IP + listening ports |
| 10 | `take_action(action, ...)` | all write operations behind one confirmed tool |

> When adding a tool from this list, migrate the relevant existing tools into
> the new consolidated one and remove the old entries from `TOOL_DEFINITIONS`
> and `execute_tool()`.

---

## Planned capabilities

### 5. `query_storage(type)`

Consolidates `get_disk_usage`. Add `JELLYFIN_URL`/`JELLYFIN_API_KEY` to `.env` first if
you want the Jellyfin-aware breakdown.

| type | What it returns |
|---|---|
| `usage` | Current `get_disk_usage` output (/ and /mnt/media) — **replaces existing tool** |
| `breakdown` | `du -sh` per top-level folder under `/mnt/media` (Movies, Shows, Music, Video staging) |
| `largest` | Top N largest files under a given path (whitelist: /mnt/media only) |

**Server deps:** none.

---

### 6. `query_system_health(aspect)`

Consolidates `get_system_stats`. Remaining aspects need server packages first.

| aspect | What it returns | Server dep |
|---|---|---|
| `stats` | Current `get_system_stats` output (load, mem, GPU) — **replaces existing tool** | none |
| `temperatures` | CPU thermal zone readings + GPU temp (already in stats) | `sudo apt install lm-sensors && sudo sensors-detect --auto` |
| `smart` | SMART summary for `/dev/sda` — health, reallocated sectors, power-on hours | `sudo apt install smartmontools` + sudoers entry (see below) |
| `failed` | `systemctl list-units --state=failed` | none |
| `updates` | `apt list --upgradable` count + package names | none |
| `processes` | Top 10 procs by CPU or memory (`ps aux` sorted) | none |

**Sudoers entry needed for SMART:**
```
# /etc/sudoers.d/discord-bot
discord-bot ALL=(ALL) NOPASSWD: /usr/sbin/smartctl -H -A *
```

---

### 7. `query_jellyfin(type)`

**New `.env` vars:** `JELLYFIN_URL=http://localhost:8096`, `JELLYFIN_API_KEY=...`
Generate the key in Jellyfin → Dashboard → API Keys.

| type | API endpoint | What it returns |
|---|---|---|
| `stats` | `GET /Items/Counts` | Total movies, shows, episodes, music albums |
| `recent` | `GET /Items?SortBy=DateCreated&SortOrder=Descending&Limit=10` | Recently added titles |
| `streams` | `GET /Sessions?ActiveWithinSeconds=30` | Active playback — user, title, DirectPlay vs Transcode, NVENC in use |
| `history` | `GET /Users/{id}/Items?Filters=IsPlayed&SortBy=DatePlayed` | Recently watched |

**High value:** `streams` tells you if NVENC is in use before triggering Nightly_Convert.
**No server deps** beyond the API key.

---

### 8. `query_ripping(type)`

| type | What it returns | Notes |
|---|---|---|
| `staging` | Files/folders in `/mnt/media/Video` with size and age | Answers "anything waiting to be processed?" |
| `queue` | Count + size of unconverted MKVs in `/mnt/media/Media` | Uses ffprobe to check codec; can be slow on large libraries — sample or limit depth |
| `recent_rips` | Last N `RipCompleted` events from App Insights | Needs `APPINSIGHTS_APP_ID` + `APPINSIGHTS_API_KEY` (read-only key from App Insights → API Access) |
| `subtitles` | Files missing sidecar `.srt`/`.sup`/`.sub` in Movies or Shows | Walk directory, check for matching sidecar filenames |

**New `.env` vars for `recent_rips`:** `APPINSIGHTS_APP_ID`, `APPINSIGHTS_API_KEY`

---

### 9. `query_network(type)`

| type | What it returns | Notes |
|---|---|---|
| `tailscale` | Peer list, online status, IPs | `tailscale status --json` |
| `external_ip` | Current public IP | HTTP GET `https://api.ipify.org` |
| `ports` | Non-loopback listening ports + process names | `ss -tlnp` |

**No server deps.**

---

### 4. `query_jenkins(type, job_name, build_number, lines)`

Merges the three existing Jenkins tools into one. Claude uses the `type` param
to pick the right query. Keeps all current functionality.

| type | Equivalent current tool |
|---|---|
| `status` | `get_jenkins_build_status` |
| `history` | `get_jenkins_build_history` |
| `log` | `get_jenkins_build_log` |

**Migration:** remove `get_jenkins_build_status`, `get_jenkins_build_history`,
`get_jenkins_build_log` from `TOOL_DEFINITIONS` and `execute_tool()` when adding this.

---

### 10. `take_action(action, target, confirm)`

> **Implement last.** Add a confirmation guard before any write tool is executed:
> Claude must state the action and the user must reply "yes" or "confirm" before
> the call goes through. Implement as a two-message flow in `_run_claude_loop`.

| action | What it does | Server dep |
|---|---|---|
| `restart_service` | `sudo systemctl restart {target}` (whitelist only) | sudoers entry |
| `trigger_jenkins` | POST to Jenkins build API with crumb | none (uses existing creds) |
| `jellyfin_scan` | `POST /Library/Refresh` | none (uses Jellyfin API key) |
| `eject_drive` | `eject /dev/sr0` | sudoers or device permission |

**Sudoers for restart + eject:**
```
discord-bot ALL=(ALL) NOPASSWD: /bin/systemctl restart jellyfin
discord-bot ALL=(ALL) NOPASSWD: /bin/systemctl restart sunshine
discord-bot ALL=(ALL) NOPASSWD: /usr/bin/eject /dev/sr0
```

---

## Proactive / scheduled notifications

These are background `asyncio` tasks in `bot.py`, not tools. They post to Discord
automatically without a user prompt.

| Feature | Trigger | New `.env` var |
|---|---|---|
| **Disk space alert** | Poll every 4h, alert if `/mnt/media` > threshold | `DISK_ALERT_THRESHOLD_PCT=85` |
| **Service watchdog** | Poll every 10 min, alert on Jellyfin/Sunshine going down | none |
| **Temperature alert** | Poll every 15 min, alert if CPU/GPU exceed threshold | `TEMP_ALERT_CPU_C=80`, `TEMP_ALERT_GPU_C=85` |
| **Morning digest** | Fire at configured hour, post overnight summary | `MORNING_DIGEST_HOUR=8` |

Morning digest content: overnight Jenkins results, disk usage, failed services,
GPU temp. Reuses existing tool functions — no new server calls needed.

**Implement disk alert and watchdog first** (simplest polling loops),
then morning digest, then temperature alert (needs `query_system_health` → `temperatures` first).

---

## Suggested implementation order

1. `query_storage` — no deps, removes an existing tool, immediate value
2. `query_system_health` (stats + failed + updates first, then add temps/SMART after apt installs)
3. `query_jellyfin` — needs API key, high value (`streams` especially)
4. `query_network` — no deps, straightforward
5. Disk alert + service watchdog (proactive, no new tools needed)
6. `query_ripping` — needs App Insights read key for `recent_rips`
7. `query_jenkins` — consolidation of existing tools, low urgency
8. Morning digest
9. `take_action` — implement last, needs confirmation flow first
