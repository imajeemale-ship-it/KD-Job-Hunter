"""Persistent, conservative application execution loop."""

import asyncio
import re
import sqlite3
from datetime import datetime, timedelta, timezone

from utils import tracker

class Blocked(Exception):
    pass


STATES = ("BACKLOG", "READY", "IN_PROGRESS", "WAITING", "NEEDS_KD", "DONE", "FAILED_RETRY")
SUCCESS = ("application submitted", "application received", "successfully submitted", "thanks for applying", "thank you for applying", "you have applied", "we received your application")
BLOCKERS = {
    "compensation": "Provide the exact compensation expectation or range.",
    "salary expectation": "Provide the exact compensation expectation or range.",
    "attest": "Review and answer the attestation.",
    "certify": "Review and answer the certification.",
    "legal": "Review and answer the legal question.",
    "captcha": "Complete the CAPTCHA.",
    "verification code": "Complete identity verification.",
    "multi-factor": "Complete MFA.",
    "assessment": "Complete the assessment.",
}


def now():
    return datetime.now(timezone.utc).isoformat()


def db():
    conn = tracker.get_db()
    conn.execute("""CREATE TABLE IF NOT EXISTS execution_queue (
        id INTEGER PRIMARY KEY, job_id TEXT NOT NULL, action TEXT NOT NULL,
        state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
        due_at TEXT NOT NULL, reason TEXT DEFAULT '', updated_at TEXT NOT NULL,
        UNIQUE(job_id, action))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS execution_events (
        id INTEGER PRIMARY KEY, timestamp TEXT NOT NULL, job_id TEXT NOT NULL,
        action TEXT NOT NULL, outcome TEXT NOT NULL, reason TEXT DEFAULT '',
        retry_count INTEGER NOT NULL, next_action TEXT DEFAULT '')""")
    conn.commit()
    return conn


def event(conn, job_id, action, outcome, reason="", retries=0, next_action=""):
    conn.execute("INSERT INTO execution_events (timestamp,job_id,action,outcome,reason,retry_count,next_action) VALUES (?,?,?,?,?,?,?)",
                 (now(), job_id, action, outcome, reason, retries, next_action))


def enqueue(job_id, action="apply", due_at=None):
    conn = db()
    try:
        row = conn.execute("SELECT status FROM applications WHERE id=?", (job_id,)).fetchone()
        if not row or (action == "apply" and row["status"] in ("applied", "interviewing", "offer", "rejected", "withdrawn")):
            return False
        cur = conn.execute("INSERT OR IGNORE INTO execution_queue(job_id,action,state,due_at,updated_at) VALUES (?,?,?,?,?)",
                           (job_id, action, "READY", due_at or now(), now()))
        if cur.rowcount:
            event(conn, job_id, action, "READY", next_action=action)
        conn.commit()
        return bool(cur.rowcount)
    finally:
        conn.close()


def queue_qualified(profile):
    threshold = profile.get("autonomous", {}).get("min_score", profile.get("preferences", {}).get("min_match_score", 65))
    conn = db()
    rows = conn.execute("SELECT id FROM applications WHERE status='matched' AND match_score>=?", (threshold,)).fetchall()
    if profile.get("autonomous", {}).get("live_submit", False):
        conn.execute("UPDATE execution_queue SET state='READY',due_at=?,updated_at=? WHERE state='WAITING' AND action='apply' AND reason LIKE 'Dry-run%'", (now(), now()))
        conn.commit()
    conn.close()
    return sum(enqueue(row["id"]) for row in rows)


def claim():
    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        stale = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        for stuck in conn.execute("SELECT * FROM execution_queue WHERE state='IN_PROGRESS' AND updated_at<?", (stale,)).fetchall():
            conn.execute("UPDATE execution_queue SET state='NEEDS_KD',reason=?,updated_at=? WHERE id=?",
                         ("Worker stopped during an attempt; check portal or email before retrying.", now(), stuck["id"]))
            event(conn, stuck["job_id"], stuck["action"], "NEEDS_KD", "Worker stopped during an attempt; check portal or email before retrying.", stuck["attempts"])
        row = conn.execute("SELECT * FROM execution_queue WHERE state IN ('READY','FAILED_RETRY') AND due_at<=? ORDER BY due_at,id LIMIT 1", (now(),)).fetchone()
        if not row:
            conn.commit()
            return None
        conn.execute("UPDATE execution_queue SET state='IN_PROGRESS',updated_at=? WHERE id=?", (now(), row["id"]))
        event(conn, row["job_id"], row["action"], "IN_PROGRESS", retries=row["attempts"])
        conn.commit()
        return dict(row)
    finally:
        conn.close()


def transition(item, state, reason="", max_attempts=3, next_action=""):
    assert state in STATES
    conn = db()
    try:
        attempts = item["attempts"] + (state == "FAILED_RETRY")
        if state == "FAILED_RETRY" and attempts >= max_attempts:
            state = "NEEDS_KD"
            reason = f"Retry limit reached: {reason}. Review application before retrying."
        due = (datetime.now(timezone.utc) + timedelta(seconds=min(3600, 60 * 2 ** max(0, attempts - 1)))).isoformat() if state == "FAILED_RETRY" else now()
        conn.execute("UPDATE execution_queue SET state=?,attempts=?,reason=?,due_at=?,updated_at=? WHERE id=?",
                     (state, attempts, reason, due, now(), item["id"]))
        event(conn, item["job_id"], item["action"], state, reason, attempts, next_action)
        conn.commit()
    finally:
        conn.close()
    return state


def verified(page_text, dry_run=False):
    text = re.sub(r"\s+", " ", page_text.lower())
    return not dry_run and any(phrase in text for phrase in SUCCESS)


def blocker(text, profile):
    lower = text.lower()
    for marker in profile.get("autonomous", {}).get("approval_required", []):
        if marker.lower() in lower:
            return f"Approval required for '{marker}': provide the exact answer or action."
    for marker, answer in BLOCKERS.items():
        if marker in lower:
            return answer
    return None


def metrics(min_score=65):
    conn = db()
    try:
        counts = {r["outcome"]: r["n"] for r in conn.execute("SELECT outcome,COUNT(*) n FROM execution_events GROUP BY outcome")}
        jobs = conn.execute("SELECT COUNT(*) n FROM applications").fetchone()["n"]
        qualified = conn.execute("SELECT COUNT(*) n FROM applications WHERE match_score IS NOT NULL AND match_score>=?", (min_score,)).fetchone()["n"]
        return dict(jobs_discovered=jobs, jobs_qualified=qualified,
                    applications_attempted=conn.execute("SELECT COUNT(*) n FROM execution_events WHERE outcome='IN_PROGRESS' AND action='apply'").fetchone()["n"],
                    applications_verified_submitted=counts.get("VERIFIED", 0),
                    applications_failed=counts.get("FAILED_RETRY", 0),
                    needs_kd=counts.get("NEEDS_KD", 0),
                    followups_sent=counts.get("FOLLOWUP_SENT", 0),
                    interviews_detected=conn.execute("SELECT COUNT(*) n FROM applications WHERE status='interviewing'").fetchone()["n"])
    finally:
        conn.close()


async def cycle(profile, apply=None):
    """Run one bounded cycle. Inject apply for tests; production uses Playwright."""
    config = profile.get("autonomous", {})
    if config.get("paused", False):
        return "PAUSED"
    queue_qualified(profile)
    # Existing tracker ghost detection drives a follow-up action without sending mail.
    if config.get("queue_ghosts", True):
        for ghost in tracker.get_ghost_alerts(days=config.get("ghost_days", 14)):
            enqueue(ghost["id"], "follow_up")
    cap = config.get("daily_cap", profile.get("rate_limits", {}).get("max_applications_per_day", 25))
    if tracker.get_today_count() >= cap:
        return "DAILY_CAP"
    item = claim()
    if not item:
        return "IDLE"
    conn = db()
    job = conn.execute("SELECT * FROM applications WHERE id=?", (item["job_id"],)).fetchone()
    conn.close()
    if not job:
        return transition(item, "NEEDS_KD", "Job record missing; restore or remove queue item.")
    job = dict(job)
    if item["action"] == "follow_up":
        return transition(item, "NEEDS_KD", "Review and send a follow-up to the employer; record the outcome.")
    if job["status"] in ("applied", "interviewing", "offer", "rejected", "withdrawn"):
        return transition(item, "DONE", "Already submitted or closed.")
    reason = blocker(job.get("description") or "", {"autonomous": {"approval_required": config.get("approval_required", [])}})
    if reason:
        return transition(item, "NEEDS_KD", reason)
    dry_run = not config.get("live_submit", False)
    if apply is None:
        async def apply(job, dry_run):
            from playwright.async_api import async_playwright
            from utils.brain import ClaudeBrain
            from adapters.stagehand_adapter import apply_smart
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                try:
                    page = await browser.new_page()
                    await page.goto(job["apply_url"] or job["url"], wait_until="domcontentloaded", timeout=30000)
                    preflight = await page.locator("body").inner_text(timeout=5000)
                    reason = blocker(preflight, profile)
                    if reason:
                        raise Blocked(reason)
                    success = await apply_smart(page, job["apply_url"] or job["url"], profile,
                        ClaudeBrain(verbose=False, profile=profile), cover_letter=job.get("cover_letter") or "",
                        dry_run=dry_run, platform=job.get("platform") or "", company=job.get("company") or "",
                        title=job.get("title") or "", description=job.get("description") or "")
                    return success, await page.locator("body").inner_text(timeout=5000)
                finally:
                    await browser.close()
    try:
        success, page_text = await apply(job, dry_run)
        if dry_run:
            return transition(item, "WAITING", "Dry-run form filled; live submission disabled. Enable live_submit after review.")
        if success and verified(page_text):
            tracker.log_applied(item["job_id"], True)
            transition(item, "DONE", "Post-submit confirmation verified.", next_action="follow_up")
            conn = db()
            event(conn, item["job_id"], "apply", "VERIFIED", next_action="follow_up")
            conn.commit(); conn.close()
            enqueue(item["job_id"], "follow_up", (datetime.now(timezone.utc) + timedelta(days=config.get("follow_up_days", 7))).isoformat())
            return "VERIFIED"
        if success:
            return transition(item, "NEEDS_KD", "Submit may have occurred but confirmation is unclear; check employer portal or email before retrying.")
        return transition(item, "FAILED_RETRY", "Form submission failed before confirmation.", config.get("max_attempts", 3))
    except Blocked as exc:
        return transition(item, "NEEDS_KD", str(exc))
    except Exception as exc:
        return transition(item, "FAILED_RETRY", str(exc), config.get("max_attempts", 3))
