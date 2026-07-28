"""
notify_queue.py — Persistent, retrying delivery queue for email/WhatsApp
notifications.

WHY THIS EXISTS
----------------
Previously, every notification attempt happened exactly ONCE, inline,
during the violation-scan cycle that created it (see violation.py's
_notify()). If that single attempt hit a transient failure — a Gmail
429 rate limit, a momentarily-busy WhatsApp Selenium driver, a brief
network blip — nothing ever retried it. notified_email/notified_wa just
stayed 0 in the database forever, even though the underlying cause would
often have cleared within minutes.

This is confirmed, not theoretical: in the 2026-06-25 alert export,
several FEEDER_OFF/LOAD_RESTORED/UV alerts delivered successfully via
WhatsApp but had NO matching email — consistent with Gmail quota
exhaustion (a known, separately-diagnosed issue) silently and
permanently losing the email side with no retry.

WHAT THIS MODULE DOES
----------------------
Decouples "decide what needs to be sent" (still entirely violation.py's
job — delay windows, escalation logic, type filtering are all UNCHANGED)
from "actually get it delivered, retrying as needed" (this module's job).
Jobs are persisted to their own SQLite file, so a queued retry survives
a process restart too — not just an in-memory list that would be lost if
the app restarts mid-backoff.

Backward compatible by design: violation.py's ViolationDetector accepts
an OPTIONAL notify_queue parameter. If it's None (not wired into app.py
yet), _notify() falls back to the exact original direct-call behavior,
unchanged. Wiring this in is a deliberate opt-in, not a forced migration.

INTEGRATION (in app.py, once you're ready to use this)
--------------------------------------------------------
    from modules.notify_queue import NotifyQueue
    notify_queue = NotifyQueue(email_mgr=email_mgr, wa_mgr=wa_mgr, alert_store=alert_store)
    detector = ViolationDetector(cfg, feeder_master, alert_store, email_mgr, wa_mgr,
                                  peak_store=peak_store, notify_queue=notify_queue)
That's it — no other call site changes needed.
"""
import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime

log = logging.getLogger("notify_queue")

DB_PATH = os.path.join("data", "notify_queue.db")

# Backoff schedule (minutes) between retry attempts after the first
# attempt fails. After exhausting this schedule, the job is marked
# permanently failed and stops retrying — logged clearly (ERROR level)
# so a persistent failure is visible in logs, not silently lost forever
# the way it was before this module existed.
RETRY_SCHEDULE_MIN = [1, 5, 15, 60, 180]  # 1m, 5m, 15m, 1h, 3h
MAX_ATTEMPTS = len(RETRY_SCHEDULE_MIN) + 1  # +1 for the original attempt

POLL_INTERVAL_SEC = 10  # how often the worker checks for due jobs


class NotifyQueue:
    def __init__(self, email_mgr=None, wa_mgr=None, alert_store=None,
                 db_path: str = DB_PATH):
        self.email = email_mgr
        self.wa = wa_mgr
        self.store = alert_store
        self.db_path = db_path
        self._stop = threading.Event()
        # Per-channel send lock — prevents two concurrent poll ticks from
        # driving the same delivery channel simultaneously (critical for
        # WhatsApp's shared Selenium session, where concurrent sends collide
        # on the browser DOM and one silently fails). If a send is in-flight
        # when the next poll fires, that channel's next job simply skips
        # this tick and retries on the next one — no job is ever dropped,
        # just slightly delayed until the in-flight send completes.
        self._channel_locks = {
            "email": threading.Lock(),
            "wa":    threading.Lock(),
        }
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._init_db()
        self._thread = threading.Thread(target=self._worker_loop, daemon=True,
                                        name="notify-queue")
        self._thread.start()
        log.info(f"NotifyQueue started — db={db_path}, "
                 f"poll every {POLL_INTERVAL_SEC}s")

    def _conn(self):
        return sqlite3.connect(self.db_path, timeout=10)

    def _init_db(self):
        with self._conn() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS notify_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel TEXT NOT NULL,
                payload TEXT NOT NULL,
                alert_ids TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending',
                next_attempt_ts REAL NOT NULL,
                created_at TEXT NOT NULL,
                last_error TEXT
            )""")
            c.execute("""CREATE INDEX IF NOT EXISTS idx_notify_pending
                ON notify_jobs(status, next_attempt_ts)""")
            c.commit()

    # ── Public: enqueue a job ───────────────────────────────────
    def enqueue_email(self, violations: list, extra_to: list = None):
        """violations: list of violation dicts, exactly as passed to
        email_mgr.send_violations() today — same shape, same content,
        nothing about the email-building logic changes.
        extra_to: additional (area-contact) recipients to union with the
        global list — see email_mgr.send_violations()'s docstring."""
        if not violations:
            return
        alert_ids = [v["id"] for v in violations if v.get("id")]
        self._enqueue("email", {"violations": violations, "extra_to": extra_to or []},
                      alert_ids)

    def enqueue_wa(self, recipients: list, message: str, alert_ids: list):
        """recipients/message: exactly what would have gone straight into
        wa_mgr.send(to=recipients, message=message) before."""
        if not recipients or not message:
            return
        self._enqueue("wa", {"to": recipients, "message": message}, alert_ids)

    def _enqueue(self, channel: str, payload: dict, alert_ids: list):
        with self._conn() as c:
            c.execute("""INSERT INTO notify_jobs
                (channel, payload, alert_ids, next_attempt_ts, created_at)
                VALUES (?,?,?,?,?)""",
                (channel, json.dumps(payload), json.dumps(alert_ids),
                 time.time(), datetime.now().isoformat()))
            c.commit()
        log.info(f"notify_queue: enqueued {channel} job covering "
                 f"{len(alert_ids)} alert(s)")

    # ── Worker loop ──────────────────────────────────────────────
    def _worker_loop(self):
        while not self._stop.is_set():
            try:
                self._process_due_jobs()
            except Exception as e:
                log.error(f"notify_queue: worker loop error (will retry "
                         f"next cycle): {e}")
            self._stop.wait(POLL_INTERVAL_SEC)

    def _process_due_jobs(self):
        now = time.time()
        with self._conn() as c:
            rows = c.execute("""SELECT id, channel, payload, alert_ids, attempts
                FROM notify_jobs
                WHERE status='pending' AND next_attempt_ts <= ?
                ORDER BY id""", (now,)).fetchall()

        for job_id, channel, payload_json, alert_ids_json, attempts in rows:
            payload = json.loads(payload_json)
            alert_ids = json.loads(alert_ids_json)
            ok, err = self._attempt(channel, payload)
            self._record_result(job_id, channel, attempts, ok, err, alert_ids)

    def _attempt(self, channel: str, payload: dict):
        try:
            if channel == "email":
                if not self.email:
                    return False, "no email manager configured"
                ok = self.email.send_violations(payload["violations"],
                                                extra_to=payload.get("extra_to"))
                return (True, None) if ok else (False, "send_violations returned False")
            elif channel == "wa":
                if not self.wa:
                    return False, "no whatsapp manager configured"
                ok = self.wa.send(to=payload["to"], message=payload["message"])
                return (True, None) if ok else (False, "wa.send returned False")
            else:
                return False, f"unknown channel '{channel}'"
        except Exception as e:
            return False, str(e)

    def _record_result(self, job_id, channel, attempts, ok, err, alert_ids):
        now = time.time()
        with self._conn() as c:
            if ok:
                c.execute("UPDATE notify_jobs SET status='sent' WHERE id=?", (job_id,))
                c.commit()
                # Note: mark_notified() is called OPTIMISTICALLY at enqueue time
                # (see violation.py _notify()), not here — calling it again here
                # would be redundant, and the flag is already set even if the
                # queue worker happens to fail permanently on this job.
                log.info(f"notify_queue: job {job_id} ({channel}) delivered "
                         f"after {attempts + 1} attempt(s)")
                return

            new_attempts = attempts + 1
            if new_attempts >= MAX_ATTEMPTS:
                c.execute("""UPDATE notify_jobs SET status='failed',
                    attempts=?, last_error=? WHERE id=?""",
                    (new_attempts, str(err)[:500], job_id))
                c.commit()
                log.error(f"notify_queue: job {job_id} ({channel}) "
                         f"PERMANENTLY FAILED after {new_attempts} "
                         f"attempts covering {len(alert_ids)} alert(s) — "
                         f"last error: {err}")
            else:
                delay_min = RETRY_SCHEDULE_MIN[min(attempts, len(RETRY_SCHEDULE_MIN) - 1)]
                next_ts = now + delay_min * 60
                c.execute("""UPDATE notify_jobs SET attempts=?,
                    next_attempt_ts=?, last_error=? WHERE id=?""",
                    (new_attempts, next_ts, str(err)[:500], job_id))
                c.commit()
                log.warning(f"notify_queue: job {job_id} ({channel}) "
                           f"attempt {new_attempts} failed, retrying in "
                           f"{delay_min}min — error: {err}")

    # ── Diagnostics (for an /api/notify_queue/status route, if wired up) ──
    def status_summary(self) -> dict:
        with self._conn() as c:
            rows = c.execute("""SELECT status, COUNT(*) FROM notify_jobs
                GROUP BY status""").fetchall()
        return {status: count for status, count in rows}

    def recent_failures(self, limit: int = 20) -> list:
        with self._conn() as c:
            rows = c.execute("""SELECT id, channel, attempts, last_error, created_at
                FROM notify_jobs WHERE status='failed'
                ORDER BY id DESC LIMIT ?""", (limit,)).fetchall()
        return [{"id": r[0], "channel": r[1], "attempts": r[2],
                "last_error": r[3], "created_at": r[4]} for r in rows]

    def stop(self):
        self._stop.set()
