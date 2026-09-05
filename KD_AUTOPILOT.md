# KD Job Hunter — Autopilot Mode

This fork is configured around one rule: **KD makes the final submission decision.**

## Intended workflow

1. MR.Jobs discovers openings from its existing sources, including JobSpy, Greenhouse, Lever, custom career pages, and other configured feeds.
2. MR.Jobs scores each role against KD's private master resume.
3. Jobs below the configured threshold are ignored.
4. High-match jobs are tailored using the existing `utils/resume_tailor.py` logic.
5. `kd_autopilot.py` sends the opportunity to Telegram with **YES — Submit** and **NO — Skip** buttons.
6. **NO** marks the opportunity skipped.
7. **YES** runs the existing browser application adapter in live mode and records the result in the application tracker.

## Safety / privacy

This repository is public. Never commit:

- `profile.yaml`
- resume PDFs
- Telegram bot tokens
- Telegram chat IDs
- email credentials
- API keys
- browser session data

The upstream `.gitignore` already excludes `profile.yaml`, `resumes/`, databases, logs, and screenshots.

## Private runtime setup

Copy the KD template locally:

```bash
cp profile.kd.example.yaml profile.yaml
mkdir -p resumes
```

Fill in the private fields in `profile.yaml`, then put the master resume at:

```text
resumes/master_resume.pdf
```

Set Telegram credentials only in the runtime environment:

```bash
export KD_JOB_HUNTER_TELEGRAM_BOT_TOKEN="..."
export KD_JOB_HUNTER_TELEGRAM_CHAT_ID="..."
```

## Run the existing MR.Jobs scheduler

The dashboard/server starts the existing discovery and scoring scheduler:

```bash
python3 main.py server --port 8080
```

With the KD template, discovery runs every 6 hours and scoring every 30 minutes.

## Run the approval worker

In a second process:

```bash
python3 kd_autopilot.py
```

For a one-time test cycle:

```bash
python3 kd_autopilot.py --once
```

## Default KD rules

- Minimum match score: **82/100**
- Maximum approval cards per cycle: **5**
- Maximum successful applications per day: **5**
- Primary markets: **New York, Los Angeles, Remote**
- Priority areas: music business development, strategic partnerships, artist partnerships, music marketing, A&R, creator partnerships, licensing, publishing, music technology, and AI partnerships
- Final live submission requires KD pressing **YES** in Telegram

## Important current limitation

The upstream MR.Jobs "resume tailoring" module generates tailored summary text, achievement bullets, keywords, emphasis guidance, and a cover letter. It does **not yet render a brand-new per-job PDF resume**. The application adapter therefore still uploads the private master resume file.

The next upgrade for this fork is a resume renderer that takes the tailored content and generates an ATS-safe PDF before the approval card is sent. Until that renderer is added and tested, the Telegram card should be understood as approving the job/application package, with the master resume as the uploaded resume.

## Architecture choice

The KD approval state is stored in a separate `kd_approvals` SQLite table rather than modifying the upstream MR.Jobs tracker schema. This keeps the customization isolated and makes future upstream merges easier.
