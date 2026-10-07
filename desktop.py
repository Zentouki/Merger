"""Merger as a desktop app: the same server, shown in its own window.

Run from source:   pip install pywebview   then   python desktop.py
Build for Windows: Build-Desktop.bat   (makes dist\\Merger\\Merger.exe)

Your accounts, login and settings live in a per-user folder (on Windows: %APPDATA%\\Merger),
not next to the program, so updating or moving the program never touches them."""
import json
import logging
import os
import shutil
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path

PORT = 5000  # must match the redirect address registered in Google Cloud
URL = f"http://localhost:{PORT}"
REDIRECT = f"{URL}/oauth2callback"


def data_dir():
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    d = base / "Merger"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    return d


DATA = Path(os.environ.get("MERGER_DATA") or data_dir())  # a MERGER_DATA setting wins over the default folder
DATA.mkdir(mode=0o700, parents=True, exist_ok=True)
os.environ["MERGER_DATA"] = str(DATA)  # must be set before app.py is imported
os.environ.setdefault("MERGER_PORT", str(PORT))


def setup_logging():
    handler = RotatingFileHandler(DATA / "merger.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    if sys.stdout is None or sys.stderr is None:  # a windowed .exe has no console to print to
        sys.stdout = sys.stderr = open(os.devnull, "w")


def message(text):
    """A plain native message box, so problems are not silent in a windowed app."""
    logging.error(text)
    try:
        if sys.platform == "win32":
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, text, "Merger", 0x10)
            return
    except Exception:
        pass
    print(text, file=sys.stderr)


def _opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never route localhost through a proxy


def port_open():
    with socket.socket() as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def is_merger():
    try:
        return b"Merger" in _opener().open(f"{URL}/login", timeout=2).read(3000)
    except Exception:
        return False


def start_server():
    import app as merger  # imported only now, so MERGER_DATA above is already in place
    merger.tighten()
    if merger.STORE.exists():
        merger.save(merger.load())  # encrypts an old plain-text accounts file
    from waitress import serve
    threading.Thread(target=lambda: serve(merger.app, host="127.0.0.1", port=PORT, threads=16,
                                          max_request_body_size=5 * 1024 ** 3), daemon=True).start()
    for _ in range(60):
        if port_open():
            return merger
        time.sleep(0.25)
    raise RuntimeError("The Merger server did not start.")


class Api:
    """What the page may ask the desktop shell to do. Everything else stays in the web app."""

    def open_external(self, url):
        if isinstance(url, str) and url.startswith(("https://", "http://")):
            webbrowser.open(url)
            return True
        return False

    def link_account(self):
        # Google doesn't allow its sign-in inside embedded windows, so linking runs in the real browser.
        webbrowser.open(f"{URL}/auth/add")
        return True


def ensure_client_secret(window, target):
    """First run: ask for the client_secret.json downloaded from Google Cloud and keep a private copy."""
    if target.exists():
        return
    import webview
    ask = getattr(window, "create_confirmation_dialog", None)
    if ask and not ask("Merger", "To link Google accounts, Merger needs the client_secret.json file you "
                                 "downloaded from Google Cloud.\n\nChoose it now?"):
        return
    kind = getattr(getattr(webview, "FileDialog", None), "OPEN", None) or webview.OPEN_DIALOG
    picked = window.create_file_dialog(kind, allow_multiple=False, file_types=("JSON files (*.json)",))
    if not picked:
        return
    try:
        cfg = json.loads(Path(picked[0]).read_text(encoding="utf-8"))
        section = cfg.get("web") or cfg.get("installed") or {}
        if not (section.get("client_id") and section.get("client_secret")):
            raise ValueError("not a Google OAuth client file")
    except Exception:
        window.evaluate_js("alert('That does not look like a Google OAuth client file. Download it again from "
                           "Google Cloud (Credentials, OAuth client ID) and choose it again from the Menu.')")
        return
    shutil.copyfile(picked[0], target)
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass
    if "web" in cfg and REDIRECT not in cfg["web"].get("redirect_uris", []):
        window.evaluate_js(f"alert('Saved. But this file does not list {REDIRECT} as an authorised redirect "
                           "address, so linking will fail until you add it in Google Cloud.')")
    else:
        window.evaluate_js("location.reload()")


def main():
    setup_logging()
    secret = Path(os.environ.get("MERGER_CLIENT_SECRET") or DATA / "client_secret.json")
    first_instance = not port_open()
    if first_instance:
        start_server()
    elif not is_merger():
        return message(f"Port {PORT} is used by another program. Close it and start Merger again.")
    import webview
    window = webview.create_window("Merger", URL + "/", js_api=Api(), width=1320, height=860, min_size=(900, 600))
    if hasattr(webview, "settings"):
        webview.settings["ALLOW_DOWNLOADS"] = True
    webview.start(lambda: first_instance and ensure_client_secret(window, secret),
                  private_mode=False, storage_path=str(DATA / "webview"))


if __name__ == "__main__":
    main()
