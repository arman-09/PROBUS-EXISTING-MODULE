"""
probus_aws_debug.py — AWS-specific Probus API Discovery
=========================================================
The React SPA is served from probussense.com (nginx).
The actual REST API is on a DIFFERENT host/port on AWS.

This script:
1. Extracts the real API base URL from the React JS bundle
2. Then attempts login against that URL
3. Saves working config automatically

Run: python probus_aws_debug.py
"""

import requests, json, urllib3, re, sys, os
urllib3.disable_warnings()

FRONTEND = "https://tpnodl.probussense.com"
PASSWORD = input("Enter Probus password: ").strip()
USERNAME = "1925"
ROLE     = "Sense Admin"

S = requests.Session()
S.verify  = False
S.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/125.0.0.0",
    "Accept":     "*/*",
})

print("\n" + "="*60)
print("PHASE 1 — Extract real API URL from React JS bundle")
print("="*60)

# ── 1a. Get the main page HTML, find JS bundle filenames ──────
r = S.get(FRONTEND + "/", verify=False, timeout=15)
html = r.text

# React CRA apps inject JS bundle paths in index.html
js_files = re.findall(r'src="(/static/js/[^"]+\.js)"', html)
js_files += re.findall(r'"(/static/js/[^"]+\.chunk\.js)"', html)
# Also look for main bundle
main_bundle = re.findall(r'(/static/js/main\.[a-z0-9]+\.js)', html)
js_files = list(dict.fromkeys(main_bundle + js_files))  # dedupe, main first

print(f"  Found {len(js_files)} JS bundle files: {js_files[:5]}")

# ── 1b. Scan JS bundles for API base URL patterns ─────────────
api_candidates = set()
found_bundle   = None

URL_PATTERNS = [
    # Common env var patterns in CRA bundles
    r'(?:REACT_APP_API_URL|REACT_APP_BASE_URL|API_URL|BASE_URL|apiUrl|baseUrl|apiBaseUrl)\s*[=:]\s*["\']([^"\']+)["\']',
    # Direct URL strings that look like API endpoints
    r'"(https?://[^"]{5,100}/(?:api|v\d|graphql)[^"]*)"',
    r"'(https?://[^']{5,100}/(?:api|v\d|graphql)[^']*)'",
    # AWS API Gateway patterns
    r'"(https://[a-z0-9]+\.execute-api\.[a-z0-9-]+\.amazonaws\.com[^"]*)"',
    r'"(https://[a-z0-9]+\.amazonaws\.com[^"]*)"',
    # Internal IP/port patterns (common in internal deployments)
    r'"(https?://(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?/(?:api|v\d)[^"]*)"',
    r'"(https?://[a-zA-Z0-9.-]+:\d{2,5}/[^"]{2,80})"',
    # /api prefix without host (same-origin but different port via proxy)
    r'axios\.defaults\.baseURL\s*=\s*["\']([^"\']+)["\']',
    r'baseURL\s*:\s*["\']([^"\']+)["\']',
    r'(?:process\.env\.|window\.__env__\.)([A-Z_]+)\s*\|\|\s*["\']([^"\']+)["\']',
]

for js_path in js_files[:6]:  # check first 6 bundles
    url = FRONTEND + js_path
    print(f"\n  Scanning {js_path}...")
    try:
        r = S.get(url, verify=False, timeout=30)
        js = r.text
        size_kb = len(js) // 1024
        print(f"    Size: {size_kb} KB")

        for pattern in URL_PATTERNS:
            matches = re.findall(pattern, js)
            for m in matches:
                val = m if isinstance(m, str) else (m[1] if len(m) > 1 else m[0])
                if val and len(val) > 5 and val not in api_candidates:
                    api_candidates.add(val)
                    print(f"    🔍 Found: {val}")

        # Also look for specific Probus patterns
        probus_hits = re.findall(r'(?:probus|hes|meter|scada|energy)[^"\']{0,30}["\']([^"\']{10,100})["\']', js, re.IGNORECASE)
        for h in probus_hits[:5]:
            if '/' in h or ':' in h:
                print(f"    🔍 Probus hint: {h}")
                api_candidates.add(h)

        found_bundle = js_path
    except Exception as e:
        print(f"    Error: {e}")

# ── 1c. Also check /asset-manifest.json and /config.json ──────
for extra in ["/asset-manifest.json", "/config.json", "/env.json",
              "/static/config.json", "/api-config.json"]:
    try:
        r = S.get(FRONTEND + extra, verify=False, timeout=5)
        if r.status_code == 200 and 'json' in r.headers.get('content-type',''):
            print(f"\n  {extra} → {r.text[:400]}")
            try:
                obj = r.json()
                for k,v in (obj.items() if isinstance(obj,dict) else []):
                    if isinstance(v,str) and ('http' in v or 'api' in v.lower()):
                        api_candidates.add(v)
                        print(f"    Config key {k}: {v}")
            except: pass
    except: pass

print(f"\n  Total API URL candidates found: {len(api_candidates)}")
for c in sorted(api_candidates):
    print(f"    → {c}")

# ─────────────────────────────────────────────────────────────
# PHASE 2 — Try login against every discovered URL
# ─────────────────────────────────────────────────────────────
print("\n" + "="*60)
print("PHASE 2 — Attempt login against discovered API URLs")
print("="*60)

# Deduplicate and normalise to base URLs
bases_to_try = set()
for c in api_candidates:
    if c.startswith('http'):
        # Extract base (scheme + host + optional port)
        m = re.match(r'(https?://[^/]+)', c)
        if m:
            bases_to_try.add(m.group(1))
        bases_to_try.add(c.rstrip('/'))
    elif c.startswith('/'):
        bases_to_try.add(FRONTEND + c.rstrip('/'))

# Also try common port variations on the same host
for port in [8080, 8443, 3000, 3001, 4000, 5000, 8000, 8888, 9000]:
    bases_to_try.add(f"https://tpnodl.probussense.com:{port}")
    bases_to_try.add(f"http://tpnodl.probussense.com:{port}")

# API-specific subdomains
for sub in ['api', 'backend', 'server', 'services', 'hes', 'app']:
    bases_to_try.add(f"https://{sub}.probussense.com")

LOGIN_PATHS = ["/api/login", "/login", "/auth/login", "/api/auth/login", "/api/v1/login"]
PAYLOADS = [
    {"username": USERNAME, "password": PASSWORD, "role": ROLE},
    {"username": USERNAME, "password": PASSWORD},
    {"userName": USERNAME, "password": PASSWORD, "role": ROLE},
    {"userId":   USERNAME, "password": PASSWORD},
]

working = None

def try_login(base, path, payload, ptype="json"):
    url = base.rstrip('/') + path
    try:
        if ptype == "json":
            r = S.post(url, json=payload, timeout=8, verify=False)
        else:
            r = S.post(url, data=payload, timeout=8, verify=False)
        if r.status_code in (200, 201):
            try:
                body = r.json()
                if isinstance(body, dict):
                    token = (body.get("token") or body.get("access_token")
                             or body.get("accessToken") or body.get("jwt")
                             or (body.get("data") or {}).get("token")
                             or (body.get("data") or {}).get("access_token"))
                    if token:
                        print(f"\n  ✅✅✅ LOGIN SUCCESS!")
                        print(f"     URL   : {url}")
                        print(f"     Type  : {ptype}")
                        print(f"     Token : {str(token)[:60]}...")
                        print(f"     Keys  : {list(body.keys())}")
                        return base, path, ptype, payload, token, body
                    else:
                        print(f"  ? {url} → 200 but no token. Keys: {list(body.keys())}")
                        print(f"    Body: {json.dumps(body)[:200]}")
            except:
                pass
        elif r.status_code not in (404, 405, 000):
            print(f"  {r.status_code} {url}")
    except requests.exceptions.ConnectionError:
        pass  # host not reachable
    except requests.exceptions.Timeout:
        print(f"  TIMEOUT {url}")
    except Exception as e:
        pass
    return None

for base in sorted(bases_to_try):
    for path in LOGIN_PATHS:
        for payload in PAYLOADS:
            result = try_login(base, path, payload, "json")
            if result:
                working = result
                break
            result = try_login(base, path, payload, "form")
            if result:
                working = result
                break
        if working: break
    if working: break

# ─────────────────────────────────────────────────────────────
# PHASE 3 — Check network tab via mitmproxy instructions if failed
# ─────────────────────────────────────────────────────────────
if not working:
    print("\n" + "="*60)
    print("PHASE 3 — Could not find API automatically")
    print("="*60)
    print("""
  The API URL is not in the JS bundle or on a standard port.
  It may be loaded from a runtime config or injected server-side.

  ─── OPTION A: Chrome DevTools (quickest) ────────────────────
  1. Open Chrome → go to https://tpnodl.probussense.com/login
  2. F12 → Network tab → tick "Preserve log"
  3. Log in with username=1925 and your password
  4. In Network tab → filter by "XHR" or "Fetch"
  5. Look for a request that is NOT to probussense.com
     (could be *.amazonaws.com, *.execute-api.*, or an IP)
  6. Click that request → right-click → Copy → "Copy as cURL"
  7. Paste the cURL here

  ─── OPTION B: Check the exported Excel file ─────────────────
  The "Latest Voltage/Current Trend List" Excel you shared earlier
  — open it in Excel and check:
    File → Info → Properties → look for a source URL
  OR check if it was exported from a different URL than the login page.

  ─── OPTION C: Check the Probus HES manual/welcome email ─────
  Probus Sense typically sends an onboarding email with the API
  documentation URL. The API base URL is usually documented there.
  Search your email for "probus" "api" "swagger" or "documentation".

  ─── OPTION D: Check browser localStorage after login ────────
  After logging in manually in Chrome:
  F12 → Application tab → Local Storage → tpnodl.probussense.com
  Look for a key containing "token", "auth", or "api_url"
  Share the contents here.
    """)
    sys.exit(0)

# ─────────────────────────────────────────────────────────────
# PHASE 4 — Login worked → find data endpoints
# ─────────────────────────────────────────────────────────────
api_base, login_path, ptype, payload, token, login_body = working

print(f"\n{'='*60}")
print("PHASE 4 — Login succeeded, finding data endpoints")
print(f"{'='*60}")

S.headers["Authorization"] = f"Bearer {token}"
S.headers["x-auth-token"]  = token

today = "07-06-2026"
DATA_PATHS = [
    f"/api/sensor/voltage-current-trend?startDate={today}&endDate={today}",
    f"/api/dashboard/voltage-current-trend?startDate={today}&endDate={today}",
    "/api/sensor/voltage-current-trend",
    "/api/sensor/latest",
    "/api/meters/latest",
    "/api/live",
    "/api/readings/latest",
    f"/api/report/voltage-current?date={today}",
    "/api/dashboard/latest",
    "/api/hes/meters/latest",
]

working_data = None
for path in DATA_PATHS:
    url = api_base.rstrip('/') + path
    try:
        r = S.get(url, verify=False, timeout=15)
        if r.status_code == 404: continue
        print(f"\n  {r.status_code} {path}")
        try:
            body = r.json()
            data = (body if isinstance(body,list)
                   else body.get("data") or body.get("result")
                   or body.get("rows") or body.get("list") or [])
            count = len(data) if isinstance(data, list) else "?"
            print(f"    Records: {count}")
            if isinstance(data, list) and len(data) > 0:
                print(f"    Sample keys: {list(data[0].keys()) if isinstance(data[0],dict) else '?'}")
                working_data = path.split("?")[0]
                print(f"    *** DATA PATH FOUND: {working_data} ***")
                break
        except:
            print(f"    Raw: {r.text[:150]}")
    except Exception as e:
        print(f"    Error: {e}")

# ─────────────────────────────────────────────────────────────
# PHASE 5 — Write config
# ─────────────────────────────────────────────────────────────
print(f"\n{'='*60}")
print("PHASE 5 — Saving config to data/config.json")
print(f"{'='*60}")

cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "config.json")
os.makedirs(os.path.dirname(cfg_path), exist_ok=True)

cfg = {}
if os.path.exists(cfg_path):
    with open(cfg_path) as f:
        try: cfg = json.load(f)
        except: pass

cfg.setdefault("scraper", {})
cfg["scraper"].update({
    "username":     USERNAME,
    "password":     PASSWORD,
    "role":         ROLE,
    "base_url":     api_base,
    "_login_path":  login_path,
    "_login_ptype": ptype,
    "_data_path":   working_data or "",
})

with open(cfg_path, "w") as f:
    json.dump(cfg, f, indent=2)

print(f"""
  ✅ Config saved!
  API base : {api_base}
  Login    : {login_path} [{ptype}]
  Data     : {working_data or 'NOT FOUND'}

  Now update modules/scraper.py BASE to: {api_base}
  Then restart app.py — Fetch Now will work.
""")
