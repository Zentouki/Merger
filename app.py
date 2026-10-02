"""Merger: link several Google Drive accounts and use them as one storage.
Run locally: python app.py  ->  http://localhost:5000
"""
import json, os, re, secrets, tempfile, threading, time
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from urllib.parse import quote

from flask import (Flask, Response, jsonify, redirect, request, send_from_directory,
                   session, stream_with_context)
from cryptography.fernet import Fernet
from werkzeug.security import check_password_hash, generate_password_hash
from google.auth.transport.requests import AuthorizedSession, Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")  # http is fine on localhost only
BASE = Path(__file__).parent
STORE = BASE / "accounts.json"          # linked accounts + refresh tokens (keep private)
SECRETS = BASE / "client_secret.json"   # OAuth client downloaded from Google Cloud
SCOPES = ["https://www.googleapis.com/auth/drive"]
REDIRECT = "http://localhost:5000/oauth2callback"
FOLDER = "application/vnd.google-apps.folder"
HISTORY = BASE / "history.jsonl"        # activity log of everything done through this app
THUMBS = {}                             # (account, file id) -> Drive thumbnail URL


def log(action, acct, name=""):
    with HISTORY.open("a") as h:
        h.write(json.dumps({"t": time.strftime("%Y-%m-%d %H:%M:%S"), "action": action,
                            "account": acct, "name": name}) + "\n")

app = Flask(__name__)
KEYFILE = BASE / "secret.key"          # keeps you signed in across restarts
if not KEYFILE.exists():
    KEYFILE.write_text(secrets.token_hex(32))
app.secret_key = KEYFILE.read_text()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")


# ---------- account + credential helpers ----------
_FER = []


def fernet():
    if not _FER:
        try:  # key lives in the OS keychain when one is available
            import keyring
            k = keyring.get_password("merger", "tokens")
            if not k:
                k = Fernet.generate_key().decode()
                keyring.set_password("merger", "tokens", k)
        except Exception:  # otherwise fall back to a key file (weaker: it sits next to the data)
            kf = BASE / "token.key"
            if not kf.exists():
                kf.write_text(Fernet.generate_key().decode())
            k = kf.read_text()
        _FER.append(Fernet(k.encode()))
    return _FER[0]


def load():
    if not STORE.exists():
        return {}
    raw = STORE.read_text().strip()
    return json.loads(raw) if raw.startswith("{") else json.loads(fernet().decrypt(raw.encode()))


def save(d):
    STORE.write_text(fernet().encrypt(json.dumps(d).encode()).decode())


CRED_LOCK = threading.Lock()


def creds(email):
    with CRED_LOCK:  # listing runs in parallel; keep token refreshes from racing
        d = load()
        c = Credentials.from_authorized_user_info(d[email], SCOPES)
        if not c.valid:  # expired or missing access token -> refresh and persist
            c.refresh(Request())
            d[email] = json.loads(c.to_json())
            save(d)
        return c


def drive(email):
    return build("drive", "v3", credentials=creds(email), cache_discovery=False)


def _quota(email):
    try:
        q = drive(email).about().get(fields="storageQuota").execute()["storageQuota"]
        return {"email": email, "used": int(q["usage"]), "limit": int(q["limit"]) if q.get("limit") else None}
    except Exception as e:  # revoked token, network, etc.
        return {"email": email, "error": str(e)}


QC = {}  # email -> (timestamp, quota); cleared whenever we change a Drive


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


# ---------- Merger login ----------
AUTH = BASE / "auth.json"               # admin email + password hash (keep private)
LOCK = {"fails": 0, "until": 0.0}


@app.before_request
def guard():
    if request.endpoint in ("login", "logout", "static") or session.get("user"):
        return None
    if request.path.startswith("/api/"):
        return jsonify(error="Please sign in."), 401
    return redirect("/login")


@app.route("/login", methods=["GET", "POST"])
def login():
    auth = json.loads(AUTH.read_text()) if AUTH.exists() else None
    if request.method == "GET":
        page = (BASE / "login.html").read_text(encoding="utf-8")
        return Response(page.replace("__MODE__", "signin" if auth else "setup"), mimetype="text/html")
    j = request.get_json(force=True)
    email, pw = j.get("email", "").strip().lower(), j.get("password", "")
    if not auth:  # first run: create the one admin login
        if "@" not in email or len(pw) < 8:
            return jsonify(error="Enter a valid email and a password of at least 8 characters."), 400
        if pw != j.get("confirm"):
            return jsonify(error="Passwords don't match."), 400
        AUTH.write_text(json.dumps({"email": email, "hash": generate_password_hash(pw)}))
    else:
        if time.time() < LOCK["until"]:
            return jsonify(error="Too many attempts. Try again in a minute."), 429
        if email != auth["email"] or not check_password_hash(auth["hash"], pw):
            LOCK["fails"] += 1
            if LOCK["fails"] >= 5:
                LOCK.update(fails=0, until=time.time() + 60)
            log("failed sign-in", email)
            return jsonify(error="Incorrect email or password."), 401
        LOCK["fails"] = 0
    session.clear()
    session["user"] = email
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
    flow = Flow.from_client_secrets_file(SECRETS, SCOPES, redirect_uri=REDIRECT)
    url, state = flow.authorization_url(access_type="offline", prompt="consent select_account")
    session["state"], session["cv"] = state, flow.code_verifier
    return redirect(url)


@app.get("/oauth2callback")
def callback():
    flow = Flow.from_client_secrets_file(SECRETS, SCOPES, state=session.get("state"), redirect_uri=REDIRECT)
    flow.code_verifier = session.get("cv")
    flow.fetch_token(authorization_response=request.url)
    c = flow.credentials
    email = build("drive", "v3", credentials=c, cache_discovery=False).about() \
        .get(fields="user(emailAddress)").execute()["user"]["emailAddress"]
    d = load()
    d[email] = json.loads(c.to_json())
    save(d)
    return redirect("/")


# ---------- API ----------
@app.get("/")
def index():
    return send_from_directory(BASE, "index.html")


@app.get("/api/accounts")
def accounts():
    return jsonify(quotas())


@app.delete("/api/accounts/<email>")
def unlink(email):
    d = load()
    d.pop(email, None)
    save(d)
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
        cond = f"trashed=false and name contains '{safe}'"
    else:
        cond = f"trashed=false and '{folder}' in parents"
    if request.args.get("trash"):
        cond = "trashed=true"
    if acct and not q and not request.args.get("trash"):
        log("opened folder", acct, request.args.get("name", folder))
    def fetch(email):
        items, tok = [], None
        while True:  # follow every page, not just the first 200
            res = drive(email).files().list(
                q=cond, pageSize=1000, pageToken=tok,
                fields="nextPageToken,files(id,name,mimeType,size,modifiedTime,thumbnailLink,webViewLink)").execute()
            for f in res["files"]:
                if f.get("thumbnailLink"):
                    THUMBS[(email, f["id"])] = f.pop("thumbnailLink")
                    f["thumb"] = True
                items.append({**f, "account": email, "icon": get_entity_icon(f["mimeType"], f["name"])})
            tok = res.get("nextPageToken")
            if not tok:
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
    body = {"name": f.filename, "parents": [request.form.get("folder") or "root"]}
    r = drive(acct).files().create(body=body, media_body=media, fields="id").execute()
    QC.clear()
    log("uploaded", acct, f.filename)
    return jsonify(account=acct, id=r["id"])


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
    r = AuthorizedSession(creds(acct)).get(url, stream=True)
    return Response(stream_with_context(r.iter_content(1 << 16)), mimetype=mime,
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})


NEW = {"folder": FOLDER, "doc": "application/vnd.google-apps.document",
       "sheet": "application/vnd.google-apps.spreadsheet",
       "slide": "application/vnd.google-apps.presentation", "text": "text/plain"}


@app.post("/api/new")
def new():
    j = request.json
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
    r = AuthorizedSession(creds(acct)).get(re.sub(r"=s\d+$", "=s400", link))
    return Response(r.content, status=r.status_code, mimetype=r.headers.get("Content-Type", "image/jpeg"),
                    headers={"Cache-Control": "private, max-age=1800"})


@app.route("/api/content", methods=["GET", "PUT"])
def content():  # in-app editing of plain-text files
    acct, fid = request.args["account"], request.args["id"]
    d = drive(acct)
    meta = d.files().get(fileId=fid, fields="name,mimeType,size").execute()
    if request.method == "GET":
        if int(meta.get("size", 0)) > 2_000_000:
            return jsonify(error="Files over 2 MB can't be edited here."), 413
        log("opened for editing", acct, meta["name"])
        return jsonify(text=d.files().get_media(fileId=fid).execute().decode("utf-8", "replace"))
    media = MediaIoBaseUpload(BytesIO(request.json["text"].encode()), meta["mimeType"])
    d.files().update(fileId=fid, media_body=media).execute()
    QC.clear()
    log("edited", acct, meta["name"])
    return "", 204


@app.post("/api/share")
def share():
    j = request.json
    if j["role"] not in ("reader", "commenter", "writer"):
        return jsonify(error="Unknown access level."), 400
    perm = {"type": "user", "role": j["role"], "emailAddress": j["email"]}
    drive(j["account"]).permissions().create(fileId=j["id"], body=perm, sendNotificationEmail=True).execute()
    log(f"shared ({j['role']}) with {j['email']}", j["account"], j.get("name", ""))
    return "", 204


@app.get("/api/history")
def history():
    rows = HISTORY.read_text().splitlines()[-300:] if HISTORY.exists() else []
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
        r = AuthorizedSession(creds(src)).get(url, stream=True)
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


if __name__ == "__main__":
    if STORE.exists():
        save(load())  # encrypts a plain-text accounts.json left by an older version
    try:
        from waitress import serve  # sturdier than Flask's development server
        serve(app, host="127.0.0.1", port=5000, threads=8, max_request_body_size=5 * 1024 ** 3)
    except ImportError:
        app.run(host="127.0.0.1", port=5000)  # localhost only
