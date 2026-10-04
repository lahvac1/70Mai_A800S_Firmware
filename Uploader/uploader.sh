#!/bin/sh
#
# 70mai A800S cloud uploader.
#
# Runs in the background (started by Scripts/autorun_uploader.sh), watches the
# Wi-Fi state and, while connected to the home network in STA mode, syncs the
# SD card with the upload server configured in config.txt:
#   - reports the list of recordings (manifest),
#   - executes remote deletes,
#   - uploads requested / policy-selected files in resumable chunks.
# When the home network is not reachable it switches the camera to AP mode so
# the phone app keeps working, and periodically retries STA while no phone is
# connected to the AP.
#
# Everything is plain BusyBox sh; HTTPS needs a static curl next to this script.

VERSION=8

MMC="${MMC:-/mnt/mmc}"
BASE="${BASE:-$MMC/uploader}"
CURL="${CURL:-}"
CACERT="${CACERT:-$BASE/cacert.pem}"
TMP="${TMP:-/tmp/uploader}"
LOG="$BASE/uploader.log"
APKAP=/customer/wifi/ApkAp.sh
# Testing hook: NO_WIFI=1 skips all Wi-Fi handling and assumes a connection.
NO_WIFI="${NO_WIFI:-0}"
ONESHOT="${ONESHOT:-0}"

FOLDERS="Event Normal Parking Lapse Photo"
BS=1048576
CHUNK_BLOCKS=4

TAB="$(printf '\t')"

export PATH=/sbin:/bin:/usr/bin:/usr/sbin:/customer/wifi:/config/bin:$PATH
export LD_LIBRARY_PATH=/lib:/customer/wifi/lib:$LD_LIBRARY_PATH

mkdir -p "$TMP" "$BASE"

# The SD card is FAT and may be mounted noexec: run curl from RAM instead.
if [ -z "$CURL" ]; then
	cp -f "$BASE/curl" "$TMP/curl" 2>/dev/null && chmod 755 "$TMP/curl"
	CURL="$TMP/curl"
fi

log() {
	echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >>"$LOG"
	if [ "$(stat -c %s "$LOG" 2>/dev/null || echo 0)" -gt 262144 ]; then
		mv -f "$LOG" "$LOG.1"
	fi
}

cfg_get() {
	# cfg_get KEY DEFAULT - read KEY=value from config.txt (same rules as firmware scripts).
	_v=""
	[ -f "$MMC/config.txt" ] && _v="$(sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//p" "$MMC/config.txt" \
		| head -1 | tr -d '\r' | sed 's/[[:space:]]*#.*$//; s/[[:space:]]*$//')"
	[ -n "$_v" ] && echo "$_v" || echo "$2"
}

num() {
	# num VALUE DEFAULT - VALUE if it is a non-negative integer, else DEFAULT.
	case "$1" in ''|*[!0-9]*) echo "$2" ;; *) echo "$1" ;; esac
}

# ------------------------------------------------------------------ settings

UPLOAD_ENABLE="$(cfg_get upload_enable 0)"
UPLOAD_URL="$(cfg_get upload_url '')"
UPLOAD_URL="${UPLOAD_URL%/}"

case "$UPLOAD_ENABLE" in 1|ON|on|true|yes) ;; *) exit 0 ;; esac
if [ -z "$UPLOAD_URL" ]; then
	log "upload_url missing in config.txt, exit"
	exit 0
fi

# Defaults; config.txt overrides; server CFG lines override both (persisted).
AP_FALLBACK_SEC="$(num "$(cfg_get upload_ap_fallback_sec 120)" 120)"
STA_RETRY_SEC="$(num "$(cfg_get upload_sta_retry_sec 600)" 600)"
POLL_SEC=60
RATE_KBPS=0
MIN_AGE_SEC=120

load_server_cfg() {
	[ -f "$BASE/server.cfg" ] || return 0
	while IFS="$TAB" read -r _k _v; do
		_v="$(num "$_v" "")"
		[ -n "$_v" ] || continue
		case "$_k" in
			ap_fallback_sec) AP_FALLBACK_SEC="$_v" ;;
			sta_retry_sec) STA_RETRY_SEC="$_v" ;;
			poll_sec) POLL_SEC="$_v" ;;
			rate_kbps) RATE_KBPS="$_v" ;;
			min_age_sec) MIN_AGE_SEC="$_v" ;;
		esac
	done <"$BASE/server.cfg"
	[ "$POLL_SEC" -lt 10 ] && POLL_SEC=10
	return 0
}

# ------------------------------------------------------------------ identity

if [ ! -s "$BASE/device.id" ] || [ ! -s "$BASE/device.key" ]; then
	head -c 8 /dev/urandom | od -An -tx1 | tr -d ' \n' >"$BASE/device.id"
	head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n' >"$BASE/device.key"
	sync
	log "generated new device identity $(cat "$BASE/device.id")"
fi
DEVICE_ID="$(cat "$BASE/device.id")"
DEVICE_KEY="$(cat "$BASE/device.key")"
DEVICE_NAME="$(cfg_get upload_device_name "$(nvconf get 1 wireless.ap.ssid 2>/dev/null)")"

# ------------------------------------------------------------------ single instance

PIDFILE=/tmp/uploader.pid
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
	exit 0
fi
echo $$ >"$PIDFILE"
trap 'rm -f "$PIDFILE" "$TMP/chunk"; exit 0' INT TERM

# ------------------------------------------------------------------ Wi-Fi

wifi_mode() {
	[ "$NO_WIFI" = "1" ] && { echo STA; return; }
	if pidof hostapd >/dev/null 2>&1; then
		echo AP
	elif pidof wpa_supplicant >/dev/null 2>&1; then
		echo STA
	else
		echo OFF
	fi
}

wlan_ip() {
	ifconfig wlan0 2>/dev/null | sed -n 's/.*inet addr:\([0-9.]*\).*/\1/p' | head -1
}

sta_connected() {
	# The firmware has no wpa_cli: check association with iw, then an IPv4 address.
	[ "$NO_WIFI" = "1" ] && return 0
	if [ -x /customer/wifi/iw ] || command -v iw >/dev/null 2>&1; then
		iw dev wlan0 link 2>/dev/null | grep -q '^Connected' || return 1
	else
		[ "$(cat /sys/class/net/wlan0/operstate 2>/dev/null)" = "up" ] || return 1
	fi
	[ -n "$(wlan_ip)" ]
}

ap_clients() {
	iw dev wlan0 station dump 2>/dev/null | grep -c '^Station'
}

in_parking() {
	[ -e /tmp/parking ]
}

switch_wifi() {
	log "switching Wi-Fi to $1"
	"$APKAP" start "$1" >/dev/null 2>&1
}

# ------------------------------------------------------------------ HTTP

ensure_dns() {
	# The firmware's DHCP script writes /appconfigs/resolv.conf, but there is no
	# /etc/resolv.conf, which is where the static (musl) curl looks.
	[ "$NO_WIFI" = "1" ] && return 0
	grep -q '^nameserver' /etc/resolv.conf 2>/dev/null && return 0
	rm -f /etc/resolv.conf 2>/dev/null
	if grep -q '^nameserver' /appconfigs/resolv.conf 2>/dev/null; then
		ln -s /appconfigs/resolv.conf /etc/resolv.conf 2>/dev/null \
			|| cat /appconfigs/resolv.conf >/etc/resolv.conf
	else
		printf 'nameserver 1.1.1.1\nnameserver 8.8.8.8\n' >/etc/resolv.conf
	fi
	log "DNS: $(grep '^nameserver' /etc/resolv.conf | tr '\n' ' ')"
}

curl_error() {
	tail -n 1 "$TMP/curl.err" 2>/dev/null | cut -c1-200
}

# api METHOD PATH OUTFILE [curl args...] -> prints HTTP status code
api() {
	_m="$1"; _p="$2"; _o="$3"; shift 3
	_ca=""
	[ -f "$CACERT" ] && _ca="--cacert $CACERT"
	# shellcheck disable=SC2086
	"$CURL" -sS $_ca --connect-timeout 15 --max-time 600 -A "70mai-uploader/$VERSION" \
		-H "X-Device-Id: $DEVICE_ID" -H "X-Device-Key: $DEVICE_KEY" \
		-H "X-Uploader-Version: $VERSION" -H "X-Wifi-Mode: $(wifi_mode)" \
		-H "X-Camera-Time: $(date +%s)" \
		-X "$_m" -o "$_o" -w '%{http_code}' "$@" "$UPLOAD_URL$_p" 2>>"$TMP/curl.err"
}

register() {
	_code="$(api POST /api/v1/register "$TMP/resp" --data-binary "$DEVICE_NAME")"
	log "register: $_code $(head -c 64 "$TMP/resp" 2>/dev/null) $(curl_error)"
}

ap_credentials() {
	# AP name/password for the web UI: config.txt first, else the running hostapd config.
	AP_SSID="$(cfg_get wifi_ap_ssid '')"
	AP_PASS="$(cfg_get wifi_ap_pass '')"
	if [ -f /tmp/hostapd.conf ]; then
		[ -n "$AP_SSID" ] || AP_SSID="$(sed -n 's/^ssid=//p' /tmp/hostapd.conf | head -1)"
		[ -n "$AP_PASS" ] || AP_PASS="$(sed -n 's/^wpa_passphrase=//p' /tmp/hostapd.conf | head -1)"
	fi
	AP_SSID="$(printf '%s' "$AP_SSID" | tr -d '\r\n')"
	AP_PASS="$(printf '%s' "$AP_PASS" | tr -d '\r\n')"
}

sd_headers() {
	df -k "$MMC" 2>/dev/null | awk 'NR==2 {print $4" "$2}'
}

build_manifest() {
	: >"$TMP/manifest"
	for _d in $FOLDERS; do
		[ -d "$MMC/$_d" ] || continue
		# Skip hidden files and folders (Event/.s_Front = small preview copies).
		find "$MMC/$_d" -type f ! -path '*/.*' 2>/dev/null
	done | xargs -r stat -c "%n${TAB}%s${TAB}%Y" 2>/dev/null | sed "s#^$MMC/##" >"$TMP/manifest"
}

send_log() {
	_sz="$(stat -c %s "$LOG" 2>/dev/null || echo 0)"
	[ "$_sz" = "$LAST_LOG_SIZE" ] && return 0
	tail -n 150 "$LOG" >"$TMP/logtail"
	api POST /api/v1/log "$TMP/resp" --data-binary "@$TMP/logtail" >/dev/null && LAST_LOG_SIZE="$_sz"
}

# upload_file PATH OFFSET -> 0 done/skipped, 1 network/server error
upload_file() {
	_path="$1"; _off="$2"
	_f="$MMC/$_path"
	if [ ! -f "$_f" ]; then
		log "skip $_path: missing"
		return 0
	fi
	set -- $(stat -c '%s %Y' "$_f")
	_size="$1"; _mtime="$2"
	if [ $(( $(date +%s) - _mtime )) -lt "$MIN_AGE_SEC" ]; then
		return 0
	fi
	_rate=""
	[ "$RATE_KBPS" -gt 0 ] && _rate="--limit-rate ${RATE_KBPS}k"
	_chunk=$((BS * CHUNK_BLOCKS))
	log "upload $_path from $_off/$_size"
	while [ "$_off" -lt "$_size" ]; do
		if [ $((_off % BS)) -ne 0 ]; then
			log "upload $_path: unaligned offset $_off, restarting"
			_off=0
		fi
		dd if="$_f" of="$TMP/chunk" bs=$BS skip=$((_off / BS)) count=$CHUNK_BLOCKS 2>/dev/null
		_n="$(stat -c %s "$TMP/chunk" 2>/dev/null || echo 0)"
		if [ "$_n" -le 0 ] || [ "$_n" -gt "$_chunk" ]; then
			log "upload $_path: read failed at $_off"
			return 0
		fi
		_sha="$(sha256sum "$TMP/chunk" | cut -d' ' -f1)"
		# shellcheck disable=SC2086
		_code="$(api PUT "/api/v1/upload?path=$_path" "$TMP/resp" $_rate \
			-H "Content-Type: application/octet-stream" \
			-H "Content-Range: bytes $_off-$((_off + _n - 1))/$_size" \
			-H "X-Chunk-Sha256: $_sha" --data-binary "@$TMP/chunk")"
		case "$_code" in
			200)
				if grep -q '^DONE' "$TMP/resp"; then
					_off=$_size
				else
					_off=$((_off + _n))
				fi
				;;
			409)
				_new="$(sed -n "s/^OFFSET$TAB//p" "$TMP/resp")"
				[ -n "$_new" ] || { log "upload $_path: 409 without offset"; return 1; }
				_off="$_new"
				;;
			*)
				log "upload $_path: HTTP $_code at $_off $(head -c 100 "$TMP/resp" 2>/dev/null)"
				rm -f "$TMP/chunk"
				return 1
				;;
		esac
	done
	rm -f "$TMP/chunk"
	log "upload $_path: done"
	return 0
}

# sync_once -> 0 ok, 1 error, 2 nothing more to do this round
sync_once() {
	: >"$TMP/curl.err"
	ensure_dns
	if [ ! -x "$CURL" ] && ! command -v "$CURL" >/dev/null 2>&1; then
		log "curl missing or not executable: $CURL"
		return 1
	fi
	build_manifest
	set -- $(sd_headers)
	ap_credentials
	_code="$(api POST /api/v1/sync "$TMP/resp" -H "X-Sd-Free: ${1:-}" -H "X-Sd-Total: ${2:-}" \
		-H "X-Ap-Ssid: $AP_SSID" -H "X-Ap-Pass: $AP_PASS" -H "X-Lan-Ip: $(wlan_ip)" \
		-H "X-Local-Web: $(cfg_get local_web 1):$(cfg_get local_web_port 8080)" \
		--data-binary "@$TMP/manifest")"
	case "$_code" in
		200) ;;
		401) register; return 1 ;;
		403)
			_st="$(head -c 32 "$TMP/resp")"
			[ "$_st" != "$LAST_STATUS" ] && log "server says $_st (approve this camera in the web UI)"
			LAST_STATUS="$_st"
			[ "$_st" = "PENDING" ] && register
			return 1
			;;
		*) log "sync failed: HTTP $_code $(curl_error)"; return 1 ;;
	esac
	LAST_STATUS=APPROVED

	: >"$TMP/server.cfg"; : >"$TMP/ack"; : >"$TMP/uploads"
	_ap_min=""
	cp "$TMP/resp" "$TMP/plan"
	while IFS="$TAB" read -r _kind _a _b _c; do
		case "$_kind" in
			CFG) printf '%s\t%s\n' "$_a" "$_b" >>"$TMP/server.cfg" ;;
			DELETE)
				case "$_b" in
					*..*|/*) printf '%s\terr\tbad path\n' "$_a" >>"$TMP/ack"; continue ;;
				esac
				if rm -f "$MMC/$_b" 2>/dev/null && [ ! -e "$MMC/$_b" ]; then
					log "deleted $_b (remote request)"
					printf '%s\tok\t\n' "$_a" >>"$TMP/ack"
				else
					printf '%s\terr\trm failed\n' "$_a" >>"$TMP/ack"
				fi
				;;
			UPLOAD) printf '%s\t%s\n' "$_a" "$_b" >>"$TMP/uploads" ;;
			AP)
				_ap_min="$(num "$_b" 30)"
				printf '%s\tok\tswitching to AP for %s min\n' "$_a" "$_ap_min" >>"$TMP/ack"
				;;
		esac
	done <"$TMP/plan"

	if [ -s "$TMP/server.cfg" ] && ! cmp -s "$TMP/server.cfg" "$BASE/server.cfg"; then
		cp "$TMP/server.cfg" "$BASE/server.cfg"
		load_server_cfg
		log "server config: fallback=${AP_FALLBACK_SEC}s retry=${STA_RETRY_SEC}s poll=${POLL_SEC}s rate=${RATE_KBPS}KB/s"
	fi
	if [ -s "$TMP/ack" ]; then
		sync
		api POST /api/v1/ack "$TMP/resp" --data-binary "@$TMP/ack" >/dev/null
	fi

	if [ -n "$_ap_min" ]; then
		log "remote request: AP mode for $_ap_min min"
		send_log
		FORCE_AP_UNTIL=$(( $(date +%s) + _ap_min * 60 ))
		switch_wifi AP
		return 1
	fi

	[ -s "$TMP/uploads" ] || return 2
	_did=0
	while IFS="$TAB" read -r _p _o; do
		in_parking && return 1
		sta_connected || return 1
		upload_file "$_p" "$(num "$_o" 0)" || return 1
		_did=1
	done <"$TMP/uploads"
	[ "$_did" = "1" ] && return 0
	return 2
}

# ------------------------------------------------------------------ main loop

load_server_cfg
log "uploader v$VERSION start id=$DEVICE_ID url=$UPLOAD_URL"

# Helpers next to this script (each keeps its own pid file, so a second start is a no-op).
# Started from here so an older autorun_uploader.sh on the card is enough.
for _h in buttons localweb; do
	if [ -f "$BASE/$_h.sh" ]; then
		( sh "$BASE/$_h.sh" >/dev/null 2>"$BASE/$_h.err" ) </dev/null >/dev/null 2>&1 &
	else
		log "$_h.sh not found in $BASE"
	fi
done
LAST_LOG_SIZE=""
LAST_STATUS=""
START="$(date +%s)"
LAST_CONNECTED="$START"
MODE_SINCE="$START"
LAST_MODE=""
WAS_CONNECTED=""
FORCE_AP_UNTIL=0

while true; do
	if in_parking; then
		log "parking mode, exit"
		break
	fi
	NOW="$(date +%s)"
	MODE="$(wifi_mode)"
	if [ "$MODE" != "$LAST_MODE" ]; then
		log "wifi mode: $MODE"
		MODE_SINCE="$NOW"
		LAST_MODE="$MODE"
		[ "$MODE" = "STA" ] && LAST_CONNECTED="$NOW"
		WAS_CONNECTED=""
	fi

	case "$MODE" in
		STA)
			if sta_connected; then
				LAST_CONNECTED="$NOW"
				if [ "$WAS_CONNECTED" != "1" ]; then
					log "connected to home Wi-Fi, ip=$(wlan_ip)"
					WAS_CONNECTED=1
				fi
				sync_once
				_rc=$?
				send_log
				[ "$ONESHOT" = "1" ] && break
				# Keep going immediately while there is work and the link is up.
				[ "$_rc" = "0" ] && continue
				sleep "$POLL_SEC"
			else
				if [ "$WAS_CONNECTED" != "0" ]; then
					log "waiting for home Wi-Fi ($(iw dev wlan0 link 2>/dev/null | head -1), ip=$(wlan_ip))"
					WAS_CONNECTED=0
				fi
				if [ "$AP_FALLBACK_SEC" -gt 0 ] && [ $((NOW - LAST_CONNECTED)) -ge "$AP_FALLBACK_SEC" ]; then
					log "home Wi-Fi not reachable for $((NOW - LAST_CONNECTED))s"
					switch_wifi AP
				fi
				sleep 5
			fi
			;;
		AP)
			if [ -e /tmp/wifi_manual_ap ]; then
				# Switched by the button combo (buttons.sh): stay until toggled back.
				:
			elif [ "$FORCE_AP_UNTIL" -gt 0 ]; then
				# Requested from the web UI: stay in AP until the time is up and no phone is connected.
				if [ "$NOW" -ge "$FORCE_AP_UNTIL" ] && [ "$(ap_clients)" = "0" ]; then
					FORCE_AP_UNTIL=0
					switch_wifi STA
				fi
			elif [ "$STA_RETRY_SEC" -gt 0 ] && [ $((NOW - MODE_SINCE)) -ge "$STA_RETRY_SEC" ] \
				&& [ "$(ap_clients)" = "0" ]; then
				switch_wifi STA
			fi
			sleep 15
			;;
		*)
			# Wi-Fi switched off by the user: stay out of the way.
			sleep 30
			;;
	esac
done

rm -f "$PIDFILE"
