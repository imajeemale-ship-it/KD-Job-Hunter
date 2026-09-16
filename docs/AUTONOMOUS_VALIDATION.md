# Autonomous execution review validation

Branch: `autonomous-execution-loop`. PR #2 must remain unmerged.

## Results

- Focused autonomy suite: **73 passed**.
- Complete suite: **229 passed, 4 skipped**.
- One non-failing Starlette/HTTPX TestClient deprecation warning.
- `git diff --check`: clean.
- Edited example profile, Compose, and CI workflow YAML parse successfully.
- No real employer applications or follow-up messages were sent. New browser tests block HTTP(S) and use local synthetic forms. Scoring and SMTP are injected in the lifecycle test.

Commands:

```bash
.venv/bin/python -m pytest -q tests/test_autonomous.py tests/test_autonomous_regressions.py tests/test_autonomous_forms.py
.venv/bin/python -m pytest -q
```

## Review coverage

| Requirement | Regression evidence |
| --- | --- |
| Ordinary posting text must not trigger NEEDS_KD | Parameterized job descriptions containing compensation, legal, certification, assessment and CAPTCHA; browser fixture contains posting keywords outside the form. |
| Never invent applicant answers | Unknown custom questions never call the brain; exact verified answers, false/zero values, narrowly matched profile aliases, unknown prefilled answers, and dropdown choices are tested. Greenhouse and generic fallback tests reject generated factual values. |
| Complete autonomous loop and tailoring | `test_discover_score_tailor_apply_verify_log_followup` runs scheduler discovery/scoring, grounded preparation, an actual local Playwright form, verified logging, and fake SMTP follow-up. |
| Continue multi-step applications | A submit-type Next button advances to a different form step, then exactly one final submit; both clicks use the durable guard. |
| Automatic follow-up and ghost sequence | Two accepted follow-up stages have distinct queue identities; policy limits stop further sends. Disabled/dry-run policies do not send; actual response/status changes stop pending delivery. |
| Technical retry versus applicant blocker | Exponential 60/120-second retry delays, configured exhaustion into FAILED, missing-job operational failures, SMTP auth/rejection retries, and exact missing-contact actions are tested. |
| Duplicate prevention and restart safety | Different source IDs and canonical ATS URL aliases share a fence. Actual child processes terminate before/after the fence; only the pre-submit case resumes. Legacy interrupted queue rows migrate safely. Concurrent workers cannot race. |
| Verified-only accounting | False, unclear, negated and hypothetical confirmations do not count. Atomic success/follow-up rollback, post-click exceptions, and durable applied counts after later status changes are tested. |
| Pause and cap | Pause is reloaded immediately before clicking. Daily cap survives interview/status changes and concurrent worker attempts; it does not block follow-up work. |
| Independent metrics | Configured threshold, real adapter attempt events, unique verified outcomes, final failures, current blocked work, accepted follow-ups, and persistent interview detections are checked independently. |
| Continuous operation and compatibility | Scheduler registration and recovery on later ticks, process-crash recovery, additive schema migration, existing jobs/stats API and autonomous queue/metrics endpoints are tested. Docker persists SQLite and WAL together. |

## Deployment notes

The service was not deployed/restarted as part of this validation. Continuous operation is supported by scheduler, crash-recovery tests and persistent service configuration; this is not evidence of a live VPS soak test.

Existing Docker installations must follow the database migration steps in README before using the new `./data` volume. The live profile was not changed. Follow-up delivery needs explicitly configured SMTP and verified contacts. Do not remove ambiguous submission fences without portal/email evidence that no submission occurred.

An offline GitHub Actions workflow is included. Local results above do not claim a remote CI result.
