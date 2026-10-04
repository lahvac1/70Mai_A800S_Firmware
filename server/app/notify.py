import logging
import threading
import urllib.request

from . import config

log = logging.getLogger("notify")


def notify(title: str, message: str) -> None:
    """Fire-and-forget POST to an ntfy-compatible URL."""
    if not config.NOTIFY_URL:
        return

    def _send():
        try:
            req = urllib.request.Request(
                config.NOTIFY_URL,
                data=message.encode(),
                headers={"Title": title.encode("ascii", "replace").decode(), "Tags": "movie_camera"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=10).close()
        except Exception as exc:  # noqa: BLE001
            log.warning("notify failed: %s", exc)

    threading.Thread(target=_send, daemon=True).start()
