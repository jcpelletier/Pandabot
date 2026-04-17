# PandaBot — Feature Roadmap

This document tracks planned tools, data sources, and capabilities for PandaBot.
Work through these in any order — each item is self-contained.

---

## 1. Jellyfin API Tools

Jellyfin exposes a full REST API on port 8096. All calls need an API key
(generate one in Jellyfin → Dashboard → API Keys).

Add `JELLYFIN_URL` and `JELLYFIN_API_KEY` to `.env` and `.env.example`.

### 1a. Library stats
**Tool:** `get_jellyfin_library_stats`
**What:** Total count of movies, shows, episodes, music albums. Total library size.
**API:** `GET /Items/Counts` + `GET /Library/VirtualFolders`
**No dependencies.**

### 1b. Recently added
**Tool:** `get_jellyfin_recent` (param: `count`, default 10)
**What:** Items added in the last N days — title, type, date added.
**API:** `GET /Items?SortBy=DateCreated&SortOrder=Descending&Limit=N`
**No dependencies.**

### 1c. Active streams
**Tool:** `get_jellyfin_streams`
**What:** Current playback sessions — user, title, play method (DirectPlay vs Transcode),
video codec, whether NVENC is in use, progress.
**API:** `GET /Sessions?ControllableByUserId=&ActiveWithinSeconds=30`
**No dependencies. High value — tells you if GPU is busy before doing anything heavy.**

### 1d. Playback history
**Tool:** `get_jellyfin_history` (params: `user`, `days`, default 7)
**What:** What has been watched recently. Most played titles.
**API:** `GET /Users/{userId}/Items?Filters=IsPlayed&SortBy=DatePlayed`
**No dependencies.**

---

## 2. Ripping Pipeline Tools

### 2a. Staging area status
**Tool:** `get_staging_status`
**What:** List of folders/files currently in `/mnt/media/Video` (unprocessed rips),
with size and age. Answers "do I have anything waiting to be sorted?"
**Implementation:** `subprocess` — `du -sh /mnt/media/Video/*` + `ls -lt`
**No dependencies.**

### 2b. Conversion queue
**Tool:** `get_conversion_queue`
**What:** Count and total size of MKV files under `/mnt/media/Media` that have not
yet been converted (i.e., are not h264/hevc encoded). Uses ffprobe to check codec.
**Implementation:** Walk directory, run `ffprobe -v quiet -print_format json
-show_streams` on each MKV, filter by video codec != h264/hevc.
**Note:** Can be slow on large libraries — consider limiting to a sample or caching.
**Dependency:** `ffprobe` (already installed for subtitle extraction).

### 2c. Recently ripped (App Insights)
**Tool:** `get_recent_rips` (param: `days`, default 7)
**What:** Query App Insights custom events for `RipCompleted` (both rip-video and
rip-cd roles) in the last N days. Returns a table of title/artist, date, size/tracks.
**Implementation:** App Insights REST Query API.
`POST https://api.applicationinsights.io/v1/apps/{appId}/query`
Add `APPINSIGHTS_APP_ID` and `APPINSIGHTS_API_KEY` to `.env`.
(App Insights → Configure → API Access → create a read-only key)
**No server-side dependencies — HTTP only.**

### 2d. Subtitle sidecar status
**Tool:** `get_subtitle_status` (param: `library`, enum: movies/shows)
**What:** Count of video files that have at least one `.srt`/`.sup`/`.sub` sidecar
vs. those that have none. Surfaces files missing subtitles.
**Implementation:** Walk `/mnt/media/Media/Movies` or `/mnt/media/Media/Shows`,
check for matching sidecar filename patterns.
**No dependencies.**

---

## 3. System Health Tools

### 3a. CPU temperature
**Tool:** `get_temperatures`
**What:** CPU and any other thermal sensor readings.
**Implementation:** Read `/sys/class/thermal/thermal_zone*/temp` (divide by 1000
for °C). Optionally use `sensors` from `lm-sensors` package for labelled output.
**Server dependency:** `sudo apt install lm-sensors && sudo sensors-detect --auto`

### 3b. SMART disk health
**Tool:** `get_disk_health`
**What:** SMART summary for the 2TB HDD — overall health, reallocated sectors,
power-on hours, temperature, pending sectors.
**Implementation:** `sudo smartctl -H -A /dev/sda` (or whichever device).
**Server dependency:** `sudo apt install smartmontools`
**Permissions:** The `discord-bot` user needs passwordless sudo for `smartctl` only.
Add to `/etc/sudoers.d/discord-bot`:
```
discord-bot ALL=(ALL) NOPASSWD: /usr/sbin/smartctl -H -A *
```

### 3c. Failed services sweep
**Tool:** `get_failed_services`
**What:** Any systemd units currently in a failed state.
**Implementation:** `systemctl list-units --state=failed --no-legend`
**No dependencies.**

### 3d. Pending system updates
**Tool:** `get_pending_updates`
**What:** Count and list of upgradable apt packages.
**Implementation:** `apt list --upgradable 2>/dev/null`
**No dependencies.**

### 3e. Top processes
**Tool:** `get_top_processes` (param: `by`, enum: cpu/memory, default cpu)
**What:** Top 10 processes by CPU or memory usage.
**Implementation:** Parse `/proc` or use `ps aux --sort=-%cpu | head -11`
**No dependencies.**

---

## 4. Storage Detail Tools

### 4a. Media folder breakdown
**Tool:** `get_media_breakdown`
**What:** Size of each top-level folder under `/mnt/media` — Movies, Shows, Music,
Video (staging), etc. Answers "where is all my space going?"
**Implementation:** `du -sh /mnt/media/*`
**No dependencies.**

### 4b. Largest files
**Tool:** `get_largest_files` (params: `path`, `count`, default 10)
**What:** The N largest files under a given path.
**Implementation:** `find /mnt/media -type f -printf '%s %p\n' | sort -rn | head -N`
**No dependencies. Restrict allowed paths to a whitelist.**

---

## 5. Network / Connectivity Tools

### 5a. Tailscale status
**Tool:** `get_tailscale_status`
**What:** Current Tailscale IP, online peers, last seen times.
**Implementation:** `tailscale status --json` — parse peer list.
**Permissions:** `tailscale` CLI is already accessible.

### 5b. External IP
**Tool:** `get_external_ip`
**What:** Current public-facing IP address.
**Implementation:** HTTP GET to `https://api.ipify.org` or `https://ifconfig.me`
**No dependencies.**

### 5c. Listening ports
**Tool:** `get_listening_ports`
**What:** Summary of services listening on open ports.
**Implementation:** `ss -tlnp` — filter to non-loopback listeners.
**No dependencies.**

---

## 6. Write Actions

> **Before implementing:** Add a confirmation pattern to the bot. Claude should
> state the action it's about to take and ask "confirm?" before executing any
> write tool. This prevents accidents from misunderstood queries.

### 6a. Trigger Jenkins build
**Tool:** `trigger_jenkins_build` (param: `job_name`)
**What:** Start a Jenkins job manually.
**Implementation:** Jenkins API — POST with crumb:
```
GET  /crumbIssuer/api/json          → get crumb
POST /job/{job_name}/build
```
**No server dependencies — uses existing Jenkins credentials.**

### 6b. Restart a service
**Tool:** `restart_service` (param: `service_name`, whitelist only)
**What:** Restart a whitelisted service (Jellyfin, discord-bot, sunshine).
**Implementation:** `sudo systemctl restart {service}`
**Permissions:** Add to `/etc/sudoers.d/discord-bot`:
```
discord-bot ALL=(ALL) NOPASSWD: /bin/systemctl restart jellyfin
discord-bot ALL=(ALL) NOPASSWD: /bin/systemctl restart sunshine
```

### 6c. Trigger Jellyfin library scan
**Tool:** `trigger_jellyfin_scan` (param: `library`, optional)
**What:** Tell Jellyfin to scan for new media.
**API:** `POST /Library/Refresh`
**No server dependencies — uses existing Jellyfin credentials.**

### 6d. Eject disc drive
**Tool:** `eject_drive` (param: `drive`, default `/dev/sr0`)
**What:** Eject the optical drive.
**Implementation:** `eject /dev/sr0`
**Permissions:** Add eject to sudoers for discord-bot user (or chmod the device).

---

## 7. Proactive / Scheduled Notifications

These run on a timer inside the bot — no user prompt needed.

### 7a. Morning digest
**What:** Daily summary posted to Discord at a configured time (e.g. 8am).
Posts: overnight Jenkins results, disk usage, any failed services, GPU temp.
**Implementation:** Add an `asyncio` background task in `bot.py` that fires at
a scheduled time. Reuse existing tool functions to gather data, format a summary
message, call `post_notification()`.
Add `MORNING_DIGEST_HOUR` (0–23) to `.env`.

### 7b. Disk space alert
**What:** Post to Discord when `/mnt/media` exceeds a threshold (e.g. 85% full).
**Implementation:** Background task polling `df` every N hours (e.g. every 4h).
Add `DISK_ALERT_THRESHOLD_PCT` to `.env` (default 85).

### 7c. Service watchdog
**What:** Alert if Jellyfin or Sunshine goes down outside of Login_Test coverage.
**Implementation:** Background task polling `get_service_status` every 10 minutes.
Track last known state, alert on transition to down. De-duplicate — only alert
once until the service recovers.

### 7d. Temperature alert
**What:** Alert if CPU or GPU temperature exceeds a threshold.
**Implementation:** Background task reading temps every 15 minutes.
Add `TEMP_ALERT_CPU_C` and `TEMP_ALERT_GPU_C` to `.env` (defaults: 80, 85).
Requires 3a (CPU temperatures tool) to be implemented first.

---

## Implementation Notes

### Adding `.env` variables
Every new tool that needs config should add its variables to both:
- `/opt/discord-bot/.env` on the server
- `.env.example` in the repo (with placeholder values)

### Permissions pattern for sudo tools
Create `/etc/sudoers.d/discord-bot` on the server for any tool that needs
elevated access (smartctl, systemctl restart, eject). Use the narrowest possible
rule — specify the exact command and path, no wildcards on the command name.

### Confirmation pattern for write actions
Before implementing section 6, add this guard to `_run_claude_loop` or the
system prompt: Claude must explicitly state the action and await a "yes/confirm"
reply before calling any write tool. Consider a separate `WRITE_TOOLS` set and
a two-message confirmation flow.

### Suggested implementation order
1. Read-only, no new dependencies: 2a, 3c, 3d, 3e, 4a, 5a, 5c
2. Jellyfin API (needs API key): 1a, 1b, 1c
3. New server packages: 3a (lm-sensors), 3b (smartmontools)
4. App Insights query: 2c (needs app ID + read key)
5. Proactive tasks: 7b, 7c first (simple polling), then 7a, 7d
6. Write actions last: 6a (safest), then 6c, 6b, 6d
