"""
modules/alert_store.py — SQLite Alert Store (Clean v2)
=======================================================
Stateful: OV/UV/OL — one alert per asset+type, suppressed while active.
Edge:     OFF/SLD/SLR/DIV/RST — fires every occurrence.
"""
import sqlite3, json, logging, threading, os
from datetime import datetime

log = logging.getLogger("alert_store")
DB_PATH = "data/alerts.db"
STATEFUL = {"OV", "UV", "OL", "FEEDER_OFF", "LOAD_DIVERTED", "PT_PHASE_MISSING",
            "LINE_JUMPER_PARTING", "BC_TRIP_WHILE_DIVERTED", "COMM_DOWN",
            "COMM_DOWN_FROZEN"}
# FIX: STATEFUL must always control which alert types get latched in
# self._active (state tracking), completely independent of email.alert_types
# (notification filtering). These are two separate concerns.
# Previously latched_types was driven by email.alert_types — so any new
# STATEFUL type not yet in the user's notification config would silently
# never be latched, making is_active() always return False, and
# clear_condition() unreachable, leaving those alerts active in the DB
# forever. COMM_DOWN was the first casualty of this design flaw.

# Instant/edge events mark a single point in time and can NEVER be
# "active" — _insert() already encodes this (is_active=0 for these,
# see below). Module-level so add()'s latching decision can share the
# EXACT same set, rather than each function having its own private
# notion of which types this applies to. That mismatch was a real bug:
# add() decided whether to latch purely from the configured alert_types
# checkboxes, with no awareness that some types are instant by design —
# so if e.g. LOAD_RESTORED happened to be a checked type (likely, since
# you'd want restoration notifications), the FIRST restoration for a
# feeder would mark it "active" in memory forever (nothing ever calls
# clear_condition for a restoration — it's a one-time event), silently
# swallowing every SUBSEQUENT restoration for that same feeder as a
# no-op "touch last_seen" with no new alert, no notification, ever
# again until the app restarts.
INSTANT_TYPES = {"LOAD_RESTORED", "SUDDEN_LOAD_DROP", "SUDDEN_LOAD_RAISE",
                  "BC_LOADING_START", "BC_DIVERTED_NORMALIZED", "BC_RESUMED_DIVERSION",
                  "COMM_RESTORED"}


class AlertStore:
    def __init__(self, path=DB_PATH):
        self._path  = path
        self._lock  = threading.Lock()
        self._active: dict = {}  # asset_key -> alert_id (stateful only)
        os.makedirs(os.path.dirname(path) if os.path.dirname(path) else ".", exist_ok=True)
        self._init_db()
        self._load_active()

    def _conn(self):
        return sqlite3.connect(self._path, timeout=10)

    def _init_db(self):
        with self._conn() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS alerts (
                id TEXT PRIMARY KEY, asset_key TEXT, type TEXT, severity TEXT,
                asset_code TEXT, circle TEXT, division TEXT, gss TEXT,
                feeder TEXT, feeder_type TEXT, feeder_rating REAL,
                vr REAL, vy REAL, vb REAL, ir REAL, iy REAL, ib REAL,
                apparent_power REAL, value REAL, lim REAL, detail TEXT,
                extra_json TEXT,
                first_seen TEXT, last_seen TEXT, cleared_at TEXT,
                duration_s REAL, acked INTEGER DEFAULT 0, acked_at TEXT,
                notified_email INTEGER DEFAULT 0, notified_wa INTEGER DEFAULT 0,
                is_active INTEGER DEFAULT 1
            );
            CREATE INDEX IF NOT EXISTS ix_ak  ON alerts(asset_key);
            CREATE INDEX IF NOT EXISTS ix_ts  ON alerts(first_seen);
            CREATE INDEX IF NOT EXISTS ix_act ON alerts(is_active);
            """)

    def _load_active(self):
        try:
            with self._conn() as c:
                rows = c.execute(
                "SELECT asset_key, id FROM alerts WHERE is_active=1 AND type IN ({})".format(
                    ",".join("?" * len(STATEFUL))),
                list(STATEFUL)).fetchall()
            for ak, aid in rows:
                self._active[ak] = aid
            log.info(f"Alert store: {len(self._active)} active stateful alerts")
        except Exception as e:
            log.warning(f"Alert store init: {e}")

    def add(self, alert: dict, cfg=None) -> bool:
        """
        Insert alert. Returns True only if new (should notify).
        Latching (stateful suppression) applies to types that are in
        email.alert_types config (i.e. the checked types in the UI).
        Unchecked types: fire once, disappear on ack.
        """
        vtype = alert.get("type","")
        ak    = alert.get("asset_key","")

        # Latching (state tracking in self._active) is ALWAYS driven by the
        # module-level STATEFUL set — never by email.alert_types, which is
        # a notification-filtering config only. The previous conflation of
        # these two concerns meant any STATEFUL type not in the user's
        # configured notification list was silently never latched, making
        # is_active() always False and clear_condition() unreachable, leaving
        # those alerts permanently active in the DB. INSTANT_TYPES are still
        # explicitly excluded — they must never be treated as long-lived state.
        latched_types = STATEFUL - INSTANT_TYPES

        with self._lock:
            if vtype in latched_types:
                if ak in self._active:
                    with self._conn() as c:
                        c.execute("UPDATE alerts SET last_seen=? WHERE id=?",
                                  (datetime.now().isoformat(), self._active[ak]))
                    return False
                # DB double-check: suppress if already active in DB
                # (handles crash/restart where _active wasn't seeded)
                try:
                    with self._conn() as c:
                        row = c.execute(
                            "SELECT id FROM alerts WHERE asset_key=? AND is_active=1 LIMIT 1",
                            (ak,)).fetchone()
                    if row:
                        self._active[ak] = row[0]
                        with self._conn() as c:
                            c.execute("UPDATE alerts SET last_seen=? WHERE id=?",
                                      (datetime.now().isoformat(), row[0]))
                        return False
                except Exception:
                    pass
                self._active[ak] = alert["id"]
            self._insert(alert)
        return True

    def _insert(self, a: dict):
        now = datetime.now().isoformat()
        extra_data = {k:a.get(k) for k in
            ("BusCouplerAsset","FeederImaxBefore","BCImaxBefore","BCImaxAfter",
             "bc_before","bc_rise","_diverted_feeder","_diverted_feeder_name","_bc_asset")
            if a.get(k) is not None}
        # Store IsBusCoupler flag so frontend can apply BC-specific ack behaviour
        if a.get("IsBusCoupler"):
            extra_data["IsBusCoupler"] = True
        # GSS-level alerts carry affected feeder drill-down
        af = a.get("affected_feeders")
        if af:
            try:
                extra_data["affected_feeders"] = (
                    json.loads(af) if isinstance(af, str) else af)
            except Exception:
                pass
        extra = json.dumps(extra_data) if extra_data else None
        # Instant/edge events are never "active" — they mark a point in time
        # (INSTANT_TYPES defined at module level — shared with add(), see
        # the comment there for why that sharing matters)
        is_active = 0 if a.get("type","") in INSTANT_TYPES else 1
        with self._conn() as c:
            c.execute("""INSERT OR IGNORE INTO alerts
              (id,asset_key,type,severity,asset_code,circle,division,gss,feeder,
               feeder_type,feeder_rating,vr,vy,vb,ir,iy,ib,apparent_power,
               value,lim,detail,extra_json,first_seen,last_seen,is_active)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (a["id"],ak:=a.get("asset_key",""),a.get("type",""),a.get("severity",""),
               a.get("AssetCode",""),a.get("Circle",""),a.get("Division",""),
               a.get("Gss",""),a.get("Feeder",""),a.get("FeederType",""),
               a.get("FeederRating"),a.get("Vr"),a.get("Vy"),a.get("Vb"),
               a.get("Ir"),a.get("Iy"),a.get("Ib"),a.get("ApparentPower"),
               a.get("value"),a.get("limit"),a.get("detail",""),extra,now,now,
               is_active))

    def is_active(self, asset_key: str) -> bool:
        """Check if there is a currently active alert for this asset_key."""
        return asset_key in self._active

    def get_active_alert(self, asset_key: str) -> dict | None:
        """
        Return the full alert dict (including any custom fields persisted
        via the extra_data allowlist, e.g. _diverted_feeder) for the
        currently active alert at this asset_key. Returns None if not
        active. Used by BC_TRIP_WHILE_DIVERTED's restoration check, which
        needs to recover which feeder it was protecting EVEN AFTER that
        feeder may have already been removed from in-memory caches by an
        unrelated code path — the DB row is the durable source of truth.
        """
        aid = self._active.get(asset_key)
        if not aid:
            return None
        with self._conn() as c:
            row = c.execute("SELECT * FROM alerts WHERE id=?", (aid,)).fetchone()
        return self._row(row) if row else None

    def get_active_duration(self, asset_key: str) -> float:
        """Return seconds this alert has been active. 0 if not found."""
        aid = self._active.get(asset_key)
        if not aid:
            # Try DB for very recent alerts not yet in _active
            try:
                with self._conn() as c:
                    row = c.execute(
                        "SELECT first_seen FROM alerts WHERE asset_key=? AND is_active=1 ORDER BY first_seen DESC LIMIT 1",
                        (asset_key,)).fetchone()
                if row and row[0]:
                    from datetime import datetime as _dt
                    return (_dt.now() - _dt.fromisoformat(row[0][:19])).total_seconds()
            except Exception:
                pass
            return 0
        try:
            with self._conn() as c:
                row = c.execute("SELECT first_seen FROM alerts WHERE id=?", (aid,)).fetchone()
            if row and row[0]:
                from datetime import datetime as _dt
                return (_dt.now() - _dt.fromisoformat(row[0][:19])).total_seconds()
        except Exception:
            pass
        return 0

    def was_notified(self, asset_key: str) -> bool:
        """Check if an alert for this asset_key was ever notified (email or WA)."""
        try:
            with self._conn() as c:
                row = c.execute(
                    "SELECT notified_email, notified_wa FROM alerts "
                    "WHERE asset_key=? ORDER BY last_seen DESC LIMIT 1",
                    (asset_key,)).fetchone()
            return bool(row and (row[0] or row[1]))
        except Exception:
            return False

    def set_extra_flag(self, asset_key: str, key: str, value) -> bool:
        """
        Set a single key in an active alert's extra_json, preserving
        everything else already there. Used for one-time-per-incident
        flags (e.g. "_bc_resumed_notified") so a recurring check doesn't
        re-fire every cycle while the underlying condition stays true —
        without needing a whole new alert type/table just to remember
        that.
        """
        aid = self._active.get(asset_key)
        if not aid:
            return False
        try:
            with self._conn() as c:
                row = c.execute("SELECT extra_json FROM alerts WHERE id=?", (aid,)).fetchone()
            extra = json.loads(row[0]) if (row and row[0]) else {}
            extra[key] = value
            with self._conn() as c:
                c.execute("UPDATE alerts SET extra_json=? WHERE id=?",
                         (json.dumps(extra), aid))
                c.commit()
            return True
        except Exception as e:
            log.error(f"set_extra_flag({asset_key}, {key}) failed: {e}")
            return False

    def update_gss_detail(self, gss_key: str, vtype: str,
                          detail: str, affected_feeders: list, value: float):
        """Update detail + affected_feeders for an active GSS OV/UV alert each cycle."""
        ak  = f"{gss_key}_{vtype}"
        aid = self._active.get(ak)
        if not aid:
            return
        try:
            import json as _json
            # Preserve existing extra_json fields, update affected_feeders
            with self._conn() as c:
                row = c.execute("SELECT extra_json FROM alerts WHERE id=?", (aid,)).fetchone()
            extra = _json.loads(row[0]) if (row and row[0]) else {}
            extra["affected_feeders"] = affected_feeders
            with self._conn() as c:
                c.execute("""UPDATE alerts SET detail=?, value=?, extra_json=?, last_seen=?
                             WHERE id=?""",
                          (detail, value, _json.dumps(extra),
                           datetime.now().isoformat(), aid))
        except Exception as e:
            log.debug(f"update_gss_detail error: {e}")

    def update_ol_level(self, asset_key: str, new_level: int,
                        new_value: float, new_detail: str) -> bool:
        """
        Escalate an existing OL alert to a higher level (L2=110%, L3=120%).
        Updates the existing alert card in-place (same ID, same first_seen).
        Returns True ONLY if the level actually increased (triggers re-notification).
        """
        ak = asset_key
        with self._lock:
            aid = self._active.get(ak)
            if not aid:
                return False  # No active OL alert for this feeder
            now = datetime.now().isoformat()
            try:
                with self._conn() as c:
                    row = c.execute(
                        "SELECT extra_json, value FROM alerts WHERE id=? AND is_active=1",
                        (aid,)).fetchone()
                if not row:
                    return False
                # Parse current level from extra_json
                extra = json.loads(row[0]) if row[0] else {}
                cur_level = extra.get("ol_level", 1)
                if new_level <= cur_level:
                    return False  # Already at this level or higher
                # Upgrade level in-place
                extra["ol_level"] = new_level
                extra_str = json.dumps(extra)
                with self._conn() as c:
                    c.execute("""UPDATE alerts
                        SET value=?, detail=?, extra_json=?, last_seen=?,
                            severity=?
                        WHERE id=?""",
                        (new_value, new_detail, extra_str, now,
                         "CRITICAL" if new_level >= 3 else "HIGH",
                         aid))
                log.info(f"OL escalated to L{new_level} for {ak}")
                return True
            except Exception as e:
                log.warning(f"update_ol_level error: {e}")
                return False

    def get_ol_level(self, asset_key: str) -> int:
        """Return current OL level (1/2/3) for asset, 0 if not active."""
        ak = asset_key
        aid = self._active.get(ak)
        if not aid:
            return 0
        try:
            with self._conn() as c:
                row = c.execute(
                    "SELECT extra_json FROM alerts WHERE id=? AND is_active=1",
                    (aid,)).fetchone()
            if row and row[0]:
                return json.loads(row[0]).get("ol_level", 1)
            return 1
        except Exception:
            return 1

    def clear_condition(self, asset_code: str, vtype: str):
        """Deactivate an active alert of given type for this asset."""
        ak = f"{asset_code}_{vtype}"
        with self._lock:
            aid = self._active.pop(ak, None)
        if not aid:
            return
        now = datetime.now().isoformat()
        with self._conn() as c:
            row = c.execute("SELECT first_seen FROM alerts WHERE id=?", (aid,)).fetchone()
            dur = None
            if row:
                try: dur = (datetime.now() - datetime.fromisoformat(row[0])).total_seconds()
                except: pass
            c.execute("UPDATE alerts SET is_active=0,cleared_at=?,duration_s=?,acked=1,acked_at=? WHERE id=?",
                      (now, dur, now, aid))
            if dur:
                h,m = divmod(int(dur)//60, 60)
                log.info(f"Auto-cleared {vtype}/{asset_code} after {h}h{m:02d}m")

    def all(self, limit=200, unacked_only=False, vtype=None,
            circle=None, active_only=False) -> list:
        cl, p = [], []
        if unacked_only: cl.append("acked=0")
        if active_only:  cl.append("is_active=1")
        if vtype:
            if ',' in str(vtype):
                vtypes = [v.strip() for v in vtype.split(',')]
                cl.append(f"type IN ({','.join('?'*len(vtypes))})")
                p.extend(vtypes)
            else:
                cl.append("type=?"); p.append(vtype)
        if circle:       cl.append("circle=?"); p.append(circle)
        w = ("WHERE " + " AND ".join(cl)) if cl else ""

        if active_only:
            # active_only already scopes to is_active=1 — LIMIT is safe here,
            # there normally aren't thousands of simultaneously active alerts
            p2 = p + [limit]
            with self._conn() as c:
                rows = c.execute(
                    f"SELECT * FROM alerts {w} ORDER BY first_seen DESC LIMIT ?", p2
                ).fetchall()
            return [self._row(r) for r in rows]

        # NOT active_only: caller wants a mixed active+history view (e.g. the
        # Alert Log "All" tab). A plain "ORDER BY first_seen DESC LIMIT N" over
        # the WHOLE table can silently drop genuinely active alerts that are
        # simply old (a feeder that's been OFF for days) once N+ newer rows
        # accumulate — exactly the bug that hid BARBIL-I/TELKOI from the UI.
        # Fix: always include ALL currently active rows (regardless of age),
        # then fill the remaining budget with the most recent history rows.
        # NOTE: _conn() returns plain tuples (no row_factory), so convert to
        # dicts via self._row() BEFORE sorting/merging — never index a raw
        # tuple by column name.
        with self._conn() as c:
            active_clause = " AND is_active=1" if cl else "WHERE is_active=1"
            active_rows = c.execute(
                f"SELECT * FROM alerts {w}{active_clause} ORDER BY first_seen DESC",
                p
            ).fetchall()

            remaining = max(limit - len(active_rows), 0)
            hist_clause = (
                (w + " AND is_active=0") if cl else "WHERE is_active=0"
            )
            hist_rows = []
            if remaining > 0:
                p2 = p + [remaining]
                hist_rows = c.execute(
                    f"SELECT * FROM alerts {hist_clause} "
                    f"ORDER BY first_seen DESC LIMIT ?", p2
                ).fetchall()

        # Convert to dicts FIRST (positional zip via self._row), then sort
        # by the dict key — this works regardless of row_factory setting.
        merged = [self._row(r) for r in active_rows] + [self._row(r) for r in hist_rows]
        merged.sort(key=lambda d: d.get("first_seen") or "", reverse=True)
        return merged

    def export(self, start: str, end: str, vtypes=None) -> list:
        p = [start+"T00:00:00", end+"T23:59:59"]
        w = "WHERE first_seen>=? AND first_seen<=?"
        if vtypes:
            w += f" AND type IN ({','.join('?'*len(vtypes))})"
            p.extend(vtypes)
        with self._conn() as c:
            rows = c.execute(f"SELECT * FROM alerts {w} ORDER BY first_seen", p).fetchall()
        return [self._row(r) for r in rows]

    def summary(self) -> dict:
        with self._conn() as c:
            total   = c.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
            unacked = c.execute("SELECT COUNT(*) FROM alerts WHERE acked=0 AND is_active=1").fetchone()[0]
            active  = c.execute("SELECT COUNT(*) FROM alerts WHERE is_active=1").fetchone()[0]
            by_type = {r[0]:r[1] for r in c.execute("SELECT type,COUNT(*) FROM alerts GROUP BY type").fetchall()}
        return {"total":total,"unacked":unacked,"active":active,"by_type":by_type}

    def unacked_count(self):
        with self._conn() as c:
            return c.execute("SELECT COUNT(*) FROM alerts WHERE acked=0 AND is_active=1").fetchone()[0]

    def clear_stale_alert(self, alert_id: str, reason: str = ""):
        """Force-deactivate an alert whose condition info is stale (e.g. GSS renamed)."""
        now = datetime.now().isoformat()
        with self._conn() as c:
            row = c.execute("SELECT asset_key, first_seen FROM alerts WHERE id=?",
                            (alert_id,)).fetchone()
            if not row:
                return
            ak, first = row
            try:
                dur = (datetime.now() - datetime.fromisoformat(first)).total_seconds()
            except Exception:
                dur = None
            c.execute("""UPDATE alerts SET is_active=0, cleared_at=?, duration_s=?,
                         acked=1, acked_at=?, detail=detail||?
                         WHERE id=?""",
                      (now, dur, now, f" [Auto-cleared: {reason}]", alert_id))
        # Remove from active dict
        with self._lock:
            self._active = {k: v for k, v in self._active.items() if v != alert_id}
        log.info(f"Stale alert cleared: {alert_id} ({reason})")

    def force_clear_by_type(self, vtype: str, reason: str = "manual") -> int:
        """
        Force-deactivate ALL active alerts of a given type.
        Used for post-bug cleanup when auto-clear can't fire
        (e.g. LOAD_DIVERTED alerts created by a now-fixed detection bug).
        Returns count of cleared alerts.
        """
        now = datetime.now().isoformat()
        cleared = 0
        try:
            with self._conn() as c:
                rows = c.execute(
                    "SELECT id, asset_key, first_seen FROM alerts WHERE type=? AND is_active=1",
                    (vtype,)).fetchall()
            for aid, ak, first in rows:
                try:
                    dur = (datetime.now() - datetime.fromisoformat(first)).total_seconds()
                except Exception:
                    dur = None
                with self._conn() as c:
                    c.execute(
                        "UPDATE alerts SET is_active=0, cleared_at=?, duration_s=?, "
                        "acked=1, acked_at=?, detail=detail||? WHERE id=?",
                        (now, dur, now, f" [Force-cleared: {reason}]", aid))
                with self._lock:
                    self._active.pop(ak, None)
                cleared += 1
            if cleared:
                log.info(f"force_clear_by_type: cleared {cleared} {vtype} alerts ({reason})")
        except Exception as e:
            log.warning(f"force_clear_by_type error: {e}")
        return cleared

    def force_clear_alert(self, alert_id: str, reason: str = "manual") -> bool:
        """Force-deactivate a single alert by ID regardless of condition state."""
        now = datetime.now().isoformat()
        try:
            with self._conn() as c:
                row = c.execute(
                    "SELECT asset_key, first_seen FROM alerts WHERE id=?",
                    (alert_id,)).fetchone()
            if not row:
                return False
            ak, first = row
            try:
                dur = (datetime.now() - datetime.fromisoformat(first)).total_seconds()
            except Exception:
                dur = None
            with self._conn() as c:
                c.execute(
                    "UPDATE alerts SET is_active=0, cleared_at=?, duration_s=?, "
                    "acked=1, acked_at=?, detail=detail||? WHERE id=?",
                    (now, dur, now, f" [Force-cleared: {reason}]", alert_id))
            with self._lock:
                self._active.pop(ak, None)
            log.info(f"force_clear_alert: {alert_id} ({reason})")
            return True
        except Exception as e:
            log.warning(f"force_clear_alert error: {e}")
            return False

    def ack(self, aid: str) -> bool:
        """Acknowledge a single alert by ID."""
        now = datetime.now().isoformat()
        with self._conn() as c:
            n = c.execute(
                "UPDATE alerts SET acked=1, acked_at=? WHERE id=?",
                (now, aid)).rowcount
        return n > 0

    def ack_all(self):
        with self._conn() as c:
            c.execute("UPDATE alerts SET acked=1,acked_at=? WHERE acked=0",
                      (datetime.now().isoformat(),))

    def clear(self, keep_days: int = 90):
        """Remove resolved alerts older than keep_days. Recent history is preserved for export."""
        cutoff = (datetime.now() - __import__('datetime').timedelta(days=keep_days)).isoformat()
        with self._conn() as c:
            deleted = c.execute(
                "DELETE FROM alerts WHERE is_active=0 AND first_seen < ?",
                (cutoff,)).rowcount
        if deleted:
            log.info(f"Purged {deleted} resolved alerts older than {keep_days} days")

    def mark_notified(self, aid: str, ch: str):
        col = "notified_email" if ch=="email" else "notified_wa"
        with self._conn() as c:
            c.execute(f"UPDATE alerts SET {col}=1 WHERE id=?", (aid,))

    COLS = ["id","asset_key","type","severity","AssetCode","Circle","Division",
            "Gss","Feeder","FeederType","FeederRating","Vr","Vy","Vb","Ir","Iy","Ib",
            "ApparentPower","value","limit","detail","extra_json",
            "first_seen","last_seen","cleared_at","duration_s",
            "acked","acked_at","notified_email","notified_wa","is_active"]

    def _row(self, row) -> dict:
        d = dict(zip(self.COLS, row))
        d["timestamp"] = d["first_seen"]
        d["time"]      = d["first_seen"]
        if d.get("extra_json"):
            try: d.update(json.loads(d["extra_json"]))
            except: pass
        return d


# ════════════════════════════════════════════════════════════
# PEAK LOAD STORE — Monthly + Daily per Circle
# ════════════════════════════════════════════════════════════
class PeakLoadStore:
    """
    Tracks peak MW/MVA per circle per day and per month.
    Auto-resets monthly peak on month start (keeps history).
    Stores in same SQLite DB.
    """
    def __init__(self, db_path=DB_PATH):
        self._path = db_path
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(db_path) if os.path.dirname(db_path) else ".", exist_ok=True)
        self._init_tables()

    def _conn(self):
        return sqlite3.connect(self._path, timeout=10)

    def _init_tables(self):
        with self._conn() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS peak_daily (
                date    TEXT NOT NULL, circle  TEXT NOT NULL,
                peak_mw REAL DEFAULT 0, peak_mva REAL DEFAULT 0, peak_time TEXT,
                PRIMARY KEY (date, circle)
            );
            CREATE TABLE IF NOT EXISTS peak_monthly (
                month  TEXT NOT NULL, circle TEXT NOT NULL,
                peak_mw REAL DEFAULT 0, peak_mva REAL DEFAULT 0,
                peak_date TEXT, peak_time TEXT,
                PRIMARY KEY (month, circle)
            );
            CREATE TABLE IF NOT EXISTS peak_total (
                period_type TEXT NOT NULL, period TEXT NOT NULL,
                peak_mw REAL DEFAULT 0, peak_mva REAL DEFAULT 0, peak_time TEXT,
                PRIMARY KEY (period_type, period)
            );
            CREATE TABLE IF NOT EXISTS peak_gss_daily (
                date TEXT NOT NULL, gss TEXT NOT NULL,
                circle TEXT, peak_mw REAL DEFAULT 0, peak_mva REAL DEFAULT 0, peak_time TEXT,
                PRIMARY KEY (date, gss)
            );
            CREATE TABLE IF NOT EXISTS circle_15min (
                ts        TEXT NOT NULL,
                date      TEXT NOT NULL,
                time_slot TEXT NOT NULL,
                circle    TEXT NOT NULL,
                mw        REAL DEFAULT 0,
                mva       REAL DEFAULT 0,
                PRIMARY KEY (date, time_slot, circle)
            );
            CREATE TABLE IF NOT EXISTS feeder_hourly (
                ts        TEXT NOT NULL,
                date      TEXT NOT NULL,
                hour      INTEGER NOT NULL,
                asset     TEXT NOT NULL,
                gss       TEXT NOT NULL,
                circle    TEXT NOT NULL,
                imax      REAL DEFAULT 0,
                iavg      REAL DEFAULT 0,
                vavg      REAL DEFAULT 0,
                mw        REAL DEFAULT 0,
                PRIMARY KEY (date, hour, asset)
            );
            CREATE INDEX IF NOT EXISTS ix_circle15_date  ON circle_15min(date);
            CREATE INDEX IF NOT EXISTS ix_feeder_hr_date ON feeder_hourly(date, asset);
            """)

    def update_gss(self, gss_data: list):
        """Update per-GSS daily peak. gss_data: [{Gss, Circle, MW_now, MVA_now}]"""
        now   = datetime.now()
        date  = now.strftime("%Y-%m-%d")
        ts    = now.isoformat()
        with self._lock:
            with self._conn() as c:
                for g in gss_data:
                    gss = g.get("Gss","")
                    if not gss:
                        continue
                    mw  = g.get("MW_now", 0)
                    mva = g.get("MVA_now", 0)
                    if mw <= 0:
                        continue
                    c.execute("""
                        INSERT INTO peak_gss_daily(date,gss,circle,peak_mw,peak_mva,peak_time)
                        VALUES(?,?,?,?,?,?)
                        ON CONFLICT(date,gss) DO UPDATE SET
                          peak_mw  = CASE WHEN excluded.peak_mw  > peak_mw  THEN excluded.peak_mw  ELSE peak_mw  END,
                          peak_mva = CASE WHEN excluded.peak_mva > peak_mva THEN excluded.peak_mva ELSE peak_mva END,
                          peak_time= CASE WHEN excluded.peak_mw  > peak_mw  THEN excluded.peak_time ELSE peak_time END
                    """, (date, gss, g.get("Circle",""), mw, mva, ts))

    def get_gss_daily(self, date: str = None) -> dict:
        """Return {gss: {peak_mw, peak_mva, peak_time}} for a date (default today)."""
        d = date or datetime.now().strftime("%Y-%m-%d")
        try:
            with self._conn() as c:
                rows = c.execute(
                    "SELECT gss, peak_mw, peak_mva, peak_time FROM peak_gss_daily WHERE date=?",
                    (d,)).fetchall()
            return {r[0]: {"peak_mw": round(r[1],3), "peak_mva": round(r[2],3), "peak_time": r[3]}
                    for r in rows}
        except Exception:
            return {}

    # ── 15-min circle snapshots ──────────────────────────────
    def record_circle_15min(self, circle_data: list):
        """
        Store 15-min circle MW/MVA snapshot.
        circle_data: [{Circle, MW_now, MVA_now}]
        Keeps 7 days of data. Called every fetch cycle.
        """
        now       = datetime.now()
        date      = now.strftime("%Y-%m-%d")
        # Round down to nearest 15-min slot: "HH:MM"
        slot_min  = (now.minute // 15) * 15
        time_slot = now.strftime(f"%H:{slot_min:02d}")
        ts        = now.isoformat()

        with self._lock:
            with self._conn() as c:
                for cd in circle_data:
                    circ = cd.get("Circle","")
                    mw   = round(cd.get("MW_now", 0), 3)
                    mva  = round(cd.get("MVA_now", 0), 3)
                    if not circ or mw <= 0:
                        continue
                    c.execute("""
                        INSERT INTO circle_15min(ts,date,time_slot,circle,mw,mva)
                        VALUES(?,?,?,?,?,?)
                        ON CONFLICT(date,time_slot,circle) DO UPDATE SET
                          mw=((mw*0.6)+(excluded.mw*0.4)),
                          mva=((mva*0.6)+(excluded.mva*0.4)),
                          ts=excluded.ts
                    """, (ts, date, time_slot, circ, mw, mva))

                # Purge data older than 7 days
                cutoff = (datetime.now() - __import__('datetime').timedelta(days=7)).strftime("%Y-%m-%d")
                c.execute("DELETE FROM circle_15min WHERE date < ?", (cutoff,))

    def get_circle_prevday_slot(self, circle: str, slot_offset_min: int = 0) -> dict:
        """
        Get yesterday's circle MW at the current time slot (±offset minutes).
        Returns {mw, mva, time_slot} or None.
        """
        now      = datetime.now()
        slot_min = (now.minute // 15) * 15 + slot_offset_min
        # Adjust hour if offset crosses slot boundary
        hour_adj = 0
        while slot_min >= 60: slot_min -= 60; hour_adj += 1
        while slot_min < 0:   slot_min += 60; hour_adj -= 1
        h        = (now.hour + hour_adj) % 24
        slot     = f"{h:02d}:{slot_min:02d}"
        yesterday = (now - __import__('datetime').timedelta(days=1)).strftime("%Y-%m-%d")
        try:
            with self._conn() as c:
                row = c.execute(
                    "SELECT mw, mva, time_slot FROM circle_15min WHERE date=? AND circle=? AND time_slot=?",
                    (yesterday, circle, slot)).fetchone()
            return {"mw": round(row[0],3), "mva": round(row[1],3), "time_slot": row[2]} if row else None
        except Exception:
            return None

    def get_circle_history(self, circle: str, days: int = 7) -> list:
        """Return all 15-min slots for a circle over last N days."""
        cutoff = (datetime.now() - __import__('datetime').timedelta(days=days)).strftime("%Y-%m-%d")
        try:
            with self._conn() as c:
                rows = c.execute(
                    "SELECT date, time_slot, mw, mva FROM circle_15min WHERE circle=? AND date>=? ORDER BY date,time_slot",
                    (circle, cutoff)).fetchall()
            return [{"date":r[0],"time_slot":r[1],"mw":r[2],"mva":r[3]} for r in rows]
        except Exception:
            return []

    # ── Feeder hourly snapshots ──────────────────────────────
    def record_feeder_hourly(self, feeder_data: list):
        """
        Store hourly feeder load snapshot.
        feeder_data: [{AssetCode, Gss, Circle, Imax, Iavg, Vavg, MW}]
        Keeps 7 days. Called at start of each hour.
        """
        now  = datetime.now()
        date = now.strftime("%Y-%m-%d")
        hour = now.hour
        ts   = now.isoformat()
        with self._lock:
            with self._conn() as c:
                for f in feeder_data:
                    ac = f.get("AssetCode","")
                    if not ac:
                        continue
                    c.execute("""
                        INSERT INTO feeder_hourly(ts,date,hour,asset,gss,circle,imax,iavg,vavg,mw)
                        VALUES(?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(date,hour,asset) DO UPDATE SET
                          imax=excluded.imax, iavg=excluded.iavg,
                          vavg=excluded.vavg, mw=excluded.mw, ts=excluded.ts
                    """, (ts, date, hour, ac,
                          f.get("Gss",""), f.get("Circle",""),
                          round(f.get("Imax",0),2),
                          round(f.get("Iavg",0),2),
                          round(f.get("Vavg",0),3),
                          round(f.get("MW",0),3)))
                cutoff = (datetime.now() - __import__('datetime').timedelta(days=7)).strftime("%Y-%m-%d")
                c.execute("DELETE FROM feeder_hourly WHERE date < ?", (cutoff,))

    def get_feeder_hourly_profile(self, asset: str, days: int = 2) -> list:
        """Return hourly load profile for a feeder over last N days."""
        cutoff = (datetime.now() - __import__('datetime').timedelta(days=days)).strftime("%Y-%m-%d")
        try:
            with self._conn() as c:
                rows = c.execute(
                    "SELECT date, hour, imax, iavg, vavg, mw FROM feeder_hourly WHERE asset=? AND date>=? ORDER BY date,hour",
                    (asset, cutoff)).fetchall()
            return [{"date":r[0],"hour":r[1],"imax":r[2],"iavg":r[3],"vavg":r[4],"mw":r[5]} for r in rows]
        except Exception:
            return []

    def get_feeder_yesterday_hour(self, asset: str, hour: int = None) -> dict:
        """Get feeder's load at the same hour yesterday."""
        if hour is None:
            hour = datetime.now().hour
        yesterday = (datetime.now() - __import__('datetime').timedelta(days=1)).strftime("%Y-%m-%d")
        try:
            with self._conn() as c:
                row = c.execute(
                    "SELECT imax, iavg, mw FROM feeder_hourly WHERE asset=? AND date=? AND hour=?",
                    (asset, yesterday, hour)).fetchone()
            return {"imax": row[0], "iavg": row[1], "mw": row[2]} if row else None
        except Exception:
            return None

    def update(self, circle_data: list):
        """
        Call after each fetch cycle with list of:
          {Circle, MW_now, MVA_now}
        Updates daily and monthly peaks.
        """
        now   = datetime.now()
        date  = now.strftime("%Y-%m-%d")
        month = now.strftime("%Y-%m")
        ts    = now.isoformat()

        total_mw  = sum(c.get("MW_now",0)  for c in circle_data)
        total_mva = sum(c.get("MVA_now",0) for c in circle_data)

        with self._lock:
            with self._conn() as c:
                for cd in circle_data:
                    circ = cd.get("Circle","")
                    mw   = cd.get("MW_now", 0)
                    mva  = cd.get("MVA_now", 0)
                    if not circ:
                        continue
                    # Daily
                    c.execute("""
                        INSERT INTO peak_daily(date,circle,peak_mw,peak_mva,peak_time)
                        VALUES(?,?,?,?,?)
                        ON CONFLICT(date,circle) DO UPDATE SET
                          peak_mw  = CASE WHEN excluded.peak_mw  > peak_mw  THEN excluded.peak_mw  ELSE peak_mw  END,
                          peak_mva = CASE WHEN excluded.peak_mva > peak_mva THEN excluded.peak_mva ELSE peak_mva END,
                          peak_time= CASE WHEN excluded.peak_mw  > peak_mw  THEN excluded.peak_time ELSE peak_time END
                    """, (date, circ, mw, mva, ts))
                    # Monthly
                    c.execute("""
                        INSERT INTO peak_monthly(month,circle,peak_mw,peak_mva,peak_date,peak_time)
                        VALUES(?,?,?,?,?,?)
                        ON CONFLICT(month,circle) DO UPDATE SET
                          peak_mw   = CASE WHEN excluded.peak_mw  > peak_mw  THEN excluded.peak_mw  ELSE peak_mw  END,
                          peak_mva  = CASE WHEN excluded.peak_mva > peak_mva THEN excluded.peak_mva ELSE peak_mva END,
                          peak_date = CASE WHEN excluded.peak_mw  > peak_mw  THEN excluded.peak_date ELSE peak_date END,
                          peak_time = CASE WHEN excluded.peak_mw  > peak_mw  THEN excluded.peak_time ELSE peak_time END
                    """, (month, circ, mw, mva, date, ts))

                # Total (all circles combined)
                c.execute("""
                    INSERT INTO peak_total(period_type,period,peak_mw,peak_mva,peak_time)
                    VALUES('daily',?,?,?,?)
                    ON CONFLICT(period_type,period) DO UPDATE SET
                      peak_mw  = CASE WHEN excluded.peak_mw  > peak_mw  THEN excluded.peak_mw  ELSE peak_mw  END,
                      peak_mva = CASE WHEN excluded.peak_mva > peak_mva THEN excluded.peak_mva ELSE peak_mva END,
                      peak_time= CASE WHEN excluded.peak_mw  > peak_mw  THEN excluded.peak_time ELSE peak_time END
                """, (date, total_mw, total_mva, ts))
                c.execute("""
                    INSERT INTO peak_total(period_type,period,peak_mw,peak_mva,peak_time)
                    VALUES('monthly',?,?,?,?)
                    ON CONFLICT(period_type,period) DO UPDATE SET
                      peak_mw  = CASE WHEN excluded.peak_mw  > peak_mw  THEN excluded.peak_mw  ELSE peak_mw  END,
                      peak_mva = CASE WHEN excluded.peak_mva > peak_mva THEN excluded.peak_mva ELSE peak_mva END,
                      peak_time= CASE WHEN excluded.peak_mw  > peak_mw  THEN excluded.peak_time ELSE peak_time END
                """, (month, total_mw, total_mva, ts))

    def get_current(self) -> dict:
        """Return today's and this month's peaks per circle + total."""
        now   = datetime.now()
        date  = now.strftime("%Y-%m-%d")
        month = now.strftime("%Y-%m")
        with self._conn() as c:
            daily   = {r[0]:{"peak_mw":r[1],"peak_mva":r[2],"peak_time":r[3]}
                       for r in c.execute(
                           "SELECT circle,peak_mw,peak_mva,peak_time FROM peak_daily WHERE date=?",
                           (date,)).fetchall()}
            monthly = {r[0]:{"peak_mw":r[1],"peak_mva":r[2],"peak_date":r[3],"peak_time":r[4]}
                       for r in c.execute(
                           "SELECT circle,peak_mw,peak_mva,peak_date,peak_time FROM peak_monthly WHERE month=?",
                           (month,)).fetchall()}
            td = c.execute("SELECT peak_mw,peak_mva,peak_time FROM peak_total WHERE period_type='daily' AND period=?",
                           (date,)).fetchone()
            tm = c.execute("SELECT peak_mw,peak_mva,peak_time FROM peak_total WHERE period_type='monthly' AND period=?",
                           (month,)).fetchone()
        return {
            "daily":   daily,
            "monthly": monthly,
            "total_daily_mw":         round(td[0],3) if td else 0,
            "total_daily_mva":        round(td[1],3) if td else 0,
            "total_daily_peak_time":  td[2] if td else "",
            "total_monthly_mw":       round(tm[0],3) if tm else 0,
            "total_monthly_mva":      round(tm[1],3) if tm else 0,
            "total_monthly_peak_time":tm[2] if tm else "",
            "date":  date, "month": month,
        }

    def export(self, start_month: str, end_month: str) -> dict:
        """Export monthly peaks. Months as YYYY-MM."""
        with self._conn() as c:
            monthly = [{"month":r[0],"circle":r[1],"peak_mw":r[2],"peak_mva":r[3],
                        "peak_date":r[4],"peak_time":r[5]}
                       for r in c.execute(
                           "SELECT month,circle,peak_mw,peak_mva,peak_date,peak_time "
                           "FROM peak_monthly WHERE month>=? AND month<=? ORDER BY month,circle",
                           (start_month, end_month)).fetchall()]
            total   = [{"period":r[0],"peak_mw":r[1],"peak_mva":r[2],"peak_time":r[3]}
                       for r in c.execute(
                           "SELECT period,peak_mw,peak_mva,peak_time FROM peak_total "
                           "WHERE period_type='monthly' AND period>=? AND period<=? ORDER BY period",
                           (start_month, end_month)).fetchall()]
        return {"monthly_by_circle": monthly, "monthly_total": total}

    def reset_monthly(self, month: str = None):
        """Manual reset for a specific month (admin use). Default = current month."""
        m = month or datetime.now().strftime("%Y-%m")
        with self._conn() as c:
            c.execute("DELETE FROM peak_monthly WHERE month=?", (m,))
            c.execute("DELETE FROM peak_total WHERE period_type='monthly' AND period=?", (m,))
        log.info(f"Peak load reset for month {m}")
