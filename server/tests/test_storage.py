import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

if "DATA_DIR" not in os.environ:
    os.environ.update(DATA_DIR=tempfile.mkdtemp(), SECRET_KEY="x" * 40, WORKER_ENABLED="false")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import api, config, db, files, gdrive, settings, worker  # noqa: E402

UID = "abcdef0123456789"
HAVE_RCLONE = shutil.which("rclone") is not None


@pytest.fixture()
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.sqlite3")
    monkeypatch.setattr(config, "INCOMING_DIR", tmp_path / "incoming")
    monkeypatch.setattr(config, "RCLONE_CONFIG", str(tmp_path / "rclone.conf"))
    monkeypatch.setattr(config, "SMTP_HOST", "")
    monkeypatch.setattr(config, "DRIVE_REMOTE", "")
    db.init_db()
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO devices(id, uid, key_hash, name, status, created_at) VALUES (1, ?, 'x', 'Octavia', 'approved', 0)",
            (UID,),
        )
    return tmp_path


def add_file(conn, path, size, state="camera", local=0, recorded=1759565700, updated=None):
    if local:
        p = files.final_path(UID, path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * size)
    conn.execute(
        "INSERT INTO files(device_id, path, folder, size, mtime, recorded_at, state, local, updated_at) "
        "VALUES (1, ?, ?, ?, 0, ?, ?, ?, ?)",
        (path, path.split("/")[0], size, recorded, state, local, updated or db.now()),
    )


def offered(conn):
    """Paths the sync endpoint would offer for upload right now."""
    class Req:
        headers = {"x-device-id": UID, "x-device-key": "k" * 32}
        client = None

    from app.security import hash_device_key

    conn.execute("UPDATE devices SET key_hash=? WHERE id=1", (hash_device_key("k" * 32),))
    manifest = "\n".join(
        "%s\t%d\t0" % (r["path"], r["size"]) for r in conn.execute("SELECT path, size FROM files WHERE present=1")
    )
    resp = api._sync(Req(), manifest)
    return [l.split("\t")[1] for l in resp.body.decode().splitlines() if l.startswith("UPLOAD")]


def test_quota_limits_offered_uploads(env):
    with db.get_conn() as conn:
        settings.set_many(conn, {"storage_quota_gb": str(2500 / 1024**3)})  # 2500 B
        add_file(conn, "Event/a.MP4", 1000, recorded=3)
        add_file(conn, "Event/b.MP4", 1000, recorded=2)
        add_file(conn, "Event/c.MP4", 1000, recorded=1)
        assert offered(conn) == ["Event/a.MP4", "Event/b.MP4"]
        # One file waits for Drive: only one more fits.
        conn.execute("UPDATE files SET state='received', local=1 WHERE path='Event/a.MP4'")
        assert offered(conn) == ["Event/b.MP4"]
        # Copies already on Drive do not block new uploads (they get evicted).
        conn.execute("UPDATE files SET state='on_drive' WHERE path='Event/a.MP4'")
        assert offered(conn) == ["Event/b.MP4", "Event/c.MP4"]
        # Unlimited.
        settings.set_many(conn, {"storage_quota_gb": "0"})
        assert len(offered(conn)) == 2


def test_file_bigger_than_quota_still_goes_when_idle(env):
    with db.get_conn() as conn:
        settings.set_many(conn, {"storage_quota_gb": str(500 / 1024**3)})
        add_file(conn, "Event/big.MP4", 5000)
        add_file(conn, "Event/small.MP4", 100, recorded=0)
        assert offered(conn) == ["Event/big.MP4"]


def test_eviction_of_cached_copies(env):
    with db.get_conn() as conn:
        settings.set_many(conn, {"storage_quota_gb": str(2500 / 1024**3), "keep_local_copy": "1"})
        add_file(conn, "Event/old.MP4", 1000, state="on_drive", local=1, updated=100)
        add_file(conn, "Event/new.MP4", 1000, state="on_drive", local=1, updated=200)
        add_file(conn, "Event/wait.MP4", 1000, state="received", local=1)
        worker.enforce_quota(conn)
        rows = {r["path"]: r["local"] for r in conn.execute("SELECT path, local FROM files")}
        assert rows == {"Event/old.MP4": 0, "Event/new.MP4": 1, "Event/wait.MP4": 1}
        assert not files.final_path(UID, "Event/old.MP4").exists()
        # Copies disabled: all cached copies go, pending ones stay.
        settings.set_many(conn, {"keep_local_copy": "0"})
        worker.enforce_quota(conn)
        rows = {r["path"]: r["local"] for r in conn.execute("SELECT path, local FROM files")}
        assert rows == {"Event/old.MP4": 0, "Event/new.MP4": 0, "Event/wait.MP4": 1}


def test_pasted_token_connects_drive(env):
    assert not gdrive.connected()
    with db.get_conn() as conn:
        assert settings.drive_remote(conn) == ""
    tok = gdrive.parse_pasted_token(
        'Paste the following into your remote machine --->\n{"access_token":"a","token_type":"Bearer",'
        '"refresh_token":"r","expiry":"2026-01-01T00:00:00Z"}\n<---End paste'
    )
    gdrive.save(tok, own_client=False, scope="drive")
    assert gdrive.connected()
    with db.get_conn() as conn:
        settings.set_many(conn, {"drive_folder": "Auto/Dashcam"})
        assert settings.drive_remote(conn) == "gdrive:Auto/Dashcam"
    conf = Path(config.RCLONE_CONFIG).read_text()
    assert "[gdrive]" in conf and "type = drive" in conf
    assert oct(os.stat(config.RCLONE_CONFIG).st_mode & 0o777) == "0o600"
    gdrive.disconnect()
    assert not gdrive.connected()
    with pytest.raises(ValueError):
        gdrive.parse_pasted_token('{"access_token":"a"}')


@pytest.mark.skipif(not HAVE_RCLONE, reason="rclone not installed")
def test_upload_to_drive_and_download_from_drive(env, monkeypatch):
    from fastapi.testclient import TestClient

    from app import main

    drive = env / "drive"
    monkeypatch.setattr(config, "DRIVE_REMOTE", ":local:" + str(drive))
    data = bytes(range(256)) * 40  # 10240 B
    with db.get_conn() as conn:
        add_file(conn, "Event/Front/EV20261004-101500-0001F.MP4", len(data), state="received", local=1)
        files.final_path(UID, "Event/Front/EV20261004-101500-0001F.MP4").write_bytes(data)
        row = conn.execute(
            "SELECT f.*, d.uid, d.name AS device_name, d.config FROM files f JOIN devices d ON d.id=f.device_id"
        ).fetchone()
        worker._upload_one(conn, row, settings.drive_remote(conn))
        r = conn.execute("SELECT state, local, drive_path FROM files").fetchone()
        assert r["state"] == "on_drive" and r["local"] == 0
        assert (drive / "Octavia/Event/2025-10-04/EV20261004-101500-0001F.MP4").read_bytes() == data
        fid = conn.execute("SELECT id FROM files").fetchone()[0]
        conn.execute(
            "INSERT INTO users(username, password_hash, role, created_at) VALUES ('u', ?, 'viewer', 0)",
            (main.hash_password("password1"),),
        )

    monkeypatch.setattr(config, "COOKIE_SECURE", False)
    with TestClient(main.app) as c:
        import re

        page = c.get("/login")
        csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        c.post("/login", data={"csrf": csrf, "username": "u", "password": "password1"})
        full = c.get("/files/%d/download" % fid)
        assert full.status_code == 200 and full.content == data
        assert "attachment" in full.headers["content-disposition"]
        part = c.get("/files/%d/download?inline=1" % fid, headers={"Range": "bytes=100-199"})
        assert part.status_code == 206 and part.content == data[100:200]
        assert part.headers["content-range"] == "bytes 100-199/%d" % len(data)
        assert c.get("/files/%d/download" % fid, headers={"Range": "bytes=999999-"}).status_code == 416
        page = c.get("/devices/1?where=all").text
        assert "na Google Drive" in page and "z Google Drive" in page


def test_picked_folder_destination(env):
    tok = {"access_token": "a", "refresh_token": "r", "token_type": "Bearer", "expiry": "2099-01-01T00:00:00+00:00"}
    gdrive.save(tok, own_client=False, scope="drive")
    with db.get_conn() as conn:
        settings.set_many(conn, {"drive_folder_id": "1AbCdEfGhIjKlMnOp", "drive_folder_name": "Sdílená", "drive_folder": ""})
        remote = settings.drive_remote(conn)
        assert remote == "gdrive,root_folder_id=1AbCdEfGhIjKlMnOp:"
        dest = files.drive_destination(remote, "Octavia", "Event/Front/EV.MP4", 1759565700)
        assert dest == "gdrive,root_folder_id=1AbCdEfGhIjKlMnOp:Octavia/Event/2025-10-04/EV.MP4"
        settings.set_many(conn, {"drive_folder": "Auto"})
        dest = files.drive_destination(settings.drive_remote(conn), "Octavia", "Event/Front/EV.MP4", 1759565700)
        assert dest == "gdrive,root_folder_id=1AbCdEfGhIjKlMnOp:Auto/Octavia/Event/2025-10-04/EV.MP4"
    # Pasted rclone token = other OAuth client: no Picker.
    assert not gdrive.picker_available()
    assert gdrive.fresh_access_token() == "a"  # not expired, no refresh call
