"""Pooled Drive: link several Google Drive accounts and use them as one storage.
Run locally: python app.py  ->  http://localhost:5000
"""
import json, os, re, secrets, time
from io import BytesIO
from pathlib import Path
from urllib.parse import quote

from flask import (Flask, Response, jsonify, redirect, request, send_from_directory,
                   session, stream_with_context)
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
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(16)


# ---------- account + credential helpers ----------
def load():
    return json.loads(STORE.read_text()) if STORE.exists() else {}


def save(d):
    STORE.write_text(json.dumps(d, indent=2))


def creds(email):
    d = load()
    c = Credentials.from_authorized_user_info(d[email], SCOPES)
    if not c.valid:  # expired or missing access token -> refresh and persist
        c.refresh(Request())
        d[email] = json.loads(c.to_json())
        save(d)
    return c


def drive(email):
    return build("drive", "v3", credentials=creds(email), cache_discovery=False)


def quota(email):
    try:
        q = drive(email).about().get(fields="storageQuota").execute()["storageQuota"]
        return {"email": email, "used": int(q["usage"]), "limit": int(q["limit"]) if q.get("limit") else None}
    except Exception as e:  # revoked token, network, etc.
        return {"email": email, "error": str(e)}


def free(q):
    return float("inf") if q["limit"] is None else q["limit"] - q["used"]


def best(size=0):
    """Account with the most free space that can still fit `size` bytes."""
    ok = [q for q in map(quota, load()) if "error" not in q and free(q) >= size]
    return max(ok, key=free)["email"] if ok else None


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
    return jsonify([quota(e) for e in load()])


@app.delete("/api/accounts/<email>")
def unlink(email):
    d = load()
    d.pop(email, None)
    save(d)
    return "", 204


@app.get("/api/files")
def files():
    q, acct = request.args.get("q"), request.args.get("account")
    folder = request.args.get("folder", "root")
    if q:
        safe = q.replace("\\", "\\\\").replace("'", "\\'")
        cond = f"trashed=false and name contains '{safe}'"
    else:
        cond = f"trashed=false and '{folder}' in parents"
    if acct and not q:
        log("opened folder", acct, request.args.get("name", folder))
    out = []
    for email in [acct] if acct else load():  # merge every account into one listing
        try:
            res = drive(email).files().list(q=cond, pageSize=200,
                                            fields="files(id,name,mimeType,size,modifiedTime,thumbnailLink,webViewLink)").execute()
            for f in res["files"]:
                if f.get("thumbnailLink"):
                    THUMBS[(email, f["id"])] = f.pop("thumbnailLink")
                    f["thumb"] = True
                out.append({**f, "account": email})
        except Exception:
            pass  # a broken account shows up as "needs re-link" in /api/accounts
    out.sort(key=lambda f: (f["mimeType"] != FOLDER, f["name"].lower()))
    return jsonify(out)


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
    log(f"created {j['type']}", acct, name)
    return jsonify(account=acct, **r)


@app.delete("/api/files")
def trash():
    drive(request.args["account"]).files().update(fileId=request.args["id"], body={"trashed": True}).execute()
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


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000)  # localhost only: this app has no login of its own
