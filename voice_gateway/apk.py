"""
APK update channel for the Pandabot Flutter terminal.

The Android app has no Play Store presence and the device it runs on is a
fixed ambient terminal, so installing a new build used to mean enabling
Developer options and running `adb install -r` over USB. Instead the app
now asks the gateway what the newest build is and installs it itself.

Layout: APK_DIR holds release APKs named by the convention CI already uses,

    Pandabot-Staging-<versionCode>.apk
    Pandabot-Production-<versionCode>.apk

`versionCode` in the filename is authoritative. It is the same number passed
to `flutter build apk --build-number`, so the app can compare it against its
own installed versionCode without the gateway needing aapt to crack the APK
open. A build is offered to the device only when it is strictly greater than
what the device reports, which is also the only thing Android will let the
package installer accept.

An optional sidecar JSON next to the APK carries display metadata:

    Pandabot-Staging-29416883.json  ->  {"build_name": "1.0.217", "notes": "..."}

Both fields are optional; `build_name` falls back to "1.0.<versionCode>".
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

APK_DIR = Path(os.environ.get("APK_DIR", "/opt/apk"))

FLAVORS = {"staging": "Staging", "production": "Production"}

_NAME_RE = re.compile(r"^Pandabot-(Staging|Production)-(\d+)\.apk$")

# sha256 is only recomputed when the file identity changes, keyed by
# (path, mtime_ns, size). A release APK is ~175 MB and the launch check runs on
# every app start, so hashing on each request would be wasteful.
_hash_cache: dict[tuple[str, int, int], str] = {}


@dataclass(frozen=True)
class ApkRelease:
    path: Path
    flavor: str
    version_code: int
    build_name: str
    size: int
    notes: str | None

    def to_dict(self) -> dict:
        return {
            "flavor": self.flavor,
            "version_code": self.version_code,
            "build_name": self.build_name,
            "file_name": self.path.name,
            "size": self.size,
            "sha256": sha256_of(self.path),
            "notes": self.notes,
            "url": f"/apk/download?flavor={self.flavor}",
        }


def sha256_of(path: Path) -> str:
    """Hash a file, memoised on (path, mtime, size)."""
    st = path.stat()
    key = (str(path), st.st_mtime_ns, st.st_size)
    cached = _hash_cache.get(key)
    if cached is not None:
        return cached

    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    digest = h.hexdigest()

    # The cache is keyed by identity, so stale entries for this path can never
    # be served. Drop them anyway so a long-lived gateway does not accumulate
    # one entry per published build.
    for stale in [k for k in _hash_cache if k[0] == str(path)]:
        del _hash_cache[stale]
    _hash_cache[key] = digest
    return digest


def _read_sidecar(apk: Path) -> tuple[str | None, str | None]:
    """Return (build_name, notes) from the sidecar JSON, if present and sane."""
    sidecar = apk.with_suffix(".json")
    if not sidecar.exists():
        return None, None
    try:
        data = json.loads(sidecar.read_text())
    except (OSError, ValueError) as exc:
        logger.warning("Ignoring unreadable APK sidecar %s: %s", sidecar, exc)
        return None, None
    if not isinstance(data, dict):
        return None, None
    build_name = data.get("build_name")
    notes = data.get("notes")
    return (
        build_name if isinstance(build_name, str) else None,
        notes if isinstance(notes, str) else None,
    )


def latest(flavor: str) -> ApkRelease | None:
    """Highest-versionCode APK published for `flavor`, or None if there is none."""
    wanted = FLAVORS.get(flavor.lower())
    if wanted is None:
        return None
    if not APK_DIR.is_dir():
        logger.warning("APK_DIR %s does not exist — no updates will be served", APK_DIR)
        return None

    best: ApkRelease | None = None
    for entry in APK_DIR.iterdir():
        m = _NAME_RE.match(entry.name)
        if not m or m.group(1) != wanted:
            continue
        if not entry.is_file():
            continue
        version_code = int(m.group(2))
        if best is not None and version_code <= best.version_code:
            continue
        build_name, notes = _read_sidecar(entry)
        best = ApkRelease(
            path=entry,
            flavor=flavor.lower(),
            version_code=version_code,
            build_name=build_name or f"1.0.{version_code}",
            size=entry.stat().st_size,
            notes=notes,
        )
    return best
