#!/bin/sh
# Builds dist/uploader/ = folder to copy to the root of the camera SD card.
# Downloads a static ARMv7 curl (musl) and the Mozilla CA bundle.
set -eu
CURL_VERSION="${CURL_VERSION:-8.22.0}"
cd "$(dirname "$0")"
OUT=dist/uploader
mkdir -p "$OUT"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
curl -fsSL "https://github.com/stunnel/static-curl/releases/download/${CURL_VERSION}/curl-linux-armv7-musl-${CURL_VERSION}.tar.xz" -o "$tmp/curl.tar.xz"
tar -xJf "$tmp/curl.tar.xz" -C "$tmp"
cp "$(find "$tmp" -type f -name curl | head -1)" "$OUT/curl"
curl -fsSL https://curl.se/ca/cacert.pem -o "$OUT/cacert.pem"
cp uploader.sh buttons.sh localweb.sh localweb.cgi "$OUT/"
file "$OUT/curl" 2>/dev/null || true
echo "Done: copy $(pwd)/$OUT to the SD card as /uploader (next to config.txt)."
