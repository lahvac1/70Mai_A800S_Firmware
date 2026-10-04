#!/bin/sh
#
# Button combo -> toggle Wi-Fi between AP (phone download) and STA (home Wi-Fi).
#
# Reads key events from /dev/input/event0 next to the firmware's button_hold.sh
# (every evdev reader gets all events). Holding both combo keys together for
# upload_button_hold_sec seconds toggles the mode:
#   STA -> AP:  stays in AP until toggled back or reboot (uploader does not
#               switch back to STA on its own while /tmp/wifi_manual_ap exists)
#   AP  -> STA: back to home Wi-Fi, uploads continue
#
# config.txt:
#   upload_button_combo=103+108   key codes (default up+down), "off" disables
#   upload_button_hold_sec=3
#   upload_button_debug=1         log every key press (to find the codes)

MMC="${MMC:-/mnt/mmc}"
BASE="${BASE:-$MMC/uploader}"
LOG="$BASE/uploader.log"
KEY_DEV="${KEY_DEV:-/dev/input/event0}"
APKAP="${APKAP:-/customer/wifi/ApkAp.sh}"
MANUAL_FLAG=/tmp/wifi_manual_ap
PIDFILE=/tmp/uploader_buttons.pid

export PATH=/sbin:/bin:/usr/bin:/usr/sbin:/customer/wifi:/config/bin:$PATH
export LD_LIBRARY_PATH=/lib:/customer/wifi/lib:$LD_LIBRARY_PATH

log() {
	echo "$(date '+%Y-%m-%d %H:%M:%S') buttons: $*" >>"$LOG"
}

cfg_get() {
	_v=""
	[ -f "$MMC/config.txt" ] && _v="$(sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//p" "$MMC/config.txt" \
		| head -1 | tr -d '\r' | sed 's/[[:space:]]*#.*$//; s/[[:space:]]*$//')"
	[ -n "$_v" ] && echo "$_v" || echo "$2"
}

log "start (pid $$)"
COMBO="$(cfg_get upload_button_combo 103+108)"
HOLD_SEC="$(cfg_get upload_button_hold_sec 3)"
DEBUG="$(cfg_get upload_button_debug 0)"
case "$COMBO" in off|OFF|0|"") log "disabled (upload_button_combo=$COMBO)"; exit 0 ;; esac
KEY_A="${COMBO%%+*}"
KEY_B="${COMBO##*+}"
case "$KEY_A$KEY_B" in *[!0-9]*) log "bad upload_button_combo=$COMBO"; exit 1 ;; esac
case "$HOLD_SEC" in ''|*[!0-9]*) HOLD_SEC=3 ;; esac
HOLD_MS=$((HOLD_SEC * 1000))

if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
	log "already running (pid $(cat "$PIDFILE")), exit"
	exit 0
fi
echo $$ >"$PIDFILE"
trap 'rm -f "$PIDFILE"; exit 0' INT TERM

# Wait for the input device (the firmware may create it late).
_i=0
while [ ! -e "$KEY_DEV" ]; do
	_i=$((_i + 1))
	[ "$_i" -gt 60 ] && { log "no $KEY_DEV, exit"; rm -f "$PIDFILE"; exit 1; }
	sleep 1
done

# read_event -> sets EV_MS TYPE CODE VAL (16-byte struct input_event of a 32-bit kernel)
read_event() {
	_p="$(dd if="$KEY_DEV" bs=16 count=1 2>/dev/null | od -An -tu1 -N 16 2>/dev/null | awk '
		{ for (i = 1; i <= NF; i++) b[++n] = $i }
		END {
			if (n < 16) exit 1
			sec = b[1] + b[2]*256 + b[3]*65536 + b[4]*16777216
			usec = b[5] + b[6]*256 + b[7]*65536 + b[8]*16777216
			printf "%d %d %d %d", sec * 1000 + int(usec / 1000), b[9] + b[10]*256, b[11] + b[12]*256, b[13]
		}')"
	[ -n "$_p" ] || return 1
	set -- $_p
	EV_MS="$1"; TYPE="$2"; CODE="$3"; VAL="$4"
}

toggle_wifi() {
	if pidof hostapd >/dev/null 2>&1; then
		log "combo held: AP -> home Wi-Fi (STA)"
		rm -f "$MANUAL_FLAG"
		"$APKAP" start STA >/dev/null 2>&1 &
	else
		log "combo held: -> AP (stays until toggled back or reboot)"
		touch "$MANUAL_FLAG"
		"$APKAP" start AP >/dev/null 2>&1 &
	fi
}

log "watching $KEY_DEV, combo $KEY_A+$KEY_B held ${HOLD_SEC}s toggles AP/STA"
DOWN_A=0
DOWN_B=0
BOTH_SINCE=0
FIRED=0

while true; do
	read_event || { sleep 1; continue; }
	[ "$TYPE" = "1" ] || continue
	[ "$DEBUG" = "1" ] && [ "$VAL" != "2" ] && log "key code=$CODE value=$VAL"
	case "$CODE" in
		"$KEY_A") [ "$VAL" = "0" ] && DOWN_A=0 || DOWN_A=1 ;;
		"$KEY_B") [ "$VAL" = "0" ] && DOWN_B=0 || DOWN_B=1 ;;
		*) continue ;;
	esac
	if [ "$DOWN_A" = "1" ] && [ "$DOWN_B" = "1" ]; then
		[ "$BOTH_SINCE" = "0" ] && BOTH_SINCE="$EV_MS" && FIRED=0
		# Auto-repeat events of a held key keep coming, so the hold time is checked on each.
		if [ "$FIRED" = "0" ] && [ $((EV_MS - BOTH_SINCE)) -ge "$HOLD_MS" ]; then
			FIRED=1
			toggle_wifi
		fi
	else
		# Released: still counts if both were held long enough (no auto-repeat on this hardware).
		if [ "$BOTH_SINCE" != "0" ] && [ "$FIRED" = "0" ] && [ $((EV_MS - BOTH_SINCE)) -ge "$HOLD_MS" ]; then
			toggle_wifi
		fi
		BOTH_SINCE=0
		FIRED=0
	fi
done
