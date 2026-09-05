#!/usr/bin/env python3
"""Run daily discovery and always send KD a Telegram status summary."""

import asyncio
import re
import subprocess
import sys
from pathlib import Path

# Allow imports from the repository root when run as scripts/...
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kd_autopilot import load_profile
from utils.telegram_approval import TelegramApproval


def parse_summary(output: str) -> tuple[int, int, list[str]]:
    match = re.search(r"Results:\s*(\d+) jobs above threshold out of (\d+) scanned", output)
    strong = int(match.group(1)) if match else 0
    scanned = int(match.group(2)) if match else 0

    jobs = []
    for line in output.splitlines():
        clean = line.strip()
        if clean.startswith("🎯 "):
            jobs.append(clean[2:].strip())
    return strong, scanned, jobs[:5]


async def send_summary(profile: dict, exit_code: int, output: str) -> None:
    telegram = TelegramApproval(profile)
    if not telegram.configured:
        print("Telegram not configured; morning summary not sent.")
        return

    if exit_code != 0:
        tail = "\n".join(output.splitlines()[-8:])[-1200:]
        text = (
            "KD JOB HUNTER — MORNING SEARCH\n\n"
            f"Search failed (exit code {exit_code}).\n"
            "The approval worker may still be running, but discovery needs attention."
        )
        if tail:
            text += f"\n\nLast output:\n{tail}"
        await telegram.send_status(text)
        return

    strong, scanned, jobs = parse_summary(output)
    lines = [
        "KD JOB HUNTER — MORNING SEARCH",
        "",
        f"Scanned: {scanned}",
        f"Strong matches: {strong}",
    ]
    if jobs:
        lines += ["", "Top matches:"]
        lines.extend(f"• {job}" for job in jobs)
    elif strong == 0:
        lines += ["", "No new jobs cleared the match threshold today."]

    lines += [
        "",
        "Direct-ATS matches will arrive separately as YES/NO approval cards.",
        "Strong matches from Indeed/LinkedIn will still appear here instead of disappearing silently.",
    ]
    await telegram.send_status("\n".join(lines))


def main() -> int:
    profile = load_profile(str(ROOT / "profile.yaml"))
    proc = subprocess.run(
        [str(ROOT / ".venv/bin/python"), str(ROOT / "main.py"), "discover"],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    output = proc.stdout or ""
    print(output, end="")

    try:
        asyncio.run(send_summary(profile, proc.returncode, output))
    except Exception as exc:
        print(f"Morning Telegram summary failed: {exc}")
        return proc.returncode or 1

    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
