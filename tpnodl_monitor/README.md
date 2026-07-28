# TPNODL Realtime Load & Voltage Monitor
## Complete System — Deployment Guide

---

## Project Structure

```
tpnodl_monitor/
├── app.py                  ← Flask server (entry point)
├── requirements.txt
├── install_service.py      ← Windows Service installer
│
├── modules/
│   ├── config.py           ← Config manager (data/config.json)
│   ├── scraper.py          ← Probus HES scraper (login + fetch)
│   ├── violation.py        ← Violation detection engine
│   ├── email_mgr.py        ← SMTP email notifications
│   ├── whatsapp_mgr.py     ← WhatsApp (Twilio / Meta API)
│   ├── feeder_master.py    ← Feeder CRUD (data/feeder_master.json)
│   ├── alert_store.py      ← Alert log (data/alerts.json)
│   └── routes.py           ← All Flask REST API routes
│
├── static/
│   ├── index.html          ← Frontend HMI (served by Flask)
│   └── api.js              ← Frontend ↔ Backend API bridge
│
├── data/                   ← Auto-created at runtime
│   ├── config.json
│   ├── feeder_master.json
│   ├── alerts.json
│   ├── live_data.json
│   └── load_history.json
│
└── logs/
    └── monitor.log
```

---

## 1. Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Run development server
python app.py
# → Open http://localhost:5000
```

---

## 2. First-Time Setup in HMI

1. **Settings → Probus Login**: Enter username/password → Save & Test Connection
2. **Settings → Voltage Thresholds**: Set Vn=33 kV, OV=+6%, UV=-9%
3. **Settings → Email**: Enter SMTP credentials + recipient emails → Save → Send Test
4. **Settings → WhatsApp**: Choose provider, enter credentials + phone numbers → Save → Test
5. **Feeder Master → Import from Live Data**: Click to auto-import all live meters
6. **Feeder Master**: Set correct `FeederRating (A)` for each meter (critical for OL/SLD/SLR detection)

---

## 3. Violation Types

| Code | Name | Trigger Condition |
|------|------|-------------------|
| OV | Over Voltage | Vavg > Vn × (1 + OV%) |
| UV | Under Voltage | Vavg < Vn × (1 − UV%) |
| OL | Overload | Imax > Rating × LoadThr% |
| FEEDER_OFF | Feeder OFF | Imax was > 1A, now < 1A (sudden drop to zero) |
| SUDDEN_LOAD_DROP | Sudden Drop | Imax drops > 20% from rolling baseline AND exceeds 2σ normal variance |
| SUDDEN_LOAD_RAISE | Sudden Raise | Imax raises > 20% from rolling baseline AND exceeds 2σ normal variance |

### Sudden Load Change Detection Algorithm
- Rolling window of last 6 samples per feeder stored in `data/load_history.json`
- Baseline = mean of window; Normal band = max(2σ, 5% of baseline)
- A change is flagged ONLY when:
  - % change > configured threshold (default 20%)
  - AND absolute Δ exceeds the normal variance band
  - This avoids false positives during morning/evening demand ramps

---

## 4. Email Setup (Gmail)

1. Enable 2-Step Verification on Gmail account
2. Go to Google Account → Security → App Passwords
3. Generate App Password for "Mail"
4. Use that 16-character password in HMI settings (NOT your Gmail password)

**Settings:**
- SMTP Host: `smtp.gmail.com`
- Port: `587`
- TLS: `STARTTLS`

---

## 5. WhatsApp Setup

### Option A — Twilio Sandbox (Testing)
1. Create Twilio account at https://twilio.com
2. Go to Messaging → Try WhatsApp
3. Follow sandbox join instructions (send "join <keyword>" to sandbox number)
4. Note Account SID, Auth Token, and sandbox number (+14155238886)

### Option B — Meta Business API (Production)
1. Create Meta Business account
2. Set up WhatsApp Business API at https://developers.facebook.com
3. Get Phone Number ID and Permanent Access Token
4. Recipients must message your number first (opt-in required by Meta)

### Option C — wa.me Link
- No API credentials needed
- Click "Test" to open WhatsApp web with pre-filled message
- Recipient must click send — not fully automated

---

## 6. Windows Service (Run 24×7)

```bash
# Install pywin32 first
pip install pywin32

# Run as Administrator
python install_service.py install
python install_service.py start

# Check status in Windows Services (services.msc)
# Service name: TPNODLMonitor
```

### Without pywin32 — use NSSM
1. Download NSSM from https://nssm.cc
2. Add to PATH
3. Run `python install_service.py install` as Administrator

---

## 7. REST API Reference

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | /api/status | System health check |
| POST | /api/fetch | Trigger manual fetch |
| GET | /api/live | Live enriched data with violation flags |
| GET | /api/alerts | Alert log (params: limit, unacked=1, type, circle) |
| POST | /api/alerts/{id}/ack | Acknowledge alert |
| POST | /api/alerts/ack-all | Acknowledge all |
| POST | /api/alerts/clear | Clear all alerts |
| GET | /api/feeders | All feeder master entries |
| POST | /api/feeders | Add feeder |
| PUT | /api/feeders/{idx} | Update feeder |
| DELETE | /api/feeders/{idx} | Delete feeder |
| POST | /api/feeders/import | Bulk import |
| GET | /api/config | Get config (passwords masked) |
| POST | /api/config/{section} | Update config section |
| POST | /api/email/test | Send test email |
| POST | /api/whatsapp/test | Send test WhatsApp |
| GET | /api/logs | Last 200 server log lines |

---

## 8. Network Requirements

The server machine needs outbound HTTPS access to:
- `tpnodl.probussense.com` — Probus HES
- `api.twilio.com` — if using Twilio WhatsApp
- `graph.facebook.com` — if using Meta WhatsApp API
- `smtp.gmail.com:587` — if using Gmail SMTP

---

## 9. Troubleshooting

| Issue | Check |
|-------|-------|
| Login fails | Verify username/password in Settings. Check Probus HES is accessible from this machine. |
| No violations raised | Feeder master must have AssetCode filled and FeederRating set. Run "Import from Live Data" first. |
| Email not sending | Use Gmail App Password (not regular password). Check firewall allows TCP 587. |
| WhatsApp not sending (Twilio) | Recipients must have joined the sandbox. |
| Sudden load violations too noisy | Increase `sudden_drop_pct` / `sudden_raise_pct` in Voltage settings (e.g. 30%). Increase `trend_window_samples` (e.g. 12). |

---

## 10. Data Files

All data files are plain JSON — can be edited manually if needed:
- `data/config.json` — all settings
- `data/feeder_master.json` — feeder ratings + AssetCode mapping
- `data/alerts.json` — alert history (last 500)
- `data/live_data.json` — latest live snapshot
- `data/load_history.json` — per-feeder rolling Imax history for SLD/SLR detection
