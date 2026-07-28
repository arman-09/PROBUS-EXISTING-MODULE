"""
probus_test.py v4
=================
JWT confirms sub=1925, so username IS correct.
"user not found:null" on POST likely means the server expects
the username field under a DIFFERENT key name, or needs
an extra field like companyId / tenantId / orgCode.

This version tries the most likely remaining variations.
ONE attempt per variant — no rapid-fire.
"""
import requests, json, urllib3, sys, os, time
from datetime import datetime, timedelta
urllib3.disable_warnings()

USERNAME = "1925"
PASSWORD = "outagespsc"   # confirmed working in browser

S = requests.Session()
S.verify = False
S.headers.update({
    "User-Agent":         "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/148.0.0.0 Safari/537.36",
    "Accept":             "application/json, text/plain, */*",
    "Content-Type":       "application/json",
    "Origin":             "https://tpnodl.probussense.com",
    "Referer":            "https://tpnodl.probussense.com/",
    "sec-ch-ua":          '"Chromium";v="148", "Google Chrome";v="148", "Not/A)Brand";v="99"',
    "sec-ch-ua-mobile":   "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest":     "empty",
    "sec-fetch-mode":     "cors",
    "sec-fetch-site":     "same-site",
})

BASE      = "https://tpnodl.probussense.com:8083"
LOGIN_URL = BASE + "/user/auth/login?loginMode=SENSE_ADMIN&dbOverride=true"

# ── Different username field names the API may expect ─────────
# "user not found:null" — the null suggests the server
# extracted the username from a field it couldn't find,
# so it got null. We need to find the right field name.
PAYLOADS = [
    # Most likely — matches Probus source file "TpwodlLogin.js"
    {"userId":   USERNAME, "password": PASSWORD},
    {"loginId":  USERNAME, "password": PASSWORD},
    {"userName": USERNAME, "password": PASSWORD},
    # With role in body too
    {"userId":   USERNAME, "password": PASSWORD, "loginMode": "SENSE_ADMIN"},
    {"username": USERNAME, "password": PASSWORD, "companyCode": "TPNODL"},
    {"username": USERNAME, "password": PASSWORD, "tenantId": "TPNODL"},
    {"username": USERNAME, "password": PASSWORD, "orgCode": "TPNODL"},
    # Maybe it's nested
    {"user": {"username": USERNAME, "password": PASSWORD}},
    {"credentials": {"username": USERNAME, "password": PASSWORD}},
]

def dig(obj, *keys):
    for k in keys:
        if isinstance(obj, dict):
            if k in obj and obj[k]:
                return obj[k]
            for v in obj.values():
                if isinstance(v, dict) and k in v and v[k]:
                    return v[k]
    return None

token        = None
working_body = None

print(f"\nTrying username field variants against {LOGIN_URL}\n")

for payload in PAYLOADS:
    # Rate-limit safety — 3 second gap between each attempt
    time.sleep(3)
    r = S.post(LOGIN_URL, json=payload, timeout=15, verify=False)
    raw = r.text.strip()
    print(f"  [{r.status_code}] {json.dumps(payload)[:80]}")
    print(f"           → {raw[:120]}")

    if r.status_code == 509:
        print("\n⏳ Rate limited again. Wait 15 min.")
        sys.exit(1)

    if r.status_code in (200, 201) and raw:
        try:
            body  = r.json()
            token = dig(body, "token", "access_token", "accessToken", "jwt", "authToken")
            if token:
                print(f"\n  ✅ TOKEN FOUND with payload: {json.dumps(payload)}")
                working_body = payload
                break
        except Exception:
            pass

if not token:
    print("""
──────────────────────────────────────────────────────────────
None of the field name variants worked.

DEFINITIVE FIX — do this:
1. Chrome → https://tpnodl.probussense.com/login
2. F12 → Network tab → Preserve log ✓
3. Log in manually
4. Click the login XHR request → PAYLOAD tab
5. Share the exact JSON shown (it will show the correct field names)
──────────────────────────────────────────────────────────────
""")
    sys.exit(1)

# ── Confirmed working → save config + test data endpoint ──────
print(f"\n✅ Login working. Saving to data/config.json...")
S.headers["Authorization"] = f"Bearer {token}"

os.makedirs("data", exist_ok=True)
cfg_path = "data/config.json"
cfg = {}
if os.path.exists(cfg_path):
    with open(cfg_path) as f:
        try: cfg = json.load(f)
        except: pass
cfg.setdefault("scraper", {})
cfg["scraper"].update({"username": USERNAME, "password": PASSWORD,
                        "role": "Sense Admin", "_login_payload": working_body})
with open(cfg_path, "w") as f:
    json.dump(cfg, f, indent=2)
print("   Saved ✓")

print("\n[2] Fetching live data from :8091 ...")
now   = datetime.now()
start = (now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
end   = now.strftime("%Y-%m-%d %H:%M:%S")

r2 = S.get(
    "https://tpnodl.probussense.com:8091/meterdashboard/asset-latest-instant-param-data",
    params={"startTime": start, "endTime": end, "entityId": ""},
    timeout=30, verify=False
)
print(f"   Status : {r2.status_code}")
raw2 = r2.text.strip()
if r2.status_code == 200 and raw2:
    body2 = r2.json()
    data  = (body2 if isinstance(body2, list)
             else body2.get("data") or body2.get("result")
             or body2.get("rows") or body2.get("content") or [])
    print(f"   Records: {len(data)}")
    if data:
        print(f"   Keys   : {list(data[0].keys())}")
        with open("data/test_response.json", "w") as f:
            json.dump({"count": len(data), "working_payload": working_body,
                       "sample": data[:3]}, f, indent=2, default=str)
        print(f"\n✅ FULL SUCCESS — restart app.py and Fetch Now!")
    else:
        print(f"   Body: {raw2[:400]}")
else:
    print(f"   Body: {raw2[:400]}")
