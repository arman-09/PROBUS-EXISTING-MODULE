"""
modules/violation.py — Violation Detection Engine (Clean v3)
=============================================================
OV / UV / OL / FEEDER_OFF / SUDDEN_LOAD_DROP / SUDDEN_LOAD_RAISE
LOAD_DIVERTED / LOAD_RESTORED (Bus Coupler diversion)

Stateful (OV/UV/OL): suppressed after first alert until condition clears.
Edge (OFF/SLD/SLR/DIV): fires on every occurrence.
SLD/SLR: compares ONLY last 2 readings to avoid noise.
"""
import logging, time, json, os, statistics
from datetime import datetime
from collections import defaultdict

log = logging.getLogger("violation")

def _fmt_dur_sec(seconds: float) -> str:
    """Format seconds into 'Xh Ym' string."""
    if not seconds or seconds < 0:
        return ""
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m = rem // 60
    if h:
        return f"{h}h {m}m"
    return f"{m}m" if m else "<1m"


VT_OV  = "OV";  VT_UV  = "UV";  VT_OL  = "OL"
VT_OFF = "FEEDER_OFF"
VT_SLD = "SUDDEN_LOAD_DROP";  VT_SLR = "SUDDEN_LOAD_RAISE"
VT_DIV = "LOAD_DIVERTED";     VT_RST = "LOAD_RESTORED"
VT_PTF = "PT_PHASE_MISSING"    # Voltage unbalance > 25% (PT fuse/phase fault)
VT_LJP = "LINE_JUMPER_PARTING" # Current unbalance > 80% + load > 50% rating
VT_BCL = "BC_LOADING_START"    # BC current crossed idle→active (0A→loaded)
                                # Recorded as a normal stateless event, same
                                # pattern as SUDDEN_LOAD_RAISE. Its first_seen
                                # is the authoritative "BC load raise time"
                                # used as LOAD_DIVERTED's true start timestamp
                                # (instead of the moment detection software
                                # happened to confirm it, which can be hours
                                # or days later for an already-OFF feeder).
VT_BCT = "BC_TRIP_WHILE_DIVERTED"     # BC itself trips while actively
                                       # carrying a CONFIRMED diversion —
                                       # the diverted feeder's supply is now
                                       # genuinely cut (unlike a normal BC
                                       # FEEDER_OFF, which is meaningless
                                       # noise since BC's idle state is 0A).
                                       # Stays active through acknowledge —
                                       # this is a real outage condition.
VT_BCR = "BC_DIVERTED_NORMALIZED"     # The diverted feeder's supply is back
                                       # (BC restored AND/OR the originally-
                                       # diverted feeder's own load is back).
                                       # Auto-acknowledged like LOAD_RESTORED.

SEVERITY = {
    VT_OV:"HIGH",   VT_UV:"MEDIUM",  VT_OL:"HIGH",
    VT_OFF:"CRITICAL",
    VT_SLD:"MEDIUM",VT_SLR:"MEDIUM",
    VT_DIV:"HIGH",  VT_RST:"INFO",
    VT_PTF:"CRITICAL",  # PT phase missing — instrumentation fault
    VT_LJP:"CRITICAL",  # Line jumper parting — serious conductor fault
    VT_BCL:"INFO",       # informational — BC started carrying load
    VT_BCT:"CRITICAL",   # BC tripped while carrying a confirmed diversion —
                          # real outage for the originally-diverted feeder
    VT_BCR:"INFO",       # restoration — informational, like LOAD_RESTORED
}
STATEFUL = {VT_OV, VT_UV, VT_OL, VT_PTF, VT_LJP, VT_BCT}


class ViolationDetector:
    def __init__(self, cfg, feeder_master, alert_store, email_mgr, wa_mgr,
                 peak_store=None):
        self.cfg        = cfg
        self.fm         = feeder_master
        self.store      = alert_store
        self.email      = email_mgr
        self.wa         = wa_mgr
        self.peak_store = peak_store  # provides feeder_hourly for p75 lookups
        self._history:    dict = defaultdict(list)
        self._prev:       dict = {}
        self._ljp_cycles: dict   = {}  # asset -> consecutive LJP data cycles
        self._solar_pending: dict = {} # asset -> {first_low_ts, first_low_iso} —
                                        # tracks solar feeders currently below
                                        # solar_off_threshold_a, waiting out
                                        # solar_off_wait_minutes before OFF fires
        self._ol_notif_ts: dict  = {}  # asset_key+level -> last notif time (cooldown)
        self._pending_notify: list = [] # RST events queued by stale-clear in app.py
        self._bc = BusCouplerDiversionDetector()
        self._load_history()
        # Seed BC detector with active LOAD_DIVERTED from DB
        # prevents re-firing persistent detection after restart
        for a in alert_store.all(active_only=True, limit=200):
            if a.get("type") == VT_DIV:
                fac = a.get("AssetCode","")
                if fac and fac not in self._bc._active:
                    self._bc._active[fac] = {
                        "bc":            a.get("BusCouplerAsset",""),
                        "f_before":      float(a.get("FeederImaxBefore") or 0),
                        "bc_before":     float(a.get("BCImaxBefore") or 0),
                        "bc_after":      float(a.get("BCImaxAfter") or 0),
                        "ts":            0,
                        "first_rise_ts": a.get("first_seen") or a.get("timestamp",""),
                    }
        # _store_ref: used for is_active() checks and pre-fault load lookup
        # (queries the `alerts` table directly via raw SQL — AlertStore has
        # the connection, that part is correct)
        self._bc._store_ref = alert_store
        # _peak_store_ref: separate object — PeakLoadStore is the ONLY class
        # with get_feeder_hourly_profile() (used for p75 calculation). Passing
        # alert_store here would silently fail every call via the bare
        # except-Exception in _get_feeder_p75(), always returning 0.0 — which
        # is exactly why long-standing diversions (BARBIL-I, TELKOI) were
        # never detected: p75 always came back 0, and pre_fault_imax was also
        # never populated (extra_json was None), so ref_load was always 0
        # and the candidate gate rejected every cycle.
        self._bc._peak_store_ref = peak_store
        if self._bc._active:
            log.info(f"BC detector seeded: {len(self._bc._active)} active diversions from DB")

    HIST_FILE = "data/load_history.json"

    def _load_history(self):
        if os.path.exists(self.HIST_FILE):
            try:
                raw = json.load(open(self.HIST_FILE))
                for k, v in raw.items():
                    self._history[k] = list(v)[-8:]
                log.info(f"Load history loaded for {len(self._history)} feeders")
            except Exception as e:
                log.warning(f"History load: {e}")

    def _save_history(self):
        try:
            json.dump({k: v for k, v in self._history.items()},
                      open(self.HIST_FILE, "w"))
        except Exception as e:
            log.warning(f"History save: {e}")

    def run(self, live_data: list) -> list:
        vn       = self.cfg.get("voltage.vn_kv", 33.0)
        ov_lim   = vn * (1 + self.cfg.get("voltage.ov_pct", 6.0) / 100)
        uv_lim   = vn * (1 - self.cfg.get("voltage.uv_pct", 9.0) / 100)
        ol_thr   = self.cfg.get("voltage.load_threshold_pct", 100.0) / 100
        off_thr  = self.cfg.get("voltage.feeder_off_threshold_a", 1.0)
        solar_off_thr  = self.cfg.get("voltage.solar_off_threshold_a", 0.15)
        solar_wait_min = self.cfg.get("voltage.solar_off_wait_minutes", 10)
        drop_pct = self.cfg.get("voltage.sudden_drop_pct", 20.0) / 100
        rise_pct = self.cfg.get("voltage.sudden_raise_pct", 20.0) / 100
        now = time.time()
        raw_viols = []

        # Pre-cycle: cache which FEEDER_OFF alerts are active+notified BEFORE any clearing
        # Must be done before the feeder loop which calls clear_condition(asset, VT_OFF)
        off_notified_cache = {}  # asset_code → bool (was notified?)
        for active_key in list(self.store._active.keys()):
            if active_key.endswith(f"_{VT_OFF}"):
                asset_ac = active_key[:-len(f"_{VT_OFF}")]
                off_notified_cache[asset_ac] = self.store.was_notified(active_key)

        # ── Group feeders by GSS for voltage aggregation ──
        # GSS-level OV/UV: raise alert at GSS level when majority of feeders violate
        gss_voltage: dict = {}   # gss -> {ov_count, uv_count, ok_count, samples}

        for row in live_data:
            asset = (row.get("AssetCode") or "").strip()
            if not asset:
                continue
            vr   = float(row.get("Vr") or 0)
            vy   = float(row.get("Vy") or 0)
            vb   = float(row.get("Vb") or 0)
            ir   = float(row.get("Ir") or 0)
            iy   = float(row.get("Iy") or 0)
            ib   = float(row.get("Ib") or 0)
            vavg = (vr + vy + vb) / 3
            imax = max(ir, iy, ib)
            row["_imax"] = imax
            is_bc = row.get("IsBusCoupler", False)
            master  = self.fm.lookup(asset)
            prev    = self._prev.get(asset, {})
            prev_imax = prev.get("imax")
            gss     = row.get("Gss","")

            # ── Voltage unbalance calculation ─────────────
            v_unbal_pct = 0.0
            if vavg > 1.0:
                v_unbal_pct = (max(abs(vr-vavg), abs(vy-vavg), abs(vb-vavg)) / vavg) * 100

            # ── Voltage: collect per-GSS ──────────────────
            # Only use healthy readings (unbalance < 10%) for UV/OV classification
            if vavg > 1.0 and gss:
                if gss not in gss_voltage:
                    gss_voltage[gss] = {
                        "ov_feeders":[], "uv_feeders":[], "ok_feeders":[],
                        "Circle": row.get("Circle",""),
                        "Division": row.get("Division",""),
                        "Gss": gss,
                        "Vr": vr, "Vy": vy, "Vb": vb, "Vavg": vavg,
                    }
                gv = gss_voltage[gss]
                if vavg > gv["Vavg"]:
                    gv.update({"Vr":vr,"Vy":vy,"Vb":vb,"Vavg":vavg})
                feeder_info = {
                    "AssetCode": asset,
                    "Feeder":    row.get("Feeder",""),
                    "FeederType":row.get("FeederType",""),
                    "Vavg":      round(vavg,4),
                    "Vr":round(vr,4),"Vy":round(vy,4),"Vb":round(vb,4),
                    "Ir":round(ir,4),"Iy":round(iy,4),"Ib":round(ib,4),
                    "IsBusCoupler": is_bc,
                }
                if vavg > ov_lim:
                    gv["ov_feeders"].append(feeder_info)
                elif vavg < uv_lim and v_unbal_pct < 10.0:
                    # Only classify as UV if voltage is balanced (< 10% unbalance)
                    # High unbalance + low Vavg = PT phase missing, not true UV
                    gv["uv_feeders"].append(feeder_info)
                else:
                    gv["ok_feeders"].append(feeder_info)

            # ── Bus Coupler: SKIP OL / SLD / SLR ────────────
            # BC is normally OFF. Only allow:
            #   1. LOAD_DIVERTED (handled by BC detector below)
            #   2. FEEDER_OFF — but ONLY if BC was carrying >400% of its rating
            #      (genuine fault, not normal switching off)
            if is_bc:
                if prev_imax is not None and prev_imax > off_thr and imax <= off_thr:
                    bc_rating = float((master or {}).get("FeederRating") or 0)
                    bc_pct    = (prev_imax / bc_rating * 100) if bc_rating > 0 else 0
                    if bc_pct > 400:
                        v = self._make(VT_OFF, row, master,
                            value=round(imax, 4), limit=off_thr,
                            detail=(f"Bus Coupler tripped carrying {prev_imax:.2f}A "
                                    f"({bc_pct:.0f}% of {bc_rating}A rating) — fault trip."))
                        if self.store.add(v, self.cfg):
                            raw_viols.append(v)
                self._prev[asset] = {"imax": imax, "vavg": vavg}
                self._history[asset].append((now, imax))
                if len(self._history[asset]) > 8:
                    self._history[asset] = self._history[asset][-8:]
                continue  # skip OL / SLD / SLR for bus couplers

            # ── Overload — 3 levels (feeders only) ────────
            # L1: ≥100% (base alert, LOW priority notification)
            # L2: ≥110% (escalation, re-notifies)
            # L3: ≥120% (critical escalation, re-notifies)
            # Single alert card per feeder — escalated in-place
            if master:
                rating = float(master.get("FeederRating") or 0)
                if rating > 0:
                    load_pct = imax / rating * 100
                    ol_l3    = self.cfg.get("voltage.ol_l3_pct", 120.0)
                    ol_l2    = self.cfg.get("voltage.ol_l2_pct", 110.0)
                    ol_l1    = ol_thr * 100  # default 100%

                    if load_pct >= ol_l1:
                        # Determine current level
                        level = 3 if load_pct >= ol_l3 else 2 if load_pct >= ol_l2 else 1
                        lv_label = {1:"L1 (100%)", 2:"L2 (110%)", 3:"L3 (120%)"}[level]
                        detail = (f"Imax={imax:.2f}A / {rating}A = {load_pct:.1f}% "
                                  f"[{lv_label} limit] Rating: {rating}A")

                        ak = f"{asset}_{VT_OL}"
                        cur_level = self.store.get_ol_level(ak)

                        if cur_level == 0:
                            # No active OL — check 15-min cooldown for L2/L3
                            COOLDOWN_S  = 15 * 60
                            ts_key      = f"{ak}_{level}"
                            last_ts     = self._ol_notif_ts.get(ts_key, 0)
                            in_cooldown = (level >= 2 and
                                           (time.time() - last_ts) < COOLDOWN_S)
                            if in_cooldown:
                                rem = int((COOLDOWN_S - (time.time() - last_ts)) / 60)
                                log.debug(f"OL L{level} suppressed for {asset}: "
                                          f"{rem}min cooldown remaining")
                            v = self._make(VT_OL, row, master,
                                value=round(load_pct, 2),
                                limit=round(ol_l1, 2),
                                detail=detail)
                            v["ol_level"]      = level
                            v["_ol_suppressed"] = in_cooldown
                            # For L1: notify only if L2/L3 not already hit
                            # (If feeder jumps straight to L2/L3, skip L1 notify)
                            if self.store.add(v, self.cfg):
                                self.store.update_ol_level(ak, level, round(load_pct,2), detail)
                                if not in_cooldown:
                                    self._ol_notif_ts[ts_key] = time.time()
                                    raw_viols.append(v)
                        elif level > cur_level:
                            # Escalation — update card in-place, re-notify
                            if self.store.update_ol_level(ak, level, round(load_pct,2), detail):
                                v = self._make(VT_OL, row, master,
                                    value=round(load_pct, 2),
                                    limit=round(ol_l1, 2),
                                    detail=detail)
                                v["ol_level"]   = level
                                v["_escalated"] = True
                                raw_viols.append(v)
                                # Record escalation time as new cooldown baseline
                                self._ol_notif_ts[f"{ak}_{level}"] = time.time()
                        else:
                            # Same or lower level — just update last_seen (handled by add)
                            self.store.add(v := self._make(VT_OL, row, master,
                                value=round(load_pct,2), limit=round(ol_l1,2),
                                detail=detail), self.cfg)
                    else:
                        self.store.clear_condition(asset, VT_OL)

            # ── Feeder OFF / Restore ──────────────────────
            # OFF:     Imax <= off_thr (≤1.00A) — feeder tripped/de-energised
            # Restore: Imax >  off_thr (>1.00A) — strict, raw value, no rounding
            ak_off = f"{asset}_{VT_OFF}"
            is_solar = bool((master or {}).get("IsSolarPlant"))

            if is_solar:
                # ── SOLAR PLANT: wait-confirmation logic ──────────────────
                # 0A/low-current is a NORMAL state for solar (night, cloud
                # cover, rain, maintenance) — cannot be distinguished from a
                # real fault using current alone with a fixed time window
                # (a cloudy day can legitimately show ~0A at noon). Instead:
                #   1. Use a TIGHTER threshold (solar_off_thr, default 0.15A)
                #      that filters out the feeder's own daytime self-
                #      consumption/leakage current (typically 0.2-1A even
                #      when generation is genuinely zero from a real fault
                #      would still show SOME residual draw above 0.15A in
                #      most installations — tunable per-site via config)
                #   2. Require the LOWER reading to persist continuously for
                #      solar_off_wait_minutes before firing OFF — filters out
                #      transient dips (a brief cloud passing over)
                #   3. OFF alert's timestamp is BACKDATED to the very first
                #      cycle the low reading was observed, not when the wait
                #      period completed — so "Active Since" reflects reality
                if imax <= solar_off_thr:
                    if asset not in self._solar_pending:
                        self._solar_pending[asset] = {
                            "first_low_ts":  now,
                            "first_low_iso": datetime.now().isoformat(),
                            "first_low_imax": prev_imax if prev_imax is not None else imax,
                        }
                        log.debug(f"Solar {asset}: low current ({imax:.3f}A) — "
                                  f"starting {solar_wait_min}min wait before OFF")
                    else:
                        elapsed_min = (now - self._solar_pending[asset]["first_low_ts"]) / 60
                        if (elapsed_min >= solar_wait_min
                                and not self.store.is_active(ak_off)):
                            p = self._solar_pending[asset]
                            v = self._make(VT_OFF, row, master,
                                value=round(imax, 4), limit=solar_off_thr,
                                detail=(f"Solar plant generation stopped "
                                        f"(≤{solar_off_thr}A for {solar_wait_min}+ min). "
                                        f"Confirmed at {p['first_low_imax']:.2f}A→"
                                        f"{imax:.4f}A."))
                            # Backdate to first low reading, not confirmation time
                            v["timestamp"]  = p["first_low_iso"]
                            v["first_seen"] = p["first_low_iso"]
                            v["pre_fault_imax"] = round(p["first_low_imax"], 2)
                            if self.store.add(v, self.cfg):
                                raw_viols.append(v)
                                log.info(f"Solar OFF confirmed: {asset} "
                                         f"(low since {p['first_low_iso'][:19]}, "
                                         f"{elapsed_min:.1f}min wait)")
                else:
                    # Current back above solar threshold — cancel any pending
                    # wait, and restore if an OFF was actually confirmed
                    self._solar_pending.pop(asset, None)
                    if self.store.is_active(ak_off):
                        off_was_notified = off_notified_cache.get(asset, False)
                        off_duration = self.store.get_active_duration(ak_off)
                        feeder_name  = master.get('FeederName', asset) if master else asset
                        dur_str      = _fmt_dur_sec(off_duration) if off_duration else ""
                        rst_v = self._make(VT_RST, row, master,
                            value=round(imax, 2), limit=solar_off_thr,
                            detail=(f"Solar plant {feeder_name} generating again "
                                    f"({imax:.1f}A)."
                                    + (f" Stopped duration: {dur_str}." if dur_str else "")))
                        rst_v["_was_off_notified"] = off_was_notified
                        rst_v["_off_duration_str"] = dur_str
                        if self.store.add(rst_v, self.cfg):
                            raw_viols.append(rst_v)
                        self.store.clear_condition(asset, VT_OFF)

                self._history[asset].append((now, imax))
                if len(self._history[asset]) > 8:
                    self._history[asset] = self._history[asset][-8:]
                self._prev[asset] = {"imax": imax, "vavg": vavg}
                # Skip ONLY the standard FEEDER_OFF/Restore block (replaced
                # above by the solar wait-confirmation logic) and Sudden Load
                # Change (which would fire constantly at every sunrise/sunset
                # ramp — not a real anomaly for solar). Line Jumper Parting
                # and Overload are genuine conductor/equipment fault checks
                # that apply to solar exactly like any other feeder — an
                # inverter fault or phase fault doesn't care whether the
                # source is solar or grid, so those must still run below.
                goto_ljp = True
            else:
                goto_ljp = False

            if not goto_ljp and prev_imax is not None and prev_imax > off_thr and imax <= off_thr:
                # ── New FEEDER_OFF ────────────────────────────────────────────
                # If a previous OFF is still active (un-cleared), it means the
                # feeder went OFF → ON → OFF faster than our fetch interval caught.
                # Generate a synthetic restoration for the previous OFF first.
                if self.store.is_active(ak_off):
                    prev_off_notified = off_notified_cache.get(asset, False)
                    prev_duration     = self.store.get_active_duration(ak_off)
                    dur_str_prev      = _fmt_dur_sec(prev_duration) if prev_duration else ""
                    feeder_name       = master.get('FeederName', asset) if master else asset
                    synth_rst = self._make(VT_RST, row, master,
                        value=round(prev_imax, 2), limit=off_thr,
                        detail=(f"Feeder {feeder_name} briefly restored before re-trip. "
                                + (f"Prior outage: {dur_str_prev}." if dur_str_prev else "")))
                    synth_rst["_was_off_notified"] = prev_off_notified
                    synth_rst["_off_duration_str"] = dur_str_prev
                    synth_rst["_synthetic"]        = True
                    if self.store.add(synth_rst, self.cfg):
                        raw_viols.append(synth_rst)
                    self.store.clear_condition(asset, VT_OFF)
                    log.info(f"Synthetic RST generated for {asset} before re-trip")

                # Compute pre-fault average from history
                hist_vals = self._history.get(asset, [])
                pre_iavg  = round(
                    sum(h[1] for h in hist_vals[-4:]) / len(hist_vals[-4:]), 2
                ) if len(hist_vals) >= 2 else round(prev_imax, 2)

                v = self._make(VT_OFF, row, master,
                    value=round(imax, 4), limit=off_thr,
                    detail=f"Current {prev_imax:.2f}A→{imax:.4f}A (≤{off_thr}A). Feeder tripped.")

                # Store pre-fault load in extra_json for BC diversion comparison
                v["pre_fault_imax"] = round(prev_imax, 2)
                v["pre_fault_iavg"] = pre_iavg
                v["pre_fault_ir"]   = round(float(row.get("Ir") or 0), 2)
                v["pre_fault_iy"]   = round(float(row.get("Iy") or 0), 2)
                v["pre_fault_ib"]   = round(float(row.get("Ib") or 0), 2)

                if self.store.add(v, self.cfg):
                    raw_viols.append(v)

            elif not goto_ljp and imax > off_thr:
                # ── Feeder restored ───────────────────────────────────────────
                # goto_ljp guard: solar feeders already handled their own
                # OFF/Restore above (with wait-confirmation) — this standard
                # path must not also fire for them, or LOAD_RESTORED would be
                # double-generated.
                off_was_active   = self.store.is_active(ak_off)
                # Use pre-cycle cache: was the OFF notification actually sent?
                off_was_notified = off_notified_cache.get(asset, False)

                # Generate LOAD_RESTORED if:
                # 1. Feeder was actively OFF this cycle
                # 2. Not a BC-tracked diversion (BC detector handles those)
                # NOTE: Fire restoration regardless of whether OFF was notified.
                #       Short trips (under delay window) still need restoration record.
                if off_was_active and asset not in getattr(self._bc, '_active', {}):
                    off_duration = self.store.get_active_duration(ak_off)
                    feeder_name  = master.get('FeederName', asset) if master else asset
                    dur_str      = _fmt_dur_sec(off_duration) if off_duration else ""
                    rst_v = self._make(VT_RST, row, master,
                        value=round(imax, 2), limit=off_thr,
                        detail=(f"Feeder {feeder_name} normalized ({imax:.1f}A). "
                                f"Feeder OFF/Trip ended."
                                + (f" Outage duration: {dur_str}." if dur_str else "")))
                    rst_v["_was_off_notified"] = off_was_notified
                    rst_v["_off_duration_str"] = dur_str
                    if self.store.add(rst_v, self.cfg):
                        raw_viols.append(rst_v)
                    if not off_was_notified:
                        log.info(f"RST for {asset} — OFF was within delay window, "
                                 f"restoration will notify only if configured")

                self.store.clear_condition(asset, VT_OFF)

            # ── PT Phase Missing (feeder meters only) ─────
            # Voltage unbalance > 25% = likely PT fuse blown or phase fault
            # Only check if feeder has voltage data and is not a BC
            if not is_bc and vavg > 1.0 and imax > off_thr:
                if v_unbal_pct > 25.0:
                    v = self._make(VT_PTF, row, master,
                        value=round(v_unbal_pct, 2), limit=25.0,
                        detail=(f"Voltage unbalance {v_unbal_pct:.1f}% > 25% limit. "
                                f"Vr/Vy/Vb: {vr:.3f}/{vy:.3f}/{vb:.3f} kV "
                                f"(Vavg={vavg:.3f} kV). "
                                f"Possible PT fuse blown or phase fault."))
                    if self.store.add(v, self.cfg):
                        raw_viols.append(v)
                else:
                    self.store.clear_condition(asset, VT_PTF)

            # ── Line Jumper Parting (feeders only) ────────
            # Current unbalance > 80% + feeder loaded > 50% rating = conductor fault
            # Reset: always clear if condition gone, even if load drops below 50%
            if not is_bc and imax > off_thr and master:
                rating = float(master.get("FeederRating") or 0)
                ak_ljp = f"{asset}_{VT_LJP}"
                ljp_active = ak_ljp in self.store._active

                # ── Evaluate LJP condition ──────────────────────────────────
                ljp_condition = False
                i_unbal_pct   = 0.0
                if rating > 0 and imax / rating > 0.50:  # > 50% of rating
                    iavg = (ir + iy + ib) / 3
                    if iavg > 0:
                        i_unbal_pct = (max(abs(ir-iavg), abs(iy-iavg), abs(ib-iavg)) / iavg) * 100
                        if i_unbal_pct > 80.0:
                            ljp_condition = True

                if ljp_condition:
                    # LJP active — fire/maintain alert
                    v = self._make(VT_LJP, row, master,
                        value=round(i_unbal_pct, 2), limit=80.0,
                        detail=(f"Current unbalance {i_unbal_pct:.1f}% > 80% limit. "
                                f"Ir/Iy/Ib: {ir:.2f}/{iy:.2f}/{ib:.2f} A "
                                f"(Load={imax/rating*100:.1f}% of {rating:.0f}A rating). "
                                f"Possible line jumper parting."))
                    if self.store.add(v, self.cfg):
                        raw_viols.append(v)
                    # Count consecutive active cycles
                    self._ljp_cycles[asset] = self._ljp_cycles.get(asset, 0) + 1

                else:
                    # LJP condition gone (unbalance OK, or load dropped below 50%)
                    # ALWAYS clear — regardless of why condition cleared
                    if ljp_active:
                        cycles = self._ljp_cycles.get(asset, 0)
                        if cycles <= 1:
                            label = "LJP False Detection (Transient Data)"
                        else:
                            label = "LJP Normalized"
                        ljp_norm = self._make(VT_LJP, row, master,
                            value=round(i_unbal_pct, 2), limit=80.0,
                            detail=(f"{label}: Current balance restored. "
                                    f"Ir/Iy/Ib: {ir:.2f}/{iy:.2f}/{ib:.2f} A "
                                    f"(Imax={imax:.1f}A). "
                                    f"Was active for {cycles} data cycle(s)."))
                        ljp_norm["_ljp_norm"]  = True
                        ljp_norm["_ljp_false"] = (cycles <= 1)
                        raw_viols.append(ljp_norm)
                        self._ljp_cycles.pop(asset, None)
                    self.store.clear_condition(asset, VT_LJP)

            # ── Sudden Load Change (last 2 readings only) ─
            hist = self._history.get(asset, [])
            if (prev_imax is not None and imax > off_thr and
                    prev_imax > off_thr and prev_imax > 0):
                delta = (imax - prev_imax) / prev_imax
                # Normal band from recent window
                if len(hist) >= 3:
                    vals = [h[1] for h in hist[-6:]]
                    std  = statistics.stdev(vals) if len(vals) > 1 else 0
                    band = max(std * 2, prev_imax * 0.05)
                else:
                    band = prev_imax * 0.10
                abs_d = abs(imax - prev_imax)
                if delta < -drop_pct and abs_d > band:
                    v = self._make(VT_SLD, row, master,
                        value=round(imax,2), limit=round(prev_imax,2),
                        detail=f"Load dropped {abs(delta)*100:.1f}%: {prev_imax:.1f}A→{imax:.1f}A (Δ={imax-prev_imax:.1f}A)")
                    if self.store.add(v, self.cfg):
                        raw_viols.append(v)
                elif delta > rise_pct and abs_d > band:
                    v = self._make(VT_SLR, row, master,
                        value=round(imax,2), limit=round(prev_imax,2),
                        detail=f"Load raised {delta*100:.1f}%: {prev_imax:.1f}A→{imax:.1f}A (Δ=+{imax-prev_imax:.1f}A)")
                    if self.store.add(v, self.cfg):
                        raw_viols.append(v)

            # Update history & prev
            self._history[asset].append((now, imax))
            if len(self._history[asset]) > 8:
                self._history[asset] = self._history[asset][-8:]
            self._prev[asset] = {"imax": imax, "vavg": vavg}

        # ── GSS-level OV/UV processing ────────────────────
        # One alert per GSS (not per feeder). Lists all affected feeders inside.
        for gss, gv in gss_voltage.items():
            gss_key  = f"GSS_{gss.replace(' ','_')}"
            # Separate BC feeders from non-BC for thresholding
            ov_fdrs_all = gv["ov_feeders"]
            uv_fdrs_all = gv["uv_feeders"]
            ok_fdrs     = gv["ok_feeders"]

            # For UV/OV decision: only count non-BC feeders
            ov_fdrs_nonbc = [f for f in ov_fdrs_all if not f.get("IsBusCoupler")]
            uv_fdrs_nonbc = [f for f in uv_fdrs_all if not f.get("IsBusCoupler")]
            total_nonbc   = len(ov_fdrs_nonbc) + len(uv_fdrs_nonbc) + \
                            sum(1 for f in ok_fdrs if not f.get("IsBusCoupler"))

            if ov_fdrs_nonbc:
                worst = max(ov_fdrs_nonbc, key=lambda f: f["Vavg"])
                feeder_list = ", ".join(
                    f"{f['Feeder']} ({f['Vavg']:.3f}kV)" for f in ov_fdrs_nonbc)
                detail_str = (f"{len(ov_fdrs_nonbc)}/{total_nonbc} feeders above {ov_lim:.3f}kV"
                              + (f". Affected: {feeder_list}" if feeder_list else ""))
                v = self._make_gss(VT_OV, gv, gss_key,
                    value=round(worst["Vavg"],4), limit=round(ov_lim,4),
                    detail=detail_str,
                    affected_feeders=ov_fdrs_nonbc)
                if self.store.add(v, self.cfg):
                    raw_viols.append(v)
                else:
                    self.store.update_gss_detail(gss_key, VT_OV,
                                                 detail_str, ov_fdrs_nonbc,
                                                 round(worst["Vavg"],4))
                self.store.clear_condition(gss_key, VT_UV)

            elif uv_fdrs_nonbc:
                # UV threshold depends on how many active non-BC feeders the GSS has:
                # - GSS with only 1 active feeder → 1 UV feeder is enough (no cross-check possible)
                # - GSS with 2+ active feeders → require ≥2 UV feeders (avoids PT fault false alarm)
                active_nonbc = len(uv_fdrs_nonbc) + \
                               sum(1 for f in ok_fdrs if not f.get("IsBusCoupler"))
                single_feeder_gss = (total_nonbc == 1)
                uv_threshold_met  = (single_feeder_gss and len(uv_fdrs_nonbc) >= 1) or \
                                    (not single_feeder_gss and len(uv_fdrs_nonbc) >= 2 and active_nonbc >= 2)

                if uv_threshold_met:
                    worst = min(uv_fdrs_nonbc, key=lambda f: f["Vavg"])
                    feeder_list = ", ".join(
                        f"{f['Feeder']} ({f['Vavg']:.3f}kV)" for f in uv_fdrs_nonbc)
                    detail_str = (f"{len(uv_fdrs_nonbc)}/{total_nonbc} feeder{'s' if total_nonbc>1 else ''} "
                                  f"below {uv_lim:.3f}kV"
                                  + (f". Affected: {feeder_list}" if feeder_list else ""))
                    v = self._make_gss(VT_UV, gv, gss_key,
                        value=round(worst["Vavg"],4), limit=round(uv_lim,4),
                        detail=detail_str,
                        affected_feeders=uv_fdrs_nonbc)
                    if self.store.add(v, self.cfg):
                        raw_viols.append(v)
                    else:
                        # Alert already active — update detail + affected_feeders in-place
                        self.store.update_gss_detail(gss_key, VT_UV,
                                                     detail_str, uv_fdrs_nonbc,
                                                     round(worst["Vavg"],4))
                    self.store.clear_condition(gss_key, VT_OV)
                else:
                    # Multiple-feeder GSS but only 1 UV — likely PT fault, not true bus UV
                    log.debug(f"UV suppressed for {gss_key}: {len(uv_fdrs_nonbc)}/{total_nonbc} feeders UV (need ≥2)")
                    self.store.clear_condition(gss_key, VT_UV)
                    self.store.clear_condition(gss_key, VT_OV)
            else:
                self.store.clear_condition(gss_key, VT_OV)
                self.store.clear_condition(gss_key, VT_UV)

        # ── Bus coupler diversion ─────────────────────────
        for v in self._bc.update(live_data, off_thr):
            # Handle auto-clear sentinel
            if v.get("_clear_div"):
                self.store.clear_condition(v["_clear_div"], VT_DIV)
                continue
            vtype = v.get("type","")
            asset = v.get("AssetCode","")
            if vtype == VT_RST:
                self.store.clear_condition(asset, VT_DIV)
                ak_off           = f"{asset}_{VT_OFF}"
                # BC diversion/restoration is ALWAYS significant — notify regardless of delay
                # (BC diversions are deliberate bus transfers, not transient faults)
                off_duration = self.store.get_active_duration(ak_off)
                dur_str      = _fmt_dur_sec(off_duration) if off_duration else ""
                if dur_str:
                    v["detail"] = v.get("detail","") + f" Outage duration: {dur_str}."
                    v["_off_duration_str"] = dur_str
                v["_was_off_notified"] = True  # BC always notifies
                # Also ensure the FEEDER_OFF notification fires if it hadn't yet
                if not off_notified_cache.get(asset, False):
                    # Force-mark OFF as notified so _retry_delayed doesn't double-fire
                    off_aid = self.store._active.get(ak_off)
                    if off_aid:
                        self.store.mark_notified(off_aid, "email")
                        self.store.mark_notified(off_aid, "wa")
                if self.store.add(v, self.cfg):
                    raw_viols.append(v)
            elif vtype == VT_DIV:
                if self.store.add(v, self.cfg):
                    raw_viols.append(v)
            else:
                if self.store.add(v, self.cfg):
                    raw_viols.append(v)

        self._save_history()
        log.info(f"Scan: {len(live_data)} meters, {len(raw_viols)} new alerts")
        if raw_viols:
            self._notify(raw_viols)

        # Retry pending delayed notifications every cycle
        # Active alerts not yet notified whose delay has now elapsed
        self._retry_delayed()

        return raw_viols

    def _retry_delayed(self):
        """Re-check active unnotified alerts — send if their delay has now elapsed."""
        try:
            pending = [a for a in self.store.all(active_only=True, limit=300)
                       if not a.get("notified_email") and not a.get("notified_wa")
                       and a.get("type") not in ("SUDDEN_LOAD_DROP","SUDDEN_LOAD_RAISE","LOAD_RESTORED")]
            if pending:
                self._notify(pending)
        except Exception as e:
            log.warning(f"_retry_delayed error: {e}")

    @staticmethod
    def _make_gss(vtype, gv: dict, gss_key: str,
                  value=None, limit=None, detail="",
                  affected_feeders=None) -> dict:
        """Create a GSS-level OV/UV alert with feeder drill-down list."""
        gss = gv.get("Gss","")
        import json as _json
        return {
            "id":           f"{gss_key}_{vtype}_{int(time.time()*1000)}",
            "asset_key":    f"{gss_key}_{vtype}",
            "type":         vtype,
            "severity":     SEVERITY.get(vtype,"HIGH"),
            "AssetCode":    gss_key,     # GSS identifier
            "Circle":       gv.get("Circle",""),
            "Division":     gv.get("Division",""),
            "Gss":          gss,
            "Feeder":       f"GSS: {gss}",   # shown in alert title
            "FeederType":   "GSS",
            "FeederRating": None,
            "Vr":  gv.get("Vr"),
            "Vy":  gv.get("Vy"),
            "Vb":  gv.get("Vb"),
            "Ir":  None, "Iy": None, "Ib": None,
            "ApparentPower": None,
            "value":   value, "limit": limit, "detail": detail,
            # Feeder drill-down stored as JSON in extra field
            "affected_feeders": _json.dumps(affected_feeders or []),
            "timestamp":    datetime.now().isoformat(),
            "acked": False, "notified_email": False, "notified_wa": False,
        }

    @staticmethod
    def _make(vtype, row, master, value=None, limit=None, detail="") -> dict:
        asset = row.get("AssetCode","")
        return {
            "id":          f"{asset}_{vtype}_{int(time.time()*1000)}",
            "asset_key":   f"{asset}_{vtype}",
            "type":        vtype,
            "severity":    SEVERITY.get(vtype,"MEDIUM"),
            "AssetCode":   asset,
            "Circle":      row.get("Circle",""),
            "Division":    row.get("Division",""),
            "Gss":         row.get("Gss",""),
            "Feeder":      row.get("Feeder",""),
            "FeederType":  row.get("FeederType",""),
            "IsBusCoupler":bool(row.get("IsBusCoupler", False)),
            "FeederRating":master.get("FeederRating") if master else None,
            "Vr":row.get("Vr"),"Vy":row.get("Vy"),"Vb":row.get("Vb"),
            "Ir":row.get("Ir"),"Iy":row.get("Iy"),"Ib":row.get("Ib"),
            "ApparentPower":row.get("ApparentPower"),
            "value":value, "limit":limit, "detail":detail,
            "timestamp":   datetime.now().isoformat(),
            "acked":False, "notified_email":False, "notified_wa":False,
        }

    def _notify_wa(self, violations: list):
        """Send WA-only notifications — used for retry after startup login.
        Respects delay. Does NOT re-send email (already sent or will send on next cycle)."""
        if not self.cfg.get("whatsapp.enabled") or not violations:
            return
        wa_types    = self.cfg.get("whatsapp.alert_types")
        email_types = self.cfg.get("email.alert_types")

        # Delay check (same as _notify)
        def get_delay_min(v):
            t   = v.get("type","")
            lvl = v.get("ol_level", 1)
            if t == "OL":
                return float(self.cfg.get(f"notify_delay.OL_L{lvl}", {1:10,2:0,3:0}.get(lvl,0)))
            defaults = {VT_OV:15,VT_UV:60,VT_OFF:10,VT_PTF:0,VT_LJP:0,VT_DIV:0,VT_RST:0}
            return float(self.cfg.get(f"notify_delay.{t}", defaults.get(t, 0)))

        def delay_elapsed(v):
            delay = get_delay_min(v)
            if delay <= 0: return True
            ts = v.get("first_seen") or v.get("timestamp","")
            if not ts: return True
            try:
                age_min = (datetime.now() - datetime.fromisoformat(ts[:19].replace(" ","T"))).total_seconds() / 60
                return age_min >= delay
            except Exception:
                return True

        w_viols = [v for v in violations
                   if delay_elapsed(v)
                   and (not wa_types or v.get("type") in wa_types)
                   and (v.get("type") == VT_RST or True)]  # RST always included

        if not w_viols:
            return

        recipients = self.cfg.get("whatsapp.recipients", [])
        if not recipients:
            return

        batches = self._batch_wa_messages(w_viols, self._wa_msg)
        for batch in batches:
            if self.wa.send(to=recipients, message=batch):
                for v in w_viols:
                    self.store.mark_notified(v["id"], "wa")

    @staticmethod
    def _batch_wa_messages(viols: list, msg_fn) -> list:
        """
        Group multiple violations into batched WA messages with dotted separators.
        Returns list of message strings, each under MAX_BATCH_LEN chars.
        """
        SEPARATOR = "\n・・・・・・・・・・・・・・・・・\n"
        MAX_BATCH  = 3800  # leave room for separator overhead

        messages = [msg_fn(v) for v in viols]
        batches   = []
        current   = ""

        for msg in messages:
            if not current:
                current = msg
            elif len(current) + len(SEPARATOR) + len(msg) <= MAX_BATCH:
                current += SEPARATOR + msg
            else:
                batches.append(current)
                current = msg

        if current:
            batches.append(current)
        return batches

    def _notify(self, violations: list):
        email_types = self.cfg.get("email.alert_types")
        wa_types    = self.cfg.get("whatsapp.alert_types")

        # Per-type delay (minutes) before sending notification
        def get_delay_min(v: dict) -> float:
            t   = v.get("type","")
            lvl = v.get("ol_level", 1)
            if t == VT_OL:
                key     = f"notify_delay.OL_L{lvl}"
                default = {1:10, 2:0, 3:0}.get(lvl, 0)
            else:
                key     = f"notify_delay.{t}"
                default = {VT_OV:15,VT_UV:60,VT_OFF:10,
                           VT_PTF:0,VT_LJP:0,VT_DIV:0,VT_RST:0}.get(t, 0)
            return float(self.cfg.get(key, default))

        def within_delay(v: dict) -> bool:
            delay = get_delay_min(v)
            if delay <= 0:
                return False
            # Use first_seen from DB if available, fallback to timestamp on the violation dict
            # first_seen = when alert was FIRST created (even if this is a re-fire)
            ts = v.get("first_seen") or v.get("timestamp","")
            if not ts:
                return False
            try:
                # Parse the timestamp — could be ISO format with T or space
                ts_clean = ts[:19].replace(" ","T")
                age_min = (datetime.now() - datetime.fromisoformat(ts_clean)).total_seconds() / 60
                return age_min < delay
            except Exception:
                return False

        # Escalation: only notify at the highest OL level in this batch
        ol_max_level = {}
        for v in violations:
            if v.get("type") == VT_OL:
                ak  = v.get("asset_key","")
                lvl = v.get("ol_level", 1)
                ol_max_level[ak] = max(ol_max_level.get(ak, 0), lvl)

        def should_notify(v):
            if v.get("type") == VT_RST:
                # Notify restoration ONLY if the original OFF was notified
                # Short trips (OFF within delay window) = no OFF notif = no RST notif
                # But the DB record is always created regardless
                return v.get("_was_off_notified", False)
            # LJP normalize/false-detection: always notify (no delay)
            if v.get("_ljp_norm"):
                return True
            # OL suppressed by cooldown: never notify
            if v.get("_ol_suppressed"):
                return False
            if within_delay(v):
                return False
            if v.get("type") != VT_OL:
                return True
            return v.get("ol_level",1) == ol_max_level.get(v.get("asset_key",""), 1)

        e_viols = [v for v in violations
                   if should_notify(v) and (
                       v.get("type") == VT_RST or
                       not email_types or v["type"] in email_types)]
        w_viols = [v for v in violations
                   if should_notify(v) and (
                       v.get("type") == VT_RST or
                       not wa_types   or v["type"] in wa_types)]

        contacts = self.cfg.get("contacts", [])

        def wa_to(v):
            for c in contacts:
                if not c.get("active", True): continue
                if ((not c.get("circle")) or c["circle"].upper() == v.get("Circle","").upper()) and \
                   ((not c.get("division")) or c.get("division","").upper() in v.get("Division","").upper()):
                    nos = [n.strip() for n in (c.get("waNos","")).replace(",","\n").split("\n") if n.strip()]
                    if nos: return nos
            return self.cfg.get("whatsapp.recipients", [])

        if self.cfg.get("email.enabled") and e_viols:
            ok = self.email.send_violations(e_viols)
            if ok:
                for v in e_viols: self.store.mark_notified(v["id"],"email")

        if self.cfg.get("whatsapp.enabled") and w_viols:
            # Group by recipient list, then batch into single messages with separators
            recipient_groups: dict = {}
            for v in w_viols:
                nos = tuple(wa_to(v))
                recipient_groups.setdefault(nos, []).append(v)

            for nos, group in recipient_groups.items():
                if not nos: continue
                # Build per-violation messages with active-since appended for delayed types
                def build_msg(v):
                    msg = self._wa_msg(v)
                    delay = get_delay_min(v)
                    if delay > 0:
                        dur = self._fmt_active_since(v)
                        if dur:
                            msg = msg.rstrip() + f"\n• Active Since: {dur}"
                    return msg

                batches = self._batch_wa_messages(group, build_msg)
                for batch in batches:
                    if self.wa.send(to=list(nos), message=batch):
                        for v in group:
                            self.store.mark_notified(v["id"], "wa")

    @staticmethod
    def _fmt_active_since(v: dict) -> str:
        """Format active duration string from first_seen."""
        ts = v.get("first_seen") or v.get("timestamp","")
        if not ts:
            return ""
        try:
            from datetime import datetime as _dt
            sec = int((_dt.now() - _dt.fromisoformat(ts[:19].replace(" ","T"))).total_seconds())
            h, rem = divmod(sec, 3600)
            m = rem // 60
            if h: return f"{h}h {m}m"
            return f"{m}m"
        except Exception:
            return ""

    @staticmethod
    def _wa_msg(v: dict) -> str:
        t    = v.get("type","")
        ts   = (v.get("timestamp") or v.get("first_seen",""))[:19].replace("T"," ")
        area = f"{v.get('Circle','')}/{v.get('Division','')}".strip("/")
        gss  = v.get("Gss","")
        det  = v.get("detail","")

        ICONS  = {VT_OV:"🔴",VT_UV:"🟡",VT_OL:"🟠",VT_OFF:"⚫",
                  VT_SLD:"📉",VT_SLR:"📈",VT_DIV:"🔀",VT_RST:"✅",
                  VT_PTF:"⚠️",VT_LJP:"⛓️",VT_BCT:"⚫",VT_BCR:"✅"}
        LABELS = {VT_OV:"OV Alert",VT_UV:"UV Alert",VT_OL:"Overload Alert",
                  VT_OFF:"Feeder OFF",VT_SLD:"Load Drop",VT_SLR:"Load Rise",
                  VT_DIV:"Load Diverted",VT_RST:"Load Restored",
                  VT_PTF:"PT Phase Missing",VT_LJP:"Line Jumper Parting",
                  VT_BCT:"Feeder OFF",VT_BCR:"Feeder Normalized"}
        icon  = ICONS.get(t,"⚡")
        label = LABELS.get(t, t)

        # BC tripped while carrying a confirmed diversion — the diverted
        # feeder's own load is genuinely cut. Format matches a normal
        # Feeder OFF exactly, but Feeder field shows the diverted feeder
        # combined with the BC identity (per spec).
        if t == VT_BCT:
            feeder = v.get("Feeder") or v.get("AssetCode","")
            lines = [f"⚫ *TPNODL Feeder OFF*", "",
                     f"• Feeder: {feeder}",
                     f"• Area: {area}",
                     f"• GSS: {gss}",
                     f"• {det}",
                     f"• Time: {ts}"]
            return "\n".join(lines)

        # Restoration of the above — feeder field combined the same way,
        # auto-acknowledged, matching the LOAD_RESTORED format exactly.
        if t == VT_BCR:
            feeder  = v.get("Feeder") or v.get("AssetCode","")
            imax_v  = v.get("value","")
            dur_str = v.get("_off_duration_str","")
            lines = [f"✅ *TPNODL Feeder Normalized*", "",
                     f"• Feeder: {feeder}",
                     f"• Area: {area}",
                     f"• GSS: {gss}"]
            if imax_v:
                lines.append(f"• Current: {imax_v:.1f}A (Restored)")
            if dur_str:
                lines.append(f"• ⏱ Normalized after: *{dur_str}*")
            lines.append(f"• Time: {ts}")
            return "\n".join(lines)

        # Load Restored — show feeder name, current, and outage duration
        if t == VT_RST:
            feeder = v.get("Feeder") or v.get("AssetCode","")
            imax_v = v.get("value","")
            dur_str = v.get("_off_duration_str","")
            lines = [f"✅ *TPNODL Feeder Normalized*", "",
                     f"• Feeder: {feeder}",
                     f"• Area: {area}",
                     f"• GSS: {gss}"]
            if imax_v:
                lines.append(f"• Current: {imax_v:.1f}A (Restored)")
            if dur_str:
                lines.append(f"• ⏱ Normalized after: *{dur_str}*")
            lines.append(f"• Time: {ts}")
            return "\n".join(lines)
            return (
                f"{icon} *TPNODL {label}*\n\n"
                f"• Area: {area}\n"
                f"• GSS: {gss}\n"
                f"• {det}\n"
                f"• Time: {ts}"
            )

        # Load Diverted via Bus Coupler
        if t == VT_DIV:
            feeder    = v.get("Feeder") or v.get("AssetCode","")
            bc_asset  = v.get("BusCouplerAsset","")
            bc_after  = v.get("BCImaxAfter", 0)
            bc_before = v.get("BCImaxBefore", 0)
            pf_imax   = v.get("pre_fault_imax", v.get("FeederImaxBefore", 0))
            pf_iavg   = v.get("pre_fault_iavg", 0)
            pf_ir     = v.get("pre_fault_ir", 0)
            pf_iy     = v.get("pre_fault_iy", 0)
            pf_ib     = v.get("pre_fault_ib", 0)
            p75       = v.get("feeder_p75", 0)
            lines = [
                f"🔀 *TPNODL Load Diverted*", "",
                f"• Feeder: {feeder}",
                f"• Area: {area}",
                f"• GSS: {gss}",
            ]
            # Pre-fault load
            if pf_imax:
                lines.append(f"• Pre-Fault Load: Imax={pf_imax:.1f}A  Iavg={pf_iavg:.1f}A")
            if pf_ir or pf_iy or pf_ib:
                lines.append(f"  3-ph: Ir={pf_ir:.1f}A  Iy={pf_iy:.1f}A  Ib={pf_ib:.1f}A")
            if p75:
                lines.append(f"• Typical Load (p75): {p75:.1f}A")
            # BC load
            lines.append(f"• BC {bc_asset}: {bc_before:.1f}A → {bc_after:.1f}A (Diverted)")
            lines.append(f"• Time: {ts}")
            return "\n".join(lines)

        # LJP Normalized / False Detection
        if t == VT_LJP and v.get("_ljp_norm"):
            feeder   = v.get("Feeder") or v.get("AssetCode","")
            is_false = v.get("_ljp_false", False)
            hdr  = "⛓️ *TPNODL LJP False Detection*" if is_false else "✅ *TPNODL LJP Normalized*"
            note = ("_Transient data — not a real fault_" if is_false
                    else "_Conductor balance restored_")
            # Pull 3-ph currents directly from violation dict (set by _make from row)
            ir_v = v.get("Ir"); iy_v = v.get("Iy"); ib_v = v.get("Ib")
            imax_v = v.get("value", "")  # value = i_unbal_pct at time of normalize
            # Get 3-ph current string from detail line (already formatted there)
            det_line = det  # keep full detail for context
            lines = [hdr, note, "",
                     f"• Feeder: {feeder}",
                     f"• Area: {area}",
                     f"• GSS: {gss}"]
            # Show 3-ph load amps prominently
            if ir_v is not None and iy_v is not None and ib_v is not None:
                try:
                    imax_now = max(float(ir_v), float(iy_v), float(ib_v))
                    lines.append(f"• Load (3-ph): Ir={float(ir_v):.1f}A  Iy={float(iy_v):.1f}A  Ib={float(ib_v):.1f}A  (Imax={imax_now:.1f}A)")
                except Exception:
                    pass
            lines += [f"• {det_line}",
                      f"• Time: {ts}"]
            return "\n".join(lines)

        # GSS-level UV / OV Alert
        if t in (VT_UV, VT_OV):
            icon_uv = "🟡" if t == VT_UV else "🔴"
            lbl_uv  = "UV Alert" if t == VT_UV else "OV Alert"
            lines = [
                f"{icon_uv} *TPNODL {lbl_uv}*", "",
                f"• GSS: {gss}",
                f"• Area: {area}",
                f"• {det}",
                f"• Time: {ts}",
            ]
            return "\n".join(lines)

        # Feeder-level OL — show escalation level
        if t == VT_OL:
            level   = v.get("ol_level", 1)
            lv_icon = {1:"🟠", 2:"🔶", 3:"🔴"}.get(level, "🟠")
            lv_lbl  = {1:"Overload L1 ≥100%", 2:"⚠ Overload L2 ≥110%", 3:"🚨 Overload L3 ≥120%"}.get(level,"Overload")
            feeder  = v.get("Feeder") or v.get("AssetCode","")
            area    = f"{v.get('Circle','')}/{v.get('Division','')}".strip("/")
            lines = [
                f"{lv_icon} *TPNODL {lv_lbl}*", "",
                f"• Feeder: {feeder}",
                f"• Area: {area}",
                f"• GSS: {v.get('Gss','')}",
                f"• {det}",
                f"• Time: {ts}",
            ]
            if level > 1:
                lines.insert(1, f"_Escalated from L{level-1}_")
            return "\n".join(lines)

        # Other feeder-level alerts (OFF, LJP, PTF, etc.)
        feeder = v.get("Feeder") or v.get("AssetCode","")
        lines  = [f"{icon} *TPNODL {label}*", "",
                  f"• Feeder: {feeder}",
                  f"• Area: {area}",
                  f"• GSS: {gss}"]
        if det:
            lines.append(f"• {det}")
        lines.append(f"• Time: {ts}")
        return "\n".join(lines)


class BusCouplerDiversionDetector:
    """
    Bus Coupler Diversion Detection — exact stage flow:
    ──────────────────────────────────────────────────────────────────────────
    STAGE 1 — Feeder OFF/Tripped detected (Imax ≤ off_thr, e.g. ≤1.0A)
              ↓
    STAGE 2 — Waiting: detector watches BC every cycle, takes NO action
              until BC shows a load raise. No fixed timer — purely event-driven.
              ↓
    STAGE 3 — Trigger event observed (one of):
              (a) BC Load Raise detected  → bc_is_active AND bc rose meaningfully
              (b) Feeder Restored (ON) detected → handled separately, ends diversion
              (c) Stale/Frozen Feeder + BC Load Raise → feeder meter still reports
                  (not OFF, not "No match in live data") but Imax/Vavg values are
                  frozen for 3+ consecutive cycles — a comms glitch may be masking
                  a real outage. If BC rises while this is happening, same Load
                  Comparison Logic (Stage 4) applies, gated on staleness instead
                  of feeder_now_off.
              ↓
    STAGE 4 — Load Comparison Logic: validate the BC rise plausibly matches
              the OFF feeder's load (pre-fault load / p75 ratio check,
              30% min-rise threshold, cross-GSS guard, back-feed exclusion)
              ↓ (if satisfied)
    STAGE 5 — Waiting Cycle started: enters PENDING state, bc_count=1
              ↓
    STAGE 6 — Waiting period over: required consecutive STABLE cycles reached
              (1 feeder OFF → CONFIRMED INSTANTLY on the 1st cycle, since the
               Load Comparison checks in Stage 4 already validate the match;
               2+ feeders OFF simultaneously → 3 cycles required, since
               attributing BC's combined load to multiple candidates is
               inherently ambiguous and needs the extra stability check.
               "stable" = BC value changed >0.1A from last reading,
               >20% drift between cycles resets the counter as noise)
              ↓
    RESULT  — CONFIRMED: LOAD_DIVERTED alert fires.
              Timestamp = moment of Stage 3 trigger (first BC rise), not
              the moment confirmation completed.

    RESTORATION (separate flow, can happen at any pending/active stage):
      Feeder comes back ON → LOAD_RESTORED fires (if was CONFIRMED)
                            → pending cancelled silently (if still in Stage 5/6)

    BACK-FEED EXCLUSION (Stage 4 guard):
      All feeders in GSS are >1A (loaded) BUT BC also >1A → not a diversion,
      logged as back-feed condition, never enters PENDING.

    PERSISTENCE:
      _pending (Stage 5/6 state) saved to data/.bc_pending.json on every change.
      Survives server restart — waiting cycle count resumes correctly.
    """
    BC_CHANGE_THR    = 0.1    # A — minimum BC change to count as new stable reading
    DRIFT_RESET_PCT  = 0.20   # 20% BC load drift → reset counter (transient)
    CONFIRM_1_FEEDER = 1      # single feeder OFF — confirm immediately on 1st
                              # cycle since BC ratio + pre-fault/p75 load check
                              # already validate the match; no need to wait.
    CONFIRM_2_FEEDER = 3      # 2+ feeders OFF simultaneously — keep multi-cycle
                              # confirmation since attributing BC's combined
                              # load to multiple candidates is inherently
                              # ambiguous and needs the extra stability check.
    # No time limit — CONFIRM_REQUIRED is cycle-count based only

    PENDING_FILE = "data/.bc_pending.json"

    def __init__(self):
        self._prev:    dict = {}  # asset_code -> prev imax
        self._active:  dict = {}  # feeder_code -> {bc, f_before, bc_after, ts, first_rise_ts}
        self._pending: dict = {}  # feeder_code -> {bac, gss, fdr_snap, f_before,
                                  #   bc_count, bc_last, first_rise_ts, first_rise_val}
        self._store_ref      = None  # AlertStore — for is_active()/pre-fault lookup
        self._peak_store_ref = None  # PeakLoadStore — for get_feeder_hourly_profile (p75)
        self._stale_vals  = {}    # asset_code -> (last_imax, last_vavg, repeat_count)
        self._load_pending()

    def _load_pending(self):
        """Restore pending BC confirmations from disk (survives server restart)."""
        try:
            import json as _j, os as _os
            pf = self.PENDING_FILE
            if _os.path.exists(pf):
                data = _j.load(open(pf))
                self._pending = data
                if self._pending:
                    log.info(f"BC pending state restored: {len(self._pending)} candidate(s)")
        except Exception as e:
            log.warning(f"BC pending load error: {e}")

    def _save_pending(self):
        """Persist pending BC confirmations to disk."""
        try:
            import json as _j, os as _os
            _os.makedirs("data", exist_ok=True)
            _j.dump(self._pending, open(self.PENDING_FILE, "w"), default=str)
        except Exception as e:
            log.warning(f"BC pending save error: {e}")

    def update(self, live_data: list, off_thr: float = 1.0) -> list:
        viols   = []
        now     = time.time()
        now_iso = datetime.now().isoformat()

        # Group by GSS
        by_gss: dict = {}
        for row in live_data:
            gss = row.get("Gss","")
            if gss:
                by_gss.setdefault(gss, []).append(row)

        for gss, rows in by_gss.items():
            bcs  = [r for r in rows if r.get("IsBusCoupler")]
            fdrs = [r for r in rows if not r.get("IsBusCoupler")]

            if not bcs:
                for r in fdrs:
                    self._prev[r.get("AssetCode","")] = r.get("_imax",0)
                continue

            for bc in bcs:
                bac    = bc.get("AssetCode","")
                bcimax = bc.get("_imax", max(
                    float(bc.get("Ir",0)), float(bc.get("Iy",0)), float(bc.get("Ib",0))))
                prev_b = self._prev.get(bac, 0)
                bc_is_active = bcimax > off_thr

                # ── BC trips while carrying a CONFIRMED diversion ──────────
                # Normal BC FEEDER_OFF (0A) is meaningless noise — that's the
                # BC's idle state. But if THIS BC is currently the carrier for
                # one or more confirmed diversions (self._active), and it now
                # drops to idle, the diverted feeder(s)' supply is genuinely
                # cut. This is a real outage, distinct from both a normal
                # FEEDER_OFF and a normal diversion-restoration.
                diverted_via_this_bc = [
                    (fac2, d) for fac2, d in self._active.items()
                    if d.get("bc") == bac
                ]
                bc_just_tripped = prev_b > off_thr and bcimax <= off_thr

                if diverted_via_this_bc:
                    bc_trip_ak = f"{bac}_{VT_BCT}"
                    if bc_just_tripped and not self._store_ref.is_active(bc_trip_ak):
                        for fac2, d in diverted_via_this_bc:
                            fdr2 = next((f for f in fdrs
                                         if f.get("AssetCode","") == fac2), None)
                            fdr_name = fdr2.get("Feeder", fac2) if fdr2 else fac2
                            v = {
                                "id":        f"{bac}_{VT_BCT}_{int(time.time()*1000)}",
                                "asset_key": bc_trip_ak,
                                "type":      VT_BCT,
                                "severity":  SEVERITY.get(VT_BCT,"CRITICAL"),
                                "AssetCode": bac,
                                "Circle":    bc.get("Circle",""),
                                "Division":  bc.get("Division",""),
                                "Gss":       gss,
                                # Feeder field combines the diverted feeder's
                                # own name with the BC identity, matching the
                                # requested alert format exactly.
                                "Feeder":    f"{fdr_name}🔀{bc.get('Feeder',bac)} ({bac})",
                                "FeederType": (fdr2.get("FeederType","") if fdr2 else ""),
                                "Vr":bc.get("Vr"),"Vy":bc.get("Vy"),"Vb":bc.get("Vb"),
                                "Ir":bc.get("Ir"),"Iy":bc.get("Iy"),"Ib":bc.get("Ib"),
                                "value":     round(bcimax,4),
                                "lim":       off_thr,
                                "detail":    (f"Current {prev_b:.2f}A→{bcimax:.4f}A "
                                              f"(≤{off_thr}A). Feeder tripped."),
                                "timestamp": now_iso,
                                "first_seen": now_iso,
                                "acked":     False,
                                "notified_email": False,
                                "notified_wa":    False,
                                # Persisted (see alert_store._insert allowlist)
                                # so the restoration check below can find this
                                # alert's own diverted feeder EVEN AFTER the
                                # feeder has already been popped out of
                                # self._active by the normal LOAD_RESTORED
                                # path elsewhere — that decoupling is the
                                # actual fix for the "stays Active forever"
                                # bug: this alert no longer depends on
                                # diverted_via_this_bc still being non-empty
                                # on later cycles.
                                "_diverted_feeder":      fac2,
                                "_diverted_feeder_name": fdr_name,
                                "_bc_asset":             bac,
                            }
                            if self._store_ref.add(v, None):
                                viols.append(v)
                                log.warning(f"BC_TRIP_WHILE_DIVERTED: {bac} tripped "
                                            f"while carrying {fac2} ({fdr_name})'s "
                                            f"diverted load @ {gss}")

                # ── Restoration of BC_TRIP_WHILE_DIVERTED ───────────────────
                # Per spec: this clears when the PREVIOUSLY-DIVERTED FEEDER's
                # own load is restored — NOT when the BC itself comes back.
                # The BC tripping was the whole point of this alert (the
                # diverted feeder lost its only remaining path); waiting for
                # the BC to also recover is irrelevant and was the actual bug
                # (BC_TRIP_WHILE_DIVERTED stayed Active indefinitely even
                # after the diverted feeder visibly restored through its own
                # meter, because the old check required bc_is_active too).
                #
                # This check runs independent of diverted_via_this_bc/
                # self._active — it queries the DB directly for ANY active
                # VT_BCT alert on this BC, then checks THAT alert's own
                # recorded _diverted_feeder against the CURRENT live data.
                bct_ak = f"{bac}_{VT_BCT}"
                if self._store_ref.is_active(bct_ak):
                    active_bct = self._store_ref.get_active_alert(bct_ak)
                    diverted_fac = (active_bct or {}).get("_diverted_feeder", "")

                    if diverted_fac:
                        # Normal path: we know exactly which feeder this
                        # alert was protecting — check ITS live current.
                        diverted_fdr_live = next(
                            (f for f in fdrs if f.get("AssetCode","") == diverted_fac), None
                        )
                        if diverted_fdr_live:
                            d_imax = diverted_fdr_live.get("_imax", max(
                                float(diverted_fdr_live.get("Ir",0)),
                                float(diverted_fdr_live.get("Iy",0)),
                                float(diverted_fdr_live.get("Ib",0))))
                            feeder_restored = d_imax > off_thr
                        else:
                            feeder_restored = False
                        fdr_name_fallback = diverted_fac
                    else:
                        # LEGACY FALLBACK: this VT_BCT alert was created
                        # before _diverted_feeder persistence existed (an
                        # alert from before this fix was deployed) — we
                        # genuinely don't know which specific feeder it was
                        # protecting. Best available signal: if NO feeder
                        # at this same GSS is currently OFF, everything has
                        # since restored on its own, so it's safe to clear
                        # this stale alert rather than leaving it stuck
                        # active forever. If any feeder IS still OFF at this
                        # GSS, conservatively stay active (matches the
                        # spirit of "only clear once load is confirmed back").
                        any_off_at_gss = any(
                            f.get("_imax", max(float(f.get("Ir",0)),
                                                float(f.get("Iy",0)),
                                                float(f.get("Ib",0)))) <= off_thr
                            for f in fdrs
                        )
                        feeder_restored = not any_off_at_gss
                        d_imax = 0.0
                        fdr_name_fallback = "(unknown — legacy alert, all feeders at GSS now restored)"
                        if feeder_restored:
                            log.warning(f"BC_TRIP_WHILE_DIVERTED legacy fallback clear: "
                                        f"{bac} @ {gss} — alert predates _diverted_feeder "
                                        f"tracking, but no feeder at this GSS is OFF anymore, "
                                        f"clearing as stale")

                    if feeder_restored:
                        fdr_name = (active_bct or {}).get(
                            "_diverted_feeder_name", fdr_name_fallback)
                        dur = self._store_ref.get_active_duration(bct_ak)
                        dur_str = _fmt_dur_sec(dur) if dur else ""

                        is_legacy_clear = not diverted_fac
                        detail_text = (
                            f"All feeders at {gss} restored — clearing stale "
                            f"diversion-protection alert (predates per-feeder tracking)."
                            if is_legacy_clear else
                            f"Diverted feeder {fdr_name} restored ({d_imax:.1f}A) — "
                            f"no longer dependent on BC {bac}."
                        ) + (f" Was affected for: {dur_str}." if dur_str else "")

                        rv = {
                            "id":        f"{bac}_{VT_BCR}_{int(time.time()*1000)}",
                            "asset_key": f"{bac}_{VT_BCR}",
                            "type":      VT_BCR,
                            "severity":  SEVERITY.get(VT_BCR,"INFO"),
                            "AssetCode": bac,
                            "Circle":    bc.get("Circle",""),
                            "Division":  bc.get("Division",""),
                            "Gss":       gss,
                            "Feeder":    f"{fdr_name}🔀{bc.get('Feeder',bac)} ({bac})",
                            "FeederType": "",
                            "Vr":bc.get("Vr"),"Vy":bc.get("Vy"),"Vb":bc.get("Vb"),
                            "Ir":bc.get("Ir"),"Iy":bc.get("Iy"),"Ib":bc.get("Ib"),
                            "value":     round(d_imax,2),
                            "lim":       off_thr,
                            "detail":    detail_text,
                            "timestamp": now_iso,
                            "first_seen": now_iso,
                            "acked":     True,   # auto-acknowledged per spec
                            "notified_email": False,
                            "notified_wa":    False,
                            "_off_duration_str": dur_str,
                        }
                        if self._store_ref.add(rv, None):
                            viols.append(rv)
                            log.info(f"BC_DIVERTED_NORMALIZED: {'legacy-clear' if is_legacy_clear else diverted_fac} "
                                     f"({fdr_name}) — clearing BC_TRIP_WHILE_DIVERTED "
                                     f"for BC {bac}. Was affected {dur_str}")
                        self._store_ref.clear_condition(bac, VT_BCT)

                # ── BC_LOADING_START: BC just transitioned idle → active ──
                # Recorded as a normal DB event (like SUDDEN_LOAD_RAISE),
                # independent of whether a diversion is ultimately confirmed.
                # This gives every future diversion a real, queryable "BC
                # started carrying load" timestamp — instead of relying on
                # the moment detection software happened to confirm it, which
                # is wrong whenever confirmation is delayed (restart, long
                # OFF, etc). Going forward this makes the scenario in this
                # conversation (instant-confirm showing "now" instead of the
                # true diversion start) impossible to recur.
                if bc_is_active and prev_b <= off_thr and self._store_ref:
                    bc_ak = f"{bac}_{VT_BCL}"
                    if not self._store_ref.is_active(bc_ak):
                        bcl_v = {
                            "id":             f"{bac}_{VT_BCL}_{int(time.time()*1000)}",
                            "asset_key":      bc_ak,
                            "type":           VT_BCL,
                            "severity":       SEVERITY.get(VT_BCL,"INFO"),
                            "AssetCode":      bac,
                            "Circle":         bc.get("Circle",""),
                            "Division":       bc.get("Division",""),
                            "Gss":            gss,
                            "Feeder":         bc.get("Feeder", bac),
                            "FeederType":     bc.get("FeederType",""),
                            "Vr":bc.get("Vr"),"Vy":bc.get("Vy"),"Vb":bc.get("Vb"),
                            "Ir":bc.get("Ir"),"Iy":bc.get("Iy"),"Ib":bc.get("Ib"),
                            "value":          round(bcimax,2),
                            "lim":            off_thr,
                            "detail":         (f"BC {bac} started carrying load: "
                                                f"{prev_b:.2f}A→{bcimax:.2f}A @ {gss}"),
                            "timestamp":      now_iso,
                            "first_seen":     now_iso,
                            "acked":          False,
                            "notified_email": False,
                            "notified_wa":    False,
                        }
                        if self._store_ref.add(bcl_v, None):
                            viols.append(bcl_v)
                            log.info(f"BC_LOADING_START: {bac} {prev_b:.2f}A→"
                                     f"{bcimax:.2f}A @ {gss}")

                # Count how many feeders in this GSS are OFF this cycle
                off_fdrs_this_cycle = [
                    f for f in fdrs
                    if f.get("_imax", max(float(f.get("Ir",0)),
                                          float(f.get("Iy",0)),
                                          float(f.get("Ib",0)))) <= off_thr
                ]
                n_off = len(off_fdrs_this_cycle)

                # ── BACK-FEED CHECK: all feeders loaded AND BC also carrying load ──
                # This means BC is feeding into the bus from another source
                if bc_is_active and n_off == 0:
                    all_loaded = all(
                        f.get("_imax", max(float(f.get("Ir",0)),
                                           float(f.get("Iy",0)),
                                           float(f.get("Ib",0)))) > off_thr
                        for f in fdrs
                    )
                    if all_loaded and fdrs:
                        # BC carrying load but no feeder is OFF → back-feed condition
                        # Only log/track — don't create LOAD_DIVERTED
                        log.debug(f"BC back-feed at {gss}: BC={bcimax:.1f}A, "
                                  f"all {len(fdrs)} feeders loaded")

                for fdr in fdrs:
                    fac   = fdr.get("AssetCode","")
                    fimax = fdr.get("_imax", max(
                        float(fdr.get("Ir",0)), float(fdr.get("Iy",0)),
                        float(fdr.get("Ib",0))))
                    f_vavg = (float(fdr.get("Vr",0)) + float(fdr.get("Vy",0))
                              + float(fdr.get("Vb",0))) / 3
                    is_stale_feeder = self._check_stale(fac, fimax, f_vavg)
                    prev_f         = self._prev.get(fac)
                    feeder_now_off = fimax <= off_thr

                    # ── RESTORATION: confirmed diversion ends only when BOTH:
                    #   1. Feeder load normalized (back ON, carrying current)
                    #   2. BC load has reduced by approximately THIS feeder's
                    #      contribution — not just "BC total dropped" (which
                    #      would be wrong if multiple feeders share the same BC)
                    # Other feeders still diverted to the same BC are checked
                    # via bac_other_active_feeders so their share isn't mistaken
                    # for this feeder's load still being present.
                    if fac in self._active and not feeder_now_off:
                        d = self._active[fac]
                        bac_this   = d.get("bc", "")
                        bc_peak    = d.get("bc_after", 0)      # BC level while THIS feeder was diverted
                        this_share = d.get("f_before", 0)      # this feeder's own pre-fault load

                        # Sum the bc_after share of OTHER feeders still actively
                        # diverted to the SAME BC (excludes this feeder)
                        other_active_bc_load = sum(
                            od.get("f_before", 0)
                            for ofac, od in self._active.items()
                            if ofac != fac and od.get("bc") == bac_this
                        )
                        # Expected BC level if only the OTHER feeders remain diverted
                        expected_remaining = other_active_bc_load

                        # BC reduced relative to what's expected once this feeder
                        # is excluded — allow generous tolerance (50%) for
                        # measurement variance, but must be a real drop
                        bc_reduced = (
                            not bc_is_active
                            or bcimax <= (expected_remaining + this_share * 0.5)
                        )

                        if bc_reduced:
                            d = self._active.pop(fac)
                            self._pending.pop(fac, None)
                            v = self._viol(VT_RST, fdr, d["bc"],
                                           d["f_before"], d["bc_after"], fimax,
                                           first_rise_ts=d.get("first_rise_ts"))
                            v["detail"] = (f"Feeder {fdr.get('Feeder',fac)} restored "
                                           f"({fimax:.1f}A). BC load reduced "
                                           f"{bc_peak:.1f}A→{bcimax:.1f}A "
                                           f"(other diverted feeders on this BC: "
                                           f"{other_active_bc_load:.1f}A). "
                                           f"Bus Coupler diversion at {gss} ended.")
                            viols.append(v)
                            log.info(f"LOAD_RESTORED (BC diversion ended): {fac} @ {gss} "
                                     f"— feeder back {fimax:.1f}A, BC {bc_peak:.1f}A→{bcimax:.1f}A, "
                                     f"other feeders on BC={other_active_bc_load:.1f}A")
                            self._stale_vals.pop(fac, None)  # reset staleness tracking
                        else:
                            # Feeder is back ON but BC still carries more load than
                            # expected once this feeder's share is excluded —
                            # likely still diverted (or another feeder also diverted)
                            d["bc_after"] = bcimax
                            log.debug(f"BC diversion {fac} NOT cleared — feeder back "
                                      f"({fimax:.1f}A) but BC={bcimax:.1f}A still exceeds "
                                      f"expected remaining ({expected_remaining:.1f}A "
                                      f"from other feeders)")
                        self._prev[fac] = fimax
                        continue

                    # ── PENDING cancelled: feeder back ON before confirmation ──
                    if fac in self._pending and not feeder_now_off:
                        self._pending.pop(fac)
                        self._save_pending()
                        log.info(f"BC pending cancelled — feeder {fac} back ON @ {gss}")
                        self._prev[fac] = fimax
                        continue

                    # ── AUTO-CLEAR stale DIV sentinel (feeder back, BC idle) ──
                    # Only fire if:
                    # 1. Feeder has an active LOAD_DIVERTED in DB (not just any feeder)
                    # 2. Feeder is NOT in _bc._active (means it was seeded but BC idle now)
                    # 3. Feeder is back ON (running normally)
                    # 4. BC is idle (no longer carrying diverted load)
                    # This prevents clearing LOAD_DIVERTED alerts on restart
                    if (fac not in self._active
                            and not feeder_now_off
                            and not bc_is_active):
                        # Double-check: only clear if LOAD_DIVERTED is actually active in DB
                        ak_div = f"{fac}_{VT_DIV}"
                        if hasattr(self, '_store_ref') and self._store_ref:
                            if self._store_ref.is_active(ak_div):
                                viols.append({"_clear_div": fac})
                        # If no store ref, skip — will be cleared when feeder restoration detected

                    if not feeder_now_off:
                        self._prev[fac] = fimax
                        continue

                    # Cross-GSS guard
                    if bc.get("Gss","") != fdr.get("Gss",""):
                        self._prev[fac] = fimax
                        continue

                    # ── Already confirmed + active — update BC snapshot ──
                    if fac in self._active:
                        self._active[fac]["bc_after"] = bcimax
                        self._prev[fac] = fimax
                        continue

                    # ── DETERMINE required confirmations for this feeder ──
                    # 1 feeder OFF → 2 stable cycles
                    # 2+ feeders OFF simultaneously → 3 stable cycles
                    required = self.CONFIRM_2_FEEDER if n_off >= 2 else self.CONFIRM_1_FEEDER

                    # ── Already PENDING — check BC stability ──────────
                    if fac in self._pending:
                        p = self._pending[fac]
                        bc_diff = abs(bcimax - p["bc_last"])

                        # Check for transient swing: BC drifted >20% → reset counter
                        bc_last_val = p["bc_last"]
                        if bc_last_val > 0:
                            drift_pct = abs(bcimax - bc_last_val) / bc_last_val
                            if drift_pct > self.DRIFT_RESET_PCT and bc_diff > 1.0:
                                # Large drift — reset count, update reference
                                old_count = p["bc_count"]
                                p["bc_count"] = 1
                                p["bc_last"]  = bcimax
                                p["required"] = required
                                self._save_pending()
                                log.info(f"BC pending {fac}: count RESET "
                                         f"(drift {drift_pct*100:.1f}% > 20%) "
                                         f"was {old_count} → 1/{required}")
                                self._prev[fac] = fimax
                                continue

                        if bc_diff > self.BC_CHANGE_THR:
                            # BC value changed — count as stable new reading
                            p["bc_count"] += 1
                            p["bc_last"]   = bcimax
                            p["required"]  = required  # update in case n_off changed
                            self._save_pending()
                            log.info(f"BC pending {fac}: count={p['bc_count']}/{required} "
                                     f"(BC={bcimax:.2f}A, Δ={bc_diff:.2f}A @ {gss})")

                            if p["bc_count"] >= required:
                                # ── CONFIRMED ─────────────────────────
                                fdr_s = p["fdr_snap"]
                                # Refine using the same FEEDER_OFF vs
                                # BC_LOADING_START comparison used in the
                                # instant-confirm path — covers the case
                                # where the candidate's own first_rise_ts
                                # (set when it entered _pending) was itself
                                # delayed relative to the true BC_LOADING_START
                                # event (e.g. due to a restart or detection gap).
                                first_rise_ts = self._get_diversion_start_ts(
                                    fac, bac, p["first_rise_ts"])
                                self._active[fac] = {
                                    "bc":           bac,
                                    "f_before":     p["f_before"],
                                    "bc_before":    p.get("bc_before", 0),
                                    "bc_after":     bcimax,
                                    "ts":           now,
                                    "first_rise_ts": first_rise_ts,
                                }
                                del self._pending[fac]
                                self._save_pending()
                                v = self._viol(VT_DIV, fdr_s, bac,
                                               p["f_before"], p.get("bc_before",0),
                                               bcimax, first_rise_ts=first_rise_ts)
                                # Embed pre-fault load data in the alert
                                v["pre_fault_imax"] = p.get("pre_fault_imax", p["f_before"])
                                v["pre_fault_iavg"] = p.get("pre_fault_iavg", 0)
                                v["pre_fault_ir"]   = p.get("pre_fault_ir", 0)
                                v["pre_fault_iy"]   = p.get("pre_fault_iy", 0)
                                v["pre_fault_ib"]   = p.get("pre_fault_ib", 0)
                                v["feeder_p75"]     = p.get("feeder_p75", 0)
                                # Update detail with pre-fault load
                                pf_imax = p.get("pre_fault_imax", p["f_before"])
                                pf_iavg = p.get("pre_fault_iavg", 0)
                                v["detail"] = (
                                    f"Feeder tripped (pre-fault: Imax={pf_imax:.1f}A, "
                                    f"Iavg={pf_iavg:.1f}A). "
                                    f"BC {bac} rose {p.get('bc_before',0):.1f}A→{bcimax:.1f}A "
                                    f"@ {fdr_s.get('Gss','')}"
                                )
                                viols.append(v)
                                log.info(f"LOAD_DIVERTED confirmed: {fac} "
                                         f"(pre-fault={pf_imax:.1f}A→0), BC {bac} "
                                         f"{p.get('bc_before',0):.1f}A→{bcimax:.1f}A "
                                         f"@ {gss} after {p['bc_count']} stable cycles "
                                         f"[diversion since {first_rise_ts[:19]}]")
                        else:
                            log.debug(f"BC pending {fac}: no change "
                                      f"(BC={bcimax:.2f}A, Δ={bc_diff:.3f}A < {self.BC_CHANGE_THR}A)")
                        self._prev[fac] = fimax
                        continue

                    # ── NEW CANDIDATE ─────────────────────────────────
                    # Conditions to enter PENDING:
                    #   Feeder is OFF AND BC is carrying load AND
                    #   BC load is plausibly from this feeder
                    candidate = False
                    f_before  = prev_f if prev_f is not None else 0.0

                    # STAGE 3 trigger (a): BC Load Raise — BC was already carrying
                    # some current and rose further, in same cycle feeder tripped
                    bc_was_idle = prev_b <= off_thr
                    bc_just_turned_on = bc_was_idle and bc_is_active

                    # EDGE: feeder just tripped this cycle
                    if (prev_f is not None and prev_f > off_thr
                            and feeder_now_off and bc_is_active):
                        # BC must have risen by ≥30% of feeder's load
                        # (covers both: BC was idle and turned ON, or BC already
                        #  had load and rose further — STAGE 3 triggers (a)/(b))
                        min_bc_rise = max(prev_f * 0.30, off_thr)
                        bc_rose = bcimax - prev_b
                        if bc_just_turned_on:
                            log.debug(f"BC trigger (b) for {fac}: "
                                      f"BC was idle ({prev_b:.2f}A) now restored/ON "
                                      f"({bcimax:.2f}A) @ {gss}")
                        if bc_rose >= min_bc_rise:
                            # Validate using pre-fault load + p75
                            p75 = self._get_feeder_p75(fac)
                            # Use max(prev_f, p75) as reference — handles partial load at trip
                            ref_edge = max(prev_f, p75) if p75 > 0 else prev_f
                            # Accept if reference load is significant
                            if ref_edge > off_thr * 3:
                                candidate = True
                                f_before  = ref_edge  # use better estimate as f_before
                                log.debug(f"BC edge candidate {fac}: "
                                          f"prev={prev_f:.1f}A p75={p75:.1f}A "
                                          f"ref={ref_edge:.1f}A BC_rise={bc_rose:.1f}A")
                            else:
                                log.debug(f"BC edge rejected {fac}: prev={prev_f:.1f}A "
                                          f"p75={p75:.1f}A ref={ref_edge:.1f}A too low")

                    # PERSISTENT: feeder is OFF (regardless of how long — could be
                    # immediate or up to hours later) AND BC is carrying load.
                    # Reference load comes from THREE sources, in priority order:
                    #   1. pre_fault_imax from active FEEDER_OFF DB record (most accurate)
                    #   2. p75 historical load (works even if FEEDER_OFF record
                    #      is missing/expired/never created — e.g. DB gaps,
                    #      manual clears, or detection started after the trip)
                    #   3. prev_f (last-seen live value, weakest fallback)
                    # As long as ANY of these shows the feeder normally carries
                    # real load, it qualifies as a candidate — we no longer
                    # hard-require the FEEDER_OFF DB record to exist.
                    elif feeder_now_off and bc_is_active:
                        # NOTE: removed the old "prev_b < bcimax * 0.5" requirement.
                        # That guard demanded BC be ACTIVELY RISING between cycles,
                        # which only holds true for 1-2 cycles right after a fresh
                        # diversion starts. Once BC settles into a steady diverted
                        # level (the normal, expected state for a long-standing
                        # diversion), prev_b ≈ bcimax permanently.
                        #
                        # BUT removing that guard exposed a different bug: if
                        # ANOTHER feeder (e.g. TELKOI) is ALREADY diverted to
                        # this same BC and its load fully explains bcimax, a
                        # completely unrelated feeder going OFF (e.g.
                        # JAGMOHANPUR) would still pass the bc_ratio check
                        # against bcimax — because bcimax/ref_load can land in
                        # 0.20-5.0 purely by coincidence, even with ZERO actual
                        # rise attributable to the new feeder. This produced a
                        # false LOAD_DIVERTED for JAGMOHANPUR while BC's level
                        # was 100% TELKOI's pre-existing diversion (BC even
                        # SLIGHTLY DROPPED: 20.7A→19.6A — no rise at all).
                        #
                        # FIX: subtract every OTHER feeder's f_before already
                        # attributed to this BC (both confirmed _active AND
                        # in-progress _pending) from bcimax FIRST. Only the
                        # UNEXPLAINED remainder is eligible to be attributed
                        # to this new candidate's ratio check.
                        other_explained = sum(
                            od.get("f_before", 0)
                            for ofac, od in self._active.items()
                            if ofac != fac and od.get("bc") == bac
                        ) + sum(
                            op.get("f_before", 0)
                            for ofac, op in self._pending.items()
                            if ofac != fac and op.get("bac") == bac
                        )
                        unexplained_bc = bcimax - other_explained

                        if unexplained_bc <= off_thr:
                            log.debug(f"BC persistent rejected {fac}: BC={bcimax:.1f}A "
                                      f"fully explained by other feeder(s) on this BC "
                                      f"({other_explained:.1f}A) — no unattributed rise "
                                      f"for this feeder")
                            self._prev[fac] = fimax
                            continue

                        pfl     = self._get_prefault_load(fac)
                        p75     = self._get_feeder_p75(fac)
                        pf_load = pfl.get("pre_fault_imax") or 0.0
                        ref_load = max(pf_load, p75, prev_f or 0)

                        if ref_load <= off_thr * 2:
                            # No source shows this feeder ever carried real load —
                            # genuinely not a candidate (truly idle feeder)
                            log.debug(f"BC persistent skipped {fac}: no load reference "
                                      f"(pfl={pf_load:.1f}A p75={p75:.1f}A prev={prev_f}) "
                                      f"— feeder appears genuinely idle")
                            self._prev[fac] = fimax
                            continue

                        # Ratio check now uses the UNEXPLAINED portion of BC,
                        # not the raw total — this is the actual fix
                        bc_ratio = unexplained_bc / ref_load if ref_load > 0 else 0
                        if 0.20 <= bc_ratio <= 5.0 and unexplained_bc > off_thr * 3:
                            candidate = True
                            f_before  = ref_load
                            log.debug(f"BC persistent candidate {fac}: "
                                      f"ref={ref_load:.1f}A (pfl={pf_load:.1f}A p75={p75:.1f}A "
                                      f"prev={prev_f}) BC_total={bcimax:.1f}A "
                                      f"other_explained={other_explained:.1f}A "
                                      f"unexplained={unexplained_bc:.1f}A ratio={bc_ratio:.2f}")
                        else:
                            log.debug(f"BC persistent rejected {fac}: "
                                      f"ref={ref_load:.1f}A p75={p75:.1f}A "
                                      f"BC_total={bcimax:.1f}A other_explained="
                                      f"{other_explained:.1f}A unexplained="
                                      f"{unexplained_bc:.1f}A ratio={bc_ratio:.2f}")

                    # NOTE: POST-RESTART case (prev_f=None) intentionally NOT handled here.
                    # When prev_f=None (first cycle after restart), we skip candidate detection.
                    # On the 2nd cycle, _prev is populated and PERSISTENT detection handles it
                    # correctly with proper f_before value and BC ratio validation.
                    # This prevents false positives from stale/bad HES data on first cycle.

                    # STAGE 3c trigger: STALE/FROZEN FEEDER + BC load increase.
                    # Covers the case where a feeder's meter keeps reporting
                    # (so it never shows OFF or "No match in live data") but its
                    # Imax/Vavg values are frozen — a comms glitch is masking a
                    # real outage. If BC rises while this feeder is frozen, treat
                    # it as a diversion candidate using the SAME validation as
                    # PERSISTENT (pre-fault/p75 ratio check), just gated on
                    # staleness instead of feeder_now_off.
                    if not candidate and is_stale_feeder and bc_is_active and not feeder_now_off:
                        pfl     = self._get_prefault_load(fac)
                        p75     = self._get_feeder_p75(fac)
                        pf_load = pfl.get("pre_fault_imax") or 0.0
                        ref_load = max(pf_load, p75, fimax)  # frozen fimax itself is a valid reference
                        if ref_load > off_thr * 2:
                            bc_ratio = bcimax / ref_load if ref_load > 0 else 0
                            if 0.20 <= bc_ratio <= 5.0 and bcimax > off_thr * 3:
                                candidate = True
                                f_before  = ref_load
                                log.info(f"BC stale-feeder candidate {fac}: meter frozen "
                                         f"at {fimax:.2f}A for "
                                         f"{self.STALE_REPEAT_REQUIRED}+ cycles, "
                                         f"BC={bcimax:.1f}A ratio={bc_ratio:.2f} @ {gss}")
                            else:
                                log.debug(f"BC stale-feeder rejected {fac}: "
                                          f"ref={ref_load:.1f}A BC={bcimax:.1f}A "
                                          f"ratio={bc_ratio:.2f}")

                    if candidate:
                        p75_val = self._get_feeder_p75(fac)
                        pfl_val = self._get_prefault_load(fac)

                        if required <= 1:
                            # ── INSTANT CONFIRMATION (single feeder OFF case) ──
                            # No need to wait for a 2nd cycle — the Load
                            # Comparison checks (BC ratio 0.20-5.0x, pre-fault/
                            # p75 reference, 30% min-rise, cross-GSS guard) have
                            # already validated this is a real match. Confirm
                            # and fire LOAD_DIVERTED right now.
                            #
                            # TRUE START TIME: compare FEEDER_OFF.first_seen vs
                            # BC_LOADING_START.first_seen, take the later of the
                            # two (diversion can't predate the trip). This is
                            # what fixes "Active Since" showing the confirmation
                            # instant ("now") instead of when the diversion
                            # actually began — which is critical for feeders
                            # that have been OFF for hours/days before detection
                            # software happened to confirm the match.
                            real_start_ts = self._get_diversion_start_ts(fac, bac, now_iso)

                            self._active[fac] = {
                                "bc":            bac,
                                "f_before":      f_before,
                                "bc_before":     prev_b,
                                "bc_after":      bcimax,
                                "ts":            now,
                                "first_rise_ts": real_start_ts,
                            }
                            v = self._viol(VT_DIV, fdr, bac, f_before, prev_b,
                                           bcimax, first_rise_ts=real_start_ts)
                            v["pre_fault_imax"] = pfl_val.get("pre_fault_imax", f_before)
                            v["pre_fault_iavg"] = pfl_val.get("pre_fault_iavg", 0)
                            v["pre_fault_ir"]   = pfl_val.get("pre_fault_ir", 0)
                            v["pre_fault_iy"]   = pfl_val.get("pre_fault_iy", 0)
                            v["pre_fault_ib"]   = pfl_val.get("pre_fault_ib", 0)
                            v["feeder_p75"]     = p75_val
                            v["detail"] = (
                                f"Feeder tripped (pre-fault: Imax={f_before:.1f}A, "
                                f"p75={p75_val:.1f}A). "
                                f"BC {bac} rose {prev_b:.1f}A→{bcimax:.1f}A "
                                f"@ {gss} — confirmed on 1st cycle (single feeder OFF). "
                                f"Diversion start: {real_start_ts[:19]}"
                            )
                            viols.append(v)
                            log.info(f"LOAD_DIVERTED confirmed (instant, 1 cycle): {fac} "
                                     f"(pre-fault={f_before:.1f}A→0), BC {bac} "
                                     f"{prev_b:.1f}A→{bcimax:.1f}A @ {gss} "
                                     f"[true start: {real_start_ts[:19]}]")
                        else:
                            self._pending[fac] = {
                                "bac":              bac,
                                "gss":              gss,
                                "fdr_snap":         dict(fdr),
                                "f_before":         f_before,
                                "bc_before":        prev_b,
                                "bc_count":         1,
                                "bc_last":          bcimax,
                                "first_rise_ts":    now_iso,
                                "first_rise_val":   bcimax,
                                "required":         required,
                                "feeder_p75":       p75_val,
                                "pre_fault_imax":   pfl_val.get("pre_fault_imax", f_before),
                                "pre_fault_iavg":   pfl_val.get("pre_fault_iavg", 0),
                                "pre_fault_ir":     pfl_val.get("pre_fault_ir", 0),
                                "pre_fault_iy":     pfl_val.get("pre_fault_iy", 0),
                                "pre_fault_ib":     pfl_val.get("pre_fault_ib", 0),
                                "via_stale_feeder": is_stale_feeder,
                            }
                            self._save_pending()
                            log.info(f"BC pending {fac}: count=1/{required} "
                                     f"(BC={bcimax:.2f}A, feeder p75={p75_val:.1f}A "
                                     f"pre-fault={f_before:.1f}A @ {gss}) — "
                                     f"waiting for {required} stable cycles "
                                     f"(2+ feeders OFF simultaneously)")

                    self._prev[fac] = fimax

                # Update BC prev
                self._prev[bac] = bcimax

        return viols

    def _get_prefault_load(self, fac: str) -> dict:
        """
        Get pre-fault load from the active FEEDER_OFF alert's extra_json.
        Returns dict with pre_fault_imax, pre_fault_iavg, or empty dict.
        This is the most accurate source — recorded at exact moment of trip.
        """
        if not self._store_ref:
            return {}
        try:
            ak_off = f"{fac}_FEEDER_OFF"
            # Query DB for active FEEDER_OFF extra_json
            with self._store_ref._conn() as c:
                row = c.execute(
                    "SELECT extra_json FROM alerts WHERE asset_key=? AND is_active=1 LIMIT 1",
                    (ak_off,)
                ).fetchone()
            if not row or not row[0]:
                return {}
            import json as _j
            d = _j.loads(row[0])
            return {
                "pre_fault_imax": float(d.get("pre_fault_imax") or 0),
                "pre_fault_iavg": float(d.get("pre_fault_iavg") or 0),
                "pre_fault_ir":   float(d.get("pre_fault_ir") or 0),
                "pre_fault_iy":   float(d.get("pre_fault_iy") or 0),
                "pre_fault_ib":   float(d.get("pre_fault_ib") or 0),
            }
        except Exception:
            return {}

    def _get_diversion_start_ts(self, fac: str, bac: str, fallback_iso: str) -> str:
        """
        Determine the TRUE diversion start time by comparing two real events:
          1. FEEDER_OFF.first_seen  — when the feeder actually tripped
          2. BC_LOADING_START.first_seen — when the BC actually started
             carrying load (its own idle→active transition, recorded
             independently of any diversion confirmation)

        Per the required Load Comparison rule: the diversion can only have
        started AFTER the feeder tripped, so the true start = the LATER of
        the two timestamps (never earlier than FEEDER_OFF, since that would
        be physically impossible — the feeder must trip before its load can
        divert anywhere).

        If either event is missing (e.g. very old data predating this
        feature, or store not wired), falls back to fallback_iso (typically
        "now" at confirmation time) — this is the legacy behaviour and only
        applies to alerts created before BC_LOADING_START existed.
        """
        if not self._store_ref:
            return fallback_iso
        try:
            off_ts = None
            bcl_ts = None
            with self._store_ref._conn() as c:
                row1 = c.execute(
                    "SELECT first_seen FROM alerts WHERE asset_key=? "
                    "ORDER BY first_seen DESC LIMIT 1",
                    (f"{fac}_FEEDER_OFF",)
                ).fetchone()
                if row1 and row1[0]:
                    off_ts = row1[0]
                row2 = c.execute(
                    "SELECT first_seen FROM alerts WHERE asset_key=? "
                    "ORDER BY first_seen DESC LIMIT 1",
                    (f"{bac}_{VT_BCL}",)
                ).fetchone()
                if row2 and row2[0]:
                    bcl_ts = row2[0]

            if off_ts and bcl_ts:
                # True start = later of the two (diversion can't predate the trip)
                return max(off_ts, bcl_ts)
            if off_ts:
                return off_ts
            if bcl_ts:
                return bcl_ts
            return fallback_iso
        except Exception as e:
            log.warning(f"_get_diversion_start_ts({fac}) failed: {e}")
            return fallback_iso

    STALE_REPEAT_REQUIRED = 3  # consecutive identical readings = stale/frozen meter
    STALE_TOLERANCE       = 0.01  # A — readings within this delta count as "same"

    def _check_stale(self, fac: str, fimax: float, vavg: float) -> bool:
        """
        Track whether a feeder's Imax/Vavg has been frozen (identical reading)
        for STALE_REPEAT_REQUIRED consecutive cycles. This catches meters that
        are still reporting (so feeder doesn't show as 'No match in live data'
        or OFF) but whose values have stopped updating — e.g. a comms/DCU glitch
        where the last good reading keeps repeating. Returns True if stale.
        """
        prev = self._stale_vals.get(fac)
        if prev is None:
            self._stale_vals[fac] = (fimax, vavg, 1)
            return False
        last_imax, last_vavg, count = prev
        same = (abs(fimax - last_imax) < self.STALE_TOLERANCE
                and abs(vavg - last_vavg) < self.STALE_TOLERANCE)
        if same:
            count += 1
            self._stale_vals[fac] = (fimax, vavg, count)
            return count >= self.STALE_REPEAT_REQUIRED
        else:
            self._stale_vals[fac] = (fimax, vavg, 1)
            return False

    def _get_feeder_p75(self, fac: str) -> float:
        """
        Get 75th percentile of feeder's recent load from feeder_hourly table.
        Returns 0.0 if not enough data.

        IMPORTANT: get_feeder_hourly_profile() lives on PeakLoadStore, NOT
        AlertStore. self._peak_store_ref must be the PeakLoadStore instance
        (wired in ViolationDetector.__init__). Using AlertStore here raises
        AttributeError on every call, silently caught below, always
        returning 0.0 — which was the actual root cause of long-standing
        diversions (BARBIL-I, TELKOI OFF for 24h+) never confirming: p75
        was always 0, pre_fault_imax was also unset, so the candidate's
        reference load was always 0 and got rejected by the idle-feeder guard.
        """
        if not self._peak_store_ref:
            log.debug(f"_get_feeder_p75({fac}): no peak_store_ref configured")
            return 0.0
        try:
            profile = self._peak_store_ref.get_feeder_hourly_profile(fac, days=3)
            if not profile:
                return 0.0
            # Filter using a meaningful noise floor (off_thr), not just >0.
            # Meter noise/leakage current (0.01-0.05A) otherwise pollutes the
            # percentile and can make p75 land in the noise floor even when
            # the feeder genuinely carries real load for part of the window
            # (e.g. TELKOI: ~19h of real 30-70A load out of 85h sampled, but
            # the remaining ~65 noise-floor hours pushed p75's index into
            # the 0.01-0.02A band — exactly hiding the real load level).
            NOISE_FLOOR = 1.0  # A — matches off_thr; readings below this
                                # are "feeder OFF", not real load, regardless
                                # of being nominally >0
            vals = [r["imax"] for r in profile if r["imax"] and r["imax"] > NOISE_FLOOR]
            if len(vals) < 3:
                # Not enough real-load samples to compute a meaningful p75
                return 0.0
            vals.sort()
            p75_idx = int(len(vals) * 0.75)
            return vals[min(p75_idx, len(vals)-1)]
        except Exception as e:
            log.warning(f"_get_feeder_p75({fac}) failed: {e}")
            return 0.0

    def get_pending_state(self) -> list:
        """Return pending BC diversion candidates for frontend blinking."""
        result = []
        for fac, p in self._pending.items():
            result.append({
                "feeder_assets":  [fac],
                "bc_asset":       p.get("bac", ""),
                "gss":            p.get("gss", ""),
                "bc_changes":     p.get("bc_count", 0),
                "required":       p.get("required", self.CONFIRM_REQUIRED),
                "first_rise_ts":  p.get("first_rise_ts", ""),
            })
        return result

    @staticmethod
    def _viol(vtype, fdr, bc_ac, f_before, bc_before, bc_after, first_rise_ts=None):
        ac  = fdr.get("AssetCode","")
        ts  = first_rise_ts or datetime.now().isoformat()
        return {
            "id":              f"{ac}_{vtype}_{int(time.time()*1000)}",
            "asset_key":       f"{ac}_{vtype}",
            "type":            vtype,
            "severity":        SEVERITY.get(vtype,"HIGH"),
            "AssetCode":       ac,
            "Circle":          fdr.get("Circle",""),
            "Division":        fdr.get("Division",""),
            "Gss":             fdr.get("Gss",""),
            "Feeder":          fdr.get("Feeder",""),
            "FeederType":      fdr.get("FeederType",""),
            "BusCouplerAsset": bc_ac,
            "FeederImaxBefore":round(f_before,2),
            "BCImaxBefore":    round(bc_before,2),
            "BCImaxAfter":     round(bc_after,2),
            "Vr":fdr.get("Vr"),"Vy":fdr.get("Vy"),"Vb":fdr.get("Vb"),
            "Ir":fdr.get("Ir"),"Iy":fdr.get("Iy"),"Ib":fdr.get("Ib"),
            "ApparentPower":   fdr.get("ApparentPower"),
            "detail":          (f"Feeder tripped ({f_before:.1f}A→0A). "
                                f"BC {bc_ac} rose {bc_before:.1f}A→{bc_after:.1f}A "
                                f"@ {fdr.get('Gss','')}"),
            "timestamp":       ts,       # = first_rise_ts so "Active Since" is correct
            "first_seen":      ts,
            "acked":           False,
            "notified_email":  False,
            "notified_wa":     False,
        }
