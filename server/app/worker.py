"""Background thread that moves received files to Google Drive with rclone."""
import logging
import subprocess
import threading
import time

from . import config, db, files, mailer, settings
from .notify import notify

log = logging.getLogger("worker")

POLL_SEC = 5
STALE_PART_SEC = 7 * 24 * 3600
# Report a failing file in the summary e-mail on these attempts only (no spam on retries).
MAIL_FAIL_ATTEMPTS = (1, 5, 10, 20)


def _upload_one(conn, row, remote: str) -> None:
    src = files.final_path(row["uid"], row["path"])
    if not src.exists():
        conn.execute(
            "UPDATE files SET state='camera', local=0, error='local file missing', updated_at=? WHERE id=?",
            (db.now(), row["id"]),
        )
        return
    dest = files.drive_destination(remote, row["device_name"], row["path"], row["recorded_at"])
    conn.execute("UPDATE files SET state='drive_uploading', updated_at=? WHERE id=?", (db.now(), row["id"]))
    log.info("rclone copyto %s -> %s", src, dest)
    try:
        proc = subprocess.run(
            [config.RCLONE_BIN, "copyto", str(src), dest, "--retries", "3", "--low-level-retries", "10"],
            capture_output=True,
            text=True,
            timeout=6 * 3600,
        )
        ok, err = proc.returncode == 0, (proc.stderr or proc.stdout)[-500:]
    except (OSError, subprocess.TimeoutExpired) as exc:
        ok, err = False, str(exc)

    now = db.now()
    if not ok:
        attempts = row["attempts"] + 1
        delay = min(3600, 30 * 2**attempts)
        log.warning("upload failed (%s): %s", row["path"], err)
        with db.tx(conn):
            conn.execute(
                "UPDATE files SET state='received', attempts=?, next_try_at=?, error=?, updated_at=? WHERE id=?",
                (attempts, now + delay, err, now, row["id"]),
            )
            if attempts in MAIL_FAIL_ATTEMPTS:
                mailer.queue(conn, "fail", row, error=err)
        return

    keep = settings.keep_local_copy(conn)
    with db.tx(conn):
        conn.execute(
            "UPDATE files SET state='on_drive', drive_path=?, local=?, error=NULL, updated_at=? WHERE id=?",
            (dest, 1 if keep else 0, now, row["id"]),
        )
        mailer.queue(conn, "ok", row, drive_path=dest)
        cfg = db.device_config(row)
        if int(cfg["delete_after_upload"]) and row["present"]:
            exists = conn.execute(
                "SELECT 1 FROM commands WHERE device_id=? AND path=? AND type='delete' "
                "AND status IN ('pending','sent')",
                (row["device_id"], row["path"]),
            ).fetchone()
            if not exists:
                conn.execute(
                    "INSERT INTO commands(device_id, type, path, created_by, created_at, updated_at) "
                    "VALUES (?, 'delete', ?, 'auto', ?, ?)",
                    (row["device_id"], row["path"], now, now),
                )
    if not keep:
        src.unlink(missing_ok=True)
    if row["folder"] == "Event":
        notify("Nové video na Google Drive", "%s: %s" % (row["device_name"], row["path"]))


def _cleanup_stale_parts(conn) -> None:
    rows = conn.execute(
        "SELECT f.id, f.path, d.uid FROM files f JOIN devices d ON d.id=f.device_id "
        "WHERE f.state='uploading' AND f.present=0 AND f.updated_at < ?",
        (db.now() - STALE_PART_SEC,),
    ).fetchall()
    for r in rows:
        files.part_path(r["uid"], r["path"]).unlink(missing_ok=True)
        conn.execute("UPDATE files SET state='camera', upload_size=NULL WHERE id=?", (r["id"],))


def enforce_quota(conn) -> None:
    """Drop local copies of files already on Drive: all of them when copies are not
    kept, otherwise the oldest ones while the storage limit is exceeded."""
    if not settings.keep_local_copy(conn):
        over = float("inf")
    else:
        quota = settings.quota_bytes(conn)
        if not quota:
            return
        over = settings.storage_usage(conn)["total"] - quota
    if over <= 0:
        return
    rows = conn.execute(
        "SELECT f.id, f.path, f.size, d.uid FROM files f JOIN devices d ON d.id=f.device_id "
        "WHERE f.local=1 AND f.state='on_drive' ORDER BY f.updated_at"
    ).fetchall()
    for r in rows:
        if over <= 0:
            break
        files.final_path(r["uid"], r["path"]).unlink(missing_ok=True)
        conn.execute("UPDATE files SET local=0 WHERE id=?", (r["id"],))
        over -= r["size"]
        log.info("removed local copy of %s (stays on Drive)", r["path"])


def run_forever(stop: threading.Event) -> None:
    with db.get_conn() as conn:
        conn.execute("UPDATE files SET state='received' WHERE state='drive_uploading'")
    last_cleanup = 0.0
    while not stop.is_set():
        try:
            with db.get_conn() as conn:
                if time.time() - last_cleanup > 3600:
                    _cleanup_stale_parts(conn)
                    last_cleanup = time.time()
                enforce_quota(conn)
                remote = settings.drive_remote(conn)
                if remote:
                    row = conn.execute(
                        "SELECT f.*, d.uid, d.name AS device_name, d.config FROM files f "
                        "JOIN devices d ON d.id=f.device_id "
                        "WHERE f.state='received' AND f.next_try_at<=? ORDER BY f.recorded_at LIMIT 1",
                        (db.now(),),
                    ).fetchone()
                    if row is not None:
                        _upload_one(conn, row, remote)
                        continue
                mailer.flush_if_quiet(conn)
                mailer.deliver_pending(conn)
        except Exception:  # noqa: BLE001
            log.exception("worker iteration failed")
        stop.wait(POLL_SEC)


def start() -> threading.Event:
    stop = threading.Event()
    threading.Thread(target=run_forever, args=(stop,), name="drive-worker", daemon=True).start()
    return stop
