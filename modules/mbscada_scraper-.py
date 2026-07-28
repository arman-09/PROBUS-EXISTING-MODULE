"""
modules/mbscada_scraper.py — MB SCADA Weather Scraper (v6 — block-data API)
==============================================================================
REWRITTEN against the REAL confirmed API (captured via browser DevTools
Network tab on https://mbscada.com/tpnodl/block-average):

    GET https://mbscada.com/tpnodl/server/api/block-data?siteId=<ID>&date=<YYYY-MM-DD>
    Response: {"success": true, "data": [
        {"timeBlock": "0:0-14", "scheduleHour": 0, "scheduleMinuteLower": 0,
         "scheduleMinuteUpper": 14, "avgAT": 29.25, "avgRH": 88.98,
         "maxWS": 3.856, "rain": 0, "recordingCount": 172}, ...
    ]}

    avgAT  = average air temperature, °C
    avgRH  = average relative humidity, %
    maxWS  = max wind speed, m/s (convert ×3.6 for km/h)
    rain   = rainfall in this 15-min block, mm
    recordingCount = number of raw samples averaged into this block —
                     used for stale-sensor detection (see below)

No authentication was required for this GET endpoint in testing — if your
deployment DOES require a login, see _get_auth_headers() below; the old
JWT login flow is preserved but only used if a 401 is encountered.

SITE MANAGEMENT (replaces the static PSS_MAP/CIRCLE_MAP dicts from v5):
Sites are no longer hardcoded. They're loaded from data/weather_sites.json,
which the new Settings UI ("🌡 WMS Weather Station Configuration" tab)
reads/writes. Each site entry:
    {
      "site_id": "1004_01",          # the siteId query param
      "sheet_name": "TPNODL-Kalimandir PSS",  # for reference/matching exports
      "display_name": "Kalimandir PSS",
      "location": "Balasore (Kalimandir)",
      "circle": "Balasore",          # representative circle for aggregation
      "weight": 1.0,                 # contribution weight if multiple
                                      # sites map to the same circle
      "active": true
    }
New sites can be added entirely through the UI — no code change required,
matching your STLF module's design exactly.

MULTI-SITE PER CIRCLE:
If multiple active sites share the same `circle`, their readings are
weight-averaged for that circle's aggregate (see _aggregate_circles()).

STALE/NON-COMMUNICATING SENSOR DETECTION:
Per requirement: if a site's avgAT/avgRH/maxWS values are IDENTICAL
(within float tolerance) across 2+ CONSECUTIVE 15-min blocks, that
parameter is flagged as stale/non-communicating for that site, and
EXCLUDED from aggregation for the affected timeframe. A genuinely live
sensor essentially never reports the exact same float reading twice in a
row across 15-minute averaging windows — repeated identical values is the
signature of a frozen/disconnected sensor still echoing its last good
value (or a comms failure backfilling with a cached value).
"""

import os, json, time, sqlite3, threading, logging
import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger("mbscada_scraper")

# ── Confirmed real endpoints ────────────────────────────────────────────────
MBSCADA_API_BASE   = "https://mbscada.com/tpnodl/server/api"
API_BLOCK_DATA      = f"{MBSCADA_API_BASE}/block-data"
API_DOWNLOAD_REPORT = f"{MBSCADA_API_BASE}/download-block-report"
API_LOGIN           = f"{MBSCADA_API_BASE}/auth/login"   # only used as fallback on 401

SITES_CONFIG_FILE = "data/weather_sites.json"
CREDS_CONFIG_FILE = "data/weather_config.json"

POLL_INTERVAL_SEC = 300     # 5 min — matches the 15-min block granularity
                            # with margin; no need to poll faster than the
                            # data itself updates
STALE_BLOCK_THRESHOLD = 2   # 2+ identical consecutive blocks = stale sensor
FLOAT_TOLERANCE = 0.001     # treat values within this delta as "identical"


# ── Site configuration ──────────────────────────────────────────────────────

def load_sites() -> list:
    """
    Load configured weather stations from data/weather_sites.json.
    Returns [] if the file doesn't exist yet (first run, no sites
    configured) — callers should treat that as "nothing to fetch", not
    an error.
    """
    if not os.path.exists(SITES_CONFIG_FILE):
        return []
    try:
        with open(SITES_CONFIG_FILE) as f:
            data = json.load(f)
        return data.get("sites", [])
    except Exception as e:
        log.error(f"load_sites() failed: {e}")
        return []


def save_sites(sites: list) -> bool:
    """Persist the full site list (used by the Settings UI's Save button —
    replaces the entire list each time, same pattern as Feeder Master)."""
    try:
        os.makedirs(os.path.dirname(SITES_CONFIG_FILE), exist_ok=True)
        with open(SITES_CONFIG_FILE, "w") as f:
            json.dump({"sites": sites, "updated_at": datetime.now().isoformat()},
                      f, indent=2)
        return True
    except Exception as e:
        log.error(f"save_sites() failed: {e}")
        return False


def get_known_circles(sites: list) -> set:
    """All circles currently assigned to at least one active site."""
    return {s.get("circle","") for s in sites if s.get("active") and s.get("circle")}


# ── Credentials (only used if the API ever requires auth — see fetch_once) ──

def _load_credentials() -> dict:
    try:
        with open(CREDS_CONFIG_FILE) as f:
            cfg = json.load(f)
        return {"company":  cfg.get("mbscada_company", "TPNODL"),
                "username": cfg.get("mbscada_user", ""),
                "password": cfg.get("mbscada_password", "")}
    except Exception:
        return {}


_jwt: dict = {"token": None, "expires": None, "lock": threading.Lock()}


def _get_jwt() -> str | None:
    creds = _load_credentials()
    if not creds.get("username") or not creds.get("password"):
        return None
    with _jwt["lock"]:
        now = datetime.now()
        if _jwt["token"] and _jwt["expires"] and _jwt["expires"] > now:
            return _jwt["token"]
        try:
            resp = requests.post(API_LOGIN, json={
                "clientId": creds["company"], "username": creds["username"],
                "password": creds["password"]
            }, timeout=15, verify=False)
            resp.raise_for_status()
            token = resp.json().get("token")
            if token:
                _jwt["token"] = token
                _jwt["expires"] = now + timedelta(minutes=85)
                return token
        except Exception as e:
            log.warning(f"MB SCADA login failed: {e}")
    return None


# ── Block-data fetcher (the real, confirmed working API) ───────────────────

def fetch_site_block_data(site_id: str, date_str: str) -> list:
    """
    Fetch one site's 15-min block data for one date.
    date_str: "YYYY-MM-DD"
    Returns the raw `data` list from the API, or [] on any failure
    (network error, site not found, bad date, etc.) — callers should
    treat an empty list as "no data available", not necessarily an error
    worth surfacing loudly every cycle.
    """
    try:
        resp = requests.get(API_BLOCK_DATA, params={"siteId": site_id, "date": date_str},
                            timeout=20, verify=False)
        if resp.status_code == 401:
            # Confirmed-working endpoint needed no auth in testing, but
            # if a deployment DOES require it, retry once with a JWT.
            token = _get_jwt()
            if token:
                resp = requests.get(API_BLOCK_DATA,
                                    params={"siteId": site_id, "date": date_str},
                                    headers={"Authorization": f"Bearer {token}"},
                                    timeout=20, verify=False)
        resp.raise_for_status()
        body = resp.json()
        if not body.get("success"):
            log.warning(f"block-data for {site_id}@{date_str}: success=false")
            return []
        return body.get("data", [])
    except Exception as e:
        log.warning(f"fetch_site_block_data({site_id}, {date_str}) failed: {e}")
        return []


def test_connection(site_id: str = None) -> dict:
    """
    Test connectivity — fetches today's block data for one site (or the
    first configured site if none specified). Used by the Settings UI's
    "Test" button (per-site) and the general "Test Connection" check.
    """
    sites = load_sites()
    if site_id is None:
        if not sites:
            return {"ok": False, "error": "No weather stations configured yet"}
        site_id = sites[0]["site_id"]

    today = datetime.now().strftime("%Y-%m-%d")
    data = fetch_site_block_data(site_id, today)
    if not data:
        return {"ok": False, "error": f"No data returned for site {site_id} "
                                       f"on {today} — check siteId or try "
                                       f"yesterday's date (data may not have "
                                       f"started accumulating yet today)"}
    latest = data[-1]
    return {"ok": True, "site_id": site_id, "blocks_returned": len(data),
            "latest_block": latest}


# ── Stale/non-communicating sensor detection ────────────────────────────────

def _detect_stale_params(blocks: list) -> dict:
    """
    Per requirement: if avgAT/avgRH/maxWS is IDENTICAL (within float
    tolerance) across STALE_BLOCK_THRESHOLD+ CONSECUTIVE blocks, that
    specific parameter is flagged stale for the LATEST block — meaning a
    sensor failure is suspected and that parameter's latest value should
    be excluded from aggregation.

    Returns {"avgAT": bool, "avgRH": bool, "maxWS": bool} — True = stale,
    exclude this parameter's latest value.

    Checked independently per-parameter because a real-world sensor
    failure often affects one channel (e.g. the anemometer fails but
    temperature/humidity keep updating normally) — flagging the whole
    site dead would needlessly discard still-valid data.

    IMPORTANT (fix): two kinds of block must NEVER feed this check:
      - blocks with recordingCount in (0, None) — these are "no data has
        landed yet for this slot" (e.g. the slot is still in progress, or
        the upstream API pre-creates a stub row for a not-yet-elapsed
        block). They are not a reading at all, identical or otherwise.
      - blocks where the value is exactly 0 — those already have their
        own dedicated "zero reading excluded (likely sensor fault)"
        handling in get_circle_weather_now(). Letting them also feed this
        repeated-value check caused several genuine "not arrived yet"
        zero-stub blocks in a row to be misreported as a "non-communicating
        sensor" on top of the (also wrong) zero-exclusion message.
    Including either of the above here is exactly what produced false
    "3 zero reading(s) excluded" warnings any time the report's lookback
    window happened to include an in-progress or not-yet-populated block.
    """
    result = {"avgAT": False, "avgRH": False, "maxWS": False}
    real_blocks = [b for b in blocks if (b.get("recordingCount") or 0) > 0]
    if len(real_blocks) < STALE_BLOCK_THRESHOLD + 1:
        return result  # not enough confirmed history to judge yet

    recent = real_blocks[-(STALE_BLOCK_THRESHOLD + 1):]
    for param in ("avgAT", "avgRH", "maxWS"):
        vals = [b.get(param) for b in recent if b.get(param) is not None]
        # Exclude exact-zero — handled separately as a per-block fault,
        # not as a "frozen sensor" signal.
        vals = [v for v in vals if abs(v) > FLOAT_TOLERANCE]
        if len(vals) < STALE_BLOCK_THRESHOLD + 1:
            continue
        # Check if the LAST STALE_BLOCK_THRESHOLD+1 values are all
        # identical (within tolerance) to each other
        first = vals[0]
        all_same = all(abs(v - first) <= FLOAT_TOLERANCE for v in vals)
        if all_same:
            result[param] = True
    return result


# ── Per-circle aggregation (multi-site weighted average) ───────────────────

def _floor_to_quarter(dt: datetime) -> datetime:
    """Floor a datetime down to the most recent :00/:15/:30/:45 mark
    (seconds/microseconds zeroed too). This is the END boundary of the
    most recently COMPLETED 15-min block at/before dt — e.g. 01:07:32
    floors to 01:00:00, meaning the last completed block is 00:45-00:59."""
    minute = (dt.minute // 15) * 15
    return dt.replace(minute=minute, second=0, microsecond=0)


def _expected_block_starts(boundary: datetime) -> list:
    """
    Given the END boundary of the last completed block, return the 4
    (date_str, hour, minute_lower) tuples — in chronological order —
    identifying the exact 4 completed 15-min blocks that make up "the
    last 1 hour ending at boundary". This is what implements the rule:
        report for 01:00 → blocks ending 00:15,00:30,00:45,01:00
                            (i.e. starting 00:00,00:15,00:30,00:45, today)
        report for 00:00 → blocks ending 23:15,23:30,23:45,00:00
                            (i.e. starting 23:00,23:15,23:30,23:45,
                             PREVIOUS day)
    Crossing midnight is handled naturally since this works off real
    datetime arithmetic, not array indexing.
    """
    starts = []
    for i in range(4, 0, -1):
        start_dt = boundary - timedelta(minutes=15 * i)
        starts.append((start_dt.strftime("%Y-%m-%d"), start_dt.hour, start_dt.minute))
    return starts


def _index_blocks(blocks: list, date_str: str) -> dict:
    """Key a day's raw block list by (date_str, scheduleHour, scheduleMinuteLower)
    for exact-slot lookup."""
    idx = {}
    for b in blocks:
        try:
            idx[(date_str, int(b["scheduleHour"]), int(b["scheduleMinuteLower"]))] = b
        except (KeyError, TypeError, ValueError):
            continue
    return idx


def _get_window_blocks(site_id: str, as_of: datetime) -> list:
    """
    Return exactly the 4 completed 15-min blocks for `site_id` that make
    up "the last 1 hour ending at as_of" (per the report-slot semantics
    described above), in chronological order. Each entry is either the
    real block dict, or None if that slot has no data at all (e.g. not
    yet published) — None is NOT a zero reading and must never be treated
    as one.

    This replaces the old `blocks[-4:]` tail-slice, which silently broke
    whenever:
      - the report ran for an earlier slot than "right now" (mismatch
        between wall-clock "last 4 array entries" and the slot actually
        being reported)
      - the upstream array already contains a stub/in-progress entry for
        the current, not-yet-complete block
      - the window needed to span midnight (yesterday's tail + today's
        head) — the old code only ever fell back to yesterday's data
        wholesale if today had zero blocks, it never combined both.
    """
    boundary = _floor_to_quarter(as_of)
    expected = _expected_block_starts(boundary)

    needed_dates = {d for d, _, _ in expected}
    by_date = {}
    for d in needed_dates:
        by_date[d] = _index_blocks(fetch_site_block_data(site_id, d), d)

    out = []
    for d, h, m in expected:
        out.append(by_date.get(d, {}).get((d, h, m)))
    return out


def get_circle_weather_now(sites: list = None, as_of: "datetime|None" = None) -> dict:
    """
    Fetch the LAST 1 HOUR of 15-min blocks (4 blocks) for every active
    site, apply stale-sensor filtering, then aggregate per circle
    (weighted average across sites sharing the same circle):

        Temperature, Humidity — averaged across the last hour's blocks,
            excluding stale and exact-zero readings (fault signature)
        Wind Speed            — MAXIMUM across the last hour's blocks
            (matches "Max Wind Speed" semantics — a gust at any point in
            the hour should show, not get averaged away)
        Rainfall               — SUM across the last hour's blocks (each
            block's rain value is already that block's own 15-min total,
            so summing 4 blocks = the hour's total — genuine zeros are
            kept here, since "no rain in this block" is real data, not
            a fault)

    `as_of` — the report's NOMINAL time (e.g. the management report
    slot, "2026-06-23 01:00:00"). Defaults to datetime.now() for live
    dashboard use. The 4-block window is always the 4 completed blocks
    ending exactly at `as_of`'s most recent 15-min boundary — e.g.:
        as_of = 01:00 → blocks ending 00:15,00:30,00:45,01:00 (today)
        as_of = 00:00 → blocks ending 23:15,23:30,23:45,00:00
                         (23:00-23:45 blocks are PREVIOUS day)
    This is what FIXES the false "N zero reading(s) excluded" warnings:
    the window is now picked by exact slot match against `as_of`, not by
    slicing the tail of whatever array the API happened to return — so it
    can no longer accidentally include an in-progress/not-yet-published
    block just because that block currently sits last in the array.

    A slot with NO data at all (sensor hasn't published yet, or the
    report ran for a slot whose data hasn't landed) is excluded from the
    average SILENTLY — that is "no data yet", not a fault, and must never
    generate a "likely sensor fault" warning. Only a slot that DID report
    (recordingCount > 0) with an exact-zero value is a genuine suspected
    fault.

    Returns:
        {
          "Balasore": {
            "avg_temp_c": 28.4, "avg_humidity": 91.2,
            "total_rain_mm": 3.2, "max_wind_kmph": 14.8,
            "n_stations": 1, "stations": ["Kalimandir PSS"],
            "stale_warnings": []   # list of "{station}: {param} stale" strings
          }, ...
        }
    """
    sites = sites if sites is not None else load_sites()
    active_sites = [s for s in sites if s.get("active")]
    if not active_sites:
        return {}

    as_of = as_of or datetime.now()

    by_circle: dict = {}
    for site in active_sites:
        site_id = site["site_id"]
        circle  = site.get("circle","")
        weight  = float(site.get("weight", 1.0) or 1.0)
        name    = site.get("display_name", site_id)
        if not circle:
            continue

        # Exactly the 4 completed blocks for "last 1 hour ending at
        # as_of" — entries are the real block dict, or None if that slot
        # has no data published at all (NOT a zero reading).
        last_hour_blocks = _get_window_blocks(site_id, as_of)
        if not any(last_hour_blocks):
            continue  # nothing published for this site in this window at all

        # Stale/frozen-sensor check uses whichever blocks in the window
        # actually have data, for context (today's tail of real history).
        real_window_blocks = [b for b in last_hour_blocks if b is not None]
        stale = _detect_stale_params(real_window_blocks)

        by_circle.setdefault(circle, {
            "temp_vals": [], "rh_vals": [], "ws_vals": [], "rain_total": 0.0,
            "stations": [], "stale_warnings": [],
        })
        c = by_circle[circle]
        c["stations"].append(name)

        def _split_fault_vs_missing(param):
            """Among the 4 window slots, separate:
            - usable: real, non-zero readings
            - fault_count: slots that DID report (recordingCount>0) but
              with an exact-zero value — genuine suspected sensor fault
            - missing_count: slots with no data at all (None, or
              recordingCount==0) — simply not arrived yet, not a fault
            """
            usable, fault_count, missing_count = [], 0, 0
            for b in last_hour_blocks:
                if b is None or (b.get("recordingCount") or 0) == 0:
                    missing_count += 1
                    continue
                v = b.get(param)
                if v is None:
                    missing_count += 1
                elif v == 0:
                    fault_count += 1
                else:
                    usable.append(v)
            return usable, fault_count, missing_count

        # Temperature & Humidity: average across the window's usable
        # blocks, excluding exact-zero readings (fault signature — a real
        # outdoor sensor essentially never reads precisely 0°C or 0% RH)
        # and excluding entirely if the sensor is flagged stale overall.
        # Missing/not-yet-published slots are silently excluded — no
        # warning, since they're not a fault, just not arrived yet.
        if not stale["avgAT"]:
            at_vals, at_faults, _ = _split_fault_vs_missing("avgAT")
            if at_vals:
                c["temp_vals"].append((sum(at_vals) / len(at_vals), weight))
            if at_faults:
                c["stale_warnings"].append(
                    f"{name}: {at_faults} zero-temperature reading(s) "
                    f"in last hour excluded (likely sensor fault)")
        else:
            c["stale_warnings"].append(f"{name}: temperature sensor non-communicating")

        if not stale["avgRH"]:
            rh_vals, rh_faults, _ = _split_fault_vs_missing("avgRH")
            if rh_vals:
                c["rh_vals"].append((sum(rh_vals) / len(rh_vals), weight))
            if rh_faults:
                c["stale_warnings"].append(
                    f"{name}: {rh_faults} zero-humidity reading(s) "
                    f"in last hour excluded (likely sensor fault)")
        else:
            c["stale_warnings"].append(f"{name}: humidity sensor non-communicating")

        # Wind Speed: MAXIMUM across the window's published blocks. 0 km/h
        # is kept — calm wind is a real, valid reading, not a fault.
        # Not-yet-published slots are simply skipped (None entries).
        if not stale["maxWS"]:
            ws_vals = [b["maxWS"] * 3.6 for b in real_window_blocks
                       if (b.get("recordingCount") or 0) > 0 and b.get("maxWS") is not None]
            if ws_vals:
                c["ws_vals"].append((max(ws_vals), weight))
        else:
            c["stale_warnings"].append(f"{name}: wind sensor non-communicating")

        # Rainfall: SUM across the window's published blocks (genuine
        # zeros kept — "no rain in this 15-min block" is real data, not
        # a fault; not-yet-published slots contribute 0 to the sum since
        # there's nothing to add for them yet).
        hour_rain = sum((b.get("rain", 0) or 0) for b in real_window_blocks
                        if (b.get("recordingCount") or 0) > 0)
        c["rain_total"] += hour_rain * weight

    result = {}
    for circle, c in by_circle.items():
        def _wavg(pairs):
            if not pairs:
                return None
            total_w = sum(w for _, w in pairs)
            return round(sum(v * w for v, w in pairs) / total_w, 2) if total_w else None

        total_weight = sum(float(s.get("weight",1.0) or 1.0)
                           for s in active_sites if s.get("circle") == circle)
        result[circle] = {
            "avg_temp_c":     _wavg(c["temp_vals"]),
            "avg_humidity":   _wavg(c["rh_vals"]),
            "max_wind_kmph":  max((v for v,_ in c["ws_vals"]), default=None),
            "total_rain_mm":  round(c["rain_total"] / total_weight, 2) if total_weight else round(c["rain_total"],2),
            "n_stations":     len(c["stations"]),
            "stations":       c["stations"],
            "stale_warnings": c["stale_warnings"],
        }
    return result


# ── SQLite persistence (for last-hour aggregation in weather_report.py) ────

def init_db(db_path: str):
    with sqlite3.connect(db_path) as c:
        c.execute("""CREATE TABLE IF NOT EXISTS pss_weather_live (
            id INTEGER PRIMARY KEY AUTOINCREMENT, fetched_at TEXT NOT NULL,
            site_id TEXT, pss_name TEXT, circle_name TEXT,
            t2m REAL, rh2m REAL, ws2m_kmph REAL, rain_mm REAL,
            sensor_ok INTEGER)""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_pss_fetched ON pss_weather_live(fetched_at)")
        c.commit()


def save_to_db(db_path: str, circle_weather: dict):
    now = datetime.now().isoformat()
    with sqlite3.connect(db_path) as c:
        for circle, wx in circle_weather.items():
            sensor_ok = 0 if wx.get("stale_warnings") else 1
            c.execute("""INSERT INTO pss_weather_live
                (fetched_at, circle_name, t2m, rh2m, ws2m_kmph, rain_mm, sensor_ok)
                VALUES (?,?,?,?,?,?,?)""",
                (now, circle, wx.get("avg_temp_c"), wx.get("avg_humidity"),
                 wx.get("max_wind_kmph"), wx.get("total_rain_mm"), sensor_ok))
        c.execute("DELETE FROM pss_weather_live WHERE fetched_at < datetime('now','-30 days')")
        c.commit()


# ── Background loop ─────────────────────────────────────────────────────────

_cache: dict = {}
_cache_lock = threading.Lock()


def get_cache() -> dict:
    with _cache_lock:
        return dict(_cache)


def fetch_once(as_of=None) -> dict:
    """as_of: optional datetime to align the window to a specific report
    slot (see get_circle_weather_now). Defaults to "now" for the regular
    background poll loop, which always reflects live current conditions."""
    global _cache
    cw = get_circle_weather_now(as_of=as_of)
    result = {"ok": bool(cw), "circle_weather": cw,
              "timestamp": datetime.now().isoformat()}
    if as_of is None:
        # Only the "live now" fetch updates the shared dashboard cache —
        # a slot-aligned report fetch (as_of given) is a one-off snapshot
        # for that report only and must not overwrite what the dashboard
        # is currently showing.
        with _cache_lock:
            _cache = result
    return result


def get_weather_anomalies() -> list:
    """
    Flat list of current sensor-fault/non-communicating warnings across
    all circles, for the dashboard's on-screen notification — kept
    separate from the management report, which should show ONLY the
    weather data table and never these remarks.
    Each item: {"circle": str, "station": str, "message": str}
    """
    cached = get_cache()
    cw = cached.get("circle_weather") or {}
    out = []
    for circle, wx in cw.items():
        for msg in wx.get("stale_warnings", []):
            station = msg.split(":", 1)[0].strip()
            out.append({"circle": circle, "station": station, "message": msg})
    return out


def start_background_loop(db_path: str = "data/pss_weather.db",
                          interval: int = POLL_INTERVAL_SEC):
    try:
        init_db(db_path)
    except Exception as e:
        log.error(f"start_background_loop: init_db failed: {e}")

    log.info(f"MB SCADA weather loop started (interval={interval}s)")
    while True:
        t0 = time.time()
        try:
            r = fetch_once()
            if r["ok"]:
                save_to_db(db_path, r["circle_weather"])
                log.info(f"Weather fetch OK: {len(r['circle_weather'])} circles")
            else:
                log.info("Weather fetch: no active sites configured or no data returned")
        except Exception as e:
            log.error(f"Weather loop error: {e}")
        time.sleep(max(10, interval - (time.time() - t0)))


# ── Standalone CLI ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--test", metavar="SITE_ID", nargs="?", const=True,
                  help="Test connection (optionally for a specific site_id)")
    p.add_argument("--loop", action="store_true")
    p.add_argument("--interval", type=int, default=POLL_INTERVAL_SEC)
    a = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    if a.test:
        site_id = a.test if isinstance(a.test, str) else None
        r = test_connection(site_id)
        print(json.dumps(r, indent=2, default=str))
    elif a.loop:
        start_background_loop(interval=a.interval)
    else:
        r = fetch_once()
        print(json.dumps(r, indent=2, default=str))
