#!/bin/sh

##############################
## CLOUD UPLOADER
##############################
## Starts /mnt/mmc/uploader/uploader.sh in the background on every boot.
## Keep this script on the SD card (do NOT move it to Autorun_completed).
## Note: autorun scripts are sourced by the firmware, so never call "exit" here.

if [ -f /mnt/mmc/uploader/uploader.sh ]; then
	( sleep 20; ionice -c2 -n7 nice -n 19 sh /mnt/mmc/uploader/uploader.sh >/dev/null 2>&1 ) </dev/null >/dev/null 2>&1 &
fi
## Button combo (default up+down held 3 s) toggles Wi-Fi AP <-> home Wi-Fi.
if [ -f /mnt/mmc/uploader/buttons.sh ]; then
	( sleep 15; sh /mnt/mmc/uploader/buttons.sh >/dev/null 2>/mnt/mmc/uploader/buttons.err ) </dev/null >/dev/null 2>&1 &
fi
## Local web page with the recordings: http://<camera IP>:8080/
if [ -f /mnt/mmc/uploader/localweb.sh ]; then
	( sleep 10; sh /mnt/mmc/uploader/localweb.sh >/dev/null 2>/mnt/mmc/uploader/localweb.err ) </dev/null >/dev/null 2>&1 &
fi
