"""
modules/config.py — Persistent JSON configuration manager
All settings read/write here. Thread-safe.
"""

import json, os, threading, logging
from copy import deepcopy

try:
    import modules.secret_vault as _vault
    _VAULT_AVAILABLE = True
except ImportError:
    _VAULT_AVAILABLE = False

log = logging.getLogger("config")

# Fields that get DPAPI-encrypted on disk (see secret_vault.py) — these
# are exactly the credentials that would let someone impersonate you or
# pivot into your accounts/SCADA system if config.json were copied off
# this machine. In-memory (self._data) always holds plaintext; only the
# file on disk holds the encrypted form. get()/set() callers throughout
# the rest of the app are completely unaffected.
SENSITIVE_KEYS = (
    "scraper.password",
    "email.password",
    "whatsapp.twilio_token",
    "whatsapp.meta_access_token",
)

DEFAULTS = {
    "scraper": {
        "base_url": "https://tpnodl.probussense.com",
        "username": "1925",
        "password": "",
        "role": "Sense Admin",
        "interval_minutes": 10,
        "timeout_seconds": 30,
        "session_token": "",
        "token_expiry": 0
    },
    "voltage": {
        "vn_kv": 33.0,
        "ov_pct": 6.0,
        "uv_pct": 9.0,
        "load_threshold_pct": 100.0,
        "feeder_off_threshold_a": 1.0,
        # Consecutive scan cycles a meter must be absent from live data
        # before a COMM_DOWN alert is created. Default 2 = 4 minutes at
        # the standard 2-min polling interval. Set to 3 if false positives
        # are seen on brief SCADA connectivity hiccups.
        "comm_miss_threshold": 2,
        # Consecutive cycles ALL 6 parameters (Vr/Vy/Vb/Ir/Iy/Ib) must be
        # identical before declaring "Frozen Data / Meter OFF". Default 5 =
        # 10 minutes at the standard 2-min polling interval.
        "frozen_cycles_threshold": 5,
        # Safety-net timeout (minutes) for BC-tracked feeder restoration —
        # see violation.py's BusCouplerDiversionDetector.update(). Forces
        # a LOAD_RESTORED record through if the feeder's own current has
        # clearly normalized but the BC-load-share math never conclusively
        # confirms a reduction — prevents indefinite stuck state where a
        # genuinely-restored feeder never gets a restoration record at all.
        "bc_restore_force_timeout_min": 10,
        "sudden_drop_pct": 20.0,
        "sudden_raise_pct": 20.0,
        "trend_window_samples": 6,
        # Solar Plant feeders: 0A is a NORMAL state (night, cloud cover, rain,
        # maintenance) — cannot be reliably distinguished from a real fault
        # using current alone with a fixed time window. Instead use a tighter
        # threshold (filters out daytime self-consumption/leakage current)
        # plus a confirmation wait (filters out transient cloud-cover dips).
        # OFF only fires after the LOWER current persists continuously for
        # the full wait period — and the alert's timestamp is backdated to
        # the FIRST cycle the low reading was observed, not when the wait
        # period completed.
        "solar_off_threshold_a": 0.15,
        "solar_off_wait_minutes": 10
    },
    "email": {
        "enabled": False,
        "smtp_host": "smtp.gmail.com",
        "smtp_port": 587,
        "tls": "STARTTLS",
        "from_addr": "",
        "password": "",
        "recipients": [],
        "subject_prefix": "[TPNODL ALERT]",
        "cooldown_minutes": 30,
        # If True, send via Gmail API (HTTPS/443) instead of SMTP (587/465).
        # Use this when SMTP ports are blocked by the network firewall but
        # normal HTTPS traffic works fine. Requires running
        # gmail_oauth_setup.py once to generate data/gmail_token.json —
        # see that script's docstring for setup steps.
        "use_gmail_api": False,
        # Multiple Gmail accounts to split recipients across, each with
        # its OWN independent daily sending quota. Empty list = backward-
        # compatible single-account behavior using from_addr + the
        # default data/gmail_token.json. To add more, run
        # gmail_oauth_setup.py again signed in as each additional
        # account, save its output to a distinct path, and add an entry
        # here: [{"token_file": "data/gmail_token.json",
        # "from_addr": "tpnodl.pscc1@gmail.com"},
        # {"token_file": "data/gmail_token_2.json",
        # "from_addr": "tpnodl.pscc2@gmail.com"}]. Recipients are split
        # round-robin across however many accounts are listed — e.g. 120
        # recipients across 3 accounts = ~40/account/send, roughly
        # tripling effective daily capacity for the same recipient list.
        "gmail_accounts": [],
        # If True, recipients whose domain matches outlook_domains are
        # routed through Microsoft Graph instead of Gmail — a SEPARATE
        # quota pool, and better deliverability for those recipients since
        # they then receive mail natively from an Outlook-ecosystem
        # sender instead of Gmail/Google-Group-relayed mail (which commonly
        # fails Microsoft's strict DMARC/SPF/DKIM alignment checks).
        # Requires running outlook_oauth_setup.py once to generate
        # data/outlook_token.json.
        "use_outlook_api": False,
        # Domains routed via Microsoft Graph when use_outlook_api is True.
        # Defaults to the consumer Microsoft domains — add your own org's
        # Microsoft 365 domain(s) here too if recipients use a custom
        # domain hosted on Microsoft 365 rather than @outlook.com itself.
        "outlook_domains": ["outlook.com", "hotmail.com", "live.com", "msn.com"]
    },
    "whatsapp": {
        "enabled": False,
        "provider": "twilio",
        "twilio_sid": "",
        "twilio_token": "",
        "twilio_from": "whatsapp:+14155238886",
        "meta_phone_id": "",
        "meta_access_token": "",
        "recipients": [],
        "cooldown_minutes": 30
    },
    "contacts": [],
    "auth": {
        # Salted hash only (werkzeug.security.generate_password_hash) —
        # the plaintext password is never stored anywhere. Empty string
        # means "no password set yet" — see routes.py's /login route,
        # which prompts to SET a password on first access rather than
        # locking the operator out before one exists.
        "password_hash": ""
    }
}


class Config:
    def __init__(self, path: str):
        self._path = path
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(path) if os.path.dirname(path) else ".", exist_ok=True)
        self._data = deepcopy(DEFAULTS)
        self._load()

    def _load(self):
        if os.path.exists(self._path):
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                self._deep_merge(self._data, saved)
                # Disk holds DPAPI-encrypted values for SENSITIVE_KEYS (or
                # plain legacy values from before this protection existed —
                # unprotect() passes those through unchanged) — decrypt
                # into memory so the rest of the app sees plaintext exactly
                # as before, transparently.
                if _VAULT_AVAILABLE:
                    self._apply_sensitive(self._data, _vault.unprotect)
                log.info(f"Config loaded from {self._path}")
            except Exception as e:
                log.warning(f"Config load error: {e} — using defaults")
        else:
            self._save()

    def _apply_sensitive(self, data: dict, fn):
        """Walk each dotted key in SENSITIVE_KEYS and replace its value
        with fn(value) in place, if present and non-empty."""
        for key in SENSITIVE_KEYS:
            parts = key.split(".")
            d = data
            ok = True
            for p in parts[:-1]:
                if isinstance(d, dict) and p in d:
                    d = d[p]
                else:
                    ok = False
                    break
            if ok and isinstance(d, dict) and d.get(parts[-1]):
                d[parts[-1]] = fn(d[parts[-1]])

    def _save(self):
        # Encrypt a SEPARATE copy for disk — self._data (in memory) must
        # stay plaintext, since every other module in this app calls
        # cfg.get("email.password") etc. expecting the real value back.
        to_write = deepcopy(self._data)
        if _VAULT_AVAILABLE:
            self._apply_sensitive(to_write, _vault.protect)
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump(to_write, f, indent=2, ensure_ascii=False)

    def _deep_merge(self, base, override):
        for k, v in override.items():
            if k in base and isinstance(base[k], dict) and isinstance(v, dict):
                self._deep_merge(base[k], v)
            else:
                base[k] = v

    def get(self, key: str, default=None):
        """Dot-notation get: cfg.get('email.smtp_host')"""
        with self._lock:
            parts = key.split(".")
            d = self._data
            for p in parts:
                if isinstance(d, dict) and p in d:
                    d = d[p]
                else:
                    return default
            return d

    def set(self, key: str, value):
        """Dot-notation set + persist."""
        with self._lock:
            parts = key.split(".")
            d = self._data
            for p in parts[:-1]:
                d = d.setdefault(p, {})
            d[parts[-1]] = value
            self._save()

    def update_section(self, section: str, data: dict):
        """Replace an entire top-level section."""
        with self._lock:
            if section in self._data and isinstance(self._data[section], dict):
                self._deep_merge(self._data[section], data)
            else:
                self._data[section] = data
            self._save()
        log.info(f"Config section '{section}' updated")

    def all(self) -> dict:
        with self._lock:
            d = deepcopy(self._data)
        # Mask passwords
        for sec in ("scraper", "email", "whatsapp"):
            if sec in d and "password" in d[sec]:
                d[sec]["password"] = "***" if d[sec]["password"] else ""
            if sec == "whatsapp":
                for k in ("twilio_token", "meta_access_token"):
                    if k in d[sec]:
                        d[sec][k] = "***" if d[sec][k] else ""
        return d

    def all_raw(self) -> dict:
        with self._lock:
            return deepcopy(self._data)
