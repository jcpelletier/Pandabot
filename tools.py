"""
Tool implementations for the Panda server Discord bot.

Most tools are read-only observability. Write actions (move, rename, delete)
are gated behind ENABLE_WRITE_ACTIONS and always require explicit confirmation
before executing.
"""

import re
import shutil
import subprocess
import os
import glob as _glob
import json
import datetime
import logging
import random
import threading
import urllib.parse
import requests

# Family feature imports (optional, gated by ENABLE_FAMILY)
try:
    from family.sheet_reader import SheetReader
    from family.cache import Cache
except ImportError:
    # When tools.py is imported directly (e.g., by tests), fall back to sys.path hack
    import sys, os as _os
    _pkg_root = _os.path.dirname(_os.path.abspath(__file__))
    if _pkg_root not in sys.path:
        sys.path.insert(0, _pkg_root)
    from family.sheet_reader import SheetReader  # noqa: F811
    from family.cache import Cache  # noqa: F811

from pandabot_core.pm import github as _ghpm

logger = logging.getLogger("panda-bot")

# ---------------------------------------------------------------------------
# Feature flags — set to "false" in .env to disable entire subsystems
# ---------------------------------------------------------------------------

ENABLE_JELLYFIN      = os.environ.get("ENABLE_JELLYFIN",      "true").lower() == "true"
ENABLE_JENKINS       = os.environ.get("ENABLE_JENKINS",       "true").lower() == "true"
ENABLE_RIPPING       = os.environ.get("ENABLE_RIPPING",       "true").lower() == "true"
ENABLE_SMART         = os.environ.get("ENABLE_SMART",         "true").lower() == "true"
ENABLE_WRITE_ACTIONS     = os.environ.get("ENABLE_WRITE_ACTIONS",     "true").lower()  == "true"
ENABLE_GAMING            = os.environ.get("ENABLE_GAMING",            "true").lower()  == "true"
ENABLE_CRAWL_ANALYTICS   = os.environ.get("ENABLE_CRAWL_ANALYTICS",   "false").lower() == "true"
ENABLE_GITHUB_PM         = os.environ.get("ENABLE_GITHUB_PM",         "false").lower() == "true"
ENABLE_LOCAL_LLM         = os.environ.get("ENABLE_LOCAL_LLM",         "false").lower() == "true"
ENABLE_FAMILY            = os.environ.get("ENABLE_FAMILY",            "false").lower() == "true"
ENABLE_DEV_AGENT         = os.environ.get("ENABLE_DEV_AGENT",         "false").lower() == "true"
ENABLE_WEATHER           = os.environ.get("ENABLE_WEATHER",           "false").lower() == "true"
ENABLE_STREAMING         = os.environ.get("ENABLE_STREAMING",         "false").lower() == "true"
_DEV_AGENT_URL           = os.environ.get("DEV_AGENT_URL",            "http://localhost:8766")
_VOICE_GATEWAY_URL       = os.environ.get("VOICE_GATEWAY_URL",        "http://127.0.0.1:8900")
_VOICE_GATEWAY_TOKEN     = os.environ.get("VOICE_GATEWAY_TOKEN",      "")
STEAM_LIBRARY_PATH   = os.path.expanduser(
    os.environ.get("STEAM_LIBRARY_PATH", "~/.steam/steam/steamapps")
)

# ---------------------------------------------------------------------------
# Env-var parsing helpers
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# Connection / credential constants
# ---------------------------------------------------------------------------

JENKINS_URL    = os.environ.get("JENKINS_URL", "http://localhost:8080")
JENKINS_USER   = os.environ.get("JENKINS_USER", "admin")
JENKINS_TOKEN  = os.environ.get("JENKINS_TOKEN", "")
JELLYFIN_URL   = os.environ.get("JELLYFIN_URL", "http://localhost:8096")
# Public-facing URL for stream/image URLs sent to remote clients (e.g. the
# Flutter voice terminal). The internal JELLYFIN_URL is usually localhost
# which clients can't reach. Falls back to JELLYFIN_URL when unset.
JELLYFIN_PUBLIC_URL = os.environ.get("JELLYFIN_PUBLIC_URL", "") or JELLYFIN_URL

# HTTPS base URL used only for Cast stream URLs. Newer Chromecast/Google Home
# firmware blocks HTTP media — use an HTTPS reverse-proxy URL here.
# Falls back to JELLYFIN_PUBLIC_URL when not set (HTTP, fine for older devices).
JELLYFIN_CAST_BASE_URL = os.environ.get("JELLYFIN_CAST_BASE_URL", "") or JELLYFIN_PUBLIC_URL
JELLYFIN_TOKEN = os.environ.get("JELLYFIN_API_KEY", "")
APPINSIGHTS_APP_ID  = os.environ.get("APPINSIGHTS_APP_ID", "")
AZURE_TENANT_ID     = os.environ.get("AZURE_TENANT_ID", "")
AZURE_CLIENT_ID     = os.environ.get("AZURE_CLIENT_ID", "")
AZURE_CLIENT_SECRET = os.environ.get("AZURE_CLIENT_SECRET", "")

CRAWL_ANALYTICS_URL   = os.environ.get("CRAWL_ANALYTICS_URL",   "")
CRAWL_ANALYTICS_TOKEN = os.environ.get("CRAWL_ANALYTICS_TOKEN", "")

# Weather (optional, gated by ENABLE_WEATHER)
HOME_LATITUDE  = os.environ.get("HOME_LATITUDE",  "")
HOME_LONGITUDE = os.environ.get("HOME_LONGITUDE", "")

STAGING_PATH = os.environ.get("STAGING_PATH", "/mnt/media/Video")
MEDIA_PATH   = os.environ.get("MEDIA_PATH",   "/mnt/media/Media")

# Family feature (optional, gated by ENABLE_FAMILY env var)
FAMILY_SPREADSHEET_ID = os.environ.get("FAMILY_SPREADSHEET_ID", "")
FAMILY_SHEET_NAME = os.environ.get("FAMILY_SHEET_NAME", "Sheet1")
FAMILY_CREDENTIALS_PATH = os.environ.get("FAMILY_CREDENTIALS_PATH", "")

# ---------------------------------------------------------------------------
# Configurable whitelists and lists
# ---------------------------------------------------------------------------

# file logs: env format  "name:/path/to/log,name2:/path2"
# Only populated when ripping is enabled (these are rip-specific logs).
# Deployers without ripping can still add arbitrary file logs via FILE_LOGS.
ALLOWED_FILE_LOGS: dict[str, str] = _csv_dict(
    "FILE_LOGS",
    "rip-video:/var/log/rip-video.log,rip-cd:/var/log/rip-cd.log",
) if ENABLE_RIPPING else _csv_dict("FILE_LOGS", "")

# Docker containers the bot is allowed to read logs from / check status of
ALLOWED_DOCKER_LOGS: set[str] = _csv_set("DOCKER_LOG_CONTAINERS", "jellyfin,jenkins")

# Docker containers the bot is allowed to restart (empty = tool hidden)
RESTARTABLE_CONTAINERS: set[str] = _csv_set("RESTARTABLE_CONTAINERS", "")

# Systemd services (non-Docker) the bot is allowed to inspect
ALLOWED_SYSTEMD_SERVICES: set[str] = _csv_set("SYSTEMD_SERVICES", "sunshine,tailscaled,cockpit,ssh")

# Jenkins job names (used in trigger, status, history tools and the system prompt)
JENKINS_JOBS: list[str] = [
    j.strip()
    for j in os.environ.get("JENKINS_JOBS", "Login_Test,Process_Movies,Nightly_Convert").split(",")
    if j.strip()
]

# SMART drive devices: env format  "/dev/sda:label,/dev/sdb:label"
SMART_DEVICES: list[tuple[str, str]] = list(_csv_dict(
    "SMART_DEVICES",
    "/dev/sda:SanDisk SSD PLUS (boot),/dev/sdb:Seagate ST4000DM004 (media)",
).items())

# All services the bot knows about (used in get_service_status error messages)
ALL_SERVICES = sorted(
    list(ALLOWED_FILE_LOGS.keys())
    + list(ALLOWED_DOCKER_LOGS)
    + list(ALLOWED_SYSTEMD_SERVICES)
)

# ---------------------------------------------------------------------------
# App Insights token cache — refreshed automatically when expired
# ---------------------------------------------------------------------------

_ai_token_cache: dict = {"token": None, "expires": 0.0}


def _get_appinsights_token() -> str:
    """Return a valid Azure AD bearer token for the App Insights query API."""
    import time
    cache = _ai_token_cache
    if cache["token"] and time.time() < cache["expires"] - 60:
        return cache["token"]
    resp = requests.post(
        f"https://login.microsoftonline.com/{AZURE_TENANT_ID}/oauth2/token",
        data={
            "grant_type":    "client_credentials",
            "client_id":     AZURE_CLIENT_ID,
            "client_secret": AZURE_CLIENT_SECRET,
            "resource":      "https://api.applicationinsights.io",
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    cache["token"]   = data["access_token"]
    cache["expires"] = time.time() + int(data.get("expires_in", 3600))
    logger.info("App Insights token refreshed (expires in %ss)", data.get("expires_in", "?"))
    return cache["token"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _jenkins_auth():
    return (JENKINS_USER, JENKINS_TOKEN) if JENKINS_TOKEN else None

def _fmt_duration(ms: int) -> str:
    s = ms // 1000
    if s < 60:
        return f"{s}s"
    return f"{s // 60}m {s % 60}s"

def _fmt_timestamp(ms: int) -> str:
    """Format a Jenkins millisecond epoch timestamp in server local time."""
    if not ms:
        return "unknown"
    dt = datetime.datetime.fromtimestamp(ms / 1000).astimezone()  # local time with TZ
    return dt.strftime("%Y-%m-%d %H:%M %Z")


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def get_disk_usage() -> str:
    """df -h on root and media drive."""
    lines = []
    for path in ["/", "/mnt/media"]:
        try:
            r = subprocess.run(
                ["df", "-h", path],
                capture_output=True, text=True, timeout=10
            )
            lines.append(r.stdout.strip())
        except Exception as e:
            lines.append(f"{path}: error — {e}")
    return "\n\n".join(lines)


def get_log_tail(log_name: str, lines: int = 50) -> str:
    """Tail the last N lines of an allowed log (max 200)."""
    lines = min(max(lines, 1), 200)

    if log_name in ALLOWED_DOCKER_LOGS:
        r = subprocess.run(
            ["docker", "logs", "--tail", str(lines), log_name],
            capture_output=True, text=True, timeout=20
        )
        output = (r.stdout + r.stderr).strip()
        return output or f"(no output from docker logs {log_name})"

    if log_name in ALLOWED_FILE_LOGS:
        path = ALLOWED_FILE_LOGS[log_name]
        r = subprocess.run(
            ["tail", "-n", str(lines), path],
            capture_output=True, text=True, timeout=10
        )
        return r.stdout.strip() or f"(log empty: {path})"

    return (
        f"Unknown log '{log_name}'. "
        f"Available: {', '.join(sorted({**ALLOWED_FILE_LOGS, **{k: None for k in ALLOWED_DOCKER_LOGS}}))}"
    )


def get_service_status(service_name: str) -> str:
    """Check whether a service or container is running."""
    if service_name in ALLOWED_DOCKER_LOGS:
        r = subprocess.run(
            ["docker", "ps", "--filter", f"name=^/{service_name}$",
             "--format", "{{.Status}}\t{{.Image}}"],
            capture_output=True, text=True, timeout=10
        )
        status = r.stdout.strip()
        return f"{service_name}: {status}" if status else f"{service_name}: not running (container absent)"

    if service_name in ALLOWED_SYSTEMD_SERVICES:
        r = subprocess.run(
            ["systemctl", "is-active", service_name],
            capture_output=True, text=True, timeout=10
        )
        state = r.stdout.strip()
        r2 = subprocess.run(
            ["systemctl", "show", service_name,
             "--property=ActiveState,SubState,LoadState"],
            capture_output=True, text=True, timeout=10
        )
        return f"{service_name}: {state}\n{r2.stdout.strip()}"

    return (
        f"Unknown service '{service_name}'. "
        f"Available: {', '.join(ALL_SERVICES)}"
    )


def restart_container(container: str, confirmed: bool = False) -> str:
    """Restart a whitelisted Docker container."""
    if container not in RESTARTABLE_CONTAINERS:
        allowed = ", ".join(sorted(RESTARTABLE_CONTAINERS)) or "none"
        return f"Container '{container}' is not in the restart allowlist. Allowed: {allowed}."
    if not confirmed:
        return f"Restart container '{container}'? This will briefly interrupt the service. Reply 'yes' to confirm."
    r = subprocess.run(["docker", "restart", container], capture_output=True, text=True, timeout=30)
    if r.returncode == 0:
        return f"Container '{container}' restarted successfully."
    return f"Failed to restart '{container}': {r.stderr.strip()}"


def get_jenkins_build_status(job_name: str | None = None) -> str:
    """
    Quick status snapshot. Omit job_name for all-jobs overview,
    or provide a job name for its last build details.
    """
    auth = _jenkins_auth()
    try:
        if job_name:
            url = f"{JENKINS_URL}/job/{job_name}/lastBuild/api/json"
            r = requests.get(url, auth=auth, timeout=10)
            if r.status_code == 404:
                return f"Job '{job_name}' not found."
            r.raise_for_status()
            d = r.json()
            return json.dumps({
                "job":        job_name,
                "number":     d.get("number"),
                "result":     d.get("result"),
                "building":   d.get("building"),
                "started":    _fmt_timestamp(d.get("timestamp", 0)),
                "duration":   _fmt_duration(d.get("duration", 0)),
                "url":        d.get("url"),
            }, indent=2)
        else:
            url = (
                f"{JENKINS_URL}/api/json"
                "?tree=jobs[name,lastBuild[number,result,building,timestamp,duration]]"
            )
            r = requests.get(url, auth=auth, timeout=10)
            r.raise_for_status()
            jobs = r.json().get("jobs", [])
            lines = []
            for job in jobs:
                lb = job.get("lastBuild") or {}
                building = lb.get("building", False)
                result   = lb.get("result", "—")
                num      = lb.get("number", "—")
                started  = _fmt_timestamp(lb.get("timestamp", 0))
                state    = "BUILDING" if building else (result or "never run")
                lines.append(f"  {job['name']}: #{num} → {state}  ({started})")
            return "Jenkins jobs:\n" + "\n".join(lines)
    except requests.RequestException as e:
        return f"Jenkins API error: {e}"


def get_jenkins_build_history(job_name: str, count: int = 10, since_days: int | None = None) -> str:
    """
    Return recent builds for a job with number, result, start time and duration.
    count: last N builds (used when since_days is not set).
    since_days: if set, return all builds from the last N days instead of using count.
    """
    auth = _jenkins_auth()
    try:
        if since_days is not None:
            # Fetch enough builds to cover the requested window (cap at 200 for safety)
            fetch_count = 200
            cutoff_ms = (datetime.datetime.utcnow() - datetime.timedelta(days=since_days)).timestamp() * 1000
        else:
            fetch_count = min(max(count, 1), 50)
            cutoff_ms = None

        url = (
            f"{JENKINS_URL}/job/{job_name}/api/json"
            f"?tree=builds[number,result,building,timestamp,duration,url]{{0,{fetch_count}}}"
        )
        r = requests.get(url, auth=auth, timeout=10)
        if r.status_code == 404:
            return f"Job '{job_name}' not found."
        r.raise_for_status()
        builds = r.json().get("builds", [])

        if cutoff_ms is not None:
            builds = [b for b in builds if b.get("timestamp", 0) >= cutoff_ms]
            header = f"Builds for {job_name} in the last {since_days} day(s) ({len(builds)} total):"
        else:
            header = f"Last {len(builds)} builds for {job_name}:"

        if not builds:
            window = f"in the last {since_days} days" if since_days else f"(none found)"
            return f"No builds found for '{job_name}' {window}."

        # For since_days mode, also include a pass/fail summary
        lines = [header]
        if since_days is not None:
            success = sum(1 for b in builds if b.get("result") == "SUCCESS")
            failure = sum(1 for b in builds if b.get("result") == "FAILURE")
            lines.append(f"  Summary: {success} passed, {failure} failed out of {len(builds)} runs")

        for b in builds:
            building = b.get("building", False)
            result   = "BUILDING" if building else (b.get("result") or "IN PROGRESS")
            started  = _fmt_timestamp(b.get("timestamp", 0))
            duration = _fmt_duration(b.get("duration", 0))
            num      = b.get("number", "?")
            lines.append(f"  #{num}  {result:<10}  {started}  ({duration})")
        return "\n".join(lines)
    except requests.RequestException as e:
        return f"Jenkins API error: {e}"


def trigger_jenkins_job(job_name: str) -> str:
    """
    Trigger a Jenkins job build immediately.
    Returns confirmation, estimated duration, and scheduling hints for a follow-up check.
    """
    auth = _jenkins_auth()
    try:
        # Fetch nextBuildNumber + recent durations in one call
        meta_url = (
            f"{JENKINS_URL}/job/{job_name}/api/json"
            "?tree=nextBuildNumber,builds[duration,result]{0,5}"
        )
        mr = requests.get(meta_url, auth=auth, timeout=10)
        if mr.status_code == 404:
            return f"Job '{job_name}' not found. Known jobs: {', '.join(JENKINS_JOBS)}"
        mr.raise_for_status()
        mdata = mr.json()

        next_build_num = mdata.get("nextBuildNumber")
        builds = mdata.get("builds", [])
        durations = [
            b["duration"] // 1000
            for b in builds
            if b.get("result") and b.get("duration", 0) > 0
        ]
        avg_seconds = int(sum(durations) / len(durations)) if durations else None

        # Trigger the build
        trigger_url = f"{JENKINS_URL}/job/{job_name}/build"
        r = requests.post(trigger_url, auth=auth, timeout=10)
        if r.status_code == 404:
            return f"Job '{job_name}' not found."
        if r.status_code == 400:
            return (
                f"Job '{job_name}' requires build parameters and cannot be triggered "
                "without them via this tool."
            )
        r.raise_for_status()

        lines = [f"✅ '{job_name}' build #{next_build_num or '?'} queued."]

        # Calculate suggested check timing
        if avg_seconds:
            m, s = divmod(avg_seconds, 60)
            lines.append(f"Recent avg duration: {m}m {s}s")
            # First check: ~80% of expected duration (gives build time to start + nearly finish)
            initial_wait = max(2, int(avg_seconds * 0.8 / 60))
            # Recheck interval: ~20% of expected duration, capped 1–10 min
            check_interval = max(1, min(10, int(avg_seconds * 0.2 / 60)))
        else:
            initial_wait = 5
            check_interval = 3

        lines.append(
            f"Suggested schedule: first check in {initial_wait} min, "
            f"recheck every {check_interval} min if still building."
        )
        lines.append(
            'Use condition_pattern: \'"result":\\s*"(SUCCESS|FAILURE|UNSTABLE|ABORTED)"\' '
            "— this only matches once the build finishes (result is null while building)."
        )
        return "\n".join(lines)

    except requests.RequestException as e:
        return f"Jenkins trigger error: {e}"


def set_jenkins_schedule(job_name: str, schedule: str = "", confirmed: bool = False) -> str:
    """
    View or change the cron trigger schedule for a Jenkins job.

    schedule=""           → show the current schedule only (no change)
    schedule="H * * * *"  + confirmed=False → preview the change, ask for confirmation
    schedule="H * * * *"  + confirmed=True  → apply the change
    schedule="disabled"   → remove the timer trigger (disable scheduled runs)
    """
    import re as _re

    if job_name not in JENKINS_JOBS:
        return f"Job '{job_name}' is not in the allowed list: {', '.join(sorted(JENKINS_JOBS))}"

    auth = _jenkins_auth()
    config_url = f"{JENKINS_URL}/job/{job_name}/config.xml"

    # ── Fetch current config ─────────────────────────────────────────────────
    try:
        r = requests.get(config_url, auth=auth, timeout=10)
        if r.status_code == 404:
            return f"Job '{job_name}' not found on Jenkins."
        r.raise_for_status()
        xml = r.text
    except requests.RequestException as e:
        return f"Could not fetch config for '{job_name}': {e}"

    # ── Parse current timer spec ─────────────────────────────────────────────
    m = _re.search(r"<hudson\.triggers\.TimerTrigger>\s*<spec>(.*?)</spec>",
                   xml, _re.DOTALL)
    current_spec = m.group(1).strip() if m else None
    current_desc = f"`{current_spec}`" if current_spec else "none (not scheduled)"

    # ── View-only mode ───────────────────────────────────────────────────────
    if not schedule:
        return f"Current schedule for **{job_name}**: {current_desc}"

    new_spec = None if schedule.lower() == "disabled" else schedule
    new_desc = f"`{new_spec}`" if new_spec else "none (disabled)"

    # ── Preview / confirmation gate ──────────────────────────────────────────
    if not confirmed:
        if current_spec == new_spec:
            return f"**{job_name}** schedule is already {current_desc} — no change needed."
        lines = [
            f"Ready to update **{job_name}** schedule:",
            f"  Current: {current_desc}",
            f"  New:     {new_desc}",
            "",
            "Reply **yes** to confirm, or ignore to cancel.",
        ]
        return "\n".join(lines)

    # ── Apply the change ─────────────────────────────────────────────────────
    has_trigger = bool(_re.search(r"<hudson\.triggers\.TimerTrigger>", xml))

    if new_spec is None:
        # Remove timer trigger entirely
        xml = _re.sub(
            r"\s*<hudson\.triggers\.TimerTrigger>.*?</hudson\.triggers\.TimerTrigger>",
            "", xml, flags=_re.DOTALL,
        )
    elif has_trigger:
        # Update existing spec in-place
        xml = _re.sub(
            r"(<hudson\.triggers\.TimerTrigger>\s*<spec>).*?(</spec>)",
            rf"\g<1>{new_spec}\2",
            xml, flags=_re.DOTALL,
        )
    else:
        # Inject a new TimerTrigger
        block = (
            f"<hudson.triggers.TimerTrigger>"
            f"<spec>{new_spec}</spec>"
            f"</hudson.triggers.TimerTrigger>"
        )
        if _re.search(r"<triggers\s*/>", xml):
            xml = _re.sub(r"<triggers\s*/>", f"<triggers>{block}</triggers>", xml)
        elif "<triggers>" in xml:
            xml = xml.replace("<triggers>", f"<triggers>{block}", 1)
        else:
            xml = xml.replace("</project>", f"<triggers>{block}</triggers>\n</project>")

    try:
        pr = requests.post(
            config_url, data=xml.encode("utf-8"),
            headers={"Content-Type": "application/xml"},
            auth=auth, timeout=10,
        )
        pr.raise_for_status()
    except requests.RequestException as e:
        return f"Failed to save config for '{job_name}': {e}"

    if new_spec:
        return f"✅ **{job_name}** schedule updated to `{new_spec}`."
    else:
        return f"✅ **{job_name}** scheduled trigger removed — job will only run when triggered manually."


def get_jenkins_build_log(
    job_name: str,
    build_number: int | str | None = None,
    lines: int = 100,
) -> str:
    """
    Fetch the console log for a specific build (or 'last' / lastBuild).
    Returns the last N lines (max 300).
    """
    lines = min(max(lines, 1), 300)
    auth = _jenkins_auth()

    # Resolve build selector
    if build_number is None or str(build_number).lower() in ("last", "latest", "lastbuild"):
        build_selector = "lastBuild"
    else:
        build_selector = str(build_number)

    try:
        url = f"{JENKINS_URL}/job/{job_name}/{build_selector}/consoleText"
        r = requests.get(url, auth=auth, timeout=20)
        if r.status_code == 404:
            return f"Build not found: {job_name} #{build_selector}"
        r.raise_for_status()

        log_lines = r.text.splitlines()
        total = len(log_lines)
        tail  = log_lines[-lines:]

        header = f"--- {job_name} #{build_selector} | {total} lines total | showing last {len(tail)} ---\n"
        return header + "\n".join(tail)
    except requests.RequestException as e:
        return f"Jenkins API error: {e}"


def query_jellyfin(query_type: str = "stats") -> str:
    """Query the Jellyfin media server API."""
    if not JELLYFIN_TOKEN:
        return "JELLYFIN_API_KEY not configured in .env"

    headers = {"X-Emby-Token": JELLYFIN_TOKEN, "Accept": "application/json"}

    try:
        if query_type == "stats":
            r = requests.get(f"{JELLYFIN_URL}/Items/Counts", headers=headers, timeout=10)
            r.raise_for_status()
            d = r.json()
            lines = ["Jellyfin library:"]
            if d.get("MovieCount"):    lines.append(f"  Movies:   {d['MovieCount']}")
            if d.get("SeriesCount"):   lines.append(f"  Shows:    {d['SeriesCount']}")
            if d.get("EpisodeCount"):  lines.append(f"  Episodes: {d['EpisodeCount']}")
            if d.get("SongCount"):     lines.append(f"  Songs:    {d['SongCount']}")
            if d.get("AlbumCount"):    lines.append(f"  Albums:   {d['AlbumCount']}")
            if d.get("BoxSetCount"):   lines.append(f"  Box sets: {d['BoxSetCount']}")
            return "\n".join(lines)

        elif query_type == "recent":
            # Need a real user ID — fetch the first non-automation user
            ur = requests.get(f"{JELLYFIN_URL}/Users", headers=headers, timeout=10)
            ur.raise_for_status()
            users = [u for u in ur.json() if u.get("Name", "").lower() != "automation"]
            if not users:
                return "No users found in Jellyfin."
            uid = users[0]["Id"]
            params = {
                "SortBy": "DateCreated", "SortOrder": "Descending",
                "Limit": 10, "Recursive": "true",
                "IncludeItemTypes": "Movie,Series",
                "Fields": "DateCreated,ProductionYear",
            }
            r = requests.get(f"{JELLYFIN_URL}/Users/{uid}/Items",
                             headers=headers, params=params, timeout=10)
            r.raise_for_status()
            items = r.json().get("Items", [])
            if not items:
                return "No recently added items found."
            lines = ["Recently added:"]
            for item in items:
                added = item.get("DateCreated", "")[:10]
                year  = item.get("ProductionYear", "")
                itype = item.get("Type", "")
                lines.append(f"  [{itype}] {item['Name']} ({year})  added {added}")
            return "\n".join(lines)

        elif query_type == "streams":
            r = requests.get(f"{JELLYFIN_URL}/Sessions",
                             headers=headers, params={"ActiveWithinSeconds": 60}, timeout=10)
            r.raise_for_status()
            sessions = [s for s in r.json() if s.get("NowPlayingItem")]
            if not sessions:
                return "No active streams."
            lines = ["Active streams:"]
            for s in sessions:
                item      = s.get("NowPlayingItem", {})
                user      = s.get("UserName", "unknown")
                title     = item.get("Name", "unknown")
                method    = s.get("PlayState", {}).get("PlayMethod", "unknown")
                tc        = s.get("TranscodingInfo") or {}
                hw        = tc.get("IsVideoDirectStream", False)
                codec_out = tc.get("VideoCodec", "")
                nvenc     = "NVENC" if "nvenc" in codec_out.lower() else ""
                detail    = f"{method}" + (f" → {codec_out} {nvenc}".strip() if codec_out else "")
                lines.append(f"  {user}: {title}  [{detail}]")
            return "\n".join(lines)

        elif query_type == "history":
            ur = requests.get(f"{JELLYFIN_URL}/Users", headers=headers, timeout=10)
            ur.raise_for_status()
            users = [u for u in ur.json() if u.get("Name", "").lower() != "automation"]
            if not users:
                return "No users found."
            lines = ["Recently watched:"]
            for user in users:
                params = {
                    "SortBy": "DatePlayed", "SortOrder": "Descending",
                    "Limit": 5, "Filters": "IsPlayed", "Recursive": "true",
                    "IncludeItemTypes": "Movie,Episode",
                    "Fields": "DateLastMediaAdded",
                }
                r = requests.get(f"{JELLYFIN_URL}/Users/{user['Id']}/Items",
                                 headers=headers, params=params, timeout=10)
                r.raise_for_status()
                items = r.json().get("Items", [])
                if items:
                    lines.append(f"  {user['Name']}:")
                    for item in items:
                        lines.append(f"    - {item['Name']} ({item.get('Type', '')})")
            return "\n".join(lines) if len(lines) > 1 else "No watch history found."

        elif query_type == "week":
            # Items added in the last 7 days, grouped by type with counts
            since = (datetime.datetime.utcnow() - datetime.timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S")
            ur = requests.get(f"{JELLYFIN_URL}/Users", headers=headers, timeout=10)
            ur.raise_for_status()
            users = [u for u in ur.json() if u.get("Name", "").lower() != "automation"]
            if not users:
                return "No users found in Jellyfin."
            uid = users[0]["Id"]
            lines = ["Jellyfin additions this week:"]
            for item_type, label in [("Movie", "Movies"), ("Series", "Shows"), ("MusicAlbum", "Music albums")]:
                params = {
                    "SortBy": "DateCreated", "SortOrder": "Descending",
                    "Recursive": "true", "IncludeItemTypes": item_type,
                    "Fields": "DateCreated,ProductionYear",
                    "MinDateLastSaved": since,
                }
                items = requests.get(f"{JELLYFIN_URL}/Users/{uid}/Items",
                                     headers=headers, params=params, timeout=10).json().get("Items", [])
                if items:
                    names = [f"{i['Name']} ({i.get('ProductionYear','?')})" for i in items]
                    lines.append(f"  {label} ({len(items)}): {', '.join(names)}")
            return "\n".join(lines) if len(lines) > 1 else "Nothing added to Jellyfin this week."

        elif query_type == "search_movies":
            # Return all movies with genres + overview so Claude can answer
            # genre/vibe questions ("stoner movies", "horror from the 80s", etc.)
            ur = requests.get(f"{JELLYFIN_URL}/Users", headers=headers, timeout=10)
            ur.raise_for_status()
            users = [u for u in ur.json() if u.get("Name", "").lower() != "automation"]
            if not users:
                return "No users found in Jellyfin."
            uid = users[0]["Id"]
            params = {
                "IncludeItemTypes": "Movie",
                "Recursive": "true",
                "Fields": "Genres,Overview,CommunityRating,OfficialRating,Tags",
                "SortBy": "SortName",
                "SortOrder": "Ascending",
                "Limit": "500",
            }
            r = requests.get(f"{JELLYFIN_URL}/Users/{uid}/Items",
                             headers=headers, params=params, timeout=15)
            r.raise_for_status()
            items = r.json().get("Items", [])
            if not items:
                return "No movies found in Jellyfin library."
            lines = [f"Movies in library ({len(items)}):"]
            for item in items:
                name    = item.get("Name", "?")
                year    = item.get("ProductionYear", "")
                genres  = ", ".join(item.get("Genres") or [])
                rating  = item.get("CommunityRating")
                overview = (item.get("Overview") or "").strip()
                overview = (overview[:160] + "…") if len(overview) > 160 else overview
                rating_str = f" ★{rating:.1f}" if rating else ""
                genre_str  = f" [{genres}]" if genres else ""
                year_str   = f" ({year})" if year else ""
                line = f"  {name}{year_str}{rating_str}{genre_str}"
                if overview:
                    line += f" — {overview}"
                lines.append(line)
            return "\n".join(lines)

        else:
            return f"Unknown query_type '{query_type}'. Available: stats, recent, streams, history, week, search_movies"

    except requests.RequestException as e:
        return f"Jellyfin API error: {e}"


# ---------------------------------------------------------------------------
# Jellyfin music — play_music + control tools (voice-first; Discord-safe)
# ---------------------------------------------------------------------------
# These tools are designed for the voice gateway. They emit WebSocket
# envelopes via a thread-local voice context the gateway installs around
# run_claude_loop. When no voice context is active (e.g. Discord usage),
# they still return a useful text summary, but no envelope is broadcast.
#
# MVP single-device assumption: voice context is per-thread, and the
# gateway's transcribe handler is the only caller that sets it. If we
# ever fan out to multiple concurrent voice clients per process, this
# context model needs to switch to per-request keys.

_music_log = logging.getLogger("pandabot.music")
_voice_local = threading.local()


def set_voice_context(ctx):
    """Install (or clear by passing None) a per-thread voice context.

    `ctx`, if given, is a dict with:
      - 'emit': callable taking a single envelope dict (broadcast queue)
      - 'silent_tts': bool, set True by tools that should suppress TTS
    """
    _voice_local.context = ctx


def get_voice_context():
    return getattr(_voice_local, 'context', None)


def _emit_envelope(envelope, silent_tts=False):
    ctx = get_voice_context()
    if ctx is None:
        return
    try:
        ctx['emit'](envelope)
    except Exception:
        _music_log.exception("envelope emit failed")
    if silent_tts:
        ctx['silent_tts'] = True


def _jf_headers():
    return {"X-Emby-Token": JELLYFIN_TOKEN, "Accept": "application/json"}


def _jf_get_user_id():
    """Return the first non-automation Jellyfin user ID, or None on failure."""
    try:
        r = requests.get(f"{JELLYFIN_URL}/Users", headers=_jf_headers(), timeout=10)
        r.raise_for_status()
        users = [u for u in r.json() if u.get("Name", "").lower() != "automation"]
        return users[0]["Id"] if users else None
    except requests.RequestException as e:
        _music_log.warning("Jellyfin user fetch failed: %s", e)
        return None


def _jf_find_playlist(name):
    """Search Jellyfin for a playlist by name (exact match preferred).
    Returns the first matching playlist item dict, or None."""
    uid = _jf_get_user_id()
    if not uid:
        return None
    params = {
        "IncludeItemTypes": "Playlist",
        "Recursive": "true",
        "SearchTerm": name,
        "Limit": 5,
    }
    try:
        r = requests.get(f"{JELLYFIN_URL}/Users/{uid}/Items",
                         headers=_jf_headers(), params=params, timeout=10)
        r.raise_for_status()
        items = r.json().get("Items", [])
        name_lower = name.strip().lower()
        for item in items:
            if item.get("Name", "").lower() == name_lower:
                return item
        return items[0] if items else None
    except requests.RequestException as e:
        _music_log.warning("Jellyfin playlist search %r failed: %s", name, e)
        return None


def _jf_playlist_tracks(playlist_id, uid):
    """Return all audio tracks in a Jellyfin playlist."""
    try:
        r = requests.get(
            f"{JELLYFIN_URL}/Playlists/{playlist_id}/Items",
            headers=_jf_headers(),
            params={"UserId": uid, "Fields": "RunTimeTicks", "Limit": 500},
            timeout=15,
        )
        r.raise_for_status()
        return r.json().get("Items", [])
    except requests.RequestException as e:
        _music_log.warning("Jellyfin playlist items fetch failed: %s", e)
        return []


def _jf_currently_playing_audio():
    """Return the NowPlayingItem for the first active audio session, or None."""
    try:
        r = requests.get(
            f"{JELLYFIN_URL}/Sessions",
            headers=_jf_headers(),
            params={"ActiveWithinSeconds": 300},
            timeout=10,
        )
        r.raise_for_status()
        for session in r.json():
            item = session.get("NowPlayingItem")
            if item and item.get("MediaType") == "Audio":
                return item
    except requests.RequestException as e:
        _music_log.warning("Jellyfin sessions fetch failed: %s", e)
    return None


def _jf_search(name, item_types, artist_id=None, limit=10):
    """Wrapper around Jellyfin's /Items search."""
    if not JELLYFIN_TOKEN:
        return []
    # Movie/Series/Episode visibility is user-scoped on this server; the
    # unscoped /Items endpoint returns 0 results for those types.
    video_types = {"Movie", "Series", "Episode"}
    uid = None
    if any(t in item_types for t in video_types):
        uid = _jf_get_user_id()
    base = f"{JELLYFIN_URL}/Users/{uid}/Items" if uid else f"{JELLYFIN_URL}/Items"
    params = {
        "searchTerm": name,
        "IncludeItemTypes": item_types,
        "Recursive": "true",
        "Limit": limit,
    }
    if artist_id:
        params["ArtistIds"] = artist_id
    try:
        r = requests.get(base, headers=_jf_headers(), params=params, timeout=10)
        r.raise_for_status()
        return r.json().get("Items", [])
    except requests.RequestException as e:
        _music_log.warning("Jellyfin %s search %r failed: %s", item_types, name, e)
        return []


def _jf_artist_tracks(artist_id, limit=200):
    if not JELLYFIN_TOKEN:
        return []
    params = {
        "ArtistIds": artist_id,
        "IncludeItemTypes": "Audio",
        "Recursive": "true",
        "Limit": limit,
        "SortBy": "Name",
    }
    try:
        r = requests.get(f"{JELLYFIN_URL}/Items", headers=_jf_headers(), params=params, timeout=15)
        r.raise_for_status()
        return r.json().get("Items", [])
    except requests.RequestException as e:
        _music_log.warning("Jellyfin artist-tracks fetch failed: %s", e)
        return []


def _jf_album_tracks(album_id):
    if not JELLYFIN_TOKEN:
        return []
    params = {
        "ParentId": album_id,
        "IncludeItemTypes": "Audio",
        "SortBy": "ParentIndexNumber,IndexNumber",
        "Limit": 200,
    }
    try:
        r = requests.get(f"{JELLYFIN_URL}/Items", headers=_jf_headers(), params=params, timeout=15)
        r.raise_for_status()
        return r.json().get("Items", [])
    except requests.RequestException as e:
        _music_log.warning("Jellyfin album-tracks fetch failed: %s", e)
        return []


def _stream_url(item_id):
    """Build a streamable URL with ?api_key= query-string auth (per story #112).

    Uses JELLYFIN_PUBLIC_URL so the Android client can reach Jellyfin on the
    LAN — JELLYFIN_URL is typically http://localhost:8096 which is wrong from
    the phone's perspective.
    """
    return (
        f"{JELLYFIN_PUBLIC_URL}/Audio/{item_id}/stream"
        f"?api_key={urllib.parse.quote(JELLYFIN_TOKEN)}&static=true"
    )


def _cast_stream_url(item_id):
    """Build a Cast-compatible MP3 stream URL (story #125).

    Chromecast receivers require an accurate Content-Type declaration.
    Using static=true serves the original file format (FLAC, etc.) but we
    declare audio/mpeg, causing the receiver to reject the stream silently.
    Requesting .mp3 tells Jellyfin to transcode to MP3 regardless of source
    format, so the declared content-type is always accurate.

    JELLYFIN_CAST_BASE_URL should point to an HTTPS reverse proxy (e.g.
    https://jellyfin.jpelletier.com) because newer Cast firmware blocks HTTP.
    """
    return (
        f"{JELLYFIN_CAST_BASE_URL}/Audio/{item_id}/stream.mp3"
        f"?api_key={urllib.parse.quote(JELLYFIN_TOKEN)}"
    )


def _art_url(item_id):
    return (
        f"{JELLYFIN_PUBLIC_URL}/Items/{item_id}/Images/Primary"
        f"?api_key={urllib.parse.quote(JELLYFIN_TOKEN)}"
    )


def _track_to_queue_item(item):
    item_id = item.get("Id")
    return {
        "id": item_id,
        "title": item.get("Name"),
        "artist": (item.get("AlbumArtist") or (item.get("Artists") or [None])[0] or ""),
        "album": item.get("Album", ""),
        "duration_ms": (item.get("RunTimeTicks") or 0) // 10000,
        "url": _stream_url(item_id),
        # Separate Cast URL: forces MP3 transcoding so content-type is always accurate
        # (static=true serves original FLAC/etc. which Cast receivers reject when told audio/mpeg)
        "cast_url": _cast_stream_url(item_id),
        # Album art is attached to the album/parent for tracks
        "art_url": _art_url(item.get("AlbumId") or item_id),
    }


def play_music(track=None, album=None, artist=None, cast_target=None):
    """Search Jellyfin and assemble a play queue.

    On success emits a play_audio envelope (with silent_tts) and returns a
    short summary the LLM logs/mirrors. On miss returns a "not found"
    sentence the LLM speaks back so the user knows the search terms.
    """
    if not JELLYFIN_TOKEN:
        return "Music playback is not configured (JELLYFIN_API_KEY missing)."

    track = (track or "").strip() or None
    album = (album or "").strip() or None
    artist = (artist or "").strip() or None

    if not any([track, album, artist]):
        return "I didn't catch what to play. Try saying an artist, album, or song title."

    # Resolve artist ID up front when an artist was named
    artist_id = None
    artist_name_resolved = artist
    if artist:
        hits = _jf_search(artist, "MusicArtist", limit=5)
        if hits:
            artist_id = hits[0].get("Id")
            artist_name_resolved = hits[0].get("Name", artist)
        else:
            return f"I searched for {artist} but couldn't find that artist in your library."

    # Album path (album-only, or album+artist)
    if album:
        album_hits = _jf_search(album, "MusicAlbum", artist_id=artist_id, limit=5)
        if not album_hits and artist_id:
            album_hits = _jf_search(album, "MusicAlbum", limit=5)
        if album_hits:
            album_item = album_hits[0]
            tracks = _jf_album_tracks(album_item["Id"])
            if tracks:
                queue = [_track_to_queue_item(t) for t in tracks]
                artist_display = album_item.get("AlbumArtist") or artist_name_resolved or "Unknown"
                summary = f"Playing {album_item['Name']} by {artist_display}."
                _emit_envelope({
                    "type": "play_audio",
                    "queue": queue,
                    "summary": summary,
                    "source": {"album_id": album_item["Id"], "kind": "album"},
                    **({"cast_target": cast_target} if cast_target else {}),
                })
                return summary
        if artist:
            return f"I searched for {album} by {artist} but couldn't find that album in your library."
        return f"I searched for the album {album} but couldn't find it in your library."

    # Track path (track-only or track+artist), with album-wins tiebreak
    if track:
        # Playlist priority: when no artist is specified, a playlist name beats a track match
        if not artist:
            playlist_hit = _jf_find_playlist(track)
            if playlist_hit:
                uid = _jf_get_user_id()
                if uid:
                    pl_tracks = _jf_playlist_tracks(playlist_hit["Id"], uid)
                    if pl_tracks:
                        if get_voice_context() is None:
                            return (
                                f"Found a playlist called '{playlist_hit['Name']}' — "
                                "playlist playback is only available in the Pandabot voice terminal."
                            )
                        queue = [_track_to_queue_item(t) for t in pl_tracks]
                        summary = f"Playing playlist {playlist_hit['Name']} ({len(queue)} tracks)."
                        _emit_envelope(
                            {
                                "type": "play_audio",
                                "queue": queue,
                                "summary": summary,
                                "source": {"playlist_id": playlist_hit["Id"], "kind": "playlist"},
                                **({"cast_target": cast_target} if cast_target else {}),
                            },
                            silent_tts=True,
                        )
                        return summary

        # Album-wins tiebreak: a same-named album by the named artist beats a track match
        if artist_id:
            album_alt = _jf_search(track, "MusicAlbum", artist_id=artist_id, limit=3)
            if album_alt:
                album_item = album_alt[0]
                tracks = _jf_album_tracks(album_item["Id"])
                if tracks:
                    queue = [_track_to_queue_item(t) for t in tracks]
                    artist_display = album_item.get("AlbumArtist") or artist_name_resolved or "Unknown"
                    summary = f"Playing the {album_item['Name']} album by {artist_display}."
                    _emit_envelope({
                        "type": "play_audio",
                        "queue": queue,
                        "summary": summary,
                        "source": {"album_id": album_item["Id"], "kind": "album"},
                        **({"cast_target": cast_target} if cast_target else {}),
                    })
                    return summary

        track_hits = _jf_search(track, "Audio", artist_id=artist_id, limit=10)
        if track_hits:
            t = track_hits[0]
            queue = [_track_to_queue_item(t)]
            artist_display = (
                t.get("AlbumArtist")
                or (t.get("Artists") or [None])[0]
                or artist_name_resolved
                or "Unknown"
            )
            summary = f"Playing {t.get('Name')} by {artist_display}."
            _emit_envelope({
                "type": "play_audio",
                "queue": queue,
                "summary": summary,
                "source": {"track_id": t["Id"], "kind": "track"},
                **({"cast_target": cast_target} if cast_target else {}),
            })
            return summary
        if artist:
            return f"I searched for {track} by {artist} but couldn't find that song."
        return f"I searched for the song {track} but couldn't find it in your library."

    # Artist-only path: shuffle all tracks by that artist
    if artist_id:
        tracks = _jf_artist_tracks(artist_id)
        if tracks:
            random.shuffle(tracks)
            queue = [_track_to_queue_item(t) for t in tracks]
            summary = f"Shuffling {len(queue)} tracks by {artist_name_resolved}."
            _emit_envelope({
                "type": "play_audio",
                "queue": queue,
                "summary": summary,
                "source": {"artist_id": artist_id, "kind": "artist_shuffle"},
                **({"cast_target": cast_target} if cast_target else {}),
            })
            return summary
        return f"I found {artist_name_resolved} but they have no playable tracks in the library."

    return "I didn't catch what to play."


def _control_music(action):
    """Emit a playback_control envelope (pause/resume/skip/stop) and suppress TTS."""
    _emit_envelope({"type": "playback_control", "action": action}, silent_tts=True)
    return f"ok ({action})"


def pause_music():
    return _control_music("pause")


def resume_music():
    return _control_music("resume")


def skip_track():
    return _control_music("skip")


def previous_track():
    return _control_music("previous")


def stop_music():
    """Soft stop — current track is paused, queue + position saved so
    'resume music' picks it back up. UI keeps the now-playing card in
    its 'on hold' state."""
    return _control_music("stop")


def exit_music():
    """Hard stop — fully exit music mode. Now-playing card disappears,
    queue is dropped, resume is not possible after this."""
    return _control_music("exit")


def set_loop_mode(mode: str = "all"):
    """Cycle/set the loop mode. mode = off | all | one."""
    if mode not in ("off", "all", "one"):
        mode = "all"
    _emit_envelope(
        {"type": "playback_control", "action": "loop", "mode": mode},
        silent_tts=True,
    )
    return f"ok (loop={mode})"


# ---------------------------------------------------------------------------
# Jellyfin video cast (story #127)
# ---------------------------------------------------------------------------

def _video_cast_stream_url(item_id, direct_play=True):
    """Build a *seekable* Cast video stream URL.

    The previous progressive endpoint (/Videos/{id}/stream?Container=mp4...)
    serves a non-seekable transcode (Accept-Ranges: none), so the Cast receiver
    could only reload from 0 on a seek. Both branches below are seekable:

    direct_play=True — source is already H264/AAC in an MP4 container. Serve the
    original file with ?static=true; Jellyfin sends it with byte-range support
    (Accept-Ranges: bytes), so the receiver seeks inside the file with no reload
    and no transcode.

    direct_play=False — incompatible codec/container (HEVC, MPEG-4, AV1, MKV...).
    Use an HLS master playlist; Jellyfin transcodes to H264/AAC on demand and
    the VOD playlist lists every segment, so the receiver seeks by jumping
    segments — again with no reload.

    JELLYFIN_CAST_BASE_URL must be HTTPS; newer Cast firmware blocks HTTP.
    """
    key = urllib.parse.quote(JELLYFIN_TOKEN)
    if direct_play:
        return (
            f"{JELLYFIN_CAST_BASE_URL}/Videos/{item_id}/stream"
            f"?static=true&api_key={key}"
        )
    return (
        f"{JELLYFIN_CAST_BASE_URL}/Videos/{item_id}/master.m3u8"
        f"?MediaSourceId={item_id}&VideoCodec=h264&AudioCodec=aac&api_key={key}"
    )


# Codecs/containers the Chromecast Default Media Receiver can direct-play.
_CAST_DIRECT_VIDEO_CODECS = {"h264"}
_CAST_DIRECT_AUDIO_CODECS = {"aac", "mp3"}


def _is_cast_direct_play(data):
    """True if a Jellyfin item DTO can be direct-played by a Chromecast.

    Requires H264 video, AAC/MP3 audio, and an MP4/MOV container. Anything else
    (HEVC, AV1, MPEG-4, MKV, AC3/DTS audio...) must go through HLS transcoding.
    """
    container = (data.get("Container") or "").lower()
    if not container:
        sources = data.get("MediaSources") or []
        if sources:
            container = (sources[0].get("Container") or "").lower()
    if not ("mp4" in container or "mov" in container):
        return False
    video_codec = audio_codec = None
    for stream in data.get("MediaStreams", []):
        if stream.get("Type") == "Video" and video_codec is None:
            video_codec = (stream.get("Codec") or "").lower()
        elif stream.get("Type") == "Audio" and audio_codec is None:
            audio_codec = (stream.get("Codec") or "").lower()
    return (
        video_codec in _CAST_DIRECT_VIDEO_CODECS
        and audio_codec in _CAST_DIRECT_AUDIO_CODECS
    )


def _video_subtitle_url(item_id, stream_index):
    """Build a VTT subtitle URL accessible from Cast devices."""
    # Jellyfin subtitle route requires mediaSourceId between itemId and Subtitles;
    # for local (non-split) content mediaSourceId == itemId.
    return (
        f"{JELLYFIN_CAST_BASE_URL}/Videos/{item_id}/{item_id}/Subtitles/{stream_index}/0/Stream.vtt"
        f"?api_key={urllib.parse.quote(JELLYFIN_TOKEN)}"
    )


def _jf_video_info(item_id):
    """Fetch subtitle tracks, resume position, runtime, and Cast compatibility.

    Returns (subtitles, resume_ticks, runtime_ticks, direct_play) where:
      subtitles: list of {index, label, language, url}
      resume_ticks: int (0 if not started or user not found)
      runtime_ticks: int total duration in 100ns ticks (0 if unavailable)
      direct_play: bool — True if the Chromecast can play the original file
                   (H264/AAC/MP4), False if it needs HLS transcoding. Defaults
                   to False on error so we fall back to the always-playable HLS.
    """
    uid = _jf_get_user_id()
    subtitles = []
    resume_ticks = 0
    runtime_ticks = 0
    direct_play = False
    try:
        params = {"Fields": "MediaStreams,MediaSources,Container"}
        if uid:
            params["UserId"] = uid
        r = requests.get(
            f"{JELLYFIN_URL}/Items/{item_id}",
            headers=_jf_headers(),
            params=params,
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        runtime_ticks = data.get("RunTimeTicks") or 0
        direct_play = _is_cast_direct_play(data)
        for stream in data.get("MediaStreams", []):
            if stream.get("Type") == "Subtitle" and not stream.get("IsForced", False):
                idx = stream.get("Index", 0)
                subtitles.append({
                    "index": idx,
                    "label": stream.get("DisplayTitle") or stream.get("Language") or "Unknown",
                    "language": stream.get("Language") or "",
                    "url": _video_subtitle_url(item_id, idx),
                })
        if uid:
            resume_ticks = data.get("UserData", {}).get("PlaybackPositionTicks", 0) or 0
    except requests.RequestException as e:
        _music_log.warning("Jellyfin video info fetch failed for %s: %s", item_id, e)
    return subtitles, resume_ticks, runtime_ticks, direct_play


def _jf_next_unwatched_episode(series_id):
    """Return the next unwatched episode for a TV series, or None."""
    uid = _jf_get_user_id()
    if not uid:
        return None
    params = {
        "UserId": uid,
        "Filters": "IsUnplayed",
        "ParentId": series_id,
        "IncludeItemTypes": "Episode",
        "Recursive": "true",
        "SortBy": "PremiereDate,SortName",
        "SortOrder": "Ascending",
        "Fields": "MediaStreams",
        "Limit": 1,
    }
    try:
        r = requests.get(f"{JELLYFIN_URL}/Items", headers=_jf_headers(), params=params, timeout=10)
        r.raise_for_status()
        items = r.json().get("Items", [])
        return items[0] if items else None
    except requests.RequestException as e:
        _music_log.warning("Jellyfin next-unwatched fetch failed for series %s: %s", series_id, e)
        return None


def play_video(title, media_type=None, cast_target=None):
    """Search Jellyfin for a video item and cast it via the cast_video envelope.

    Emits cast_video (silent_tts) so the Flutter app loads the video on the
    named Chromecast device. Returns a short summary or an error message the
    LLM speaks back.
    """
    if not JELLYFIN_TOKEN:
        return "Video cast is not configured (JELLYFIN_API_KEY missing)."
    if not cast_target:
        return (
            "I need to know which Chromecast device to cast to. "
            "Please say which device — for example, 'cast to the Living Room TV'."
        )

    title = (title or "").strip()
    if not title:
        return "I didn't catch what video to play. Try saying a movie or show title."

    media_type = (media_type or "movie").lower().strip()
    if media_type in ("show", "series", "tv show", "tv series"):
        jf_type = "Series"
        type_display = "show"
    else:
        jf_type = "Movie"
        type_display = "movie"

    # Search Jellyfin for the requested type
    hits = _jf_search(title, jf_type, limit=5)
    # If movie search misses, try series as a fallback (and vice versa)
    if not hits:
        alt_type = "Series" if jf_type == "Movie" else "Movie"
        alt_hits = _jf_search(title, alt_type, limit=3)
        if alt_hits:
            hits = alt_hits
            jf_type = alt_type
            type_display = "show" if alt_type == "Series" else "movie"

    if not hits:
        return f"I searched for the {type_display} '{title}' but couldn't find it in your library."

    item = hits[0]
    item_id = item["Id"]
    display_title = item.get("Name", title)

    # For series: load the next unwatched episode
    if jf_type == "Series":
        episode = _jf_next_unwatched_episode(item_id)
        if episode:
            item = episode
            item_id = episode["Id"]
            ep_name = episode.get("Name", "")
            series_name = episode.get("SeriesName") or display_title
            season_num = episode.get("ParentIndexNumber")
            ep_num = episode.get("IndexNumber")
            if season_num is not None and ep_num is not None:
                display_title = f"{series_name} S{season_num:02d}E{ep_num:02d}"
                if ep_name:
                    display_title += f" – {ep_name}"
            else:
                display_title = series_name
            type_display = "episode"
        else:
            # No unwatched episodes: try Episode search directly
            ep_hits = _jf_search(title, "Episode", limit=3)
            if ep_hits:
                item = ep_hits[0]
                item_id = item["Id"]
                display_title = (
                    f"{item.get('SeriesName', display_title)}: {item.get('Name', display_title)}"
                )
            else:
                return (
                    f"I found '{display_title}' but there are no unwatched episodes in your library."
                )

    # Fetch subtitle tracks, resume position, runtime, and Cast compatibility
    subtitles, resume_ticks, runtime_ticks, direct_play = _jf_video_info(item_id)

    # Thumbnail (poster art)
    thumb_url = _art_url(item_id)

    _emit_envelope(
        {
            "type": "cast_video",
            "item_id": item_id,
            "stream_url": _video_cast_stream_url(item_id, direct_play),
            # Tells the Flutter app which Cast content-type to declare:
            # HLS needs application/x-mpegurl; direct-play is a plain MP4.
            "content_type": "video/mp4" if direct_play else "application/x-mpegurl",
            "resume_ticks": resume_ticks,
            "runtime_ticks": runtime_ticks,
            "title": display_title,
            "thumbnail_url": thumb_url,
            "subtitles": subtitles,
            "cast_target": cast_target,
        },
        silent_tts=True,
    )

    resume_msg = ""
    if resume_ticks > 0:
        resume_s = resume_ticks // 10_000_000
        m, s = divmod(resume_s, 60)
        resume_msg = f", resuming from {m}:{s:02d}"

    return f"Casting {display_title} to {cast_target}{resume_msg}."


# ---------------------------------------------------------------------------
# Jellyfin playlist management
# ---------------------------------------------------------------------------

def create_playlist(name: str) -> str:
    """Create a new Jellyfin music playlist."""
    if not JELLYFIN_TOKEN:
        return "Music features are not configured (JELLYFIN_API_KEY missing)."
    name = (name or "").strip()
    if not name:
        return "Playlist name is required."
    uid = _jf_get_user_id()
    if not uid:
        return "Could not find a Jellyfin user to create the playlist under."
    existing = _jf_find_playlist(name)
    if existing and existing.get("Name", "").lower() == name.lower():
        return f"A playlist called '{existing['Name']}' already exists."
    try:
        r = requests.post(
            f"{JELLYFIN_URL}/Playlists",
            headers={**_jf_headers(), "Content-Type": "application/json"},
            json={"Name": name, "UserId": uid, "MediaType": "Audio"},
            timeout=10,
        )
        r.raise_for_status()
        return f"Created playlist '{name}'."
    except requests.RequestException as e:
        return f"Failed to create playlist: {e}"


def add_currently_playing_to_playlist(playlist_name: str) -> str:
    """Add the currently playing Jellyfin audio track to a named playlist."""
    if not JELLYFIN_TOKEN:
        return "Music features are not configured (JELLYFIN_API_KEY missing)."
    playlist_name = (playlist_name or "").strip()
    if not playlist_name:
        return "Playlist name is required."
    item = _jf_currently_playing_audio()
    if not item:
        return "Nothing is currently playing in Jellyfin."
    track_id = item.get("Id")
    track_name = item.get("Name", "Unknown")
    artist = item.get("AlbumArtist") or (item.get("Artists") or [None])[0] or "Unknown"
    playlist = _jf_find_playlist(playlist_name)
    if not playlist:
        return (
            f"I couldn't find a playlist called '{playlist_name}'. "
            f"Create it first with 'create a playlist called {playlist_name}'."
        )
    uid = _jf_get_user_id()
    if not uid:
        return "Could not find a Jellyfin user."
    try:
        r = requests.post(
            f"{JELLYFIN_URL}/Playlists/{playlist['Id']}/Items",
            headers=_jf_headers(),
            params={"Ids": track_id, "UserId": uid},
            timeout=10,
        )
        r.raise_for_status()
        return f"Added '{track_name}' by {artist} to '{playlist['Name']}'."
    except requests.RequestException as e:
        return f"Failed to add track to playlist: {e}"


def play_playlist(playlist_name: str, cast_target=None) -> str:
    """Play a Jellyfin playlist. Only available from the Flutter voice terminal."""
    if not JELLYFIN_TOKEN:
        return "Music features are not configured (JELLYFIN_API_KEY missing)."
    ctx = get_voice_context()
    if ctx is None:
        return "Playlist playback is only available in the Pandabot voice terminal, not Discord."
    playlist_name = (playlist_name or "").strip()
    if not playlist_name:
        return "Playlist name is required."
    uid = _jf_get_user_id()
    if not uid:
        return "Could not find a Jellyfin user."
    playlist = _jf_find_playlist(playlist_name)
    if not playlist:
        return f"I couldn't find a playlist called '{playlist_name}'."
    tracks = _jf_playlist_tracks(playlist["Id"], uid)
    if not tracks:
        return f"The playlist '{playlist['Name']}' is empty."
    queue = [_track_to_queue_item(t) for t in tracks]
    summary = f"Playing playlist {playlist['Name']} ({len(queue)} tracks)."
    _emit_envelope(
        {
            "type": "play_audio",
            "queue": queue,
            "summary": summary,
            "source": {"playlist_id": playlist["Id"], "kind": "playlist"},
            **({"cast_target": cast_target} if cast_target else {}),
        },
        silent_tts=True,
    )
    return summary


def query_ripping(query_type: str = "staging") -> str:
    """Query the disc ripping and media pipeline."""
    import os, time

    if query_type == "staging":
        # Files/folders in the staging area waiting to be processed by Sort_Rips
        try:
            entries = []
            for name in os.listdir(STAGING_PATH):
                if name.lower() == "processed":
                    continue
                full = os.path.join(STAGING_PATH, name)
                try:
                    stat = os.stat(full)
                    age_h = (time.time() - stat.st_mtime) / 3600
                    if os.path.isdir(full):
                        r = subprocess.run(["du", "-sh", full],
                                           capture_output=True, text=True, timeout=15)
                        size = r.stdout.split()[0] if r.returncode == 0 else "?"
                    else:
                        size = _fmt_bytes(stat.st_size)
                    entries.append((name, size, age_h))
                except Exception:
                    entries.append((name, "?", 0))

            if not entries:
                return f"Staging area is empty — nothing waiting to be processed."
            lines = [f"Staging area ({STAGING_PATH}) — {len(entries)} item(s) pending Sort_Rips:"]
            for name, size, age_h in sorted(entries, key=lambda x: -x[2]):
                age_str = f"{age_h:.0f}h ago" if age_h < 48 else f"{age_h/24:.1f}d ago"
                lines.append(f"  {name}  [{size}]  added {age_str}")
            return "\n".join(lines)
        except Exception as e:
            return f"Error reading staging area: {e}"

    elif query_type == "subtitles":
        # Video files missing subtitle sidecar files in Movies and Shows
        VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".m4v", ".mov"}
        SIDECAR_EXTS = {".srt", ".sup", ".sub", ".ass", ".vtt"}
        results = {}
        for library in ["Movies", "Shows"]:
            lib_path = os.path.join(MEDIA_PATH, library)
            if not os.path.isdir(lib_path):
                continue
            missing, total = [], 0
            for root, _, files in os.walk(lib_path):
                for f in files:
                    base, ext = os.path.splitext(f)
                    if ext.lower() not in VIDEO_EXTS:
                        continue
                    total += 1
                    full_base = os.path.join(root, base)
                    # Sidecars match {base}.* or {base}.{lang}.*
                    has_sidecar = any(
                        any(os.path.exists(f"{full_base}{sep}{sc}")
                            for sep in (".", ".en.", ".fr.", ".es.", ".de."))
                        for sc in ("srt", "sup", "sub", "ass", "vtt")
                    ) or any(
                        fname.startswith(base + ".") and
                        os.path.splitext(fname)[1].lower() in SIDECAR_EXTS
                        for fname in files
                    )
                    if not has_sidecar:
                        missing.append(os.path.relpath(os.path.join(root, f), lib_path))
            results[library] = {"total": total, "missing": len(missing), "files": missing}

        lines = ["Subtitle sidecar status:"]
        for library, data in results.items():
            have = data["total"] - data["missing"]
            lines.append(f"\n  {library}: {have}/{data['total']} have subtitles "
                         f"({data['missing']} missing)")
            for f in sorted(data["files"])[:10]:
                lines.append(f"    - {f}")
            if data["missing"] > 10:
                lines.append(f"    … and {data['missing'] - 10} more")
        return "\n".join(lines)

    elif query_type == "recent_rips":
        if not all([APPINSIGHTS_APP_ID, AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET]):
            return (
                "App Insights query not configured. Add to .env:\n"
                "  APPINSIGHTS_APP_ID    — App Insights → Overview → Application ID\n"
                "  AZURE_TENANT_ID       — Entra ID → App registrations → your app\n"
                "  AZURE_CLIENT_ID       — same page\n"
                "  AZURE_CLIENT_SECRET   — Certificates & secrets\n"
                "(App registration needs Monitoring Reader role on the App Insights resource)"
            )
        try:
            token = _get_appinsights_token()
            query = (
                "customEvents "
                "| where name == 'RipCompleted' "
                "| where timestamp > ago(30d) "
                "| extend disc_title = tostring(customDimensions.disc_title), "
                "         artist = tostring(customDimensions.artist), "
                "         album  = tostring(customDimensions.album), "
                "         tracks = tostring(customDimensions.track_count), "
                "         size   = tostring(customDimensions.final_size), "
                "         role   = cloud_RoleName "
                "| project timestamp, role, disc_title, artist, album, tracks, size "
                "| order by timestamp desc "
                "| take 20"
            )
            resp = requests.post(
                f"https://api.applicationinsights.io/v1/apps/{APPINSIGHTS_APP_ID}/query",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"query": query},
                timeout=15,
            )
            resp.raise_for_status()
            rows = resp.json()["tables"][0]["rows"]
            if not rows:
                return "No rip events found in the last 30 days."
            lines = [f"Last {len(rows)} rips (30-day window):"]
            for row in rows:
                ts, role, disc_title, artist, album, tracks, size = row
                title = disc_title
                # App Insights returns UTC ISO 8601 — convert to server local time
                try:
                    utc_dt = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    local_dt = utc_dt.astimezone()
                    date = local_dt.strftime("%Y-%m-%d")
                except Exception:
                    date = ts[:10]
                if role == "rip-cd":
                    lines.append(f"  [{date}] 🎵 {artist} — {album} ({tracks} tracks)")
                else:
                    lines.append(f"  [{date}] 🎬 {title}  {size}")
            return "\n".join(lines)
        except requests.RequestException as e:
            logger.error("App Insights query_ripping failed: %s", e)
            return f"App Insights query error: {e}"

    else:
        return f"Unknown query_type '{query_type}'. Available: staging, subtitles, recent_rips"


def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def get_performance_history(metric: str = "cpu", hours: int = 1) -> str:
    """
    Query PCP pmlogger for historical performance data (same source as Cockpit graphs).
    metric: cpu | memory | disk | network
    hours:  1–168
    """
    hours = max(1, min(168, int(hours)))

    # Sample density: finer for short windows, coarser for long ones
    if hours <= 2:
        interval = "2min"
    elif hours <= 6:
        interval = "5min"
    else:
        interval = "15min"

    METRIC_MAP = {
        "cpu": {
            "metrics": ["kernel.all.cpu.user", "kernel.all.cpu.sys", "kernel.all.cpu.idle"],
            "note": "Values are ms/s per CPU. % ≈ (user+sys) / (ncpu × 10).",
        },
        "memory": {
            "metrics": ["mem.util.used", "mem.util.free"],
            "note": "Values in bytes. Divide by 1073741824 for GB.",
        },
        "disk": {
            "metrics": ["disk.all.read_bytes", "disk.all.write_bytes"],
            "note": "Values in bytes/s across all disks.",
        },
        "network": {
            "metrics": ["network.interface.in.bytes", "network.interface.out.bytes"],
            "note": "Values in bytes/s. Columns repeat per active interface.",
        },
    }

    if metric not in METRIC_MAP:
        return f"Unknown metric '{metric}'. Available: {', '.join(METRIC_MAP.keys())}"

    config = METRIC_MAP[metric]

    # For CPU, include ncpu so Claude can calculate percentages
    extra_info = ""
    if metric == "cpu":
        try:
            ncpu = int(subprocess.check_output(["nproc"], text=True).strip())
            extra_info = f"  CPU count: {ncpu}\n"
        except Exception:
            pass

    cmd = [
        "pmrep",
        "-S", f"-{hours}hour",
        "-t", interval,
        "-o", "csv",
    ] + config["metrics"]

    pcp_error = None
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            err = result.stderr.strip()
            if "Cannot connect" in err or "Connection refused" in err:
                pcp_error = "pmcd not running"
            elif "No data" in err or "no data" in err:
                pcp_error = "no data"
            else:
                pcp_error = f"pmrep error: {err}"
        else:
            output = result.stdout.strip()
            if not output:
                pcp_error = "no output"
            else:
                lines = output.splitlines()
                header_line = lines[0] if lines else ""
                data_lines  = lines[1:] if len(lines) > 1 else []
                if len(data_lines) > 35:
                    data_lines = data_lines[-35:]

                out = [
                    f"=== {metric.upper()} history — last {hours}h (sampled every {interval}) ===",
                    config["note"],
                ]
                if extra_info:
                    out.append(extra_info.strip())
                out.append(header_line)
                out.extend(data_lines)
                return "\n".join(out)

    except FileNotFoundError:
        pcp_error = "pmrep not found"
    except subprocess.TimeoutExpired:
        pcp_error = "pmrep timed out"
    except Exception as e:
        pcp_error = str(e)

    # Fallback to sar (sysstat) for CPU metrics when PCP is unavailable
    if metric == "cpu":
        return _get_cpu_history_sar(hours, pcp_error)

    return (
        f"PCP unavailable ({pcp_error}) and no fallback exists for {metric} metrics. "
        "Install/start PCP: sudo apt install pcp cockpit-pcp && sudo systemctl start pmcd pmlogger"
    )


def _get_cpu_history_sar(hours: int, pcp_error: str) -> str:
    """Fallback CPU history using sar from sysstat."""
    import datetime

    start_time = (datetime.datetime.now() - datetime.timedelta(hours=hours)).strftime("%H:%M:%S")

    try:
        result = subprocess.run(
            ["sar", "-u", "-s", start_time],
            capture_output=True, text=True, timeout=10,
        )
        output = result.stdout.strip()
        if not output:
            return (
                f"PCP unavailable ({pcp_error}) and sar returned no data. "
                "Install sysstat: sudo apt install sysstat"
            )

        lines = output.splitlines()
        # sar output: header lines, then data rows ending with an "Average:" line
        data_rows = []
        avg_line = ""
        idle_values = []
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("Linux") or stripped.startswith("Average"):
                if stripped.startswith("Average"):
                    avg_line = stripped
                continue
            parts = stripped.split()
            # Data rows have time in first column; idle % is last numeric column before EOL
            # Header row contains "%idle" — skip it
            if "%idle" in stripped:
                continue
            try:
                idle_values.append(float(parts[-1]))
                data_rows.append(stripped)
            except (ValueError, IndexError):
                continue

        if not data_rows and not avg_line:
            return (
                f"PCP unavailable ({pcp_error}). sar is installed but has no data for the "
                f"past {hours}h — sysstat logging may not be enabled "
                "(check /etc/default/sysstat: set ENABLED=true, then sudo systemctl enable --now sysstat)."
            )

        # Cap rows at 35 (keep most recent)
        if len(data_rows) > 35:
            data_rows = data_rows[-35:]

        avg_idle = sum(idle_values) / len(idle_values) if idle_values else None

        out = [
            f"=== CPU history — last {hours}h (via sar/sysstat; PCP unavailable: {pcp_error}) ===",
            "Columns: time  CPU  %user  %nice  %system  %iowait  %steal  %idle",
        ]
        if avg_idle is not None:
            out.append(f"Average idle over window: {avg_idle:.1f}%  (average busy: {100 - avg_idle:.1f}%)")
        out.extend(data_rows)
        if avg_line:
            out.append(avg_line)
        return "\n".join(out)

    except FileNotFoundError:
        return (
            f"PCP unavailable ({pcp_error}) and sar not found. "
            "Install either: sudo apt install pcp cockpit-pcp  OR  sudo apt install sysstat"
        )
    except subprocess.TimeoutExpired:
        return f"PCP unavailable ({pcp_error}) and sar timed out."
    except Exception as e:
        return f"PCP unavailable ({pcp_error}); sar fallback failed: {e}"


def query_system_health(aspect: str = "stats") -> str:
    """Check various aspects of system health."""

    if aspect == "stats":
        return get_system_stats()

    elif aspect == "failed":
        try:
            r = subprocess.run(
                ["systemctl", "list-units", "--state=failed", "--no-pager", "--no-legend"],
                capture_output=True, text=True, timeout=10,
            )
            output = r.stdout.strip()
            if not output:
                return "✅ No failed systemd units."
            lines = ["Failed systemd units:"]
            for line in output.splitlines():
                lines.append(f"  {line.strip()}")
            return "\n".join(lines)
        except Exception as e:
            return f"Error checking failed units: {e}"

    elif aspect == "updates":
        try:
            r = subprocess.run(
                ["apt", "list", "--upgradable"],
                capture_output=True, text=True, timeout=30,
            )
            pkgs = [l for l in r.stdout.splitlines()
                    if l and not l.startswith("Listing")]
            if not pkgs:
                return "✅ System is up to date — no upgradable packages."
            return f"{len(pkgs)} upgradable package(s):\n" + "\n".join(f"  {p}" for p in pkgs)
        except Exception as e:
            return f"Error checking updates: {e}"

    elif aspect == "processes":
        try:
            r = subprocess.run(
                ["ps", "aux", "--sort=-%cpu"],
                capture_output=True, text=True, timeout=10,
            )
            lines = r.stdout.splitlines()
            return "\n".join(lines[:16])  # header + top 15
        except Exception as e:
            return f"Error listing processes: {e}"

    elif aspect == "smart":
        DEVICES = SMART_DEVICES
        # Attributes we care about, by name as reported by smartctl
        KEY_ATTRS = {
            "Reallocated_Sector_Ct", "Current_Pending_Sector", "Offline_Uncorrectable",
            "Reported_Uncorrect", "Power_On_Hours", "Power_Cycle_Count", "Start_Stop_Count",
            "Temperature_Celsius", "Airflow_Temperature_Cel",
            "Program_Fail_Count", "Erase_Fail_Count", "Total_Write/Erase_Count",
            "End-to-End_Error", "Runtime_Bad_Block",
        }

        def _parse_hours(raw: str) -> int | None:
            """Parse power-on hours from raw value like '15674h+36m+...' or '2745'."""
            try:
                return int(raw.split("h")[0].replace("+", "").strip())
            except (ValueError, IndexError):
                return None

        parts = []
        for device, label in DEVICES:
            try:
                r = subprocess.run(
                    ["/usr/sbin/smartctl", "-H", "-A", device],
                    capture_output=True, text=True, timeout=15,
                )
                out = r.stdout

                # Overall health
                health = "unknown"
                for line in out.splitlines():
                    if "overall-health self-assessment test result:" in line:
                        health = line.split(":")[-1].strip()
                        break

                # Parse attribute table (lines after the ATTRIBUTE_NAME header)
                attrs: dict[str, str] = {}
                in_table = False
                for line in out.splitlines():
                    if "ATTRIBUTE_NAME" in line:
                        in_table = True
                        continue
                    if in_table:
                        cols = line.split()
                        if len(cols) >= 10:
                            # cols[9] is the start of RAW_VALUE; some attributes append
                            # annotations like "(Min/Max 20/46)" — grab only the first token
                            attrs[cols[1]] = cols[9]

                health_icon = "✅" if health == "PASSED" else "🔴"
                lines = [f"{device}  {label}", f"  Health: {health_icon} {health}"]

                # Sector errors — critical, flag non-zero
                for key, label_str in [
                    ("Reallocated_Sector_Ct",   "Reallocated sectors  "),
                    ("Current_Pending_Sector",   "Pending sectors      "),
                    ("Offline_Uncorrectable",    "Offline uncorrectable"),
                    ("Reported_Uncorrect",       "Reported uncorrectable"),
                    ("End-to-End_Error",         "End-to-end errors    "),
                    ("Runtime_Bad_Block",        "Runtime bad blocks   "),
                ]:
                    if key in attrs:
                        val = attrs[key].split("h")[0].strip()
                        flag = " ⚠️" if val not in ("0", "") and not val.startswith("-") else ""
                        lines.append(f"  {label_str}: {val}{flag}")

                # Power-on time
                if "Power_On_Hours" in attrs:
                    h = _parse_hours(attrs["Power_On_Hours"])
                    if h is not None:
                        lines.append(f"  Power-on hours       : {h:,}h  ({h // 24:,} days / {h // 8760:.1f} yrs)")

                # Temperature
                temp_raw = attrs.get("Temperature_Celsius") or attrs.get("Airflow_Temperature_Cel")
                if temp_raw:
                    try:
                        t = int(temp_raw.split()[0])
                        flag = " ⚠️" if t > 50 else ""
                        lines.append(f"  Temperature          : {t}°C{flag}")
                    except ValueError:
                        lines.append(f"  Temperature          : {temp_raw}")

                # SSD-specific wear
                for key, label_str in [
                    ("Program_Fail_Count",       "Program fail count   "),
                    ("Erase_Fail_Count",         "Erase fail count     "),
                    ("Total_Write/Erase_Count",  "Total write/erase    "),
                ]:
                    if key in attrs:
                        val = attrs[key]
                        flag = " ⚠️" if key in ("Program_Fail_Count", "Erase_Fail_Count") \
                                        and val not in ("0", "") else ""
                        lines.append(f"  {label_str}: {val}{flag}")

                parts.append("\n".join(lines))

            except FileNotFoundError:
                parts.append(f"{device}: smartctl not found — run: sudo apt install smartmontools")
            except Exception as e:
                parts.append(f"{device}: error — {e}")

        return "\n\n".join(parts)

    else:
        return f"Unknown aspect '{aspect}'. Available: stats, failed, updates, processes, smart"


def query_storage(query_type: str = "usage", limit: int = 20) -> str:
    """Check disk usage and storage breakdown."""

    if query_type == "usage":
        return get_disk_usage()

    elif query_type == "breakdown":
        base = "/mnt/media"
        try:
            entries = []
            for name in sorted(os.listdir(base)):
                full = os.path.join(base, name)
                r = subprocess.run(
                    ["du", "-sh", "--apparent-size", full],
                    capture_output=True, text=True, timeout=60,
                )
                if r.returncode == 0:
                    size = r.stdout.split("\t", 1)[0]
                    entries.append((name, size))
            if not entries:
                return f"No entries found under {base}."
            lines = [f"Storage breakdown for {base}:"]
            for name, size in entries:
                lines.append(f"  {size:>8}  {name}")
            return "\n".join(lines)
        except Exception as e:
            return f"Error getting breakdown: {e}"

    elif query_type == "largest":
        base = "/mnt/media"
        limit = min(max(limit, 1), 50)
        try:
            r = subprocess.run(
                ["find", base, "-type", "f", "-printf", "%s\t%p\n"],
                capture_output=True, text=True, timeout=120,
            )
            if r.returncode != 0:
                return f"Error scanning files: {r.stderr.strip()}"
            entries = []
            for line in r.stdout.splitlines():
                try:
                    size_str, fpath = line.split("\t", 1)
                    entries.append((int(size_str), fpath))
                except ValueError:
                    pass
            if not entries:
                return "No files found."
            entries.sort(reverse=True)
            shown = entries[:limit]
            lines = [f"Top {len(shown)} largest files under {base} ({len(entries)} total):"]
            for size, fpath in shown:
                rel = os.path.relpath(fpath, base)
                lines.append(f"  {_fmt_bytes(size):>10}  {rel}")
            return "\n".join(lines)
        except Exception as e:
            return f"Error finding largest files: {e}"

    else:
        return f"Unknown query_type '{query_type}'. Available: usage, breakdown, largest"


def query_network(query_type: str = "tailscale") -> str:
    """Query network status."""

    if query_type == "tailscale":
        try:
            r = subprocess.run(
                ["tailscale", "status"],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode != 0:
                return f"Tailscale error: {(r.stderr or r.stdout).strip()}"
            return r.stdout.strip() or "Tailscale: no output"
        except FileNotFoundError:
            return "tailscale CLI not found — is Tailscale installed?"
        except Exception as e:
            return f"Error querying Tailscale: {e}"

    elif query_type == "external_ip":
        try:
            r = requests.get("https://api.ipify.org", timeout=5)
            r.raise_for_status()
            return f"External IP: {r.text.strip()}"
        except Exception as e:
            return f"Error getting external IP: {e}"

    elif query_type == "ports":
        try:
            r = subprocess.run(
                ["ss", "-tlnp"],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode != 0:
                return f"ss error: {r.stderr.strip()}"
            return r.stdout.strip() or "No listening TCP ports found."
        except Exception as e:
            return f"Error listing ports: {e}"

    else:
        return f"Unknown query_type '{query_type}'. Available: tailscale, external_ip, ports"


DISCORD_CHANNEL_ID = int(os.environ.get("DISCORD_CHANNEL_ID", "0"))


def manage_schedule(action: str, **kwargs) -> str:
    """Create, list, or cancel scheduled tasks."""
    from pandabot_core import scheduler as sched
    sched.init_db()

    if action == "list":
        tasks = sched.list_pending()
        if not tasks:
            return "No scheduled tasks pending."
        lines = ["Pending scheduled tasks:"]
        for t in tasks:
            fire_local = (
                datetime.datetime.fromisoformat(t["fire_at"])
                .astimezone()
                .strftime("%a %b %d %I:%M %p %Z")
            )
            type_note = t["task_type"]
            if t["task_type"] == "condition_check":
                type_note = f"condition check {t['attempt']}/{t['max_attempts']}"
            recurr = f" [🔁 {t['recurrence_rule']}]" if t["recurrence_rule"] else ""
            lines.append(f"  #{t['id']}  {fire_local}  — {t['description']} ({type_note}){recurr}")
        return "\n".join(lines)

    if action == "cancel":
        task_id = kwargs.get("id")
        if not task_id:
            return "cancel requires an id."
        cancelled = sched.cancel_task(int(task_id))
        return f"Task #{task_id} cancelled." if cancelled else f"Task #{task_id} not found or already done."

    if action == "create":
        fire_at = kwargs.get("fire_at")
        if not fire_at:
            return "create requires fire_at (local ISO datetime, e.g. '2026-04-18T09:00:00')."
        description = kwargs.get("description", "Scheduled task")
        task_type   = kwargs.get("task_type", "one_shot")

        # Parse tool_calls — may arrive as list of dicts or JSON string
        raw_tc = kwargs.get("tool_calls") or []
        if isinstance(raw_tc, str):
            import json as _json
            raw_tc = _json.loads(raw_tc)

        task_id = sched.add_task(
            fire_at_local          = fire_at,
            channel_id             = DISCORD_CHANNEL_ID,
            description            = description,
            task_type              = task_type,
            tool_calls             = raw_tc,
            intro_message          = kwargs.get("intro_message"),
            static_message         = kwargs.get("static_message"),
            generative_prompt      = kwargs.get("generative_prompt"),
            condition_pattern      = kwargs.get("condition_pattern"),
            met_message            = kwargs.get("met_message"),
            not_met_message        = kwargs.get("not_met_message"),
            max_attempts           = int(kwargs.get("max_attempts", 5)),
            check_interval_minutes = int(kwargs.get("check_interval_minutes", 30)),
            recurrence_rule        = kwargs.get("recurrence_rule"),
        )

        local_dt  = datetime.datetime.fromisoformat(fire_at)
        time_str  = local_dt.strftime("%A %b %d at %I:%M %p")
        type_note = {
            "one_shot":        "fires once",
            "condition_check": (f"checks up to {kwargs.get('max_attempts', 5)}× "
                                f"every {kwargs.get('check_interval_minutes', 30)} min"),
            "recurring":       f"repeats ({kwargs.get('recurrence_rule', '?')})",
        }.get(task_type, task_type)
        return f"✅ Scheduled #{task_id}: \"{description}\" — {time_str} ({type_note})"

    return f"Unknown action '{action}'. Use create, list, or cancel."


def manage_files(action: str, source: str, dest: str = "", confirmed: bool = False) -> str:
    """
    Move, rename, or delete files and folders within the media library.
    All paths must stay within MEDIA_PATH or STAGING_PATH.
    Always call with confirmed=False first — returns a preview.
    Only call with confirmed=True after the user explicitly says 'yes'.
    """
    import time
    import errno as _errno

    def _remove(path: str, retries: int = 6, delay: float = 3.0) -> None:
        """Remove a file, retrying on transient EROFS (ntfs-3g checkpoint windows).
        Up to 6 retries × 3 s = 18 s window, which covers observed checkpoint durations."""
        last_err: Exception = OSError("no attempts made")
        for _ in range(retries):
            try:
                os.remove(path)
                return
            except OSError as e:
                if e.errno == _errno.EROFS:
                    last_err = e
                    time.sleep(delay)
                    continue
                raise
        raise last_err

    def _sync_before_write() -> None:
        """Force kernel buffer flush so ntfs-3g clears any pending dirty-bit transactions."""
        try:
            os.sync()
            time.sleep(0.5)
        except Exception:
            pass

    ALLOWED_ROOTS = [p for p in [MEDIA_PATH, STAGING_PATH] if p]

    def _resolve(p: str) -> str:
        if not os.path.isabs(p):
            p = os.path.join(MEDIA_PATH, p)
        return os.path.realpath(os.path.normpath(p))

    def _is_allowed(real_path: str) -> bool:
        return any(
            real_path == os.path.realpath(root) or
            real_path.startswith(os.path.realpath(root) + os.sep)
            for root in ALLOWED_ROOTS
        )

    def _is_root(real_path: str) -> bool:
        return any(real_path == os.path.realpath(root) for root in ALLOWED_ROOTS)

    def _dir_manifest(path: str) -> tuple[list[str], int, int]:
        """Return (display_lines, file_count, total_bytes) for a directory."""
        files = []
        total_bytes = 0
        for dirpath, _, filenames in os.walk(path):
            for fname in sorted(filenames):
                full = os.path.join(dirpath, fname)
                try:
                    sz = os.path.getsize(full)
                    total_bytes += sz
                    files.append((os.path.relpath(full, path), sz))
                except OSError:
                    pass
        files.sort()
        shown = files[:20]
        lines = [f"  {rel}  ({_fmt_bytes(sz)})" for rel, sz in shown]
        if len(files) > 20:
            lines.append(f"  … and {len(files) - 20} more file(s)")
        return lines, len(files), total_bytes

    # ── Validate source ───────────────────────────────────────────────────────
    src = _resolve(source)

    if not _is_allowed(src):
        return f"Source path not allowed. Must be under: {', '.join(ALLOWED_ROOTS)}"
    if _is_root(src):
        return "Cannot operate on a root library path directly."
    if not os.path.exists(src):
        return f"Not found: {src}"

    # ── delete ────────────────────────────────────────────────────────────────
    if action == "delete":
        if os.path.isfile(src):
            size = _fmt_bytes(os.path.getsize(src))
            manifest_lines = [f"  {os.path.basename(src)}  ({size})"]
            total_desc = size
        else:
            manifest_lines, file_count, total_bytes = _dir_manifest(src)
            total_desc = f"{file_count} file(s), {_fmt_bytes(total_bytes)} total"

        if not confirmed:
            kind = "file" if os.path.isfile(src) else "directory and all contents"
            lines = [
                f"Ready to permanently delete {kind}:",
                f"  {src}",
                "",
            ] + manifest_lines + [
                "",
                f"Total: {total_desc}",
                "",
                "⚠️ This cannot be undone. Reply **yes** to confirm.",
            ]
            return "\n".join(lines)

        _sync_before_write()
        try:
            if os.path.isfile(src):
                _remove(src)
            else:
                shutil.rmtree(src)
            return f"✅ Deleted: {src}"
        except Exception as e:
            return f"Delete failed: {e}"

    # ── rename ────────────────────────────────────────────────────────────────
    elif action == "rename":
        if not dest:
            return "rename requires dest — the new name only (not a path)."
        if os.sep in dest or "/" in dest:
            return (
                "rename dest must be a bare name with no path separators. "
                "To relocate a file, use move instead."
            )

        new_path = os.path.join(os.path.dirname(src), dest)
        new_real = os.path.realpath(new_path)

        if not _is_allowed(new_real):
            return "Renamed path would fall outside allowed library roots."
        if os.path.exists(new_path):
            return f"Cannot rename: a file or folder named '{dest}' already exists here."

        if not confirmed:
            return "\n".join([
                "Ready to rename:",
                f"  {os.path.basename(src)}",
                f"  → {dest}",
                f"  (in {os.path.dirname(src)})",
                "",
                "Reply **yes** to confirm.",
            ])

        try:
            os.rename(src, new_path)
            return f"✅ Renamed: {os.path.basename(src)} → {dest}"
        except Exception as e:
            return f"Rename failed: {e}"

    # ── move ──────────────────────────────────────────────────────────────────
    elif action == "move":
        if not dest:
            return "move requires dest — the target directory or full destination path."

        dst = _resolve(dest)

        # If dest is an existing directory, move src inside it
        effective_dst = os.path.join(dst, os.path.basename(src)) if os.path.isdir(dst) else dst
        effective_real = os.path.realpath(effective_dst)

        if not _is_allowed(effective_real):
            return f"Destination not allowed. Must be under: {', '.join(ALLOWED_ROOTS)}"
        if os.path.exists(effective_dst):
            return f"Cannot move: destination already exists: {effective_dst}"

        parent = os.path.dirname(effective_dst)
        if not os.path.isdir(parent):
            return f"Destination parent directory does not exist: {parent}"

        if not confirmed:
            return "\n".join([
                "Ready to move:",
                f"  {src}",
                f"  → {effective_dst}",
                "",
                "Reply **yes** to confirm.",
            ])

        try:
            shutil.move(src, effective_dst)
            return f"✅ Moved: {src} → {effective_dst}"
        except Exception as e:
            return f"Move failed: {e}"

    # ── rename_all ────────────────────────────────────────────────────────────
    elif action == "rename_all":
        if not os.path.isdir(src):
            return f"rename_all requires source to be a directory: {src}"

        pattern = dest or "rip_{n:02d}"

        try:
            entries = sorted(
                f for f in os.listdir(src)
                if os.path.isfile(os.path.join(src, f))
            )
        except OSError as e:
            return f"Could not list directory: {e}"

        if not entries:
            return f"No files found in: {src}"

        # Build rename plan — preserve each file's original extension
        plan: list[tuple[str, str]] = []
        for i, fname in enumerate(entries, start=1):
            ext = os.path.splitext(fname)[1]
            try:
                new_name = pattern.format(n=i) + ext
            except (KeyError, ValueError) as e:
                return (
                    f"Invalid pattern '{pattern}': {e}. "
                    "Use {{n}} as the counter placeholder, e.g. rip_{{n:02d}}."
                )
            plan.append((fname, new_name))

        # Reject patterns that produce duplicates (e.g. a pattern with no {n})
        new_names = [p[1] for p in plan]
        if len(new_names) != len(set(new_names)):
            return (
                "Pattern would produce duplicate filenames — include {n} "
                "as a counter, e.g. rip_{n:02d}."
            )

        if not confirmed:
            col = max(len(old) for old, _ in plan)
            lines = [
                f"Ready to rename {len(plan)} file(s) in:",
                f"  {src}",
                "",
            ]
            for old_name, new_name in plan:
                lines.append(f"  {old_name:<{col}}  →  {new_name}")
            lines += ["", "Reply **yes** to confirm."]
            return "\n".join(lines)

        errors: list[str] = []
        done = 0
        for old_name, new_name in plan:
            if old_name == new_name:
                done += 1
                continue
            old_path = os.path.join(src, old_name)
            new_path = os.path.join(src, new_name)
            try:
                os.rename(old_path, new_path)
                done += 1
            except Exception as e:
                errors.append(f"{old_name}: {e}")

        if errors:
            return f"Renamed {done}/{len(plan)} files. Errors:\n" + "\n".join(errors)
        return f"✅ Renamed {done} file(s) in {src}"

    # ── delete_matching ───────────────────────────────────────────────────────
    elif action == "delete_matching":
        if not os.path.isdir(src):
            return f"delete_matching requires source to be a directory: {src}"
        if not dest:
            return "delete_matching requires dest — a glob pattern like *.srt or *.srt,*.ass,*.sup"

        import fnmatch
        patterns = [p.strip() for p in dest.split(",") if p.strip()]

        # Collect matching files recursively
        matches: list[str] = []
        for dirpath, _, filenames in os.walk(src):
            for fname in sorted(filenames):
                if any(fnmatch.fnmatch(fname, pat) for pat in patterns):
                    matches.append(os.path.join(dirpath, fname))
        matches.sort()

        if not matches:
            return f"No files matching {dest!r} found under {src}"

        total_bytes = sum(
            os.path.getsize(f) for f in matches if os.path.exists(f)
        )

        if not confirmed:
            # Group by subdirectory: show count + size per folder, not individual files
            from collections import defaultdict
            by_dir: dict[str, list[str]] = defaultdict(list)
            for f in matches:
                rel_dir = os.path.relpath(os.path.dirname(f), src)
                by_dir[rel_dir].append(f)

            lines = [
                f"Ready to delete {len(matches)} file(s) matching {dest!r} under:",
                f"  {src}",
                "",
            ]
            for rel_dir in sorted(by_dir):
                dir_files = by_dir[rel_dir]
                dir_bytes = sum(os.path.getsize(f) for f in dir_files if os.path.exists(f))
                display = "." if rel_dir == "." else rel_dir
                lines.append(f"  {display}/  — {len(dir_files)} file(s), {_fmt_bytes(dir_bytes)}")
            lines += [
                "",
                f"Total: {len(matches)} file(s), {_fmt_bytes(total_bytes)}",
                "",
                "⚠️ This cannot be undone. Reply **yes** to confirm.",
            ]
            return "\n".join(lines)

        errors: list[str] = []
        _sync_before_write()
        done = 0
        for f in matches:
            try:
                _remove(f)
                done += 1
            except Exception as e:
                errors.append(f"{os.path.relpath(f, src)}: {e}")

        if errors:
            return (
                f"Deleted {done}/{len(matches)} files. Errors:\n"
                + "\n".join(errors)
            )
        return f"✅ Deleted {done} file(s) matching {dest!r} from {src}"

    # ── merge ─────────────────────────────────────────────────────────────────
    elif action == "merge":
        if not dest:
            return "merge requires dest — the existing target directory to merge files into."

        dst = _resolve(dest)

        if not os.path.isdir(src):
            return f"merge source must be a directory: {src}"
        if not os.path.isdir(dst):
            return f"merge dest must be an existing directory: {dst}"
        if not _is_allowed(dst):
            return f"Destination not allowed. Must be under: {', '.join(ALLOWED_ROOTS)}"
        if _is_root(dst):
            return "Cannot merge into a root library path directly."
        if os.path.realpath(src) == os.path.realpath(dst):
            return "Source and destination are the same directory."

        # Collect all files from source (preserving relative subdirectory structure)
        try:
            src_files: list[tuple[str, str, str]] = []
            for dirpath, _, filenames in os.walk(src):
                for fname in sorted(filenames):
                    full_src = os.path.join(dirpath, fname)
                    rel = os.path.relpath(full_src, src)
                    full_dst = os.path.join(dst, rel)
                    src_files.append((full_src, full_dst, rel))
            src_files.sort(key=lambda t: t[2])
        except OSError as e:
            return f"Could not list source directory: {e}"

        if not src_files:
            return f"Source directory is empty: {src}"

        conflicts = [rel for _, full_dst, rel in src_files if os.path.exists(full_dst)]
        total_bytes = sum(os.path.getsize(s) for s, _, _ in src_files if os.path.exists(s))

        if not confirmed:
            lines = [
                f"Ready to merge {len(src_files)} file(s):",
                f"  from  {src}",
                f"  into  {dst}",
                "",
            ]
            shown = src_files[:20]
            for _, _, rel in shown:
                lines.append(f"  {rel}")
            if len(src_files) > 20:
                lines.append(f"  … and {len(src_files) - 20} more file(s)")
            lines.append(f"\nTotal: {len(src_files)} file(s), {_fmt_bytes(total_bytes)}")
            lines.append("Source folder will be removed after merge.")
            if conflicts:
                lines.append(f"\n⚠️ {len(conflicts)} filename conflict(s) — these already exist in dest:")
                for rel in conflicts[:5]:
                    lines.append(f"  {rel}")
                if len(conflicts) > 5:
                    lines.append(f"  … and {len(conflicts) - 5} more")
                lines.append("\nResolve conflicts first (rename or delete them), then retry.")
                return "\n".join(lines)
            lines.append("\nReply **yes** to confirm.")
            return "\n".join(lines)

        if conflicts:
            return (
                f"Cannot merge: {len(conflicts)} filename conflict(s) exist in destination. "
                "Resolve them first."
            )

        _sync_before_write()
        errors: list[str] = []
        done = 0
        for full_src, full_dst, rel in src_files:
            try:
                os.makedirs(os.path.dirname(full_dst), exist_ok=True)
                shutil.move(full_src, full_dst)
                done += 1
            except Exception as e:
                errors.append(f"{rel}: {e}")

        if errors:
            return f"Merged {done}/{len(src_files)} files. Errors:\n" + "\n".join(errors)

        try:
            shutil.rmtree(src)
        except Exception as e:
            return f"✅ Merged {done} file(s). Warning: could not remove source folder: {e}"
        return (
            f"✅ Merged {done} file(s) from {os.path.basename(src)!r} "
            f"into {os.path.basename(dst)!r} and removed source folder."
        )

    else:
        return f"Unknown action '{action}'. Use: delete, delete_matching, merge, rename, rename_all, or move."


def query_media_library(action: str, path: str = "", pattern: str = "",
                        limit: int = 20, file_type: str = "video") -> str:
    """Inspect files in the media library or staging area."""
    ALLOWED_ROOTS = [p for p in [MEDIA_PATH, STAGING_PATH] if p]

    def _is_allowed(p: str) -> bool:
        rp = os.path.realpath(p)
        return any(rp.startswith(os.path.realpath(root)) for root in ALLOWED_ROOTS)

    def _fmt_duration(seconds: float) -> str:
        s = int(seconds)
        h, rem = divmod(s, 3600)
        m, sec = divmod(rem, 60)
        return f"{h}:{m:02d}:{sec:02d}"

    VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".ts", ".wmv", ".flv", ".mpg", ".mpeg"}

    if action == "list_dir":
        target = path if path else MEDIA_PATH
        if not os.path.isabs(target):
            target = os.path.join(MEDIA_PATH, target)
        target = os.path.normpath(target)
        if not _is_allowed(target):
            return f"Path not allowed. Must be under: {', '.join(ALLOWED_ROOTS)}"
        if not os.path.isdir(target):
            return f"Directory not found: {target}"

        try:
            entries = sorted(os.scandir(target), key=lambda e: (not e.is_dir(), e.name.lower()))
        except Exception as e:
            return f"Error listing directory: {e}"

        if not entries:
            return f"{target}: empty directory."

        lines = [f"{target}:"]
        for e in entries:
            if e.is_dir():
                lines.append(f"  [dir]  {e.name}/")
            else:
                try:
                    size = _fmt_bytes(e.stat().st_size)
                except Exception:
                    size = "?"
                lines.append(f"  {size:>10}  {e.name}")
        return "\n".join(lines)

    elif action == "file_info":
        if not path:
            return "file_info requires a path."
        full_path = path if os.path.isabs(path) else os.path.join(MEDIA_PATH, path)
        full_path = os.path.normpath(full_path)
        if not _is_allowed(full_path):
            return f"Path not allowed. Must be under: {', '.join(ALLOWED_ROOTS)}"
        if not os.path.exists(full_path):
            return f"File not found: {full_path}"

        try:
            r = subprocess.run(
                ["ffprobe", "-v", "quiet", "-print_format", "json",
                 "-show_format", "-show_streams", full_path],
                capture_output=True, text=True, timeout=30,
            )
            if r.returncode != 0:
                return f"ffprobe error: {r.stderr.strip() or 'unknown error'}"

            data = json.loads(r.stdout)
            fmt = data.get("format", {})
            streams = data.get("streams", [])

            size_bytes = int(fmt.get("size", 0))
            duration_s = float(fmt.get("duration", 0) or 0)
            bitrate_bps = int(fmt.get("bit_rate", 0) or 0)

            lines = [
                f"File:     {os.path.basename(full_path)}",
                f"Size:     {_fmt_bytes(size_bytes)}",
                f"Duration: {_fmt_duration(duration_s)}",
            ]
            if bitrate_bps:
                lines.append(f"Bitrate:  {bitrate_bps // 1000:,} kbps  "
                              f"({bitrate_bps // 1_000_000:.1f} Mbps)")

            for stream in streams:
                ctype = stream.get("codec_type", "")
                cname = stream.get("codec_name", "?")
                if ctype == "video":
                    w = stream.get("width", "?")
                    h = stream.get("height", "?")
                    fps_raw = stream.get("r_frame_rate", "")
                    try:
                        n, d = fps_raw.split("/")
                        fps = f"{int(n)/int(d):.2f} fps"
                    except Exception:
                        fps = fps_raw
                    vbr = stream.get("bit_rate")
                    vbr_str = f"  {int(vbr)//1000:,} kbps" if vbr else ""
                    profile = stream.get("profile", "")
                    profile_str = f" [{profile}]" if profile else ""
                    lines.append(f"Video:    {cname}{profile_str}  {w}x{h}  {fps}{vbr_str}")
                elif ctype == "audio":
                    ch = stream.get("channels", "?")
                    sr = stream.get("sample_rate", "?")
                    lang = (stream.get("tags") or {}).get("language", "")
                    lang_str = f" [{lang}]" if lang else ""
                    abr = stream.get("bit_rate")
                    abr_str = f"  {int(abr)//1000} kbps" if abr else ""
                    lines.append(f"Audio:    {cname}  {ch}ch  {sr} Hz{abr_str}{lang_str}")
                elif ctype == "subtitle":
                    lang = (stream.get("tags") or {}).get("language", "")
                    lang_str = f" [{lang}]" if lang else ""
                    lines.append(f"Subtitle: {cname}{lang_str}")

            return "\n".join(lines)

        except FileNotFoundError:
            return "ffprobe not found — install with: sudo apt install ffmpeg"
        except Exception as e:
            return f"Error reading file metadata: {e}"

    elif action == "find_files":
        root = path if path else MEDIA_PATH
        if not os.path.isabs(root):
            root = os.path.join(MEDIA_PATH, root)
        root = os.path.normpath(root)
        if not _is_allowed(root):
            return f"Path not allowed. Must be under: {', '.join(ALLOWED_ROOTS)}"
        if not os.path.isdir(root):
            return f"Directory not found: {root}"

        limit = min(max(limit, 1), 100)
        entries = []
        for dirpath, _, files in os.walk(root):
            for fname in files:
                if pattern and pattern.lower() not in fname.lower():
                    continue
                # File-type filter
                ext = os.path.splitext(fname)[1].lower()
                if file_type == "video" and ext not in VIDEO_EXTS:
                    continue
                full = os.path.join(dirpath, fname)
                try:
                    stat = os.stat(full)
                    rel = os.path.relpath(full, root)
                    entries.append((rel, stat.st_size, stat.st_mtime, ext))
                except Exception:
                    pass

        if not entries:
            filter_note = f" ({file_type} files)" if file_type == "video" else ""
            msg = f"No{filter_note} files found under {root}"
            return msg + (f" matching '{pattern}'" if pattern else "") + "."

        entries.sort(key=lambda x: -x[2])  # newest first
        shown = entries[:limit]
        total = len(entries)
        header_label = "Video files" if file_type == "video" else "All files"
        lines = [f"{header_label} in {root}  ({total} total, showing {len(shown)}):"]
        for rel, size, mtime, ext in shown:
            dt = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d")
            kind = "[VIDEO]" if ext in VIDEO_EXTS else "[OTHER]"
            lines.append(f"  {rel}  [{_fmt_bytes(size)}]  modified {dt}  {kind}")
        return "\n".join(lines)

    else:
        return f"Unknown action '{action}'. Use: file_info, find_files"


def get_system_stats() -> str:
    """CPU load, memory, and GPU stats."""
    parts = []

    try:
        load = os.getloadavg()
        parts.append(f"CPU load (1/5/15 min): {load[0]:.2f}  {load[1]:.2f}  {load[2]:.2f}")
    except Exception as e:
        parts.append(f"CPU load: unavailable ({e})")

    try:
        with open("/proc/meminfo") as f:
            mi = {l.split(":")[0]: l.split(":")[1].strip()
                  for l in f.read().splitlines() if ":" in l}
        total = mi.get("MemTotal", "?")
        avail = mi.get("MemAvailable", "?")
        parts.append(f"Memory: {avail} free of {total}")
    except Exception as e:
        parts.append(f"Memory: unavailable ({e})")

    try:
        r = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=name,temperature.gpu,memory.used,memory.total,utilization.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10
        )
        name, temp, mem_used, mem_total, util = [x.strip() for x in r.stdout.strip().split(",")]
        parts.append(f"GPU: {name} | {temp}°C | VRAM {mem_used}/{mem_total} MiB | {util}% util")
    except Exception as e:
        parts.append(f"GPU: unavailable ({e})")

    return "\n".join(parts)


def get_hardware_info() -> str:
    """Query motherboard model, CPU, GPU, RAM capacity, and disk model info."""
    parts = ["=== Hardware Information ==="]

    # Motherboard — read from sysfs DMI entries (no root needed)
    try:
        vendor = ""
        product = ""
        version = ""
        for path, key in [
            ("/sys/devices/virtual/dmi/id/board_vendor", "vendor"),
            ("/sys/devices/virtual/dmi/id/board_name", "product"),
            ("/sys/devices/virtual/dmi/id/board_version", "version"),
        ]:
            try:
                with open(path) as f:
                    val = f.read().strip()
                    if key == "vendor":
                        vendor = val
                    elif key == "product":
                        product = val
                    elif key == "version":
                        version = val
            except (FileNotFoundError, PermissionError, OSError):
                pass
        if vendor and product:
            mobo = f"Motherboard: {vendor} {product}"
            if version and version not in ("To be filled by O.E.M.", ""):
                mobo += f" ({version})"
            parts.append(mobo)
        else:
            parts.append("Motherboard: info not available")
    except Exception as e:
        parts.append(f"Motherboard: error ({e})")

    # CPU
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("model name"):
                    cpu_model = line.split(":", 1)[1].strip()
                    # Count cores
                    core_count = 0
                    with open("/proc/cpuinfo") as f2:
                        for l2 in f2:
                            if l2.startswith("processor"):
                                core_count += 1
                    parts.append(f"CPU: {cpu_model} ({core_count} cores)")
                    break
    except Exception as e:
        parts.append(f"CPU: unavailable ({e})")

    # GPU
    try:
        r = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=name,memory.total,driver_version",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            name, vram, driver = [x.strip() for x in r.stdout.strip().split(",")]
            parts.append(f"GPU: {name} ({vram} MiB VRAM) — driver {driver}")
        else:
            # Fallback: try lspci
            r2 = subprocess.run(
                ["lspci"],
                capture_output=True, text=True, timeout=10,
            )
            for line in r2.stdout.splitlines():
                if "VGA" in line or "3D" in line:
                    parts.append(f"GPU: {line.strip()}")
                    break
    except FileNotFoundError:
        parts.append("GPU: nvidia-smi not found (no NVIDIA driver?)")
    except subprocess.TimeoutExpired:
        parts.append("GPU: nvidia-smi timed out")
    except Exception as e:
        parts.append(f"GPU: error ({e})")

    # RAM — from /proc/meminfo (no root needed)
    try:
        mem_total_kb = 0
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    mem_total_kb = int(line.split(":")[1].strip().split()[0])
                    break
        if mem_total_kb > 0:
            parts.append(f"RAM: {mem_total_kb // 1048576} GB usable")
        else:
            parts.append("RAM: unable to determine")
    except Exception as e:
        parts.append(f"RAM: error ({e})")

    # Disk storage — physical drives
    try:
        r = subprocess.run(
            ["lsblk", "-o", "NAME,SIZE,TYPE,MODEL,MOUNTPOINT", "-d", "-e", "7,11"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            lines = [l for l in r.stdout.splitlines() if l.strip()]
            parts.append("Storage:")
            for line in lines:
                parts.append(f"  {line}")
    except Exception as e:
        parts.append(f"Storage: error ({e})")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Gaming tools
# ---------------------------------------------------------------------------

def _parse_acf(path: str) -> dict:
    """Parse a Steam ACF manifest into a flat dict of lowercase keys → values."""
    result: dict = {}
    with open(path) as fh:
        for line in fh:
            m = re.match(r'^\s*"([^"]+)"\s+"([^"]*)"', line)
            if m:
                result[m.group(1).lower()] = m.group(2)
    return result


def _load_steam_games() -> list[dict]:
    """Return a list of dicts for every installed Steam game (from ACF manifests)."""
    games = []
    for acf_path in _glob.glob(os.path.join(STEAM_LIBRARY_PATH, "appmanifest_*.acf")):
        try:
            d = _parse_acf(acf_path)
            games.append({
                "appid":      d.get("appid", "?"),
                "name":       d.get("name", "Unknown"),
                "installdir": d.get("installdir", ""),
                "size":       int(d.get("sizeondisk", 0)),
                "last_played": int(d.get("lastplayed", 0)),
                "acf_path":   acf_path,
            })
        except Exception:
            continue
    return games


def query_steam(action: str = "library") -> str:
    """List installed Steam games or show disk usage breakdown."""
    games = _load_steam_games()
    if not games:
        return "No installed Steam games found."

    if action == "library":
        games.sort(key=lambda g: g["name"].lower())
        lines = [f"**Steam Library** — {len(games)} games\n"]
        for g in games:
            size_str = f"{g['size'] / 1024**3:.1f} GB"
            if g["last_played"]:
                lp = datetime.datetime.fromtimestamp(g["last_played"]).strftime("%Y-%m-%d")
                played = f"last played {lp}"
            else:
                played = "never played"
            lines.append(f"• {g['name']} — {size_str}, {played}")
        return "\n".join(lines)

    elif action == "disk_usage":
        games.sort(key=lambda g: g["size"], reverse=True)
        total = sum(g["size"] for g in games)
        lines = [f"**Steam Disk Usage** — {total / 1024**3:.1f} GB total\n"]
        for g in games:
            pct = g["size"] / total * 100 if total else 0
            lines.append(f"• {g['name']} — {g['size'] / 1024**3:.1f} GB ({pct:.0f}%)")
        return "\n".join(lines)

    return f"Unknown action: {action}"


def manage_steam(action: str, game: str = "", confirmed: bool = False) -> str:
    """Remove an installed Steam game (with confirmation)."""
    if action != "remove":
        return f"Unknown action: {action}"
    if not game:
        return "Specify a game name to remove."

    games = _load_steam_games()
    matches = [g for g in games if game.lower() in g["name"].lower()]

    if not matches:
        names = ", ".join(g["name"] for g in games)
        return f"No game matching '{game}' found. Installed: {names}"
    if len(matches) > 1:
        return f"Multiple matches for '{game}': {', '.join(m['name'] for m in matches)}. Be more specific."

    g = matches[0]
    game_dir = os.path.join(STEAM_LIBRARY_PATH, "common", g["installdir"])
    size_gb = g["size"] / 1024**3

    if not confirmed:
        return "\n".join([
            f"**Remove '{g['name']}'?**",
            f"• Folder: `{game_dir}`",
            f"• Manifest: `{g['acf_path']}`",
            f"• Size: {size_gb:.1f} GB",
            "",
            "Reply **yes** to confirm.",
        ])

    errors = []
    if os.path.isdir(game_dir):
        try:
            shutil.rmtree(game_dir)
        except Exception as e:
            errors.append(f"folder removal failed: {e}")
    if os.path.exists(g["acf_path"]):
        try:
            os.remove(g["acf_path"])
        except Exception as e:
            errors.append(f"manifest removal failed: {e}")

    if errors:
        return "⚠️ Partial removal: " + "; ".join(errors)
    return f"✅ '{g['name']}' removed — {size_gb:.1f} GB freed."


def shutdown_steam() -> str:
    """Gracefully shut down Steam; force-kills after 10 s if it doesn't respond."""
    import time

    check = subprocess.run(["pgrep", "-x", "steam"], capture_output=True)
    if check.returncode != 0:
        return "Steam is not running."

    subprocess.run(["/usr/games/steam", "-shutdown"], capture_output=True)
    time.sleep(10)

    check2 = subprocess.run(["pgrep", "-x", "steam"], capture_output=True)
    if check2.returncode != 0:
        return "✅ Steam shut down cleanly."

    # Still running — force kill
    subprocess.run(["pkill", "-KILL", "-f", "steam"], capture_output=True)
    time.sleep(2)
    check3 = subprocess.run(["pgrep", "-x", "steam"], capture_output=True)
    if check3.returncode != 0:
        return "✅ Steam force-killed (it didn't respond to graceful shutdown)."

    return "⚠️ Could not stop Steam — processes may still be running."


def launch_steam() -> str:
    """Launch Steam in Big Picture mode on the server's local display."""
    check = subprocess.run(["pgrep", "-x", "steam"], capture_output=True)
    if check.returncode == 0:
        return "Steam is already running."

    # Must run as genesis — Steam lives in genesis's home. discord-bot has a
    # sudoers entry granting NOPASSWD access to /usr/games/steam as genesis.
    subprocess.Popen(
        ["sudo", "-u", "genesis", "-H", "/usr/games/steam", "-gamepadui"],
        env={"DISPLAY": ":0", "HOME": "/home/genesis", "USER": "genesis",
             "XDG_RUNTIME_DIR": "/run/user/1000",
             "PULSE_SERVER": "unix:/run/user/1000/pulse/native",
             "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/usr/games"},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return "✅ Steam launching in Big Picture mode."


def query_system(aspect: str = "stats", limit: int = 20) -> str:
    """Unified system query — dispatches to the underlying health/storage/network functions."""
    if aspect in ("stats", "failed", "updates", "processes", "smart"):
        return query_system_health(aspect)
    elif aspect == "storage_usage":
        return query_storage("usage", limit)
    elif aspect == "storage_breakdown":
        return query_storage("breakdown", limit)
    elif aspect == "storage_largest":
        return query_storage("largest", limit)
    elif aspect == "network_tailscale":
        return query_network("tailscale")
    elif aspect == "network_ip":
        return query_network("external_ip")
    elif aspect == "network_ports":
        return query_network("ports")
    elif aspect == "hardware":
        return get_hardware_info()
    return f"Unknown aspect: {aspect}"


def query_jenkins(action: str, job_name: str | None = None,
                  build_number: int | None = None, count: int = 10,
                  since_days: int | None = None, lines: int = 100) -> str:
    """Unified Jenkins query — dispatches to status/history/log functions."""
    if action == "status":
        return get_jenkins_build_status(job_name)
    elif action == "history":
        return get_jenkins_build_history(job_name=job_name, count=count, since_days=since_days)
    elif action == "log":
        return get_jenkins_build_log(job_name=job_name, build_number=build_number, lines=lines)
    return f"Unknown action: {action}"


# ---------------------------------------------------------------------------
# Crawl analytics
# ---------------------------------------------------------------------------

def query_crawl_analytics(action: str = "summary") -> str:
    """Query the configured AI crawl analytics endpoint."""
    if not CRAWL_ANALYTICS_URL or not CRAWL_ANALYTICS_TOKEN:
        return "Crawl analytics is not configured (missing CRAWL_ANALYTICS_URL or CRAWL_ANALYTICS_TOKEN)."
    base = CRAWL_ANALYTICS_URL.rstrip("/")
    if action == "summary":
        url = f"{base}/visits?token={CRAWL_ANALYTICS_TOKEN}"
    elif action == "export":
        url = f"{base}/visits/export?token={CRAWL_ANALYTICS_TOKEN}"
    else:
        return f"Unknown action: {action!r}. Use 'summary' or 'export'."
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        if action == "export":
            return resp.text[:5000]
        return json.dumps(resp.json(), indent=2)
    except Exception as e:
        return f"Failed to fetch crawl analytics: {e}"


# ---------------------------------------------------------------------------
# LLM usage / cost query
# ---------------------------------------------------------------------------

def query_llm_usage(action: str = "recent", days: int = 30, limit: int = 20) -> str:
    """Query the LLM usage log — token counts and estimated API cost."""
    from pandabot_core.llm import usage as _llm
    return _llm.query_usage(action=action, days=days, limit=limit)


# ---------------------------------------------------------------------------
# GitHub Issues (project-management backend; thin wrappers over pandabot_core.pm.github)
# ---------------------------------------------------------------------------

def list_github_issues(repo: str, status: str = "open", limit: int = 25) -> str:
    return _ghpm.list_issues(repo, state=status, limit=limit)


def get_github_issue(repo: str, number: int) -> str:
    return _ghpm.get_issue(repo, number)


def list_github_sub_issues(repo: str, number: int) -> str:
    return _ghpm.list_sub_issues(repo, number)


def search_github_issues(query: str, repo: str = "", limit: int = 25) -> str:
    return _ghpm.search_issues(query, repo=repo, limit=limit)


def list_github_milestones(repo: str) -> str:
    return _ghpm.list_milestones(repo)


def create_github_issue(repo: str, title: str, body: str = "", labels: str = "",
                        assignee: str = "", milestone: int = 0, parent: int = 0) -> str:
    return _ghpm.create_issue(repo, title, body=body, labels=labels,
                              assignee=assignee, milestone=milestone, parent=parent)


def update_github_issue(repo: str, number: int, title: str = "", body: str = "",
                        state: str = "", labels: str = "", assignee: str = "",
                        milestone: int = 0) -> str:
    return _ghpm.update_issue(repo, number, title=title, body=body, state=state,
                              labels=labels, assignee=assignee, milestone=milestone)


# ---------------------------------------------------------------------------
# Model switching (local LLM support)
# ---------------------------------------------------------------------------

# Maps the friendly names a user might say to profile names defined in .env.
# Profile names come from PANDABOT_PROFILE_<NAME>_TYPE env var keys (lowercased).
_MODEL_ALIASES: dict[str, str] = {
    "gemma":    os.environ.get("LOCAL_LLM_PROFILE_NAME", "gemma"),
    "local":    os.environ.get("LOCAL_LLM_PROFILE_NAME", "gemma"),
    "deepseek": os.environ.get("DEEPSEEK_PROFILE_NAME",  "deepseek"),
    "haiku":    os.environ.get("HAIKU_PROFILE_NAME",      "haiku"),
    "claude":   os.environ.get("HAIKU_PROFILE_NAME",      "haiku"),
    "fast":     os.environ.get("HAIKU_PROFILE_NAME",      "haiku"),
}


def query_model_status() -> str:
    """Return the currently active LLM profile and available options."""
    from pandabot_core.llm.provider import get_active_profile_name, get_available_profiles, get_provider
    name = get_active_profile_name()
    provider = get_provider()
    available = get_available_profiles()
    return (
        f"Active profile: {name} (model: {provider.primary_model})\n"
        f"Available profiles: {', '.join(available)}"
    )


def switch_model(model_name: str) -> str:
    """Switch the active LLM model profile."""
    from pandabot_core.llm.provider import (
        get_available_profiles, set_active_profile, get_active_profile_name,
    )
    target = _MODEL_ALIASES.get(model_name.lower().strip(), model_name.lower().strip())
    available = get_available_profiles()
    if target not in available:
        return (
            f"Unknown model '{model_name}'. "
            f"Available profiles: {', '.join(available)}."
        )
    set_active_profile(target)
    return f"Switched to **{target}**. Subsequent messages will use this model."


# ---------------------------------------------------------------------------
# Tool schema definitions for Claude — built dynamically from feature flags
# ---------------------------------------------------------------------------

def _build_tool_definitions() -> list[dict]:
    """Construct the tool list Claude sees, gated by feature flags."""

    # Log names available for tailing — built from current whitelists
    _all_log_names = sorted(list(ALLOWED_FILE_LOGS.keys()) + list(ALLOWED_DOCKER_LOGS))

    # query_system aspects
    _system_aspects = [
        "stats", "failed", "updates", "processes",
        "storage_usage", "storage_breakdown", "storage_largest",
        "network_tailscale", "network_ip", "network_ports",
        "hardware",
    ]
    _smart_desc = ""
    if ENABLE_SMART and SMART_DEVICES:
        _system_aspects.append("smart")
        _device_summary = "; ".join(f"{dev} ({label})" for dev, label in SMART_DEVICES)
        _smart_desc = (
            f"smart: SMART drive health ({_device_summary}) — "
            "reallocated sectors, pending sectors, power-on hours, temperature, SSD wear counters. "
        )

    tools = [
        # --- System (health + storage + network) ---
        {
            "name": "query_system",
            "description": (
                "Check system health, storage, network, or hardware info. "
                "stats: CPU load average, memory, GPU temp/VRAM/utilisation. "
                "failed: systemd units in a failed state. "
                "updates: apt packages available to upgrade. "
                "processes: top 15 processes by CPU. "
                + _smart_desc +
                "storage_usage: df -h for / and /mnt/media — overall free/used space. "
                "storage_breakdown: du per top-level folder under /mnt/media (Movies, Shows, Music, Video). "
                "storage_largest: top N largest files under /mnt/media — useful for reclaiming space. "
                "network_tailscale: Tailscale peer list with online/offline status and IPs. "
                "network_ip: current public IP address of the server. "
                "network_ports: listening TCP ports and the processes bound to them. "
                "hardware: motherboard model, CPU model/core count, GPU model/VRAM/driver, "
                "RAM capacity/type/speed per DIMM, and physical disk models/sizes."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "aspect": {
                        "type": "string",
                        "enum": _system_aspects,
                        "description": "Which aspect to check. Default: stats.",
                        "default": "stats",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "storage_largest only: how many files to return (1–50, default 20).",
                        "default": 20,
                    },
                },
                "required": [],
            },
        },
        # --- Log tailing ---
        {
            "name": "get_log_tail",
            "description": (
                "Retrieve the last N lines from an allowed service log. "
                f"Available logs: {', '.join(_all_log_names) or 'none configured'}."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "log_name": {
                        "type": "string",
                        "enum": _all_log_names,
                        "description": "Which service log to read.",
                    },
                    "lines": {
                        "type": "integer",
                        "description": "How many lines to return (1–200, default 50).",
                        "default": 50,
                    },
                },
                "required": ["log_name"],
            },
        },
        # --- Service status ---
        {
            "name": "get_service_status",
            "description": (
                "Check whether a system service or Docker container is running. "
                f"Available services: {', '.join(ALL_SERVICES)}."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "service_name": {
                        "type": "string",
                        "enum": ALL_SERVICES,
                        "description": "The service or container to inspect.",
                    },
                },
                "required": ["service_name"],
            },
        },
        # --- Container restart (optional; gated on RESTARTABLE_CONTAINERS) ---
        *([{
            "name": "restart_container",
            "description": (
                "Restart a Docker container. Use when a container is unresponsive or needs a fresh start. "
                f"Allowed containers: {', '.join(sorted(RESTARTABLE_CONTAINERS))}. "
                "Always call with confirmed=False first to show the user what will be restarted. "
                "Only call with confirmed=True after the user explicitly says yes."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "container": {
                        "type": "string",
                        "enum": sorted(RESTARTABLE_CONTAINERS),
                        "description": "The container to restart.",
                    },
                    "confirmed": {
                        "type": "boolean",
                        "description": "Set true only after the user has explicitly confirmed.",
                        "default": False,
                    },
                },
                "required": ["container"],
            },
        }] if RESTARTABLE_CONTAINERS else []),
        # --- Media library ---
        {
            "name": "query_media_library",
            "description": (
                f"Inspect files in the media library ({MEDIA_PATH}) or staging area.\n"
                "list_dir: list all files and subdirectories in a directory (one level, no recursion).\n"
                "file_info: full ffprobe metadata for one file — codec, resolution, duration, "
                "bitrate, and all audio/subtitle tracks. Use this to answer 'why wasn't X "
                "converted?' (check video bitrate — NVENC re-encodes land at ~3–8 Mbps; "
                "original rips are typically 15–40 Mbps) or 'how long is this movie?'.\n"
                "find_files: recursively list files in a directory with sizes, modification "
                f"dates, and file type tags. Defaults to video files only (.mkv, .mp4, .avi, "
                "etc.) so non-video files (ROMs, images, subtitles, metadata) are excluded. "
                "Set file_type='all' to include every file type. "
                "Each result line includes a [VIDEO] or [OTHER] tag for easy identification. "
                "Path can be absolute or relative to {MEDIA_PATH}."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list_dir", "file_info", "find_files"],
                        "description": (
                            "list_dir: shallow directory listing. "
                            "file_info: metadata for one file. "
                            "find_files: recursive file search."
                        ),
                    },
                    "path": {
                        "type": "string",
                        "description": (
                            f"list_dir/find_files: directory to scan (default: {MEDIA_PATH}). "
                            f"file_info: path to the file (absolute or relative to {MEDIA_PATH})."
                        ),
                    },
                    "pattern": {
                        "type": "string",
                        "description": "find_files only: filter to filenames containing this string (case-insensitive).",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "find_files only: max results to return (1–100, default 20).",
                        "default": 20,
                    },
                    "file_type": {
                        "type": "string",
                        "enum": ["video", "all"],
                        "description": (
                            "find_files only: 'video' (default) returns only playable video files "
                            "(.mkv, .mp4, .avi, .m4v, .mov, .ts, .wmv, .flv, .mpg, .mpeg). "
                            "'all' returns every file type (ROMs, images, subtitles, etc.). "
                            "Use 'all' only when explicitly looking for non-video files."
                        ),
                        "default": "video",
                    },
                },
                "required": ["action"],
            },
        },
        # --- Performance history ---
        {
            "name": "get_performance_history",
            "description": (
                "Query historical performance metrics from PCP/pmlogger — the same data "
                "source Cockpit uses for its performance graphs. Returns a time-series CSV "
                "sampled at regular intervals. Use this to answer questions like 'was the "
                "CPU spiking last night?' or 'how much memory was used over the past week?'. "
                "Available metrics: cpu (user/sys/idle rates), memory (used/free bytes), "
                "disk (read/write bytes/s), network (in/out bytes/s per interface). "
                "Max window: 168h (1 week)."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "metric": {
                        "type": "string",
                        "enum": ["cpu", "memory", "disk", "network"],
                        "description": "Which metric category to query.",
                        "default": "cpu",
                    },
                    "hours": {
                        "type": "integer",
                        "description": "How many hours back to look (1–168, default 1). Use 168 for a full week.",
                        "default": 1,
                    },
                },
                "required": [],
            },
        },
        # --- Scheduler ---
        {
            "name": "manage_schedule",
            "description": (
                "Schedule future tasks, list pending ones, or cancel them. "
                "Use this whenever the user asks for something at a future time, on a condition, "
                "or on a recurring schedule — instead of answering immediately. "
                "Decide at schedule time which tools to run and what message to post; "
                "the task fires without an LLM call unless generative_prompt is set.\n"
                "action='create': schedule a new task. Required: fire_at (local ISO, e.g. "
                "'2026-04-18T09:00:00'), description. "
                "task_type: 'one_shot' (default), 'condition_check' (retry until pattern matches), "
                "'recurring' (repeat on recurrence_rule).\n"
                "action='list': show all pending tasks.\n"
                "action='cancel': cancel by id."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["create", "list", "cancel"],
                        "description": "Operation to perform.",
                    },
                    "id": {"type": "integer", "description": "Task id — required for cancel."},
                    "description": {"type": "string", "description": "Human-readable task description."},
                    "fire_at": {
                        "type": "string",
                        "description": "Local ISO datetime for first/only fire: '2026-04-18T09:00:00'.",
                    },
                    "task_type": {
                        "type": "string",
                        "enum": ["one_shot", "condition_check", "recurring"],
                    },
                    "tool_calls": {
                        "type": "array",
                        "description": (
                            "Tools to execute at fire time. "
                            "Each item: {\"tool\": \"tool_name\", \"args\": {...}}. "
                            "Use exact tool names from this tool list."
                        ),
                        "items": {"type": "object"},
                    },
                    "intro_message": {
                        "type": "string",
                        "description": "Static text posted before tool results.",
                    },
                    "static_message": {
                        "type": "string",
                        "description": "Fully pre-written message — posted as-is, no tools run. "
                                       "Use for jokes, reminders, pre-generated summaries.",
                    },
                    "generative_prompt": {
                        "type": "string",
                        "description": "Prompt for a small Haiku call at fire time. "
                                       "Use {results} to include tool output. "
                                       "Only use when dynamic synthesis is needed.",
                    },
                    "condition_pattern": {
                        "type": "string",
                        "description": "Regex matched against combined tool output. "
                                       "Task is done when it matches.",
                    },
                    "met_message": {"type": "string", "description": "Posted when condition is satisfied."},
                    "not_met_message": {"type": "string", "description": "Posted when condition not yet met (will retry)."},
                    "max_attempts": {
                        "type": "integer",
                        "description": "Max retries for condition_check before giving up (default 5).",
                    },
                    "check_interval_minutes": {
                        "type": "integer",
                        "description": "Minutes between condition_check retries. "
                                       "Set based on expected duration (rip ~30, subtitle scan ~120).",
                    },
                    "recurrence_rule": {
                        "type": "string",
                        "description": "For recurring tasks. 'monthly:D' fires on day D each month. "
                                       "'weekly:W' fires each week (W: 0=Mon…6=Sun, same time as fire_at).",
                    },
                },
                "required": ["action"],
            },
        },
    ]

    # --- LLM usage / cost ---
    tools.append({
        "name": "query_llm_usage",
        "description": (
            "Query the bot's own LLM API usage log — token counts and estimated USD cost. "
            "Use this to answer questions like 'how much did we spend on Claude last month?', "
            "'how much did that last question cost?', or 'which model costs the most?'. "
            "action='recent': last N conversations with per-conversation cost (newest first). "
            "action='daily': cost per day for the last `days` days. "
            "action='monthly': cost per calendar month (all time). "
            "action='by_model': cost and token breakdown per model for the last `days` days. "
            "Costs are estimates based on published Anthropic pricing and may not reflect "
            "cache discounts or pricing changes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["recent", "daily", "monthly", "by_model"],
                    "description": "Which report to run. Default: recent.",
                    "default": "recent",
                },
                "days": {
                    "type": "integer",
                    "description": "For daily/by_model: how many days back to include (default 30).",
                    "default": 30,
                },
                "limit": {
                    "type": "integer",
                    "description": "For recent: max conversations to show (default 20).",
                    "default": 20,
                },
            },
            "required": [],
        },
    })

    # --- Jenkins tools (gated) ---
    if ENABLE_JENKINS:
        _jobs_str = ", ".join(JENKINS_JOBS)
        tools.append({
            "name": "trigger_jenkins_job",
            "description": (
                "Trigger a Jenkins job to run immediately. "
                "Returns a confirmation, the estimated build duration from recent history, "
                "and scheduling hints (initial wait + recheck interval). "
                "After triggering, ALWAYS use manage_schedule to create a condition_check task so the "
                "user gets a follow-up notification when the build finishes — separate from any "
                "Jenkins webhook messages. "
                "Pattern: trigger → manage_schedule(condition_check, tool_calls=[query_jenkins(action=status)], "
                "condition_pattern='\"result\":\\s*\"(SUCCESS|FAILURE|UNSTABLE|ABORTED)\"', "
                "generative_prompt='Jenkins job finished. Summarise the outcome in 1–2 sentences from {{results}}.'). "
                f"Known jobs: {_jobs_str}."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "job_name": {
                        "type": "string",
                        "description": f"Exact Jenkins job name. Known jobs: {_jobs_str}.",
                    },
                },
                "required": ["job_name"],
            },
        })
        tools.append({
            "name": "set_jenkins_schedule",
            "description": (
                f"View or change the cron timer schedule for a Jenkins job. Known jobs: {_jobs_str}.\n"
                "Call with no schedule to view the current schedule.\n"
                "Call with a schedule + confirmed=false to preview the change and ask the user to confirm.\n"
                "Call with confirmed=true only after the user explicitly says 'yes'.\n"
                "Use standard Jenkins cron syntax (e.g. 'H * * * *' = every hour, "
                "'H 3 * * *' = daily at 3am, 'H/15 * * * *' = every 15 min). "
                "Use 'disabled' to remove the scheduled trigger entirely."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "job_name": {
                        "type": "string",
                        "description": f"Exact Jenkins job name. Known jobs: {_jobs_str}.",
                    },
                    "schedule": {
                        "type": "string",
                        "description": (
                            "Jenkins cron expression (e.g. 'H * * * *'), "
                            "'disabled' to remove the trigger, "
                            "or omit/empty to view the current schedule without changing it."
                        ),
                    },
                    "confirmed": {
                        "type": "boolean",
                        "description": "Set true only after the user has explicitly confirmed the change.",
                        "default": False,
                    },
                },
                "required": ["job_name"],
            },
        })
        tools.append({
            "name": "query_jenkins",
            "description": (
                "Query Jenkins build information. "
                "status: snapshot of all jobs (omit job_name) or one job's latest build result. "
                "history: list of recent builds with results, start times, and durations. "
                "Use since_days=7 for weekly digests — includes a pass/fail summary. "
                "log: console output for a build (latest if build_number omitted). "
                f"Known jobs: {_jobs_str}."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["status", "history", "log"],
                        "description": "What to query.",
                    },
                    "job_name": {
                        "type": "string",
                        "description": f"Job name. Required for history and log. Omit for all-jobs status overview. Known jobs: {_jobs_str}.",
                    },
                    "build_number": {
                        "type": "integer",
                        "description": "log only: specific build number to fetch. Omit for the latest build.",
                    },
                    "count": {
                        "type": "integer",
                        "description": "history only: number of recent builds to return (default 10). Ignored when since_days is set.",
                        "default": 10,
                    },
                    "since_days": {
                        "type": "integer",
                        "description": "history only: return all builds from the last N days with a pass/fail summary.",
                    },
                    "lines": {
                        "type": "integer",
                        "description": "log only: lines from the end of the log to return (default 100, max 300).",
                        "default": 100,
                    },
                },
                "required": ["action"],
            },
        })

    # --- Jellyfin tools (gated) ---
    if ENABLE_JELLYFIN:
        tools.append({
            "name": "query_jellyfin",
            "description": (
                "Query the Jellyfin media server. "
                "stats: library counts (movies, shows, episodes, music). "
                "recent: last 10 items added to the library. "
                "week: movies, shows, and music albums added in the last 7 days — use this for weekly digests. "
                "streams: active playback sessions — who is watching what, "
                "DirectPlay vs Transcode, whether NVENC is in use. "
                "history: recently watched titles per user. "
                "search_movies: full movie list with genres, community rating, and overview — "
                "use this for any question about what movies are in the library, "
                "genre or mood recommendations (horror, comedy, stoner, 80s, etc.), "
                "or finding movies by description."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "query_type": {
                        "type": "string",
                        "enum": ["stats", "recent", "week", "streams", "history", "search_movies"],
                        "description": "What to query.",
                        "default": "stats",
                    },
                },
                "required": [],
            },
        })

        # --- Music tools (voice gateway uses these to drive the player) ---
        tools.append({
            "name": "play_music",
            "description": (
                "Play music from the Jellyfin library. Use this whenever the user asks to "
                "play music. Extract structured params from their utterance: "
                "track (song title), album (album name), artist. Examples: "
                "'play Bob Marley' -> artist='Bob Marley'; "
                "'play Legend by Bob Marley' -> album='Legend', artist='Bob Marley'; "
                "'play Three Little Birds by Bob Marley' -> track='Three Little Birds', "
                "artist='Bob Marley'. "
                "If only an artist is given, the library shuffles that artist's tracks. "
                "When a name could be either an album or a song, prefer album. "
                "PLAYLIST PRIORITY: when only a track name is given (no artist), the tool "
                "checks playlists first — if a playlist with that name exists it plays instead. "
                "For an explicit playlist request ('play the Disco Favorites playlist'), "
                "prefer play_playlist. "
                "If the tool returns a not-found message (starts with 'I searched for'), "
                "speak it back to the user verbatim so they know the gateway heard the "
                "request correctly and the failure was a library miss, not a misunderstanding. "
                "CASTING: If the user says to cast or send the music to a device (e.g. 'cast it "
                "to the living room', 'play on the TV'), set cast_target to the EXACT device "
                "name from the Chromecast device list in the system prompt. Fuzzy-match their "
                "utterance to the list. If ambiguous between multiple devices, respond with the "
                "available names and ask them to be more specific — do not call this tool yet. "
                "If no Chromecast devices are listed, tell the user none are found on the network."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "track": {"type": "string", "description": "Song title, if the user named one."},
                    "album": {"type": "string", "description": "Album name, if the user named one."},
                    "artist": {"type": "string", "description": "Artist name, if the user named one."},
                    "cast_target": {"type": "string", "description": "Exact Chromecast device name to cast to, if the user asked to cast."},
                },
                "required": [],
            },
        })
        tools.append({
            "name": "pause_music",
            "description": "Pause the currently playing music. Use when the user says 'pause', 'pause the music', 'hold on', etc.",
            "input_schema": {"type": "object", "properties": {}, "required": []},
        })
        tools.append({
            "name": "resume_music",
            "description": "Resume music that was previously paused or stopped. Use when the user says 'resume music', 'continue playing', 'keep going', etc.",
            "input_schema": {"type": "object", "properties": {}, "required": []},
        })
        tools.append({
            "name": "skip_track",
            "description": "Skip to the next track in the current music queue. Use when the user says 'skip', 'next', 'next song', 'skip this one', etc.",
            "input_schema": {"type": "object", "properties": {}, "required": []},
        })
        tools.append({
            "name": "previous_track",
            "description": "Play the previous track in the current music queue. Use when the user says 'back', 'previous', 'previous song', 'go back', 'last song'.",
            "input_schema": {"type": "object", "properties": {}, "required": []},
        })
        tools.append({
            "name": "stop_music",
            "description": "Soft-stop the music — pauses with the queue + position saved so 'resume music' picks it back up later. Use when the user says 'stop' (alone), 'stop the music' (without 'playing'), 'hold on'. For a full exit that clears the now-playing card, use exit_music instead.",
            "input_schema": {"type": "object", "properties": {}, "required": []},
        })
        tools.append({
            "name": "exit_music",
            "description": "Fully exit music mode — drops the queue, clears the now-playing card. After this, 'resume music' will say there's nothing to resume. Use when the user says 'stop playing music', 'exit music', 'turn off the music', 'I'm done with music', 'close the music'.",
            "input_schema": {"type": "object", "properties": {}, "required": []},
        })
        tools.append({
            "name": "set_loop_mode",
            "description": "Set the music loop mode. mode='all' loops the whole queue, mode='one' repeats the current track, mode='off' plays through once. Use when the user says 'loop', 'repeat', 'loop this album', 'repeat this song', 'stop looping', 'turn off repeat'.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "enum": ["off", "all", "one"],
                        "description": "'all' = repeat the queue, 'one' = repeat the current track, 'off' = no repeat.",
                    },
                },
                "required": ["mode"],
            },
        })

        # --- Video cast tool (story #127) ---
        tools.append({
            "name": "play_video",
            "description": (
                "Cast a Jellyfin video (movie or TV show) to a Chromecast device. "
                "Use when the user asks to watch, cast, or play a video on the TV. "
                "Examples: 'cast Tomorrowland to the TV', 'play Breaking Bad on the living room TV', "
                "'watch Inception on the bedroom TV'. "
                "Extract the title from their utterance and set media_type to 'movie' (default) or "
                "'show'/'series'. For a show with no episode specified, the tool picks the next "
                "unwatched episode automatically. "
                "CASTING: cast_target is REQUIRED — set it to the EXACT device name from the "
                "Chromecast device list in the system prompt. Fuzzy-match their utterance to the list. "
                "If ambiguous between multiple devices, list the available names and ask — do NOT "
                "call this tool yet. If no Chromecast devices are listed, tell the user none are found. "
                "If the tool returns a not-found message (starts with 'I searched for'), speak it back "
                "verbatim so the user knows it was a library miss, not a mishearing."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Movie or show title to search for."},
                    "media_type": {
                        "type": "string",
                        "enum": ["movie", "show"],
                        "description": "'movie' (default) or 'show'/'series'.",
                        "default": "movie",
                    },
                    "cast_target": {
                        "type": "string",
                        "description": "Exact Chromecast device name from the system prompt device list.",
                    },
                },
                "required": ["title", "cast_target"],
            },
        })

        tools.append({
            "name": "create_playlist",
            "description": (
                "Create a new empty Jellyfin music playlist. "
                "Use when the user says 'create a playlist called X', 'make a playlist named X', etc. "
                "Works from both Discord and the Flutter voice terminal. "
                "Do NOT use this as part of an add-to-playlist request — create and add are separate steps."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Name for the new playlist."},
                },
                "required": ["name"],
            },
        })
        tools.append({
            "name": "add_currently_playing_to_playlist",
            "description": (
                "Add the currently playing Jellyfin audio track to an existing playlist. "
                "Use ONLY when the user refers to what is playing right now — "
                "'add this song to Disco Favorites', 'add this to my playlist', "
                "'put the current track in Road Trip'. "
                "Do NOT use when the user names a specific song ('add Billie Jean to…') — "
                "this tool can only add what Jellyfin is actively playing. "
                "Works from both Discord and the Flutter voice terminal."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "playlist_name": {"type": "string", "description": "Name of the existing playlist to add to."},
                },
                "required": ["playlist_name"],
            },
        })
        tools.append({
            "name": "play_playlist",
            "description": (
                "Play a Jellyfin music playlist from the Flutter voice terminal. "
                "Use when the user explicitly names a playlist to play: "
                "'play the Disco Favorites playlist', 'play my road trip playlist', "
                "'put on Disco Favorites'. "
                "Only works from the Flutter voice terminal — if called from Discord, "
                "explain it is Flutter-only. "
                "CASTING: same cast_target rules as play_music."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "playlist_name": {"type": "string", "description": "Name of the playlist to play."},
                    "cast_target": {"type": "string", "description": "Exact Chromecast device name, if the user asked to cast."},
                },
                "required": ["playlist_name"],
            },
        })

    # --- Write-action tools (gated) ---
    if ENABLE_WRITE_ACTIONS:
        _roots_desc = ", ".join(p for p in [MEDIA_PATH, STAGING_PATH] if p)
        tools.append({
            "name": "manage_files",
            "description": (
                f"Move, rename, or delete files and folders inside the media library ({_roots_desc}). "
                "All operations are restricted to those paths — no escaping to the filesystem. "
                "ALWAYS call with confirmed=False first to show the user a preview. "
                "Only call with confirmed=True after the user explicitly says yes. "
                "delete: removes a single file or entire directory tree (shows full manifest in preview). "
                "delete_matching: deletes all files matching a glob pattern (e.g. *.srt or *.srt,*.ass,*.sup) "
                "recursively under source directory — use this to bulk-delete subtitle files or other file types "
                "without touching video files; shows full file list and total size before confirming. "
                "merge: moves all files from source directory into an existing dest directory in one operation, "
                "then removes the (now-empty) source folder — use this to combine multi-disc album folders or "
                "flatten a nested rip into its parent; blocks if any filename conflicts exist in dest. "
                "rename: renames a single file or folder in-place — dest must be a bare name, no path separators. "
                "rename_all: renames every file in a directory to sequential generic names in one operation — "
                "dest is the name pattern (e.g. rip_{n:02d}); file extensions are preserved; "
                "use this to bulk-reset identified media filenames back to generic rip names for reprocessing. "
                "move: relocates source to dest directory or full destination path."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["move", "merge", "rename", "rename_all", "delete", "delete_matching"],
                        "description": "Operation to perform.",
                    },
                    "source": {
                        "type": "string",
                        "description": (
                            "Path to the file or folder to act on. "
                            "Relative paths are resolved from the media library root."
                        ),
                    },
                    "dest": {
                        "type": "string",
                        "description": (
                            "For move: target directory or full destination path. "
                            "For merge: existing target directory to merge all source files into. "
                            "For rename: new bare filename (no slashes). "
                            "For rename_all: name pattern with {n} as the counter, e.g. rip_{n:02d} "
                            "(default: rip_{n:02d}). Extensions are always preserved. "
                            "For delete_matching: comma-separated glob pattern(s), e.g. *.srt or *.srt,*.ass,*.sup. "
                            "Not used for delete."
                        ),
                        "default": "",
                    },
                    "confirmed": {
                        "type": "boolean",
                        "description": "False (default) shows a dry-run preview. True executes after user confirmation.",
                        "default": False,
                    },
                },
                "required": ["action", "source"],
            },
        })

    # --- Ripping tools (gated) ---
    if ENABLE_RIPPING:
        tools.append({
            "name": "query_ripping",
            "description": (
                "Query the disc ripping and media pipeline. "
                "staging: files/folders currently in the staging area waiting to be processed by Sort_Rips. "
                "subtitles: which movies and shows are missing subtitle sidecar files (.srt/.sup). "
                "recent_rips: last 20 rip events from App Insights (video and CD, last 30 days)."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "query_type": {
                        "type": "string",
                        "enum": ["staging", "subtitles", "recent_rips"],
                        "description": "What to query.",
                        "default": "staging",
                    },
                },
                "required": [],
            },
        })

    if ENABLE_GAMING:
        tools.append({
            "name": "query_steam",
            "description": (
                "Query the Steam game library installed on the server. "
                "library: all installed games with sizes and last-played dates. "
                "disk_usage: same list sorted by size largest-first — use this to find "
                "large games to remove and free up space."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["library", "disk_usage"],
                        "description": "What to query. Default: library.",
                        "default": "library",
                    },
                },
                "required": [],
            },
        })
        tools.append({
            "name": "manage_steam",
            "description": (
                "Remove an installed Steam game from the server. "
                "Deletes the game folder and its ACF manifest; Steam registers the "
                "removal automatically on next launch. "
                "ALWAYS call with confirmed=False first to show the user what will be "
                "deleted and how much space will be freed. "
                "Only call with confirmed=True after the user explicitly says yes."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["remove"],
                        "description": "Action to perform.",
                    },
                    "game": {
                        "type": "string",
                        "description": "Game name to remove (partial, case-insensitive match).",
                    },
                    "confirmed": {
                        "type": "boolean",
                        "description": "Set true only after the user has explicitly confirmed.",
                        "default": False,
                    },
                },
                "required": ["action", "game"],
            },
        })
        tools.append({
            "name": "shutdown_steam",
            "description": (
                "Shut down Steam on the server. Use this when the user is done gaming "
                "and Steam is still running in the background (e.g. after disconnecting "
                "from Moonlight without exiting Steam first). Tries a graceful shutdown "
                "first; force-kills after 10 seconds if Steam doesn't respond."
            ),
            "input_schema": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        })
        tools.append({
            "name": "launch_steam",
            "description": (
                "Launch Steam in Big Picture mode on the server's local display. "
                "Use this when the user wants to play locally (monitor + controller "
                "plugged directly into the server) and Steam isn't already running. "
                "Does nothing if Steam is already running."
            ),
            "input_schema": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        })

    # --- Crawl analytics (gated — opt-in, off by default) ---
    if ENABLE_CRAWL_ANALYTICS and CRAWL_ANALYTICS_URL:
        tools.append({
            "name": "query_crawl_analytics",
            "description": (
                "Query the AI crawl analytics endpoint — a log of AI agents that have "
                "visited a configured external site and reported what they were looking for. "
                "summary: JSON list of visits with agent name, query, purpose, location, "
                "and timestamp — use this to summarize which agents visited, what they "
                "queried, and any notable patterns. "
                "export: raw CSV download of all visits."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["summary", "export"],
                        "description": "What to fetch. Default: summary.",
                        "default": "summary",
                    },
                },
                "required": [],
            },
        })

    # --- Weather ---
    if ENABLE_WEATHER:
        tools.append({
            "name": "get_weather",
            "description": (
                "Get current weather conditions and a 7-day forecast. "
                "Uses the server's home location by default (configured via HOME_LATITUDE / HOME_LONGITUDE). "
                "Pass a city name in 'location' to get weather for any other place instead — "
                "e.g. 'Boston', 'Tokyo', 'Paris, France'. "
                "Returns current temperature, precipitation, wind speed, and a day-by-day "
                "forecast with high/low temps, precipitation probability, and weather description."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": (
                            "City name to look up (e.g. 'Boston', 'Tokyo'). "
                            "Omit to use the server's home location."
                        ),
                        "default": "",
                    },
                },
                "required": [],
            },
        })

    # --- GitHub Issues ---
    if ENABLE_GITHUB_PM:
        tools += [
            {
                "name": "list_github_issues",
                "description": (
                    "List issues in a repo, most-recently-updated first. "
                    "Filter by status: open (default), closed, or all."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "repo":   {"type": "string", "description": "Repo short name (e.g. 'Pandabot') or 'owner/repo'."},
                        "status": {"type": "string", "enum": ["open", "closed", "all"], "description": "Status filter. Default: open.", "default": "open"},
                        "limit":  {"type": "integer", "description": "Max results. Default: 25.", "default": 25},
                    },
                    "required": ["repo"],
                },
            },
            {
                "name": "get_github_issue",
                "description": "Get full details for a specific issue by number.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "repo":   {"type": "string", "description": "Repo short name or 'owner/repo'."},
                        "number": {"type": "integer", "description": "Issue number."},
                    },
                    "required": ["repo", "number"],
                },
            },
            {
                "name": "list_github_sub_issues",
                "description": "List the child (sub-)issues of an issue — use on an epic to find all its stories/tasks.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "repo":   {"type": "string", "description": "Repo short name or 'owner/repo'."},
                        "number": {"type": "integer", "description": "Parent issue number."},
                    },
                    "required": ["repo", "number"],
                },
            },
            {
                "name": "search_github_issues",
                "description": "Full-text search across issues. Optionally scope to a repo.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search text."},
                        "repo":  {"type": "string", "description": "Optional repo short name or 'owner/repo' to scope the search.", "default": ""},
                        "limit": {"type": "integer", "description": "Max results. Default: 25.", "default": 25},
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "list_github_milestones",
                "description": "List open milestones (releases/sprints) for a repo.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "repo": {"type": "string", "description": "Repo short name or 'owner/repo'."},
                    },
                    "required": ["repo"],
                },
            },
            {
                "name": "create_github_issue",
                "description": (
                    "Create an issue in a repo. labels is a comma-separated string "
                    "(use 'type: epic|story|task|bug' for the work-item type). "
                    "Pass parent to link the new issue as a sub-issue of that epic/parent."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "repo":      {"type": "string", "description": "Repo short name or 'owner/repo'."},
                        "title":     {"type": "string", "description": "Issue title."},
                        "body":      {"type": "string", "description": "Optional markdown body.", "default": ""},
                        "labels":    {"type": "string", "description": "Optional comma-separated labels, e.g. 'type: bug, status: new'.", "default": ""},
                        "assignee":  {"type": "string", "description": "Optional GitHub login to assign.", "default": ""},
                        "milestone": {"type": "integer", "description": "Optional milestone number.", "default": 0},
                        "parent":    {"type": "integer", "description": "Optional parent issue number to nest under as a sub-issue.", "default": 0},
                    },
                    "required": ["repo", "title"],
                },
            },
            {
                "name": "update_github_issue",
                "description": (
                    "Update an existing issue. Only provided fields are changed. "
                    "state is 'open' or 'closed'. labels is comma-separated and replaces the label set."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "repo":      {"type": "string", "description": "Repo short name or 'owner/repo'."},
                        "number":    {"type": "integer", "description": "Issue number."},
                        "title":     {"type": "string", "description": "New title.", "default": ""},
                        "body":      {"type": "string", "description": "New markdown body.", "default": ""},
                        "state":     {"type": "string", "enum": ["open", "closed"], "description": "Open or close the issue.", "default": ""},
                        "labels":    {"type": "string", "description": "Comma-separated labels (replaces the set).", "default": ""},
                        "assignee":  {"type": "string", "description": "GitHub login to assign.", "default": ""},
                        "milestone": {"type": "integer", "description": "Milestone number, or -1 to clear. 0=no change.", "default": 0},
                    },
                    "required": ["repo", "number"],
                },
            },
        ]

    # --- Family (gated — opt-in via ENABLE_FAMILY env var) ---
    if ENABLE_FAMILY and FAMILY_SPREADSHEET_ID:
        tools.append({
            "name": "query_family_info",
            "description": (
                "AUTHORITATIVE SOURCE for all family and relationship information. "
                "The sheet is a directory with one row per person and columns like "
                "'Name', 'Relationship to Joel', 'Birthdate', 'Phone', 'Email', "
                "'Address', 'Child of'. "
                "ALWAYS call this tool when the user asks anything about a specific "
                "person — their name, relationship, birthday, contact info, or who "
                "their parents/children are. Do NOT answer from memory or training "
                "data; the sheet is the only valid source. "
                "Always ask for the person's name if not provided."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "person": {
                        "type": "string",
                        "description": "Name of the family member to look up or ask about.",
                    },
                    "relationship": {
                        "type": "string",
                        "description": (
                            "Optional relationship type to filter by. "
                            "e.g. 'son', 'daughter', 'brother', 'sister', 'mother', "
                            "'father', 'wife', 'husband', 'god daughter'. "
                            "The sheet is centered on Joel, so when person='Joel' this "
                            "finds people by their relationship to Joel. "
                            "When person is someone else, finds relatives matching that "
                            "description who are also connected to that person."
                        ),
                    },
                },
                "required": ["person"],
            },
        })

    if ENABLE_LOCAL_LLM:
        from pandabot_core.llm.provider import get_available_profiles as _get_profiles
        _local_profile = os.environ.get("LOCAL_LLM_PROFILE_NAME", "gemma")
        _avail = _get_profiles()
        _avail_str = ", ".join(f"'{p}'" for p in _avail)
    if ENABLE_DEV_AGENT:
        tools.append({
            "name": "trigger_dev_agent",
            "description": (
                "Hand off a development task to Pandabot-Dev, the autonomous coding agent. "
                "Use when the user asks for a code change, new feature, or bug fix to any "
                "Pandabot repo (Pandabot, pandabot-core, PandabotQA, MediaManagement, etc.). "
                "Describe exactly what needs to change and why. "
                "Pandabot-Dev will consult GitHub Issues for context, submit the task to Jules, "
                "review the resulting PR with DeepSeek, and post all updates in #pandabot-dev."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": (
                            "Full description of the code change needed — what to add, change, "
                            "or fix, which repo it belongs to, and why."
                        ),
                    },
                    "context": {
                        "type": "string",
                        "description": "Optional extra context such as a GitHub issue number or related background.",
                    },
                },
                "required": ["task"],
            },
        })

        tools += [
            {
                "name": "query_model_status",
                "description": (
                    "Return the currently active LLM model and all available profiles. "
                    "Use when the user asks which model is active, what models are available, "
                    "or wants to confirm a switch worked."
                ),
                "input_schema": {"type": "object", "properties": {}, "required": []},
            },
            {
                "name": "switch_model",
                "description": (
                    "Switch the active LLM model. Use when the user asks to change models. "
                    f"Available profiles: {_avail_str}. "
                    f"'{_local_profile}' is local via llama.cpp (fast, private, no API cost). "
                    "After switching, tell the user which model is now active."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "model_name": {
                            "type": "string",
                            "description": f"Model to switch to. Available: {_avail_str}.",
                        },
                    },
                    "required": ["model_name"],
                },
            },
        ]

    if ENABLE_STREAMING:
        tools += [
            {
                "name": "play_radio",
                "description": (
                    "Stream an internet radio station by call sign or name. "
                    "With no device, streams to the Flutter voice terminal. "
                    "With a device name, casts to that Google Home speaker. "
                    "Examples: 'Stream WGBH', 'Cast WRPS to kitchen speaker'."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Call sign or station name to search for, e.g. 'WGBH' or 'NPR Boston'.",
                        },
                        "device": {
                            "type": "string",
                            "description": "Friendly name of a Google Home speaker to cast to. Omit to play on the Flutter terminal.",
                        },
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "stop_radio",
                "description": "Stop the currently playing radio stream (local or cast).",
                "input_schema": {"type": "object", "properties": {}, "required": []},
            },
            {
                "name": "radio_status",
                "description": "Report what radio station is currently playing and where.",
                "input_schema": {"type": "object", "properties": {}, "required": []},
            },
            {
                "name": "list_speakers",
                "description": (
                    "Discover Google Home and Chromecast devices on the LAN. "
                    "Use when the user asks what speakers are available or wants "
                    "to cast radio but isn't sure of the device name."
                ),
                "input_schema": {"type": "object", "properties": {}, "required": []},
            },
        ]

    return tools


# ---------------------------------------------------------------------------
# Family lookup (optional, gated by ENABLE_FAMILY)
# ---------------------------------------------------------------------------

_family_reader: "SheetReader | None" = None
_family_cache: "Cache | None" = None


def _get_family_reader() -> SheetReader:
    global _family_reader
    if _family_reader is None:
        _family_reader = SheetReader(
            FAMILY_SPREADSHEET_ID,
            FAMILY_SHEET_NAME,
            FAMILY_CREDENTIALS_PATH if FAMILY_CREDENTIALS_PATH else None,
        )
    return _family_reader


def _get_family_cache() -> Cache:
    global _family_cache
    if _family_cache is None:
        _family_cache = Cache()
    return _family_cache


def _query_family_info(person: str, relationship: str = "") -> str:
    """Look up family info from the family Google Sheet.

    The sheet has one row per person with columns such as:
      Name, Relationship to Joel, Birthdate, Phone, Email, Address, Child of, etc.

    Query patterns:
      ─ Only *person* given  → find the row whose Name matches *person*
                                and return all known info about them.
      ─ *person* = "Joel" and *relationship* given
                              → find rows whose "Relationship to Joel" column
                                matches *relationship* (e.g. "son", "brother").
      ─ *person* ≠ "Joel" and *relationship* given
                              → find rows that mention *person* (e.g. in "Child of")
                                AND also match the *relationship* description.
    """
    reader = _get_family_reader()
    cache = _get_family_cache()
    cache_key = f"{person}:{relationship}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    all_rows = reader.read_all()
    if not all_rows:
        result = "No data available in the family sheet."
        cache.set(cache_key, result)
        return result

    person_lower = person.lower()

    # ── Without relationship: find the person by Name ──
    if not relationship:
        # Try exact Name match first
        matches = [row for row in all_rows if row.get("Name", "").lower() == person_lower]
        if not matches:
            # Try partial Name match (e.g. "Diane" matches "Diane Pelletier")
            matches = [row for row in all_rows if person_lower in row.get("Name", "").lower()]
        if matches:
            lines = [json.dumps(r, ensure_ascii=False) for r in matches]
            result = f"Family info for '{person}':\n" + "\n".join(lines)
        else:
            # Fallback: search all columns for the name
            fuzzy = [
                row for row in all_rows
                if person_lower in " ".join(row.values()).lower()
            ]
            if fuzzy:
                lines = [json.dumps(r, ensure_ascii=False) for r in fuzzy]
                result = f"No exact match for '{person}'. Found related rows:\n" + "\n".join(lines)
            else:
                result = f"No family info found for '{person}'."
        cache.set(cache_key, result)
        return result

    # ── With relationship ──
    rel_lower = relationship.lower()

    # Strategy: search the "Relationship to Joel" column for the relationship value
    rel_col = "Relationship to Joel"
    by_relationship = [
        row for row in all_rows
        if str(row.get(rel_col, "")).lower() == rel_lower
    ]

    # Also try partial match on relationship column values
    if not by_relationship:
        by_relationship = [
            row for row in all_rows
            if rel_lower in str(row.get(rel_col, "")).lower()
        ]

    if not by_relationship:
        result = f"No family members found with relationship '{relationship}'."
        cache.set(cache_key, result)
        return result

    # Filter by person if provided (e.g. "Diane's son" → rows mentioning Diane in Child of)
    if person_lower != "joel":
        filtered = [
            row for row in by_relationship
            if person_lower in " ".join(row.values()).lower()
        ]
        if filtered:
            by_relationship = filtered
        # If filtering clears the list, fall back to the unfiltered list
        # (the person might not be mentioned in the matching rows)

    # Build human-friendly output
    parts: list[str] = []
    for row in by_relationship:
        name = row.get("Name", "?")
        rel_val = row.get("Relationship to Joel", "")
        child_of = row.get("Child of", "")
        line_parts = [f"**{name}**"]
        if rel_val:
            line_parts.append(rel_val)
        if child_of:
            line_parts.append(f"(child of {child_of})")
        parts.append(" — ".join(line_parts))

    result = f"Family members related as '{relationship}':\n" + "\n".join(parts)
    cache.set(cache_key, result)
    return result


# ---------------------------------------------------------------------------
# Dev agent handoff (optional, gated by ENABLE_DEV_AGENT)
# ---------------------------------------------------------------------------

def trigger_dev_agent(task: str, context: str = "") -> str:
    """Forward a development task to Pandabot-Dev via its local webhook."""
    if not ENABLE_DEV_AGENT:
        return "Dev agent is not enabled (set ENABLE_DEV_AGENT=true)."
    try:
        payload = {"task": task, "context": context, "requester": "Pandabot"}
        r = requests.post(f"{_DEV_AGENT_URL}/dev-task", json=payload, timeout=10)
        if r.status_code == 200:
            return "Dev task submitted to Pandabot-Dev. Check #pandabot-dev for progress and updates."
        return f"Pandabot-Dev returned {r.status_code}: {r.text[:200]}"
    except Exception as exc:
        logger.warning("trigger_dev_agent failed: %s", exc)
        return f"Failed to reach Pandabot-Dev: {exc}"


# ---------------------------------------------------------------------------
# Weather (optional, gated by ENABLE_WEATHER)
# ---------------------------------------------------------------------------

_WMO_DESCRIPTIONS = {
    0: "Clear sky",
    1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Foggy", 48: "Icy fog",
    51: "Light drizzle", 53: "Moderate drizzle", 55: "Dense drizzle",
    61: "Slight rain", 63: "Moderate rain", 65: "Heavy rain",
    71: "Slight snow", 73: "Moderate snow", 75: "Heavy snow",
    77: "Snow grains",
    80: "Slight rain showers", 81: "Moderate rain showers", 82: "Violent rain showers",
    85: "Slight snow showers", 86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with slight hail", 99: "Thunderstorm with heavy hail",
}


def _wmo_desc(code: int) -> str:
    return _WMO_DESCRIPTIONS.get(code, f"Weather code {code}")


def get_weather(location: str = "") -> str:
    """Fetch current conditions + 7-day forecast from Open-Meteo."""
    if not ENABLE_WEATHER:
        return "Weather feature is not enabled (set ENABLE_WEATHER=true)."

    lat: str | None = None
    lon: str | None = None
    location_label = "home"

    if location.strip():
        # Resolve city name to coordinates via Open-Meteo Geocoding API
        try:
            geo = requests.get(
                "https://geocoding-api.open-meteo.com/v1/search",
                params={"name": location.strip(), "count": 1, "language": "en", "format": "json"},
                timeout=10,
            ).json()
        except Exception as exc:
            return f"Could not reach geocoding API: {exc}"
        results = geo.get("results") or []
        if not results:
            return f"Location not found: '{location}'. Try a different city name."
        best = results[0]
        lat = str(best["latitude"])
        lon = str(best["longitude"])
        name = best.get("name", location)
        country = best.get("country", "")
        admin1 = best.get("admin1", "")
        parts = [p for p in [name, admin1, country] if p]
        location_label = ", ".join(parts)
    else:
        if not HOME_LATITUDE or not HOME_LONGITUDE:
            return (
                "No home location configured. "
                "Set HOME_LATITUDE and HOME_LONGITUDE in .env, "
                "or ask about a specific city (e.g. 'What's the weather in Boston?')."
            )
        lat = HOME_LATITUDE
        lon = HOME_LONGITUDE
        location_label = "home"

    try:
        resp = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat,
                "longitude": lon,
                "current_weather": "true",
                "daily": ",".join([
                    "temperature_2m_max",
                    "temperature_2m_min",
                    "precipitation_sum",
                    "precipitation_probability_max",
                    "weathercode",
                    "windspeed_10m_max",
                ]),
                "temperature_unit": "fahrenheit",
                "windspeed_unit": "mph",
                "precipitation_unit": "inch",
                "timezone": "auto",
                "forecast_days": 7,
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        return f"Could not fetch weather data: {exc}"

    cw = data.get("current_weather", {})
    cur_temp   = cw.get("temperature", "?")
    cur_wind   = cw.get("windspeed", "?")
    cur_code   = cw.get("weathercode", -1)
    cur_desc   = _wmo_desc(cur_code)
    is_day     = cw.get("is_day", 1)
    time_label = "daytime" if is_day else "nighttime"

    daily = data.get("daily", {})
    dates      = daily.get("time", [])
    highs      = daily.get("temperature_2m_max", [])
    lows       = daily.get("temperature_2m_min", [])
    precip     = daily.get("precipitation_sum", [])
    precip_pct = daily.get("precipitation_probability_max", [])
    wind_max   = daily.get("windspeed_10m_max", [])
    codes      = daily.get("weathercode", [])

    lines = [f"**Weather for {location_label}** ({time_label})", ""]
    lines.append(
        f"**Now:** {cur_desc}, {cur_temp}°F, wind {cur_wind} mph"
    )
    lines.append("")
    lines.append("**7-day forecast:**")

    today = datetime.date.today()
    for i, date_str in enumerate(dates):
        try:
            d = datetime.date.fromisoformat(date_str)
        except ValueError:
            continue
        if d == today:
            day_label = "Today"
        elif d == today + datetime.timedelta(days=1):
            day_label = "Tomorrow"
        else:
            day_label = f"{d.strftime('%A')} {d.day} {d.strftime('%b')}"

        hi  = f"{highs[i]}°F"   if i < len(highs)      else "?"
        lo  = f"{lows[i]}°F"    if i < len(lows)        else "?"
        pct = f"{precip_pct[i]}%" if i < len(precip_pct) else "?"
        mm  = f'{precip[i]}"'   if i < len(precip)      else "?"
        wnd = f"{wind_max[i]} mph" if i < len(wind_max) else "?"
        desc = _wmo_desc(codes[i]) if i < len(codes) else "?"

        rain_part = f", rain {pct} ({mm})" if precip_pct and int(precip_pct[i] or 0) > 0 else ""
        lines.append(
            f"  **{day_label}:** {desc}, {hi}/{lo}{rain_part}, wind {wnd}"
        )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Internet radio streaming (voice-first; Discord-safe)
# ---------------------------------------------------------------------------
# Stations are discovered via Radio Browser API (radio-browser.info).
# Casting is handled entirely by the Flutter terminal (flutter_chrome_cast),
# matching the same architecture as Jellyfin music — the bot passes a
# cast_target name in the WS envelope and Flutter drives the Chromecast SDK.
# list_speakers reads the cast device list the Flutter app already reported
# to the gateway via the cast_devices WS message.

_radio_log = logging.getLogger("pandabot.radio")
_RADIO_BROWSER_API = "https://de1.api.radio-browser.info/json"
_radio_lock = threading.Lock()
_radio_state: dict = {"station": None, "mode": None}


def _rb_search(query: str) -> list[dict]:
    """Search Radio Browser API by name/call sign. Returns list of station dicts."""
    try:
        r = requests.get(
            f"{_RADIO_BROWSER_API}/stations/search",
            params={"name": query, "countrycode": "US", "limit": 5,
                    "order": "votes", "reverse": "true"},
            headers={"User-Agent": "Pandabot/1.0"},
            timeout=8,
        )
        r.raise_for_status()
        results = r.json()
        if results:
            return results
        # Second pass without country filter for non-US stations
        r2 = requests.get(
            f"{_RADIO_BROWSER_API}/stations/search",
            params={"name": query, "limit": 5, "order": "votes", "reverse": "true"},
            headers={"User-Agent": "Pandabot/1.0"},
            timeout=8,
        )
        r2.raise_for_status()
        return r2.json()
    except Exception as exc:
        _radio_log.warning("Radio Browser search for %r failed: %s", query, exc)
        return []


def _gateway_post_radio(path: str, payload: dict) -> bool:
    """POST to the voice gateway for Discord-path radio control. Fire-and-forget."""
    if not _VOICE_GATEWAY_TOKEN:
        _radio_log.warning("VOICE_GATEWAY_TOKEN not set; skipping gateway post to %s", path)
        return False
    try:
        r = requests.post(
            f"{_VOICE_GATEWAY_URL}{path}",
            json=payload,
            headers={"Authorization": f"Bearer {_VOICE_GATEWAY_TOKEN}"},
            timeout=5,
        )
        return r.ok
    except Exception as exc:
        _radio_log.warning("Gateway POST %s failed: %s", path, exc)
        return False


def play_radio(query: str, device: str | None = None) -> str:
    """Search Radio Browser by call sign/name and stream it.

    device=None → play on the Flutter terminal (local).
    device=<name> → cast to that Google Home speaker via Flutter's Cast SDK.
    Casting is identical to play_music with cast_target — Flutter handles it.
    """
    stations = _rb_search(query)
    if not stations:
        return (
            f"Couldn't find a radio station matching '{query}'. "
            "Try the full call sign, e.g. WGBH or WRPS."
        )

    station = stations[0]
    name = station.get("name", query).strip()
    url = (station.get("url_resolved") or station.get("url") or "").strip()
    if not url:
        return f"Found {name} but couldn't get a playable stream URL for it."

    mode = "cast" if device else "local"
    envelope: dict = {"type": "play_radio", "station": name, "url": url}
    if device:
        envelope["cast_target"] = device

    with _radio_lock:
        _radio_state.update({"station": name, "mode": mode})

    ctx = get_voice_context()
    if ctx is not None:
        _emit_envelope(envelope, silent_tts=True)
    else:
        _gateway_post_radio("/play_radio", envelope)

    if device:
        return f"Casting {name} to {device}."
    return f"Streaming {name} on your terminal."


def stop_radio() -> str:
    """Stop the current radio stream (local or cast)."""
    with _radio_lock:
        station = _radio_state.get("station") or "radio"
        _radio_state.update({"station": None, "mode": None})

    if station == "radio":
        return "No radio is currently playing."

    envelope: dict = {"type": "stop_radio"}
    ctx = get_voice_context()
    if ctx is not None:
        _emit_envelope(envelope, silent_tts=True)
    else:
        _gateway_post_radio("/stop_radio", {})
    return f"Stopped {station}."


def radio_status() -> str:
    """Return current radio playback status."""
    with _radio_lock:
        state = dict(_radio_state)
    if not state.get("station"):
        return "No radio is currently playing."
    return f"Playing {state['station']} ({state.get('mode', '?')} mode)."


def list_speakers() -> str:
    """Return Google Home / Chromecast devices the Flutter terminal can see.

    The Flutter app reports its discovered cast devices to the gateway via the
    cast_devices WS message on connect and on change. The gateway exposes them
    at GET /cast_devices.
    """
    try:
        r = requests.get(
            f"{_VOICE_GATEWAY_URL}/cast_devices",
            headers={"Authorization": f"Bearer {_VOICE_GATEWAY_TOKEN}"},
            timeout=5,
        )
        r.raise_for_status()
        data = r.json()
        devices: list[str] = data.get("devices", [])
        if not devices:
            return (
                "No speakers found. Make sure the Flutter terminal is connected "
                "and has discovered Cast devices."
            )
        return "Available speakers: " + ", ".join(devices) + "."
    except Exception as exc:
        _radio_log.warning("list_speakers: gateway request failed: %s", exc)
        return f"Couldn't reach the voice gateway to list speakers: {exc}"


TOOL_DEFINITIONS = _build_tool_definitions()


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def execute_tool(name: str, inputs: dict) -> str:
    if name == "get_disk_usage":            # backward compat for any saved scheduled tasks
        return get_disk_usage()
    if name == "get_log_tail":
        log_name = inputs.get("log_name")
        if not log_name:
            return "Error: get_log_tail requires a non-empty 'log_name' parameter."
        return get_log_tail(
            log_name=log_name,
            lines=inputs.get("lines", 50),
        )
    if name == "get_service_status":
        service_name = inputs.get("service_name")
        if not service_name:
            return "Error: get_service_status requires a non-empty 'service_name' parameter."
        return get_service_status(service_name)
    if name == "trigger_jenkins_job":
        job_name = inputs.get("job_name")
        if not job_name:
            return "Error: trigger_jenkins_job requires a non-empty 'job_name' parameter."
        return trigger_jenkins_job(job_name)
    if name == "set_jenkins_schedule":
        job_name = inputs.get("job_name")
        if not job_name:
            return "Error: set_jenkins_schedule requires a non-empty 'job_name' parameter."
        return set_jenkins_schedule(
            job_name=job_name,
            schedule=inputs.get("schedule", ""),
            confirmed=inputs.get("confirmed", False),
        )
    if name == "query_jenkins":
        action = inputs.get("action")
        if not action:
            return "Error: query_jenkins requires a non-empty 'action' parameter (status, history, or log)."
        return query_jenkins(
            action=action,
            job_name=inputs.get("job_name"),
            build_number=inputs.get("build_number"),
            count=inputs.get("count", 10),
            since_days=inputs.get("since_days"),
            lines=inputs.get("lines", 100),
        )
    if name == "get_jenkins_build_status":   # backward compat for saved scheduled tasks
        return get_jenkins_build_status(inputs.get("job_name"))
    if name == "get_jenkins_build_history":  # backward compat
        return get_jenkins_build_history(
            job_name=inputs["job_name"],
            count=inputs.get("count", 10),
            since_days=inputs.get("since_days"),
        )
    if name == "get_jenkins_build_log":      # backward compat
        return get_jenkins_build_log(
            job_name=inputs["job_name"],
            build_number=inputs.get("build_number"),
            lines=inputs.get("lines", 100),
        )
    if name == "query_media_library":
        action = inputs.get("action")
        if not action:
            return "Error: query_media_library requires a non-empty 'action' parameter (list_dir, file_info, or find_files)."
        return query_media_library(
            action=action,
            path=inputs.get("path", ""),
            pattern=inputs.get("pattern", ""),
            limit=inputs.get("limit", 20),
            file_type=inputs.get("file_type", "video"),
        )
    if name == "get_system_stats":          # backward compat for any saved scheduled tasks
        return get_system_stats()
    if name == "query_system":
        return query_system(inputs.get("aspect", "stats"), inputs.get("limit", 20))
    if name == "query_system_health":        # backward compat for saved scheduled tasks
        return query_system_health(inputs.get("aspect", "stats"))
    if name == "query_storage":              # backward compat
        return query_storage(
            query_type=inputs.get("query_type", "usage"),
            limit=inputs.get("limit", 20),
        )
    if name == "query_network":              # backward compat
        return query_network(inputs.get("query_type", "tailscale"))
    if name == "query_jellyfin":
        return query_jellyfin(inputs.get("query_type", "stats"))
    if name == "play_music":
        return play_music(
            track=inputs.get("track"),
            album=inputs.get("album"),
            artist=inputs.get("artist"),
            cast_target=inputs.get("cast_target"),
        )
    if name == "pause_music":
        return pause_music()
    if name == "resume_music":
        return resume_music()
    if name == "skip_track":
        return skip_track()
    if name == "stop_music":
        return stop_music()
    if name == "previous_track":
        return previous_track()
    if name == "exit_music":
        return exit_music()
    if name == "set_loop_mode":
        return set_loop_mode(inputs.get("mode", "all"))
    if name == "play_video":
        return play_video(
            title=inputs.get("title", ""),
            media_type=inputs.get("media_type", "movie"),
            cast_target=inputs.get("cast_target"),
        )
    if name == "create_playlist":
        return create_playlist(inputs.get("name", ""))
    if name == "add_currently_playing_to_playlist":
        return add_currently_playing_to_playlist(inputs.get("playlist_name", ""))
    if name == "play_playlist":
        return play_playlist(inputs.get("playlist_name", ""), inputs.get("cast_target"))
    if name == "query_ripping":
        return query_ripping(inputs.get("query_type", "staging"))
    if name == "get_performance_history":
        return get_performance_history(
            metric=inputs.get("metric", "cpu"),
            hours=inputs.get("hours", 1),
        )
    if name == "manage_schedule":
        action = inputs.pop("action", "list")
        return manage_schedule(action, **inputs)
    if name in ("take_action", "manage_files"):   # take_action: backward compat
        return manage_files(
            action=inputs["action"],
            source=inputs["source"],
            dest=inputs.get("dest", ""),
            confirmed=inputs.get("confirmed", False),
        )
    if name == "query_steam":
        return query_steam(inputs.get("action", "library"))
    if name == "manage_steam":
        return manage_steam(
            action=inputs["action"],
            game=inputs.get("game", ""),
            confirmed=inputs.get("confirmed", False),
        )
    if name == "shutdown_steam":
        return shutdown_steam()
    if name == "launch_steam":
        return launch_steam()
    if name == "restart_container":
        return restart_container(
            container=inputs["container"],
            confirmed=inputs.get("confirmed", False),
        )
    if name == "query_crawl_analytics":
        return query_crawl_analytics(inputs.get("action", "summary"))
    if name == "query_llm_usage":
        return query_llm_usage(
            action=inputs.get("action", "recent"),
            days=inputs.get("days", 30),
            limit=inputs.get("limit", 20),
        )
    if name == "list_github_issues":
        return list_github_issues(inputs["repo"], inputs.get("status", "open"), inputs.get("limit", 25))
    if name == "get_github_issue":
        return get_github_issue(inputs["repo"], inputs["number"])
    if name == "list_github_sub_issues":
        return list_github_sub_issues(inputs["repo"], inputs["number"])
    if name == "search_github_issues":
        return search_github_issues(inputs["query"], inputs.get("repo", ""), inputs.get("limit", 25))
    if name == "list_github_milestones":
        return list_github_milestones(inputs["repo"])
    if name == "create_github_issue":
        return create_github_issue(
            inputs["repo"], inputs["title"], inputs.get("body", ""),
            inputs.get("labels", ""), inputs.get("assignee", ""),
            inputs.get("milestone", 0), inputs.get("parent", 0),
        )
    if name == "update_github_issue":
        return update_github_issue(
            inputs["repo"], inputs["number"], inputs.get("title", ""),
            inputs.get("body", ""), inputs.get("state", ""), inputs.get("labels", ""),
            inputs.get("assignee", ""), inputs.get("milestone", 0),
        )
    # ── Family ───────────────────────────────────────────────────────────────
    if name == "query_family_info":
        if not ENABLE_FAMILY:
            return "Family feature is not enabled (set ENABLE_FAMILY=true)."
        if not FAMILY_SPREADSHEET_ID:
            return "Family feature is not configured (set FAMILY_SPREADSHEET_ID)."
        return _query_family_info(
            person=inputs.get("person", ""),
            relationship=inputs.get("relationship", ""),
        )
    if name == "query_model_status":
        return query_model_status()
    if name == "switch_model":
        model_name = inputs.get("model_name", "")
        if not model_name:
            return "Error: switch_model requires a non-empty 'model_name' parameter."
        return switch_model(model_name)
    if name == "trigger_dev_agent":
        return trigger_dev_agent(
            task=inputs.get("task", ""),
            context=inputs.get("context", ""),
        )
    if name == "get_weather":
        return get_weather(inputs.get("location", ""))
    if name == "play_radio":
        return play_radio(
            query=inputs.get("query", ""),
            device=inputs.get("device"),
        )
    if name == "stop_radio":
        return stop_radio()
    if name == "radio_status":
        return radio_status()
    if name == "list_speakers":
        return list_speakers()
    return f"Unknown tool: {name}"
