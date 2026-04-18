# Path A Portability Refactor Plan

Make Pandabot deployable on any machine without touching source code.
All panda-specific values move to `.env`; feature flags allow disabling
entire subsystems (Jellyfin, Jenkins, ripping, SMART) when they aren't present.

---

## Overview of changes

| File | What changes |
|---|---|
| `tools.py` | Feature flags + env-driven whitelists; dynamic `TOOL_DEFINITIONS` |
| `bot.py` | Bot identity + server description env vars; refactored system prompt |
| `.env.example` | New vars with panda defaults |

---

## 1. `tools.py`

### 1a. Feature flags (top of file, after existing env vars)

```python
ENABLE_JELLYFIN = os.environ.get("ENABLE_JELLYFIN", "true").lower() == "true"
ENABLE_JENKINS  = os.environ.get("ENABLE_JENKINS",  "true").lower() == "true"
ENABLE_RIPPING  = os.environ.get("ENABLE_RIPPING",  "true").lower() == "true"
ENABLE_SMART    = os.environ.get("ENABLE_SMART",    "true").lower() == "true"
```

### 1b. Parsing helpers

```python
def _csv_set(env_var: str, default: str) -> set[str]:
    """Parse a comma-separated env var into a set of stripped strings."""
    raw = os.environ.get(env_var, default)
    return {s.strip() for s in raw.split(",") if s.strip()}

def _csv_dict(env_var: str, default: str) -> dict[str, str]:
    """Parse 'key:value,key:value' env var into a dict."""
    raw = os.environ.get(env_var, default)
    result = {}
    for item in raw.split(","):
        item = item.strip()
        if ":" in item:
            k, _, v = item.partition(":")
            result[k.strip()] = v.strip()
    return result
```

### 1c. Replace hardcoded whitelists

**Before:**
```python
ALLOWED_FILE_LOGS         = {"rip-video": "/var/log/rip-video.log", "rip-cd": "/var/log/rip-cd.log"}
ALLOWED_DOCKER_LOGS       = {"jellyfin", "jenkins"}
ALLOWED_SYSTEMD_SERVICES  = {"sunshine", "tailscaled", "cockpit", "ssh"}
```

**After:**
```python
ALLOWED_FILE_LOGS = _csv_dict(
    "FILE_LOGS",
    "rip-video:/var/log/rip-video.log,rip-cd:/var/log/rip-cd.log",
) if ENABLE_RIPPING else {}

ALLOWED_DOCKER_LOGS = _csv_set(
    "DOCKER_LOG_CONTAINERS",
    "jellyfin,jenkins",
)

ALLOWED_SYSTEMD_SERVICES = _csv_set(
    "SYSTEMD_SERVICES",
    "sunshine,tailscaled,cockpit,ssh",
)
```

Note: `ALLOWED_DOCKER_LOGS` and `ALLOWED_SYSTEMD_SERVICES` are **not** gated by feature flags
because they're used by `get_service_status` and `get_log_tail` regardless — the flags only
control whether the *tool definitions* are exposed to Claude.

### 1d. JENKINS_JOBS list

**Before:** hardcoded in `trigger_jenkins_job`:
```python
KNOWN_JOBS = ["Login_Test", "Process_Movies", "Nightly_Convert"]
```

**After:** module-level constant:
```python
JENKINS_JOBS = [j.strip() for j in
    os.environ.get("JENKINS_JOBS", "Login_Test,Process_Movies,Nightly_Convert").split(",")
    if j.strip()
]
```

Use `JENKINS_JOBS` in `trigger_jenkins_job` and in all tool description strings that list job names.

### 1e. SMART device list

**Before:** hardcoded inside `query_system_health(aspect="smart")`:
```python
DEVICES = [
    ("/dev/sda", "SanDisk SSD PLUS (boot)"),
    ("/dev/sdb", "Seagate ST4000DM004 (media)"),
]
```

**After:** module-level constant:
```python
SMART_DEVICES = list(_csv_dict(
    "SMART_DEVICES",
    "/dev/sda:SanDisk SSD PLUS (boot),/dev/sdb:Seagate ST4000DM004 (media)",
).items())  # list of (device, label) tuples
```

Replace `DEVICES` in the `smart` aspect with `SMART_DEVICES`.

### 1f. Dynamic TOOL_DEFINITIONS

Convert from a static list literal to a function or a dynamically built list at module load.

Rules:
- `query_jellyfin` — include only if `ENABLE_JELLYFIN`
- `query_ripping` — include only if `ENABLE_RIPPING`
- `trigger_jenkins_job`, `get_jenkins_build_status`, `get_jenkins_build_history`, `get_jenkins_build_log` — include only if `ENABLE_JENKINS`
- `query_system_health` — always included, but remove `"smart"` from the `aspect` enum when `not ENABLE_SMART`; remove `"smart"` from the description
- `get_log_tail` — enum and description built from `list(ALLOWED_FILE_LOGS) + list(ALLOWED_DOCKER_LOGS)` (sorted)
- `get_service_status` — enum built from `ALL_SERVICES` (already dynamic)
- `trigger_jenkins_job` + `get_jenkins_build_status` description strings — replace hardcoded job list with `", ".join(JENKINS_JOBS)`

Implementation pattern — build as a list comprehension with helper:

```python
def _build_tool_definitions() -> list[dict]:
    tools = []
    # Always-on tools ...
    tools.append({ ... })  # query_storage
    tools.append({ ... })  # get_log_tail  (enum built from current whitelists)
    tools.append({ ... })  # get_service_status
    tools.append({ ... })  # query_system_health (smart aspect conditional)
    tools.append({ ... })  # query_network
    tools.append({ ... })  # query_media_library
    tools.append({ ... })  # get_performance_history
    tools.append({ ... })  # manage_schedule

    if ENABLE_JENKINS:
        tools.append({ ... })  # trigger_jenkins_job
        tools.append({ ... })  # get_jenkins_build_status
        tools.append({ ... })  # get_jenkins_build_history
        tools.append({ ... })  # get_jenkins_build_log

    if ENABLE_JELLYFIN:
        tools.append({ ... })  # query_jellyfin

    if ENABLE_RIPPING:
        tools.append({ ... })  # query_ripping

    return tools

TOOL_DEFINITIONS = _build_tool_definitions()
```

---

## 2. `bot.py`

### 2a. New config vars (add after existing constants)

```python
BOT_NAME             = os.environ.get("BOT_NAME",   "PandaBot")
BOT_EMOJI            = os.environ.get("BOT_EMOJI",  "🐼")
TZ_NAME              = os.environ.get("TZ_NAME",    "America/New_York (Eastern Time, EDT/EST)")
SERVER_DESCRIPTION   = os.environ.get("SERVER_DESCRIPTION",  "")   # free-form override
HARDWARE_DESCRIPTION = os.environ.get("HARDWARE_DESCRIPTION",
                           "NVIDIA GTX 970 (4 GB VRAM), 2 TB NTFS HDD at /mnt/media")
```

### 2b. Refactor `_build_system_prompt()`

**Current:** hardcodes "Panda", lists Jellyfin/Jenkins/Sunshine/GTX 970, timezone, job names.

**New logic:**

```
You are {BOT_NAME}, a helpful assistant for a home Ubuntu Server machine.
Current server date/time: {now}.
```

**Services block** — two paths:

1. If `SERVER_DESCRIPTION` is set → use it verbatim as the services paragraph.
2. Otherwise → build dynamically from feature flags imported from `tools`:
   ```python
   from tools import ENABLE_JELLYFIN, ENABLE_JENKINS, ENABLE_RIPPING, JENKINS_JOBS
   lines = ["The server runs:"]
   if ENABLE_JELLYFIN:
       lines.append("  - Jellyfin (Docker, port 8096) — media server")
   if ENABLE_JENKINS:
       lines.append(f"  - Jenkins (Docker, port 8080) — CI/CD, jobs: {', '.join(JENKINS_JOBS)}")
   # Systemd services from ALLOWED_SYSTEMD_SERVICES (always shown)
   # Tailscale line (if TAILSCALE_IP set)
   if ENABLE_RIPPING:
       lines.append("  - MakeMKV + abcde for disc ripping (udev auto-rip pipeline)")
   ```

**Hardware line:**
```
Hardware: {HARDWARE_DESCRIPTION}.
```

**Timezone line:**
```
Server timezone: {TZ_NAME}.
```

**Jenkins instruction block** — only appended when `ENABLE_JENKINS` is true.

### 2c. Startup announcement

**Before:** `f"🐼 **PandaBot v{BOT_VERSION}** online"`

**After:** `f"{BOT_EMOJI} **{BOT_NAME} v{BOT_VERSION}** online"`

### 2d. Empty-mention reply

**Before:** `"Hey! Ask me anything about the server status."`

**After:** `f"Hey! Ask me anything about the server."` *(or keep generic — minor)*

---

## 3. `.env.example` additions

Add a new section after `WATCHDOG_SERVICES`:

```bash
# --- Feature flags (set to false to disable entire subsystems) ---
ENABLE_JELLYFIN=true
ENABLE_JENKINS=true
ENABLE_RIPPING=true
ENABLE_SMART=true

# --- Bot identity ---
BOT_NAME=PandaBot
BOT_EMOJI=🐼

# --- Server description (leave blank to auto-build from feature flags) ---
SERVER_DESCRIPTION=
HARDWARE_DESCRIPTION=NVIDIA GTX 970 (4 GB VRAM), 2 TB NTFS HDD at /mnt/media

# --- Timezone (shown in system prompt and timestamps) ---
TZ_NAME=America/New_York (Eastern Time, EDT/EST)

# --- Service whitelists (comma-separated; defaults match panda config) ---
DOCKER_LOG_CONTAINERS=jellyfin,jenkins
SYSTEMD_SERVICES=sunshine,tailscaled,cockpit,ssh
FILE_LOGS=rip-video:/var/log/rip-video.log,rip-cd:/var/log/rip-cd.log
JENKINS_JOBS=Login_Test,Process_Movies,Nightly_Convert

# --- SMART drives (device:label pairs, comma-separated) ---
SMART_DEVICES=/dev/sda:SanDisk SSD PLUS (boot),/dev/sdb:Seagate ST4000DM004 (media)
```

---

## Execution order

1. `tools.py` — feature flags + parsers + whitelists (no functional change yet, panda defaults hold)
2. `tools.py` — JENKINS_JOBS + SMART_DEVICES constants (replace hardcoded literals in functions)
3. `tools.py` — dynamic `TOOL_DEFINITIONS` via `_build_tool_definitions()`
4. `bot.py` — add new config vars
5. `bot.py` — refactor `_build_system_prompt()`
6. `bot.py` — startup announcement
7. `.env.example` — add new vars

Test after step 3 (tool definitions still match what Claude expects) and after step 6 (full smoke test).

---

## What does NOT change

- All tool *implementations* are unchanged — only which tools are exposed to Claude
- `execute_tool` dispatch is unchanged — tools can still be called from saved scheduled tasks
  even when their feature flag is off (scheduler bypasses Claude)
- `WATCHDOG_SERVICES` is already env-driven — no change needed
- `DISK_ALERT_PATH` / `DISK_ALERT_THRESHOLD_PCT` — already env-driven
- Webhook, auth, telemetry — unchanged

---

## Rollback

All new env vars have hardcoded panda defaults. A fresh deploy with an unmodified `.env`
is identical in behaviour to the current code — no `.env` changes required on panda itself.
