"""
outlook_oauth_setup.py — One-time Microsoft Graph mail-send authorization
==========================================================================
Run this ONCE (any machine with a browser — doesn't have to be the PSCC
server itself) to authorize sending mail via Microsoft Graph and generate
data/outlook_token.json. After that, email_mgr.py's Outlook send path
needs no further interaction — the refresh token is reused indefinitely
(Microsoft rotates it automatically on each use; email_mgr.py persists
the rotated value back to this same file).

Uses the OAuth2 DEVICE CODE flow — no redirect URI, no local web server,
no Azure app secret required. You just visit a URL and type a short code.

────────────────────────────────────────────────────────────────────────
ONE-TIME AZURE SETUP (do this first, in the Azure Portal)
────────────────────────────────────────────────────────────────────────
1. Go to https://portal.azure.com → "App registrations" → "New registration"
2. Name it anything (e.g. "TPNODL PSCC Mail Sender")
3. Supported account types: pick whichever matches your situation —
   "Accounts in this organizational directory only" if this is a work/
   school Microsoft 365 account, or "Personal Microsoft accounts only"
   for an @outlook.com account.
4. Redirect URI: leave blank (not needed for device code flow)
5. After creation, note the "Application (client) ID" — you'll paste it
   in below.
6. Go to "Authentication" (left sidebar) → scroll to "Advanced settings"
   → set "Allow public client flows" to YES → Save.
   (Without this, the device code flow will fail with an error about
   the client not being configured for public client flows.)
7. Go to "API permissions" → "Add a permission" → "Microsoft Graph" →
   "Delegated permissions" → search "Mail.Send" → check it → also add
   "offline_access" the same way → "Add permissions".
   If this is a work/school account and the org requires admin consent,
   ask your IT admin to click "Grant admin consent" on this page —
   otherwise the device code flow below will fail at the consent step.
8. Note your "Directory (tenant) ID" too (also on the Overview page) —
   for a personal @outlook.com account, just use "consumers" instead.

────────────────────────────────────────────────────────────────────────
RUNNING THIS SCRIPT
────────────────────────────────────────────────────────────────────────
    pip install requests --break-system-packages   (if not already installed)
    python outlook_oauth_setup.py

It will print a URL and a short code. Visit the URL on any device, enter
the code, and sign in with the Outlook/Microsoft 365 account you want to
send FROM. Once approved, this script saves data/outlook_token.json.

Copy that file to the PSCC server's data/ folder if this was run
elsewhere, then set email.use_outlook_api = true in config (or via the
Settings UI, once exposed there).
"""
import json
import os
import sys
import time

try:
    import requests
except ImportError:
    print("ERROR: 'requests' library not installed. Run:")
    print("  pip install requests --break-system-packages")
    sys.exit(1)

# FIX: previously this was a relative path ("data/outlook_token.json"),
# which Python resolves relative to the CURRENT WORKING DIRECTORY — i.e.
# wherever you happened to launch the script FROM, not where the script
# file itself lives. Running it via a full path from a different folder
# (e.g. from C:\Users\you> instead of cd-ing into the project folder
# first) silently wrote the token into the wrong place with no error.
# Anchoring to the script's own directory makes this work correctly no
# matter how/from-where it's launched.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(SCRIPT_DIR, "data", "outlook_token.json")
SCOPE = "https://graph.microsoft.com/Mail.Send offline_access"


def main():
    print("=" * 70)
    print("Microsoft Graph mail-send authorization (device code flow)")
    print("=" * 70)
    print()
    client_id = input("Application (client) ID from Azure App registration: ").strip()
    tenant = input("Directory (tenant) ID (or just press Enter for a personal "
                   "@outlook.com account): ").strip() or "consumers"

    devicecode_url = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/devicecode"
    token_url = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"

    resp = requests.post(devicecode_url, data={
        "client_id": client_id,
        "scope": SCOPE,
    }, timeout=20)

    if resp.status_code != 200:
        print(f"\n[ERROR] Failed to start device code flow ({resp.status_code}):")
        print(resp.text[:500])
        sys.exit(1)

    dc = resp.json()
    print()
    print(dc.get("message", ""))
    print()
    print("Waiting for you to complete sign-in in the browser...")

    interval = dc.get("interval", 5)
    expires_in = dc.get("expires_in", 900)
    deadline = time.time() + expires_in

    while time.time() < deadline:
        time.sleep(interval)
        poll = requests.post(token_url, data={
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "client_id": client_id,
            "device_code": dc["device_code"],
        }, timeout=20)
        data = poll.json()

        if poll.status_code == 200:
            os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
            with open(TOKEN_FILE, "w") as f:
                json.dump({
                    "client_id": client_id,
                    "tenant": tenant,
                    "refresh_token": data["refresh_token"],
                }, f, indent=2)
            print()
            print(f"✅ Success! Saved {TOKEN_FILE}")
            print()
            print("Next steps:")
            print(f"  1. If this was run on a different machine, copy {TOKEN_FILE}")
            print("     to the PSCC server's data/ folder.")
            print("  2. Set email.use_outlook_api = true in your config")
            print("     (and check/adjust email.outlook_domains if needed).")
            return

        err = data.get("error", "")
        if err == "authorization_pending":
            continue  # keep polling, user hasn't finished yet
        elif err == "slow_down":
            interval += 5
            continue
        else:
            print(f"\n[ERROR] {err}: {data.get('error_description', '')}")
            sys.exit(1)

    print("\n[ERROR] Timed out waiting for sign-in. Run this script again.")
    sys.exit(1)


if __name__ == "__main__":
    main()
