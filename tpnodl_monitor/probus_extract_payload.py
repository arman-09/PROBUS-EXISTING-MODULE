"""
probus_extract_payload.py
==========================
Downloads the Probus React JS bundle and finds the exact
login payload field names from TpwodlLogin.js source.
Run: python probus_extract_payload.py
"""
import requests, re, urllib3
urllib3.disable_warnings()

S = requests.Session()
S.verify = False
S.headers["User-Agent"] = "Mozilla/5.0 Chrome/148.0.0.0"

print("Fetching React app bundle to find login payload...")
# Get homepage to find JS bundle paths
r = S.get("https://tpnodl.probussense.com/", timeout=15)
html = r.text

# Find all JS chunk files
js_paths = re.findall(r'src="(/static/js/[^"]+\.js)"', html)
js_paths += re.findall(r'(/static/js/[a-zA-Z0-9.]+\.chunk\.js)', html)
js_paths = list(dict.fromkeys(js_paths))
print(f"Found {len(js_paths)} JS files: {js_paths[:4]}")

for path in js_paths:
    url = "https://tpnodl.probussense.com" + path
    try:
        r = S.get(url, timeout=30)
        js = r.text
        if "TpwodlLogin" not in js and "auth/login" not in js:
            continue
        print(f"\n✓ Found login code in {path} ({len(js)//1024}KB)")

        # Find the login function block
        # Look for patterns around auth/login
        hits = [(m.start(), m.group()) for m in
                re.finditer(r'.{0,200}auth.login.{0,200}', js)]
        for pos, hit in hits[:5]:
            print(f"\n  Snippet: ...{hit}...")

        # Find object literals near password field
        pwd_hits = [(m.start(), m.group()) for m in
                    re.finditer(r'.{0,100}["\']password["\']\s*:.{0,200}', js)]
        for pos, hit in pwd_hits[:5]:
            print(f"\n  Password obj: {hit[:300]}")

        # Find field names used in login payload
        field_hits = re.findall(
            r'[{,]\s*["\']?(\w+)["\']?\s*:\s*(?:[a-zA-Z_.]+(?:username|password|user|login|id)[a-zA-Z_.]*)',
            js, re.IGNORECASE
        )
        print(f"\n  Field names near credentials: {list(set(field_hits))[:20]}")

    except Exception as e:
        print(f"  Error {path}: {e}")
