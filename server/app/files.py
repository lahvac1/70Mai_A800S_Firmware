"""Helpers shared by the camera API, the web UI and the Drive worker."""
import os
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config

_PATH_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._\-/]{0,254}$")
_NAME_TS_RE = re.compile(r"(20\d{2})(\d{2})(\d{2})[-_]?(\d{2})(\d{2})(\d{2})")
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._\- ]+")

TZ = ZoneInfo(config.TIMEZONE)


def valid_camera_path(path: str) -> bool:
    if not _PATH_RE.match(path):
        return False
    parts = path.split("/")
    # Hidden entries (e.g. Event/.s_Front = small preview copies) are not recordings.
    if any(p == "" or p.startswith(".") for p in parts) or len(parts) < 2:
        return False
    return parts[0] in config.CAMERA_FOLDERS


def recorded_at(path: str, mtime: int) -> int:
    """Recording time from the 70mai file name (local time), falling back to mtime."""
    m = _NAME_TS_RE.search(os.path.basename(path))
    if m:
        try:
            y, mo, d, h, mi, s = (int(x) for x in m.groups())
            return int(datetime(y, mo, d, h, mi, s, tzinfo=TZ).timestamp())
        except ValueError:
            pass
    return mtime


def device_dir(device_uid: str) -> Path:
    return config.INCOMING_DIR / device_uid


def final_path(device_uid: str, path: str) -> Path:
    return device_dir(device_uid) / path


def part_path(device_uid: str, path: str) -> Path:
    return device_dir(device_uid) / (path + ".part")


def safe_name(name: str) -> str:
    return _SAFE_NAME_RE.sub("_", name).strip() or "camera"


def drive_destination(remote: str, device_name: str, path: str, recorded: int) -> str:
    folder = path.split("/", 1)[0]
    rel = path.split("/", 1)[1]
    date = datetime.fromtimestamp(recorded, TZ).strftime("%Y-%m-%d")
    sub = config.DRIVE_PATH_TEMPLATE.format(
        device=safe_name(device_name), folder=folder, date=date, name=os.path.basename(path), path=rel
    )
    base = remote if remote.endswith(":") else remote.rstrip("/") + "/"
    return base + sub.lstrip("/")
