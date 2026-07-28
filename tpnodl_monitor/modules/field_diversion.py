"""
modules/field_diversion.py — Manual Field-Level Load Diversion Marking
========================================================================
Allows an operator to mark an active FEEDER_OFF alert as "actually
diverted through a field device" — covering real-world cases the
automatic BC-based detector (BusCouplerDiversionDetector in violation.py)
CANNOT see, because the diversion happens outside the monitored Bus
Coupler path: a field crew physically closes a jumper/switch to route
load through a DIFFERENT feeder, possibly at a DIFFERENT GSS entirely.

DESIGN — kept fully separate from violation.py / alert_store.py by design
(per explicit requirement: "existing system should not hamper"):
  - Own SQLite table (field_diversions) in the SAME alerts.db file, but
    a logically independent table — no foreign keys into the `alerts`
    table's schema, no triggers, no shared transactions with the core
    alert-processing code path.
  - violation.py and alert_store.py are NOT imported or modified by this
    module. This module only READS alert data (via a passed-in AlertStore
    instance) to validate the alert_id being marked still exists/is active,
    and WRITES to its own separate table.
  - All reads needed by mgmt_report.py / index.html go through the
    functions in THIS file only — no other existing module needs to know
    this feature exists for the core system to keep working if this file
    were deleted entirely.

WORKFLOW:
  1. Operator sees an active FEEDER_OFF alert with no automatic BC
     diversion detected.
  2. Operator knows from field reports that load WAS actually diverted —
     e.g. through a jumper to a different feeder, possibly at another GSS.
  3. Operator calls mark_field_diversion(alert_id, source_asset_code, ...)
     — picks the GSS+Feeder the load was diverted FROM (the field device/
     source path), via a dropdown populated from FeederMaster.all().
  4. This alert now shows "Load Diverted (Field Marked)" in the dashboard/
     reports, with the marked source GSS/Feeder included, exactly like
     the automatic LOAD_DIVERTED alerts — but flagged as manually entered.
  5. Operator can un-mark it (e.g. entered by mistake) via clear_field_mark().
  6. Marking auto-clears when the underlying FEEDER_OFF alert itself
     clears (feeder restored) — checked via is_mark_stale().
"""

import sqlite3
import logging
from datetime import datetime

log = logging.getLogger("field_diversion")

DB_PATH = "data/alerts.db"   # same file as AlertStore — separate table only


def _conn():
    return sqlite3.connect(DB_PATH, timeout=10)


def init_table():
    """Create the field_diversions table if it doesn't exist. Safe to call
    on every startup — idempotent, no effect if table already exists."""
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS field_diversions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                alert_id        TEXT NOT NULL,
                asset_code      TEXT NOT NULL,
                feeder_name     TEXT,
                gss_name        TEXT,
                source_asset_code TEXT NOT NULL,
                source_feeder_name TEXT,
                source_gss_name    TEXT,
                diversion_time  TEXT,
                note            TEXT,
                marked_by       TEXT,
                marked_at       TEXT NOT NULL,
                cleared_at      TEXT,
                is_active       INTEGER NOT NULL DEFAULT 1
            )
        """)
        # Migration: add diversion_time to tables created before this field
        # existed — SQLite ALTER TABLE ADD COLUMN is safe to attempt and
        # idempotent-by-effect (we catch the "duplicate column" error).
        try:
            c.execute("ALTER TABLE field_diversions ADD COLUMN diversion_time TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists — fine
        c.execute("CREATE INDEX IF NOT EXISTS idx_fd_alert_id "
                  "ON field_diversions(alert_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_fd_active "
                  "ON field_diversions(is_active)")
        c.commit()
    log.info("field_diversions table ready")


def mark_field_diversion(alert_id: str, asset_code: str, feeder_name: str,
                          gss_name: str, source_asset_code: str,
                          source_feeder_name: str, source_gss_name: str,
                          diversion_time: str = "", note: str = "",
                          marked_by: str = "") -> dict:
    """
    Mark an active FEEDER_OFF alert as field-diverted.

    alert_id, asset_code, feeder_name, gss_name — identify the OFF feeder
        being marked (the one that's actually showing 0A but is field-
        diverted elsewhere).
    source_asset_code, source_feeder_name, source_gss_name — the GSS/
        Feeder the load was physically routed THROUGH (selected by the
        operator from the feeder master list — may be same-GSS or a
        completely different GSS).
    diversion_time — operator-entered ISO timestamp of when the diversion
        ACTUALLY happened in the field (may be well before the marking
        action itself — e.g. marked at 3pm for something that happened at
        11am). Distinct from marked_at, which is always "now" (an audit
        trail of when the operator did the marking, not when the event
        occurred). If not provided, defaults to "now" as a reasonable
        fallback for callers that don't collect this field.
    note — optional free-text (e.g. "Jumper closed to adjacent feeder
        per field crew report, ref ticket #1234").

    Returns the created record as a dict. If an active mark already
    exists for this alert_id, it's cleared first (one active mark per
    alert at a time — re-marking replaces, doesn't duplicate).
    """
    # Auto-clear any existing active mark for this alert before creating
    # a new one — prevents duplicate/stale marks accumulating if an
    # operator re-marks the same alert with corrected source info.
    clear_field_mark(alert_id, reason="superseded by re-mark")

    now = datetime.now().isoformat()
    div_time = diversion_time or now

    with _conn() as c:
        cur = c.execute("""
            INSERT INTO field_diversions
                (alert_id, asset_code, feeder_name, gss_name,
                 source_asset_code, source_feeder_name, source_gss_name,
                 diversion_time, note, marked_by, marked_at, is_active)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,1)
        """, (alert_id, asset_code, feeder_name, gss_name,
              source_asset_code, source_feeder_name, source_gss_name,
              div_time, note, marked_by, now))
        c.commit()
        new_id = cur.lastrowid

    log.info(f"Field diversion marked: {asset_code} ({feeder_name}) "
             f"-> diverted via {source_asset_code} ({source_feeder_name}) "
             f"@ {source_gss_name}, diversion_time={div_time}, "
             f"by={marked_by or 'unknown'}")

    return {
        "id": new_id, "alert_id": alert_id, "asset_code": asset_code,
        "feeder_name": feeder_name, "gss_name": gss_name,
        "source_asset_code": source_asset_code,
        "source_feeder_name": source_feeder_name,
        "source_gss_name": source_gss_name,
        "diversion_time": div_time,
        "note": note, "marked_by": marked_by, "marked_at": now,
        "is_active": True,
    }


def clear_field_mark(alert_id: str, reason: str = "") -> bool:
    """
    Clear (deactivate) the active field-diversion mark for an alert_id.
    Used both for explicit operator un-marking AND for auto-clearing when
    the underlying FEEDER_OFF restores (see auto_clear_stale_marks below).
    Returns True if a mark was actually cleared, False if none was active.
    """
    now = datetime.now().isoformat()
    with _conn() as c:
        cur = c.execute("""
            UPDATE field_diversions SET is_active=0, cleared_at=?
            WHERE alert_id=? AND is_active=1
        """, (now, alert_id))
        c.commit()
        cleared = cur.rowcount > 0
    if cleared:
        log.info(f"Field diversion mark cleared for alert {alert_id}"
                 f"{f' ({reason})' if reason else ''}")
    return cleared


def get_active_mark(alert_id: str) -> dict | None:
    """Return the active field-diversion mark for an alert_id, or None."""
    with _conn() as c:
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT * FROM field_diversions WHERE alert_id=? AND is_active=1 "
            "ORDER BY marked_at DESC LIMIT 1",
            (alert_id,)
        ).fetchone()
    return dict(row) if row else None


def get_all_active_marks() -> list:
    """Return all currently active field-diversion marks — used by
    mgmt_report.py / index.html to annotate the alert list without each
    caller needing to query per-alert_id individually."""
    with _conn() as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT * FROM field_diversions WHERE is_active=1 "
            "ORDER BY marked_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def auto_clear_stale_marks(active_alert_ids: set) -> int:
    """
    Clear any field-diversion mark whose underlying alert_id is no longer
    in the set of currently-active alert IDs (i.e. the FEEDER_OFF it was
    marking has since cleared/restored on its own). Called once per fetch
    cycle from app.py with the current set of active alert IDs — this is
    the ONLY point of contact with the core alert lifecycle, and it's a
    read-only check against a passed-in set, not a live query into
    alert_store's internals.

    Returns the number of marks cleared.
    """
    marks = get_all_active_marks()
    cleared = 0
    for m in marks:
        if m["alert_id"] not in active_alert_ids:
            if clear_field_mark(m["alert_id"], reason="underlying alert no longer active"):
                cleared += 1
    if cleared:
        log.info(f"Auto-cleared {cleared} stale field-diversion mark(s)")
    return cleared


def feeder_picker_options(feeder_master) -> list:
    """
    Build the dropdown options list for the "select source GSS & Feeder"
    picker, from the existing FeederMaster instance (passed in — this
    module never imports feeder_master.py directly, keeping the
    separation clean). Returns a list of dicts suitable for direct JSON
    serialization to the frontend.
    """
    try:
        all_feeders = feeder_master.all()
    except Exception as e:
        log.warning(f"feeder_picker_options: could not read FeederMaster: {e}")
        return []

    options = []
    for f in all_feeders:
        ac = f.get("AssetCode", "")
        if not ac:
            continue
        options.append({
            "asset_code":  ac,
            "feeder_name": f.get("FeederName", ac),
            "gss_name":    f.get("GssName", ""),
            "circle_name": f.get("CircleName", ""),
            "division_name": f.get("DivisionName", ""),
        })
    options.sort(key=lambda o: (o["circle_name"], o["gss_name"], o["feeder_name"]))
    return options
