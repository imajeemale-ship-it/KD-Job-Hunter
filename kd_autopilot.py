#!/usr/bin/env python3
"""KD Job Hunter approval worker.

Workflow:
1. MR.Jobs discovers and scores opportunities.
2. This worker finds high-scoring matched jobs.
3. It generates tailored application content.
4. It sends KD a Telegram YES/NO approval card.
5. YES submits the application; NO skips it.

The worker never stores Telegram credentials in git. It reads them from env vars.
"""

import argparse
import asyncio
import json
import yaml
from pathlib import Path

from playwright.async_api import async_playwright

from adapters.stagehand_adapter import apply_smart
from utils.brain import ClaudeBrain
from utils.kd_approval_state import (
    create_waiting,
    get_approved,
    get_by_job,
    get_by_nonce,
    mark_failed,
    mark_submitted,
    set_decision,
)
from utils.resume_parser import extract_resume_text
from utils.resume_tailor import tailor_resume
from utils.telegram_approval import TelegramApproval
from utils.tracker import (
    get_job_by_id,
    get_jobs_by_status,
    get_tailored_resume,
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
    brain = ClaudeBrain(verbose=False, profile=profile)
    candidates = [
        j for j in get_jobs_by_status("matched")
        if int(j.get("match_score") or 0) >= min_score and not get_by_job(j["id"])
    ]
    candidates.sort(key=lambda j: int(j.get("match_score") or 0), reverse=True)

    queued = 0
    for job in candidates[:max_packets]:
        description = job.get("description") or ""
        tailored = tailor_resume(description, base_resume_text, profile, brain=brain)
        update_tailored_resume(job["id"], tailored)

        nonce, message_id = await telegram.send_job_for_approval(
            job,
            tailored_summary=tailored.get("tailored_summary", ""),
        )
        create_waiting(job["id"], nonce, message_id)
        queued += 1
        print(f"Queued for KD approval: {job['title']} @ {job['company']} ({job['match_score']})")

    return queued


async def submit_job(profile: dict, job_id: str) -> bool:
    job = get_job_by_id(job_id)
    if not job:
        raise RuntimeError(f"Job not found: {job_id}")

    rate_limits = profile.get("rate_limits", {})
    approval_cfg = profile.get("approval", {})
    headless = bool(approval_cfg.get("headless_submission", True))

    tailored = get_tailored_resume(job_id) or {}
    cover_letter = (
        tailored.get("tailored_cover_letter")
        or job.get("cover_letter")
        or ""
    )

    brain = ClaudeBrain(verbose=True, profile=profile)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless, slow_mo=50)
        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080}
        )
        page = await context.new_page()
        try:
            success = await apply_smart(
                page,
                job.get("apply_url") or job.get("url"),
                profile,
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

    log_applied(job_id, bool(success))
    if success:
        mark_submitted(job_id)
        print(f"Submitted: {job['title']} @ {job['company']}")
    else:
        mark_failed(job_id, "Application adapter returned false")
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
        set_decision(job_id, approved)

        if not approved:
            job = get_job_by_id(job_id) or {}
            log_skipped(job_id, "Declined by KD")
            await telegram.acknowledge(
                decision.get("callback_query_id", ""), "Skipped."
            )
            print(f"KD declined: {job.get('title')} @ {job.get('company')}")
            processed += 1
            continue

        await telegram.acknowledge(
            decision.get("callback_query_id", ""), "Approved. Submitting now."
        )
        try:
            await submit_job(profile, job_id)
        except Exception as exc:
            mark_failed(job_id, str(exc))
            print(f"Submission failed for {job_id}: {exc}")
        processed += 1

    return processed


async def submit_pending_approved(profile: dict) -> int:
    """Retry jobs already approved but not yet successfully submitted."""
    count = 0
    for approval in get_approved():
        try:
            await submit_job(profile, approval["job_id"])
        except Exception as exc:
            mark_failed(approval["job_id"], str(exc))
            print(f"Submission retry failed for {approval['job_id']}: {exc}")
        count += 1
    return count


async def run_once(profile: dict) -> None:
    await queue_high_matches(profile)
    await process_telegram_decisions(profile)
    await submit_pending_approved(profile)


async def run_forever(profile: dict) -> None:
    interval = int(profile.get("approval", {}).get("poll_interval_seconds", 30))
    while True:
        try:
            await run_once(profile)
        except Exception as exc:
            print(f"KD approval worker error: {exc}")
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
