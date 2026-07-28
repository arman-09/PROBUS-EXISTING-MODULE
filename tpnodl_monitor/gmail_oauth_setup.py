"""
gmail_oauth_setup.py — One-time Gmail API authorization
=========================================================
Run this ONCE manually (python gmail_oauth_setup.py) to authorize
TPNODL Monitor to send email via your Gmail account WITHOUT using SMTP
(no port 587/465 needed — uses HTTPS/443 only, exactly like a browser).

After this completes, it saves data/gmail_token.json which the running
server (email_mgr.py) reuses forever to send mail. You only need to
re-run this if the token is revoked or you change accounts.

SETUP STEPS (one-time, ~5 minutes):
------------------------------------
1. Go to https://console.cloud.google.com/
2. Create a new project (or pick an existing one)
3. Enable the "Gmail API":
   APIs & Services → Library → search "Gmail API" → Enable
4. Create OAuth credentials:
   APIs & Services → Credentials → Create Credentials → OAuth client ID
   Application type: "Desktop app"
   Download the JSON — save it as data/gmail_client_secret.json
   (rename whatever Google gives you to exactly this filename)
5. Add yourself as a test user (if app is in "Testing" mode):
   APIs & Services → OAuth consent screen → Test users → Add your email
6. Run this script: python gmail_oauth_setup.py
   It opens a browser — log in with the Gmail account you want to send
   FROM, approve "Send email on your behalf" — and that's it.

This script only needs to run on ANY machine with a browser — does NOT
have to be the server itself. Copy the resulting data/gmail_token.json
to the server's data/ folder afterward if you ran it elsewhere.
"""

import json, os, sys, base64, webbrowser
import urllib.parse as _up
import http.server, threading, time

import requests

BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
SECRET_FILE  = os.path.join(BASE_DIR, "data", "gmail_client_secret.json")
TOKEN_FILE   = os.path.join(BASE_DIR, "data", "gmail_token.json")
SCOPE        = "https://www.googleapis.com/auth/gmail.send"
REDIRECT_URI = "http://localhost:8765/callback"

GOOGLE_AUTH_URL  = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"


def load_client_secret():
    if not os.path.exists(SECRET_FILE):
        print(f"\n❌ Missing file: {SECRET_FILE}")
        print("   Download OAuth client credentials from Google Cloud Console")
        print("   (Desktop app type) and save as exactly this filename.\n")
        sys.exit(1)
    with open(SECRET_FILE) as f:
        data = json.load(f)
    # Google's downloaded file nests under "installed" or "web"
    creds = data.get("installed") or data.get("web") or data
    return creds["client_id"], creds["client_secret"]


_auth_code = {"code": None}


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = _up.urlparse(self.path)
        params = _up.parse_qs(parsed.query)
        if "code" in params:
            _auth_code["code"] = params["code"][0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h2>Authorization successful!</h2>"
                              b"<p>You can close this tab and return to the terminal.</p>")
        else:
            self.send_response(400)
            self.end_headers()

    def log_message(self, fmt, *args):
        pass  # silence default request logging


def run_local_server_and_wait():
    try:
        server = http.server.HTTPServer(("localhost", 8765), _CallbackHandler)
    except OSError as e:
        print(f"\n⚠ Could not start local server on port 8765 ({e})")
        print("  Falling back to manual mode — see below.\n")
        return None

    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    print("Waiting for browser authorization (up to 60s)...")
    waited = 0
    while _auth_code["code"] is None and waited < 60:
        time.sleep(1)
        waited += 1
    server.shutdown()
    return _auth_code["code"]


def manual_code_entry():
    """
    Fallback when the local callback server doesn't receive the redirect
    (e.g. Windows Firewall silently blocked it, or antivirus intercepted
    the localhost connection). The browser still completes the Google
    login/consent and redirects to a localhost URL that fails to load —
    but the authorization code is RIGHT THERE in the browser's address bar.
    We just need you to copy it from there instead of the browser doing it
    automatically.
    """
    print("\n" + "=" * 70)
    print("MANUAL MODE — the browser couldn't reach the local script,")
    print("but your Google login still succeeded. Look at your browser's")
    print("address bar right now — it shows something like:")
    print()
    print("  http://localhost:8765/callback?...&code=4/0Adk...XXXXX&scope=...")
    print()
    print("Copy ONLY the value between 'code=' and the next '&' character.")
    print("=" * 70)
    code = input("\nPaste the code here and press Enter: ").strip()
    return code if code else None


def main():
    client_id, client_secret = load_client_secret()

    params = {
        "client_id":     client_id,
        "redirect_uri":  REDIRECT_URI,
        "response_type": "code",
        "scope":         SCOPE,
        "access_type":   "offline",   # critical — gives us a refresh_token
        "prompt":        "consent",   # forces refresh_token even on re-auth
    }
    auth_url = GOOGLE_AUTH_URL + "?" + _up.urlencode(params)

    print("\nOpening browser for Gmail authorization...")
    print("If it doesn't open automatically, visit this URL manually:\n")
    print(auth_url)
    print()
    webbrowser.open(auth_url)

    code = run_local_server_and_wait()
    if not code:
        code = manual_code_entry()
    if not code:
        print("\n❌ No authorization code received. Try again.")
        sys.exit(1)

    print("Exchanging authorization code for tokens...")
    resp = requests.post(GOOGLE_TOKEN_URL, data={
        "code":          code,
        "client_id":     client_id,
        "client_secret": client_secret,
        "redirect_uri":  REDIRECT_URI,
        "grant_type":    "authorization_code",
    }, timeout=20)

    if resp.status_code != 200:
        print(f"\n❌ Token exchange failed: {resp.status_code}\n{resp.text}")
        sys.exit(1)

    token_data = resp.json()
    if "refresh_token" not in token_data:
        print("\n⚠ No refresh_token returned. This usually means you've already")
        print("  authorized this app before. Go to:")
        print("  https://myaccount.google.com/permissions")
        print("  Remove access for this app, then run this script again.")
        sys.exit(1)

    os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
    save_data = {
        "client_id":     client_id,
        "client_secret": client_secret,
        "refresh_token": token_data["refresh_token"],
    }
    with open(TOKEN_FILE, "w") as f:
        json.dump(save_data, f, indent=2)

    print(f"\n✅ Success! Token saved to: {TOKEN_FILE}")
    print("   The TPNODL Monitor server will now use this to send email")
    print("   via Gmail API (HTTPS only — no SMTP port needed).")
    print("\n   Enable it in Notifications settings:")
    print("   'Use Gmail API (no SMTP port required)' → toggle ON\n")


if __name__ == "__main__":
    main()
