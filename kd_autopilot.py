#!/usr/bin/env python3
"""KD Job Hunter approval worker.

Workflow:
1. MR.Jobs discovers and scores opportunities.
2. This worker finds high-scoring matched jobs.
3. It generates a truthful tailored ATS PDF and cover letter.
4. It sends KD a Telegram YES/NO approval card.
5. YES submits with the tailored PDF; NO skips it.
"""

import argparse
import asyncio
import copy
import yaml
from pathlib import Path

from playwright.async_api import async_playwright

from adapters.stagehand_adapter import apply_smart
from utils.brain import ClaudeBrain
from utils.live_safety import is_greenhouse_live_url
from adapters.greenhouse import GreenhouseSubmissionError
from utils.kd_approval_state import (
    create_waiting,
    claim_submission,
    get_approved,
    get_by_job,
    get_by_nonce,
    mark_failed,
    mark_submitted,
    set_decision,
)
from utils.resume_parser import extract_resume_text
from utils.resume_tailor import tailor_resume
from utils.resume_pdf import render_tailored_resume_pdf
from utils.telegram_approval import TelegramApproval
from utils.tracker import (
    get_job_by_id,
    get_jobs_by_status,
    get_tailored_resume,
    get_today_count,
    log_applied,
    log_skipped,
    update_tailored_resume,
)


def load_profile(path: str = "profile.yaml") -> dict:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(
            "profile.yaml not found. Copy profile.kd.example.yaml to profile.yaml "
            "and fill in the private values locally."
        )
    with p.open() as f:
        return yaml.safe_load(f)


async def queue_high_matches(profile: dict) -> int:
    approval_cfg = profile.get("approval", {})
    min_score = int(
        approval_cfg.get(
            "min_score",
            profile.get("preferences", {}).get("min_match_score", 82),
        )
    )
    max_packets = int(approval_cfg.get("max_packets_per_cycle", 5))
    telegram = TelegramApproval(profile)
    telegram.require_configured()

    base_resume_text = extract_resume_text(profile.get("resume_path", ""))
    if not base_resume_text:
        raise RuntimeError("Master resume could not be read; refusing to generate applications.")

    brain = ClaudeBrain(verbose=False, profile=profile)

    candidates = [
        j for j in get_jobs_by_status("matched")
        if int(j.get("match_score") or 0) >= min_score
        and not get_by_job(j["id"])
        and is_greenhouse_live_url(j.get("apply_url") or j.get("url") or "")
    ]
    candidates.sort(key=lambda j: int(j.get("match_score") or 0), reverse=True)

    queued = 0
    for job in candidates[:max_packets]:
        description = job.get("description") or ""
        tailored = tailor_resume(description, base_resume_text, profile, brain=brain)
        if tailored.get("error"):
            print(f"Tailoring warning for {job['id']}: {tailored['error']}")

        tailored_pdf = render_tailored_resume_pdf(
            tailored,
            profile,
            job,
            base_resume_text=base_resume_text,
        )
        tailored["generated_pdf_path"] = tailored_pdf
        update_tailored_resume(job["id"], tailored)

        nonce, message_id = await telegram.send_job_for_approval(
            job,
            tailored_resume_path=tailored_pdf,
            tailored_summary=tailored.get("tailored_summary", ""),
        )
        create_waiting(job["id"], nonce, message_id)
        queued += 1
        print(
            f"Queued for KD approval: {job['title']} @ {job['company']} "
            f"({job['match_score']}) — {tailored_pdf}"
        )

    return queued


async def submit_job(profile: dict, job_id: str) -> bool:
    if not claim_submission(job_id, int(profile.get("rate_limits", {}).get("max_applications_per_day", 5))):
        return False
    job = get_job_by_id(job_id) or {}
    label = f"{job.get('title', 'Job')} @ {job.get('company', 'Unknown company')}"
    telegram = TelegramApproval(profile)

    async def notify(text):
        try:
            await telegram.send_status(text)
        except Exception:
            # Transport exceptions can include bot credentials. Never print them.
            print(f"Telegram status delivery failed for {job_id}", flush=True)

    await notify(f"APPROVED — SUBMITTING\n\n{label}\n\nWorking on the application now...")
    try:
        success = await _submit_claimed_job(profile, job_id)
    except Exception as exc:
        # Adapter errors are controlled messages; other exceptions may contain secrets.
        reason = str(exc) if isinstance(exc, GreenhouseSubmissionError) else "Submission stopped unexpectedly; outcome unknown, review worker state before retry"
        mark_failed(job_id, reason)
        success = (get_by_job(job_id) or {}).get("status") == "submitted"
    if success:
        await notify(f"SUBMITTED ✅\n\n{label}\n\nApplication submitted successfully.")
    else:
        reason = (get_by_job(job_id) or {}).get("error") or "Submission could not be completed"
        await notify(f"SUBMISSION FAILED ❌\n\n{label}\n\nReason: {reason}")
    return success


async def _submit_claimed_job(profile: dict, job_id: str) -> bool:
    job = get_job_by_id(job_id)
    if not job:
        raise RuntimeError(f"Job not found: {job_id}")

    rate_limits = profile.get("rate_limits", {})
    max_per_day = min(5, int(rate_limits.get("max_applications_per_day", 5)))
    if get_today_count() >= max_per_day:
        raise GreenhouseSubmissionError(f"Daily application limit reached ({max_per_day}); no submit clicked")

    approval_cfg = profile.get("approval", {})
    headless = bool(approval_cfg.get("headless_submission", True))
    tailored = get_tailored_resume(job_id) or {}
    tailored_pdf = tailored.get("generated_pdf_path", "")
    if not tailored_pdf or not Path(tailored_pdf).exists():
        raise GreenhouseSubmissionError("Tailored resume PDF is missing; no submit clicked")

    cover_letter = tailored.get("tailored_cover_letter") or job.get("cover_letter") or ""

    # Give the adapter a copy of the profile whose resume path points to the
    # job-specific generated PDF. The private master resume is never modified.
    submit_profile = copy.deepcopy(profile)
    submit_profile["resume_path"] = tailored_pdf

    apply_url = job.get("apply_url") or job.get("url") or ""
    if not is_greenhouse_live_url(apply_url):
        reason = "Live autonomous submission blocked: unsupported ATS hostname"
        mark_failed(job_id, reason)
        print(reason)
        return False

    brain = ClaudeBrain(verbose=True, profile=submit_profile)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless, slow_mo=50)
        context = await browser.new_context(viewport={"width": 1920, "height": 1080})
        page = await context.new_page()
        try:
            success = await apply_smart(
                page,
                job.get("apply_url") or job.get("url"),
                submit_profile,
                brain,
                cover_letter=cover_letter,
                dry_run=False,
                platform=job.get("platform", ""),
                company=job.get("company", ""),
                title=job.get("title", ""),
                description=job.get("description", ""),
            )
        finally:
            await browser.close()

    if success:
        mark_submitted(job_id)
        log_applied(job_id, True)
        print(f"Submitted: {job['title']} @ {job['company']}")
    else:
        mark_failed(job_id, "Submission outcome unknown: adapter returned no confirmation; review before retry")
    return bool(success)


async def process_telegram_decisions(profile: dict) -> int:
    telegram = TelegramApproval(profile)
    telegram.require_configured()
    decisions = await telegram.poll_decisions(timeout=2)
    processed = 0

    for decision in decisions:
        approval = get_by_nonce(decision["nonce"])
        if not approval or approval.get("status") != "waiting":
            await telegram.acknowledge(
                decision.get("callback_query_id", ""),
                "That decision is no longer active.",
            )
            continue

        job_id = approval["job_id"]
        approved = bool(decision["approved"])
        if not set_decision(job_id, approved):
            continue

        if not approved:
            job = get_job_by_id(job_id) or {}
            log_skipped(job_id, "Declined by KD")
            await telegram.acknowledge(decision.get("callback_query_id", ""), "Skipped.")
            print(f"KD declined: {job.get('title')} @ {job.get('company')}")
            processed += 1
            continue

        await telegram.acknowledge(
            decision.get("callback_query_id", ""), "Approved. Submitting now."
        )

        await submit_job(profile, job_id)
        processed += 1

    return processed


async def submit_pending_approved(profile: dict) -> int:
    """Retry jobs already approved but not yet successfully submitted."""
    count = 0
    for approval in get_approved():
        await submit_job(profile, approval["job_id"])
        count += 1
    return count


async def run_once(profile: dict) -> None:
    await process_telegram_decisions(profile)
    await submit_pending_approved(profile)
    await queue_high_matches(profile)


async def run_forever(profile: dict) -> None:
    print("KD approval worker started: Greenhouse-only live submissions, daily cap 5", flush=True)
    interval = int(profile.get("approval", {}).get("poll_interval_seconds", 30))
    while True:
        try:
            await run_once(profile)
        except Exception as exc:
            print(f"KD approval worker error ({type(exc).__name__}); inspect local configuration", flush=True)
        await asyncio.sleep(max(interval, 10))


def main() -> None:
    parser = argparse.ArgumentParser(description="KD Job Hunter Telegram approval worker")
    parser.add_argument("--once", action="store_true", help="Run one queue/decision cycle and exit")
    args = parser.parse_args()

    profile = load_profile()
    if args.once:
        asyncio.run(run_once(profile))
    else:
        asyncio.run(run_forever(profile))


if __name__ == "__main__":
    main()
