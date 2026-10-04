"""Google Drive connection managed from the web UI (writes rclone.conf)."""
import configparser
import json
import os
import subprocess
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
ABOUT_URL = "https://www.googleapis.com/drive/v3/about?fields=user(emailAddress)"
# drive.file: the app sees only files and folders it created itself.
SCOPE = "https://www.googleapis.com/auth/drive.file"


def _conf_path() -> Path:
    return Path(config.RCLONE_CONFIG)


def _read() -> configparser.ConfigParser:
    cp = configparser.ConfigParser(interpolation=None)
    p = _conf_path()
    if p.exists():
        cp.read(p)
    return cp


def _write(cp: configparser.ConfigParser) -> None:
    p = _conf_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w") as fh:
        cp.write(fh)
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)


def connected() -> bool:
    cp = _read()
    name = config.GDRIVE_REMOTE_NAME
    return cp.has_section(name) and bool(cp.get(name, "token", fallback=""))


def oauth_available() -> bool:
    return bool(config.GOOGLE_CLIENT_ID and config.GOOGLE_CLIENT_SECRET)


def redirect_uri(base_url: str) -> str:
    return base_url.rstrip("/") + "/settings/google/callback"


def auth_url(base_url: str, state: str) -> str:
    q = {
        "client_id": config.GOOGLE_CLIENT_ID,
        "redirect_uri": redirect_uri(base_url),
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    }
    return AUTH_URL + "?" + urllib.parse.urlencode(q)


def exchange_code(code: str, base_url: str) -> dict:
    data = urllib.parse.urlencode(
        {
            "code": code,
            "client_id": config.GOOGLE_CLIENT_ID,
            "client_secret": config.GOOGLE_CLIENT_SECRET,
            "redirect_uri": redirect_uri(base_url),
            "grant_type": "authorization_code",
        }
    ).encode()
    with urllib.request.urlopen(urllib.request.Request(TOKEN_URL, data=data), timeout=30) as resp:
        tok = json.load(resp)
    if "refresh_token" not in tok:
        raise ValueError("Google nevrátil refresh token, zkus to znovu.")
    expiry = datetime.now(timezone.utc) + timedelta(seconds=int(tok.get("expires_in", 3600)))
    return {
        "access_token": tok["access_token"],
        "token_type": tok.get("token_type", "Bearer"),
        "refresh_token": tok["refresh_token"],
        "expiry": expiry.isoformat(),
    }


def account_email(token: dict) -> str:
    try:
        req = urllib.request.Request(ABOUT_URL, headers={"Authorization": "Bearer " + token["access_token"]})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.load(resp).get("user", {}).get("emailAddress", "")
    except Exception:  # noqa: BLE001
        return ""


def save(token: dict, own_client: bool, scope: str = "drive.file") -> None:
    cp = _read()
    name = config.GDRIVE_REMOTE_NAME
    if cp.has_section(name):
        cp.remove_section(name)
    cp.add_section(name)
    cp.set(name, "type", "drive")
    cp.set(name, "scope", scope)
    if own_client:
        cp.set(name, "client_id", config.GOOGLE_CLIENT_ID)
        cp.set(name, "client_secret", config.GOOGLE_CLIENT_SECRET)
    cp.set(name, "token", json.dumps(token))
    _write(cp)


def parse_pasted_token(text: str) -> dict:
    """Token JSON printed by `rclone authorize "drive"` (the part between the arrows)."""
    text = text.strip()
    if "--->" in text:
        text = text.split("--->", 1)[1]
    if "<---" in text:
        text = text.split("<---", 1)[0]
    tok = json.loads(text.strip())
    if not isinstance(tok, dict) or "refresh_token" not in tok or "access_token" not in tok:
        raise ValueError("V tokenu chybí access_token nebo refresh_token.")
    return tok


def picker_available() -> bool:
    """Picker needs our own OAuth client (the token must belong to the same Cloud project) and an API key."""
    if not (oauth_available() and config.GOOGLE_API_KEY and connected()):
        return False
    return _read().get(config.GDRIVE_REMOTE_NAME, "client_id", fallback="") == config.GOOGLE_CLIENT_ID


def project_number() -> str:
    """Google Picker app id = Cloud project number = numeric prefix of the OAuth client id."""
    return config.GOOGLE_CLIENT_ID.split("-", 1)[0]


def fresh_access_token() -> str:
    """Access token for the browser Picker, refreshed (and saved for rclone) when about to expire."""
    cp = _read()
    name = config.GDRIVE_REMOTE_NAME
    token = json.loads(cp.get(name, "token"))
    try:
        expiry = datetime.fromisoformat(token.get("expiry", "").replace("Z", "+00:00"))
    except ValueError:
        expiry = datetime.min.replace(tzinfo=timezone.utc)
    if expiry - datetime.now(timezone.utc) > timedelta(minutes=5):
        return token["access_token"]
    data = urllib.parse.urlencode(
        {
            "client_id": cp.get(name, "client_id", fallback=config.GOOGLE_CLIENT_ID),
            "client_secret": cp.get(name, "client_secret", fallback=config.GOOGLE_CLIENT_SECRET),
            "refresh_token": token["refresh_token"],
            "grant_type": "refresh_token",
        }
    ).encode()
    with urllib.request.urlopen(urllib.request.Request(TOKEN_URL, data=data), timeout=30) as resp:
        new = json.load(resp)
    token["access_token"] = new["access_token"]
    token["expiry"] = (datetime.now(timezone.utc) + timedelta(seconds=int(new.get("expires_in", 3600)))).isoformat()
    cp.set(name, "token", json.dumps(token))
    _write(cp)
    return token["access_token"]


def disconnect() -> None:
    cp = _read()
    if cp.has_section(config.GDRIVE_REMOTE_NAME):
        cp.remove_section(config.GDRIVE_REMOTE_NAME)
        _write(cp)


def rclone(*args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run([config.RCLONE_BIN, *args], capture_output=True, text=True, timeout=timeout)


def check_folder(remote: str) -> tuple[bool, str]:
    """Create the target folder if needed and report how many items it contains."""
    try:
        mk = rclone("mkdir", remote)
        if mk.returncode != 0:
            return False, (mk.stderr or mk.stdout)[-400:]
        ls = rclone("lsjson", remote, "--max-depth", "1")
        if ls.returncode != 0:
            return False, (ls.stderr or ls.stdout)[-400:]
        return True, "Složka %s je dostupná (%d položek)." % (remote, len(json.loads(ls.stdout or "[]")))
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        return False, str(exc)
