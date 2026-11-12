"""
desktop.py
----------
The "just double-click it" entry point used by start.bat / start.sh.

Starts Ward's Flask app in a background thread and opens it in a native
window if the optional `pywebview` package is installed (and your OS has a
webview backend); otherwise it opens your default browser.

The port is chosen safely: WARD_PORT (default 5000) if it is genuinely free,
otherwise any free port. This matters on macOS, where AirPlay Receiver owns
port 5000 and would otherwise be mistaken for Ward.
"""
from __future__ import annotations

import logging
import socket
import sys
import threading
import time
import webbrowser

from werkzeug.serving import make_server

from app import create_app
from config import Config

log = logging.getLogger("ward.desktop")


def _free_port(host: str, preferred: int) -> int:
    """`preferred` if we can actually bind it right now, else an OS-chosen port."""
    for port in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((host, port))
            except OSError:
                continue
            return s.getsockname()[1]
    raise RuntimeError("No free local port available.")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    host = Config.HOST
    port = _free_port(host, Config.PORT)

    try:
        # Binding happens here, in this thread, so a failure is reported
        # immediately instead of being mistaken for another server answering.
        server = make_server(host, port, create_app(), threaded=True)
    except Exception:
        log.exception("Ward could not start.")
        sys.exit(1)

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.2)
    if not thread.is_alive():
        log.error("Ward's server stopped immediately -- see the error above.")
        sys.exit(1)

    url = f"http://{host}:{port}"
    log.info("Ward is running at %s", url)

    try:
        import webview  # optional: `pip install pywebview`

        webview.create_window("Ward", url, width=1180, height=820, min_size=(720, 560))
        webview.start()
    except ImportError:
        _run_in_browser(url, thread)
    except Exception:
        log.warning("Native window unavailable; falling back to your browser.", exc_info=True)
        _run_in_browser(url, thread)
    finally:
        server.shutdown()


def _run_in_browser(url: str, thread: threading.Thread) -> None:
    webbrowser.open(url)
    log.info("Leave this window open. Press Ctrl+C to stop Ward.")
    try:
        while thread.is_alive():
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Stopping Ward.")


if __name__ == "__main__":
    main()
