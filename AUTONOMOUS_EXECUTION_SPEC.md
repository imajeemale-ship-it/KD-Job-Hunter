# KD Job Hunter — Autonomous Execution Loop

## Mission
Turn KD Job Hunter from a scheduled bot into a persistent autonomous execution agent that produces real application outcomes with minimal human intervention.

## Core loop
Discover → Score → Tailor → Apply → Verify submission → Log → Schedule follow-up → Continue.

## Required queue states
- BACKLOG
- READY
- IN_PROGRESS
- WAITING
- NEEDS_KD
- DONE
- FAILED_RETRY

## Required behavior
1. Add a persistent execution queue and worker/orchestrator layer.
2. Reuse the existing tracker, scheduler, Playwright adapters, email checker, and profile config.
3. Allow configurable autonomous live submission for jobs above a threshold. Preserve dry-run mode for testing.
4. Count an application only after a verification step confirms a real post-submit success state.
5. Add safe retries with exponential backoff, max attempts, and failure reason logging.
6. Prevent duplicate submissions with idempotency checks.
7. Escalate to NEEDS_KD only for genuine blockers, including:
   - compensation decisions
   - legal/attestation questions
   - identity verification, CAPTCHA, MFA
   - assessments
   - questions where auto-answering could be misleading
   - anything explicitly marked approval-required in profile config
8. After a verified application, automatically queue a follow-up action.
9. Detect no-response / ghost states and create the next follow-up action.
10. Add a kill switch / pause flag and configurable daily cap.
11. Run continuously under the existing service/VPS runtime rather than depending on a daily Telegram report.
12. Every execution event must store timestamp, job id, action, outcome, error/reason, retry count, and next action.

## Outcome metrics
Track these separately from discovery/scoring activity:
- jobs_discovered
- jobs_qualified
- applications_attempted
- applications_verified_submitted
- applications_failed
- needs_kd
- followups_sent
- interviews_detected

## Acceptance criteria
- Agent can run unattended for multiple cycles.
- A qualified job can move from discovered to verified applied without user input when no blocker exists.
- Recoverable failures retry automatically.
- Genuine blockers move to NEEDS_KD with a concise reason and exact required answer/action.
- Successful submissions are verified before status=applied.
- Duplicate submission is prevented.
- Follow-up is automatically queued after a successful application.
- Metrics clearly distinguish jobs found/scored from real submitted applications.
- Existing dashboard/API remains functional.

## Engineering guidance
- Prefer an orchestrator/worker layer over rewriting the existing app.
- Keep deterministic execution logic deterministic; use the LLM only for scoring, tailoring, and interpreting ambiguous forms.
- Add tests for queue transitions, retry behavior, verification logic, escalation rules, idempotency, and daily caps.
- Update AGENTS.md and README with autonomous mode, configuration, operational controls, and recovery instructions.

## Deliverable
Implement on this branch (`autonomous-execution-loop`), run the full test suite, and open a PR to `main` with a concise migration/setup checklist and proof that at least one end-to-end dry-run cycle reaches VERIFIED or NEEDS_KD correctly.
