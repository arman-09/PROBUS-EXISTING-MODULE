"""
TPNODL Realtime Load & Voltage Monitor — Main Flask Server
=============================================================
Entry point. Starts scheduler, registers all API routes.
Run: python app.py
"""

import os, sys, logging, threading, time
from datetime import datetime
import urllib3
# Always run from the script's own directory — prevents relative path failures
os.chdir(os.path.dirname(os.path.abspath(__file__)))
# Suppress Selenium's urllib3 connection pool noise
logging.getLogger("urllib3.connectionpool").setLevel(logging.ERROR)
from flask import Flask, jsonify, send_from_directory, send_file
from flask_cors import CORS
import schedule

from modules.config        import Config
from modules.scraper       import ProbusScraper
from modules.violation     import ViolationDetector
from modules.email_mgr     import EmailManager
from modules.whatsapp_mgr  import WhatsAppManager
from modules.feeder_master import FeederMaster
from modules.alert_store   import AlertStore, PeakLoadStore
from modules.mgmt_report   import ManagementReporter
from modules.notify_queue  import NotifyQueue

# Field-level diversion marking — fully separate module, optional.
# See modules/field_diversion.py docstring for design rationale.
try:
    from modules.field_diversion import auto_clear_stale_marks as _fd_auto_clear
    _FIELD_DIVERSION_AVAILABLE_APP = True
except ImportError:
    _FIELD_DIVERSION_AVAILABLE_APP = False
    def _fd_auto_clear(active_ids): return 0
import modules.routes as routes

# ─── Logging ──────────────────────────────────────────────
os.makedirs("logs", exist_ok=True)

from logging.handlers import TimedRotatingFileHandler

# Daily log rotation: writes to logs/monitor.log during the current day.
# At midnight (local server time), the handler automatically:
#   1. Renames the just-finished day's content to logs/monitor.log.YYYY-MM-DD
#   2. Starts a fresh, empty logs/monitor.log for the new day
# So each calendar day's logs end up in their own dated file automatically —
# no manual log splitting needed, and the log file never grows unbounded.
_file_handler = TimedRotatingFileHandler(
    "logs/monitor.log",
    when="midnight",     # rotate at 00:00 server-local time
    interval=1,          # every 1 day
    backupCount=90,      # keep 90 days of history, older files auto-deleted
    encoding="utf-8",
    utc=False,           # use server's local time, not UTC, to match "00:00 local"
)
# Rotated files get suffix ".YYYY-MM-DD" appended automatically by the
# handler's default naming — this matches its built-in suffix format,
# no extra configuration needed for that part.
_file_handler.suffix = "%Y-%m-%d"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        _file_handler,
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("app")

# Server start time — used to give management-report catch-up logic a small
# grace window so it doesn't fire on stale/incomplete live data immediately
# after a restart that happens to land right at a slot boundary.
_SERVER_START = time.time()

# ─── App ──────────────────────────────────────────────────
app = Flask(__name__, static_folder="static", template_folder="templates")
CORS(app, resources={r"/api/*": {"origins": "*"}})

# ─── Shared service instances ─────────────────────────────
cfg          = Config("data/config.json")
feeder_master= FeederMaster("data/feeder_master.json")
alert_store  = AlertStore("data/alerts.db")
peak_store   = PeakLoadStore("data/alerts.db")

# Startup migration 1: mark instant types as is_active=0
try:
    import sqlite3 as _sq
    _INSTANT = ("LOAD_RESTORED","SUDDEN_LOAD_DROP","SUDDEN_LOAD_RAISE")
    with _sq.connect("data/alerts.db") as _c:
        _n = _c.execute(
            "UPDATE alerts SET is_active=0 WHERE type IN ({}) AND is_active=1".format(
                ",".join("?"*len(_INSTANT))), _INSTANT).rowcount
    if _n:
        log.info(f"Migration: marked {_n} instant-type alerts inactive")
except Exception as _e:
    log.warning(f"Migration 1: {_e}")

# Startup migration 3: clear FEEDER_OFF alerts on Bus Coupler feeders
# BC's normal state is 0A — it should never have a FEEDER_OFF alert
try:
    with _sq.connect("data/alerts.db") as _c:
        _n = _c.execute("""
            UPDATE alerts SET is_active=0, acked=1
            WHERE type='FEEDER_OFF' AND is_active=1
            AND (UPPER(feeder) LIKE '%BUS COUPL%' OR UPPER(feeder) LIKE '%BUS COUPLER%')
        """).rowcount
    if _n:
        log.info(f"Migration: cleared {_n} BC FEEDER_OFF alerts")
except Exception as _e:
    log.warning(f"Migration 3: {_e}")
try:
    with _sq.connect("data/alerts.db") as _c:
        # Find asset_keys with multiple active alerts
        dups = _c.execute("""
            SELECT asset_key, COUNT(*) as cnt FROM alerts
            WHERE is_active=1 GROUP BY asset_key HAVING cnt > 1
        """).fetchall()
        deduped = 0
        for ak, cnt in dups:
            # Keep newest, deactivate older ones
            rows = _c.execute(
                "SELECT id FROM alerts WHERE asset_key=? AND is_active=1 ORDER BY first_seen DESC",
                (ak,)).fetchall()
            for row in rows[1:]:  # skip first (newest)
                _c.execute("UPDATE alerts SET is_active=0,acked=1 WHERE id=?", (row[0],))
                deduped += 1
        if deduped:
            log.info(f"Migration: deduped {deduped} duplicate active alerts")
except Exception as _e:
    log.warning(f"Migration 2: {_e}")
# Migration 4 REMOVED (was: auto-clear LOAD_DIVERTED alerts older than 30 min
# on every restart). This was wrong — a genuine diversion can legitimately
# stay active for hours (the feeder remains OFF and BC keeps carrying its
# load the whole time). LOAD_DIVERTED is a normal stateful alert exactly
# like FEEDER_OFF: it must persist across restarts and is only cleared when
# BOTH the feeder load normalizes AND BC load reduces (handled correctly in
# violation.py's BusCouplerDiversionDetector + the _active seeding in
# ViolationDetector.__init__, which restores confirmed diversions from DB
# on every startup). No time-based auto-clear should ever apply here.

email_mgr    = EmailManager(cfg)
wa_mgr       = WhatsAppManager(cfg)

# NotifyQueue: decouples email/WA delivery from the scan cycle completely.
# The scan cycle enqueues in <1ms and returns immediately — the queue's
# background thread handles actual delivery (including WhatsApp Selenium,
# which was previously blocking the scan cycle for ~77s inline) with
# retry/backoff. Survives process restarts via SQLite persistence.
notify_queue = NotifyQueue(email_mgr=email_mgr, wa_mgr=wa_mgr,
                            alert_store=alert_store,
                            db_path="data/notify_queue.db")

# Auto-connect WhatsApp Web on startup (non-blocking background thread)
def _wa_startup_connect():
    import time as _time
    _time.sleep(3)  # let Flask start first
    log.info("WA: startup auto-connect...")
    wa_cfg = cfg.get("whatsapp.provider", "wa_web")
    if wa_cfg == "wa_web" and cfg.get("whatsapp.enabled", False):
        result = wa_mgr.auto_connect()
        log.info(f"WA startup connect: {result.get('message','')}")
        # Wait for login to complete (up to 60s)
        for _ in range(30):
            _time.sleep(2)
            if wa_mgr._ready:
                log.info("WA ready — checking for pending unnotified alerts")
                _retry_wa_pending()
                break

def _retry_wa_pending():
    """Send WA notifications for alerts that were created before WA was ready.
    Only sends alerts whose notification delay has already elapsed."""
    from datetime import datetime as _dt
    try:
        pending = [a for a in alert_store.all(limit=50)
                   if not a.get("notified_wa") and a.get("is_active")]
        if not pending:
            return

        # Filter: only send if delay has elapsed (same logic as _notify)
        def delay_elapsed(a: dict) -> bool:
            t    = a.get("type","")
            lvl  = a.get("ol_level", 1)
            if t == "OL":
                delay = float(cfg.get(f"notify_delay.OL_L{lvl}", {1:10,2:0,3:0}.get(lvl,0)))
            else:
                defaults = {"OV":15,"UV":60,"FEEDER_OFF":10,
                            "PT_PHASE_MISSING":0,"LINE_JUMPER_PARTING":0,
                            "LOAD_DIVERTED":0,"LOAD_RESTORED":0}
                delay = float(cfg.get(f"notify_delay.{t}", defaults.get(t, 0)))
            if delay <= 0:
                return True
            ts = a.get("first_seen") or a.get("timestamp","")
            if not ts:
                return True
            try:
                age_min = (_dt.now() - _dt.fromisoformat(ts[:19].replace(" ","T"))).total_seconds() / 60
                return age_min >= delay
            except Exception:
                return True

        ready = [a for a in pending if delay_elapsed(a)]
        deferred = len(pending) - len(ready)
        if deferred:
            log.info(f"WA retry: {deferred} alert(s) still within delay window — deferred")
        if not ready:
            return
        log.info(f"WA retry: sending {len(ready)} pending alert(s)")
        detector._notify_wa(ready)
    except Exception as e:
        log.warning(f"WA retry error: {e}")

import threading as _th
_th.Thread(target=_wa_startup_connect, daemon=True, name="wa-startup").start()
scraper      = ProbusScraper(cfg)
scraper.set_feeder_master(feeder_master)   # FM is source of truth for names
detector     = ViolationDetector(cfg, feeder_master, alert_store, email_mgr, wa_mgr,
                                  peak_store=peak_store, notify_queue=notify_queue)

# Attach to routes module
mgmt_reporter = ManagementReporter(cfg, alert_store, scraper, peak_store, feeder_master, wa_mgr,
                                    server_start_ts=_SERVER_START, email_mgr=email_mgr)
routes.init(app, cfg, scraper, detector, feeder_master, alert_store, email_mgr, wa_mgr,
            peak_store, mgmt_reporter, notify_queue=notify_queue)

# ─── Serve frontend ───────────────────────────────────────
# Search order: static/index.html → static/index_base.html → project root
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def _find_html():
    candidates = [
        os.path.join(BASE_DIR, "static", "index.html"),
        os.path.join(BASE_DIR, "static", "index_base.html"),
        os.path.join(BASE_DIR, "index.html"),
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return None

@app.route("/")
def index():
    path = _find_html()
    if path:
        directory = os.path.dirname(path)
        filename  = os.path.basename(path)
        return send_from_directory(directory, filename)
    return ("<h2>Frontend not found</h2>"
            "<p>Place <b>index.html</b> in the <code>static/</code> folder "
            "next to <code>app.py</code>.</p>"
            "<p>API is running — try <a href='/api/status'>/api/status</a></p>"), 200

@app.route("/<path:path>")
def static_files(path):
    # Try static/ first, then project root
    static_path = os.path.join(BASE_DIR, "static", path)
    if os.path.exists(static_path):
        return send_from_directory(os.path.join(BASE_DIR, "static"), path)
    root_path = os.path.join(BASE_DIR, path)
    if os.path.exists(root_path):
        return send_from_directory(BASE_DIR, path)
    return jsonify({"error": "not found", "path": path}), 404

# ─── Scheduler thread ─────────────────────────────────────
def run_fetch_cycle():
    """Called by scheduler: scrape → detect → notify → update peaks → cleanup stale."""
    try:
        log.info("=== Scheduled fetch cycle START ===")
        live = scraper.fetch()
        if live:
            detector.run(live)
            # Drain RST events queued by stale-clear (generated outside scan loop)
            if detector._pending_notify:
                log.info(f"Processing {len(detector._pending_notify)} stale-RST event(s)")
                pending_rst = list(detector._pending_notify)
                detector._pending_notify.clear()
                # Fire async — same non-blocking pattern as detector.run()
                import threading as _th
                _th.Thread(target=detector._notify, args=(pending_rst,),
                           daemon=True, name="notify-stale-rst").start()
            # Aggregate by Circle for circle-level peaks
            circles = {}
            gss_agg = {}
            feeder_snap = []
            now_dt = datetime.now()

            for r in live:
                circ = r.get("Circle","")
                gss  = r.get("Gss","")
                raw_ap = float(r.get("ActivePower")  or 0)
                raw_sp = float(r.get("ApparentPower") or 0)
                # Apply PowerSign correction from Feeder Master
                _fm_entry = feeder_master.lookup(r.get("AssetCode",""))
                _sign = (_fm_entry.get("PowerSign","") if _fm_entry else "").strip().lower()
                if _sign == "-":
                    ap, sp = -raw_ap, -raw_sp
                elif _sign == "solar":
                    ap, sp = raw_ap, raw_sp   # solar: keep sign (negative = generation)
                else:
                    ap, sp = abs(raw_ap), abs(raw_sp)  # default: force positive for load feeders
                ir_  = float(r.get("Ir") or 0)
                iy_  = float(r.get("Iy") or 0)
                ib_  = float(r.get("Ib") or 0)
                imax = max(ir_, iy_, ib_)
                iavg = (ir_ + iy_ + ib_) / 3
                vr_  = float(r.get("Vr") or 0)
                vy_  = float(r.get("Vy") or 0)
                vb_  = float(r.get("Vb") or 0)
                vavg = (vr_ + vy_ + vb_) / 3

                if circ:
                    circles.setdefault(circ, {"Circle":circ,"MW_now":0,"MVA_now":0})
                    circles[circ]["MW_now"]  += ap
                    circles[circ]["MVA_now"] += sp
                if gss:
                    gss_agg.setdefault(gss, {"Gss":gss,"Circle":circ,"MW_now":0,"MVA_now":0})
                    gss_agg[gss]["MW_now"]  += ap
                    gss_agg[gss]["MVA_now"] += sp
                ac = r.get("AssetCode","")
                if ac and not r.get("IsBusCoupler"):
                    feeder_snap.append({
                        "AssetCode": ac, "Gss": gss, "Circle": circ,
                        "Imax": imax, "Iavg": iavg, "Vavg": vavg, "MW": ap,
                    })

            if circles:
                peak_store.update(list(circles.values()))
                # 15-min circle snapshot (every fetch cycle, ~2min interval)
                peak_store.record_circle_15min(list(circles.values()))

            if gss_agg:
                peak_store.update_gss(list(gss_agg.values()))

            # Hourly feeder snapshot — only at the start of each hour
            if now_dt.minute < 3 and feeder_snap:  # first 2 min of each hour
                peak_store.record_feeder_hourly(feeder_snap)
                log.info(f"Hourly feeder snapshot: {len(feeder_snap)} feeders stored")

            # Auto-cleanup stale GSS-level alerts every cycle
            live_gss    = {r.get("Gss","").strip() for r in live if r.get("Gss")}
            live_assets = {r.get("AssetCode","") for r in live if r.get("AssetCode")}

            # Build lookup: asset -> current imax
            live_imax   = {}
            for r in live:
                ac = r.get("AssetCode","")
                if ac:
                    live_imax[ac] = max(float(r.get("Ir",0)),
                                        float(r.get("Iy",0)),
                                        float(r.get("Ib",0)))

            off_thr = cfg.get("voltage.feeder_off_threshold_a", 1.0)
            stale   = 0
            for a in alert_store.all(active_only=True, limit=500):
                ac    = a.get("AssetCode","")
                atype = a.get("type","")

                # Skip if violation scan already cleared this alert this cycle
                if not alert_store.is_active(a.get("asset_key","")):
                    continue

                if ac.startswith("GSS_"):
                    # GSS alert: deactivate if GSS not in live data
                    gss = (a.get("Gss") or "").strip()
                    if gss and gss not in live_gss:
                        alert_store.clear_stale_alert(a["id"], "GSS renamed/removed")
                        stale += 1

                elif atype == "FEEDER_OFF":
                    curr = live_imax.get(ac)
                    feeder_name = (a.get("Feeder") or "").upper()
                    is_bc_alert = a.get("IsBusCoupler") or "BUS COUPL" in feeder_name
                    if is_bc_alert:
                        # BC FEEDER_OFF should never be active — BC OFF is normal
                        alert_store.clear_stale_alert(a["id"], "BC normal state is 0A")
                        stale += 1
                    elif curr is not None and curr > off_thr:
                        # Feeder is back ON — generate LOAD_RESTORED before clearing
                        # Only fires if violation scan didn't already handle it
                        # (violation scan calls clear_condition → removes from _active)
                        ak_off = a.get("asset_key","")
                        if not alert_store.is_active(ak_off):
                            # Violation scan already created LOAD_RESTORED and cleared it
                            # Stale-clear was redundant — just skip
                            continue
                        was_notified = alert_store.was_notified(ak_off)
                        try:
                            # Compute from a['first_seen'] directly — most reliable
                            fs_str = a.get("first_seen","")
                            if fs_str:
                                from datetime import datetime as _dtx
                                fs_dt   = _dtx.fromisoformat(str(fs_str)[:19])
                                dur_s   = (datetime.now() - fs_dt).total_seconds()
                                _h,_m   = int(dur_s//3600), int((dur_s%3600)//60)
                                dur_str = f"{_h}h {_m}m" if _h else f"{_m}m"
                            else:
                                dur_str = ""
                        except Exception:
                            dur_str = ""
                        # Find live row for this asset to get current V/I
                        live_row = next((r for r in live if r.get("AssetCode","") == ac), {})
                        master   = feeder_master.lookup(ac)
                        if master or live_row:
                            try:
                                rst_v = detector._make(
                                    "LOAD_RESTORED", live_row or {"AssetCode": ac},
                                    master,
                                    value=round(curr, 2), limit=off_thr,
                                    detail=(f"Feeder {a.get('Feeder', ac)} normalized "
                                            f"({curr:.1f}A). Feeder OFF/Trip ended."
                                            + (f" Outage duration: {dur_str}." if dur_str else "")))
                                rst_v["_was_off_notified"] = was_notified
                                rst_v["_off_duration_str"] = dur_str
                                if alert_store.add(rst_v, cfg):
                                    detector._pending_notify.append(rst_v)
                                    log.info(f"LOAD_RESTORED (stale-clear): {ac} "
                                             f"restored to {curr:.1f}A, "
                                             f"was_notified={was_notified}")
                            except Exception as _re:
                                log.warning(f"Could not create RST for stale {ac}: {_re}")
                        alert_store.clear_stale_alert(
                            a["id"], f"Feeder restored ({curr:.2f}A > {off_thr}A)")
                        stale += 1

                elif atype == "LOAD_DIVERTED":
                    # Diversion clearing is NOT handled here. It requires BOTH:
                    #   1. Feeder load normalized (back ON)
                    #   2. BC load reduced (accounting for any OTHER feeders
                    #      still diverted to the same BC)
                    # That dual-check logic lives in violation.py's
                    # BusCouplerDiversionDetector.update() RESTORATION block,
                    # which runs every cycle via detector.run(live) — and is
                    # the only place LOAD_DIVERTED should ever be cleared.
                    # This stale-clear loop must NOT touch LOAD_DIVERTED at all,
                    # or it bypasses the BC-load check and clears prematurely
                    # while BC is still carrying the diverted load.
                    pass

            if stale:
                log.info(f"Auto-cleared {stale} stale alert(s)")

            # Field-level diversion marks: clear any mark whose underlying
            # FEEDER_OFF alert is no longer active. Read-only access to
            # alert_store's _active dict values (the set of currently
            # active alert IDs) — field_diversion.py itself never imports
            # or touches alert_store directly.
            if _FIELD_DIVERSION_AVAILABLE_APP:
                try:
                    active_ids = set(alert_store._active.values())
                    _fd_auto_clear(active_ids)
                except Exception as e:
                    log.warning(f"field_diversion auto-clear check failed: {e}")

            log.info(f"Cycle complete: {len(live)} meters, {stale} stale cleared")
            # Management hourly report
            mgmt_reporter.check_and_send()
        else:
            log.warning("Fetch returned no data")
    except Exception as e:
        log.exception(f"Fetch cycle error: {e}")

def scheduler_thread():
    interval = cfg.get("scraper.interval_minutes", 10)
    # Support 1-min granularity (schedule library uses whole minutes)
    # Valid: 1,2,3,4,5,10,15,30
    interval = max(1, int(interval))
    schedule.every(interval).minutes.do(run_fetch_cycle)
    log.info(f"Scheduler: fetch every {interval} min")
    while True:
        schedule.run_pending()
        time.sleep(15)  # check every 15s for sub-5-min intervals

# ─── Main ─────────────────────────────────────────────────
if __name__ == "__main__":
    # Initial fetch on startup
    t = threading.Thread(target=scheduler_thread, daemon=True)
    t.start()
    threading.Thread(target=run_fetch_cycle, daemon=True).start()

    # ── MB SCADA Weather background loop ──────────────────────────────────
    # Starts if at least one weather station is configured (the block-data
    # API confirmed to need NO authentication for the sites tested — login
    # credentials in weather_config.json are now an OPTIONAL fallback only
    # used if a 401 is ever encountered, not a hard requirement to start).
    try:
        import os as _os
        if _os.path.exists("data/weather_sites.json"):
            try:
                import modules.mbscada_scraper as _mb_loop
                threading.Thread(
                    target=_mb_loop.start_background_loop,
                    kwargs={"db_path": "data/pss_weather.db", "interval": 300},
                    daemon=True,
                ).start()
                log.info("MB SCADA weather background loop started")
            except ImportError:
                log.info("mbscada_scraper module not installed — weather "
                         "integration disabled (this is fine if you don't "
                         "need the Circle-wise Weather table)")
        else:
            log.info("data/weather_sites.json not found — weather loop not "
                     "started. Configure stations via Settings → WMS Weather "
                     "Station Configuration, then restart the server.")
    except Exception as e:
        log.warning(f"Weather loop startup check failed: {e}")

    # ── Dynamic port resolution ───────────────────────────────────────────
    # Read port chosen by find_free_port.py (written to data/.port)
    # Falls back to scanning for a free port if file missing
    def _get_port(preferred=7777):
        import socket as _sock
        port_file = os.path.join(BASE_DIR, "data", ".port")
        # Try reading from port file (set by start.bat / tray.pyw)
        try:
            p = int(open(port_file).read().strip())
            log.info(f"Port file: using port {p}")
            return p
        except Exception:
            pass
        # Fallback: find free port ourselves
        def free(p):
            with _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM) as s:
                s.setsockopt(_sock.SOL_SOCKET, _sock.SO_REUSEADDR, 1)
                try: s.bind(("0.0.0.0", p)); return True
                except OSError: return False
        if free(preferred):
            return preferred
        for p in range(7700, 7801):
            if p != preferred and free(p):
                log.info(f"Port {preferred} busy — using {p}")
                return p
        return preferred  # last resort

    PORT = _get_port()
    log.info(f"TPNODL Monitor server starting on http://0.0.0.0:{PORT}")
    log.info(f"Dashboard: http://127.0.0.1:{PORT}  |  http://10.40.107.137:{PORT}")

    # Simple robust startup — use app.run() which handles SO_REUSEADDR internally
    # werkzeug's make_server has a known issue where serve_forever() can exit silently
    import socket as _s, subprocess as _subp

    def _port_busy(p):
        try:
            r = _subp.run(
                f'netstat -ano 2>nul | findstr " :{p} " | findstr "LISTENING"',
                shell=True, capture_output=True, text=True, timeout=3)
            if r.stdout.strip(): return True
        except Exception: pass
        try:
            with _s.socket(_s.AF_INET, _s.SOCK_STREAM) as ts:
                ts.settimeout(0.3)
                return ts.connect_ex(("127.0.0.1", p)) == 0
        except Exception: return False

    # If preferred port is busy, find a free one
    if _port_busy(PORT):
        log.warning(f"Port {PORT} is busy — scanning for free port...")
        found = None
        for p in range(7700, 7851):
            if not _port_busy(p):
                found = p
                break
        if found:
            PORT = found
            port_file = os.path.join(BASE_DIR, "data", ".port")
            open(port_file, "w").write(str(PORT))
            log.info(f"Using port {PORT}")
            log.info(f"Dashboard: http://127.0.0.1:{PORT}  |  http://10.40.107.137:{PORT}")
        else:
            log.error("No free port found in 7700-7850!")
            sys.exit(1)

    try:
        app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
    except Exception as e:
        log.error(f"Server fatal error: {type(e).__name__}: {e}", exc_info=True)
        sys.exit(1)
