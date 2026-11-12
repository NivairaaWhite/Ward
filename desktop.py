#!/usr/bin/env python3
"""
desktop.py
----------
The "just double-click it" entry point -- start.bat/start.sh call this
instead of running app.py's dev server directly. Starts the same Flask
app in a background thread, then opens it as close to a native window
as your machine supports:

  1. If the optional `pywebview` package is installed *and* your OS has
     a working webview backend (built in on Windows/macOS; needs
     webkit2gtk or qtwebengine on Linux -- `pip install pywebview`),
     you get a real app window: no browser tabs or address bar.
  2. Otherwise -- the common case if you haven't installed pywebview,
     or on a Linux box without a webview backend -- this falls back to
     opening your default browser to the local address. Still one
     click, just a browser tab instead of a standalone window.

Either way: no typing a URL, no touching a terminal after the first
run. pywebview is deliberately NOT a hard dependency (it needs system
GUI libraries on Linux that aren't guaranteed present, and Ward should
never fail to start over an optional nicety) -- see README "Running as
a desktop app."
"""
from __future__ import annotations

import socket
import sys
import time
import threading
import webbrowser

HOST = "127.0.0.1"
PREFERRED_PORT = 5000


def _port_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.25)
        return s.connect_ex((host, port)) == 0


def _pick_port() -> int:
    """5000 if it's free, else any free port. On macOS, AirPlay Receiver
    holds 5000, which used to make Ward open the wrong page."""
    for port in (PREFERRED_PORT, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((HOST, port))
            except OSError:
                continue
            return s.getsockname()[1]
    raise RuntimeError("No free local port available.")


def _run_flask(port: int) -> None:
    # Importing app builds the Flask app exactly once (app.py creates it
    # at module level) -- calling create_app() again here built it twice.
    from app import app

    # debug=False, use_reloader=False: this is the end-user launcher, not
    # a dev server -- no reloader subprocess spawning a second copy.
    app.run(host=HOST, port=port, debug=False, use_reloader=False)


def main() -> None:
    port = _pick_port()
    server_thread = threading.Thread(target=_run_flask, args=(port,), daemon=True)
    server_thread.start()

    print(f"Starting Ward on http://{HOST}:{port} ...")
    ready = False
    for _ in range(80):  # up to ~20s for first-run DB setup
        if _port_open(HOST, port):
            ready = True
            break
        if not server_thread.is_alive():
            # The Flask thread crashed (e.g. a missing package) -- its
            # traceback is printed above, so don't keep waiting.
            break
        time.sleep(0.25)
    if not ready:
        print("Ward didn't start -- check the error output above.", file=sys.stderr)
        sys.exit(1)

    url = f"http://{HOST}:{port}"

    try:
        import webview  # optional -- only present if you `pip install pywebview`

        webview.create_window("Ward", url, width=1180, height=820, min_size=(720, 560))
        webview.start()
    except Exception:
        # No pywebview, or no GUI backend on this system -- never let an
        # optional nicety stop Ward from starting.
        webbrowser.open(url)
        print(f"Ward is running at {url}")
        print("Leave this window open. Close it (or Ctrl+C) to stop Ward.")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            print("\nStopping Ward.")


if __name__ == "__main__":
    main()
