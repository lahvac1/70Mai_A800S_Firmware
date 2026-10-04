#!/bin/sh
# Bind-mounted volumes are usually created by root on the host; hand them to the app user.
set -e
chown app:app /data /config/rclone 2>/dev/null || true
[ -d /data/incoming ] && chown -R app:app /data/incoming 2>/dev/null || true
exec setpriv --reuid=app --regid=app --init-groups "$@"
