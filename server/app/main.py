import asyncio
import hashlib
import json
import logging
import math
import mimetypes
import re
import secrets
import shutil
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import api, config, db, files, gdrive, mailer, settings, worker
from .security import hash_password, verify_password

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")

BASE = Path(__file__).parent
PAGE_SIZE = 200
# Filter options on the device page: key -> (label, SQL condition on files).
STATE_FILTERS = {
    "camera": ("jen v kameře", "state='camera' AND requested=0"),
    "requested": ("vyžádáno pro upload", "requested=1 AND state IN ('camera','uploading')"),
    "uploading": ("nahrává se z kamery", "state='uploading'"),
    "server": ("na serveru", "local=1"),
    "waiting": ("čeká na Google Drive", "state IN ('received','drive_uploading')"),
    "on_drive": ("na Google Drive", "state='on_drive'"),
}
STREAM_CHUNK = 256 * 1024


def bootstrap_admin() -> None:
    with db.get_conn() as conn:
        if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
            return
        if not config.ADMIN_PASSWORD:
            raise RuntimeError("No users exist yet: set ADMIN_PASSWORD to create the first admin")
        email = config.ADMIN_EMAIL or None
        conn.execute(
            "INSERT INTO users(username, password_hash, role, created_at, email, "
            "notify_uploads, notify_errors, notify_devices) VALUES (?,?,?,?,?,?,?,?)",
            (config.ADMIN_USERNAME, hash_password(config.ADMIN_PASSWORD), "admin", db.now(), email,
             *(1 if email else 0,) * 3),
        )
        log.info("created initial admin user %r", config.ADMIN_USERNAME)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if len(config.SECRET_KEY) < 32:
        raise RuntimeError("SECRET_KEY must be at least 32 characters")
    db.init_db()
    bootstrap_admin()
    stop = worker.start() if config.WORKER_ENABLED else None
    yield
    if stop:
        stop.set()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(
    SessionMiddleware,
    secret_key=config.SECRET_KEY or "unset",
    session_cookie="dashcam_session",
    https_only=config.COOKIE_SECURE,
    same_site="lax",
    max_age=14 * 24 * 3600,
)
app.include_router(api.router)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")


def _fmt_dt(ts):
    if not ts:
        return "–"
    return datetime.fromtimestamp(int(ts), files.TZ).strftime("%d.%m.%Y %H:%M:%S")


def _fmt_size(n):
    if n is None:
        return "–"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%.0f %s" if unit == "B" else "%.1f %s") % (n, unit)
        n /= 1024


def _fmt_ago(ts):
    if not ts:
        return "nikdy"
    d = int(time.time()) - int(ts)
    if d < 90:
        return "před %d s" % max(d, 0)
    if d < 5400:
        return "před %d min" % (d // 60)
    if d < 172800:
        return "před %d h" % (d // 3600)
    return "před %d dny" % (d // 86400)


templates.env.filters["dt"] = _fmt_dt
templates.env.filters["size"] = _fmt_size
templates.env.filters["ago"] = _fmt_ago
templates.env.globals["STATE_FILTERS"] = STATE_FILTERS
templates.env.globals["CAMERA_FOLDERS"] = config.CAMERA_FOLDERS
templates.env.globals["mail_enabled"] = mailer.enabled()
templates.env.globals["invite_ttl_hours"] = config.INVITE_TTL_HOURS


# ---------------------------------------------------------------- auth helpers


class LoginRequired(Exception):
    pass


class Forbidden(Exception):
    pass


@app.exception_handler(LoginRequired)
async def _login_required(_request, _exc):
    return RedirectResponse("/login", status_code=303)


@app.exception_handler(Forbidden)
async def _forbidden(_request, _exc):
    return PlainTextResponse("Nemáš oprávnění.", status_code=403)


def current_user(request: Request, admin: bool = False):
    uid = request.session.get("uid")
    if not uid:
        raise LoginRequired()
    with db.get_conn() as conn:
        user = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if user is None:
        request.session.clear()
        raise LoginRequired()
    if admin and user["role"] != "admin":
        raise Forbidden()
    return user


def csrf_token(request: Request) -> str:
    tok = request.session.get("csrf")
    if not tok:
        tok = secrets.token_urlsafe(24)
        request.session["csrf"] = tok
    return tok


async def form_checked(request: Request):
    form = await request.form()
    if not secrets.compare_digest(str(form.get("csrf", "")), request.session.get("csrf", "")):
        raise Forbidden()
    return form


def render(request: Request, name: str, user=None, **ctx):
    flash = request.session.pop("flash", None)
    with db.get_conn() as conn:
        drive_remote = settings.drive_remote(conn)
    return templates.TemplateResponse(
        request,
        name,
        {"user": user, "csrf": csrf_token(request), "flash": flash, "drive_remote": drive_remote, **ctx},
    )


def flash(request: Request, msg: str) -> None:
    request.session["flash"] = msg


def back(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def safe_next(value, default: str) -> str:
    v = str(value or "")
    return v if v.startswith("/") and not v.startswith("//") else default


# ---------------------------------------------------------------- login


@app.get("/healthz")
def healthz():
    return PlainTextResponse("ok")


@app.get("/login")
def login_page(request: Request):
    return render(request, "login.html")


@app.post("/login")
async def login(request: Request):
    form = await form_checked(request)
    username = str(form.get("username", "")).strip()
    password = str(form.get("password", ""))
    with db.get_conn() as conn:
        user = conn.execute(
            "SELECT * FROM users WHERE username=? OR (email<>'' AND lower(email)=lower(?))", (username, username)
        ).fetchone()
        if user is None or not verify_password(password, user["password_hash"]):
            db.audit(conn, username or "?", "login_failed", api.client_ip(request))
            await asyncio.sleep(1)
            flash(request, "Špatné jméno nebo heslo.")
            return back("/login")
        db.audit(conn, username, "login", api.client_ip(request))
    request.session.clear()
    request.session["uid"] = user["id"]
    return back("/")


@app.post("/logout")
async def logout(request: Request):
    await form_checked(request)
    request.session.clear()
    return back("/login")


# ---------------------------------------------------------------- devices


@app.get("/")
def index(request: Request):
    user = current_user(request)
    with db.get_conn() as conn:
        devices = conn.execute(
            """SELECT d.*,
                 (SELECT COUNT(*) FROM files f WHERE f.device_id=d.id AND f.present=1) AS n_present,
                 (SELECT COUNT(*) FROM files f WHERE f.device_id=d.id AND f.state='on_drive') AS n_drive,
                 (SELECT COUNT(*) FROM files f WHERE f.device_id=d.id AND f.state IN ('received','drive_uploading')) AS n_server,
                 (SELECT COUNT(*) FROM commands c WHERE c.device_id=d.id AND c.status IN ('pending','sent')) AS n_cmds
               FROM devices d ORDER BY d.status='pending' DESC, d.name"""
        ).fetchall()
        storage = _storage_info(conn)
    return render(request, "index.html", user, devices=devices, storage=storage)



def _storage_info(conn) -> dict:
    usage = settings.storage_usage(conn)
    quota = settings.quota_bytes(conn)
    disk = shutil.disk_usage(config.DATA_DIR)
    return {
        **usage,
        "quota": quota,
        "percent": min(100, round(100 * usage["total"] / quota)) if quota else None,
        "full": bool(quota) and usage["pending"] >= quota,
        "disk_free": disk.free,
    }


def _get_device(conn, device_id: int):
    dev = conn.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
    if dev is None:
        raise Forbidden()
    return dev


@app.post("/devices/{device_id}/status")
async def device_status(request: Request, device_id: int):
    user = current_user(request, admin=True)
    form = await form_checked(request)
    status = str(form.get("status"))
    if status not in ("approved", "blocked"):
        raise Forbidden()
    with db.get_conn() as conn:
        dev = _get_device(conn, device_id)
        conn.execute("UPDATE devices SET status=? WHERE id=?", (status, device_id))
        db.audit(conn, user["username"], "device_" + status, dev["name"])
    flash(request, "Kamera %s: %s." % (dev["name"], "schválena" if status == "approved" else "zablokována"))
    return back(safe_next(form.get("next"), "/"))


@app.post("/devices/{device_id}/delete")
async def device_delete(request: Request, device_id: int):
    user = current_user(request, admin=True)
    await form_checked(request)
    with db.get_conn() as conn:
        dev = _get_device(conn, device_id)
        conn.execute("DELETE FROM devices WHERE id=?", (device_id,))
        db.audit(conn, user["username"], "device_delete", dev["name"])
    shutil.rmtree(files.device_dir(dev["uid"]), ignore_errors=True)
    flash(request, "Kamera %s odebrána. Pokud se znovu ozve, bude čekat na schválení." % dev["name"])
    return back("/")


@app.post("/devices/{device_id}/merge")
async def device_merge(request: Request, device_id: int):
    """Give an existing camera the identity of a newly registered one (lost device.key on the SD card).

    The old camera keeps its name, settings, file history and partial uploads;
    the new (empty) registration is removed."""
    user = current_user(request, admin=True)
    form = await form_checked(request)
    try:
        target_id = int(str(form.get("target_id", "")))
    except ValueError:
        target_id = 0
    with db.get_conn() as conn:
        new = _get_device(conn, device_id)
        target = _get_device(conn, target_id)
        if new["id"] == target["id"]:
            raise Forbidden()
        with db.tx(conn):
            conn.execute("DELETE FROM devices WHERE id=?", (new["id"],))
            conn.execute(
                "UPDATE devices SET uid=?, key_hash=?, last_seen=?, last_ip=? WHERE id=?",
                (new["uid"], new["key_hash"], new["last_seen"], new["last_ip"], target["id"]),
            )
            db.audit(
                conn, user["username"], "device_merge",
                "%s (%s) -> %s (%s)" % (new["name"], new["uid"], target["name"], target["uid"]),
            )
    old_dir, new_dir = files.device_dir(target["uid"]), files.device_dir(new["uid"])
    if old_dir.exists():
        shutil.rmtree(new_dir, ignore_errors=True)
        old_dir.rename(new_dir)
    flash(request, "Kamera %s teď používá novou identitu, historie zůstala." % target["name"])
    return back("/devices/%d" % target["id"])


@app.get("/devices/{device_id}")
def device_detail(request: Request, device_id: int):
    user = current_user(request)
    qp = request.query_params
    folder = qp.get("folder", "")
    state = qp.get("state", "")
    where = qp.get("where", "camera")
    date_from = qp.get("from", "")
    date_to = qp.get("to", "")
    try:
        page = max(1, int(qp.get("page", "1")))
    except ValueError:
        page = 1

    sql = " FROM files WHERE device_id=?"
    params: list = [device_id]
    if folder:
        sql += " AND folder=?"
        params.append(folder)
    if state in STATE_FILTERS:
        sql += " AND " + STATE_FILTERS[state][1]
    if where == "camera":
        sql += " AND present=1"
    elif where == "gone":
        sql += " AND present=0"
    for value, op, end in ((date_from, ">=", False), (date_to, "<", True)):
        if value:
            try:
                d = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=files.TZ)
            except ValueError:
                continue
            ts = int(d.timestamp()) + (86400 if end else 0)
            sql += " AND recorded_at %s ?" % op
            params.append(ts)

    with db.get_conn() as conn:
        dev = _get_device(conn, device_id)
        total = conn.execute("SELECT COUNT(*)" + sql, params).fetchone()[0]
        rows = conn.execute(
            "SELECT *" + sql + " ORDER BY recorded_at DESC LIMIT ? OFFSET ?",
            params + [PAGE_SIZE, (page - 1) * PAGE_SIZE],
        ).fetchall()
        cmds = conn.execute(
            "SELECT * FROM commands WHERE device_id=? ORDER BY id DESC LIMIT 30", (device_id,)
        ).fetchall()
        pending_delete = {
            r["path"]
            for r in conn.execute(
                "SELECT path FROM commands WHERE device_id=? AND type='delete' AND status IN ('pending','sent')",
                (device_id,),
            )
        }
    progress = {}
    for r in rows:
        if r["state"] == "uploading" and r["size"]:
            part = files.part_path(dev["uid"], r["path"])
            done = part.stat().st_size if part.exists() else 0
            progress[r["id"]] = min(100, round(100 * done / r["size"]))
    filters = {"folder": folder, "state": state, "where": where, "from": date_from, "to": date_to}
    return render(
        request,
        "device.html",
        user,
        dev=dev,
        cfg=db.device_config(dev),
        rows=rows,
        total=total,
        page=page,
        pages=max(1, math.ceil(total / PAGE_SIZE)),
        filters=filters,
        cmds=cmds,
        pending_delete=pending_delete,
        progress=progress,
    )


def _int_field(form, name: str, lo: int, hi: int, default: int) -> int:
    try:
        return min(hi, max(lo, int(str(form.get(name, default)))))
    except ValueError:
        return default


@app.post("/devices/{device_id}/config")
async def device_config(request: Request, device_id: int):
    user = current_user(request, admin=True)
    form = await form_checked(request)
    folders = [f for f in form.getlist("folders") if f in config.CAMERA_FOLDERS]
    d = config.DEFAULT_DEVICE_CONFIG
    cfg = {
        "upload_enabled": 1 if form.get("upload_enabled") else 0,
        "folders": ",".join(folders),
        "rate_kbps": _int_field(form, "rate_kbps", 0, 1_000_000, d["rate_kbps"]),
        "ap_fallback_sec": _int_field(form, "ap_fallback_sec", 0, 86400, d["ap_fallback_sec"]),
        "sta_retry_sec": _int_field(form, "sta_retry_sec", 0, 86400, d["sta_retry_sec"]),
        "poll_sec": _int_field(form, "poll_sec", 10, 3600, d["poll_sec"]),
        "min_age_sec": _int_field(form, "min_age_sec", 0, 3600, d["min_age_sec"]),
        "delete_after_upload": 1 if form.get("delete_after_upload") else 0,
    }
    name = str(form.get("name", "")).strip()[:64]
    with db.get_conn() as conn:
        dev = _get_device(conn, device_id)
        conn.execute(
            "UPDATE devices SET config=?, name=? WHERE id=?", (json.dumps(cfg), name or dev["name"], device_id)
        )
        db.audit(conn, user["username"], "device_config", "%s %s" % (dev["name"], json.dumps(cfg)))
    flash(request, "Nastavení uloženo, kamera si ho stáhne při příštím spojení.")
    return back("/devices/%d" % device_id)


@app.post("/devices/{device_id}/files")
async def files_action(request: Request, device_id: int):
    user = current_user(request, admin=True)
    form = await form_checked(request)
    action = str(form.get("action"))
    ids = [int(x) for x in form.getlist("ids") if str(x).isdigit()]
    nxt = safe_next(form.get("next"), "/devices/%d" % device_id)
    if not ids:
        flash(request, "Nic nevybráno.")
        return back(nxt)
    now = db.now()
    with db.get_conn() as conn:
        dev = _get_device(conn, device_id)
        marks = ",".join("?" * len(ids))
        rows = conn.execute(
            "SELECT * FROM files WHERE device_id=? AND id IN (%s)" % marks, [device_id] + ids
        ).fetchall()
        n = 0
        with db.tx(conn):
            for r in rows:
                if action == "delete" and r["present"]:
                    exists = conn.execute(
                        "SELECT 1 FROM commands WHERE device_id=? AND path=? AND type='delete' "
                        "AND status IN ('pending','sent')",
                        (device_id, r["path"]),
                    ).fetchone()
                    if not exists:
                        conn.execute(
                            "INSERT INTO commands(device_id, type, path, created_by, created_at, updated_at) "
                            "VALUES (?, 'delete', ?, ?, ?, ?)",
                            (device_id, r["path"], user["username"], now, now),
                        )
                        n += 1
                elif action == "upload" and r["present"] and r["state"] in ("camera", "uploading"):
                    conn.execute("UPDATE files SET requested=1 WHERE id=?", (r["id"],))
                    n += 1
                elif action == "unrequest" and r["requested"]:
                    conn.execute("UPDATE files SET requested=0 WHERE id=?", (r["id"],))
                    n += 1
                elif action == "retry" and r["state"] == "received":
                    conn.execute("UPDATE files SET next_try_at=0, attempts=0 WHERE id=?", (r["id"],))
                    n += 1
            if n:
                db.audit(
                    conn,
                    user["username"],
                    "files_" + action,
                    "%s: %s" % (dev["name"], ", ".join(r["path"] for r in rows)[:2000]),
                )
    labels = {
        "delete": "Smazání naplánováno u %d souborů (proběhne při příštím spojení kamery).",
        "upload": "Upload vyžádán u %d souborů.",
        "unrequest": "Žádost o upload zrušena u %d souborů.",
        "retry": "Nový pokus o Drive upload u %d souborů.",
    }
    flash(request, labels.get(action, "%d") % n)
    return back(nxt)


@app.post("/devices/{device_id}/ap")
async def device_ap(request: Request, device_id: int):
    user = current_user(request, admin=True)
    form = await form_checked(request)
    minutes = _int_field(form, "minutes", 5, 24 * 60, 30)
    now = db.now()
    with db.get_conn() as conn:
        dev = _get_device(conn, device_id)
        conn.execute(
            "INSERT INTO commands(device_id, type, path, created_by, created_at, updated_at) VALUES (?, 'ap', ?, ?, ?, ?)",
            (device_id, str(minutes), user["username"], now, now),
        )
        db.audit(conn, user["username"], "device_ap", "%s %d min" % (dev["name"], minutes))
    flash(
        request,
        "Kamera se přepne do AP při příštím spojení (do %d s) na %d minut." % (db.device_config(dev)["poll_sec"], minutes),
    )
    return back("/devices/%d" % device_id)


@app.post("/commands/{cmd_id}/cancel")
async def command_cancel(request: Request, cmd_id: int):
    user = current_user(request, admin=True)
    await form_checked(request)
    with db.get_conn() as conn:
        cmd = conn.execute("SELECT * FROM commands WHERE id=?", (cmd_id,)).fetchone()
        if cmd is None:
            raise Forbidden()
        conn.execute(
            "UPDATE commands SET status='failed', result='zrušeno uživatelem', updated_at=? "
            "WHERE id=? AND status IN ('pending','sent')",
            (db.now(), cmd_id),
        )
        db.audit(conn, user["username"], "command_cancel", cmd["path"])
    return back("/devices/%d" % cmd["device_id"])


_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


def _parse_range(header: str, size: int):
    """Single byte range -> (start, end) inclusive, None for no/unsupported range, False if unsatisfiable."""
    m = _RANGE_RE.match(header.strip()) if header else None
    if not m or (not m.group(1) and not m.group(2)):
        return None
    if m.group(1):
        start = int(m.group(1))
        end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
    else:
        start, end = max(0, size - int(m.group(2))), size - 1
    if start > end or start >= size:
        return False
    return start, end


def _disposition(name: str, inline: bool) -> str:
    return '%s; filename="%s"' % ("inline" if inline else "attachment", name.replace('"', ""))


@app.get("/files/{file_id}/download")
async def file_download(request: Request, file_id: int):
    current_user(request)
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT f.*, d.uid FROM files f JOIN devices d ON d.id=f.device_id WHERE f.id=?", (file_id,)
        ).fetchone()
    if row is None:
        raise Forbidden()
    name = Path(row["path"]).name
    inline = request.query_params.get("inline") == "1"
    local = files.final_path(row["uid"], row["path"])
    if local.exists():
        return FileResponse(local, filename=name, content_disposition_type="inline" if inline else "attachment")
    if row["state"] != "on_drive" or not row["drive_path"]:
        return PlainTextResponse("Soubor není na serveru ani na Google Drive.", status_code=404)
    return await _stream_from_drive(request, row["drive_path"], row["size"], name, inline)


async def _stream_from_drive(request: Request, drive_path: str, size: int, name: str, inline: bool):
    rng = _parse_range(request.headers.get("range", ""), size)
    if rng is False:
        return PlainTextResponse("", status_code=416, headers={"Content-Range": "bytes */%d" % size})
    args = [config.RCLONE_BIN, "cat", drive_path]
    start, end = rng if rng else (0, size - 1)
    if rng:
        args += ["--offset", str(start), "--count", str(end - start + 1)]
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
    )
    # Fail early (e.g. file deleted on Drive) instead of sending a broken 200.
    first = await proc.stdout.read(STREAM_CHUNK)
    if not first and size > 0:
        await proc.wait()
        return PlainTextResponse("Soubor se nepodařilo načíst z Google Drive.", status_code=502)

    async def body():
        try:
            yield first
            while chunk := await proc.stdout.read(STREAM_CHUNK):
                yield chunk
        finally:
            if proc.returncode is None:
                proc.kill()
            await proc.wait()

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(end - start + 1),
        "Content-Disposition": _disposition(name, inline),
    }
    if rng:
        headers["Content-Range"] = "bytes %d-%d/%d" % (start, end, size)
    media = mimetypes.guess_type(name)[0] or "application/octet-stream"
    return StreamingResponse(body(), status_code=206 if rng else 200, headers=headers, media_type=media)


# ---------------------------------------------------------------- settings


@app.get("/settings")
def settings_page(request: Request):
    user = current_user(request, admin=True)
    with db.get_conn() as conn:
        values = settings.get_all(conn)
        storage = _storage_info(conn)
    custom_remote = bool(config.DRIVE_REMOTE) and not config.DRIVE_REMOTE.startswith(config.GDRIVE_REMOTE_NAME + ":")
    return render(
        request,
        "settings.html",
        user,
        s=values,
        storage=storage,
        google_connected=gdrive.connected(),
        oauth_available=gdrive.oauth_available(),
        picker_available=gdrive.picker_available(),
        picker_missing_key=gdrive.oauth_available() and not config.GOOGLE_API_KEY,
        redirect_uri=gdrive.redirect_uri(_public_base(request)),
        custom_remote=config.DRIVE_REMOTE if custom_remote else "",
    )


_FOLDER_RE = re.compile(r"^[^\\:*?\"<>|]{1,200}$")


@app.post("/settings")
async def settings_save(request: Request):
    user = current_user(request, admin=True)
    form = await form_checked(request)
    folder = str(form.get("drive_folder", "")).strip().strip("/")
    with db.get_conn() as conn:
        picked = bool(settings.get(conn, "drive_folder_id"))
    # Inside a picked folder the sub-folder is optional; in My Drive a folder name is required.
    if (folder or not picked) and (not _FOLDER_RE.match(folder) or ".." in folder.split("/")):
        flash(request, "Neplatný název složky.")
        return back("/settings")
    try:
        quota = max(0.0, float(str(form.get("storage_quota_gb", "0")).replace(",", ".")))
    except ValueError:
        flash(request, "Limit místa musí být číslo.")
        return back("/settings")
    values = {
        "drive_folder": folder,
        "storage_quota_gb": ("%g" % quota),
        "keep_local_copy": "1" if form.get("keep_local_copy") else "0",
    }
    with db.get_conn() as conn:
        settings.set_many(conn, values)
        db.audit(conn, user["username"], "settings", json.dumps(values, ensure_ascii=False))
    flash(request, "Nastavení uloženo.")
    return back("/settings")


@app.post("/settings/google/check")
async def google_check(request: Request):
    current_user(request, admin=True)
    await form_checked(request)
    with db.get_conn() as conn:
        remote = settings.drive_remote(conn)
    if not remote:
        flash(request, "Google Drive není připojený.")
        return back("/settings")
    ok, msg = await asyncio.to_thread(gdrive.check_folder, remote)
    flash(request, ("OK: " if ok else "Chyba: ") + msg)
    return back("/settings")


@app.get("/settings/google/connect")
def google_connect(request: Request):
    current_user(request, admin=True)
    if not gdrive.oauth_available():
        raise Forbidden()
    state = secrets.token_urlsafe(24)
    request.session["oauth_state"] = state
    return RedirectResponse(gdrive.auth_url(_public_base(request), state), status_code=303)


@app.get("/settings/google/callback")
async def google_callback(request: Request):
    user = current_user(request, admin=True)
    qp = request.query_params
    expected = request.session.pop("oauth_state", None)
    if not expected or not secrets.compare_digest(qp.get("state", ""), expected):
        flash(request, "Neplatná odpověď od Google (state), zkus to znovu.")
        return back("/settings")
    if qp.get("error") or not qp.get("code"):
        flash(request, "Google připojení zrušeno: %s" % qp.get("error", "bez kódu"))
        return back("/settings")
    try:
        token = await asyncio.to_thread(gdrive.exchange_code, qp["code"], _public_base(request))
    except Exception as exc:  # noqa: BLE001
        flash(request, "Výměna kódu za token selhala: %s" % exc)
        return back("/settings")
    email = await asyncio.to_thread(gdrive.account_email, token)
    gdrive.save(token, own_client=True)
    with db.get_conn() as conn:
        settings.set_many(conn, {"google_account": email})
        db.audit(conn, user["username"], "google_connect", email)
    flash(request, "Google Drive připojen%s." % (" (%s)" % email if email else ""))
    return back("/settings")


@app.post("/settings/google/token")
async def google_token(request: Request):
    user = current_user(request, admin=True)
    form = await form_checked(request)
    try:
        token = gdrive.parse_pasted_token(str(form.get("token", "")))
    except ValueError as exc:
        flash(request, "Token nejde načíst: %s" % exc)
        return back("/settings")
    email = await asyncio.to_thread(gdrive.account_email, token)
    gdrive.save(token, own_client=False, scope="drive")
    with db.get_conn() as conn:
        settings.set_many(conn, {"google_account": email})
        db.audit(conn, user["username"], "google_connect", email or "token")
    flash(request, "Google Drive připojen%s." % (" (%s)" % email if email else ""))
    return back("/settings")


_DRIVE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,200}$")


@app.get("/settings/google/picker-token")
async def google_picker_token(request: Request):
    current_user(request, admin=True)
    if not gdrive.picker_available():
        raise Forbidden()
    try:
        token = await asyncio.to_thread(gdrive.fresh_access_token)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": str(exc)}, status_code=502)
    return JSONResponse(
        {"token": token, "api_key": config.GOOGLE_API_KEY, "app_id": gdrive.project_number()},
        headers={"Cache-Control": "no-store"},
    )


@app.post("/settings/google/folder")
async def google_folder(request: Request):
    user = current_user(request, admin=True)
    form = await form_checked(request)
    folder_id = str(form.get("folder_id", "")).strip()
    name = str(form.get("folder_name", "")).strip()[:200]
    if folder_id and not _DRIVE_ID_RE.match(folder_id):
        flash(request, "Neplatné ID složky.")
        return back("/settings")
    values = {"drive_folder_id": folder_id, "drive_folder_name": name if folder_id else ""}
    with db.get_conn() as conn:
        if folder_id:
            # The picked folder itself is the target; sub-folder stays optional.
            values["drive_folder"] = str(form.get("subfolder", "")).strip().strip("/")
        elif not settings.get(conn, "drive_folder"):
            values["drive_folder"] = "70mai"
        settings.set_many(conn, values)
        db.audit(conn, user["username"], "drive_folder", "%s %s" % (folder_id or "My Drive", name))
        remote = settings.drive_remote(conn)
    if folder_id:
        ok, msg = await asyncio.to_thread(gdrive.check_folder, remote)
        flash(request, ("Složka vybrána: %s. " % (name or folder_id)) + ("OK: " if ok else "Chyba: ") + msg)
    else:
        flash(request, "Ukládá se zase do Můj disk / %s." % settings_folder_name(remote))
    return back("/settings")


def settings_folder_name(remote: str) -> str:
    return remote.split(":", 1)[1] if ":" in remote else remote


@app.post("/settings/google/disconnect")
async def google_disconnect(request: Request):
    user = current_user(request, admin=True)
    await form_checked(request)
    gdrive.disconnect()
    with db.get_conn() as conn:
        settings.set_many(conn, {"google_account": ""})
        db.audit(conn, user["username"], "google_disconnect", "")
    flash(request, "Google Drive odpojen. Nové soubory zůstanou na serveru (v rámci limitu místa).")
    return back("/settings")


# ---------------------------------------------------------------- users & invitations


def _admin_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM users WHERE role='admin'").fetchone()[0]


def _public_base(request: Request) -> str:
    return (config.PUBLIC_URL or str(request.base_url)).rstrip("/")


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _issue_invitation(conn, request: Request, inv_id: int, email: str, role: str, invited_by: str):
    """Give the invitation a fresh token and expiry; queue the e-mail. Returns (link, outbox_id)."""
    token = secrets.token_urlsafe(32)
    expires = db.now() + config.INVITE_TTL_HOURS * 3600
    conn.execute(
        "UPDATE invitations SET token_hash=?, expires_at=? WHERE id=?", (_token_hash(token), expires, inv_id)
    )
    link = "%s/invite/%s" % (_public_base(request), token)
    outbox_id = None
    if mailer.enabled():
        subject, body = mailer.invitation(link, role, invited_by, expires)
        outbox_id = mailer.outbox_add(conn, email, subject, body)
    return link, outbox_id


async def _deliver_now(outbox_id) -> bool:
    if outbox_id is None:
        return False

    def run():
        with db.get_conn() as conn:
            return mailer.deliver_pending(conn, only_id=outbox_id)

    return await asyncio.to_thread(run)


def _invite_flash(request: Request, email: str, link: str, sent: bool) -> None:
    if sent:
        flash(request, "Pozvánka odeslána na %s." % email)
    elif mailer.enabled():
        flash(request, "E-mail se teď nepodařilo odeslat, zkusí se znovu. Odkaz můžeš poslat i ručně: %s" % link)
    else:
        flash(request, "SMTP není nastavené, pošli pozvánku ručně: %s" % link)


@app.get("/users")
def users_page(request: Request):
    user = current_user(request, admin=True)
    with db.get_conn() as conn:
        users = conn.execute("SELECT * FROM users ORDER BY username").fetchall()
        invites = conn.execute(
            "SELECT * FROM invitations WHERE accepted_at IS NULL ORDER BY id DESC"
        ).fetchall()
    return render(request, "users.html", user, users=users, invites=invites, now=db.now())


@app.post("/invitations")
async def invitation_create(request: Request):
    user = current_user(request, admin=True)
    form = await form_checked(request)
    email = str(form.get("email", "")).strip()
    role = "admin" if form.get("role") == "admin" else "viewer"
    if not mailer.valid_email(email):
        flash(request, "Neplatný e-mail.")
        return back("/users")
    with db.get_conn() as conn:
        if conn.execute("SELECT 1 FROM users WHERE lower(email)=lower(?)", (email,)).fetchone():
            flash(request, "Uživatel s tímto e-mailem už existuje.")
            return back("/users")
        with db.tx(conn):
            conn.execute(
                "DELETE FROM invitations WHERE lower(email)=lower(?) AND accepted_at IS NULL", (email,)
            )
            cur = conn.execute(
                "INSERT INTO invitations(email, role, token_hash, expires_at, created_by, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (email, role, secrets.token_hex(16), 0, user["username"], db.now()),
            )
            link, outbox_id = _issue_invitation(conn, request, cur.lastrowid, email, role, user["username"])
            db.audit(conn, user["username"], "invite", "%s (%s)" % (email, role))
    _invite_flash(request, email, link, await _deliver_now(outbox_id))
    return back("/users")


@app.post("/invitations/{inv_id}/resend")
async def invitation_resend(request: Request, inv_id: int):
    user = current_user(request, admin=True)
    await form_checked(request)
    with db.get_conn() as conn:
        inv = conn.execute("SELECT * FROM invitations WHERE id=? AND accepted_at IS NULL", (inv_id,)).fetchone()
        if inv is None:
            raise Forbidden()
        with db.tx(conn):
            link, outbox_id = _issue_invitation(conn, request, inv_id, inv["email"], inv["role"], user["username"])
            db.audit(conn, user["username"], "invite_resend", inv["email"])
    _invite_flash(request, inv["email"], link, await _deliver_now(outbox_id))
    return back("/users")


@app.post("/invitations/{inv_id}/delete")
async def invitation_delete(request: Request, inv_id: int):
    user = current_user(request, admin=True)
    await form_checked(request)
    with db.get_conn() as conn:
        inv = conn.execute("SELECT * FROM invitations WHERE id=? AND accepted_at IS NULL", (inv_id,)).fetchone()
        if inv is None:
            raise Forbidden()
        conn.execute("DELETE FROM invitations WHERE id=?", (inv_id,))
        db.audit(conn, user["username"], "invite_delete", inv["email"])
    flash(request, "Pozvánka pro %s zrušena." % inv["email"])
    return back("/users")


def _valid_invitation(conn, token: str):
    inv = conn.execute("SELECT * FROM invitations WHERE token_hash=?", (_token_hash(token),)).fetchone()
    if inv is None or inv["accepted_at"] or inv["expires_at"] < db.now():
        return None
    return inv


@app.get("/invite/{token}")
def invite_page(request: Request, token: str):
    with db.get_conn() as conn:
        inv = _valid_invitation(conn, token)
    if inv is None:
        return render(request, "invite.html", invalid=True)
    return render(request, "invite.html", inv=inv, token=token, invalid=False)


_USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,32}$")


@app.post("/invite/{token}")
async def invite_accept(request: Request, token: str):
    form = await form_checked(request)
    username = str(form.get("username", "")).strip()
    password = str(form.get("password", ""))
    email = str(form.get("email", "")).strip()
    with db.get_conn() as conn:
        inv = _valid_invitation(conn, token)
        if inv is None:
            return render(request, "invite.html", invalid=True)
        error = None
        if not _USERNAME_RE.match(username):
            error = "Jméno: 3–32 znaků, písmena bez diakritiky, číslice, tečka, pomlčka, podtržítko."
        elif len(password) < 8:
            error = "Heslo musí mít aspoň 8 znaků."
        elif password != str(form.get("password2", "")):
            error = "Hesla se neshodují."
        elif not mailer.valid_email(email):
            error = "Neplatný e-mail."
        elif conn.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
            error = "Jméno už je obsazené."
        elif conn.execute("SELECT 1 FROM users WHERE lower(email)=lower(?)", (email,)).fetchone():
            error = "Tento e-mail už používá jiný účet."
        if error:
            flash(request, error)
            return back("/invite/%s" % token)
        is_admin = inv["role"] == "admin"
        with db.tx(conn):
            cur = conn.execute(
                "INSERT INTO users(username, password_hash, role, created_at, email, "
                "notify_uploads, notify_errors, notify_devices) VALUES (?,?,?,?,?,?,?,?)",
                (
                    username,
                    hash_password(password),
                    inv["role"],
                    db.now(),
                    email,
                    1 if form.get("notify_uploads") else 0,
                    1 if form.get("notify_errors") else 0,
                    1 if is_admin and form.get("notify_devices") else 0,
                ),
            )
            conn.execute(
                "UPDATE invitations SET accepted_at=?, accepted_user_id=? WHERE id=?",
                (db.now(), cur.lastrowid, inv["id"]),
            )
            db.audit(conn, username, "register", "%s (%s), pozval %s" % (email, inv["role"], inv["created_by"]))
    request.session.clear()
    request.session["uid"] = cur.lastrowid
    flash(request, "Účet vytvořen. Vítej!")
    return back("/")


@app.post("/users/{user_id}/update")
async def user_update(request: Request, user_id: int):
    user = current_user(request, admin=True)
    form = await form_checked(request)
    role = "admin" if form.get("role") == "admin" else "viewer"
    password = str(form.get("password", ""))
    with db.get_conn() as conn:
        target = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if target is None:
            raise Forbidden()
        if target["role"] == "admin" and role != "admin" and _admin_count(conn) <= 1:
            flash(request, "Poslední admin nemůže přijít o roli.")
            return back("/users")
        if password and len(password) < 8:
            flash(request, "Heslo musí mít aspoň 8 znaků.")
            return back("/users")
        conn.execute("UPDATE users SET role=? WHERE id=?", (role, user_id))
        if role != "admin":
            conn.execute("UPDATE users SET notify_devices=0 WHERE id=?", (user_id,))
        if password:
            conn.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password(password), user_id))
        db.audit(conn, user["username"], "user_update", "%s role=%s%s" % (target["username"], role, " +heslo" if password else ""))
    flash(request, "Uživatel %s upraven." % target["username"])
    return back("/users")


@app.post("/users/{user_id}/delete")
async def user_delete(request: Request, user_id: int):
    user = current_user(request, admin=True)
    await form_checked(request)
    with db.get_conn() as conn:
        target = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if target is None:
            raise Forbidden()
        if target["id"] == user["id"]:
            flash(request, "Sám sebe smazat nejde.")
            return back("/users")
        if target["role"] == "admin" and _admin_count(conn) <= 1:
            flash(request, "Posledního admina smazat nejde.")
            return back("/users")
        conn.execute("DELETE FROM users WHERE id=?", (user_id,))
        db.audit(conn, user["username"], "user_delete", target["username"])
    flash(request, "Uživatel %s smazán." % target["username"])
    return back("/users")


# ---------------------------------------------------------------- profile


@app.get("/account")
def account_page(request: Request):
    user = current_user(request)
    return render(request, "account.html", user)


@app.post("/account/profile")
async def profile_update(request: Request):
    user = current_user(request)
    form = await form_checked(request)
    email = str(form.get("email", "")).strip()
    if email and not mailer.valid_email(email):
        flash(request, "Neplatný e-mail.")
        return back("/account")
    prefs = {k: 1 if form.get(k) else 0 for k in ("notify_uploads", "notify_errors", "notify_devices")}
    if user["role"] != "admin":
        prefs["notify_devices"] = 0
    if any(prefs.values()) and not email:
        flash(request, "Pro upozornění vyplň e-mail.")
        return back("/account")
    with db.get_conn() as conn:
        if email and conn.execute(
            "SELECT 1 FROM users WHERE lower(email)=lower(?) AND id<>?", (email, user["id"])
        ).fetchone():
            flash(request, "Tento e-mail už používá jiný účet.")
            return back("/account")
        conn.execute(
            "UPDATE users SET email=?, notify_uploads=?, notify_errors=?, notify_devices=? WHERE id=?",
            (email or None, prefs["notify_uploads"], prefs["notify_errors"], prefs["notify_devices"], user["id"]),
        )
        db.audit(conn, user["username"], "profile_update", "%s %s" % (email, prefs))
    flash(request, "Profil uložen.")
    return back("/account")


@app.post("/account/password")
async def account_password(request: Request):
    user = current_user(request)
    form = await form_checked(request)
    if not verify_password(str(form.get("old", "")), user["password_hash"]):
        flash(request, "Současné heslo nesedí.")
        return back("/account")
    new = str(form.get("new", ""))
    if len(new) < 8:
        flash(request, "Nové heslo musí mít aspoň 8 znaků.")
        return back("/account")
    with db.get_conn() as conn:
        conn.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password(new), user["id"]))
        db.audit(conn, user["username"], "password_change", "")
    flash(request, "Heslo změněno.")
    return back("/account")


@app.post("/account/test-mail")
async def account_test_mail(request: Request):
    user = current_user(request)
    await form_checked(request)
    if not mailer.enabled():
        flash(request, "SMTP není na serveru nastavené.")
        return back("/account")
    if not user["email"]:
        flash(request, "Nejdřív vyplň e-mail.")
        return back("/account")
    try:
        await asyncio.to_thread(
            mailer.send, user["email"], "Dashcam: testovací e-mail", "E-mailová upozornění fungují.\n"
        )
    except Exception as exc:  # noqa: BLE001
        flash(request, "Odeslání selhalo: %s" % exc)
        return back("/account")
    flash(request, "Testovací e-mail odeslán na %s." % user["email"])
    return back("/account")


@app.get("/audit")
def audit_page(request: Request):
    user = current_user(request, admin=True)
    with db.get_conn() as conn:
        rows = conn.execute("SELECT * FROM audit ORDER BY id DESC LIMIT 500").fetchall()
    return render(request, "audit.html", user, rows=rows)
