"""
Read-only tool implementations for the Panda server Discord bot.

These are the ONLY operations the bot can perform. No code execution,
no writes, no destructive commands — strictly observability.
"""

import subprocess
import os
import json
import datetime
import requests

JENKINS_URL = os.environ.get("JENKINS_URL", "http://localhost:8080")
JENKINS_USER = os.environ.get("JENKINS_USER", "admin")
JENKINS_TOKEN = os.environ.get("JENKINS_TOKEN", "")

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
    return f"Unknown tool: {name}"
