"""Camera-facing API.

The camera side is a BusyBox shell script, so requests and responses are plain
text with tab-separated fields instead of JSON.
"""
import hashlib
import os
import re
import threading

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse
from starlette.concurrency import run_in_threadpool

from . import config, db, files, mailer, settings
from .notify import notify
from .security import hash_device_key, verify_device_key

router = APIRouter(prefix="/api/v1")

_UID_RE = re.compile(r"^[0-9a-f]{8,64}$")
_RANGE_RE = re.compile(r"^bytes (\d+)-(\d+)/(\d+)$")
UPLOADS_PER_SYNC = 10
CFG_KEYS = ("upload_enabled", "rate_kbps", "ap_fallback_sec", "sta_retry_sec", "poll_sec", "min_age_sec")

_chunk_locks: dict[str, threading.Lock] = {}
_chunk_locks_guard = threading.Lock()


def text(body: str, status: int = 200) -> PlainTextResponse:
    return PlainTextResponse(body, status_code=status)


def client_ip(request: Request) -> str:
    return (
        request.headers.get("cf-connecting-ip")
        or request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        or (request.client.host if request.client else "")
    )


def _int_header(request: Request, name: str):
    try:
        return int(request.headers.get(name, ""))
    except ValueError:
        return None


class DeviceAuthError(Exception):
    def __init__(self, status: int, body: str):
        self.status = status
        self.body = body


def authenticate(conn, request: Request, require_approved: bool = True):
    uid = request.headers.get("x-device-id", "").lower()
    key = request.headers.get("x-device-key", "")
    if not _UID_RE.match(uid) or len(key) < 16:
        raise DeviceAuthError(401, "UNKNOWN")
    row = conn.execute("SELECT * FROM devices WHERE uid=?", (uid,)).fetchone()
    if row is None:
        raise DeviceAuthError(401, "UNKNOWN")
    if not verify_device_key(key, row["key_hash"]):
        raise DeviceAuthError(403, "FORBIDDEN")
    if row["status"] == "blocked":
        raise DeviceAuthError(403, "BLOCKED")
    if require_approved and row["status"] != "approved":
        raise DeviceAuthError(403, "PENDING")
    return row


_IPV4_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


def _lan_ip(request: Request) -> str:
    ip = request.headers.get("x-lan-ip", "").strip()
    return ip if _IPV4_RE.match(ip) else ""


def _local_web_port(request: Request):
    """'1:8080' -> 8080, '0:8080' -> 0 (disabled), missing header -> None (keep)."""
    v = request.headers.get("x-local-web")
    if not v:
        return None
    enabled, _, port = v.partition(":")
    if enabled.strip() in ("0", "off", "OFF", "no"):
        return 0
    return int(port) if port.strip().isdigit() and 0 < int(port) < 65536 else 8080


def _touch(conn, device, request: Request) -> None:
    conn.execute(
        """UPDATE devices SET last_seen=?, last_ip=?,
               camera_time=COALESCE(?, camera_time),
               sd_free_kb=COALESCE(?, sd_free_kb),
               sd_total_kb=COALESCE(?, sd_total_kb),
               uploader_version=COALESCE(?, uploader_version),
               wifi_mode=COALESCE(?, wifi_mode),
               ap_ssid=COALESCE(NULLIF(?, ''), ap_ssid),
               ap_pass=COALESCE(NULLIF(?, ''), ap_pass),
               lan_ip=COALESCE(NULLIF(?, ''), lan_ip),
               local_web_port=CASE WHEN ? IS NULL THEN local_web_port ELSE NULLIF(?, 0) END
           WHERE id=?""",
        (
            db.now(),
            client_ip(request),
            _int_header(request, "x-camera-time"),
            _int_header(request, "x-sd-free"),
            _int_header(request, "x-sd-total"),
            request.headers.get("x-uploader-version"),
            request.headers.get("x-wifi-mode"),
            request.headers.get("x-ap-ssid", "")[:64],
            request.headers.get("x-ap-pass", "")[:64],
            _lan_ip(request),
            *(_local_web_port(request),) * 2,
            device["id"],
        ),
    )


@router.post("/register")
async def register(request: Request):
    body = (await request.body()).decode(errors="replace")
    return await run_in_threadpool(_register, request, body)


def _register(request: Request, body: str):
    uid = request.headers.get("x-device-id", "").lower()
    key = request.headers.get("x-device-key", "")
    if not _UID_RE.match(uid) or len(key) < 16:
        return text("BAD_IDENTITY", 400)
    name = body.strip().splitlines()[0][:64] if body.strip() else ""
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM devices WHERE uid=?", (uid,)).fetchone()
        if row is None:
            name = name or "Kamera %s" % uid[:6]
            with db.tx(conn):
                conn.execute(
                    "INSERT INTO devices(uid, key_hash, name, status, created_at, last_seen, last_ip) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (uid, hash_device_key(key), name, "pending", db.now(), db.now(), client_ip(request)),
                )
                db.audit(conn, "camera:" + uid[:8], "register", "%s from %s" % (name, client_ip(request)))
                mailer.new_device(conn, name, client_ip(request))
            notify("Nová kamera čeká na schválení", "%s (%s)" % (name, client_ip(request)))
            return text("PENDING")
        if not verify_device_key(key, row["key_hash"]):
            return text("FORBIDDEN", 403)
        _touch(conn, row, request)
        return text(row["status"].upper())


@router.post("/sync")
async def sync(request: Request):
    body = (await request.body()).decode(errors="replace")
    return await run_in_threadpool(_sync, request, body)


def _parse_manifest(body: str):
    out = {}
    for line in body.splitlines():
        parts = line.rstrip("\r").split("\t")
        if len(parts) != 3:
            continue
        path, size, mtime = parts
        if not valid_path(path):
            continue
        try:
            out[path] = (int(size), int(mtime))
        except ValueError:
            continue
    return out


def valid_path(path: str) -> bool:
    return files.valid_camera_path(path) and not path.endswith(".part")


def _sync(request: Request, body: str):
    manifest = _parse_manifest(body)
    with db.get_conn() as conn:
        try:
            device = authenticate(conn, request)
        except DeviceAuthError as e:
            return text(e.body, e.status)
        cfg = db.device_config(device)
        now = db.now()
        with db.tx(conn):
            _touch(conn, device, request)
            # Rows of hidden preview files from older versions that were never uploaded.
            conn.execute(
                "DELETE FROM files WHERE device_id=? AND path LIKE '%/.%' AND state IN ('camera','uploading')",
                (device["id"],),
            )
            conn.execute("CREATE TEMP TABLE IF NOT EXISTS seen(path TEXT PRIMARY KEY)")
            conn.execute("DELETE FROM seen")
            conn.executemany("INSERT OR IGNORE INTO seen(path) VALUES (?)", [(p,) for p in manifest])
            conn.executemany(
                """INSERT INTO files(device_id, path, folder, size, mtime, recorded_at, present, updated_at)
                   VALUES (?,?,?,?,?,?,1,?)
                   ON CONFLICT(device_id, path) DO UPDATE SET
                       size=excluded.size, mtime=excluded.mtime, present=1,
                       updated_at=CASE WHEN files.present=0 OR files.size<>excluded.size
                                       THEN excluded.updated_at ELSE files.updated_at END""",
                [
                    (device["id"], p, p.split("/", 1)[0], s, m, files.recorded_at(p, m), now)
                    for p, (s, m) in manifest.items()
                ],
            )
            conn.execute(
                "UPDATE files SET present=0, updated_at=? WHERE device_id=? AND present=1 "
                "AND path NOT IN (SELECT path FROM seen)",
                (now, device["id"]),
            )
            # Delete commands for files that are already gone are done.
            conn.execute(
                "UPDATE commands SET status='done', result='file not on camera', updated_at=? "
                "WHERE device_id=? AND type='delete' AND status IN ('pending','sent') "
                "AND path NOT IN (SELECT path FROM seen)",
                (now, device["id"]),
            )
            deletes = conn.execute(
                "SELECT id, path FROM commands WHERE device_id=? AND type='delete' "
                "AND status IN ('pending','sent') ORDER BY id",
                (device["id"],),
            ).fetchall()
            # Switch to AP for N minutes (path holds the minutes). Only the newest pending one counts.
            ap_cmd = conn.execute(
                "SELECT id, path FROM commands WHERE device_id=? AND type='ap' AND status='pending' "
                "ORDER BY id DESC LIMIT 1",
                (device["id"],),
            ).fetchone()
            conn.execute(
                "UPDATE commands SET status='failed', result='nahrazeno novějším', updated_at=? "
                "WHERE device_id=? AND type='ap' AND status='pending' AND id<>?",
                (now, device["id"], ap_cmd["id"] if ap_cmd else 0),
            )
            conn.execute(
                "UPDATE commands SET status='sent', updated_at=? WHERE device_id=? AND status='pending'",
                (now, device["id"]),
            )

            folders = [f.strip() for f in str(cfg["folders"]).split(",") if f.strip()]
            pending_delete = {r["path"] for r in deletes}
            q = (
                "SELECT * FROM files WHERE device_id=? AND present=1 AND state IN ('camera','uploading') "
                "AND (requested=1"
            )
            params: list = [device["id"]]
            if int(cfg["upload_enabled"]) and folders:
                q += " OR folder IN (%s)" % ",".join("?" * len(folders))
                params += folders
            q += ") ORDER BY requested DESC, state='uploading' DESC, recorded_at DESC LIMIT ?"
            params.append(UPLOADS_PER_SYNC + len(pending_delete))
            candidates = [r for r in conn.execute(q, params).fetchall() if r["path"] not in pending_delete]

        # Storage limit: only offer what fits next to the files still waiting for Drive.
        # Local copies of files already on Drive are evicted by the worker when needed.
        quota = settings.quota_bytes(conn)
        avail = quota - settings.storage_usage(conn)["pending"] if quota else None

        lines = ["STATUS\tAPPROVED"]
        lines += ["CFG\t%s\t%s" % (k, int(cfg[k])) for k in CFG_KEYS]
        lines += ["DELETE\t%d\t%s" % (r["id"], r["path"]) for r in deletes]
        if ap_cmd:
            # The camera acks, switches Wi-Fi and ignores UPLOAD lines in this response.
            lines.append("AP\t%d\t%s" % (ap_cmd["id"], ap_cmd["path"]))
            return text("\n".join(lines) + "\n")
        offered = 0
        for r in candidates:
            if offered >= UPLOADS_PER_SYNC:
                break
            offset = 0
            part = files.part_path(device["uid"], r["path"])
            if r["upload_size"] == r["size"] and part.exists():
                offset = part.stat().st_size
            if avail is not None:
                need = r["size"] - offset
                # A single file bigger than the whole limit still goes through when nothing is waiting.
                if need > avail and not (offered == 0 and avail >= quota):
                    continue
                avail -= need
            lines.append("UPLOAD\t%s\t%d\t%d" % (r["path"], offset, r["size"]))
            offered += 1
        return text("\n".join(lines) + "\n")


@router.put("/upload")
async def upload(request: Request, path: str):
    if not valid_path(path):
        return text("BAD_PATH", 400)
    m = _RANGE_RE.match(request.headers.get("content-range", ""))
    if not m:
        return text("BAD_RANGE", 400)
    start, end, total = (int(x) for x in m.groups())
    if end < start or end >= total or end - start + 1 > config.MAX_CHUNK_BYTES:
        return text("BAD_RANGE", 400)
    data = await request.body()
    return await run_in_threadpool(
        _upload, request, path, start, end, total, data, request.headers.get("x-chunk-sha256", "").lower()
    )


def _lock_for(key: str) -> threading.Lock:
    with _chunk_locks_guard:
        return _chunk_locks.setdefault(key, threading.Lock())


def _upload(request: Request, path: str, start: int, end: int, total: int, data: bytes, sha: str):
    with db.get_conn() as conn:
        try:
            device = authenticate(conn, request)
        except DeviceAuthError as e:
            return text(e.body, e.status)
        _touch(conn, device, request)
        with _lock_for(device["uid"] + "/" + path):
            row = conn.execute(
                "SELECT * FROM files WHERE device_id=? AND path=?", (device["id"], path)
            ).fetchone()
            if row is None:
                return text("UNKNOWN_FILE", 404)
            if row["state"] in ("received", "drive_uploading", "on_drive"):
                return text("DONE")
            part = files.part_path(device["uid"], path)
            part.parent.mkdir(parents=True, exist_ok=True)
            received = part.stat().st_size if part.exists() else 0

            if row["upload_size"] != total or received > total:
                if start != 0:
                    part.unlink(missing_ok=True)
                    conn.execute("UPDATE files SET upload_size=? WHERE id=?", (total, row["id"]))
                    return text("OFFSET\t0", 409)
                received = 0
                part.unlink(missing_ok=True)
                conn.execute("UPDATE files SET upload_size=? WHERE id=?", (total, row["id"]))
            if start != received:
                return text("OFFSET\t%d" % received, 409)
            if len(data) != end - start + 1:
                return text("BAD_LENGTH", 400)
            if sha and hashlib.sha256(data).hexdigest() != sha:
                return text("BAD_HASH", 400)

            with open(part, "ab") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            received += len(data)

            if received < total:
                conn.execute(
                    "UPDATE files SET state='uploading', updated_at=? WHERE id=?", (db.now(), row["id"])
                )
                return text("OK\t%d" % received)

            os.replace(part, files.final_path(device["uid"], path))
            with db.tx(conn):
                conn.execute(
                    "UPDATE files SET state='received', local=1, requested=0, attempts=0, next_try_at=0, error=NULL, "
                    "updated_at=? WHERE id=?",
                    (db.now(), row["id"]),
                )
            return text("DONE")


@router.post("/ack")
async def ack(request: Request):
    body = (await request.body()).decode(errors="replace")
    return await run_in_threadpool(_ack, request, body)


def _ack(request: Request, body: str):
    with db.get_conn() as conn:
        try:
            device = authenticate(conn, request)
        except DeviceAuthError as e:
            return text(e.body, e.status)
        now = db.now()
        with db.tx(conn):
            for line in body.splitlines():
                parts = line.rstrip("\r").split("\t", 2)
                if len(parts) < 2 or not parts[0].isdigit():
                    continue
                cmd = conn.execute(
                    "SELECT * FROM commands WHERE id=? AND device_id=?", (int(parts[0]), device["id"])
                ).fetchone()
                if cmd is None:
                    continue
                ok = parts[1] == "ok"
                msg = parts[2][:500] if len(parts) > 2 else ""
                conn.execute(
                    "UPDATE commands SET status=?, result=?, updated_at=? WHERE id=?",
                    ("done" if ok else "failed", msg, now, cmd["id"]),
                )
                if ok and cmd["type"] == "delete":
                    conn.execute(
                        "UPDATE files SET present=0, updated_at=? WHERE device_id=? AND path=?",
                        (now, device["id"], cmd["path"]),
                    )
        return text("OK")


@router.post("/log")
async def camera_log(request: Request):
    body = (await request.body())[-65536:].decode(errors="replace")
    return await run_in_threadpool(_log, request, body)


def _log(request: Request, body: str):
    with db.get_conn() as conn:
        try:
            device = authenticate(conn, request, require_approved=False)
        except DeviceAuthError as e:
            return text(e.body, e.status)
        conn.execute("UPDATE devices SET log_tail=? WHERE id=?", (body, device["id"]))
        return text("OK")
