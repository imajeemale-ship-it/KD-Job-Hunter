"""Policy-driven follow-ups using configured contacts and SMTP, never inferred mail."""
import asyncio
import json
import re
import smtplib
from email.message import EmailMessage

from utils import autonomous as worker


def _address(value):
    return isinstance(value, str) and re.fullmatch(r"[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+", value)


def prepare_message(job, profile, stage):
    policy = profile.get("autonomous", {}).get("follow_up", {})
    metadata = json.loads(job.get("metadata") or "{}")
    recipient = policy.get("contacts", {}).get(job["id"])
    contact = metadata.get("follow_up_contact", {})
    if not recipient and isinstance(contact, dict) and contact.get("verified") is True:
        recipient = contact.get("email")
    if not _address(recipient):
        raise worker.Blocked("Follow-up contact missing. Required action: configure a verified employer email for this application in autonomous.follow_up.contacts.")
    sender = profile.get("personal", {}).get("email")
    if not _address(sender):
        raise worker.Blocked("Follow-up sender missing. Required action: configure personal.email.")
    if not job.get("title") or not job.get("company") or not job.get("applied_at"):
        raise worker.Blocked("Follow-up application facts missing. Required action: confirm role, company and application date.")
    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = f"Follow-up: {job['title']} application"
    message["Message-ID"] = f"<kd-{worker.identity(job)}-{stage}@{sender.split('@')[1]}>"
    message.set_content(
        f"Hello,\n\nI am following up on my application for {job['title']} at {job['company']}, "
        f"submitted on {job['applied_at'][:10]}. Could you share any update on its status?\n\nThank you.\n"
    )
    return message


async def smtp_send(message, profile, before_send):
    """Authenticate before the delivery fence; connection/auth errors may retry."""
    config = profile.get("autonomous", {}).get("follow_up", {}).get("smtp", {})
    if not config.get("host"):
        raise RuntimeError("Follow-up SMTP host is not configured.")

    def send():
        client_type = smtplib.SMTP_SSL if config.get("ssl", False) else smtplib.SMTP
        client = client_type(config["host"], config.get("port", 465 if config.get("ssl") else 587), timeout=30)
        try:
            if not config.get("ssl", False):
                client.starttls()
            if config.get("username"):
                client.login(config["username"], config.get("password", ""))
            before_send()
            rejected = client.send_message(message)
            return not rejected
        finally:
            # A disconnect during QUIT cannot undo an accepted DATA response.
            try:
                client.quit()
            except (smtplib.SMTPException, OSError):
                client.close()

    task = asyncio.create_task(asyncio.to_thread(send))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # Keep the worker lock while the SMTP thread finishes; a cancellation
        # cannot let a second worker race an uncompleted send.
        await task
        raise


async def execute_follow_up(item, job, profile, transport=None, load_profile=None):
    config = profile.get("autonomous", {})
    policy = config.get("follow_up", {})
    if not policy.get("enabled", False):
        return worker.transition(item, "WAITING", "Follow-up delivery disabled by policy.")
    stage = 1 if item["action"] == "follow_up" else int(item["action"].split(":")[1])
    if stage > policy.get("max_messages", 2):
        return worker.transition(item, "DONE", "Follow-up policy limit reached.")
    message = prepare_message(job, profile, stage)
    if not config.get("live_submit", False):
        return worker.transition(item, "WAITING", "Dry-run follow-up prepared; delivery disabled.")
    before_send = lambda: worker._begin_submit(item, job, profile, load_profile)
    try:
        delivered = await (transport or smtp_send)(message, profile, before_send)
    except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError) as exc:
        # Explicit SMTP rejection proves the message was not accepted. Network
        # disconnects/timeouts after DATA remain ambiguous and must not retry.
        conn = worker.db()
        conn.execute("UPDATE execution_queue SET phase='preparing' WHERE id=?", (item["id"],))
        conn.commit()
        conn.close()
        raise RuntimeError(f"SMTP rejected follow-up before acceptance: {exc}") from exc
    if not delivered:
        if worker._uncertain(item):
            raise worker.Blocked("Follow-up delivery unclear. Required action: check sent mail before retrying.")
        raise RuntimeError("Follow-up transport failed before delivery.")
    if not worker._uncertain(item):
        raise RuntimeError("Follow-up transport did not establish the delivery fence.")
    conn = worker.db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        due = worker.later(days=policy.get("interval_days", config.get("ghost_days", 14)))
        next_action = f"follow_up:{stage + 1}" if stage < policy.get("max_messages", 2) else ""
        conn.execute("UPDATE execution_queue SET state='DONE',reason='SMTP delivery accepted.',updated_at=? WHERE id=?", (worker.now(), item["id"]))
        conn.execute("UPDATE applications SET follow_up_count=COALESCE(follow_up_count,0)+1,follow_up_date=?,last_activity=? WHERE id=?", (due if next_action else None, worker.now(), job["id"]))
        worker.event(conn, job["id"], item["action"], "FOLLOWUP_SENT", "SMTP accepted delivery; employer response unknown.", item["attempts"] - 1, next_action)
        if next_action:
            worker._enqueue(conn, job["id"], next_action, due)
        conn.commit()
    finally:
        conn.close()
    return "FOLLOWUP_SENT"
