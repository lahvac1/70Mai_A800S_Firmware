"""E-mail: invitations and per-user notifications.

Every e-mail goes through mail_outbox and is delivered by the worker thread,
with retries when the SMTP server is unavailable.

Upload notifications are batched: the worker records every finished upload and
every upload failure into mail_queue. Once the upload pipeline has been quiet
for MAIL_QUIET_SEC (no camera is sending chunks and nothing waits for Drive),
each subscribed user gets one summary with the parts they asked for.
"""
import logging
import re
import smtplib
import ssl
from collections import defaultdict
from datetime import datetime
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from . import config, db, files

log = logging.getLogger("mailer")

MAX_LINES = 500
MAX_ATTEMPTS = 10
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def enabled() -> bool:
    return bool(config.SMTP_HOST)


def valid_email(addr: str) -> bool:
    return bool(_EMAIL_RE.match(addr)) and len(addr) <= 254


def send(to_addr: str, subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = config.SMTP_FROM or config.SMTP_USER
    msg["To"] = to_addr
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="dashcam")
    msg.set_content(body)
    ctx = ssl.create_default_context()
    if config.SMTP_SECURITY == "ssl":
        smtp = smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, timeout=30, context=ctx)
    else:
        smtp = smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=30)
    with smtp:
        if config.SMTP_SECURITY == "starttls":
            smtp.starttls(context=ctx)
        if config.SMTP_USER:
            smtp.login(config.SMTP_USER, config.SMTP_PASSWORD)
        smtp.send_message(msg)


# ---------------------------------------------------------------- outbox


def outbox_add(conn, to_addr: str, subject: str, body: str) -> int:
    cur = conn.execute(
        "INSERT INTO mail_outbox(to_addr, subject, body, created_at) VALUES (?,?,?,?)",
        (to_addr, subject, body, db.now()),
    )
    return cur.lastrowid


def deliver_pending(conn, only_id: int | None = None) -> bool:
    """Send due e-mails from the outbox. Returns False if any delivery failed."""
    if not enabled():
        return False
    if only_id is not None:
        rows = conn.execute("SELECT * FROM mail_outbox WHERE id=?", (only_id,)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM mail_outbox WHERE next_try_at<=? ORDER BY id LIMIT 20", (db.now(),)
        ).fetchall()
    all_ok = True
    for r in rows:
        try:
            send(r["to_addr"], r["subject"], r["body"])
        except Exception as exc:  # noqa: BLE001
            all_ok = False
            attempts = r["attempts"] + 1
            log.warning("e-mail to %s failed (attempt %d): %s", r["to_addr"], attempts, exc)
            if attempts >= MAX_ATTEMPTS:
                conn.execute("DELETE FROM mail_outbox WHERE id=?", (r["id"],))
                log.error("giving up e-mail to %s: %s", r["to_addr"], r["subject"])
            else:
                conn.execute(
                    "UPDATE mail_outbox SET attempts=?, next_try_at=?, last_error=? WHERE id=?",
                    (attempts, db.now() + 300 * attempts, str(exc)[:500], r["id"]),
                )
            continue
        conn.execute("DELETE FROM mail_outbox WHERE id=?", (r["id"],))
        log.info("e-mail sent to %s: %s", r["to_addr"], r["subject"])
    return all_ok


def _footer() -> str:
    lines = ["", "--"]
    if config.PUBLIC_URL:
        lines.append(config.PUBLIC_URL)
    lines.append("Upozornění si nastavíš na svém profilu.")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- templates


def plural(n: int, one: str, few: str, many: str) -> str:
    """Czech plural: 1 video, 2-4 videa, 5+ videí."""
    return "%d %s" % (n, one if n == 1 else few if 2 <= n <= 4 else many)


def _size(n) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%.1f %s" % (n, unit)
        n /= 1024


def _dt(ts) -> str:
    return datetime.fromtimestamp(int(ts), files.TZ).strftime("%d.%m.%Y %H:%M:%S")


def compose(rows) -> tuple[str, str]:
    ok = [r for r in rows if r["kind"] == "ok"]
    fail = [r for r in rows if r["kind"] == "fail"]
    parts = []
    if ok:
        parts.append(plural(len(ok), "video", "videa", "videí") + " na Google Drive")
    if fail:
        parts.append(plural(len(fail), "chyba", "chyby", "chyb") + " uploadu")
    subject = "Dashcam: " + ", ".join(parts)

    lines = []
    if fail:
        lines.append("CHYBY UPLOADU NA GOOGLE DRIVE")
        lines.append("Soubory zůstávají na serveru a worker to bude dál zkoušet.")
        lines.append("")
        for r in fail[:MAX_LINES]:
            lines.append("  [%s] %s  (pokus %d)" % (r["device_name"], r["path"], r["attempts"] + 1))
            lines.append("      %s" % (r["error"] or "").strip().replace("\n", " ")[-300:])
        lines.append("")
    if ok:
        total = sum(r["size"] or 0 for r in ok)
        lines.append(
            "NAHRÁNO NA GOOGLE DRIVE: %s, %s" % (plural(len(ok), "soubor", "soubory", "souborů"), _size(total))
        )
        by_dev = defaultdict(list)
        for r in ok:
            by_dev[r["device_name"]].append(r)
        for dev, items in by_dev.items():
            lines.append("")
            lines.append("%s:" % dev)
            for r in sorted(items, key=lambda x: x["recorded_at"])[:MAX_LINES]:
                lines.append("  %s  %-48s %10s" % (_dt(r["recorded_at"]), r["path"], _size(r["size"])))
                lines.append("      -> %s" % r["drive_path"])
    if len(rows) > MAX_LINES:
        lines.append("")
        lines.append("(zkráceno, celkem %d položek)" % len(rows))
    return subject, "\n".join(lines) + "\n" + _footer()


def invitation(link: str, role: str, invited_by: str, expires_at: int) -> tuple[str, str]:
    subject = "Pozvánka do Dashcam upload"
    body = (
        "Ahoj,\n\n"
        "%s tě pozval(a) do Dashcam upload (role: %s).\n"
        "Účet si založíš a nastavíš upozornění na tomto odkazu:\n\n"
        "  %s\n\n"
        "Odkaz platí do %s a dá se použít jen jednou.\n"
        "Pokud pozvánku nečekáš, e-mail ignoruj.\n"
    ) % (invited_by, role, link, _dt(expires_at))
    return subject, body


# ---------------------------------------------------------------- notifications


def queue(conn, kind: str, row, drive_path: str = "", error: str = "") -> None:
    """Record a finished ('ok') or failed ('fail') Drive upload for the next summary."""
    if not enabled():
        return
    conn.execute(
        "INSERT INTO mail_queue(ts, kind, device_name, path, size, recorded_at, drive_path, error, attempts) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (db.now(), kind, row["device_name"], row["path"], row["size"], row["recorded_at"], drive_path,
         error, row["attempts"]),
    )


def flush_if_quiet(conn) -> None:
    """Turn the queued upload results into one summary per subscribed user once idle."""
    if not enabled():
        return
    last_ts, last_id = conn.execute("SELECT MAX(ts), MAX(id) FROM mail_queue").fetchone()
    if last_id is None:
        return
    busy = conn.execute(
        "SELECT MAX(updated_at) FROM files WHERE state IN ('uploading','drive_uploading') "
        "OR (state='received' AND next_try_at<=?)",
        (db.now(),),
    ).fetchone()[0]
    if db.now() - max(last_ts, busy or 0) < config.MAIL_QUIET_SEC:
        return
    rows = conn.execute("SELECT * FROM mail_queue WHERE id<=? ORDER BY id", (last_id,)).fetchall()
    users = conn.execute(
        "SELECT * FROM users WHERE email IS NOT NULL AND email<>'' AND (notify_uploads=1 OR notify_errors=1)"
    ).fetchall()
    with db.tx(conn):
        for u in users:
            subset = [
                r for r in rows
                if (r["kind"] == "ok" and u["notify_uploads"]) or (r["kind"] == "fail" and u["notify_errors"])
            ]
            if subset:
                subject, body = compose(subset)
                outbox_add(conn, u["email"], subject, body)
        conn.execute("DELETE FROM mail_queue WHERE id<=?", (last_id,))


def new_device(conn, name: str, ip: str) -> None:
    if not enabled():
        return
    users = conn.execute(
        "SELECT email FROM users WHERE role='admin' AND notify_devices=1 AND email IS NOT NULL AND email<>''"
    ).fetchall()
    body = (
        "Nová kamera se připojila a čeká na schválení:\n\n  %s (IP %s)\n\n"
        "Schválíš ji v přehledu kamer.\n" % (name, ip)
    ) + _footer()
    for u in users:
        outbox_add(conn, u["email"], "Dashcam: nová kamera čeká na schválení", body)
