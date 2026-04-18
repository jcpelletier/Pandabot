"""
Read-only tool implementations for the Panda server Discord bot.

These are the ONLY operations the bot can perform. No code execution,
no writes, no destructive commands — strictly observability.
"""

import subprocess
import os
import json
import datetime
import logging
import requests

logger = logging.getLogger("panda-bot")

# ---------------------------------------------------------------------------
# Feature flags — set to "false" in .env to disable entire subsystems
# ---------------------------------------------------------------------------

ENABLE_JELLYFIN = os.environ.get("ENABLE_JELLYFIN", "true").lower() == "true"
ENABLE_JENKINS  = os.environ.get("ENABLE_JENKINS",  "true").lower() == "true"
ENABLE_RIPPING  = os.environ.get("ENABLE_RIPPING",  "true").lower() == "true"
ENABLE_SMART    = os.environ.get("ENABLE_SMART",    "true").lower() == "true"

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
JELLYFIN_TOKEN = os.environ.get("JELLYFIN_API_KEY", "")
APPINSIGHTS_APP_ID  = os.environ.get("APPINSIGHTS_APP_ID", "")
AZURE_TENANT_ID     = os.environ.get("AZURE_TENANT_ID", "")
AZURE_CLIENT_ID     = os.environ.get("AZURE_CLIENT_ID", "")
AZURE_CLIENT_SECRET = os.environ.get("AZURE_CLIENT_SECRET", "")

STAGING_PATH = os.environ.get("STAGING_PATH", "/mnt/media/Video")
MEDIA_PATH   = os.environ.get("MEDIA_PATH",   "/mnt/media/Media")

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

        else:
            return f"Unknown query_type '{query_type}'. Available: stats, recent, streams, history, week"

    except requests.RequestException as e:
        return f"Jellyfin API error: {e}"


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

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            err = result.stderr.strip()
            if "Cannot connect" in err or "Connection refused" in err:
                return "PCP daemon (pmcd) is not running — historical metrics unavailable."
            if "No data" in err or "no data" in err:
                return f"No {metric} data yet — pmlogger may have just started. Try again in a few minutes."
            return f"pmrep error: {err}"

        output = result.stdout.strip()
        if not output:
            return (
                f"No {metric} data available for the past {hours}h. "
                "pmlogger may have just started collecting — data builds up over time."
            )

        lines = output.splitlines()
        # Always keep the CSV header (first line) + cap data rows at 35
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
        return "pmrep not found — PCP may not be installed (sudo apt install pcp cockpit-pcp)."
    except subprocess.TimeoutExpired:
        return "pmrep timed out after 30s."
    except Exception as e:
        return f"Error querying performance history: {e}"


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
    import scheduler as sched

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


def query_media_library(action: str, path: str = "", pattern: str = "", limit: int = 20) -> str:
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

    if action == "file_info":
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
                if os.path.splitext(fname)[1].lower() not in VIDEO_EXTS:
                    continue
                if pattern and pattern.lower() not in fname.lower():
                    continue
                full = os.path.join(dirpath, fname)
                try:
                    stat = os.stat(full)
                    rel = os.path.relpath(full, root)
                    entries.append((rel, stat.st_size, stat.st_mtime))
                except Exception:
                    pass

        if not entries:
            msg = f"No video files found under {root}"
            return msg + (f" matching '{pattern}'" if pattern else "") + "."

        entries.sort(key=lambda x: -x[2])  # newest first
        shown = entries[:limit]
        total = len(entries)
        lines = [f"Video files in {root}  ({total} total, showing {len(shown)}):"]
        for rel, size, mtime in shown:
            dt = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d")
            lines.append(f"  {rel}  [{_fmt_bytes(size)}]  modified {dt}")
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


# ---------------------------------------------------------------------------
# Tool schema definitions for Claude — built dynamically from feature flags
# ---------------------------------------------------------------------------

def _build_tool_definitions() -> list[dict]:
    """Construct the tool list Claude sees, gated by feature flags."""

    # Log names available for tailing — built from current whitelists
    _all_log_names = sorted(list(ALLOWED_FILE_LOGS.keys()) + list(ALLOWED_DOCKER_LOGS))

    # query_system_health aspects — smart only if enabled and devices are configured
    _health_aspects = ["stats", "failed", "updates", "processes"]
    _smart_desc = ""
    if ENABLE_SMART and SMART_DEVICES:
        _health_aspects.append("smart")
        _device_summary = "; ".join(f"{dev} ({label})" for dev, label in SMART_DEVICES)
        _smart_desc = (
            f"smart: SMART drive health ({_device_summary}) — "
            "reallocated sectors, pending sectors, power-on hours, temperature, SSD wear counters. "
        )

    tools = [
        # --- Storage ---
        {
            "name": "query_storage",
            "description": (
                "Check disk usage and storage breakdown for the server. "
                "usage: df -h for / and /mnt/media — overall free/used space. "
                "breakdown: du -sh per top-level folder under /mnt/media (Movies, Shows, Music, Video staging). "
                "largest: top N largest files under /mnt/media — useful for finding space to reclaim (default 20, max 50)."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "query_type": {
                        "type": "string",
                        "enum": ["usage", "breakdown", "largest"],
                        "description": "What to query. Default: usage.",
                        "default": "usage",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "largest only: how many files to return (1–50, default 20).",
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
        # --- Media library ---
        {
            "name": "query_media_library",
            "description": (
                f"Inspect files in the media library ({MEDIA_PATH}) or staging area.\n"
                "file_info: full ffprobe metadata for one file — codec, resolution, duration, "
                "bitrate, and all audio/subtitle tracks. Use this to answer 'why wasn't X "
                "converted?' (check video bitrate — NVENC re-encodes land at ~3–8 Mbps; "
                "original rips are typically 15–40 Mbps) or 'how long is this movie?'.\n"
                "find_files: list video files in a directory with sizes and modification dates. "
                f"Path can be absolute or relative to {MEDIA_PATH}."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["file_info", "find_files"],
                        "description": "file_info: metadata for one file. find_files: list files in a directory.",
                    },
                    "path": {
                        "type": "string",
                        "description": (
                            f"file_info: path to the file (absolute or relative to {MEDIA_PATH}). "
                            f"find_files: directory to scan (default: {MEDIA_PATH})."
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
                },
                "required": ["action"],
            },
        },
        # --- System health ---
        {
            "name": "query_system_health",
            "description": (
                "Check system health from multiple angles. "
                "stats: CPU load average, memory usage, GPU temp/VRAM/utilisation (if nvidia-smi present). "
                "failed: any systemd units in a failed state. "
                "updates: apt packages available to upgrade. "
                "processes: top 15 processes by CPU usage. "
                + _smart_desc
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "aspect": {
                        "type": "string",
                        "enum": _health_aspects,
                        "description": "Which health aspect to check. Default: stats.",
                        "default": "stats",
                    },
                },
                "required": [],
            },
        },
        # --- Network ---
        {
            "name": "query_network",
            "description": (
                "Query network status. "
                "tailscale: peer list with online/offline status and IPs. "
                "external_ip: current public IP address of the server. "
                "ports: listening TCP ports and the processes bound to them."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "query_type": {
                        "type": "string",
                        "enum": ["tailscale", "external_ip", "ports"],
                        "description": "What to query. Default: tailscale.",
                        "default": "tailscale",
                    },
                },
                "required": [],
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
                "Pattern: trigger → manage_schedule(condition_check, tool_calls=[get_jenkins_build_status], "
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
            "name": "get_jenkins_build_status",
            "description": (
                "Quick status snapshot for Jenkins. "
                "Omit job_name for an overview of all jobs with their last build result. "
                "Provide job_name for details on that job's most recent build. "
                f"Known jobs: {_jobs_str}."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "job_name": {
                        "type": "string",
                        "description": "Specific job name. Omit for all-jobs overview.",
                    },
                },
                "required": [],
            },
        })
        tools.append({
            "name": "get_jenkins_build_history",
            "description": (
                "Get a list of recent builds for a Jenkins job — numbers, results, "
                "start times, and durations. Use this to spot patterns like repeated "
                "failures or to find a specific build number before fetching its log. "
                "Use since_days=7 for weekly digests — returns a pass/fail summary plus "
                "the individual build list for that window."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "job_name": {
                        "type": "string",
                        "description": f"Job name. Known jobs: {_jobs_str}.",
                    },
                    "count": {
                        "type": "integer",
                        "description": "How many recent builds to return (1–50, default 10). Ignored when since_days is set.",
                        "default": 10,
                    },
                    "since_days": {
                        "type": "integer",
                        "description": "If set, return all builds from the last N days instead of using count. Includes a pass/fail summary.",
                    },
                },
                "required": ["job_name"],
            },
        })
        tools.append({
            "name": "get_jenkins_build_log",
            "description": (
                "Fetch the console log for a specific Jenkins build. "
                "Use build_number to target a past run (get the number from get_jenkins_build_history), "
                "or omit it to get the latest build's log. "
                "Returns the last N lines (default 100, max 300)."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "job_name": {
                        "type": "string",
                        "description": f"Job name. Known jobs: {_jobs_str}.",
                    },
                    "build_number": {
                        "type": "integer",
                        "description": "Build number to fetch. Omit for the latest build.",
                    },
                    "lines": {
                        "type": "integer",
                        "description": "How many lines from the end of the log to return (1–300, default 100).",
                        "default": 100,
                    },
                },
                "required": ["job_name"],
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
                "history: recently watched titles per user."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "query_type": {
                        "type": "string",
                        "enum": ["stats", "recent", "week", "streams", "history"],
                        "description": "What to query.",
                        "default": "stats",
                    },
                },
                "required": [],
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

    return tools


TOOL_DEFINITIONS = _build_tool_definitions()


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def execute_tool(name: str, inputs: dict) -> str:
    if name == "get_disk_usage":            # backward compat for any saved scheduled tasks
        return get_disk_usage()
    if name == "get_log_tail":
        return get_log_tail(
            log_name=inputs["log_name"],
            lines=inputs.get("lines", 50),
        )
    if name == "get_service_status":
        return get_service_status(inputs["service_name"])
    if name == "trigger_jenkins_job":
        return trigger_jenkins_job(inputs["job_name"])
    if name == "get_jenkins_build_status":
        return get_jenkins_build_status(inputs.get("job_name"))
    if name == "get_jenkins_build_history":
        return get_jenkins_build_history(
            job_name=inputs["job_name"],
            count=inputs.get("count", 10),
            since_days=inputs.get("since_days"),
        )
    if name == "get_jenkins_build_log":
        return get_jenkins_build_log(
            job_name=inputs["job_name"],
            build_number=inputs.get("build_number"),
            lines=inputs.get("lines", 100),
        )
    if name == "query_media_library":
        return query_media_library(
            action=inputs["action"],
            path=inputs.get("path", ""),
            pattern=inputs.get("pattern", ""),
            limit=inputs.get("limit", 20),
        )
    if name == "get_system_stats":          # backward compat for any saved scheduled tasks
        return get_system_stats()
    if name == "query_system_health":
        return query_system_health(inputs.get("aspect", "stats"))
    if name == "query_storage":
        return query_storage(
            query_type=inputs.get("query_type", "usage"),
            limit=inputs.get("limit", 20),
        )
    if name == "query_network":
        return query_network(inputs.get("query_type", "tailscale"))
    if name == "query_jellyfin":
        return query_jellyfin(inputs.get("query_type", "stats"))
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
    return f"Unknown tool: {name}"
