"""
Management Hourly Report — TPNODL PSCC
Sends summarised violation alerts + circle demand to management contacts.
Configurable interval, separate contact groups, feeder whitelist.
"""
import logging, json, os, smtplib, ssl
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text      import MIMEText

# Optional weather integration — see weather_report.py docstring for the
# "never break the core report" design rationale. If this module or its
# own mbscada_scraper dependency is missing, the weather table is simply
# omitted from reports rather than causing a startup/report failure.
try:
    from modules.weather_report import get_circle_weather_rows, build_weather_html_table, get_weather_anomalies
    _WEATHER_AVAILABLE = True
except ImportError:
    _WEATHER_AVAILABLE = False
    def get_circle_weather_rows(as_of=None): return {}
    def build_weather_html_table(cw, circles): return ""
    def get_weather_anomalies(): return []

log = logging.getLogger(__name__)

REPORT_TYPES = {"OV","UV","OL","FEEDER_OFF","LOAD_DIVERTED","LINE_JUMPER_PARTING",
                "BC_TRIP_WHILE_DIVERTED"}
# Note: COMM_DOWN is deliberately excluded — communication loss is an operational
# monitoring concern for the live dashboard and instant WA/email alerts only; it
# must not appear in the Management Report which management circulates.


class ManagementReporter:
    def __init__(self, cfg, alert_store, scraper, peak_store, fm, wa_mgr=None,
                 server_start_ts=None, email_mgr=None):
        self.cfg         = cfg
        self.store       = alert_store
        self.scraper     = scraper
        self.peak_store  = peak_store
        self.fm          = fm
        self.wa          = wa_mgr
        self.email       = email_mgr  # EmailManager — routes via Gmail API or
                                       # SMTP depending on email.use_gmail_api.
                                       # Management reports previously had
                                       # their OWN separate _smtp_send() below,
                                       # completely disconnected from the
                                       # Gmail API work done in email_mgr.py —
                                       # that's why alerts switched to Gmail
                                       # API correctly but management reports
                                       # kept silently trying (blocked) SMTP.
        self._last_sent_slot = None   # tracks last sent (slot string "YYYY-MM-DD HH:MM")
        # server_start_ts: used ONLY to delay a catch-up send by a few seconds
        # if restart happened to land right at a slot boundary (so live data
        # has a moment to populate first). This NEVER skips a slot — it only
        # defers the send within the SAME slot. The slot is still marked
        # "pending" (not sent) until send_now() actually runs, so the very
        # next fetch cycle after the grace window will send it. A slot can
        # never be silently missed because of this.
        self._server_start_ts = server_start_ts

    @staticmethod
    def _current_slot(interval_min: int) -> str:
        """
        Return the current reporting slot as 'YYYY-MM-DD HH:MM'.
        interval=60 → slots at :00 of every hour
        interval=30 → slots at :00 and :30 of every hour
        interval=120 → slots at :00 of every even hour
        interval=240 → slots at :00 every 4 hours
        """
        from datetime import datetime as _dt
        now = _dt.now()
        # How many full interval_min blocks have elapsed since midnight?
        mins_since_midnight = now.hour * 60 + now.minute
        slot_num    = mins_since_midnight // interval_min
        slot_minute = slot_num * interval_min
        slot_hour   = slot_minute // 60
        slot_min    = slot_minute % 60
        return now.strftime(f"%Y-%m-%d {slot_hour:02d}:{slot_min:02d}")

    # ── Schedule check (called from fetch cycle) ──────────────────────────────
    # GUARANTEE: every configured slot fires EXACTLY ONCE, never zero times.
    # The grace-window check below only ever DELAYS a send within the same
    # slot — it never marks the slot as sent, so if this cycle is skipped,
    # the very next fetch cycle (a few minutes later, same slot) will try
    # again and succeed. There is no path here that can cause a slot to be
    # silently missed for the whole hour.
    STARTUP_GRACE_SEC = 90  # only matters if restart landed within this many
                             # seconds of a slot boundary — otherwise this
                             # check is a no-op and behaviour is unchanged

    # Weather data (MB SCADA block-data API) lags behind the report slot
    # boundary by a short, variable margin — the 15-min block isn't always
    # immediately available right at :00. This delay applies EVERY slot
    # (not just after a restart, unlike STARTUP_GRACE_SEC above): the report
    # waits this many seconds past the slot boundary before generating, so
    # weather data has time to land. Same "defer, never skip" guarantee as
    # the startup grace window — if this cycle defers, the slot stays
    # pending and the next fetch cycle (a few minutes later, same slot)
    # tries again; the slot still fires exactly once, just slightly later.
    WEATHER_DATA_DELAY_SEC = 90  # 1.5 min — within the requested 1-1.5 min range

    def check_and_send(self):
        interval_min = int(self.cfg.get("mgmt_report.interval_min", 60))
        if not self.cfg.get("mgmt_report.enabled", False):
            return

        slot = self._current_slot(interval_min)

        # Already sent in this slot — skip (survives restart via cfg persistence)
        last_slot = self.cfg.get("mgmt_report.last_sent_slot", "")
        if slot == last_slot or slot == self._last_sent_slot:
            return

        # Grace window: if the server JUST restarted (within STARTUP_GRACE_SEC)
        # AND we're right at the very start of this slot, defer by a few
        # cycles so live data has time to populate before the report reads it.
        # This does NOT mark the slot sent — it simply returns early and lets
        # the next fetch cycle (well within the same slot, since slots are
        # 30-240 min long) try again. The slot is still pending, guaranteeing
        # it fires once this hour, just not on the very first post-restart tick.
        if self._server_start_ts is not None:
            import time as _time
            uptime = _time.time() - self._server_start_ts
            if uptime < self.STARTUP_GRACE_SEC:
                log.info(f"Management report: deferring slot {slot} — "
                         f"server uptime {uptime:.0f}s < {self.STARTUP_GRACE_SEC}s "
                         f"grace window (will retry next cycle, same slot)")
                return

        # Weather-data delay: wait WEATHER_DATA_DELAY_SEC past the slot
        # boundary every time (not just after a restart), so the MB SCADA
        # block-data API has had a chance to publish this period's block
        # before the Circle-wise Weather table is built. Computed from the
        # slot's own boundary time, not server uptime, so it applies
        # consistently on every report regardless of restart timing.
        import time as _time
        from datetime import datetime as _dt
        slot_dt = _dt.strptime(slot, "%Y-%m-%d %H:%M")
        seconds_into_slot = (_dt.now() - slot_dt).total_seconds()
        if seconds_into_slot < self.WEATHER_DATA_DELAY_SEC:
            log.info(f"Management report: deferring slot {slot} — only "
                     f"{seconds_into_slot:.0f}s into slot, waiting for "
                     f"{self.WEATHER_DATA_DELAY_SEC}s to allow weather data "
                     f"to become available (will retry next cycle, same slot)")
            return

        # Mark sent BEFORE generating to prevent double-fire if send is slow
        self._last_sent_slot = slot
        self.cfg.set("mgmt_report.last_sent_slot", slot)

        log.info(f"Management report: generating for slot {slot}...")
        self.send_now(report_time=slot_dt)

    # ── Build and send (email + WhatsApp) ─────────────────────────────────────
    def send_now(self, report_time=None):
        """
        report_time: the report's NOMINAL slot time (e.g. the hourly slot
        boundary). When None (manual "Send Report Now" button), defaults
        to right now — a manual send is always a live snapshot, there's
        no slot it's standing in for.

        This is what the weather table's 1-hour window is anchored to —
        see weather_report.get_circle_weather_rows()/mbscada_scraper.
        get_circle_weather_now() for why this must be the report's own
        slot time and not whatever "now" happens to be when this runs.
        """
        report_time = report_time or datetime.now()
        email_ok = False
        wa_ok    = False
        # NOTE: weather data is fetched lazily, inside _build_html() via
        # get_circle_weather_rows(as_of=report_time) — that call already
        # does a fresh, slot-aligned live fetch when as_of is given, so
        # there's no need to pre-warm anything here (an earlier version
        # of this method did a separate forced fetch first; that became
        # redundant once get_circle_weather_rows() started doing its own
        # live fetch per report_time, and would have just doubled API
        # calls to the MB SCADA endpoint on every report).

        # Email — routed through EmailManager so it respects
        # email.use_gmail_api exactly like regular violation alerts do.
        # Falls back to this class's own _smtp_send only if email_mgr
        # wasn't wired in (e.g. older app.py not yet updated).
        email_rcpt = self.cfg.get("mgmt_report.email_recipients", [])
        if email_rcpt:
            html  = self._build_html(report_time)
            plain = self._build_plain()
            now_str = datetime.now().strftime("%d-%b-%Y %H:%M")
            subject = f"⚡ TPNODL PSCC | Realtime Grid Health & Operational Alert Report | {now_str}"
            if self.email:
                email_ok = self.email._send_email(email_rcpt, subject, plain, html)
            else:
                log.warning("Management report: email_mgr not wired in — "
                            "falling back to legacy direct SMTP (will fail "
                            "if SMTP ports are blocked)")
                email_ok = self._smtp_send(email_rcpt, html, plain)
            if email_ok:
                log.info(f"Management report emailed to {len(email_rcpt)} recipients")
        else:
            log.warning("Management report: no email recipients configured")

        # WhatsApp
        wa_rcpt = self.cfg.get("mgmt_report.wa_recipients", [])
        if wa_rcpt and self.wa:
            wa_msg = self._build_wa_message()
            # Small delay to prevent merging with violation alert messages
            import time as _time
            _time.sleep(2)
            wa_ok  = self.wa.send(to=wa_rcpt, message=wa_msg)
            if wa_ok:
                log.info(f"Management report sent via WA to {len(wa_rcpt)} recipients")
        elif wa_rcpt and not self.wa:
            log.warning("Management report: WA recipients configured but WA manager not available")

        return email_ok or wa_ok

    # ── Feeder whitelist filter ───────────────────────────────────────────────
    def _is_monitored(self, alert: dict) -> bool:
        """
        Returns True if this alert's feeder should appear in the report.
        - Bus Couplers always excluded — EXCEPT BC_TRIP_WHILE_DIVERTED,
          which is explicitly in REPORT_TYPES on purpose: its Feeder field
          is "{diverted feeder}🔀{BC name} ({BC asset})", which contains
          "BUS COUPL" from the BC's own name and would otherwise always
          get caught by the blanket exclusion below — even though this
          alert represents the diverted feeder's ACTUAL supply being OFF
          (the BC was its only remaining path and that just tripped too),
          not routine BC idle-state noise.
        - If whitelist configured: only include listed feeders
        - If blacklist configured: exclude listed feeders
        """
        ac = alert.get("AssetCode","")
        feeder = (alert.get("Feeder") or "").upper()
        if alert.get("type") != "BC_TRIP_WHILE_DIVERTED" and \
           ("BUS COUPL" in feeder or alert.get("IsBusCoupler")):
            return False
        whitelist = self.cfg.get("mgmt_report.feeder_whitelist", [])
        blacklist = self.cfg.get("mgmt_report.feeder_blacklist", [])
        if whitelist:
            return ac in whitelist or any(w in feeder for w in whitelist)
        if blacklist:
            return ac not in blacklist
        return True

    # ── Smart diversion matching ──────────────────────────────────────────────
    def _annotate_field_marks(self, alerts: list) -> list:
        """
        Apply operator-entered field-level diversion marks (see
        modules/field_diversion.py) onto matching FEEDER_OFF alerts in the
        given list. This is ADDITIVE — runs after _match_diversion() — and
        only touches alerts that don't already have an automatic
        _diversion_via from the BC-based detector (a feeder can't be both
        automatically AND manually marked as diverted at the same time;
        automatic detection always takes priority since it's based on
        live current readings, not operator judgment).

        Field-marked alerts get the SAME _diversion_via / _is_diverted_off /
        severity-downgrade treatment as automatic diversions, so they
        render identically in reports — just with source info reflecting
        the manually-selected GSS/Feeder instead of a Bus Coupler asset,
        and a "(Field Marked)" tag so it's clear this came from operator
        input rather than automatic detection.
        """
        try:
            from modules.field_diversion import get_all_active_marks
        except ImportError:
            return alerts  # field_diversion module not installed — no-op

        try:
            marks = get_all_active_marks()
        except Exception as e:
            log.warning(f"_annotate_field_marks: could not read marks: {e}")
            return alerts

        if not marks:
            return alerts

        marks_by_alert_id = {m["alert_id"]: m for m in marks}

        for a in alerts:
            if a.get("type") != "FEEDER_OFF" or a.get("_diversion_via"):
                continue  # not OFF, or already has an automatic match
            aid = a.get("id","")
            mark = marks_by_alert_id.get(aid)
            if not mark:
                continue
            a["_diversion_via"]      = mark.get("source_asset_code","")
            a["_diversion_via_name"] = mark.get("source_feeder_name","")
            a["_diversion_via_gss"]  = mark.get("source_gss_name","")
            a["_is_diverted_off"]    = True
            a["_is_field_marked"]    = True   # distinguishes from automatic
            a["_field_mark_note"]    = mark.get("note","")
            a["severity"]            = "INFO"  # same downgrade as automatic
        return alerts

    def _match_diversion(self, alerts: list) -> list:
        """
        For each LOAD_DIVERTED alert, find the most likely OFF feeder it maps to.
        When multiple feeders are OFF and BC load > 400%, use historical load pattern
        to decide which feeder matches the BC load.
        Returns enriched alert list with DIV alerts merged into their OFF feeder.
        """
        div_alerts  = [a for a in alerts if a.get("type") == "LOAD_DIVERTED"]
        off_alerts  = [a for a in alerts if a.get("type") == "FEEDER_OFF"]
        other       = [a for a in alerts if a.get("type") not in ("LOAD_DIVERTED","FEEDER_OFF")]

        # Group by GSS
        from collections import defaultdict
        gss_offs = defaultdict(list)
        gss_divs = defaultdict(list)
        for a in off_alerts:  gss_offs[a.get("Gss","")].append(a)
        for a in div_alerts:  gss_divs[a.get("Gss","")].append(a)

        merged = []
        for gss, offs in gss_offs.items():
            divs = gss_divs.get(gss, [])
            if not divs:
                # No diversion at this GSS — include OFF alerts normally
                merged.extend(offs)
                continue

            if len(offs) == 1 and len(divs) == 1:
                # Simple 1:1 mapping — merge DIV into OFF.
                # IMPORTANT: a feeder whose load has been successfully
                # diverted via BC is NOT a real outage from the consumer's
                # perspective — supply continues uninterrupted through the
                # bus coupler. Downgrade severity to INFO so it doesn't
                # inflate the Critical count in management reports, and tag
                # it so the report-level "any real alerts?" check (below)
                # can correctly treat an all-diversion report as "Normal".
                off = offs[0]
                div = divs[0]
                off = dict(off)
                off["_diversion_via"]   = div.get("AssetCode","")
                off["_bc_load_a"]       = div.get("BCImaxAfter", div.get("value"))
                off["_is_diverted_off"] = True
                off["severity"]         = "INFO"
                merged.append(off)
            elif len(offs) > 1 and divs:
                # Multiple feeders OFF at this GSS. Each LOAD_DIVERTED alert's
                # own AssetCode IS the confirmed feeder it belongs to (the
                # violation engine already confirmed this match via BC ratio +
                # pre-fault load checks — see BusCouplerDiversionDetector).
                # That confirmed identity must NEVER be overridden by this
                # report-level heuristic scoring, which only exists to guess
                # at attribution for feeders that have NO confirmed DIV alert
                # of their own (legacy data / detection gap cases).
                #
                # BUG FIXED: previously every OFF feeder at the GSS was scored
                # against divs[0] purely on load-magnitude/yesterday-pattern
                # similarity, with no check for "is this feeder ALREADY the
                # confirmed source of a DIV alert". If a different, unrelated
                # OFF feeder's pre-trip load happened to score closer to the
                # BC's current (fluctuating) reading than the actually-
                # confirmed feeder's own pre-trip load, the wrong feeder got
                # falsely tagged "Load Diverted via BC <unrelated feeder's
                # own AssetCode>" — exactly what happened with BHADRASAHI
                # being mismatched against BARBIL-I's confirmed diversion.
                div_by_asset = {d.get("AssetCode",""): d for d in divs}
                confirmed_offs  = [o for o in offs if o.get("AssetCode","") in div_by_asset]
                unconfirmed_offs = [o for o in offs if o.get("AssetCode","") not in div_by_asset]
                unconfirmed_divs = [d for d in divs
                                    if d.get("AssetCode","") not in
                                    {o.get("AssetCode","") for o in confirmed_offs}]

                # Confirmed matches — merge directly, no scoring needed
                for off in confirmed_offs:
                    div = div_by_asset[off.get("AssetCode","")]
                    off_copy = dict(off)
                    off_copy["_diversion_via"]   = div.get("AssetCode","")
                    off_copy["_bc_load_a"]       = div.get("BCImaxAfter", div.get("value"))
                    off_copy["_is_diverted_off"] = True
                    off_copy["severity"]         = "INFO"
                    merged.append(off_copy)

                # Remaining unconfirmed OFFs vs remaining unconfirmed DIVs —
                # heuristic scoring only applies here, where there's genuine
                # ambiguity (no feeder already has its own confirmed match)
                if unconfirmed_offs and unconfirmed_divs:
                    matched = self._match_by_load_pattern(unconfirmed_offs, unconfirmed_divs, gss)
                    for m in matched:
                        if m.get("_diversion_via"):
                            m["_is_diverted_off"] = True
                            m["severity"]         = "INFO"
                    merged.extend(matched)
                elif unconfirmed_offs:
                    # No diversion candidates left to attribute — plain OFF
                    merged.extend(unconfirmed_offs)
            else:
                merged.extend(offs)
                merged.extend(divs)

        # Add GSS-level DIV alerts for GSS not in offs
        for gss, divs in gss_divs.items():
            if gss not in gss_offs:
                merged.extend(divs)

        merged.extend(other)
        # Sort by severity then first_seen
        sev_order = {"CRITICAL":0,"HIGH":1,"MEDIUM":2,"INFO":3}
        merged.sort(key=lambda a: (sev_order.get(a.get("severity","INFO"),3),
                                   a.get("first_seen","")))
        return merged

    def _match_by_load_pattern(self, offs: list, divs: list, gss: str) -> list:
        """
        Attribute an unconfirmed OFF feeder to a diversion ONLY when there is
        an actual, real BC_LOADING_START event (the BC's own idle→active
        transition, recorded independently by the violation engine — see
        VT_BCL in violation.py) that satisfies BOTH:
          1. TIMING — the BC_LOADING_START happened AT OR AFTER this feeder's
             own FEEDER_OFF.first_seen (a diversion can't start before the
             feeder that caused it actually tripped)
          2. MAGNITUDE — the recorded BC rise (bc_rise) is a plausible match
             for this feeder's own tripped load portion (within tolerance)

        This replaces the old generic load-magnitude/yesterday-pattern
        scoring, which had no requirement that an actual BC rise event ever
        occurred near the feeder's trip time — it could match purely on
        "whichever OFF feeder's old load number happens to be numerically
        closest to BC's CURRENT reading right now", with no time correlation
        at all. That's what let an unrelated feeder (BHADRASAHI) get matched
        against a diversion that had nothing to do with it.

        If no OFF feeder has a qualifying BC_LOADING_START event, NONE of
        them are tagged as diverted — they stay as plain Feeder OFF, which
        is the correct, conservative outcome when there's no real evidence.
        """
        import re

        RISE_TOLERANCE_PCT = 0.35  # bc_rise must be within ±35% of the
                                     # feeder's own tripped load to count
                                     # as a plausible magnitude match

        def get_pretripload(off_alert):
            m = re.search(r"(\d+\.?\d*)A→", off_alert.get("detail",""))
            return float(m.group(1)) if m else 0

        def get_bc_loading_events(gss_name: str) -> list:
            """Fetch BC_LOADING_START events recorded at this GSS (any time —
            we need the historical event closest to each feeder's trip,
            not just currently-active ones)."""
            if not self.store:
                return []
            try:
                rows = self.store.all(vtype="BC_LOADING_START", limit=200)
                return [r for r in rows if r.get("Gss","") == gss_name]
            except Exception as e:
                log.warning(f"BC_LOADING_START lookup failed for {gss_name}: {e}")
                return []

        bcl_events = get_bc_loading_events(gss)

        result = []
        for off in offs:
            ac        = off.get("AssetCode","")
            pre_load  = get_pretripload(off)
            off_ts    = off.get("first_seen","")

            # Find a BC_LOADING_START at/after this feeder's trip time whose
            # rise magnitude plausibly matches this feeder's own tripped load
            best_event = None
            best_diff  = None
            for ev in bcl_events:
                ev_ts = ev.get("first_seen","")
                if not ev_ts or not off_ts or ev_ts < off_ts:
                    continue  # BC rise must be AT/AFTER this feeder's trip
                bc_rise = float(ev.get("bc_rise") or 0)
                if pre_load <= 0 or bc_rise <= 0:
                    continue
                diff_pct = abs(bc_rise - pre_load) / pre_load
                if diff_pct <= RISE_TOLERANCE_PCT:
                    if best_diff is None or diff_pct < best_diff:
                        best_diff  = diff_pct
                        best_event = ev

            off_copy = dict(off)
            if best_event:
                # Find the matching LOAD_DIVERTED alert for this BC (if any) —
                # if it exists, its BCImaxAfter is a more current/accurate BC
                # reading than the BC_LOADING_START event's value (which is
                # just a snapshot from the moment the rise was first detected,
                # possibly stale by now if the diversion has been confirmed
                # and BC load has since settled/fluctuated).
                bc_asset = best_event.get("AssetCode","")
                matching_div = next(
                    (d for d in divs if d.get("BusCouplerAsset","") == bc_asset
                     or d.get("AssetCode","") == bc_asset), None)
                bc_load_display = (
                    float(matching_div.get("BCImaxAfter") or matching_div.get("value") or 0)
                    if matching_div else float(best_event.get("value") or 0)
                )
                off_copy["_diversion_via"]   = bc_asset
                off_copy["_bc_load_a"]       = bc_load_display
                off_copy["_match_pre_load"]  = pre_load
                off_copy["_match_rise_diff_pct"] = round(best_diff * 100, 1)
                log.info(f"Diversion match (BC_LOADING_START correlated): "
                         f"{off.get('Feeder')} pre_load={pre_load:.1f}A "
                         f"bc_rise={best_event.get('bc_rise')}A "
                         f"diff={best_diff*100:.1f}% "
                         f"@ {best_event.get('first_seen','?')[:19]}")
            else:
                log.debug(f"No qualifying BC_LOADING_START for {ac} "
                          f"(pre_load={pre_load:.1f}A) — staying as plain OFF")
            result.append(off_copy)
        return result

    # ── HTML report builder ───────────────────────────────────────────────────
    def _build_html(self, report_time=None) -> str:
        now_str  = datetime.now().strftime("%d-%b-%Y %H:%M")
        interval = self.cfg.get("mgmt_report.interval_min", 60)

        # Active alerts
        all_alerts = self.store.all(active_only=True, limit=300)
        monitored  = [a for a in all_alerts if a.get("type") in REPORT_TYPES
                      and self._is_monitored(a)]
        merged     = self._annotate_field_marks(self._match_diversion(monitored))

        # Circle demand
        live       = self.scraper.last_data
        circles    = {}
        for r in live:
            c = r.get("Circle","")
            if c:
                circles.setdefault(c, {"MW":0,"MVA":0})
                circles[c]["MW"]  += float(r.get("ActivePower") or 0)
                circles[c]["MVA"] += float(r.get("ApparentPower") or 0)

        # Peak
        peaks = self.peak_store.get_current() if self.peak_store else {}
        daily = peaks.get("daily", {})

        TYPE_COLOR = {
            "OV":"#ff3d71","UV":"#ffd740","OL":"#ff9100",
            "FEEDER_OFF":"#888","LOAD_DIVERTED":"#f783ac",
            "LINE_JUMPER_PARTING":"#ff6b6b","PT_PHASE_MISSING":"#ff6b6b",
        }
        TYPE_ICON  = {
            "OV":"⚡","UV":"⬇","OL":"🔴","FEEDER_OFF":"⚫",
            "LOAD_DIVERTED":"🔀","LINE_JUMPER_PARTING":"⛓️","PT_PHASE_MISSING":"⚠️",
        }
        TYPE_LABEL = {
            "OV":"Over Voltage","UV":"Under Voltage","OL":"Overload",
            "FEEDER_OFF":"Feeder OFF","LOAD_DIVERTED":"Load Diverted",
            "LINE_JUMPER_PARTING":"Line Jumper Parting","PT_PHASE_MISSING":"PT Phase Missing",
        }

        def fmt_duration(first_seen: str) -> str:
            try:
                dt  = datetime.fromisoformat(first_seen)
                sec = int((datetime.now() - dt).total_seconds())
                h, m = divmod(sec // 60, 60)
                return f"{h}h {m}m" if h else f"{m}m"
            except Exception:
                return "—"

        # Alert rows — grouped by Circle, with a header row per circle.
        # Detail text is shown in FULL (previous [:100] truncation cut off
        # the affected-feeder list for UV/OV alerts with many feeders).
        alert_rows  = ""
        alert_cards = ""   # mobile stacked-card version, shown only <600px
        if merged:
            from collections import OrderedDict
            by_circle = OrderedDict()
            for a in merged:
                circ = a.get("Circle","") or "Unspecified"
                by_circle.setdefault(circ, []).append(a)

            for circ, circle_alerts in by_circle.items():
                alert_rows += f"""
<tr>
  <td colspan="5" style="padding:10px 10px 6px 10px;background:#eef2f7;font-weight:700;font-size:13px;color:#34495e;border-top:2px solid #d6dde5">
    📍 {circ} ({len(circle_alerts)})
  </td>
</tr>"""
                alert_cards += f"""
<div style="background:#eef2f7;padding:10px 14px;font-weight:700;font-size:13px;color:#34495e;border-top:2px solid #d6dde5;margin-top:10px">
  📍 {circ} ({len(circle_alerts)})
</div>"""
                for a in circle_alerts:
                    t     = a.get("type","")
                    color = TYPE_COLOR.get(t, "#888")
                    icon  = TYPE_ICON.get(t, "•")
                    label = TYPE_LABEL.get(t, t)
                    dur   = fmt_duration(a.get("first_seen") or a.get("timestamp",""))
                    gss   = a.get("Gss","")
                    det   = a.get("detail","")   # full text — no truncation
                    val   = a.get("value")

                    # For UV/OV: feeder = GSS name only (no "GSS: GSS_NAME" duplication)
                    if t in ("OV","UV"):
                        feeder_display = gss
                        loc = f"{a.get('Circle','')} / {a.get('Division','')}"
                        val_with_unit = f"{val:.3f} kV" if val else "—"
                    else:
                        feeder_display = a.get("Feeder") or a.get("AssetCode","")
                        loc = f"{a.get('Circle','')} / {a.get('Division','')} | {gss}"
                        if t == "OL":
                            val_with_unit = f"{val:.1f}%" if val else "—"
                        else:
                            val_with_unit = f"{val:.1f}A" if val else "—"

                    div_note = ""
                    div_note_card = ""
                    if a.get("_diversion_via"):
                        if a.get("_is_field_marked"):
                            via_name = a.get("_diversion_via_name") or a["_diversion_via"]
                            via_gss  = a.get("_diversion_via_gss","")
                            div_note = (f'<br><span style="color:#9b59b6;font-size:10px">'
                                        f'🔀 Load diverted via field device — {via_name} '
                                        f'@ {via_gss} <i>(Field Marked)</i></span>')
                            div_note_card = (f'<div style="margin-top:6px;font-size:12px;color:#9b59b6">'
                                              f'🔀 Load diverted via field device — {via_name} '
                                              f'@ {via_gss} <i>(Field Marked)</i></div>')
                        else:
                            bc_load = a.get("_bc_load_a")
                            bc_load_str = f"{bc_load:.0f}A" if isinstance(bc_load,(int,float)) else "?"
                            div_note = f'<br><span style="color:#f783ac;font-size:10px">🔀 Load diverted via BC {a["_diversion_via"]} ({bc_load_str})</span>'
                            div_note_card = f'<div style="margin-top:6px;font-size:12px;color:#f783ac">🔀 Load diverted via BC {a["_diversion_via"]} ({bc_load_str})</div>'

                    alert_rows += f"""
<tr>
  <td style="padding:9px 10px;border-bottom:1px solid #e8ecf0">
    <span style="background:{color};color:#fff;padding:2px 8px;border-radius:3px;font-size:12px;font-weight:600">{icon} {label}</span>
    <span style="font-size:11px;color:#888;margin-left:6px">{val_with_unit}</span>
  </td>
  <td style="padding:9px 10px;border-bottom:1px solid #e8ecf0;font-weight:600">{feeder_display}{div_note}</td>
  <td style="padding:9px 10px;border-bottom:1px solid #e8ecf0;font-size:12px;color:#555">{loc}</td>
  <td style="padding:9px 10px;border-bottom:1px solid #e8ecf0;font-size:12px;color:#333;white-space:normal;word-break:break-word;max-width:320px">{det}</td>
  <td style="padding:9px 10px;border-bottom:1px solid #e8ecf0;text-align:center">
    <span style="background:#fff3cd;color:#856404;border-radius:3px;padding:2px 8px;font-size:12px;font-weight:600">{dur}</span>
  </td>
</tr>"""

                    # Mobile stacked card — compact, touch-friendly, full detail
                    # text visible without horizontal scrolling (the #1 mobile
                    # readability complaint with table layouts in Outlook/Gmail
                    # mobile apps).
                    alert_cards += f"""
<div style="background:#fff;border:1px solid #e8ecf0;border-left:4px solid {color};border-radius:6px;padding:12px 14px;margin:8px 12px">
  <div style="display:flex;justify-content:space-between;align-items:flex-start">
    <span style="background:{color};color:#fff;padding:3px 10px;border-radius:4px;font-size:12px;font-weight:700">{icon} {label}</span>
    <span style="background:#fff3cd;color:#856404;border-radius:4px;padding:3px 10px;font-size:12px;font-weight:700;white-space:nowrap">⏱ {dur}</span>
  </div>
  <div style="font-size:15px;font-weight:700;color:#1a1a1a;margin-top:8px">{feeder_display}</div>
  <div style="font-size:12px;color:#777;margin-top:2px">📍 {loc}</div>
  <div style="font-size:13px;color:#333;margin-top:8px;line-height:1.5">⚡ {val_with_unit} &nbsp;·&nbsp; {det}</div>
  {div_note_card}
</div>"""
        else:
            alert_rows  = '<tr><td colspan="5" style="padding:16px;text-align:center;color:#28a745;font-weight:600">✅ No active alerts</td></tr>'
            alert_cards = '<div style="padding:20px;text-align:center;color:#28a745;font-weight:600;font-size:14px">✅ No active alerts</div>'

        # Circle demand rows
        circle_rows = ""
        total_mw = 0
        for circ, d in sorted(circles.items()):
            mw  = round(d["MW"], 1)
            mva = round(d["MVA"], 1)
            pk  = round(daily.get(circ, {}).get("peak_mw", mw), 1)
            total_mw += mw
            circle_rows += f"""
<tr>
  <td style="padding:7px 12px;border-bottom:1px solid #e8ecf0;font-weight:600">{circ}</td>
  <td style="padding:7px 12px;border-bottom:1px solid #e8ecf0;text-align:right;font-family:monospace">{mw:.1f}</td>
  <td style="padding:7px 12px;border-bottom:1px solid #e8ecf0;text-align:right;font-family:monospace">{mva:.1f}</td>
  <td style="padding:7px 12px;border-bottom:1px solid #e8ecf0;text-align:right;font-family:monospace;color:#0066cc">{pk:.1f}</td>
</tr>"""

        alert_count = len(merged)
        sev_counts  = {}
        for a in merged:
            s = a.get("severity","MEDIUM")
            sev_counts[s] = sev_counts.get(s,0) + 1
        crit = sev_counts.get("CRITICAL",0)
        high = sev_counts.get("HIGH",0)

        return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  /* Responsive strategy (Suggestion #6): hybrid email-safe media queries.
     Desktop (>600px): table layout, mobile cards hidden.
     Mobile (<=600px): table hidden, stacked alert cards shown instead.
     This preserves Outlook desktop compatibility (which ignores @media
     and simply shows the table) while giving Gmail/Outlook mobile apps
     (which DO respect @media) a touch-friendly, non-scrolling layout. */
  .tpnodl-desktop-table {{ display: table; }}
  .tpnodl-mobile-cards  {{ display: none; }}
  @media only screen and (max-width: 600px) {{
    .tpnodl-desktop-table {{ display: none !important; }}
    .tpnodl-mobile-cards  {{ display: block !important; }}
    .tpnodl-kpi-card      {{ display: block !important; width: 100% !important;
                             margin-bottom: 8px !important; }}
  }}
</style>
</head>
<body style="margin:0;padding:16px;background:#f0f4f8;font-family:Arial,sans-serif">
<table width="100%" cellpadding="0" cellspacing="0" style="max-width:750px;margin:0 auto">

<!-- Header -->
<tr><td style="background:#0a1628;border-radius:8px 8px 0 0;padding:18px 22px">
  <table width="100%" cellpadding="0" cellspacing="0"><tr>
    <td>
      <div style="color:#00c4ff;font-size:21px;font-weight:bold">⚡ TPNODL PSCC</div>
      <div style="color:#8899bb;font-size:13px;margin-top:2px">Management Alert Report — {now_str}</div>
      <div style="color:#8899bb;font-size:12px;margin-top:1px">Reporting interval: every {interval} minutes</div>
    </td>
  </tr></table>
</td></tr>

<!-- Suggestion #1: Executive KPI Cards — replaces the single-line summary
     bar with 4 compact, scannable cards. Falls back gracefully to a
     2-column wrap on narrow screens, full-width stack on mobile (<600px,
     via .tpnodl-kpi-card media rule above). -->
<tr><td style="background:#141c35;padding:14px 16px">
  <table width="100%" cellpadding="0" cellspacing="0"><tr>
    <td class="tpnodl-kpi-card" width="25%" style="padding:4px">
      <div style="background:#1c2847;border-radius:8px;padding:12px;text-align:center">
        <div style="color:#8899bb;font-size:11px;text-transform:uppercase;letter-spacing:0.5px">Total Load</div>
        <div style="color:#fff;font-size:20px;font-weight:700;margin-top:4px">{total_mw:.1f}<span style="font-size:13px;color:#8899bb"> MW</span></div>
      </div>
    </td>
    <td class="tpnodl-kpi-card" width="25%" style="padding:4px">
      <div style="background:#1c2847;border-radius:8px;padding:12px;text-align:center">
        <div style="color:#8899bb;font-size:11px;text-transform:uppercase;letter-spacing:0.5px">Active Alerts</div>
        <div style="color:#00c4ff;font-size:20px;font-weight:700;margin-top:4px">{alert_count}</div>
      </div>
    </td>
    <td class="tpnodl-kpi-card" width="25%" style="padding:4px">
      <div style="background:#1c2847;border-radius:8px;padding:12px;text-align:center">
        <div style="color:#8899bb;font-size:11px;text-transform:uppercase;letter-spacing:0.5px">Critical</div>
        <div style="color:{'#ff3d71' if crit>0 else '#5a6a8a'};font-size:20px;font-weight:700;margin-top:4px">{crit}</div>
      </div>
    </td>
    <td class="tpnodl-kpi-card" width="25%" style="padding:4px">
      <div style="background:#1c2847;border-radius:8px;padding:12px;text-align:center">
        <div style="color:#8899bb;font-size:11px;text-transform:uppercase;letter-spacing:0.5px">High</div>
        <div style="color:{'#ff9100' if high>0 else '#5a6a8a'};font-size:20px;font-weight:700;margin-top:4px">{high}</div>
      </div>
    </td>
  </tr></table>
</td></tr>

<!-- Circle demand (Suggestion #2: kept unchanged structurally, just
     larger type/padding per Suggestions #4/#5) -->
<tr><td style="background:#fff;padding:0">
  <div style="background:#f8f9fa;padding:10px 18px;font-weight:700;font-size:15px;border-bottom:2px solid #dee2e6">
    📊 Circle-wise Demand
  </div>
  <table width="100%" cellpadding="0" cellspacing="0" style="font-size:13px">
    <thead><tr style="background:#e9ecef">
      <th style="padding:10px 12px;text-align:left;color:#555">Circle</th>
      <th style="padding:10px 12px;text-align:right;color:#555">MW Now</th>
      <th style="padding:10px 12px;text-align:right;color:#555">MVA Now</th>
      <th style="padding:10px 12px;text-align:right;color:#555">Day Peak MW</th>
    </tr></thead>
    <tbody>{circle_rows}</tbody>
    <tfoot><tr style="background:#e9ecef;font-weight:700">
      <td style="padding:10px 12px">TOTAL</td>
      <td style="padding:10px 12px;text-align:right;font-family:monospace">{total_mw:.1f}</td>
      <td colspan="2"></td>
    </tfoot>
  </table>
</td></tr>

{build_weather_html_table(get_circle_weather_rows(as_of=report_time), list(circles.keys()))}

<!-- Active alerts — Suggestion #3 + #6: desktop table (.tpnodl-desktop-table)
     hidden on mobile, replaced by stacked cards (.tpnodl-mobile-cards) -->
<tr><td style="background:#fff;padding:0;margin-top:16px">
  <div style="background:#f8f9fa;padding:10px 18px;font-weight:700;font-size:15px;
    border-bottom:2px solid #dee2e6;border-top:8px solid #f0f4f8">
    🚨 Active Violation Alerts ({alert_count})
  </div>

  <table width="100%" cellpadding="0" cellspacing="0" style="font-size:13px" class="tpnodl-desktop-table">
    <thead><tr style="background:#e9ecef">
      <th style="padding:10px 10px;text-align:left;color:#555;min-width:120px">Type</th>
      <th style="padding:10px 10px;text-align:left;color:#555">Feeder</th>
      <th style="padding:10px 10px;text-align:left;color:#555">Location</th>
      <th style="padding:10px 10px;text-align:left;color:#555">Detail</th>
      <th style="padding:10px 10px;text-align:center;color:#555">Active Since</th>
    </tr></thead>
    <tbody>{alert_rows}</tbody>
  </table>

  <div class="tpnodl-mobile-cards">{alert_cards}</div>
</td></tr>

<!-- Disclaimer -->
<tr><td style="background:#fff8e6;border-top:1px solid #f0d98f;padding:12px 22px">
  <span style="color:#7a5c00;font-size:12px;line-height:1.6">
    ⚠️ <strong>Disclaimer:</strong> This alert is based on meter data from the Energy Audit Meter
    installed at the GSS 33 KV Feeder Panel. If any feeder meter is not communicating, the
    corresponding alert will not be included in this report.
  </span>
</td></tr>

<!-- Footer -->
<tr><td style="background:#0a1628;border-radius:0 0 8px 8px;padding:12px 22px;text-align:center">
  <span style="color:#778;font-size:12px">TPNODL PSCC Realtime Load & Voltage Monitor | Auto-generated report</span>
</td></tr>

</table></body></html>"""

    def _build_wa_message(self) -> str:
        """
        Build concise WhatsApp report message.
        Format optimised for mobile reading — uses emojis, compact layout.
        Max ~4000 chars (WA limit).
        """
        now_str    = datetime.now().strftime("%d-%b-%Y %H:%M")
        all_alerts = self.store.all(active_only=True, limit=300)
        monitored  = [a for a in all_alerts if a.get("type") in REPORT_TYPES
                      and self._is_monitored(a)]
        merged     = self._annotate_field_marks(self._match_diversion(monitored))

        # Circle demand
        live    = self.scraper.last_data
        circles = {}
        for r in live:
            c = r.get("Circle","")
            if c:
                circles.setdefault(c, {"MW":0})
                circles[c]["MW"] += float(r.get("ActivePower") or 0)
        total_mw = sum(v["MW"] for v in circles.values())

        TYPE_ICON = {
            "OV":"⚡","UV":"⬇","OL":"🔴","FEEDER_OFF":"⚫",
            "LOAD_DIVERTED":"🔀","LINE_JUMPER_PARTING":"⛓️","PT_PHASE_MISSING":"⚠️",
        }
        TYPE_SHORT = {
            "OV":"OV","UV":"UV","OL":"OL","FEEDER_OFF":"OFF",
            "LOAD_DIVERTED":"DIV","LINE_JUMPER_PARTING":"LJP","PT_PHASE_MISSING":"PTF",
        }

        # Count by severity
        crit = sum(1 for a in merged if a.get("severity") == "CRITICAL")
        high = sum(1 for a in merged if a.get("severity") == "HIGH")

        # If EVERY remaining alert is a diversion-only event (severity
        # downgraded to INFO during merge), there's no real outage/violation
        # to report — supply is fully continuous via BC. Treat the whole
        # report as "Normal" rather than showing an alert count with a
        # diversion-only breakdown, per the requirement that diverted-OFF
        # feeders should read as informational, not as active violations.
        real_alerts   = [a for a in merged if not a.get("_is_diverted_off")]
        all_diversion = bool(merged) and not real_alerts

        header_emoji = "🔴" if crit > 0 else "🟠" if high > 0 else "🟢"
        lines = [
            f"{header_emoji} *TPNODL PSCC Management Report*",
            f"🗓️ {now_str}",
            f"━━━━━━━━━━━━━━━━━━━━━",
        ]

        # Circle demand summary
        lines.append("📊 *Circle Demand (MW)*")
        for circ, d in sorted(circles.items(), key=lambda x: -x[1]["MW"]):
            lines.append(f"  {circ}: *{d['MW']:.2f} MW*")
        lines.append(f"  🔷 *Total: {total_mw:.2f} MW*")
        lines.append("━━━━━━━━━━━━━━━━━━━━━")

        # Alerts
        if not merged or all_diversion:
            lines.append("✅ *No active violations*")
            if all_diversion:
                # Still surface diversion info, clearly marked as informational —
                # loop through so each one shows its ACTUAL source (BC asset for
                # automatic detection, or the field-marked GSS/Feeder), instead
                # of a generic summary line that always said "via BC" even for
                # field-marked diversions.
                for a in merged:
                    feeder = a.get("Feeder") or a.get("AssetCode","")
                    if a.get("_is_field_marked"):
                        via_name = a.get("_diversion_via_name") or a["_diversion_via"]
                        via_gss  = a.get("_diversion_via_gss","")
                        lines.append(f"ℹ️ {feeder} — diverted via field device "
                                      f"({via_name} @ {via_gss}, Field Marked)")
                    else:
                        bc_load = a.get("_bc_load_a","?")
                        bc_load_str = f"{bc_load:.0f}A" if isinstance(bc_load,(int,float)) else str(bc_load)
                        lines.append(f"ℹ️ {feeder} — diverted via BC "
                                      f"{a.get('_diversion_via','')} ({bc_load_str})")
        else:
            lines.append(f"🚨 *Active Alerts: {len(real_alerts)}*")
            if crit: lines.append(f"  ‼ Critical: {crit}  🔺 High: {high}")
            lines.append("")

            for a in merged:
                t     = a.get("type","")
                icon  = TYPE_ICON.get(t,"•")
                short = TYPE_SHORT.get(t, t)
                gss   = a.get("Gss","")
                dur   = self._fmt_dur(a.get("first_seen") or a.get("timestamp",""))
                val   = a.get("value")

                # Format value with correct unit
                if t in ("OV","UV"):
                    val_str = f" ({val:.3f} kV)" if val else ""
                    # UV/OV: show only GSS name (no duplicate "GSS: GSS_NAME | GSS_NAME")
                    line = f"{icon} *{short}* | {gss} | ⏱{dur}{val_str}"
                elif t == "OL":
                    lvl = a.get("ol_level",1)
                    lv_tag = {1:" L1",2:" L2",3:" L3"}.get(lvl,"")
                    val_str = f" ({val:.1f}%)" if val else ""
                    feeder = a.get("Feeder") or a.get("AssetCode","")
                    line = f"{icon} *{short}{lv_tag}* | {feeder} | {gss} | ⏱{dur}{val_str}"
                else:
                    feeder = a.get("Feeder") or a.get("AssetCode","")
                    val_str = f" ({val:.1f}A)" if val else ""
                    line = f"{icon} *{short}* | {feeder} | {gss} | ⏱{dur}{val_str}"

                if a.get("_diversion_via"):
                    if a.get("_is_field_marked"):
                        via_name = a.get("_diversion_via_name") or a["_diversion_via"]
                        via_gss  = a.get("_diversion_via_gss","")
                        line += f"\n   🔀 via field device — {via_name} @ {via_gss} (Field Marked)"
                    else:
                        bc_load = a.get("_bc_load_a","?")
                        bc_load_str = f"{bc_load:.0f}A" if isinstance(bc_load, (int,float)) else str(bc_load)
                        line += f"\n   🔀 via BC {a['_diversion_via']} ({bc_load_str})"
                lines.append(line)

        lines.append("━━━━━━━━━━━━━━━━━━━━━")
        lines.append("_TPNODL PSCC Auto Report_")

        msg = "\n".join(lines)
        # Truncate if too long (WA 4096 limit)
        if len(msg) > 3900:
            msg = msg[:3850] + "\n…(truncated)\n_TPNODL PSCC Auto Report_"
        return msg

    def _build_plain(self) -> str:
        now_str = datetime.now().strftime("%d-%b-%Y %H:%M")
        all_alerts = self.store.all(active_only=True, limit=300)
        monitored  = [a for a in all_alerts if a.get("type") in REPORT_TYPES
                      and self._is_monitored(a)]
        merged     = self._annotate_field_marks(self._match_diversion(monitored))
        lines = [f"TPNODL PSCC Management Report — {now_str}",
                 f"Active Alerts: {len(merged)}", ""]
        for a in merged:
            t     = a.get("type","")
            dur   = self._fmt_dur(a.get("first_seen") or a.get("timestamp",""))
            lines.append(f"[{t}] {a.get('Feeder',a.get('AssetCode',''))} | {a.get('Gss','')} | Active: {dur}")
            if a.get("_diversion_via"):
                if a.get("_is_field_marked"):
                    via_name = a.get("_diversion_via_name") or a["_diversion_via"]
                    via_gss  = a.get("_diversion_via_gss","")
                    lines.append(f"  → Load diverted via field device — {via_name} @ {via_gss} (Field Marked)")
                else:
                    lines.append(f"  → Load diverted via BC {a['_diversion_via']}")
        return "\n".join(lines)

    def _fmt_dur(self, ts: str) -> str:
        try:
            sec = int((datetime.now() - datetime.fromisoformat(ts)).total_seconds())
            h, m = divmod(sec // 60, 60)
            return f"{h}h {m}m" if h else f"{m}m"
        except Exception:
            return "—"

    def _smtp_send(self, to: list, html: str, plain: str) -> bool:
        from_addr = self.cfg.get("mgmt_report.from_addr","") or self.cfg.get("email.from_addr","")
        password  = self.cfg.get("mgmt_report.password","")  or self.cfg.get("email.password","")
        host      = self.cfg.get("mgmt_report.smtp_host","") or self.cfg.get("email.smtp_host","smtp.office365.com")
        port      = int(self.cfg.get("mgmt_report.smtp_port", self.cfg.get("email.smtp_port", 587)))
        tls_cfg   = self.cfg.get("mgmt_report.tls","")       or self.cfg.get("email.tls","STARTTLS")

        if not from_addr:
            log.error("Management report: sender not configured")
            return False

        now_str = datetime.now().strftime("%d-%b-%Y %H:%M")
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"⚡ TPNODL PSCC | Realtime Grid Health & Operational Alert Report | {now_str}"
        msg["From"]    = f"TPNODL PSCC <{from_addr}>"
        msg["To"]      = ", ".join(to)
        msg.attach(MIMEText(plain, "plain"))
        msg.attach(MIMEText(html,  "html"))

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        no_auth = (port == 25 and not password)
        TIMEOUT = 20

        try:
            if port == 465 or tls_cfg == "SSL":
                with smtplib.SMTP_SSL(host, port, context=ctx, timeout=TIMEOUT) as s:
                    if not no_auth: s.login(from_addr, password)
                    s.sendmail(from_addr, to, msg.as_string())
            else:
                with smtplib.SMTP(host, port, timeout=TIMEOUT) as s:
                    s.ehlo()
                    if not no_auth:
                        try: s.starttls(context=ctx); s.ehlo()
                        except Exception: pass
                        s.login(from_addr, password)
                    s.sendmail(from_addr, to, msg.as_string())
            log.info(f"Management report emailed to {to}")
            return True
        except Exception as e:
            log.error(f"Management report email error: {e}")
            return False
