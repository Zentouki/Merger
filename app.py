"""Merger: link several Google Drive accounts and use them as one storage.
Run locally: python app.py  ->  http://localhost:5000
"""
import functools, gzip, hmac, json, os, re, secrets, tempfile, threading, time
from collections import OrderedDict
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from urllib.parse import quote

from flask import (Flask, Response, g, jsonify, redirect, request, session,
                   stream_with_context)
from cryptography.fernet import Fernet
from requests.adapters import HTTPAdapter
from werkzeug.security import check_password_hash, generate_password_hash
from google.auth.transport.requests import AuthorizedSession, Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

BASE = Path(__file__).parent
DATA = Path(os.environ.get("MERGER_DATA", BASE))  # accounts, login and logs live here (set MERGER_DATA to move them)
DATA.mkdir(mode=0o700, parents=True, exist_ok=True)
HOST = os.environ.get("MERGER_HOST", "127.0.0.1")
PORT = int(os.environ.get("MERGER_PORT", "5000"))
STORE = DATA / "accounts.json"          # linked accounts + refresh tokens (keep private)
SECRETS = Path(os.environ.get("MERGER_CLIENT_SECRET", DATA / "client_secret.json"))   # OAuth client downloaded from Google Cloud
SCOPES = ["https://www.googleapis.com/auth/drive"]
REDIRECT = os.environ.get("MERGER_REDIRECT", "http://localhost:5000/oauth2callback")
if REDIRECT.startswith(("http://localhost", "http://127.0.0.1")):
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")  # plain http is only acceptable for a localhost redirect
FOLDER = "application/vnd.google-apps.folder"
HISTORY = DATA / "history.jsonl"        # activity log of everything done through this app
THUMBS = OrderedDict()  # (account, file id) -> Drive thumbnail URL, capped so it can't grow forever
_TLOCK = threading.Lock()


def remember_thumb(key, link):
    with _TLOCK:
        THUMBS[key] = link
        THUMBS.move_to_end(key)
        while len(THUMBS) > 20000:
            THUMBS.popitem(last=False)


def write_private(path, text):
    """Write atomically, readable by the owner only (0600 on Linux and macOS)."""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as h:
        h.write(text)
    os.replace(tmp, path)


_LOGLOCK = threading.Lock()


def log(action, acct, name=""):
    clean = lambda s: re.sub(r"[\x00-\x1f\x7f]", " ", str(s))[:300]
    line = json.dumps({"t": time.strftime("%Y-%m-%d %H:%M:%S"), "action": clean(action),
                       "account": clean(acct), "name": clean(name)}) + "\n"
    with _LOGLOCK:
        if HISTORY.exists() and HISTORY.stat().st_size > 5_000_000:  # rotate so the log can't grow forever
            os.replace(HISTORY, HISTORY.with_name("history.old.jsonl"))
        fd = os.open(HISTORY, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as h:
            h.write(line)


app = Flask(__name__, static_folder=None)  # no /static route: nothing needs it
app.json.sort_keys = False  # no need to sort every key of big file lists
KEYFILE = DATA / "secret.key"          # keeps you signed in across restarts
if not KEYFILE.exists():
    write_private(KEYFILE, secrets.token_hex(32))
app.secret_key = KEYFILE.read_text()
SECURE = os.environ.get("MERGER_SECURE_COOKIES") == "1"  # set to 1 when you serve Merger over https
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=SECURE,
                  PERMANENT_SESSION_LIFETIME=timedelta(days=14))


# ---------- account + credential helpers ----------
_FER = []


def fernet():
    key = os.environ.get("MERGER_TOKEN_KEY") or (
        Path(os.environ["MERGER_TOKEN_KEY_FILE"]).read_text().strip() if os.environ.get("MERGER_TOKEN_KEY_FILE") else "")
    if not _FER and key:  # from a key file or an environment variable
        _FER.append(Fernet(key.encode()))
    if not _FER:
        try:  # key lives in the OS keychain when one is available
            import keyring
            k = keyring.get_password("merger", "tokens")
            if not k:
                k = Fernet.generate_key().decode()
                keyring.set_password("merger", "tokens", k)
        except Exception:  # otherwise fall back to a key file (weaker: it sits next to the data)
            kf = DATA / "token.key"
            if not kf.exists():
                write_private(kf, Fernet.generate_key().decode())
            k = kf.read_text()
        _FER.append(Fernet(k.encode()))
    return _FER[0]


_STORE_CACHE = {"sig": None, "data": {}}
_STORE_LOCK = threading.Lock()


def load():
    """Decrypted accounts. The file is only re-read and decrypted when it has changed on disk."""
    try:
        st = STORE.stat()
    except FileNotFoundError:
        return {}
    sig = (st.st_mtime_ns, st.st_size)
    with _STORE_LOCK:
        if _STORE_CACHE["sig"] != sig:
            raw = STORE.read_text().strip()
            data = json.loads(raw) if raw.startswith("{") else json.loads(fernet().decrypt(raw.encode()))
            _STORE_CACHE.update(sig=sig, data=data)
        return dict(_STORE_CACHE["data"])  # a copy, so callers can edit it freely before save()


def save(d):
    write_private(STORE, fernet().encrypt(json.dumps(d).encode()).decode())


CRED_LOCK = threading.Lock()


_CREDS = {}  # email -> Credentials, kept in memory instead of being rebuilt from disk on every call
_TL = threading.local()  # googleapiclient/httplib2 objects must not be shared between threads
_SESS = {}


def creds(email):
    with CRED_LOCK:  # listing runs in parallel; keep token refreshes from racing
        c = _CREDS.get(email)
        if c is None:
            c = _CREDS[email] = Credentials.from_authorized_user_info(load()[email], SCOPES)
        if not c.valid:  # expired or missing access token -> refresh and persist
            c.refresh(Request())
            d = load()
            d[email] = json.loads(c.to_json())
            save(d)
        return c


def drive(email):
    """A Drive client per thread and account. Building one is slow, so it is made once and reused."""
    c = creds(email)
    cache = _TL.__dict__.setdefault("svc", {})
    hit = cache.get(email)
    if hit is None or hit[0] is not c:  # rebuilt only if the account was re-linked
        hit = cache[email] = (c, build("drive", "v3", credentials=c, cache_discovery=False))
    return hit[1]


def session_for(email):
    """One keep-alive HTTP session per account, so thumbnails and downloads reuse their connections."""
    c = creds(email)
    hit = _SESS.get(email)
    if hit is None or hit[0] is not c:
        s = AuthorizedSession(c)
        s.mount("https://", HTTPAdapter(pool_connections=4, pool_maxsize=16))
        hit = _SESS[email] = (c, s)
    return hit[1]


def _quota(email):
    try:
        res = drive(email).about().get(fields="storageQuota,user(displayName,photoLink)").execute()
        q, u = res["storageQuota"], res.get("user", {})
        return {"email": email, "used": int(q["usage"]), "limit": int(q["limit"]) if q.get("limit") else None,
                "name": u.get("displayName"), "photo": u.get("photoLink")}
    except Exception as e:  # revoked token, network, etc.
        app.logger.warning("Storage check failed for %s: %s", email, e)  # detail stays in the server log
        return {"email": email, "error": "unreachable"}


QC = {}  # quotas and folder listings with their timestamps; cleared whenever we change a Drive
LIST_TTL = 20  # seconds a folder listing is reused (going back to a folder is then instant)


def quota(email):
    hit = QC.get(email)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    q = _quota(email)
    if "error" not in q:
        QC[email] = (time.time(), q)
    return q


def quotas():
    with ThreadPoolExecutor(max_workers=8) as ex:
        return list(ex.map(quota, load()))


def free(q):
    return float("inf") if q["limit"] is None else q["limit"] - q["used"]


def best(size=0):
    """Account with the most free space that can still fit `size` bytes."""
    ok = [q for q in quotas() if "error" not in q and free(q) >= size]
    return max(ok, key=free)["email"] if ok else None


# ---------- Merger login and request protection ----------
AUTH = DATA / "auth.json"               # admin email + password hash (keep private)
LOCK = {"fails": 0, "until": 0.0, "level": 0}
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")        # Google Drive file and folder ids
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}$")
RANGE_RE = re.compile(r"^bytes=\d*-\d*$")
TEXTY = re.compile(r"^text/|^application/(json|xml|javascript|x-javascript)$")
WRITES = ("POST", "PUT", "PATCH", "DELETE")


def bad_input():
    """True if an id, folder or account in the request is malformed or unknown.
    Ids are put into Drive queries and URLs, so only plain id characters are allowed."""
    src = dict(request.args)
    if request.is_json:
        j = request.get_json(silent=True)
        if isinstance(j, dict):
            src.update({k: v for k, v in j.items() if isinstance(v, str)})
    elif request.form:
        src.update(request.form.to_dict())
    if any(src.get(k) and not ID_RE.match(src[k]) for k in ("id", "folder", "to_folder")):
        return True
    accts = [src[k] for k in ("account", "to_account", "email") if src.get(k)]
    return bool(accts) and any(x not in load() for x in accts)


@app.before_request
def guard():
    if request.method in WRITES:  # browsers always send Origin on cross-site writes
        origin = request.headers.get("Origin")
        if origin and origin.rstrip("/") != request.host_url.rstrip("/"):
            return jsonify(error="Cross-site request blocked."), 403
    if request.endpoint in ("login", "logout"):
        return None
    if not session.get("user"):
        if request.path.startswith("/api/"):
            return jsonify(error="Please sign in."), 401
        return redirect("/login")
    if request.method in WRITES and not hmac.compare_digest(
            request.headers.get("X-CSRF-Token", ""), session.get("csrf", "")):
        return jsonify(error="Your session token is missing or stale. Reload the page."), 403
    if bad_input():
        return jsonify(error="Invalid request."), 400
    return None


@app.after_request
def secure_headers(resp):
    h = resp.headers
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("X-Frame-Options", "SAMEORIGIN")
    h.setdefault("Referrer-Policy", "same-origin")
    h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
    h.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    n = getattr(g, "nonce", None)
    if resp.mimetype == "text/html" and n:  # our own pages: no inline script without this request's nonce
        h["Content-Security-Policy"] = (
            f"default-src 'none'; script-src 'nonce-{n}'; style-src 'self' 'nonce-{n}' https://fonts.googleapis.com; "
            "style-src-attr 'unsafe-inline'; font-src https://fonts.gstatic.com; "
            "img-src 'self' data: https://*.googleusercontent.com; media-src 'self'; frame-src 'self'; "
            "connect-src 'self'; form-action 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'")
        h["X-Frame-Options"] = "DENY"
        h["Cache-Control"] = "no-store"
    elif request.path.startswith("/api/") and "Cache-Control" not in h:
        h["Cache-Control"] = "no-store"
    if SECURE:
        h["Strict-Transport-Security"] = "max-age=31536000"
    return resp


@app.after_request
def compress(resp):
    """gzip pages and JSON lists (roughly 5x smaller). Streams, ranges and ready-made files are left alone."""
    if (resp.status_code == 200 and not resp.is_streamed and not resp.direct_passthrough
            and "Content-Encoding" not in resp.headers and "gzip" in request.headers.get("Accept-Encoding", "")
            and (resp.mimetype.startswith("text/") or resp.mimetype == "application/json")):
        data = resp.get_data()
        if len(data) > 1024:
            resp.set_data(gzip.compress(data, 5))
            resp.headers["Content-Encoding"] = "gzip"
            resp.headers.add("Vary", "Accept-Encoding")
    return resp


@app.errorhandler(Exception)
def on_error(e):  # never show stack traces, paths or library messages to the browser
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        code, msg = e.code, e.description
    elif isinstance(e, KeyError):
        code, msg = 400, "A required field is missing."
    else:
        app.logger.exception("Unhandled error")
        code, msg = 500, "Something went wrong on the server."
    return (jsonify(error=msg), code) if request.path.startswith("/api/") or request.is_json else (msg, code)


@functools.lru_cache(maxsize=8)
def _template(name, mtime):  # re-read only when the file changes
    return (BASE / name).read_text(encoding="utf-8")


def page(name, **repl):
    """Serve one of our HTML pages with a per-request nonce on its inline script and style."""
    g.nonce = secrets.token_urlsafe(16)
    html = _template(name, (BASE / name).stat().st_mtime_ns)
    html = html.replace("<script>", f'<script nonce="{g.nonce}">').replace("<style>", f'<style nonce="{g.nonce}">')
    for k, v in repl.items():
        html = html.replace(f"__{k}__", v)
    return Response(html, mimetype="text/html")


@app.route("/login", methods=["GET", "POST"])
def login():
    auth = json.loads(AUTH.read_text()) if AUTH.exists() else None
    if request.method == "GET":
        return page("login.html", MODE="signin" if auth else "setup")
    j = request.get_json(silent=True)
    if not isinstance(j, dict):
        return jsonify(error="Invalid request."), 400
    email, pw = str(j.get("email", "")).strip().lower()[:254], str(j.get("password", ""))[:256]
    if not auth:  # first run: create the one admin login
        if not EMAIL_RE.match(email) or len(pw) < 10:
            return jsonify(error="Enter a valid email and a password of at least 10 characters."), 400
        if pw != str(j.get("confirm", "")):
            return jsonify(error="Passwords don't match."), 400
        write_private(AUTH, json.dumps({"email": email, "hash": generate_password_hash(pw)}))
    else:
        now = time.time()
        if now < LOCK["until"]:
            return jsonify(error=f"Too many attempts. Try again in {int(LOCK['until'] - now) + 1} seconds."), 429
        pw_ok = check_password_hash(auth["hash"], pw)  # always hash, so timing doesn't reveal whether the email matched
        if not (hmac.compare_digest(email.encode(), auth["email"].encode()) and pw_ok):
            LOCK["fails"] += 1
            if LOCK["fails"] >= 5:  # 1, 3, 9, then 27 minutes
                lvl = LOCK["level"]
                LOCK.update(fails=0, level=min(lvl + 1, 3), until=now + 60 * 3 ** lvl)
            log("failed sign-in", email)
            return jsonify(error="Incorrect email or password."), 401
        LOCK.update(fails=0, level=0)
    session.clear()  # a fresh session on every sign-in
    session["user"], session["csrf"] = email, secrets.token_urlsafe(32)
    session.permanent = bool(j.get("remember"))
    log("signed in", email)
    return jsonify(ok=True)


@app.post("/logout")
def logout():
    session.clear()
    return "", 204


# ---------- OAuth: link another account ----------
@app.get("/auth/add")
def auth_add():
    from google_auth_oauthlib.flow import Flow  # imported here: it is slow to load and only needed when linking
    flow = Flow.from_client_secrets_file(SECRETS, SCOPES, redirect_uri=REDIRECT)
    url, state = flow.authorization_url(access_type="offline", prompt="consent select_account")
    session["state"], session["cv"] = state, flow.code_verifier
    return redirect(url)


@app.get("/oauth2callback")
def callback():
    from google_auth_oauthlib.flow import Flow
    if request.args.get("error"):  # the user declined on Google's page
        session.pop("state", None)
        return redirect("/")
    state = session.pop("state", None)  # single use; a link someone else made up has no matching state
    if not state or not hmac.compare_digest(request.args.get("state", ""), state):
        return jsonify(error="This link request is invalid or expired. Start again from the Menu."), 400
    flow = Flow.from_client_secrets_file(SECRETS, SCOPES, state=state, redirect_uri=REDIRECT)
    flow.code_verifier = session.pop("cv", None)
    flow.fetch_token(authorization_response=request.url)
    c = flow.credentials
    email = build("drive", "v3", credentials=c, cache_discovery=False).about() \
        .get(fields="user(emailAddress)").execute()["user"]["emailAddress"]
    d = load()
    d[email] = json.loads(c.to_json())
    save(d)
    _CREDS.pop(email, None)  # use the fresh credentials from now on
    log("linked account", email)
    return redirect("/")


# ---------- API ----------
@app.get("/")
def index():
    session.setdefault("csrf", secrets.token_urlsafe(32))
    return page("index.html", CSRF=session["csrf"])


@app.get("/api/accounts")
def accounts():
    return jsonify(quotas())


@app.delete("/api/accounts/<email>")
def unlink(email):
    d = load()
    d.pop(email, None)
    save(d)
    _CREDS.pop(email, None)
    QC.clear()
    return "", 204


def get_entity_icon(mime_type: str, file_name: str) -> str:
    """Pick an emoji for a file from its MIME type or extension."""
    if mime_type == FOLDER:
        return "📁"
    mime, name = mime_type.lower(), file_name.lower()
    if "pdf" in mime or name.endswith(".pdf"):
        return "📕"
    # Spreadsheets and presentations are tested before documents: Office MIME types such as
    # "...officedocument.spreadsheetml.sheet" also contain the word "document".
    if any(k in mime for k in ("spreadsheet", "excel", "csv")) or name.endswith((".xlsx", ".xls", ".csv")):
        return "📊"
    if any(k in mime for k in ("presentation", "powerpoint", "slide")) or name.endswith((".pptx", ".ppt")):
        return "📙"
    if any(k in mime for k in ("document", "word")) or name.endswith((".docx", ".doc", ".md", ".rtf")):
        return "📘"
    if any(k in mime for k in ("image/", "png", "jpeg", "jpg", "gif")):
        return "🖼️"
    if any(k in mime for k in ("audio/", "video/", "mp4", "mp3", "wav")):
        return "🎬"
    if any(k in mime for k in ("zip", "compressed", "x-tar", "gzip")) or name.endswith((".zip", ".rar", ".7z", ".tar.gz")):
        return "📦"
    return "📄"


@app.get("/api/files")
def files():
    q, acct = request.args.get("q"), request.args.get("account")
    folder = request.args.get("folder", "root")
    if q:
        safe = q.replace("\\", "\\\\").replace("'", "\\'")
        if request.args.get("content") == "1":  # also look inside files (Drive indexes Docs, Sheets, PDFs, Office and text files)
            cond = f"trashed=false and (name contains '{safe}' or fullText contains '{safe}')"
        else:
            cond = f"trashed=false and name contains '{safe}'"
    else:
        cond = f"trashed=false and '{folder}' in parents"
    if request.args.get("trash"):
        cond = "trashed=true"
    if acct and not q and not request.args.get("trash"):
        log("opened folder", acct, request.args.get("name", folder))
    def fetch(email):
        key = ("ls", email, cond)
        hit = QC.get(key)
        if hit and time.time() - hit[0] < LIST_TTL:
            return hit[1]
        items, tok = [], None
        while True:  # follow every page, not just the first 200
            res = drive(email).files().list(
                q=cond, pageSize=1000, pageToken=tok,
                fields="nextPageToken,files(id,name,mimeType,size,modifiedTime,thumbnailLink,webViewLink)").execute()
            for f in res["files"]:
                if f.get("thumbnailLink"):
                    remember_thumb((email, f["id"]), f.pop("thumbnailLink"))
                    f["thumb"] = True
                items.append({**f, "account": email, "icon": get_entity_icon(f["mimeType"], f["name"])})
            tok = res.get("nextPageToken")
            if not tok:
                if len(QC) > 300:  # keep the cache small
                    for k in [k for k in QC if isinstance(k, tuple)][:100]:
                        QC.pop(k, None)
                QC[key] = (time.time(), items)
                return items

    out, errors = [], []
    emails = [acct] if acct else list(load())
    with ThreadPoolExecutor(max_workers=8) as ex:
        for email, fut in [(e, ex.submit(fetch, e)) for e in emails]:
            try:
                out += fut.result()
            except Exception:
                errors.append(email)  # the page tells the user which account to re-link
    out.sort(key=lambda f: (f["mimeType"] != FOLDER, f["name"].lower()))
    return jsonify(files=out, errors=errors)


@app.post("/api/upload")
def upload():
    f = request.files["file"]
    f.stream.seek(0, 2)
    size = f.stream.tell()
    f.stream.seek(0)
    acct = request.form.get("account") or best(size)  # inside a folder -> that folder's account
    if not acct:
        return jsonify(error="No linked account has enough free space."), 507
    media = MediaIoBaseUpload(f.stream, f.mimetype or "application/octet-stream",
                              chunksize=8 * 1024 * 1024, resumable=True)
    fname = re.sub(r"[\x00-\x1f\x7f/\\]", "_", f.filename or "").strip()[:255] or "untitled"
    body = {"name": fname, "parents": [request.form.get("folder") or "root"]}
    r = drive(acct).files().create(body=body, media_body=media, fields="id").execute()
    QC.clear()
    log("uploaded", acct, fname)
    return jsonify(account=acct, id=r["id"])


VMETA = {}  # (account, id) -> (name, mimeType); lets a video's many Range requests skip the metadata call
INLINE = re.compile(r"^(image/|video/|audio/)|^application/pdf$")


def view_meta(acct, fid):
    if (acct, fid) not in VMETA:
        m = drive(acct).files().get(fileId=fid, fields="name,mimeType").execute()
        if len(VMETA) > 500:
            VMETA.clear()
        VMETA[(acct, fid)] = (m["name"], m["mimeType"])
    return VMETA[(acct, fid)]


@app.get("/api/view")
def view():
    """Stream a file inline for the viewers, with HTTP Range support so video can seek."""
    acct, fid = request.args["account"], request.args["id"]
    name, mime = view_meta(acct, fid)
    base = f"https://www.googleapis.com/drive/v3/files/{fid}"
    headers = {"Accept-Encoding": "identity"}
    if mime.startswith("application/vnd.google-apps"):  # Docs/Sheets/Slides: preview as PDF
        url, mime = f"{base}/export?mimeType=application/pdf", "application/pdf"
    elif INLINE.match(mime):  # only types that are safe to show inline
        url = base + "?alt=media"
        rng = request.headers.get("Range", "")
        if RANGE_RE.match(rng):  # a plain byte range only
            headers["Range"] = rng
    else:
        return jsonify(error="This file type can't be previewed."), 415
    r = session_for(acct).get(url, headers=headers, stream=True)
    if r.status_code >= 400:
        return jsonify(error="Google couldn't provide this file."), (r.status_code if r.status_code in (403, 404, 416) else 502)
    if not request.headers.get("Range") or request.headers["Range"].startswith("bytes=0-"):
        log("viewed", acct, name)
    out = {"Content-Type": mime, "Accept-Ranges": "bytes", "X-Content-Type-Options": "nosniff",
           "Cache-Control": "private, max-age=300"}
    if mime != "application/pdf":
        out["Content-Security-Policy"] = "sandbox"  # an SVG opened directly can't run scripts
    for h in ("Content-Length", "Content-Range"):
        if h in r.headers:
            out[h] = r.headers[h]
    return Response(stream_with_context(r.iter_content(1 << 18)), status=r.status_code, headers=out)


@app.get("/api/download")
def download():
    acct, fid = request.args["account"], request.args["id"]
    meta = drive(acct).files().get(fileId=fid, fields="name,mimeType").execute()
    log("downloaded", acct, meta["name"])
    base = f"https://www.googleapis.com/drive/v3/files/{fid}"
    if meta["mimeType"].startswith("application/vnd.google-apps"):  # Docs/Sheets -> PDF
        url, name, mime = base + "/export?mimeType=application/pdf", meta["name"] + ".pdf", "application/pdf"
    else:
        url, name, mime = base + "?alt=media", meta["name"], meta["mimeType"]
    r = session_for(acct).get(url, stream=True)
    return Response(stream_with_context(r.iter_content(1 << 18)), mimetype=mime,
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})


NEW = {"folder": FOLDER, "doc": "application/vnd.google-apps.document",
       "sheet": "application/vnd.google-apps.spreadsheet",
       "slide": "application/vnd.google-apps.presentation", "text": "text/plain"}


@app.post("/api/new")
def new():
    j = request.json
    if j.get("type") not in NEW or not str(j.get("name", "")).strip() or len(j["name"]) > 255:
        return jsonify(error="Choose a type and a name of up to 255 characters."), 400
    acct = j.get("account") or best()
    if not acct:
        return jsonify(error="No linked account available."), 507
    name = j["name"] + (".txt" if j["type"] == "text" and "." not in j["name"] else "")
    body = {"name": name, "mimeType": NEW[j["type"]], "parents": [j.get("folder") or "root"]}
    r = drive(acct).files().create(body=body, fields="id,webViewLink").execute()
    QC.clear()
    log(f"created {j['type']}", acct, name)
    return jsonify(account=acct, **r)


@app.delete("/api/files")
def trash():
    drive(request.args["account"]).files().update(fileId=request.args["id"], body={"trashed": True}).execute()
    QC.clear()
    log("moved to trash", request.args["account"], request.args.get("name", ""))
    return "", 204


@app.get("/api/thumb")
def thumb():  # proxied so the browser doesn't need to be signed in to each Google account
    acct = request.args["account"]
    link = THUMBS.get((acct, request.args["id"]))
    if not link:
        return "", 404
    size = request.args.get("s", "")
    px = min(800, max(64, int(size))) if size.isdigit() else 400  # ask Google for the size we actually show
    r = session_for(acct).get(re.sub(r"=s\d+$", f"=s{px}", link))
    ctype = r.headers.get("Content-Type", "")
    if r.status_code != 200 or not ctype.startswith("image/"):
        return "", 404
    return Response(r.content, mimetype=ctype, headers={"Cache-Control": "private, max-age=86400"})


@app.route("/api/content", methods=["GET", "PUT"])
def content():  # in-app editing of plain-text files
    acct, fid = request.args["account"], request.args["id"]
    d = drive(acct)
    meta = d.files().get(fileId=fid, fields="name,mimeType,size").execute()
    if not TEXTY.match(meta["mimeType"]):
        return jsonify(error="Only text files can be edited here."), 415
    if request.method == "GET":
        if int(meta.get("size", 0)) > 2_000_000:
            return jsonify(error="Files over 2 MB can't be edited here."), 413
        log("opened for editing", acct, meta["name"])
        return jsonify(text=d.files().get_media(fileId=fid).execute().decode("utf-8", "replace"))
    text = (request.get_json(silent=True) or {}).get("text")
    if not isinstance(text, str) or len(text) > 5_000_000:
        return jsonify(error="The text is missing or larger than 5 MB."), 400
    media = MediaIoBaseUpload(BytesIO(text.encode()), meta["mimeType"])
    d.files().update(fileId=fid, media_body=media).execute()
    QC.clear()
    log("edited", acct, meta["name"])
    return "", 204


@app.post("/api/share")
def share():
    j = request.json
    if j.get("role") not in ("reader", "commenter", "writer"):
        return jsonify(error="Unknown access level."), 400
    if not EMAIL_RE.match(str(j.get("email", ""))):
        return jsonify(error="Enter a valid email address."), 400
    perm = {"type": "user", "role": j["role"], "emailAddress": j["email"]}
    drive(j["account"]).permissions().create(fileId=j["id"], body=perm, sendNotificationEmail=True).execute()
    log(f"shared ({j['role']}) with {j['email']}", j["account"], j.get("name", ""))
    return "", 204


def tail_lines(path, n, block=262144):
    """The last n lines of a file, without reading all of it."""
    try:
        with open(path, "rb") as f:
            size = f.seek(0, 2)
            f.seek(max(0, size - block))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except FileNotFoundError:
        return []
    return (lines[1:] if size > block else lines)[-n:]  # the first line may be cut in half


@app.get("/api/history")
def history():
    rows = tail_lines(HISTORY, 300)
    return jsonify([json.loads(r) for r in reversed(rows)])


@app.post("/api/restore")
def restore():
    j = request.json
    drive(j["account"]).files().update(fileId=j["id"], body={"trashed": False}).execute()
    QC.clear()
    log("restored from trash", j["account"], j.get("name", ""))
    return "", 204


@app.delete("/api/purge")
def purge():
    acct = request.args["account"]
    drive(acct).files().delete(fileId=request.args["id"]).execute()
    QC.clear()
    log("deleted forever", acct, request.args.get("name", ""))
    return "", 204


@app.post("/api/emptytrash")
def emptytrash():
    failed = []
    for email in load():
        try:
            drive(email).files().emptyTrash().execute()
        except Exception:
            failed.append(email)
    QC.clear()
    log("emptied trash", "all accounts")
    return jsonify(failed=failed)


EXPORT = {
    "application/vnd.google-apps.document": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".docx"),
    "application/vnd.google-apps.spreadsheet": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"),
    "application/vnd.google-apps.presentation": ("application/vnd.openxmlformats-officedocument.presentationml.presentation", ".pptx"),
}


@app.post("/api/move")
def move():
    j = request.json
    src, fid, dst = j["account"], j["id"], j["to_account"]
    folder = j.get("to_folder") or None
    d = drive(src)
    meta = d.files().get(fileId=fid, fields="name,mimeType,size,parents").execute()
    if src == dst:  # same Drive: just change the parent, nothing is copied
        if not folder:
            folder = d.files().get(fileId="root", fields="id").execute()["id"]
        d.files().update(fileId=fid, addParents=folder, removeParents=",".join(meta.get("parents", [])),
                         fields="id").execute()
    else:
        mime, name = meta["mimeType"], meta["name"]
        if mime == FOLDER:
            return jsonify(error="Folders can only be moved within the same account."), 400
        base = f"https://www.googleapis.com/drive/v3/files/{fid}"
        if mime in EXPORT:  # Docs/Sheets/Slides can't be copied as-is: convert to Office files
            mime, ext = EXPORT[mime]
            url, name = f"{base}/export?mimeType={quote(mime)}", name + ext
        elif mime.startswith("application/vnd.google-apps"):
            return jsonify(error="This Google file type can't be copied to another account."), 400
        else:
            url = base + "?alt=media"
        q = quota(dst)
        if "error" in q or free(q) < int(meta.get("size", 0)):
            return jsonify(error="The destination account doesn't have enough free space."), 507
        r = session_for(src).get(url, stream=True)
        if r.status_code != 200:
            return jsonify(error="Couldn't read the original file."), 502
        with tempfile.TemporaryFile() as tmp:  # spool to disk so big files don't fill memory
            for chunk in r.iter_content(1 << 20):
                tmp.write(chunk)
            tmp.seek(0)
            media = MediaIoBaseUpload(tmp, mime, chunksize=8 * 1024 * 1024, resumable=True)
            drive(dst).files().create(body={"name": name, "parents": [folder or "root"]},
                                      media_body=media, fields="id").execute()
        d.files().update(fileId=fid, body={"trashed": True}).execute()  # original goes to Trash only after the copy worked
    QC.clear()
    log(f"moved to {dst}", src, j.get("name", ""))
    return "", 204


@app.get("/api/nodes")
def nodes():  # instant: account names only, no Google calls
    return jsonify([{"email": e} for e in load()])


@app.get("/api/node")
def node():  # one account's storage, fetched on its own so a slow account can't hold up the rest
    email = request.args.get("email", "")
    if email not in load():
        return jsonify(error="Unknown account."), 404
    return jsonify(quota(email))


def tighten():
    """Make files from older versions private too (no effect on Windows)."""
    for p in (STORE, AUTH, HISTORY, KEYFILE, DATA / "token.key"):
        try:
            if p.exists():
                os.chmod(p, 0o600)
        except OSError:
            pass


if __name__ == "__main__":
    tighten()
    if STORE.exists():
        save(load())  # encrypts a plain-text accounts.json left by an older version
    if HOST not in ("127.0.0.1", "localhost") and not AUTH.exists():
        print("WARNING: no admin login exists yet and the port is reachable beyond this computer. Create it first.")
    try:
        from waitress import serve  # sturdier than Flask's development server
        serve(app, host=HOST, port=PORT, threads=16, max_request_body_size=5 * 1024 ** 3)
    except ImportError:
        app.run(host=HOST, port=PORT)  # localhost only
