"""Server settings editable in the web UI (stored in the DB, defaults from .env)."""
from . import config, files


def _default_folder() -> str:
    remote = config.DRIVE_REMOTE
    prefix = config.GDRIVE_REMOTE_NAME + ":"
    if remote.startswith(prefix):
        return remote[len(prefix):].strip("/") or "70mai"
    return "70mai"


def defaults() -> dict:
    return {
        "drive_folder": _default_folder(),
        "storage_quota_gb": str(config.STORAGE_QUOTA_GB),
        "keep_local_copy": "1" if config.KEEP_LOCAL_COPY else "0",
        "google_account": "",
        # Folder picked in the Google Picker (works for folders shared with you).
        "drive_folder_id": "",
        "drive_folder_name": "",
    }


def get_all(conn) -> dict:
    out = defaults()
    out.update({r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")})
    return out


def get(conn, key: str) -> str:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else defaults()[key]


def set_many(conn, values: dict) -> None:
    conn.executemany(
        "INSERT INTO settings(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        [(k, str(v)) for k, v in values.items()],
    )


def quota_bytes(conn) -> int:
    try:
        gb = float(get(conn, "storage_quota_gb"))
    except ValueError:
        gb = 0
    return int(gb * 1024**3) if gb > 0 else 0


def keep_local_copy(conn) -> bool:
    return get(conn, "keep_local_copy") == "1"


def storage_usage(conn) -> dict:
    """Bytes on the server: pending = waiting for Drive (incl. partial uploads), cached = copies already on Drive."""
    pending = conn.execute(
        "SELECT COALESCE(SUM(size),0) FROM files WHERE local=1 AND state IN ('received','drive_uploading')"
    ).fetchone()[0]
    partial = 0
    for r in conn.execute(
        "SELECT f.path, d.uid FROM files f JOIN devices d ON d.id=f.device_id WHERE f.state='uploading'"
    ):
        p = files.part_path(r["uid"], r["path"])
        if p.exists():
            partial += p.stat().st_size
    cached = conn.execute(
        "SELECT COALESCE(SUM(size),0) FROM files WHERE local=1 AND state='on_drive'"
    ).fetchone()[0]
    return {"pending": pending + partial, "cached": cached, "total": pending + partial + cached}


def drive_remote(conn) -> str:
    """rclone destination root for uploads, or '' when nothing is configured."""
    from . import gdrive

    remote = config.DRIVE_REMOTE
    if remote and not remote.startswith(config.GDRIVE_REMOTE_NAME + ":"):
        return remote
    if not gdrive.connected():
        return ""
    folder = get(conn, "drive_folder").strip("/")
    folder_id = get(conn, "drive_folder_id")
    if folder_id:
        # rclone connection string: paths are relative to the picked folder.
        return "%s,root_folder_id=%s:%s" % (config.GDRIVE_REMOTE_NAME, folder_id, folder)
    return "%s:%s" % (config.GDRIVE_REMOTE_NAME, folder)

