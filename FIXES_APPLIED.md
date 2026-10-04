# Code Review Fixes Applied

## Critical Issues Fixed

### 1. Missing API Endpoints
**Issue**: Frontend calls `/api/nodes` and `/api/node` but endpoints were not defined.
**Fix**: Added both endpoints:
- `/api/nodes`: Returns list of linked accounts (instant, no Google calls)
- `/api/node?email=...`: Returns quota info for specific account (fetched independently)

### 2. Query Escaping Vulnerability
**Issue**: Search query escaping used weak `replace()` instead of proper Google Drive API escaping.
**Before**:
```python
safe = q.replace("\\", "\\\\").replace("'", "\\'")
cond = f"trashed=false and name contains '{safe}'"
```
**After**:
```python
safe = q.replace('"', '\\"')  # Proper Google Drive escaping
cond = f'trashed=false and name contains "{safe}"'
```

### 3. Incomplete `/api/move` Endpoint
**Issue**: Function was cut off mid-implementation; missing validation.
**Fixes**:
- Added validation that source and destination accounts exist
- Added folder existence validation before cross-account move
- Proper error handling for edge cases

### 4. Missing Error Logging in File Listing
**Issue**: Thread exceptions silently caught with no logging.
**Fix**: Added detailed logging in both `fetch()` and exception handlers:
```python
except Exception as e:
    logging.error(f"Failed to list files for {email}: {e}")
    errors.append(email)
```

### 5. Unsafe Secret Key Initialization
**Issue**: Empty `secret.key` file would disable session security silently.
**Fix**: Added validation and explicit error:
```python
key = KEYFILE.read_text().strip()
if not key:
    raise RuntimeError("secret.key is empty; delete it and restart")
app.secret_key = key
```

---

## High-Priority Fixes

### 6. Unescaped Filenames in Logging
**Issue**: Filenames with newlines could corrupt JSON Lines format in `history.jsonl`.
**Fix**: Changed logging to use `json.dumps()` with `default=str` to safely serialize any object:
```python
h.write(json.dumps({...}, default=str) + "\n")
```

### 7. Email Validation in `/api/share`
**Issue**: No validation on email input before sending to Google API.
**Fix**: Added email validation:
```python
email = j.get("email", "").strip()
if "@" not in email or " " in email or len(email) < 5:
    return jsonify(error="Please provide a valid email address."), 400
```

### 8. Missing Folder Validation in `/api/move`
**Issue**: Could move files to non-existent folders.
**Fix**: Added folder existence check:
```python
if folder:
    try:
        drive(dst).files().get(fileId=folder, fields="id").execute()
    except Exception:
        return jsonify(error="Destination folder not found."), 404
```

### 9. No CSRF Protection
**Issue**: Raw `fetch()` POSTs in frontend bypass Flask's default CSRF protection.
**Fixes**:
- Added CSRF token generation in session
- Added validation before POST endpoints
- Added helper functions `generate_csrf_token()` and `validate_csrf_token()`
- Applied to `/api/upload` with example for other POST endpoints

### 10. Hardcoded File Size Limit
**Issue**: `2_000_000` byte limit was a magic number.
**Fix**: Extracted to configurable constant:
```python
MAX_EDIT_SIZE = 2_000_000  # 2 MB limit for in-app text editing
# Used in endpoint:
if int(meta.get("size", 0)) > MAX_EDIT_SIZE:
    return jsonify(error=f"Files over {MAX_EDIT_SIZE // 1_000_000} MB can't be edited here."), 413
```

---

## Medium-Priority Improvements

### 11. Updated requirements.txt
**Added missing dependencies**:
- `cryptography>=41.0.0` (used by Fernet)
- `werkzeug>=3.0` (used for password hashing)
- `google-auth-httplib2>=0.2.0` (dependency)
- `waitress>=2.1.0` (production server)

### 12. Logging Setup
**Added**: Proper logging configuration with both file and console output:
```python
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(DATA / "merger.log"),
        logging.StreamHandler()
    ]
)
```
Logs written to `<DATA>/merger.log` for production debugging.

---

## Low-Priority Notes

### Remaining Considerations (Not Critical)
1. **Thread Pool Executor**: Currently creates new executor per request. Could optimize with a global reusable pool for high-traffic scenarios.
2. **Thumbnail Regex**: `re.sub(r"=s\d+$", "=s400", link)` assumes Google URL format. Monitor for API changes.
3. **Quota Cache Timeout**: 60-second cache may be too short for UIs. Consider adjusting based on usage patterns.

---

## Testing Checklist

- ✅ Python syntax valid (no compilation errors)
- ✅ All missing endpoints implemented
- ✅ Proper error logging added
- ✅ Input validation strengthened
- ✅ CSRF protection framework in place
- ✅ Secret key validation enforced
- ✅ Filename escaping fixed
- ⚠️ Needs runtime testing with Google Drive API
- ⚠️ Needs frontend CSRF token integration in index.html

---

## Frontend Integration Needed

The frontend (`index.html`) should send CSRF tokens with POST requests. Update the API calls to include:
```javascript
headers: {
    'Content-Type': 'application/json',
    'X-CSRF-Token': document.querySelector('input[name="csrf_token"]')?.value || ''
}
```

This is left as a follow-up task as it requires frontend changes outside the scope of Python backend fixes.
