# Cloud uploader (camera side)

Background script that uploads recordings from the SD card to the upload server
(`server/`) whenever the camera is connected to your home Wi-Fi in STA mode.
The server then moves them to Google Drive.

No firmware rebuild is needed: it runs from the SD card through the existing
autorun mechanism. AP mode (downloading videos with the phone) keeps working.

## Install

1. Build the SD card folder (downloads a static ARMv7 `curl` and the CA bundle):

   ```
   ./fetch_deps.sh
   ```

2. Copy to the SD card root:
   - `dist/uploader/` as `/uploader/` (contains `uploader.sh`, `buttons.sh`, `localweb.sh`, `localweb.cgi`, `curl`, `cacert.pem`)
   - `../Scripts/autorun_uploader.sh`

3. Add to `config.txt`:

   ```
   wifi_mode=STA
   wifi_sta_ssid=HOME_WIFI
   wifi_sta_pass=homepassword
   wifi_sta_time_sync=1
   upload_enable=1
   upload_url=https://dash.firelahva.cz
   upload_device_name=Octavia        # optional, shown on the server
   upload_ap_fallback_sec=120        # optional, server value wins once paired
   upload_sta_retry_sec=600          # optional, server value wins once paired
   ```

4. Reboot the camera. It shows up on the server as *waiting for approval*.
   Approve it once in the web UI.

## How it works

```
boot -> STA (home Wi-Fi)
          | connected                 | not connected for ap_fallback_sec
          v                           v
     sync with server               switch to AP (phone app works)
     - send file list                 | no phone connected for sta_retry_sec
     - delete files on request        v
     - upload selected files        try STA again
```

- Recording is not affected: the camera's recorder (`cardv`) is a separate
  process; the uploader only reads finished files (older than `min_age_sec`) and
  runs with low CPU/IO priority.
- Uploads are sent in 4 MB chunks with a SHA-256 checksum and resume where they
  stopped after the car leaves Wi-Fi range or power is cut.
- In parking mode the firmware turns Wi-Fi off, so the uploader exits; it starts
  again on the next boot.
- Which folders are uploaded, rate limit and timings are set per camera on the
  server (default: `Event` only).

## Switching to AP at home

- **Button combo:** hold *up + down* together for 3 s. STA -> AP (stays in AP until
  toggled back or reboot), AP -> STA. Configure in `config.txt`:
  `upload_button_combo=103+108` (key codes, `off` disables), `upload_button_hold_sec=3`.
  `upload_button_debug=1` logs every key press to `uploader.log`, which shows the
  codes of the physical buttons if the default combo does not match.
- **Web UI:** camera detail -> *Přepnout do AP na N min*; the camera returns to home
  Wi-Fi after N minutes once no phone is connected.

The camera's AP name and password (from `wifi_ap_ssid` / `wifi_ap_pass`, or the
running hostapd config) are shown on the camera page in the web UI.

## Local download page

`localweb.sh` serves the video folders (Event, Normal, Parking, Lapse, Photo) at
`http://<camera IP>:8080/`, on the home Wi-Fi and in AP mode, with a list sorted
newest first, download links and seeking (HTTP Range). `config.txt`:

```
local_web=1
local_web_port=8080
local_web_user=admin
local_web_pass=secret      # empty = no password
```

The camera's LAN address and the link are shown on the camera page in the web UI.

## Static IP

Supported by the firmware itself (`config.txt`), DNS then uses the gateway:

```
wifi_sta_ip=192.168.1.50
wifi_sta_gateway=192.168.1.1
wifi_sta_mask=255.255.255.0
```

A DHCP reservation for the camera's MAC address in the router works as well.

## Files on the SD card

| File | Purpose |
|---|---|
| `uploader/device.id`, `uploader/device.key` | Camera identity created on first run. Delete both to re-pair. |
| `uploader/server.cfg` | Last configuration received from the server |
| `uploader/uploader.log` | Log (also visible in the web UI) |

## Debugging

Enable `telnet=1` in `config.txt`, then on the camera:

```
tail -f /mnt/mmc/uploader/uploader.log
ps | grep uploader
```

Run it against a local server without Wi-Fi handling (any Linux with BusyBox + curl):

```
MMC=/path/to/sdcard CURL=curl NO_WIFI=1 ONESHOT=1 sh uploader.sh
```
