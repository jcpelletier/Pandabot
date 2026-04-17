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

JENKINS_URL    = os.environ.get("JENKINS_URL", "http://localhost:8080")
JENKINS_USER   = os.environ.get("JENKINS_USER", "admin")
JENKINS_TOKEN  = os.environ.get("JENKINS_TOKEN", "")
JELLYFIN_URL        = os.environ.get("JELLYFIN_URL", "http://localhost:8096")
JELLYFIN_TOKEN      = os.environ.get("JELLYFIN_API_KEY", "")
APPINSIGHTS_APP_ID      = os.environ.get("APPINSIGHTS_APP_ID", "")
AZURE_TENANT_ID         = os.environ.get("AZURE_TENANT_ID", "")
AZURE_CLIENT_ID         = os.environ.get("AZURE_CLIENT_ID", "")
AZURE_CLIENT_SECRET     = os.environ.get("AZURE_CLIENT_SECRET", "")

# Token cache — refreshed automatically when expired
_ai_token_cache: dict = {"token": None, "expires": 0.0}


def _get_appinsights_token() -> str:
    """Return a valid Azure AD bearer token for the App Insights query API.
    Tokens are cached for their lifetime (~1 hour) to avoid unnecessary requests."""
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
STAGING_PATH        = os.environ.get("STAGING_PATH", "/mnt/media/Video")
MEDIA_PATH          = os.environ.get("MEDIA_PATH", "/mnt/media/Media")

# Whitelist of logs the bot is allowed to tail
ALLOWED_FILE_LOGS = {
    "rip-video": "/var/log/rip-video.log",
    "rip-cd":    "/var/log/rip-cd.log",
}

# Docker containers the bot is allowed to read logs from
ALLOWED_DOCKER_LOGS = {"jellyfin", "jenkins"}

# Systemd services (non-Docker) the bot is allowed to inspect
ALLOWED_SYSTEMD_SERVICES = {"sunshine", "tailscaled", "cockpit", "ssh"}

# All services the bot knows about
ALL_SERVICES = sorted(
    list(ALLOWED_FILE_LOGS.keys())
    + list(ALLOWED_DOCKER_LOGS)
    + list(ALLOWED_SYSTEMD_SERVICES)
)


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
    if not ms:
        return "unknown"
    dt = datetime.datetime.utcfromtimestamp(ms / 1000)
    return dt.strftime("%Y-%m-%d %H:%M UTC")


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


def get_jenkins_build_history(job_name: str, count: int = 10) -> str:
    """
    Return the last N builds for a job with number, result, start time and duration.
    """
    count = min(max(count, 1), 50)
    auth = _jenkins_auth()
    try:
        url = (
            f"{JENKINS_URL}/job/{job_name}/api/json"
            f"?tree=builds[number,result,building,timestamp,duration,url]{{0,{count}}}"
        )
        r = requests.get(url, auth=auth, timeout=10)
        if r.status_code == 404:
            return f"Job '{job_name}' not found."
        r.raise_for_status()
        builds = r.json().get("builds", [])
        if not builds:
            return f"No builds found for '{job_name}'."

        lines = [f"Last {len(builds)} builds for {job_name}:"]
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

        else:
            return f"Unknown query_type '{query_type}'. Available: stats, recent, streams, history"

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
                "| extend title  = tostring(customDimensions.disc_title), "
                "         artist = tostring(customDimensions.artist), "
                "         album  = tostring(customDimensions.album), "
                "         tracks = tostring(customDimensions.track_count), "
                "         size   = tostring(customDimensions.final_size), "
                "         role   = cloud_RoleName "
                "| project timestamp, role, title, artist, album, tracks, size "
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
                ts, role, title, artist, album, tracks, size = row
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
    hours:  1–24
    """
    hours = max(1, min(24, int(hours)))

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
# Tool schema definitions for Claude
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS = [
    {
        "name": "get_disk_usage",
        "description": (
            "Check disk space on the server. "
            "Returns df -h output for / (SSD) and /mnt/media (2 TB HDD)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "get_log_tail",
        "description": (
            "Retrieve the last N lines from an allowed service log. "
            "Available logs: rip-video, rip-cd, jellyfin, jenkins."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "log_name": {
                    "type": "string",
                    "enum": ["rip-video", "rip-cd", "jellyfin", "jenkins"],
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
    {
        "name": "get_jenkins_build_status",
        "description": (
            "Quick status snapshot for Jenkins. "
            "Omit job_name for an overview of all jobs with their last build result. "
            "Provide job_name for details on that job's most recent build. "
            "Known jobs: Login_Test, Process_Movies, Nightly_Convert."
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
    },
    {
        "name": "get_jenkins_build_history",
        "description": (
            "Get a list of recent builds for a Jenkins job — numbers, results, "
            "start times, and durations. Use this to spot patterns like repeated "
            "failures or to find a specific build number before fetching its log."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "job_name": {
                    "type": "string",
                    "description": "Job name (e.g. Login_Test, Process_Movies, Nightly_Convert).",
                },
                "count": {
                    "type": "integer",
                    "description": "How many recent builds to return (1–50, default 10).",
                    "default": 10,
                },
            },
            "required": ["job_name"],
        },
    },
    {
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
                    "description": "Job name (e.g. Login_Test).",
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
    },
    {
        "name": "get_system_stats",
        "description": "Get CPU load average, memory usage, and NVIDIA GPU stats.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "query_jellyfin",
        "description": (
            "Query the Jellyfin media server. "
            "stats: library counts (movies, shows, episodes, music). "
            "recent: last 10 items added to the library. "
            "streams: active playback sessions — who is watching what, "
            "DirectPlay vs Transcode, whether NVENC is in use. "
            "history: recently watched titles per user."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query_type": {
                    "type": "string",
                    "enum": ["stats", "recent", "streams", "history"],
                    "description": "What to query.",
                    "default": "stats",
                },
            },
            "required": [],
        },
    },
    {
        "name": "query_ripping",
        "description": (
            "Query the disc ripping and media pipeline. "
            "staging: files/folders currently in the staging area waiting to be processed by Sort_Rips. "
            "subtitles: which movies and shows are missing subtitle sidecar files (.srt/.sup). "
            "recent_rips: last 20 rip events from App Insights (video and CD, last 30 days) — "
            "requires APPINSIGHTS_APP_ID and APPINSIGHTS_API_KEY to be configured."
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
    },
    {
        "name": "get_performance_history",
        "description": (
            "Query historical performance metrics from PCP/pmlogger — the same data "
            "source Cockpit uses for its performance graphs. Returns a time-series CSV "
            "sampled at regular intervals. Use this to answer questions like 'was the "
            "CPU spiking last night?' or 'how much memory was used over the past 6 hours?'. "
            "Available metrics: cpu (user/sys/idle rates), memory (used/free bytes), "
            "disk (read/write bytes/s), network (in/out bytes/s per interface)."
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
                    "description": "How many hours back to look (1–24, default 1).",
                    "default": 1,
                },
            },
            "required": [],
        },
    },
]


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def execute_tool(name: str, inputs: dict) -> str:
    if name == "get_disk_usage":
        return get_disk_usage()
    if name == "get_log_tail":
        return get_log_tail(
            log_name=inputs["log_name"],
            lines=inputs.get("lines", 50),
        )
    if name == "get_service_status":
        return get_service_status(inputs["service_name"])
    if name == "get_jenkins_build_status":
        return get_jenkins_build_status(inputs.get("job_name"))
    if name == "get_jenkins_build_history":
        return get_jenkins_build_history(
            job_name=inputs["job_name"],
            count=inputs.get("count", 10),
        )
    if name == "get_jenkins_build_log":
        return get_jenkins_build_log(
            job_name=inputs["job_name"],
            build_number=inputs.get("build_number"),
            lines=inputs.get("lines", 100),
        )
    if name == "get_system_stats":
        return get_system_stats()
    if name == "query_jellyfin":
        return query_jellyfin(inputs.get("query_type", "stats"))
    if name == "query_ripping":
        return query_ripping(inputs.get("query_type", "staging"))
    if name == "get_performance_history":
        return get_performance_history(
            metric=inputs.get("metric", "cpu"),
            hours=inputs.get("hours", 1),
        )
    return f"Unknown tool: {name}"
