#!/bin/sh
# Directory listing for busybox httpd (run as /cgi-bin/index.cgi for every folder URL).

MMC=/mnt/mmc
ROOT="${ROOT:-/tmp/lweb}"
FOLDERS="Event Normal Parking Lapse Photo"

uri="${REQUEST_URI%%\?*}"
case "$uri" in
	*..*|*\'*|*\"*|*\<*|*\>*) printf 'Status: 400 Bad Request\r\nContent-Type: text/plain\r\n\r\nbad path\n'; exit 0 ;;
esac
[ "${uri%/}" = "$uri" ] && uri="$uri/"

# Folders created after boot (e.g. the first Event) get their link on demand.
for _d in $FOLDERS; do
	[ -d "$MMC/$_d" ] && [ ! -e "$ROOT/$_d" ] && ln -s "$MMC/$_d" "$ROOT/$_d"
done

dir="$ROOT$uri"
if [ ! -d "$dir" ]; then
	printf 'Status: 404 Not Found\r\nContent-Type: text/plain\r\n\r\nnot found\n'
	exit 0
fi

printf 'Content-Type: text/html; charset=utf-8\r\nCache-Control: no-store\r\n\r\n'
cat <<EOF
<!doctype html><html lang="cs"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kamera ${uri}</title>
<style>
body{font:15px/1.4 system-ui,sans-serif;margin:0;padding:12px 16px;background:#f4f5f7;color:#1d2026}
@media (prefers-color-scheme:dark){body{background:#111317;color:#e6e8eb}a{color:#7aa7ff}td{border-color:#2b3038}}
h1{font-size:1.2rem}table{border-collapse:collapse;width:100%}td{padding:6px 8px;border-bottom:1px solid #e3e5e9}
td.n{white-space:nowrap}.m{opacity:.7;font-size:.9em}a.f{font-family:ui-monospace,monospace;word-break:break-all}
</style></head><body>
<h1><a href="/">Kamera</a>${uri}</h1>
EOF

if [ "$uri" = "/" ]; then
	echo "<table>"
	for _d in $FOLDERS; do
		[ -e "$ROOT/$_d" ] || continue
		_n="$(find "$MMC/$_d" -type f ! -path '*/.*' 2>/dev/null | wc -l)"
		echo "<tr><td><a href=\"/$_d/\">$_d</a></td><td class=\"m\">$_n souborů</td></tr>"
	done
	echo "</table>"
else
	echo "<p class=\"m\"><a href=\"../\">&larr; zpět</a> · nejnovější nahoře · H.265 videa přehraje Safari/Edge nebo VLC</p><table>"
	cd "$dir" || exit 0
	ls -A 2>/dev/null | grep -v '^\.' | xargs -r stat -c '%Y|%s|%F|%n' 2>/dev/null | sort -t'|' -k1,1nr | awk -F'|' -v uri="$uri" '
		function human(n) {
			if (n >= 1073741824) return sprintf("%.1f GB", n / 1073741824)
			if (n >= 1048576) return sprintf("%.1f MB", n / 1048576)
			if (n >= 1024) return sprintf("%.0f KB", n / 1024)
			return n " B"
		}
		$3 == "directory" { printf "<tr><td class=\"n\"></td><td><a href=\"%s%s/\">%s/</a></td><td></td><td></td></tr>\n", uri, $4, $4; next }
		{
			printf "<tr><td class=\"n\">%s</td><td><a class=\"f\" href=\"%s%s\">%s</a></td><td class=\"n m\">%s</td>", strftime("%d.%m.%Y %H:%M", $1), uri, $4, $4, human($2)
			printf "<td class=\"n\"><a href=\"%s%s\" download>stáhnout</a></td></tr>\n", uri, $4
		}'
	echo "</table>"
fi
echo "</body></html>"
