import hashlib
import os
import re
import sys
import tempfile
from pathlib import Path

import pytest

_tmp = tempfile.mkdtemp()
os.environ.update(
    DATA_DIR=_tmp,
    SECRET_KEY="x" * 40,
    ADMIN_USERNAME="admin",
    ADMIN_PASSWORD="adminpass1",
    COOKIE_SECURE="false",
    WORKER_ENABLED="false",
    DRIVE_REMOTE="",
    RCLONE_CONFIG=os.path.join(_tmp, "rclone.conf"),
)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from app import db  # noqa: E402
from app.main import app  # noqa: E402

UID = "a1b2c3d4e5f60718"
KEY = "k" * 32
HDR = {"X-Device-Id": UID, "X-Device-Key": KEY}


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def login(client, username="admin", password="adminpass1"):
    page = client.get("/login")
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    r = client.post("/login", data={"username": username, "password": password, "csrf": csrf}, follow_redirects=False)
    assert r.status_code == 303
    return csrf


def csrf_of(client, url="/"):
    return re.search(r'name="csrf" value="([^"]+)"', client.get(url).text).group(1)


def device_id():
    with db.get_conn() as conn:
        return conn.execute("SELECT id FROM devices WHERE uid=?", (UID,)).fetchone()["id"]


def put_chunk(client, path, data, start, total):
    return client.put(
        "/api/v1/upload",
        params={"path": path},
        content=data,
        headers={
            **HDR,
            "Content-Range": "bytes %d-%d/%d" % (start, start + len(data) - 1, total),
            "X-Chunk-Sha256": hashlib.sha256(data).hexdigest(),
        },
    )


def test_unknown_device_is_rejected(client):
    r = client.post("/api/v1/sync", content="", headers=HDR)
    assert r.status_code == 401 and r.text == "UNKNOWN"


def test_register_requires_approval(client):
    r = client.post("/api/v1/register", content="Moje auto\n", headers=HDR)
    assert r.text == "PENDING"
    r = client.post("/api/v1/sync", content="", headers=HDR)
    assert r.status_code == 403 and r.text == "PENDING"
    # Same id with a different key must not take over the device.
    r = client.post("/api/v1/register", content="x", headers={**HDR, "X-Device-Key": "z" * 32})
    assert r.status_code == 403


def test_admin_approves(client):
    login(client)
    assert "Moje auto" in client.get("/").text
    csrf = csrf_of(client)
    r = client.post(
        "/devices/%d/status" % device_id(), data={"csrf": csrf, "status": "approved"}, follow_redirects=False
    )
    assert r.status_code == 303
    assert client.post("/api/v1/register", content="", headers=HDR).text == "APPROVED"


def test_csrf_is_enforced(client):
    r = client.post("/devices/%d/status" % device_id(), data={"csrf": "bad", "status": "blocked"})
    assert r.status_code == 403


MANIFEST = (
    "Event/Front/EV20261004-101500-000001F.MP4\t10\t1759565700\n"
    "Normal/Front/NO20261004-100000-000002F.MP4\t20\t1759564800\n"
    "../etc/passwd\t1\t1\n"
)


def test_sync_lists_event_uploads_only(client):
    r = client.post("/api/v1/sync", content=MANIFEST, headers={**HDR, "X-Sd-Free": "1000", "X-Sd-Total": "2000"})
    assert r.status_code == 200
    lines = r.text.splitlines()
    assert lines[0] == "STATUS\tAPPROVED"
    assert "CFG\tpoll_sec\t60" in lines
    uploads = [l for l in lines if l.startswith("UPLOAD")]
    assert uploads == ["UPLOAD\tEvent/Front/EV20261004-101500-000001F.MP4\t0\t10"]
    with db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 2


def test_chunked_upload_with_resume(client):
    path = "Event/Front/EV20261004-101500-000001F.MP4"
    data = b"0123456789"
    assert put_chunk(client, path, data[:4], 0, 10).text == "OK\t4"
    # Wrong offset -> server tells where to continue.
    r = put_chunk(client, path, data[6:], 6, 10)
    assert r.status_code == 409 and r.text == "OFFSET\t4"
    # Sync reports the resume offset.
    r = client.post("/api/v1/sync", content=MANIFEST, headers=HDR)
    assert "UPLOAD\t%s\t4\t10" % path in r.text
    # Corrupted chunk is rejected.
    bad = client.put(
        "/api/v1/upload",
        params={"path": path},
        content=b"xxxxxx",
        headers={**HDR, "Content-Range": "bytes 4-9/10", "X-Chunk-Sha256": hashlib.sha256(b"456789").hexdigest()},
    )
    assert bad.status_code == 400
    assert put_chunk(client, path, data[4:], 4, 10).text == "DONE"
    stored = Path(_tmp) / "incoming" / UID / path
    assert stored.read_bytes() == data
    r = client.post("/api/v1/sync", content=MANIFEST, headers=HDR)
    assert "UPLOAD" not in r.text


def test_request_upload_and_remote_delete(client):
    csrf = csrf_of(client)
    with db.get_conn() as conn:
        normal = conn.execute("SELECT id, path FROM files WHERE folder='Normal'").fetchone()
    r = client.post(
        "/devices/%d/files" % device_id(),
        data={"csrf": csrf, "action": "upload", "ids": [normal["id"]]},
        follow_redirects=False,
    )
    assert r.status_code == 303
    r = client.post("/api/v1/sync", content=MANIFEST, headers=HDR)
    assert "UPLOAD\t%s\t0\t20" % normal["path"] in r.text

    client.post(
        "/devices/%d/files" % device_id(),
        data={"csrf": csrf, "action": "delete", "ids": [normal["id"]]},
        follow_redirects=False,
    )
    r = client.post("/api/v1/sync", content=MANIFEST, headers=HDR)
    deletes = [l.split("\t") for l in r.text.splitlines() if l.startswith("DELETE")]
    assert len(deletes) == 1 and deletes[0][2] == normal["path"]
    # A file scheduled for deletion is not offered for upload.
    assert "UPLOAD\t%s" % normal["path"] not in r.text
    assert client.post("/api/v1/ack", content="%s\tok\t\n" % deletes[0][1], headers=HDR).text == "OK"
    with db.get_conn() as conn:
        assert conn.execute("SELECT present FROM files WHERE id=?", (normal["id"],)).fetchone()[0] == 0
        assert conn.execute("SELECT status FROM commands").fetchone()[0] == "done"
    page = client.get("/devices/%d?where=all" % device_id()).text
    assert "smazáno v kameře" in page


def test_config_change_reaches_camera(client):
    csrf = csrf_of(client)
    client.post(
        "/devices/%d/config" % device_id(),
        data={"csrf": csrf, "name": "Octavia", "upload_enabled": "on", "folders": ["Event", "Parking"],
              "rate_kbps": "500", "ap_fallback_sec": "90", "sta_retry_sec": "0", "poll_sec": "30", "min_age_sec": "60"},
        follow_redirects=False,
    )
    r = client.post("/api/v1/sync", content=MANIFEST, headers=HDR)
    assert "CFG\trate_kbps\t500" in r.text and "CFG\tap_fallback_sec\t90" in r.text


def accept_invite(email, role="viewer", **fields):
    csrf = csrf_of(client_ref[0], "/users")
    r = client_ref[0].post("/invitations", data={"csrf": csrf, "email": email, "role": role})
    link = re.search(r"(/invite/[A-Za-z0-9_-]+)", r.text).group(1)
    guest = TestClient(app)
    guest.__enter__()
    page = guest.get(link)
    assert email in page.text
    c = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    data = {"csrf": c, "username": "petr", "email": email, "password": "petrpass1", "password2": "petrpass1",
            "notify_errors": "on"}
    data.update(fields)
    r = guest.post(link, data=data, follow_redirects=False)
    return guest, link, r


client_ref = []


def test_invitation_flow(client):
    client_ref.append(client)
    guest, link, r = accept_invite("petr@example.com")
    assert r.status_code == 303 and r.headers["location"] == "/"
    with db.get_conn() as conn:
        u = conn.execute("SELECT * FROM users WHERE username='petr'").fetchone()
    assert u["role"] == "viewer" and u["email"] == "petr@example.com"
    assert (u["notify_uploads"], u["notify_errors"], u["notify_devices"]) == (0, 1, 0)
    # Logged in right after registration, viewer permissions only.
    assert guest.get("/users").status_code == 403
    assert guest.get("/devices/%d" % device_id()).status_code == 200
    c = csrf_of(guest)
    assert guest.post("/devices/%d/files" % device_id(), data={"csrf": c, "action": "delete", "ids": [1]}).status_code == 403
    # The link works only once.
    assert "Pozvánka neplatí" in guest.get(link).text
    guest.__exit__(None, None, None)
    with TestClient(app) as other:
        login(other, "petr@example.com", "petrpass1")  # login by e-mail works too


def test_profile_notifications(client):
    with TestClient(app) as petr:
        login(petr, "petr", "petrpass1")
        c = csrf_of(petr, "/account")
        # viewer cannot subscribe to new-camera alerts
        petr.post("/account/profile", data={"csrf": c, "email": "petr@example.com",
                                            "notify_uploads": "on", "notify_devices": "on"})
        with db.get_conn() as conn:
            u = conn.execute("SELECT * FROM users WHERE username='petr'").fetchone()
        assert (u["notify_uploads"], u["notify_errors"], u["notify_devices"]) == (1, 0, 0)
        # notifications without e-mail are refused
        petr.post("/account/profile", data={"csrf": c, "email": "", "notify_uploads": "on"})
        with db.get_conn() as conn:
            assert conn.execute("SELECT email FROM users WHERE username='petr'").fetchone()[0] == "petr@example.com"


def test_invalid_and_expired_invitations(client):
    assert "Pozvánka neplatí" in client.get("/invite/nonsense").text
    csrf = csrf_of(client, "/users")
    r = client.post("/invitations", data={"csrf": csrf, "email": "petr@example.com"})
    assert "už existuje" in r.text
    r = client.post("/invitations", data={"csrf": csrf, "email": "eva@example.com", "role": "admin"})
    link = re.search(r"(/invite/[A-Za-z0-9_-]+)", r.text).group(1)
    with db.get_conn() as conn:
        conn.execute("UPDATE invitations SET expires_at=1 WHERE email='eva@example.com'")
    assert "Pozvánka neplatí" in client.get(link).text
    # Resend issues a new valid link and invalidates the old one.
    with db.get_conn() as conn:
        inv_id = conn.execute("SELECT id FROM invitations WHERE email='eva@example.com'").fetchone()[0]
    r = client.post("/invitations/%d/resend" % inv_id, data={"csrf": csrf})
    new_link = re.search(r"(/invite/[A-Za-z0-9_-]+)", r.text).group(1)
    assert new_link != link and "Založení účtu" in client.get(new_link).text
    assert "Nová kamera čeká na schválení" in client.get(new_link).text  # admin invite offers device alerts


def test_last_admin_protected(client):
    csrf = csrf_of(client, "/users")
    with db.get_conn() as conn:
        admin_id = conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()[0]
    client.post("/users/%d/update" % admin_id, data={"csrf": csrf, "role": "viewer"})
    with db.get_conn() as conn:
        assert conn.execute("SELECT role FROM users WHERE id=?", (admin_id,)).fetchone()[0] == "admin"


def test_blocked_device(client):
    csrf = csrf_of(client)
    client.post("/devices/%d/status" % device_id(), data={"csrf": csrf, "status": "blocked"})
    r = client.post("/api/v1/sync", content=MANIFEST, headers=HDR)
    assert r.status_code == 403 and r.text == "BLOCKED"


def test_remote_ap_switch(client):
    csrf = csrf_of(client)
    client.post("/devices/%d/status" % device_id(), data={"csrf": csrf, "status": "approved"})
    client.post("/devices/%d/ap" % device_id(), data={"csrf": csrf, "minutes": "1"})  # clamped to 5
    client.post("/devices/%d/ap" % device_id(), data={"csrf": csrf, "minutes": "20"})
    r = client.post("/api/v1/sync", content=MANIFEST, headers=HDR)
    ap = [l.split("\t") for l in r.text.splitlines() if l.startswith("AP")]
    assert len(ap) == 1 and ap[0][2] == "20"
    assert "UPLOAD" not in r.text  # no uploads while switching to AP
    client.post("/api/v1/ack", content="%s\tok\tswitching\n" % ap[0][1], headers=HDR)
    with db.get_conn() as conn:
        rows = conn.execute("SELECT path, status FROM commands WHERE type='ap' ORDER BY id").fetchall()
    assert [(r["path"], r["status"]) for r in rows] == [("5", "failed"), ("20", "done")]
    assert "AP" not in [l[:2] for l in client.post("/api/v1/sync", content=MANIFEST, headers=HDR).text.splitlines()]


def test_ap_credentials_shown(client):
    client.post("/api/v1/sync", content=MANIFEST, headers={**HDR, "X-Ap-Ssid": "70mai_A800S", "X-Ap-Pass": "tajne123"})
    # Empty headers (older uploader) keep the last known values.
    client.post("/api/v1/sync", content=MANIFEST, headers={**HDR, "X-Ap-Ssid": "", "X-Ap-Pass": ""})
    page = client.get("/devices/%d" % device_id()).text
    assert "70mai_A800S" in page and 'data-secret="tajne123"' in page


def test_local_web_link(client):
    client.post("/api/v1/sync", content=MANIFEST, headers={**HDR, "X-Lan-Ip": "192.168.1.101", "X-Local-Web": "1:8080"})
    assert "http://192.168.1.101:8080/" in client.get("/devices/%d" % device_id()).text
    client.post("/api/v1/sync", content=MANIFEST, headers={**HDR, "X-Lan-Ip": "<script>", "X-Local-Web": "0:8080"})
    page = client.get("/devices/%d" % device_id()).text
    assert "Videa v síti" not in page and "<script>" not in page


def test_merge_new_identity_into_existing_camera(client):
    csrf = csrf_of(client)
    old_id = device_id()
    client.post("/devices/%d/status" % old_id, data={"csrf": csrf, "status": "approved"})
    with db.get_conn() as conn:
        n_files = conn.execute("SELECT COUNT(*) FROM files WHERE device_id=?", (old_id,)).fetchone()[0]
    assert n_files > 0
    new_hdr = {"X-Device-Id": "ffff0000ffff0000", "X-Device-Key": "n" * 32}
    assert client.post("/api/v1/register", content="Moje auto", headers=new_hdr).text == "PENDING"
    with db.get_conn() as conn:
        new_id = conn.execute("SELECT id FROM devices WHERE uid='ffff0000ffff0000'").fetchone()[0]
    assert "Je to stará kamera" in client.get("/").text
    r = client.post("/devices/%d/merge" % new_id, data={"csrf": csrf, "target_id": old_id}, follow_redirects=False)
    assert r.status_code == 303
    # New key works for the old camera with its history; old key is gone.
    r = client.post("/api/v1/sync", content=MANIFEST, headers=new_hdr)
    assert r.status_code == 200
    with db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM devices WHERE uid='ffff0000ffff0000'").fetchone()[0] == 1
        assert conn.execute("SELECT id FROM devices WHERE uid='ffff0000ffff0000'").fetchone()[0] == old_id
        assert conn.execute("SELECT COUNT(*) FROM files WHERE device_id=?", (old_id,)).fetchone()[0] == n_files
    assert client.post("/api/v1/sync", content=MANIFEST, headers=HDR).status_code == 401
    assert (Path(_tmp) / "incoming" / "ffff0000ffff0000").exists()
