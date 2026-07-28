"""
probus_debug.py — Probus HES API Endpoint Discovery
=====================================================
Run this in the tpnodl_monitor folder:
    python probus_debug.py

It will:
1. Try every likely login method/path/content-type combination
2. Print the raw response for each
3. Once login works, probe the data endpoints
4. Save working config back to data/config.json automatically
"""

import requests, json, urllib3, sys, os
urllib3.disable_warnings()

BASE     = "https://tpnodl.probussense.com"
USERNAME = "1925"
PASSWORD = input("Enter Probus password: ").strip()
ROLE     = "Sense Admin"

SESSION = requests.Session()
SESSION.verify = False
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/125.0.0.0 Safari/537.36",
    "Origin":  BASE,
    "Referer": BASE + "/login",
    "Accept":  "application/json, text/plain, */*",
})

# ─────────────────────────────────────────────────────────────
# STEP 1 — First visit homepage to get any cookies/CSRF tokens
# ─────────────────────────────────────────────────────────────
print("\n" + "="*60)
print("STEP 1 — GET homepage to collect cookies")
print("="*60)
try:
    r = SESSION.get(BASE + "/login", timeout=15, verify=False)
    print(f"  Status : {r.status_code}")
    print(f"  Cookies: {dict(SESSION.cookies)}")
    # Look for CSRF token in response
    csrf = None
    for line in r.text.splitlines():
        if "csrf" in line.lower() or "xsrf" in line.lower() or "_token" in line.lower():
            print(f"  CSRF hint: {line.strip()[:120]}")
except Exception as e:
    print(f"  ERROR: {e}")

# ─────────────────────────────────────────────────────────────
# STEP 2 — Try every known login combination
# ─────────────────────────────────────────────────────────────
print("\n" + "="*60)
print("STEP 2 — Probe all login endpoint combinations")
print("="*60)

LOGIN_PATHS = [
    "/api/login",
    "/api/auth/login",
    "/api/user/login",
    "/api/v1/login",
    "/api/v2/login",
    "/auth/login",
    "/login",
    "/api/signin",
    "/api/authenticate",
    "/api/token",
    "/api/auth/token",
    "/api/user/authenticate",
]

PAYLOADS = [
    # JSON variations
    ("json",  {"username": USERNAME, "password": PASSWORD, "role": ROLE}),
    ("json",  {"username": USERNAME, "password": PASSWORD}),
    ("json",  {"userName": USERNAME, "password": PASSWORD, "role": ROLE}),
    ("json",  {"userId":   USERNAME, "password": PASSWORD}),
    ("json",  {"loginId":  USERNAME, "password": PASSWORD}),
    ("json",  {"email":    USERNAME, "password": PASSWORD}),
    # Form-encoded
    ("form",  {"username": USERNAME, "password": PASSWORD, "role": ROLE}),
    ("form",  {"username": USERNAME, "password": PASSWORD}),
    ("form",  {"userName": USERNAME, "password": PASSWORD}),
]

working_login = None

# ── helpers (defined early so loops can call them) ──────────
def _has_token(body):
    if not isinstance(body, dict): return False
    for k in ("token","access_token","accessToken","jwt","Bearer",
              "authToken","auth_token","sessionToken"):
        if body.get(k): return True
    if isinstance(body.get("data"), dict):
        return _has_token(body["data"])
    return False

def _extract_token(body):
    if not isinstance(body, dict): return None
    for k in ("token","access_token","accessToken","jwt","Bearer",
              "authToken","auth_token","sessionToken"):
        if body.get(k): return body[k]
    if isinstance(body.get("data"), dict):
        return _extract_token(body["data"])
    return None

for path in LOGIN_PATHS:
    url = BASE + path
    for ptype, payload in PAYLOADS:
        try:
            if ptype == "json":
                r = SESSION.post(url, json=payload, timeout=10, verify=False)
            else:
                r = SESSION.post(url, data=payload, timeout=10, verify=False)

            if r.status_code == 405:
                # Method not allowed for this path — try GET with params
                r2 = SESSION.get(url, params=payload, timeout=10, verify=False)
                if r2.status_code not in (404, 405):
                    print(f"\n  ✓ GET {path}?params → {r2.status_code}")
                    try:
                        body = r2.json()
                        print(f"    Response keys: {list(body.keys()) if isinstance(body,dict) else type(body)}")
                        print(f"    Body[:300]: {json.dumps(body)[:300]}")
                        if _has_token(body):
                            working_login = ("GET_PARAMS", path, payload, body)
                            break
                    except:
                        print(f"    Raw: {r2.text[:200]}")
                continue

            if r.status_code == 404:
                continue  # path doesn't exist

            # Any non-404/405 response is interesting
            status_mark = "✓" if r.status_code in (200,201) else "?"
            print(f"\n  {status_mark} {ptype.upper()} {path} → {r.status_code}")
            try:
                body = r.json()
                print(f"    Response keys: {list(body.keys()) if isinstance(body,dict) else type(body)}")
                print(f"    Body[:400]: {json.dumps(body)[:400]}")
                if _has_token(body):
                    print(f"    *** TOKEN FOUND! ***")
                    working_login = (ptype, path, payload, body)
                    break
            except Exception:
                print(f"    Raw[:200]: {r.text[:200]}")

        except requests.exceptions.ConnectionError as e:
            print(f"  ✗ Connection error: {e}")
            sys.exit(1)
        except Exception as e:
            print(f"  ✗ {path} [{ptype}]: {e}")

    if working_login:
        break


# ─────────────────────────────────────────────────────────────
# STEP 3 — If still no token, try GET on /api/dashboard
#           (some systems use cookie-session, not Bearer token)
# ─────────────────────────────────────────────────────────────
if not working_login:
    print("\n" + "="*60)
    print("STEP 3 — No Bearer token found. Checking for session-cookie auth...")
    print("="*60)
    # After attempted logins, cookies may be set
    print(f"  Cookies after login attempts: {dict(SESSION.cookies)}")

    # Try hitting dashboard directly (cookies might have been set)
    r = SESSION.get(BASE + "/api/dashboard/voltage-current-trend",
                    timeout=10, verify=False,
                    params={"startDate": "07-06-2026", "endDate": "07-06-2026"})
    print(f"  Dashboard probe: {r.status_code}")
    if r.status_code == 200:
        try:
            body = r.json()
            print(f"  Keys: {list(body.keys()) if isinstance(body,dict) else type(body)}")
            print("  *** Session-cookie auth is working! No Bearer token needed ***")
            working_login = ("COOKIE", "/already-authenticated", {}, {})
        except:
            print(f"  Raw: {r.text[:300]}")

# ─────────────────────────────────────────────────────────────
# STEP 4 — Intercept actual browser login to see real request
# ─────────────────────────────────────────────────────────────
if not working_login:
    print("\n" + "="*60)
    print("STEP 4 — Manual browser intercept instructions")
    print("="*60)
    print("""
  None of the automated attempts worked. The Probus API may use
  a non-standard authentication flow.

  To find the real endpoint:
  1. Open Chrome → F12 → Network tab
  2. Go to https://tpnodl.probussense.com/login
  3. Log in manually with your credentials
  4. In the Network tab, filter by 'Fetch/XHR'
  5. Find the POST request that returns a token/cookie
  6. Right-click → Copy → Copy as cURL
  7. Paste that cURL command here and I'll reverse-engineer the auth

  Alternatively, share:
  - The exact URL of the login request
  - Request Method (POST/GET)
  - Content-Type header
  - Request payload format
    """)
    sys.exit(0)

# ─────────────────────────────────────────────────────────────
# STEP 5 — Token found → probe data endpoints
# ─────────────────────────────────────────────────────────────
print("\n" + "="*60)
print("STEP 5 — Login SUCCESS! Probing data endpoints")
print("="*60)

ptype, login_path, login_payload, login_body = working_login
token = _extract_token(login_body)
print(f"  Auth method : {ptype}")
print(f"  Login path  : {login_path}")
print(f"  Token found : {'YES — ' + str(token)[:40] + '...' if token else 'NO (cookie-session)'}")

if token:
    SESSION.headers["Authorization"] = f"Bearer {token}"
    SESSION.headers["x-auth-token"]  = token

today = "07-06-2026"
DATA_PATHS = [
    f"/api/sensor/voltage-current-trend?startDate={today}&endDate={today}",
    f"/api/dashboard/voltage-current-trend?startDate={today}&endDate={today}",
    "/api/sensor/voltage-current-trend",
    "/api/dashboard/latest-voltage-current",
    "/api/meters/latest",
    "/api/live-data",
    "/api/sensor/latest",
    f"/api/report/voltage-current?startDate={today}&endDate={today}",
    "/dashboard/api/voltage-current",
]

working_data = None
for path in DATA_PATHS:
    try:
        r = SESSION.get(BASE + path, timeout=15, verify=False)
        if r.status_code == 404:
            continue
        print(f"\n  {path} → {r.status_code}")
        try:
            body = r.json()
            count = 0
            data = (body if isinstance(body,list)
                   else body.get("data") or body.get("result")
                   or body.get("rows") or body.get("list") or [])
            if isinstance(data, list):
                count = len(data)
            print(f"    Keys: {list(body.keys()) if isinstance(body,dict) else '(list)'}")
            print(f"    Records: {count}")
            if count > 0:
                print(f"    Sample[0] keys: {list(data[0].keys()) if isinstance(data[0],dict) else '?'}")
                print(f"    Sample[0]: {json.dumps(data[0])[:300]}")
                working_data = path.split("?")[0]
                print(f"    *** DATA ENDPOINT FOUND: {working_data} ***")
                break
        except:
            print(f"    Raw[:200]: {r.text[:200]}")
    except Exception as e:
        print(f"    Error: {e}")

# ─────────────────────────────────────────────────────────────
# STEP 6 — Write working config back to data/config.json
# ─────────────────────────────────────────────────────────────
print("\n" + "="*60)
print("STEP 6 — Saving working config")
print("="*60)

config_path = os.path.join(os.path.dirname(__file__), "data", "config.json")
if os.path.exists(config_path):
    with open(config_path) as f:
        cfg = json.load(f)
else:
    cfg = {}

cfg.setdefault("scraper", {})
cfg["scraper"]["username"]   = USERNAME
cfg["scraper"]["password"]   = PASSWORD
cfg["scraper"]["role"]       = ROLE
if login_path != "/already-authenticated":
    clean_path = login_path.split("?")[0]
    cfg["scraper"]["_login_path"]  = clean_path
    cfg["scraper"]["_login_ptype"] = ptype
if working_data:
    cfg["scraper"]["_data_path"] = working_data

os.makedirs("data", exist_ok=True)
with open(config_path, "w") as f:
    json.dump(cfg, f, indent=2)

print(f"  Config saved to {config_path}")
print(f"  Login path : {login_path}")
print(f"  Login type : {ptype}")
print(f"  Data path  : {working_data or 'NOT FOUND — share Step 4 instructions above'}")
print("\n  ✅ Restart app.py — it will now use the working endpoints automatically.")
