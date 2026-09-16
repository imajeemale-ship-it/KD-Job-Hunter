"""Persistent execution using the existing tracker, scheduler and form adapter.

The OS lock serializes workers (and is released on crash). The durable submit
fence survives crashes; only failures before that fence may be retried.
"""

import asyncio
import fcntl
import hashlib
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

from utils import tracker


class Blocked(Exception):
    """An exact applicant answer or human action is required."""


class Paused(Exception):
    pass


class DailyCap(Exception):
    pass


STATES = ("BACKLOG", "READY", "IN_PROGRESS", "WAITING", "NEEDS_KD", "DONE", "FAILED_RETRY", "FAILED")
SUCCESS = ("application submitted", "application received", "successfully submitted", "thanks for applying", "thank you for applying", "you have applied", "we received your application")
CLOSED = ("applied", "interviewing", "offer", "rejected", "withdrawn", "archived", "ignored")


def now():
    return datetime.now(timezone.utc).isoformat()


def later(days=0, seconds=0):
    return (datetime.now(timezone.utc) + timedelta(days=days, seconds=seconds)).isoformat()


def db():
    conn = tracker.get_db()
    conn.execute("""CREATE TABLE IF NOT EXISTS execution_queue (
        id INTEGER PRIMARY KEY, job_id TEXT NOT NULL, action TEXT NOT NULL,
        state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
        due_at TEXT NOT NULL, reason TEXT DEFAULT '', updated_at TEXT NOT NULL,
        UNIQUE(job_id, action))""")
    columns = {r[1] for r in conn.execute("PRAGMA table_info(execution_queue)")}
    # Legacy IN_PROGRESS rows have unknown submission state: preserve the fence.
    if "phase" not in columns:
        conn.execute("ALTER TABLE execution_queue ADD COLUMN phase TEXT NOT NULL DEFAULT 'preparing'")
        conn.execute("UPDATE execution_queue SET phase='submitting' WHERE state='IN_PROGRESS'")
    conn.execute("""CREATE TABLE IF NOT EXISTS execution_events (
        id INTEGER PRIMARY KEY, timestamp TEXT NOT NULL, job_id TEXT NOT NULL,
        action TEXT NOT NULL, outcome TEXT NOT NULL, reason TEXT DEFAULT '',
        retry_count INTEGER NOT NULL, next_action TEXT DEFAULT '')""")
    conn.execute("""CREATE TABLE IF NOT EXISTS submission_fences (
        identity TEXT PRIMARY KEY, job_id TEXT NOT NULL, outcome TEXT NOT NULL,
        created_at TEXT NOT NULL)""")
    for row in conn.execute("""SELECT a.* FROM applications a JOIN execution_queue q ON q.job_id=a.id
        WHERE q.action='apply' AND (q.phase='submitting' OR
        (q.state='NEEDS_KD' AND (q.reason LIKE '%confirmation%' OR q.reason LIKE '%Worker stopped%' OR q.reason LIKE '%Submit may%')))""").fetchall():
        conn.execute("INSERT OR IGNORE INTO submission_fences VALUES (?,?,?,?)", (identity(dict(row)), row["id"], "SUBMITTING", now()))
    conn.commit()
    return conn


def event(conn, job_id, action, outcome, reason="", retries=0, next_action=""):
    conn.execute("INSERT INTO execution_events (timestamp,job_id,action,outcome,reason,retry_count,next_action) VALUES (?,?,?,?,?,?,?)",
                 (now(), job_id, action, outcome, reason, retries, next_action))


def threshold(profile):
    return profile.get("autonomous", {}).get("min_score", profile.get("preferences", {}).get("min_match_score", 65))


def identity(job):
    """Canonical application URL, ignoring only tracking parameters."""
    url = job.get("apply_url") or job.get("url") or ""
    parts = urlsplit(url)
    query = sorted((k, v) for k, v in parse_qsl(parts.query) if not k.lower().startswith("utm_") and k.lower() not in ("source", "ref", "referrer", "gh_src"))
    host = parts.netloc.lower()
    path = parts.path.rstrip("/")
    if host in ("boards.greenhouse.io", "job-boards.greenhouse.io"):
        host = "job-boards.greenhouse.io"
    if host in ("jobs.lever.co", "jobs.eu.lever.co", "jobs.ashbyhq.com"):
        path = re.sub(r"/(apply|application)$", "", path)
    scheme = "https" if parts.scheme.lower() in ("http", "https") else parts.scheme.lower()
    canonical = urlunsplit((scheme, host, path, urlencode(query), ""))
    return hashlib.sha256((canonical or "job:" + job["id"]).encode()).hexdigest()


def _enqueue(conn, job_id, action="apply", due_at=None):
    cur = conn.execute("INSERT OR IGNORE INTO execution_queue(job_id,action,state,due_at,updated_at) VALUES (?,?,?,?,?)",
                       (job_id, action, "READY", due_at or now(), now()))
    if cur.rowcount:
        event(conn, job_id, action, "READY", next_action=action)
    return bool(cur.rowcount)


def enqueue(job_id, action="apply", due_at=None):
    conn = db()
    try:
        row = conn.execute("SELECT status,applied_at FROM applications WHERE id=?", (job_id,)).fetchone()
        if not row or (action == "apply" and (row["status"] in CLOSED or row["applied_at"])):
            return False
        result = _enqueue(conn, job_id, action, due_at)
        conn.commit()
        return result
    finally:
        conn.close()


def queue_qualified(profile):
    conn = db()
    try:
        rows = conn.execute("SELECT id FROM applications WHERE status='matched' AND match_score>=? AND applied_at IS NULL", (threshold(profile),)).fetchall()
        if profile.get("autonomous", {}).get("live_submit", False):
            conn.execute("UPDATE execution_queue SET state='READY',due_at=?,updated_at=? WHERE state='WAITING' AND reason LIKE 'Dry-run%'", (now(), now()))
        if profile.get("autonomous", {}).get("follow_up", {}).get("enabled", False):
            conn.execute("UPDATE execution_queue SET state='READY',due_at=?,updated_at=? WHERE state='WAITING' AND action LIKE 'follow_up%' AND reason='Follow-up delivery disabled by policy.'", (now(), now()))
        result = sum(_enqueue(conn, row["id"]) for row in rows)
        conn.commit()
        return result
    finally:
        conn.close()


def claim(max_attempts=3, apply_allowed=True):
    """Called only while holding the worker lock; abandoned work is now safe to recover."""
    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        for stuck in conn.execute("SELECT * FROM execution_queue WHERE state='IN_PROGRESS'").fetchall():
            uncertain = stuck["phase"] == "submitting"
            state = "NEEDS_KD" if uncertain else ("FAILED" if stuck["attempts"] >= max_attempts else "FAILED_RETRY")
            reason = "Worker stopped after submission began; check employer portal/email before retrying." if uncertain else "Worker interrupted before submission; safe to retry."
            conn.execute("UPDATE execution_queue SET state=?,reason=?,due_at=?,updated_at=? WHERE id=?", (state, reason, later(seconds=60), now(), stuck["id"]))
            event(conn, stuck["job_id"], stuck["action"], state, reason, stuck["attempts"], "verify" if uncertain else "retry")
        row = conn.execute("SELECT * FROM execution_queue WHERE state IN ('READY','FAILED_RETRY') AND due_at<=? AND (action!='apply' OR ?) ORDER BY due_at,id LIMIT 1", (now(), apply_allowed)).fetchone()
        if not row:
            conn.commit()
            return None
        conn.execute("UPDATE execution_queue SET state='IN_PROGRESS',phase='preparing',updated_at=? WHERE id=?", (now(), row["id"]))
        event(conn, row["job_id"], row["action"], "IN_PROGRESS", retries=row["attempts"])
        conn.commit()
        return dict(row)
    finally:
        conn.close()


def transition(item, state, reason="", max_attempts=3, next_action=""):
    assert state in STATES
    conn = db()
    try:
        if state == "FAILED_RETRY" and item["attempts"] >= max_attempts:
            state = "FAILED"
            reason = f"Technical retry limit reached: {reason}"
        due = later(seconds=min(3600, 60 * 2 ** max(0, item["attempts"] - 1))) if state == "FAILED_RETRY" else now()
        conn.execute("UPDATE execution_queue SET state=?,reason=?,due_at=?,updated_at=? WHERE id=?", (state, reason, due, now(), item["id"]))
        event(conn, item["job_id"], item["action"], state, reason, item["attempts"], next_action)
        conn.commit()
    finally:
        conn.close()
    return state


def verified(page_text, dry_run=False):
    """Recognize affirmative confirmation sentences, not instructions/questions."""
    if dry_run:
        return False
    for match in re.finditer(r"[^.!?\n]+[.!?\n]?", page_text.lower()):
        sentence = re.sub(r"\s+", " ", match.group()).strip()
        if sentence.endswith("?") or re.search(r"\b(not|never|failed|error|unable|if|retry)\b", sentence):
            continue
        if any(re.match(re.escape(phrase) + r"(?:[.! ,]|$)", sentence) for phrase in SUCCESS):
            return True
    return False


def blocker(question, profile):
    """Only call on actual questions/controls, never posting or body text."""
    from utils.answers import trusted_answer
    lower = question.lower()
    for marker in profile.get("autonomous", {}).get("approval_required", []):
        if marker.lower() in lower:
            return f"Question: {question}. Approval required: provide the exact answer or complete this action."
    for pattern, action in (
        (r"captcha|verification code|one.time (?:code|password)|multi.factor|\bmfa\b", "Complete verification/CAPTCHA/MFA"),
        (r"\b(attest|certify|certification|assessment)\b", "Review and complete this attestation or assessment"),
    ):
        if re.search(pattern, lower) and trusted_answer(question, profile) is None:
            return f"Question: {question}. Required action: {action}."
    return None


def today_verified(conn):
    # Historical applied_at remains valid after interview/offer/rejection changes.
    return conn.execute("""SELECT COUNT(DISTINCT id) FROM (
        SELECT job_id AS id FROM execution_events WHERE action='apply' AND outcome='VERIFIED' AND DATE(timestamp)=DATE('now')
        UNION SELECT id FROM applications WHERE applied_at IS NOT NULL AND status!='failed' AND DATE(applied_at)=DATE('now')
    )""").fetchone()[0]


def metrics(min_score=None, profile=None):
    if min_score is None:
        if profile is None:
            from scheduler import get_profile
            profile = get_profile() or {}
        min_score = threshold(profile)
    conn = db()
    try:
        def scalar(query, args=()):
            return conn.execute(query, args).fetchone()[0]
        return dict(
            jobs_discovered=scalar("SELECT COUNT(*) FROM applications"),
            jobs_qualified=scalar("SELECT COUNT(*) FROM applications WHERE match_score>=?", (min_score,)),
            applications_attempted=scalar("SELECT COUNT(*) FROM execution_events WHERE action='apply' AND outcome='ATTEMPTED'"),
            applications_verified_submitted=scalar("SELECT COUNT(DISTINCT job_id) FROM execution_events WHERE action='apply' AND outcome='VERIFIED'"),
            applications_failed=scalar("SELECT COUNT(*) FROM execution_queue WHERE action='apply' AND state='FAILED'"),
            needs_kd=scalar("SELECT COUNT(*) FROM execution_queue WHERE state='NEEDS_KD'"),
            followups_sent=scalar("SELECT COUNT(*) FROM execution_events WHERE action LIKE 'follow_up%' AND outcome='FOLLOWUP_SENT'"),
            interviews_detected=scalar("SELECT COUNT(*) FROM application_status_events WHERE status='interviewing'")
                if scalar("SELECT COUNT(*) FROM sqlite_master WHERE name='application_status_events'") else scalar("SELECT COUNT(*) FROM applications WHERE status='interviewing'"),
        )
    finally:
        conn.close()


def _attempt(item):
    item["attempts"] += 1
    conn = db()
    conn.execute("UPDATE execution_queue SET attempts=?,updated_at=? WHERE id=?", (item["attempts"], now(), item["id"]))
    event(conn, item["job_id"], item["action"], "STARTED", retries=item["attempts"] - 1)
    conn.commit()
    conn.close()


def _uncertain(item):
    conn = db()
    row = conn.execute("SELECT phase FROM execution_queue WHERE id=?", (item["id"],)).fetchone()
    conn.close()
    return row and row["phase"] == "submitting"


def _begin_submit(item, job, profile, load_profile=None, resolved_url=None, continuation=False):
    current = load_profile() if load_profile else profile
    if not current or current.get("autonomous", {}).get("paused", False) or not current.get("autonomous", {}).get("enabled", True) or not current.get("autonomous", {}).get("live_submit", False):
        raise Paused("Submission disabled by current configuration.")
    config = current.get("autonomous", {})
    if item["action"].startswith("follow_up") and not config.get("follow_up", {}).get("enabled", False):
        raise Paused("Follow-up delivery disabled by current configuration.")
    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if item["action"] == "apply":
            current_job = conn.execute("SELECT status,applied_at,match_score FROM applications WHERE id=?", (job["id"],)).fetchone()
            if not current_job or current_job["status"] != "matched" or current_job["applied_at"] or (current_job["match_score"] or 0) < threshold(current):
                raise Blocked("Application eligibility/status changed during preparation; verify the tracked application before retrying.")
            if today_verified(conn) >= config.get("daily_cap", current.get("rate_limits", {}).get("max_applications_per_day", 25)):
                raise DailyCap()
            keys = {identity(job)}
            if resolved_url:
                keys.add(identity(dict(job, apply_url=resolved_url)))
            for key in keys:
                existing = conn.execute("SELECT job_id FROM submission_fences WHERE identity=?", (key,)).fetchone()
                if existing and not (continuation and existing["job_id"] == job["id"]):
                    raise Blocked("Submission already started for this application URL; check employer portal/email. Do not resubmit.")
            for key in keys:
                conn.execute("INSERT OR IGNORE INTO submission_fences VALUES (?,?,?,?)", (key, job["id"], "SUBMITTING", now()))
        else:
            current_job = conn.execute("SELECT status FROM applications WHERE id=?", (job["id"],)).fetchone()
            if not current_job or current_job["status"] != "applied":
                raise Paused("Application status changed before follow-up delivery.")
        conn.execute("UPDATE execution_queue SET phase='submitting',updated_at=? WHERE id=?", (now(), item["id"]))
        event(conn, item["job_id"], item["action"], "SUBMITTING", retries=item["attempts"] - 1, next_action="verify")
        conn.commit()
    finally:
        conn.close()


def _submit_guard(item, job, profile, load_profile):
    # Only this live attempt may advance its own already-fenced wizard. A fresh
    # attempt always encounters the durable fence before opening a browser.
    started = False
    def before_submit(url=None):
        nonlocal started
        _begin_submit(item, job, profile, load_profile, url, continuation=started)
        started = True
    return before_submit


def _finish_verified(item, config):
    """Log success, queue follow-up and finish the item in one transaction."""
    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        due = later(days=config.get("follow_up_days", 7))
        conn.execute("UPDATE applications SET status='applied',applied_at=?,last_activity=?,follow_up_date=? WHERE id=?", (now(), now(), due, item["job_id"]))
        conn.execute("UPDATE execution_queue SET state='DONE',reason='Post-submit confirmation verified.',updated_at=? WHERE id=?", (now(), item["id"]))
        conn.execute("UPDATE submission_fences SET outcome='VERIFIED' WHERE job_id=?", (item["job_id"],))
        event(conn, item["job_id"], "apply", "VERIFIED", retries=item["attempts"] - 1, next_action="follow_up")
        _enqueue(conn, item["job_id"], "follow_up", due)
        conn.commit()
    finally:
        conn.close()
    tracker._emit("job_applied", {"id": item["job_id"], "success": True})
    return "VERIFIED"


async def _browser_apply(job, profile, dry_run, before_submit):
    from playwright.async_api import async_playwright
    from adapters.stagehand_adapter import apply_smart
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            success = await apply_smart(page, job["apply_url"] or job["url"], profile,
                None, cover_letter=job.get("cover_letter") or "",
                dry_run=dry_run, platform=job.get("platform") or "", company=job.get("company") or "",
                title=job.get("title") or "", description=job.get("description") or "", before_submit=before_submit)
            # The strict adapter verifies the post-click frame, including iframe forms.
            return success, getattr(page, "_kd_confirmation_text", "") if success and not dry_run else ""
        finally:
            await browser.close()


async def cycle(profile, apply=None, follow_up=None, load_profile=None, tailor=None):
    """One bounded unit of work. Tests inject transports; production uses browser/SMTP."""
    if profile.get("autonomous", {}).get("paused", False):
        return "PAUSED"
    lock_path = Path(str(tracker.DB_PATH) + ".worker.lock")
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return "BUSY"
        try:
            return await _cycle(profile, apply, follow_up, load_profile, tailor)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


async def _cycle(profile, apply, follow_up, load_profile, tailor):
    config = profile.get("autonomous", {})
    queue_qualified(profile)
    if config.get("queue_ghosts", True):
        for ghost in tracker.get_ghost_alerts(days=config.get("ghost_days", 14)):
            # INSERT OR IGNORE is safe for old jobs; subsequent stages are queued
            # atomically with confirmed delivery, never inferred from silence.
            enqueue(ghost["id"], "follow_up")
    conn = db()
    cap_reached = today_verified(conn) >= config.get("daily_cap", profile.get("rate_limits", {}).get("max_applications_per_day", 25))
    conn.close()
    item = claim(config.get("max_attempts", 3), apply_allowed=not cap_reached)
    if not item:
        return "DAILY_CAP" if cap_reached else "IDLE"
    conn = db()
    row = conn.execute("SELECT * FROM applications WHERE id=?", (item["job_id"],)).fetchone()
    conn.close()
    if not row:
        return transition(item, "FAILED", "Job record missing; operational queue error.")
    job = dict(row)
    if item["action"] == "apply":
        if job["status"] in CLOSED or job.get("applied_at"):
            return transition(item, "DONE", "Already submitted or closed.")
        if job["status"] != "matched" or (job.get("match_score") or 0) < threshold(profile):
            return transition(item, "WAITING", "Job no longer qualifies at the configured threshold.")
        conn = db()
        duplicate = conn.execute("SELECT 1 FROM submission_fences WHERE identity=? OR job_id=?", (identity(job), job["id"])).fetchone()
        # Include older tracker submissions and ambiguous queue rows predating fences.
        for other in conn.execute("SELECT * FROM applications WHERE id!=? AND applied_at IS NOT NULL AND status!='failed'", (job["id"],)):
            duplicate = duplicate or identity(dict(other)) == identity(job)
        conn.close()
        if duplicate:
            return transition(item, "NEEDS_KD", "Existing submission or unresolved submit fence; verify employer portal/email before any retry.")
    else:
        if job["status"] != "applied":
            return transition(item, "DONE", "Employer response/status change; stop no-response follow-ups.")
    _attempt(item)
    dry_run = not config.get("live_submit", False)
    try:
        if item["action"].startswith("follow_up"):
            from utils.follow_up import execute_follow_up
            return await execute_follow_up(item, job, profile, follow_up, load_profile)
        if tailor is not None:
            job = await tailor(job, profile)
        elif apply is None:
            from utils.resume_tailor import prepare_application
            job = await asyncio.to_thread(prepare_application, job, profile)
        conn = db()
        event(conn, item["job_id"], "tailor", "READY", "Application content prepared from trusted data.", next_action="apply")
        conn.commit()
        conn.close()
        if load_profile:
            latest = load_profile()
            if not latest or latest.get("autonomous", {}).get("paused", False):
                raise Paused()
        conn = db()
        event(conn, item["job_id"], "apply", "ATTEMPTED", retries=item["attempts"] - 1)
        conn.commit()
        conn.close()
        guard = _submit_guard(item, job, profile, load_profile)
        if apply is None:
            success, page_text = await _browser_apply(job, profile, dry_run,
                guard)
        else:
            success, page_text = await apply(job, dry_run,
                before_submit=guard)
        if dry_run and success:
            return transition(item, "WAITING", "Dry-run form filled; live submission disabled.")
        if success and _uncertain(item) and verified(page_text, dry_run):
            return _finish_verified(item, config)
        if _uncertain(item):
            return transition(item, "NEEDS_KD", "Submission may have occurred; check employer portal/email. Do not resubmit.")
        return transition(item, "FAILED_RETRY", "Form failed before submission.", config.get("max_attempts", 3))
    except Paused:
        return transition(item, "READY", "Execution paused before submission.")
    except DailyCap:
        return transition(item, "READY", "Daily verified-application cap reached.")
    except Blocked as exc:
        return transition(item, "NEEDS_KD", str(exc))
    except Exception as exc:
        if _uncertain(item):
            return transition(item, "NEEDS_KD", f"Delivery/submission may have occurred: {exc}. Verify portal/email before retrying.")
        return transition(item, "FAILED_RETRY", str(exc), config.get("max_attempts", 3))
