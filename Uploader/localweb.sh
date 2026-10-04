#!/bin/sh
#
# Local web page with the recordings: http://<camera IP>:8080/
# Works on the home Wi-Fi (STA) and in AP mode. Only the video folders are
# published (never config.txt or the uploader identity).
#
# config.txt:
#   local_web=1                 0 disables
#   local_web_port=8080
#   local_web_user=admin
#   local_web_pass=secret       optional; empty = no password (anyone on the network)

MMC="${MMC:-/mnt/mmc}"
BASE="${BASE:-$MMC/uploader}"
LOG="$BASE/uploader.log"
ROOT="${ROOT:-/tmp/lweb}"
CONF=/tmp/lweb.conf
PIDFILE=/tmp/lweb.pid

export PATH=/sbin:/bin:/usr/bin:/usr/sbin:$PATH

log() {
	echo "$(date '+%Y-%m-%d %H:%M:%S') localweb: $*" >>"$LOG"
}

cfg_get() {
	_v=""
	[ -f "$MMC/config.txt" ] && _v="$(sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//p" "$MMC/config.txt" \
		| head -1 | tr -d '\r' | sed 's/[[:space:]]*#.*$//; s/[[:space:]]*$//')"
	[ -n "$_v" ] && echo "$_v" || echo "$2"
}

case "$(cfg_get local_web 1)" in 0|off|OFF|no) exit 0 ;; esac
PORT="$(cfg_get local_web_port 8080)"
case "$PORT" in ''|*[!0-9]*) PORT=8080 ;; esac
USER="$(cfg_get local_web_user admin)"
PASS="$(cfg_get local_web_pass '')"

if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
	exit 0
fi

# Document root in RAM: the CGI must be executable (the SD card is FAT/noexec)
# and only symlinks to the video folders are exposed.
rm -rf "$ROOT"
mkdir -p "$ROOT/cgi-bin"
cp "$BASE/localweb.cgi" "$ROOT/cgi-bin/index.cgi" && chmod 755 "$ROOT/cgi-bin/index.cgi" || {
	log "missing $BASE/localweb.cgi, exit"
	exit 1
}
for _d in Event Normal Parking Lapse Photo; do
	[ -d "$MMC/$_d" ] && ln -s "$MMC/$_d" "$ROOT/$_d"
done

echo "A:*" >"$CONF"
if [ -n "$PASS" ]; then
	echo "/:$USER:$PASS" >>"$CONF"
fi
chmod 600 "$CONF"

httpd -f -p "$PORT" -h "$ROOT" -c "$CONF" </dev/null >/dev/null 2>&1 &
echo $! >"$PIDFILE"
sleep 1
if kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
	log "serving recordings on port $PORT$([ -n "$PASS" ] && echo ' (password protected)')"
else
	log "httpd failed to start on port $PORT"
	rm -f "$PIDFILE"
	exit 1
fi
