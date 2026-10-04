# Dashcam upload server

Receives recordings from 70mai A800S cameras running `Uploader/uploader.sh`
and moves them to Google Drive with rclone.

- Pairing: a new camera appears as *waiting for approval*; approve it once.
- Per-camera settings, changed remotely: auto-uploaded folders (default `Event`),
  rate limit, AP fallback / STA retry timing, delete-after-upload.
- List of recordings on the camera with recording date, filters by folder,
  state and date range.
- Remote actions: request upload of any recording, delete recordings from the
  camera SD card.
- Users with roles `admin` (everything) and `viewer` (read + download), registration by e-mail invitation.
- Audit log, camera log viewer, optional ntfy push notifications.
- Per-user e-mail notifications: upload summary after the queue is done, upload errors, new cameras.

HTTPS and DNS are expected to be handled by Cloudflare in front of the server.

## Setup

```
cd server
cp .env.example .env
# edit .env: SECRET_KEY (openssl rand -hex 32), ADMIN_PASSWORD, DRIVE_REMOTE
docker compose build
```

### Google Drive (personal account)

Connected in the web UI: *Nastavení* -> Google Drive. Two ways:

**A. One click (recommended).** Create your own OAuth client once:

1. [Google Cloud Console](https://console.cloud.google.com/) -> new project -> *APIs & Services* ->
   enable **Google Drive API**.
2. *OAuth consent screen*: External, add your Gmail as test user, then **Publish app**
   (otherwise Google expires the refresh token after 7 days). Scope `drive.file` needs no verification.
3. *Credentials* -> *Create credentials* -> *OAuth client ID* -> **Web application**,
   authorized redirect URI `https://dash.firelahva.cz/settings/google/callback`.
4. Put the ID and secret into `.env` (`GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`),
   `docker compose up -d`, then *Nastavení* -> *Připojit Google účet*.

With scope `drive.file` the app only sees files and folders it created itself.

**B. Paste a token.** On a computer with a browser run `rclone authorize "drive"`
and paste the printed token in *Nastavení* -> *Připojit vložením tokenu z rclone*.

In *Nastavení* you also choose the Drive folder (e.g. `70mai` or `Auto/Dashcam`);
files go to `<folder>/<camera>/<Event|Normal|...>/<YYYY-MM-DD>/<file>`.
The *Ověřit / vytvořit složku* button tests the connection.

### Server storage limit

*Nastavení* -> *Limit místa na serveru*. Files waiting for Drive (including partial
uploads) may use at most this much space; when it is full the server stops giving
cameras new uploads and the rest waits on the SD card. Optionally keep a copy of
uploaded files on the server for faster downloads; the oldest copies are removed
when the limit is reached (they stay on Drive).

### Recording states and downloads

The recording list shows: on the camera, requested for upload, uploading from the
camera (%), on the server, waiting for Google Drive, on Google Drive. Every file that
is on the server or on Google Drive can be played or downloaded from the web; Drive
files are streamed through the server (with seeking support).

### Start

```
docker compose up -d
docker compose logs -f
```

The app listens on port `8000` (`HTTP_PORT` in `.env`).

## Cloudflare

Option A: proxied DNS record `dash.firelahva.cz` pointing to the server, SSL
mode *Full*. Forward port 8000 or put any reverse proxy in front of it.

Option B: Cloudflare Tunnel, no open port. Create a tunnel in Zero Trust with a
public hostname `dash.firelahva.cz` -> `http://app:8000`, put the token into
`CLOUDFLARE_TUNNEL_TOKEN` and run `docker compose --profile tunnel up -d`.

Either way:

- Camera chunks are 4 MB, well below Cloudflare's 100 MB request limit.
- If *Bot Fight Mode* or a WAF challenge is enabled, add a rule that skips them
  for `URI Path starts with /api/v1/`. The camera cannot solve challenges.

## Notifications

Set `NOTIFY_URL` to an ntfy topic (e.g. `https://ntfy.sh/your-secret-topic`).
You get a push when a new camera asks for approval and when a new `Event` video
reaches Google Drive.

## Users, invitations and e-mail

Users are invited by an admin (*Uživatelé* -> *Pozvat uživatele*: e-mail + role).
The invitee gets an e-mail with a one-time link (valid `INVITE_TTL_HOURS`, default
7 days) where they pick a username, password and notifications. Pending
invitations can be resent (new link, old one stops working) or cancelled. Without
SMTP the link is shown to the admin to send manually. Login works with the
username or the e-mail.

Each user chooses notifications on their profile (`/account`):

| Notification | Who | When |
|---|---|---|
| Upload summary | everyone | one e-mail after the Drive upload queue is done |
| Upload errors | everyone | in the same summary; a failing file is reported on attempts 1, 5, 10, 20 only |
| New camera waiting for approval | admins | immediately |

"Queue is done" means no camera has sent a chunk and nothing has waited for Drive
for `MAIL_QUIET_SEC` (default 5 min), so 10 Event videos uploaded in a row give one
e-mail. All e-mails go through an outbox in the database and are retried
(up to 10 times) when the SMTP server is unavailable.

SMTP is configured in `.env` (`SMTP_*`). Gmail: `smtp.gmail.com`, port 587,
`starttls`, an [app password](https://myaccount.google.com/apppasswords).
Users can send themselves a test e-mail from the profile page.

## Storage

Two bind mounts, host paths set in `.env`:

| `.env` | Default | Container | Content |
|---|---|---|---|
| `DATA_PATH` | `./data` | `/data` | `dashcam.sqlite3` (cameras, users, file list, log) and `incoming/<camera>/...` = videos received but not yet on Google Drive |
| `RCLONE_CONFIG_PATH` | `./rclone` | `/config/rclone` | `rclone.conf` with the Drive token |

`incoming/` is normally almost empty: files are deleted after a successful Drive
upload (unless `KEEP_LOCAL_COPY=true`). It grows only when Drive is down or not
configured, so give `DATA_PATH` room for that backlog (an Event clip is ~400 MB
per camera). Back up both paths; the token is a secret.

## Camera API

Plain text, tab separated (the client is BusyBox `sh`). Auth headers
`X-Device-Id` and `X-Device-Key` on every request.

| Endpoint | Body | Response |
|---|---|---|
| `POST /api/v1/register` | camera name | `PENDING` / `APPROVED` / `BLOCKED` |
| `POST /api/v1/sync` | manifest lines `path\tsize\tmtime` | `STATUS`, `CFG k v`, `DELETE id path`, `UPLOAD path offset size` lines |
| `PUT /api/v1/upload?path=` | chunk, `Content-Range`, `X-Chunk-Sha256` | `OK\t<bytes>`, `DONE`, or 409 `OFFSET\t<bytes>` |
| `POST /api/v1/ack` | `id\tok\|err\tmessage` lines | `OK` |
| `POST /api/v1/log` | log tail | `OK` |

## Tests

```
docker build -t dashcam-upload:test .
docker run --rm --entrypoint sh -v "$PWD/tests:/srv/tests:ro" dashcam-upload:test \
  -c "pip install -q pytest httpx && python -m pytest -q tests"
```
