"""
modules/email_mgr.py — Email Notification Manager (v2)
========================================================
Key fixes:
- Always tries STARTTLS on port 587 regardless of UI setting
- Batches all violations into ONE email per cycle (not one per violation)
- Max 1 alert email per COOLDOWN_MINUTES regardless of violation count
- Proper error messages for each failure mode

Gmail API support (no SMTP port required):
- If email.use_gmail_api=True and data/gmail_token.json exists, sends mail
  over HTTPS (443) via the Gmail API instead of SMTP (587/465). Use this
  when the network blocks outbound SMTP ports but allows normal HTTPS
  (e.g. WhatsApp Web / browsers work fine, but smtp.gmail.com:587 times out).
- One-time setup: run gmail_oauth_setup.py once (any machine with a browser)
  to authorize and generate data/gmail_token.json, then copy that file to
  the server if it was generated elsewhere. After that, sending needs no
  further interaction — the refresh token is reused indefinitely.
"""

import smtplib, logging, ssl, time, os, json, base64
from email.mime.multipart import MIMEMultipart
from email.mime.text      import MIMEText
from datetime             import datetime

try:
    import requests as _requests
except ImportError:
    _requests = None

log = logging.getLogger("email_mgr")

# Absolute minimum gap between any two emails (seconds)
EMAIL_COOLDOWN_SEC = 300   # 5 minutes between batch emails

TYPE_COLORS = {
    "OV":               "#dc3545",
    "UV":               "#ffc107",
    "OL":               "#fd7e14",
    "FEEDER_OFF":       "#212529",
    "SUDDEN_LOAD_DROP": "#6f42c1",
    "SUDDEN_LOAD_RAISE":"#0d6efd",
    "LOAD_DIVERTED":    "#e83e8c",
    "LOAD_RESTORED":    "#20c997",
}
TYPE_LABELS = {
    "OV":               "⚡ Over Voltage",
    "UV":               "⬇ Under Voltage",
    "OL":               "🔴 Overload",
    "FEEDER_OFF":       "⚫ Feeder OFF",
    "SUDDEN_LOAD_DROP": "📉 Sudden Load Drop",
    "SUDDEN_LOAD_RAISE":"📈 Sudden Load Raise",
    "LOAD_DIVERTED":    "🔀 Load Diverted via BC",
    "LOAD_RESTORED":    "✅ Load Restored",
}


class EmailManager:
    def __init__(self, cfg):
        self.cfg = cfg
        self._last_sent = 0   # epoch of last successful send
        self._gmail_tokens = {}   # token_file path -> {"access_token", "expiry"}
        self._ms_access_token = None
        self._ms_token_expiry = 0   # epoch — access token validity

    # ── Public: send batch of violations ─────────────────────
    def send_violations(self, violations: list, extra_to: list = None) -> bool:
        """Send all violations as ONE consolidated email.

        extra_to: additional recipients (e.g. from matching Circle/Division
        contact-directory entries) to union with the global email.recipients
        list — see violation.py's _notify() for how this is assembled.
        Global Contacts ALWAYS receive every email; extra_to ADDS the
        dedicated area contacts on top, it never replaces the global list."""
        if not violations:
            return False
        if not self.cfg.get("email.enabled", False):
            log.debug("Email disabled — skipping")
            return False
        # Batch cooldown — don't spam
        if time.time() - self._last_sent < EMAIL_COOLDOWN_SEC:
            log.debug(f"Email batch cooldown active ({EMAIL_COOLDOWN_SEC}s)")
            return False

        global_to = self.cfg.get("email.recipients", [])
        # dict.fromkeys preserves order while deduping — global contacts
        # listed first, area-specific additions appended after
        to = list(dict.fromkeys(global_to + (extra_to or [])))
        subj_px = self.cfg.get("email.subject_prefix", "[TPNODL ALERT]")
        if not to:
            log.warning("Email: no recipients configured")
            return False

        # Group by type for subject line
        type_counts = {}
        for v in violations:
            t = v.get("type","?")
            type_counts[t] = type_counts.get(t, 0) + 1
        summary = ", ".join(f"{TYPE_LABELS.get(t,t).split()[-1]}×{n}"
                            for t, n in type_counts.items())
        subject = f"{subj_px} {summary}"

        plain = self._plain_body(violations)
        html  = self._html_body(violations)

        ok = self._send_email(to, subject, plain, html)
        if ok:
            self._last_sent = time.time()
            log.info(f"Batch email sent: {len(violations)} violations → {to}")
        return ok

    # ── Public: test email ────────────────────────────────────
    def send_test(self) -> dict:
        to = self.cfg.get("email.recipients", [])
        if not to:
            return {"ok": False, "error": "No recipients configured"}
        subject = (self.cfg.get("email.subject_prefix","[TPNODL ALERT]")
                   + " TEST — Email working correctly")
        body = (f"This is a test email from TPNODL Load & Voltage Monitor.\n"
                f"Sent at: {datetime.now().strftime('%d-%b-%Y %H:%M:%S')}\n"
                f"If you received this, email notifications are configured correctly.")
        ok = self._send_email(to, subject, body, self._simple_html(body))
        return {"ok": ok, "to": to,
                "error": None if ok else "SMTP send failed — check server log"}

    # ── Dispatcher: routes to Gmail API or SMTP ───────────────
    def _send_email(self, to: list, subject: str,
                     plain: str, html: str) -> bool:
        """
        FIX — dual-provider routing: previously every recipient went
        through the SAME Gmail account regardless of domain, which meant
        (a) all 120+ recipients counted against ONE Gmail account's daily
        sending cap (~500/day personal, ~2,000/day Workspace), and
        (b) Outlook/Microsoft-365 recipients received mail relayed
        through Gmail/a Google Group, which commonly fails DMARC/SPF/DKIM
        alignment on Microsoft's strict receivers and gets silently
        junked — exactly the "Gmail gets it, Outlook doesn't" pattern.

        Now: recipients whose domain matches email.outlook_domains (configurable
        — defaults to the consumer Microsoft domains; add your own org's
        Microsoft 365 domain there too if applicable) are routed through
        Microsoft Graph instead, using a SEPARATE OAuth identity with its
        own independent daily quota. Everyone else still goes through
        Gmail exactly as before. This is a real fix for both problems at
        once: it roughly DOUBLES total daily capacity (two independent
        quota pools instead of one), and Outlook recipients now receive
        mail sent natively from an Outlook-ecosystem sender, which
        Microsoft's own mail servers trust far more readily.
        """
        outlook_domains = set(d.lower().lstrip("@") for d in
                              (self.cfg.get("email.outlook_domains") or
                               ["outlook.com", "hotmail.com", "live.com", "msn.com"]))
        use_outlook_api = self.cfg.get("email.use_outlook_api", False)

        if use_outlook_api and outlook_domains:
            ms_to, gmail_to = [], []
            for addr in to:
                domain = addr.rsplit("@", 1)[-1].lower() if "@" in addr else ""
                (ms_to if domain in outlook_domains else gmail_to).append(addr)
        else:
            ms_to, gmail_to = [], list(to)

        results = []
        if gmail_to:
            results.append(self._send_via_gmail(gmail_to, subject, plain, html))
        if ms_to:
            results.append(self._outlook_api_send(ms_to, subject, plain, html))

        if not results:
            log.warning("_send_email: no recipients after domain split — nothing sent")
            return False
        return any(results)

    def _send_via_gmail(self, to: list, subject: str, plain: str, html: str) -> bool:
        use_gmail_api = self.cfg.get("email.use_gmail_api", False)
        if not use_gmail_api:
            return self._smtp_send(to, subject, plain, html)

        accounts = self.cfg.get("email.gmail_accounts") or []
        if not accounts:
            # Backward-compatible single-account fallback — exactly the
            # old behavior, using email.from_addr + the default token file.
            accounts = [{"token_file": self.GMAIL_TOKEN_FILE,
                        "from_addr": self.cfg.get("email.from_addr", "")}]

        if len(accounts) == 1:
            ok = self._gmail_api_send(to, subject, plain, html, accounts[0])
            if ok:
                return True
            log.warning("Gmail API send failed — NOT falling back to SMTP "
                        "(SMTP ports are likely blocked on this network; "
                        "see log above for the Gmail API error)")
            return False

        # Multiple accounts: split recipients roughly evenly across them.
        # Each account only draws against ITS OWN daily quota — e.g. 120
        # recipients across 3 accounts = ~40/account/send instead of
        # 120/account/send, roughly TRIPLING effective daily capacity for
        # the same recipient list and send frequency.
        n = len(accounts)
        chunks = [to[i::n] for i in range(n)]  # round-robin split, not contiguous blocks
        results = []
        for account, chunk in zip(accounts, chunks):
            if not chunk:
                continue
            ok = self._gmail_api_send(chunk, subject, plain, html, account)
            results.append(ok)
            if not ok:
                log.error(f"Gmail API: send failed for account "
                          f"{account.get('from_addr','?')} — its "
                          f"{len(chunk)} recipients did NOT receive this "
                          f"email (other accounts' recipients are "
                          f"unaffected — see per-account log lines above)")
        if not results:
            return False
        if not all(results):
            log.warning(f"Gmail API: {results.count(False)}/{len(results)} "
                       f"account(s) failed this send — partial delivery, "
                       f"not falling back to SMTP for the failed portion "
                       f"(SMTP ports are likely blocked on this network)")
        return any(results)

    # ── Gmail API send (HTTPS only — no SMTP port needed) ────
    # Default/legacy single-account token file — still used when
    # email.gmail_accounts isn't configured (backward compatible).
    GMAIL_TOKEN_FILE = os.path.join("data", "gmail_token.json")
    GMAIL_TOKEN_URL  = "https://oauth2.googleapis.com/token"
    GMAIL_SEND_URL   = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"

    def _gmail_get_access_token(self, token_file: str) -> str | None:
        """Returns a valid access token for the Gmail account whose
        refresh_token lives in token_file, refreshing it if expired/
        missing. Tokens are cached PER ACCOUNT (keyed by token_file path)
        in self._gmail_tokens, so multiple accounts don't clobber each
        other's cached access token — no browser interaction needed for
        any of this, ever, once each account's token file exists."""
        if _requests is None:
            log.error("Gmail API: 'requests' library not available")
            return None

        cached = self._gmail_tokens.get(token_file)
        if cached and time.time() < cached["expiry"] - 60:
            return cached["access_token"]

        if not os.path.exists(token_file):
            log.error(f"Gmail API: token file not found at {token_file}. "
                       f"Run gmail_oauth_setup.py once (pointing its output "
                       f"at this path) to authorize this account.")
            return None

        try:
            with open(token_file) as f:
                tok = json.load(f)
        except Exception as e:
            log.error(f"Gmail API: failed to read token file {token_file}: {e}")
            return None

        try:
            resp = _requests.post(self.GMAIL_TOKEN_URL, data={
                "client_id":     tok["client_id"],
                "client_secret": tok["client_secret"],
                "refresh_token": tok["refresh_token"],
                "grant_type":    "refresh_token",
            }, timeout=20)
        except Exception as e:
            log.error(f"Gmail API: token refresh request failed for "
                      f"{token_file}: {e}")
            return None

        if resp.status_code != 200:
            log.error(f"Gmail API: token refresh failed for {token_file} "
                       f"({resp.status_code}): {resp.text[:300]}")
            return None

        data = resp.json()
        self._gmail_tokens[token_file] = {
            "access_token": data["access_token"],
            "expiry": time.time() + int(data.get("expires_in", 3600)),
        }
        return data["access_token"]

    def _gmail_api_send(self, to: list, subject: str,
                        plain: str, html: str, account: dict = None) -> bool:
        if _requests is None:
            log.error("Gmail API: 'requests' library not installed")
            return False

        account = account or {"token_file": self.GMAIL_TOKEN_FILE,
                              "from_addr": self.cfg.get("email.from_addr", "")}
        token_file = account.get("token_file", self.GMAIL_TOKEN_FILE)
        from_addr  = account.get("from_addr", "")

        # Diagnostic: a personal Gmail account caps at ~500 recipients/day
        # TOTAL across all messages (Workspace accounts: ~2,000/day). This
        # is a Gmail mailbox-level limit — separate from, and NOT raised
        # by, anything in Cloud Console's OAuth/API quota pages. A large
        # recipient list times a frequent send schedule can blow through
        # this in a single send, which then fails EVERY subsequent retry
        # for the rest of the day (this isn't a transient burst — backoff
        # retries below won't help once the daily cap is actually gone).
        # Warn loudly here so a wall of bare 429s doesn't have to be
        # re-diagnosed from scratch next time. (With multiple accounts
        # configured, this threshold applies PER ACCOUNT, post-split —
        # that's the whole point of splitting.)
        if len(to) > 50:
            log.warning(
                f"Gmail API ({from_addr}): sending to {len(to)} individual "
                f"recipients in one go. A personal Gmail account's daily "
                f"sending cap is ~500 recipients TOTAL (Workspace: ~2,000) "
                f"— at this recipient count, even a few sends/day can "
                f"exhaust it and then fail every attempt until the next "
                f"day. Consider a Google Group, or adding another account "
                f"to email.gmail_accounts to split the load further.")

        access_token = self._gmail_get_access_token(token_file)
        if not access_token:
            return False

        if not from_addr:
            log.error(f"Gmail API: no from_addr configured for account "
                      f"using token file {token_file}")
            return False

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = f"TPNODL Monitor <{from_addr}>"
        msg["To"]      = ", ".join(to)
        msg.attach(MIMEText(plain, "plain"))
        msg.attach(MIMEText(html,  "html"))

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")

        # FIX: previously a single 429 (User-rate-limit-exceeded — Gmail's
        # per-user burst quota, easily tripped when a violation-alert
        # email and the hourly management-report email land in the same
        # second) just failed the send outright with no retry. Google's
        # own guidance for this exact error is retry-with-backoff — the
        # "Retry after <timestamp>" in the error body is frequently
        # already in the past by the time we see it (the quota window is
        # shorter than network/processing latency), so a short retry
        # almost always succeeds on the 2nd or 3rd attempt rather than
        # needing to wait for whatever absolute time the error mentions.
        RETRY_DELAYS = (3, 8, 20)  # seconds; 3 attempts total beyond the first
        attempt = 0
        while True:
            try:
                resp = _requests.post(
                    self.GMAIL_SEND_URL,
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Content-Type":  "application/json",
                    },
                    json={"raw": raw},
                    timeout=20,
                )
            except Exception as e:
                log.error(f"Gmail API ({from_addr}): send request failed: {e}")
                return False

            if resp.status_code == 200:
                log.info(f"Email sent via Gmail API ({from_addr}) to {to}: {subject[:60]}")
                return True

            if resp.status_code == 429 and attempt < len(RETRY_DELAYS):
                delay = RETRY_DELAYS[attempt]
                log.warning(f"Gmail API ({from_addr}): rate-limited (429) — "
                           f"retrying in {delay}s "
                           f"(attempt {attempt + 1}/{len(RETRY_DELAYS)})")
                time.sleep(delay)
                attempt += 1
                continue

            log.error(f"Gmail API ({from_addr}): send failed ({resp.status_code}): {resp.text[:300]}")
            if resp.status_code == 401:
                # Access token rejected — force refresh on next attempt,
                # for THIS account only (other accounts' cached tokens are
                # unaffected since they're keyed separately by token_file)
                self._gmail_tokens.pop(token_file, None)
            return False

    # ── Microsoft Graph API send (Outlook/Microsoft-365 recipients) ──
    # One-time setup: run outlook_oauth_setup.py once (any machine with a
    # browser) to authorize and generate data/outlook_token.json — uses
    # the OAuth2 device-code flow, no redirect URI / web server needed.
    # After that, sending needs no further interaction.
    MS_TOKEN_FILE = os.path.join("data", "outlook_token.json")
    MS_SEND_URL   = "https://graph.microsoft.com/v1.0/me/sendMail"

    def _ms_token_url(self, tenant: str) -> str:
        return f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"

    def _outlook_get_access_token(self) -> str | None:
        """Returns a valid Microsoft Graph access token, refreshing it if
        expired/missing. Uses the refresh_token saved by
        outlook_oauth_setup.py — no browser interaction needed for this,
        ever, exactly mirroring the Gmail token-refresh pattern above."""
        if _requests is None:
            log.error("Outlook API: 'requests' library not available")
            return None

        if self._ms_access_token and time.time() < self._ms_token_expiry - 60:
            return self._ms_access_token

        if not os.path.exists(self.MS_TOKEN_FILE):
            log.error(f"Outlook API: token file not found at {self.MS_TOKEN_FILE}. "
                       f"Run outlook_oauth_setup.py once to authorize.")
            return None

        try:
            with open(self.MS_TOKEN_FILE) as f:
                tok = json.load(f)
        except Exception as e:
            log.error(f"Outlook API: failed to read token file: {e}")
            return None

        try:
            resp = _requests.post(self._ms_token_url(tok.get("tenant", "common")), data={
                "client_id":     tok["client_id"],
                "refresh_token": tok["refresh_token"],
                "grant_type":    "refresh_token",
                "scope":         "https://graph.microsoft.com/Mail.Send offline_access",
            }, timeout=20)
        except Exception as e:
            log.error(f"Outlook API: token refresh request failed: {e}")
            return None

        if resp.status_code != 200:
            log.error(f"Outlook API: token refresh failed ({resp.status_code}): "
                       f"{resp.text[:300]}")
            return None

        data = resp.json()
        self._ms_access_token = data["access_token"]
        self._ms_token_expiry = time.time() + int(data.get("expires_in", 3600))
        # Microsoft rotates refresh tokens on use — persist the new one or
        # the NEXT refresh attempt will fail with an invalid_grant error.
        new_refresh = data.get("refresh_token")
        if new_refresh and new_refresh != tok.get("refresh_token"):
            tok["refresh_token"] = new_refresh
            try:
                with open(self.MS_TOKEN_FILE, "w") as f:
                    json.dump(tok, f, indent=2)
            except Exception as e:
                log.error(f"Outlook API: failed to persist rotated refresh "
                          f"token (next refresh may fail): {e}")
        return self._ms_access_token

    def _outlook_api_send(self, to: list, subject: str,
                          plain: str, html: str) -> bool:
        if _requests is None:
            log.error("Outlook API: 'requests' library not installed")
            return False

        access_token = self._outlook_get_access_token()
        if not access_token:
            return False

        # Same diagnostic threshold as the Gmail path — Microsoft 365 caps
        # are typically higher (~10,000/day for Business plans) but still
        # finite, and the same "same 120 people, N times/day" math applies.
        if len(to) > 50:
            log.warning(
                f"Outlook API: sending to {len(to)} individual recipients "
                f"in one go. Microsoft 365 sending caps are typically "
                f"higher than personal Gmail's but still finite and still "
                f"counted per-recipient-per-send, same as Gmail.")

        body = {
            "message": {
                "subject": subject,
                "body": {"contentType": "HTML", "content": html},
                "toRecipients": [{"emailAddress": {"address": a}} for a in to],
            },
            "saveToSentItems": "true",
        }

        RETRY_DELAYS = (3, 8, 20)
        attempt = 0
        while True:
            try:
                resp = _requests.post(
                    self.MS_SEND_URL,
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Content-Type":  "application/json",
                    },
                    json=body,
                    timeout=20,
                )
            except Exception as e:
                log.error(f"Outlook API: send request failed: {e}")
                return False

            # Graph's sendMail returns 202 Accepted on success, not 200
            if resp.status_code == 202:
                log.info(f"Email sent via Outlook/Graph API to {to}: {subject[:60]}")
                return True

            if resp.status_code == 429 and attempt < len(RETRY_DELAYS):
                delay = RETRY_DELAYS[attempt]
                log.warning(f"Outlook API: rate-limited (429) — retrying in "
                           f"{delay}s (attempt {attempt + 1}/{len(RETRY_DELAYS)})")
                time.sleep(delay)
                attempt += 1
                continue

            log.error(f"Outlook API: send failed ({resp.status_code}): {resp.text[:300]}")
            if resp.status_code == 401:
                self._ms_access_token = None
                self._ms_token_expiry = 0
            return False

    def _smtp_send(self, to: list, subject: str,
                   plain: str, html: str) -> bool:
        from_addr = self.cfg.get("email.from_addr", "")
        password  = self.cfg.get("email.password", "")
        host      = self.cfg.get("email.smtp_host", "smtp.gmail.com")
        port      = int(self.cfg.get("email.smtp_port", 587))
        tls_cfg   = self.cfg.get("email.tls", "STARTTLS")

        if not from_addr:
            log.error("Email: sender address not configured")
            return False
        if not password:
            log.error("Email: password not configured — set App Password in Notifications")
            return False

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = f"TPNODL Monitor <{from_addr}>"
        msg["To"]      = ", ".join(to)
        msg.attach(MIMEText(plain, "plain"))
        msg.attach(MIMEText(html,  "html"))

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode    = ssl.CERT_NONE

        use_ssl = (port == 465 or tls_cfg == "SSL")
        no_auth = (port == 25 and not password)  # internal relay — no auth needed
        TIMEOUT = 20

        try:
            if use_ssl:
                with smtplib.SMTP_SSL(host, port, context=ctx, timeout=TIMEOUT) as s:
                    if not no_auth:
                        s.login(from_addr, password)
                    s.sendmail(from_addr, to, msg.as_string())
            else:
                with smtplib.SMTP(host, port, timeout=TIMEOUT) as s:
                    s.ehlo()
                    if not no_auth:
                        try:
                            s.starttls(context=ctx)
                            s.ehlo()
                        except smtplib.SMTPException as tls_err:
                            log.warning(f"STARTTLS failed ({tls_err}) — trying plain")
                        s.login(from_addr, password)
                    s.sendmail(from_addr, to, msg.as_string())
            log.info(f"Email sent to {to}: {subject[:60]}")
            return True

        except smtplib.SMTPAuthenticationError:
            log.error(
                "SMTP Auth failed. For Gmail use App Password:\n"
                "  myaccount.google.com → Security → 2-Step Verification → App passwords\n"
                f"  Host: {host}:{port}  From: {from_addr}"
            )
        except (ConnectionRefusedError, OSError, TimeoutError) as e:
            log.error(
                f"SMTP connection failed: {e}\n"
                f"  Cannot reach {host}:{port} — possible causes:\n"
                f"  1. Firewall/proxy blocking outbound port {port}\n"
                f"  2. Try port 465 (SSL) instead of 587 (STARTTLS)\n"
                f"  3. Check if smtp.gmail.com is accessible from this network\n"
                f"  Tip: test with: telnet {host} {port}"
            )
        except smtplib.SMTPException as e:
            log.error(f"SMTP error: {e}")
        except Exception as e:
            log.error(f"Email exception: {e}")
        return False

    # ── Templates ─────────────────────────────────────────────
    @staticmethod
    def _plain_body(violations: list) -> str:
        lines = [
            f"TPNODL Violation Alert — {len(violations)} violation(s)",
            f"Time: {datetime.now().strftime('%d-%b-%Y %H:%M:%S')}",
            "=" * 60,
        ]
        for v in violations:
            label = TYPE_LABELS.get(v.get("type",""), v.get("type",""))
            lines += [
                f"\n{label}",
                f"  Feeder  : {v.get('Feeder','')}",
                f"  Asset   : {v.get('AssetCode','')}",
                f"  Location: {v.get('Circle','')} / {v.get('Division','')}",
                f"  GSS     : {v.get('Gss','')}",
                f"  Detail  : {v.get('detail','')}",
                f"  Time    : {v.get('timestamp','')[:19].replace('T',' ')}",
            ]
        return "\n".join(lines)

    @staticmethod
    def _simple_html(body: str) -> str:
        return (f'<html><body style="font-family:Arial;padding:20px">'
                f'<pre style="font-size:13px">{body}</pre></body></html>')

    @staticmethod
    def _html_body(violations: list) -> str:
        now_str = datetime.now().strftime('%d-%b-%Y %H:%M:%S')

        def card(v):
            color  = TYPE_COLORS.get(v.get("type",""), "#888")
            label  = TYPE_LABELS.get(v.get("type",""), v.get("type",""))
            imax   = max(float(v.get("Ir") or 0), float(v.get("Iy") or 0), float(v.get("Ib") or 0))
            vavg   = (float(v.get("Vr") or 0) + float(v.get("Vy") or 0) + float(v.get("Vb") or 0)) / 3
            feeder = v.get("Feeder","") or v.get("AssetCode","")
            loc    = f"{v.get('Circle','')} / {v.get('Division','')} | GSS: {v.get('Gss','')}"
            detail = v.get("detail","")
            ts     = v.get("timestamp","")[:16].replace("T", " ")
            rating = v.get("FeederRating")
            elec   = f"Vavg: {vavg:.3f} kV"
            if imax: elec += f" | Imax: {imax:.1f}A"
            if rating: elec += f" / {rating}A"

            return f"""
<table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:12px;border:1px solid #e0e0e0;border-radius:6px;border-left:4px solid {color};background:#ffffff;font-family:Arial,sans-serif">
  <tr>
    <td style="padding:12px 16px">
      <table width="100%" cellpadding="0" cellspacing="0">
        <tr>
          <td>
            <span style="background:{color};color:#fff;padding:2px 10px;border-radius:3px;font-size:11px;font-weight:bold">{label}</span>
            <span style="font-size:16px;font-weight:bold;color:#1a1a2e;margin-left:8px">{feeder}</span>
          </td>
          <td align="right" style="font-size:11px;color:#888;white-space:nowrap">{ts}</td>
        </tr>
        <tr><td colspan="2" style="padding-top:6px;font-size:12px;color:#555">{loc}</td></tr>
        <tr><td colspan="2" style="padding-top:6px;font-size:13px;color:#333">{detail}</td></tr>
        <tr><td colspan="2" style="padding-top:4px;font-size:11px;color:#888;font-family:monospace">{elec}</td></tr>
      </table>
    </td>
  </tr>
</table>"""

        cards = "\n".join(card(v) for v in violations)

        return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:16px;background:#f4f6f9;font-family:Arial,sans-serif">
<table width="100%" cellpadding="0" cellspacing="0" style="max-width:600px;margin:0 auto">
  <!-- Header -->
  <tr><td style="background:#0a1628;border-radius:8px 8px 0 0;padding:16px 20px">
    <table width="100%" cellpadding="0" cellspacing="0">
      <tr>
        <td><span style="color:#00c4ff;font-size:18px;font-weight:bold">⚡ TPNODL Alert</span><br>
            <span style="color:#8899bb;font-size:12px">Central PSCC — Realtime Load &amp; Voltage Monitor</span></td>
        <td align="right"><span style="background:#ff3d71;color:#fff;padding:4px 12px;border-radius:20px;font-size:13px;font-weight:bold">{len(violations)} Violation(s)</span></td>
      </tr>
    </table>
  </td></tr>
  <!-- Time bar -->
  <tr><td style="background:#141c35;padding:8px 20px;border-bottom:1px solid #1e2d4a">
    <span style="color:#aaa;font-size:12px">🕐 {now_str}</span>
  </td></tr>
  <!-- Cards -->
  <tr><td style="background:#f4f6f9;padding:16px 0">
    {cards}
  </td></tr>
  <!-- Footer -->
  <tr><td style="background:#0a1628;border-radius:0 0 8px 8px;padding:12px 20px;text-align:center">
    <span style="color:#556;font-size:11px">Automated alert from TPNODL PSCC Monitoring System. Log in to acknowledge.</span>
  </td></tr>
</table>
</body></html>"""
