"""
modules/weather_report.py — Circle-wise Weather Summary for Management Report
================================================================================
Thin wrapper around mbscada_scraper.py's get_circle_weather_now() for the
management report's HTML table. Rewritten alongside the v6 scraper
(block-data API) — see mbscada_scraper.py's own docstring for the full
data-source/aggregation/stale-detection design.

DESIGN NOTE — kept separate from mgmt_report.py on purpose:
This module owns only the HTML rendering for the weather table.
mgmt_report.py calls get_circle_weather_rows() and just renders whatever
comes back — if mbscada_scraper.py is unavailable, misconfigured, or no
weather stations are configured yet, this module degrades to returning an
empty dict, and the report simply omits the weather table rather than
crashing the entire management report generation.
"""

import logging

log = logging.getLogger("weather_report")

try:
    import modules.mbscada_scraper as _mb
    _MBSCADA_AVAILABLE = True
except ImportError:
    _MBSCADA_AVAILABLE = False
    log.warning("mbscada_scraper module not found — weather table will be "
                "omitted from management reports.")


def get_circle_weather_rows(as_of=None) -> dict:
    """
    Public entry point for mgmt_report.py. Returns the live aggregated
    per-circle weather (see mbscada_scraper.get_circle_weather_now() for
    the exact shape), or {} if unavailable/unconfigured.

    as_of: the report's NOMINAL slot time (e.g. "2026-06-23 01:00:00").
    When given, this does a FRESH, slot-aligned fetch — the report must
    show data for the exact 1-hour window ending at its own slot, not
    whatever the dashboard's background-poll cache happens to hold at
    the moment the report happens to run (those are two different
    things: the cache reflects "right now", a report reflects "the hour
    ending at its slot"). When omitted (dashboard/general use), prefers
    the in-memory cache (populated by the background loop every
    POLL_INTERVAL_SEC) to avoid a live API call on every single caller —
    falls back to a live fetch only if the cache is empty.
    """
    if not _MBSCADA_AVAILABLE:
        log.warning("get_circle_weather_rows: mbscada_scraper not available — returning empty")
        return {}
    try:
        if as_of is not None:
            log.info(f"get_circle_weather_rows: slot-aligned fetch for as_of={as_of}")
            return _mb.get_circle_weather_now(as_of=as_of)

        cached = _mb.get_cache()
        if cached.get("ok") and cached.get("circle_weather"):
            log.info(f"get_circle_weather_rows: using cache, {len(cached['circle_weather'])} circle(s): "
                     f"{list(cached['circle_weather'].keys())}")
            return cached["circle_weather"]
        # Cache empty — try a direct live fetch as a fallback (e.g. right
        # after startup before the background loop's first cycle completes)
        log.info("get_circle_weather_rows: cache empty/not ok, attempting live fetch")
        live = _mb.get_circle_weather_now()
        log.info(f"get_circle_weather_rows: live fetch returned {len(live)} circle(s): "
                 f"{list(live.keys())}")
        return live
    except Exception as e:
        log.error(f"get_circle_weather_rows() failed: {e}")
        return {}


def get_weather_anomalies() -> list:
    """Pass-through for the dashboard's on-screen notification — see
    mbscada_scraper.get_weather_anomalies(). Never used by the
    management report, which intentionally omits these remarks."""
    if not _MBSCADA_AVAILABLE:
        return []
    try:
        return _mb.get_weather_anomalies()
    except Exception as e:
        log.error(f"get_weather_anomalies() failed: {e}")
        return []


def build_weather_html_table(circle_weather: dict, known_circles: list) -> str:
    """
    Build the HTML table fragment for the management report, matching the
    visual style of the existing Circle-wise Demand table. Circles with no
    weather data show "—" rather than being omitted, so the table's circle
    ordering always matches the Demand table above it.

    Renders only the weather data itself (avg temp/humidity, rainfall,
    max wind, and which station(s) cover the circle) — sensor-fault /
    non-communicating remarks are intentionally NOT shown here. Those are
    a live operational concern for the dashboard's on-screen notification
    (see mbscada_scraper.get_weather_anomalies()), not something that
    belongs in a report management circulates — a stale management report
    shouldn't be cluttered with caveats that may already be stale
    themselves by the time someone reads the email.
    """
    if not circle_weather and not known_circles:
        return ""

    # CASE-NORMALIZATION FIX: circle_weather's keys come from the weather
    # station config (whatever case the operator picked in the dropdown,
    # e.g. "Balasore"), while known_circles comes from the LIVE Probus
    # demand data's raw "Circle" field, which is consistently ALL CAPS
    # (e.g. "BALASORE") in this deployment. A direct dict lookup between
    # two different-case strings silently returns {} for every circle —
    # exactly the "all dashes despite valid data" bug. Build a
    # case-insensitive lookup map instead of relying on exact string match.
    circle_weather_ci = {k.strip().upper(): v for k, v in circle_weather.items()}

    rows = ""
    for circ in sorted(known_circles):
        wx = circle_weather_ci.get(circ.strip().upper(), {})
        t    = wx.get("avg_temp_c")
        rh   = wx.get("avg_humidity")
        rain = wx.get("total_rain_mm")
        ws   = wx.get("max_wind_kmph")
        stations = wx.get("stations", [])

        t_str    = f"{t:.1f}"    if t    is not None else "—"
        rh_str   = f"{rh:.1f}"   if rh   is not None else "—"
        rain_str = f"{rain:.2f}" if rain is not None else "—"
        ws_str   = f"{ws:.1f}"   if ws   is not None else "—"

        station_html = ""
        if stations:
            station_html = (f'<br><span style="color:#999;font-size:9px">'
                            f'via {", ".join(stations)}</span>')

        rows += f"""
<tr>
  <td style="padding:10px 12px;border-bottom:1px solid #e8ecf0;font-weight:600">{circ}{station_html}</td>
  <td style="padding:10px 12px;border-bottom:1px solid #e8ecf0;text-align:right;font-family:monospace">{t_str}</td>
  <td style="padding:10px 12px;border-bottom:1px solid #e8ecf0;text-align:right;font-family:monospace">{rh_str}</td>
  <td style="padding:10px 12px;border-bottom:1px solid #e8ecf0;text-align:right;font-family:monospace">{rain_str}</td>
  <td style="padding:10px 12px;border-bottom:1px solid #e8ecf0;text-align:right;font-family:monospace">{ws_str}</td>
</tr>"""

    return f"""
<tr><td style="background:#fff;padding:0">
  <div style="background:#f8f9fa;padding:10px 18px;font-weight:700;font-size:15px;border-bottom:2px solid #dee2e6">
    🌦️ Circle-wise Weather (Last 1 Hour)
  </div>
  <table width="100%" cellpadding="0" cellspacing="0" style="font-size:13px">
    <thead><tr style="background:#e9ecef">
      <th style="padding:10px 12px;text-align:left;color:#555">Circle</th>
      <th style="padding:10px 12px;text-align:right;color:#555">Avg Temp (°C)</th>
      <th style="padding:10px 12px;text-align:right;color:#555">Avg Humidity (%)</th>
      <th style="padding:10px 12px;text-align:right;color:#555">Total Rainfall (mm)</th>
      <th style="padding:10px 12px;text-align:right;color:#555">Max Wind Speed (km/h)</th>
    </tr></thead>
    <tbody>{rows}</tbody>
  </table>
  <div style="padding:6px 18px;font-size:10.5px;color:#999;background:#fafbfc">
    Source: MB SCADA Cloud PSS weather stations. Circles without a covering
    weather station show "—".
  </div>
</td></tr>"""
