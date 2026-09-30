"""Pooled Drive: link several Google Drive accounts and use them as one storage.
Run locally: python app.py  ->  http://localhost:5000
"""
import json, os, secrets
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
    out = []
    for email in [acct] if acct else load():  # merge every account into one listing
        try:
            res = drive(email).files().list(q=cond, pageSize=200,
                                            fields="files(id,name,mimeType,size,modifiedTime)").execute()
            out += [{**f, "account": email} for f in res["files"]]
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
    return jsonify(account=acct, id=r["id"])


@app.get("/api/download")
def download():
    acct, fid = request.args["account"], request.args["id"]
    meta = drive(acct).files().get(fileId=fid, fields="name,mimeType").execute()
    base = f"https://www.googleapis.com/drive/v3/files/{fid}"
    if meta["mimeType"].startswith("application/vnd.google-apps"):  # Docs/Sheets -> PDF
        url, name, mime = base + "/export?mimeType=application/pdf", meta["name"] + ".pdf", "application/pdf"
    else:
        url, name, mime = base + "?alt=media", meta["name"], meta["mimeType"]
    r = AuthorizedSession(creds(acct)).get(url, stream=True)
    return Response(stream_with_context(r.iter_content(1 << 16)), mimetype=mime,
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})


@app.post("/api/folder")
def mkdir():
    j = request.json
    acct = j.get("account") or best()
    body = {"name": j["name"], "mimeType": FOLDER, "parents": [j.get("folder") or "root"]}
    return jsonify(id=drive(acct).files().create(body=body, fields="id").execute()["id"])


@app.delete("/api/files")
def trash():
    drive(request.args["account"]).files().update(fileId=request.args["id"], body={"trashed": True}).execute()
    return "", 204


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000)  # localhost only: this app has no login of its own
