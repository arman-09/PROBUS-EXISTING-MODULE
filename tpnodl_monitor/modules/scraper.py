"""
modules/scraper.py — Probus HES Scraper (Clean v3)
====================================================
Auth : POST :8083/user/auth/login?loginMode=SENSE_ADMIN&dbOverride=true
       Body : {"userId":"1925","password":"..."}
Data : GET  :8091/meterdashboard/asset-latest-instant-param-data
Meta : loaded from data/meta_lookup.json (Circle/Division/Gss/Feeder)
"""
import time, logging, json, os, urllib3
from datetime import datetime, timedelta
import requests

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
log = logging.getLogger("scraper")

PROBUS_HOST = "tpnodl.probussense.com"
LIVE_FILE = "data/live_data.json"
META_FILE = "data/meta_lookup.json"
IP_CACHE_FILE = "data/probus_ip.txt"
META_REFRESH_HRS = 6


def _resolve_probus_host(cfg) -> str:
    """
    Resolve Probus hostname. Uses:
    1. config override: probus.host_override (e.g. direct IP)
    2. DNS resolution of tpnodl.probussense.com
    3. Cached IP from last successful resolution
    Returns hostname/IP to use, or original hostname as fallback.
    """
    override = cfg.get("probus.host_override", "")
    if override:
        log.info(f"Probus host override: {override}")
        return override

    import socket
    try:
        ip = socket.gethostbyname(PROBUS_HOST)
        # Cache the resolved IP
        try:
            with open(IP_CACHE_FILE, "w") as f:
                f.write(ip)
        except Exception:
            pass
        return PROBUS_HOST  # DNS works — use hostname
    except Exception:
        # DNS failed — try cached IP
        try:
            if os.path.exists(IP_CACHE_FILE):
                ip = open(IP_CACHE_FILE).read().strip()
                if ip:
                    log.warning(f"DNS failed for {PROBUS_HOST} — using cached IP {ip}")
                    return ip
        except Exception:
            pass
        log.error(f"Cannot resolve {PROBUS_HOST} and no cached IP available")
        return PROBUS_HOST  # return original, will fail with clearer error


class ProbusScraper:
    def __init__(self, cfg):
        self.cfg              = cfg
        self._token           = None
        self._token_expiry    = 0
        self._last_data       = []
        self._last_fetch_time = None
        self._meta_lookup     = {}
        self._meta_fetched_at = 0

        # Resolve host (uses DNS or cached IP fallback)
        host = _resolve_probus_host(cfg)
        self._auth_url = f"https://{host}:8083/user/auth/login"
        self._data_url = f"https://{host}:8091/meterdashboard/asset-latest-instant-param-data"
        self._asset_url= f"https://{host}:8082/assets/getOrgHierarchy"

        self._session = requests.Session()
        self._session.verify = False
        self._session.headers.update({
            "User-Agent":   "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/148.0.0.0",
            "Accept":       "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin":       f"https://{PROBUS_HOST}",
            "Referer":      f"https://{PROBUS_HOST}/",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-site",
        })
        os.makedirs("data", exist_ok=True)
        self._load_cached()

    # ── Cache ─────────────────────────────────────────────────
    def _load_cached(self):
        # Load meta first
        if os.path.exists(META_FILE):
            try:
                obj = json.load(open(META_FILE, encoding="utf-8"))
                self._meta_lookup     = obj.get("lookup", {})
                self._meta_fetched_at = obj.get("fetched_at_epoch", 0)
                log.info(f"Meta lookup loaded: {len(self._meta_lookup)} assets")
            except Exception as e:
                log.warning(f"Meta load: {e}")

        # Load live data
        if os.path.exists(LIVE_FILE):
            try:
                obj = json.load(open(LIVE_FILE, encoding="utf-8"))
                data = obj.get("data", [])
                self._last_fetch_time = obj.get("fetched_at")
                # Merge meta immediately so startup dashboard is populated
                self._last_data = self._merge_meta(data)
                log.info(f"Live cache loaded: {len(self._last_data)} records (meta merged)")
            except Exception as e:
                log.warning(f"Live cache load: {e}")

    def _save_live(self, data):
        with open(LIVE_FILE, "w", encoding="utf-8") as f:
            json.dump({"fetched_at": datetime.now().isoformat(),
                       "count": len(data), "data": data}, f,
                      ensure_ascii=False, indent=2)

    def _save_meta(self):
        with open(META_FILE, "w", encoding="utf-8") as f:
            json.dump({"fetched_at": datetime.now().isoformat(),
                       "fetched_at_epoch": self._meta_fetched_at,
                       "count": len(self._meta_lookup),
                       "lookup": self._meta_lookup}, f,
                      ensure_ascii=False, indent=2)

    # ── Auth ──────────────────────────────────────────────────
    def _login(self) -> bool:
        username = self.cfg.get("scraper.username", "")
        password = self.cfg.get("scraper.password", "")
        if not username or not password:
            log.error("No credentials — set in Notifications > Probus Login")
            return False
        role = self.cfg.get("scraper.role", "Sense Admin")
        mode = role.upper().replace(" ", "_")
        url  = f"{self._auth_url}?loginMode={mode}&dbOverride=true"
        try:
            r = self._session.post(url, json={"userId": username, "password": password},
                                   timeout=self.cfg.get("scraper.timeout_seconds", 30),
                                   verify=False)
            log.info(f"Login HTTP {r.status_code}")
            if r.status_code not in (200, 201):
                log.error(f"Login failed: {r.text[:200]}")
                return False
            body  = r.json()
            token = (body.get("token") or body.get("access_token") or
                     body.get("accessToken") or body.get("jwt") or
                     (body.get("data") or {}).get("token") or
                     (body.get("data") or {}).get("accessToken"))
            if not token:
                log.error(f"Login 200 but no token. Keys: {list(body.keys())}")
                return False
            self._token        = token
            self._token_expiry = time.time() + 3500
            self._session.headers["Authorization"] = f"Bearer {token}"
            log.info("Probus login OK ✓")
            return True
        except Exception as e:
            log.error(f"Login error: {e}")
            return False

    def _ensure_auth(self) -> bool:
        if self._token and time.time() < self._token_expiry:
            return True
        return self._login()

    # ── Meta refresh ──────────────────────────────────────────
    def _refresh_meta_if_stale(self):
        age = (time.time() - self._meta_fetched_at) / 3600
        if self._meta_lookup and age < META_REFRESH_HRS:
            return
        log.info("Refreshing asset metadata...")
        now   = datetime.now()
        start = (now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        end   = now.strftime("%Y-%m-%d %H:%M:%S")
        urls  = [
            f"https://{PROBUS_HOST}:8091/meterdashboard/latest-voltage-current-trend",
            f"https://{PROBUS_HOST}:8091/meterdashboard/latest-voltage-current-trend-list",
        ]
        for url in urls:
            try:
                r = self._session.get(url,
                    params={"startTime": start, "endTime": end, "entityId": ""},
                    timeout=30, verify=False)
                if r.status_code != 200:
                    continue
                body = r.json()
                rows = (body if isinstance(body, list) else
                        body.get("data") or body.get("result") or body.get("rows") or [])
                if not rows:
                    continue
                sample = rows[0] if rows else {}
                if any(k in sample for k in ("Circle","circle","circleName","Division","division")):
                    self._build_meta_from_rows(rows)
                    log.info(f"Meta refreshed from API: {len(self._meta_lookup)} assets")
                    return
            except Exception as e:
                log.warning(f"Meta refresh {url}: {e}")

    def _build_meta_from_rows(self, rows: list):
        def pick(row, *keys):
            for k in keys:
                v = row.get(k)
                if v and str(v).strip():
                    return str(v).strip()
            return ""
        lookup = {}
        for row in rows:
            ac = pick(row,"AssetCode","assetCode","code","name")
            if not ac:
                continue
            lookup[ac] = {
                "Circle":     pick(row,"Circle","circle","circleName"),
                "Division":   pick(row,"Division","division","divisionName"),
                "Gss":        pick(row,"Gss","gss","gssName","substation"),
                "Feeder":     pick(row,"Feeder","feeder","feederName","assetName"),
                "FeederType": pick(row,"FeederType","feederType","connectionType","type"),
            }
        if lookup:
            self._meta_lookup     = lookup
            self._meta_fetched_at = time.time()
            self._save_meta()

    # ── Fetch ─────────────────────────────────────────────────
    def fetch(self) -> list:
        if not self._ensure_auth():
            log.error("Auth failed — check credentials in Notifications tab")
            return self._last_data
        self._refresh_meta_if_stale()
        now   = datetime.now()
        start = (now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        end   = now.strftime("%Y-%m-%d %H:%M:%S")
        try:
            r = self._session.get(self._data_url,
                params={"startTime": start, "endTime": end, "entityId": ""},
                timeout=self.cfg.get("scraper.timeout_seconds", 30), verify=False)
            if r.status_code == 401:
                self._token = None
                if not self._login():
                    return self._last_data
                r = self._session.get(self._data_url,
                    params={"startTime": start, "endTime": end, "entityId": ""},
                    timeout=30, verify=False)
            if r.status_code not in (200, 201):
                log.error(f"Data fetch {r.status_code}: {r.text[:200]}")
                return self._last_data
            body = r.json()
            raw  = (body if isinstance(body, list) else
                    body.get("data") or body.get("result") or
                    body.get("rows") or body.get("content") or [])
            if not raw:
                log.warning(f"Empty data. Keys: {list(body.keys()) if isinstance(body,dict) else type(body)}")
                return self._last_data
            data = [self._norm(row) for row in raw if isinstance(row, dict)]
            data = self._merge_meta(data)
            self._last_data       = data
            self._last_fetch_time = now.isoformat()
            self._save_live(data)
            log.info(f"Fetched {len(data)} records ✓")
            return data
        except Exception as e:
            log.error(f"Fetch error: {e}")
            return self._last_data

    @staticmethod
    def _norm(row: dict) -> dict:
        def f(*keys):
            for k in keys:
                v = row.get(k)
                if v is not None:
                    try: return float(v)
                    except: pass
            return 0.0
        return {
            "AssetCode":     str(row.get("name") or row.get("code") or row.get("assetCode") or row.get("AssetCode") or ""),
            "ServerTime":    str(row.get("serverTime") or row.get("ServerTime") or row.get("timestamp") or ""),
            "Vr": f("vr","Vr"),  "Vy": f("vy","Vy"),  "Vb": f("vb","Vb"),
            "Ir": f("ir","Ir"),  "Iy": f("iy","Iy"),  "Ib": f("ib","Ib"),
            "ActivePower":   f("activePower","ActivePower"),
            "ReactivePower": f("reactivePower","ReactivePower"),
            "ApparentPower": f("apparentPower","ApparentPower"),
            "Circle":"", "Division":"", "Gss":"", "Feeder":"",
            "FeederType":"", "IsBusCoupler": False,
        }

    def set_feeder_master(self, fm):
        """Called from app.py so scraper can use FM as authoritative source."""
        self._fm = fm

    def _merge_meta(self, data: list) -> list:
        """
        Merge Circle/Division/Gss/Feeder into each live row.
        Priority: Feeder Master (user-curated) > meta_lookup (auto from Probus).
        This ensures manually corrected feeder names always win.
        """
        fm = getattr(self, "_fm", None)
        for row in data:
            ac = row.get("AssetCode","")

            # 1. Try Feeder Master first (most accurate — user-curated)
            fm_entry = fm.lookup(ac) if fm else None
            if fm_entry and fm_entry.get("FeederName"):
                row["Circle"]   = fm_entry.get("CircleName","")
                row["Division"] = fm_entry.get("DivisionName","")
                row["Gss"]      = fm_entry.get("GssName","")
                row["Feeder"]   = fm_entry.get("FeederName","") or ac
                row["FeederType"] = fm_entry.get("FeederType","")
            else:
                # 2. Fall back to meta_lookup (auto-fetched from Probus)
                meta = self._meta_lookup.get(ac, {})
                row["Circle"]   = meta.get("Circle","")
                row["Division"] = meta.get("Division","")
                row["Gss"]      = meta.get("Gss","")
                row["Feeder"]   = meta.get("Feeder","") or ac
                row["FeederType"] = meta.get("FeederType","")

            fn = row["Feeder"].upper()
            row["IsBusCoupler"] = "BUS" in fn and ("COUPL" in fn or "BC" in fn)
        return data

    @property
    def last_data(self): return self._last_data
    @property
    def last_fetch_time(self): return self._last_fetch_time

    def status(self) -> dict:
        return {
            "authenticated": bool(self._token and time.time() < self._token_expiry),
            "last_fetch":    self._last_fetch_time,
            "record_count":  len(self._last_data),
            "meta_count":    len(self._meta_lookup),
        }
    def invalidate_meta(self):
        self._meta_fetched_at = 0
