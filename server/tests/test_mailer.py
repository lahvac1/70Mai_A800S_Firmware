import os
import sys
import tempfile
from pathlib import Path

import pytest

if "DATA_DIR" not in os.environ:
    os.environ.update(DATA_DIR=tempfile.mkdtemp(), SECRET_KEY="x" * 40, WORKER_ENABLED="false")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config, db, files, mailer, settings, worker  # noqa: E402


@pytest.fixture()
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.sqlite3")
    monkeypatch.setattr(config, "INCOMING_DIR", tmp_path / "incoming")
    monkeypatch.setattr(config, "SMTP_HOST", "smtp.test")
    monkeypatch.setattr(config, "MAIL_QUIET_SEC", 300)
    monkeypatch.setattr(config, "DRIVE_REMOTE", ":local:" + str(tmp_path / "drive"))
    sent = []
    monkeypatch.setattr(mailer, "send", lambda to, subject, body: sent.append((to, subject, body)))
    db.init_db()
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO devices(id, uid, key_hash, name, status, created_at) "
            "VALUES (1, 'abcdef0123456789', 'x', 'Octavia', 'approved', 0)"
        )
        users = [
            ("all", "all@test", 1, 1, 1, "admin"),
            ("errors", "errors@test", 0, 1, 0, "viewer"),
            ("none", "none@test", 0, 0, 0, "viewer"),
        ]
        for name, email, up, err, dev, role in users:
            conn.execute(
                "INSERT INTO users(username, password_hash, role, created_at, email, notify_uploads, "
                "notify_errors, notify_devices) VALUES (?,?,?,?,?,?,?,?)",
                (name, "x", role, 0, email, up, err, dev),
            )
    return sent


def flush(conn):
    mailer.flush_if_quiet(conn)
    mailer.deliver_pending(conn)


def add_received(conn, path, size=5):
    p = files.final_path("abcdef0123456789", path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * size)
    conn.execute(
        "INSERT INTO files(device_id, path, folder, size, mtime, recorded_at, state, updated_at) "
        "VALUES (1, ?, 'Event', ?, 1759565700, 1759565700, 'received', ?)",
        (path, size, db.now()),
    )


def next_row(conn):
    return conn.execute(
        "SELECT f.*, d.uid, d.name AS device_name, d.config FROM files f JOIN devices d ON d.id=f.device_id "
        "WHERE f.state='received' AND f.next_try_at<=? ORDER BY f.id LIMIT 1",
        (db.now(),),
    ).fetchone()


def age_everything(conn, seconds):
    conn.execute("UPDATE mail_queue SET ts=ts-?", (seconds,))
    conn.execute("UPDATE files SET updated_at=updated_at-?", (seconds,))


def test_one_summary_after_queue_is_done(env, monkeypatch):
    sent = env
    with db.get_conn() as conn:
        add_received(conn, "Event/Front/EV20261004-101500-000001F.MP4")
        add_received(conn, "Event/Front/EV20261004-101600-000002F.MP4")
        worker._upload_one(conn, next_row(conn), settings.drive_remote(conn))
        # Queue not finished yet: no e-mail.
        flush(conn)
        worker._upload_one(conn, next_row(conn), settings.drive_remote(conn))
        assert next_row(conn) is None
        # Finished but not quiet long enough (a camera may still be sending).
        flush(conn)
        assert sent == []
        age_everything(conn, 301)
        flush(conn)
        # Only the user subscribed to uploads gets the summary.
        assert [m[0] for m in sent] == ["all@test"]
        to, subject, body = sent[0]
        assert "2 videa na Google Drive" in subject
        assert "EV20261004-101500-000001F.MP4" in body and "Octavia" in body
        flush(conn)
        assert len(sent) == 1
        assert conn.execute("SELECT COUNT(*) FROM mail_queue").fetchone()[0] == 0


def test_camera_still_uploading_delays_summary(env):
    sent = env
    with db.get_conn() as conn:
        add_received(conn, "Event/Front/EV20261004-101500-000001F.MP4")
        worker._upload_one(conn, next_row(conn), settings.drive_remote(conn))
        age_everything(conn, 301)
        conn.execute(
            "INSERT INTO files(device_id, path, folder, size, mtime, recorded_at, state, updated_at) "
            "VALUES (1, 'Event/Front/EV2.MP4', 'Event', 9, 0, 0, 'uploading', ?)",
            (db.now(),),
        )
        flush(conn)
        assert sent == []


def test_failure_reported_once(env, monkeypatch):
    sent = env
    monkeypatch.setattr(config, "RCLONE_BIN", "false")
    with db.get_conn() as conn:
        add_received(conn, "Event/Front/EV20261004-101500-000001F.MP4")
        worker._upload_one(conn, next_row(conn), settings.drive_remote(conn))
        conn.execute("UPDATE files SET next_try_at=0")
        worker._upload_one(conn, next_row(conn), settings.drive_remote(conn))  # attempt 2: not reported again
        assert conn.execute("SELECT COUNT(*) FROM mail_queue WHERE kind='fail'").fetchone()[0] == 1
        conn.execute("UPDATE files SET next_try_at=? ", (db.now() + 600,))
        age_everything(conn, 301)
        flush(conn)
        assert sorted(m[0] for m in sent) == ["all@test", "errors@test"]
        assert all("1 chyba uploadu" in m[1] for m in sent)


def test_send_failure_is_retried(env, monkeypatch):
    def boom(to, subject, body):
        raise OSError("connection refused")

    monkeypatch.setattr(mailer, "send", boom)
    with db.get_conn() as conn:
        add_received(conn, "Event/Front/EV20261004-101500-000001F.MP4")
        worker._upload_one(conn, next_row(conn), settings.drive_remote(conn))
        age_everything(conn, 301)
        flush(conn)
        row = conn.execute("SELECT * FROM mail_outbox").fetchone()
        assert row["to_addr"] == "all@test" and row["attempts"] == 1 and row["next_try_at"] > db.now()
        sent = []
        monkeypatch.setattr(mailer, "send", lambda to, subject, body: sent.append(to))
        conn.execute("UPDATE mail_outbox SET next_try_at=0")
        mailer.deliver_pending(conn)
        assert sent == ["all@test"]
        assert conn.execute("SELECT COUNT(*) FROM mail_outbox").fetchone()[0] == 0


def test_new_device_alert_only_for_subscribed_admins(env):
    sent = env
    with db.get_conn() as conn:
        mailer.new_device(conn, "Kamera X", "1.2.3.4")
        mailer.deliver_pending(conn)
    assert [m[0] for m in sent] == ["all@test"] and "Kamera X" in sent[0][2]


def test_czech_plural():
    assert mailer.plural(1, "video", "videa", "videí") == "1 video"
    assert mailer.plural(3, "video", "videa", "videí") == "3 videa"
    assert mailer.plural(5, "video", "videa", "videí") == "5 videí"
