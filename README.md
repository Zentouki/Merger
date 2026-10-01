# Merger

Link several Google Drive accounts and use them as one storage pool.

- One combined storage bar, with a segment per account
- One merged file list (root folders of every account side by side), plus cross-account search
- Uploads go to the account with the most free space that fits the file; inside a folder they stay in that folder's account
- Download (Docs/Sheets export as PDF), new folder, delete (moves to Drive trash), unlink

## Setup

1. In [Google Cloud Console](https://console.cloud.google.com/), create a project and enable the **Google Drive API**.
2. **OAuth consent screen**: choose External, then add every Google account you plan to link as a **test user**.
3. **Credentials → Create credentials → OAuth client ID → Web application**.
   Add the redirect URI `http://localhost:5000/oauth2callback`.
4. Download the JSON and save it as `client_secret.json` next to `app.py`.
5. Install and run:

```bash
pip install -r requirements.txt
python app.py
```

Open http://localhost:5000 and click **Link a Google account** once per account.

## Good to know

- `accounts.json` holds refresh tokens in plain text. Keep it private and out of git.
- While the consent screen is in **Testing**, Google expires refresh tokens after 7 days; accounts then show "needs re-linking". Publishing the app (it can stay unverified for personal use) removes that limit.
- Each file lives whole in one account. Files larger than any single account's free space can't be stored, and splitting files across accounts isn't implemented.
- The server binds to `127.0.0.1` and has no login of its own. Don't expose it to a network without adding authentication.

## Login

The first time you open Merger it asks you to create an email and password. After that you sign in with them. Your sign-ins and failed attempts appear in **Activity**.

Forgot the password? Stop the app, delete `auth.json`, and start it again to set a new one. `auth.json` and `secret.key` are created next to `app.py`; keep them private.
