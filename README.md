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

## Security and speed notes

- `accounts.json` is now encrypted. The key is kept in your operating system keychain (via `keyring`) or, if none exists, in `token.key`. Lose the key and you simply re-link your accounts. An older plain-text `accounts.json` is encrypted automatically on the next start.
- Lists and storage checks run across all accounts at the same time, and storage numbers are cached for a minute.
- Folders with more than 200 items now load fully, and an account that can't be read shows a warning instead of silently disappearing.
- If `waitress` is installed (it is in `requirements.txt`), Merger uses it instead of Flask's development server.

## Trash and moving files

- **Trash** (sidebar) lists trashed files from every account. You can restore a file, delete it forever, or empty the trash.
- **Move** works within one account by changing its folder. To another account, Merger copies the file, then puts the original in Trash. Docs, Sheets and Slides are converted to Word, Excel and PowerPoint. Folders can only move within one account.

## Icon

`icon.svg`, `icon-512.png` and `favicon.ico` are the app icon. A copy is already embedded in both pages as the browser tab icon.

## Viewers

Click a file (or choose **Open**) to view it without leaving Merger.

- **Pictures:** full-screen viewer with zoom (wheel, + and -, double-click), drag to pan, rotate, 1:1 and Fit. Left and right arrows move to the next picture.
- **Video and audio:** custom player with seeking, 10-second skips, speed, volume, picture-in-picture and fullscreen. Keys: space or K play, arrows seek 5 s, J and L seek 10 s, M mute, F fullscreen. It remembers where you stopped. Browsers can't play every format (for example MKV or AVI); you'll be offered a download.
- **Documents:** PDFs and Google Docs, Sheets and Slides preview as PDF. Text files open in an editor with line numbers, word count, wrap, text size, Ctrl+S to save, a Markdown preview and a JSON formatter. Word, Excel and PowerPoint files, and Google files you want to change, open in Google's own editor.

## Security notes

- Every change you make (upload, delete, share, move, edit, link) must carry a per-session token, and cross-site requests are refused. Pages carry a strict Content-Security-Policy, so injected scripts can't run, and the usual hardening headers are set.
- Ids and accounts in requests are checked before they reach Google, and errors never show server details in the browser.
- Linking an account only works from a link request you started yourself.
- Sign-in compares in constant time and locks for 1, 3, 9 and then 27 minutes after repeated failures. The password needs at least 10 characters.
- `accounts.json`, `auth.json`, `secret.key`, `token.key` and the history are written atomically and readable only by you (Linux and macOS; Windows ignores file modes, so keep the folder private). The history rotates at 5 MB.
- Serving Merger over https? Set `MERGER_SECURE_COOKIES=1` so the cookie is only sent securely and browsers are told to insist on https. Plain http is only accepted for a `localhost` redirect address.
- Merger needs full Google Drive access to see your existing files, so treat `accounts.json` like a password. `MERGER_TOKEN_KEY_FILE` (or `MERGER_TOKEN_KEY`) keeps the encryption key out of the data folder.
- Keep libraries current: `pip install pip-audit && pip-audit -r requirements.txt` lists known vulnerabilities.

## Searching file contents

Turn on **Contents** next to the search box to search inside files as well as their names. Google indexes the text of Docs, Sheets, Slides, PDFs, Office files and plain text, so those are found by what they say. Photos, videos and other files are only found by name. It matches whole words, so a part of a word may not match.
