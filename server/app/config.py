import os
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "dashcam.sqlite3"
INCOMING_DIR = DATA_DIR / "incoming"

SECRET_KEY = os.environ.get("SECRET_KEY", "")
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "")
INVITE_TTL_HOURS = int(os.environ.get("INVITE_TTL_HOURS", "168"))
COOKIE_SECURE = _bool("COOKIE_SECURE", True)
TIMEZONE = os.environ.get("TZ", "Europe/Prague")

# rclone destination. "gdrive:<folder>" = the Google account connected in the web UI
# (folder is only the initial default, it is changed in Nastavení). Any other
# rclone remote (e.g. ":local:/path") is used as is. Empty = Google via web UI.
DRIVE_REMOTE = os.environ.get("DRIVE_REMOTE", "")
RCLONE_CONFIG = os.environ.get("RCLONE_CONFIG", str(DATA_DIR / "rclone.conf"))
# Remote name the web UI writes into rclone.conf.
GDRIVE_REMOTE_NAME = "gdrive"
# Own Google OAuth client (type "Web application") for the one-click connection
# in the web UI. Redirect URI: <PUBLIC_URL>/settings/google/callback
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
# Browser API key for the Google Picker folder chooser (enable "Google Picker API").
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")
# Default for the server storage limit (GB, 0 = unlimited); changed in the web UI.
STORAGE_QUOTA_GB = float(os.environ.get("STORAGE_QUOTA_GB", "20"))
DRIVE_PATH_TEMPLATE = os.environ.get("DRIVE_PATH_TEMPLATE", "{device}/{folder}/{date}/{name}")
KEEP_LOCAL_COPY = _bool("KEEP_LOCAL_COPY", False)
RCLONE_BIN = os.environ.get("RCLONE_BIN", "rclone")
WORKER_ENABLED = _bool("WORKER_ENABLED", True)

# ntfy.sh-compatible URL (plain-text POST). Empty = no notifications.
NOTIFY_URL = os.environ.get("NOTIFY_URL", "")

# SMTP for invitations and notifications. Empty SMTP_HOST = no e-mails.
# Recipients and notification types are chosen by each user on their profile.
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_SECURITY = os.environ.get("SMTP_SECURITY", "starttls").strip().lower()  # starttls | ssl | none
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM = os.environ.get("SMTP_FROM", "")
# Send the summary once the upload queue has been idle this long.
MAIL_QUIET_SEC = int(os.environ.get("MAIL_QUIET_SEC", "300"))
# Public address of the web UI, used for links in e-mails (invitations).
PUBLIC_URL = os.environ.get("PUBLIC_URL", "")

# Max accepted chunk size. Cloudflare free plan limits request bodies to 100 MB.
MAX_CHUNK_BYTES = int(os.environ.get("MAX_CHUNK_BYTES", str(32 * 1024 * 1024)))

# Folders the camera reports in its manifest.
CAMERA_FOLDERS = ["Event", "Normal", "Parking", "Lapse", "Photo"]

DEFAULT_DEVICE_CONFIG = {
    "upload_enabled": 1,
    "folders": "Event",
    "rate_kbps": 0,
    "ap_fallback_sec": 120,
    "sta_retry_sec": 600,
    "poll_sec": 60,
    "min_age_sec": 120,
    "delete_after_upload": 0,
}
