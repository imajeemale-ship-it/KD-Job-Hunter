"""Isolated SQLite and injected transports: never contact real employers."""
import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from utils import autonomous as worker, tracker
from utils.answers import trusted_answer


@pytest.fixture
def profile(tmp_path, monkeypatch):
    monkeypatch.setattr(tracker, 'DB_PATH', tmp_path / 'applications.db')
    return {'personal': {'first_name': 'Test', 'last_name': 'Applicant', 'email': 'test@example.com'},
            'autonomous': {'live_submit': True, 'max_attempts': 3, 'min_score': 80}}


def job(job_id='one', score=90, url=None, description='Build software'):
    tracker.log_discovered(SimpleNamespace(id=job_id, title='Engineer', company='Example', platform='greenhouse',
        url=url or f'https://example.com/jobs/{job_id}', apply_url=url or f'https://example.com/jobs/{job_id}',
        location='Remote', description=description, metadata={}))
    tracker.log_matched(job_id, score, 'Match', 'Old generated cover letter')


def sql(query, args=()):
    conn = worker.db()
    rows = conn.execute(query, args).fetchall()
    conn.commit()
    conn.close()
    return [dict(r) for r in rows]


def due():
    sql("UPDATE execution_queue SET due_at='2000-01-01'")


async def confirmed(job, dry_run, before_submit):
    if not dry_run:
        before_submit()
    return True, 'Thank you for applying!'


async def failed(job, dry_run, before_submit):
    raise TimeoutError('Network temporarily unavailable')


@pytest.mark.parametrize('description', [
    'Competitive compensation and benefits', 'Work with our legal department',
    'Certify software builds and develop assessment tools', 'Implement CAPTCHA for customers',
])
def test_job_description_is_never_a_blocker(profile, description):
    job(description=description)
    assert asyncio.run(worker.cycle(profile, confirmed)) == 'VERIFIED'


@pytest.mark.parametrize('question', ['Name of your manager', 'How many years of Kubernetes experience?',
    'Are you legally authorized to work in Canada?', 'Have you ever been convicted?', 'What is your security clearance?'])
def test_no_broad_or_ai_inference(profile, question):
    profile['common_answers'] = {'years_experience': '8', 'authorized_to_work': 'Yes'}
    assert trusted_answer(question, profile) is None


def test_exact_cached_values_and_false_zero(profile):
    profile['verified_answers'] = {'How many years of Kubernetes experience?': 0, 'Have you signed an NDA?': False}
    assert trusted_answer('How many years of Kubernetes experience?', profile) == '0'
    assert trusted_answer('Have you signed an NDA?', profile) == 'False'
    profile['common_answers'] = {'salary_expectation': '120000'}
    assert trusted_answer('Expected compensation', profile) == '120000'


def test_unknown_custom_question_cannot_call_brain(profile):
    from adapters import stagehand_adapter as adapter
    brain = MagicMock()
    field = {'field_purpose': 'custom', 'name': 'Do you have a clearance?', 'selector': '#answer', 'role': 'textbox'}
    async def run():
        from unittest.mock import patch
        with patch.object(adapter, '_resolve_selector', AsyncMock(return_value='#answer')):
            with pytest.raises(worker.Blocked, match='Do you have a clearance'):
                await adapter._fill_form_step(MagicMock(), '', profile, brain, '', {'fields': [field]}, [])
    asyncio.run(run())
    brain.answer_question.assert_not_called()


def test_actual_attestation_and_explicit_approval(profile):
    assert 'certify' in worker.blocker('Do you certify this statement?', profile)
    profile['verified_answers'] = {'Do you certify this statement?': 'Yes'}
    assert worker.blocker('Do you certify this statement?', profile) is None
    profile['autonomous']['approval_required'] = ['certify']
    assert 'Approval required' in worker.blocker('Do you certify this statement?', profile)


def test_backoff_and_exhaustion_are_operational(profile):
    job()
    for attempt, expected in [(1, 'FAILED_RETRY'), (2, 'FAILED_RETRY'), (3, 'FAILED')]:
        start = datetime.now(timezone.utc)
        assert asyncio.run(worker.cycle(profile, failed)) == expected
        item = sql("SELECT * FROM execution_queue WHERE action='apply'")[0]
        assert item['attempts'] == attempt
        if expected == 'FAILED_RETRY':
            delay = (datetime.fromisoformat(item['due_at']) - start).total_seconds()
            assert 60 * 2 ** (attempt - 1) <= delay < 60 * 2 ** (attempt - 1) + 5
        due()
    metrics = worker.metrics(profile=profile)
    assert metrics['applications_failed'] == 1
    assert metrics['applications_attempted'] == 3
    assert metrics['needs_kd'] == 0
    assert asyncio.run(worker.cycle(profile, confirmed)) == 'IDLE'


def test_failed_dry_run_is_retryable(profile):
    profile['autonomous']['live_submit'] = False
    job()
    assert asyncio.run(worker.cycle(profile, failed)) == 'FAILED_RETRY'
    assert tracker.get_today_count() == 0


@pytest.mark.parametrize('outcome', ['exception', 'false', 'unclear'])
def test_post_submit_ambiguity_cannot_retry(profile, outcome):
    job()
    calls = []
    async def uncertain(job, dry_run, before_submit):
        calls.append(job['id'])
        before_submit()
        if outcome == 'exception':
            raise TimeoutError('Lost response after click')
        return outcome == 'unclear', 'Still loading'
    assert asyncio.run(worker.cycle(profile, uncertain)) == 'NEEDS_KD'
    # Even an accidental administrative requeue cannot bypass the durable fence.
    sql("UPDATE execution_queue SET state='READY'")
    assert asyncio.run(worker.cycle(profile, uncertain)) == 'NEEDS_KD'
    assert calls == ['one']
    assert worker.metrics(profile=profile)['applications_verified_submitted'] == 0
    assert tracker.get_today_count() == 0


def test_same_url_under_different_discovery_ids_cannot_submit_twice(profile):
    job('one', url='https://example.com/jobs/42?utm_source=feed')
    job('two', url='https://example.com/jobs/42?utm_source=search')
    assert asyncio.run(worker.cycle(profile, confirmed)) == 'VERIFIED'
    assert asyncio.run(worker.cycle(profile, confirmed)) == 'NEEDS_KD'
    assert worker.metrics(profile=profile)['applications_verified_submitted'] == 1


def test_restart_before_submission_recovers_without_human(profile):
    job()
    worker.queue_qualified(profile)
    item = worker.claim()
    worker._attempt(item)
    # Simulate a terminated process: no live lock, durable IN_PROGRESS remains.
    assert asyncio.run(worker.cycle(profile, confirmed)) == 'IDLE'
    assert sql('SELECT state FROM execution_queue')[0]['state'] == 'FAILED_RETRY'
    due()
    assert asyncio.run(worker.cycle(profile, confirmed)) == 'VERIFIED'


def test_restart_after_submission_stays_fenced(profile):
    job()
    worker.queue_qualified(profile)
    item = worker.claim()
    worker._attempt(item)
    worker._begin_submit(item, tracker.get_job_by_id('one'), profile)
    assert asyncio.run(worker.cycle(profile, confirmed)) == 'IDLE'
    assert sql('SELECT state FROM execution_queue')[0]['state'] == 'NEEDS_KD'
    assert worker.metrics(profile=profile)['needs_kd'] == 1


def test_completion_is_atomic_with_follow_up(profile, monkeypatch):
    job()
    original = worker._enqueue
    def fail_follow_up(conn, job_id, action='apply', due_at=None):
        if action == 'follow_up':
            raise RuntimeError('Simulated storage error')
        return original(conn, job_id, action, due_at)
    monkeypatch.setattr(worker, '_enqueue', fail_follow_up)
    assert asyncio.run(worker.cycle(profile, confirmed)) == 'NEEDS_KD'
    assert tracker.get_job_by_id('one')['status'] == 'matched'
    assert worker.metrics(profile=profile)['applications_verified_submitted'] == 0
    assert len(sql('SELECT * FROM submission_fences')) == 1


def test_concurrent_workers_cannot_race_daily_cap(profile):
    job('one'); job('two')
    profile['autonomous']['daily_cap'] = 1
    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        async def slow(job, dry_run, before_submit):
            started.set()
            await release.wait()
            before_submit()
            return True, 'Application submitted'
        first = asyncio.create_task(worker.cycle(profile, slow))
        await started.wait()
        assert await worker.cycle(profile, confirmed) == 'BUSY'
        release.set()
        assert await first == 'VERIFIED'
        assert await worker.cycle(profile, confirmed) == 'DAILY_CAP'
    asyncio.run(run())


def test_cap_survives_status_change_and_kill_switch(profile):
    job('one'); job('two')
    profile['autonomous']['daily_cap'] = 1
    assert asyncio.run(worker.cycle(profile, confirmed)) == 'VERIFIED'
    tracker.update_job_status('one', 'interviewing')
    assert tracker.get_today_count() == 1
    assert asyncio.run(worker.cycle(profile, confirmed)) == 'DAILY_CAP'
    profile['autonomous']['paused'] = True
    assert asyncio.run(worker.cycle(profile, confirmed)) == 'PAUSED'
    assert worker.metrics(profile=profile)['applications_attempted'] == 1


def test_pause_reloaded_immediately_before_click(profile):
    job()
    current = json.loads(json.dumps(profile))
    async def apply(job, dry_run, before_submit):
        current['autonomous']['paused'] = True
        before_submit()
        pytest.fail('Must not click after pause')
    assert asyncio.run(worker.cycle(profile, apply, load_profile=lambda: current)) == 'READY'
    assert not sql('SELECT * FROM submission_fences')


def enable_followups(profile):
    profile['autonomous']['follow_up_days'] = 0
    profile['autonomous']['follow_up'] = {'enabled': True, 'contacts': {'one': 'hiring@example.com'}, 'max_messages': 2, 'interval_days': 0}


async def sent(message, profile, before_send):
    assert message['To'] == 'hiring@example.com'
    assert 'Engineer' in message.get_content()
    before_send()
    return True


def test_automatic_and_ghost_followups_are_sequence_safe(profile):
    enable_followups(profile)
    job()
    assert asyncio.run(worker.cycle(profile, confirmed)) == 'VERIFIED'
    # Cap blocks applications, not follow-up work.
    profile['autonomous']['daily_cap'] = 1
    assert asyncio.run(worker.cycle(profile, confirmed, sent)) == 'FOLLOWUP_SENT'
    assert asyncio.run(worker.cycle(profile, confirmed, sent)) == 'FOLLOWUP_SENT'
    assert asyncio.run(worker.cycle(profile, confirmed, sent)) == 'DAILY_CAP'
    assert tracker.get_job_by_id('one')['follow_up_count'] == 2
    assert worker.metrics(profile=profile)['followups_sent'] == 2
    assert len(sql("SELECT * FROM execution_queue WHERE action LIKE 'follow_up%'")) == 2


def test_followup_stops_on_actual_response(profile):
    enable_followups(profile)
    job()
    asyncio.run(worker.cycle(profile, confirmed))
    tracker.update_job_status('one', 'interviewing')
    sender = AsyncMock()
    assert asyncio.run(worker.cycle(profile, confirmed, sender)) == 'DONE'
    sender.assert_not_called()


def test_no_contact_is_exact_human_blocker(profile):
    enable_followups(profile)
    profile['autonomous']['follow_up']['contacts'] = {}
    job()
    asyncio.run(worker.cycle(profile, confirmed))
    assert asyncio.run(worker.cycle(profile, confirmed, sent)) == 'NEEDS_KD'
    assert 'verified employer email' in sql("SELECT reason FROM execution_queue WHERE action='follow_up'")[0]['reason']


def test_followup_technical_failure_retries_and_ambiguous_delivery_does_not(profile):
    enable_followups(profile)
    job()
    asyncio.run(worker.cycle(profile, confirmed))
    async def fail(message, profile, before_send):
        raise ConnectionError('SMTP connect failed')
    assert asyncio.run(worker.cycle(profile, confirmed, fail)) == 'FAILED_RETRY'
    due()
    async def ambiguous(message, profile, before_send):
        before_send()
        raise ConnectionError('SMTP disconnected after DATA')
    assert asyncio.run(worker.cycle(profile, confirmed, ambiguous)) == 'NEEDS_KD'
    due()
    assert asyncio.run(worker.cycle(profile, confirmed, sent)) == 'IDLE'
    assert worker.metrics(profile=profile)['followups_sent'] == 0


def test_followup_disabled_and_dry_run_do_not_send(profile):
    job()
    profile['autonomous']['follow_up_days'] = 0
    asyncio.run(worker.cycle(profile, confirmed))
    sender = AsyncMock()
    assert asyncio.run(worker.cycle(profile, confirmed, sender)) == 'WAITING'
    enable_followups(profile)
    profile['autonomous']['live_submit'] = False
    assert asyncio.run(worker.cycle(profile, confirmed, sender)) == 'WAITING'
    sender.assert_not_called()
    assert worker.metrics(profile=profile)['needs_kd'] == 0


def test_metrics_keep_threshold_outcomes_and_interviews_independent(profile, monkeypatch):
    import scheduler
    monkeypatch.setattr(scheduler, 'get_profile', lambda: profile)
    job('one', 90); job('two', 70)
    assert worker.metrics()['jobs_discovered'] == 2
    assert worker.metrics()['jobs_qualified'] == 1
    asyncio.run(worker.cycle(profile, confirmed))
    tracker.update_job_status('one', 'interviewing')
    tracker.update_job_status('one', 'offer')
    m = worker.metrics()
    assert m['interviews_detected'] == 1
    assert m['applications_verified_submitted'] == 1
    assert m['applications_attempted'] == 1
    assert m['applications_failed'] == 0


def test_tailoring_gate_uses_only_real_profile_facts(profile, tmp_path):
    from utils.resume_tailor import prepare_application
    job(description='Build Python services; 10 years of Java required')
    profile['skills'] = {'primary': ['Python'], 'secondary': ['SQL']}
    path = tmp_path / 'resume.txt'; path.write_text('Actual applicant resume')
    profile['resume_path'] = str(path)
    prepared = prepare_application(tracker.get_job_by_id('one'), profile)
    assert 'Python' in prepared['cover_letter']
    assert 'Java' not in prepared['cover_letter']
    assert '10 years' not in prepared['cover_letter']
    assert tracker.get_tailored_resume('one')['strategy'] == 'trusted_profile_facts'
    profile['resume_path'] = str(tmp_path / 'missing')
    with pytest.raises(worker.Blocked, match='Resume missing'):
        prepare_application(tracker.get_job_by_id('one'), profile)


def test_scheduler_recovers_on_next_tick(profile, monkeypatch):
    import scheduler
    profile['autonomous']['enabled'] = True
    monkeypatch.setattr(scheduler, 'get_profile', lambda: profile)
    cycle = AsyncMock(side_effect=[RuntimeError('temporary database issue'), 'IDLE'])
    monkeypatch.setattr(worker, 'cycle', cycle)
    asyncio.run(scheduler.scheduled_autonomous_cycle())
    assert 'temporary database issue' in scheduler._last_results['autonomous']['error']
    asyncio.run(scheduler.scheduled_autonomous_cycle())
    assert scheduler._last_results['autonomous']['outcome'] == 'IDLE'
    assert cycle.call_args.kwargs['load_profile']() is profile


def test_scheduler_registers_continuous_worker_with_runtime_enable(profile, monkeypatch):
    import scheduler
    scheduler_mock = MagicMock()
    monkeypatch.setattr(scheduler, 'scheduler', scheduler_mock)
    monkeypatch.setattr(scheduler, 'get_profile', lambda: profile)
    monkeypatch.setattr(scheduler, '_configured', False)
    scheduler.setup_scheduler()
    call = next(c for c in scheduler_mock.add_job.call_args_list if c.kwargs.get('id') == 'autonomous')
    assert call.kwargs['max_instances'] == 1
    assert call.kwargs['coalesce'] is True


def test_dashboard_metrics_queue_and_existing_endpoints(profile, monkeypatch):
    from fastapi.testclient import TestClient
    import scheduler
    from dashboard.server import app
    monkeypatch.setattr(scheduler, 'get_profile', lambda: profile)
    job('one', 70)
    client = TestClient(app)
    assert client.get('/api/autonomous/metrics').json()['jobs_qualified'] == 0
    assert client.get('/api/autonomous/queue').json() == []
    assert client.get('/api/jobs').status_code == 200
    assert client.get('/api/stats').status_code == 200


@pytest.mark.parametrize('text', ['Application submitted', 'Thank you for applying!', 'We received your application.'])
def test_positive_confirmation(text):
    assert worker.verified(text)
    assert not worker.verified(text, dry_run=True)


@pytest.mark.parametrize('text', ['Your application was not successfully submitted',
    'Click submit to see Application submitted', 'If successful, application received will appear', 'Form completed'])
def test_nonconfirmations(text):
    assert not worker.verified(text)


@pytest.mark.parametrize('cross_fence', [False, True])
def test_real_process_crash_releases_lock_and_preserves_fence(profile, cross_fence):
    import os
    import subprocess
    import sys
    job()
    worker.queue_qualified(profile)
    code = '''
import fcntl, json, os
from pathlib import Path
from utils import autonomous as w, tracker
profile = json.loads(os.environ['TEST_PROFILE'])
lock = Path(str(tracker.DB_PATH) + '.worker.lock').open('a')
fcntl.flock(lock, fcntl.LOCK_EX)
item = w.claim()
w._attempt(item)
if os.environ['TEST_FENCE'] == '1':
    w._begin_submit(item, tracker.get_job_by_id('one'), profile)
os._exit(23)
'''
    result = subprocess.run([sys.executable, '-c', code], env={**os.environ,
        'MRJOBS_DB_PATH': str(tracker.DB_PATH), 'TEST_PROFILE': json.dumps(profile),
        'TEST_FENCE': str(int(cross_fence))}, timeout=10)
    assert result.returncode == 23
    assert asyncio.run(worker.cycle(profile, confirmed)) == 'IDLE'
    assert sql('SELECT state FROM execution_queue')[0]['state'] == ('NEEDS_KD' if cross_fence else 'FAILED_RETRY')
    due()
    assert asyncio.run(worker.cycle(profile, confirmed)) == ('IDLE' if cross_fence else 'VERIFIED')


def test_legacy_ambiguous_queue_migrates_to_durable_fence(profile):
    job('one'); job('two', url='https://example.com/jobs/one')
    conn = tracker.get_db()
    conn.execute('''CREATE TABLE execution_queue (
        id INTEGER PRIMARY KEY, job_id TEXT NOT NULL, action TEXT NOT NULL,
        state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
        due_at TEXT NOT NULL, reason TEXT DEFAULT '', updated_at TEXT NOT NULL,
        UNIQUE(job_id, action))''')
    conn.execute("INSERT INTO execution_queue(job_id,action,state,due_at,updated_at) VALUES ('one','apply','IN_PROGRESS','2000','2000')")
    conn.commit(); conn.close()
    assert asyncio.run(worker.cycle(profile, confirmed)) == 'NEEDS_KD'
    assert worker.metrics(profile=profile)['applications_verified_submitted'] == 0
    assert len(sql('SELECT * FROM submission_fences')) == 1


def test_missing_job_record_is_operational_failure(profile):
    job(); worker.queue_qualified(profile)
    sql("DELETE FROM applications WHERE id='one'")
    assert asyncio.run(worker.cycle(profile, confirmed)) == 'FAILED'
    assert worker.metrics(profile=profile)['needs_kd'] == 0


def test_brain_never_generates_factual_custom_answer(profile):
    from utils.brain import ClaudeBrain
    brain = ClaudeBrain.__new__(ClaudeBrain)
    brain.ask = MagicMock(side_effect=AssertionError('No AI answers'))
    with pytest.raises(worker.Blocked, match='security clearance'):
        brain.answer_question('What is your security clearance?', profile)
    profile['verified_answers'] = {'What is your security clearance?': 'None'}
    assert brain.answer_question('What is your security clearance?', profile) == 'None'
    brain.ask.assert_not_called()


def test_smtp_transport_authenticates_and_fences_before_send(profile, monkeypatch):
    from utils import follow_up
    enable_followups(profile)
    profile['autonomous']['follow_up']['smtp'] = {'host': 'smtp.example.com', 'username': 'test', 'password': 'fixture'}
    job(); asyncio.run(worker.cycle(profile, confirmed))
    client = MagicMock()
    client.__enter__.return_value = client
    def send_message(message):
        assert sql("SELECT phase FROM execution_queue WHERE action='follow_up'")[0]['phase'] == 'submitting'
        assert message['To'] == 'hiring@example.com'
        return {}
    client.send_message.side_effect = send_message
    monkeypatch.setattr(follow_up.smtplib, 'SMTP', MagicMock(return_value=client))
    assert asyncio.run(worker.cycle(profile, confirmed)) == 'FOLLOWUP_SENT'
    client.starttls.assert_called_once()
    client.login.assert_called_once_with('test', 'fixture')


def test_smtp_auth_failure_is_retryable(profile, monkeypatch):
    from utils import follow_up
    enable_followups(profile)
    profile['autonomous']['follow_up']['smtp'] = {'host': 'smtp.example.com', 'username': 'test', 'password': 'fixture'}
    job(); asyncio.run(worker.cycle(profile, confirmed))
    client = MagicMock(); client.__enter__.return_value = client
    client.login.side_effect = follow_up.smtplib.SMTPAuthenticationError(535, b'Auth failed')
    monkeypatch.setattr(follow_up.smtplib, 'SMTP', MagicMock(return_value=client))
    assert asyncio.run(worker.cycle(profile, confirmed)) == 'FAILED_RETRY'
    client.send_message.assert_not_called()
    assert worker.metrics(profile=profile)['needs_kd'] == 0


def test_failed_preparation_is_not_an_application_attempt(profile):
    job()
    async def tailor(job, profile):
        raise RuntimeError('Temporary disk issue')
    assert asyncio.run(worker.cycle(profile, confirmed, tailor=tailor)) == 'FAILED_RETRY'
    assert worker.metrics(profile=profile)['applications_attempted'] == 0


def test_dry_run_queue_resumes_once_live_policy_enabled(profile):
    job()
    profile['autonomous']['live_submit'] = False
    assert asyncio.run(worker.cycle(profile, confirmed)) == 'WAITING'
    profile['autonomous']['live_submit'] = True
    assert asyncio.run(worker.cycle(profile, confirmed)) == 'VERIFIED'
    assert asyncio.run(worker.cycle(profile, confirmed)) == 'IDLE'


def test_known_ats_application_url_aliases_share_identity():
    assert worker.identity({'id': 'one', 'url': 'http://jobs.lever.co/example/123'}) == worker.identity({'id': 'two', 'url': 'https://jobs.lever.co/example/123/apply?utm_source=other'})
    assert worker.identity({'id': 'one', 'url': 'https://boards.greenhouse.io/example/jobs/123'}) == worker.identity({'id': 'two', 'url': 'https://job-boards.greenhouse.io/example/jobs/123'})


def test_explicit_smtp_rejection_is_safe_to_retry(profile):
    from utils import follow_up
    enable_followups(profile); job()
    asyncio.run(worker.cycle(profile, confirmed))
    async def rejected(message, profile, before_send):
        before_send()
        raise follow_up.smtplib.SMTPDataError(451, b'Try again later')
    assert asyncio.run(worker.cycle(profile, confirmed, rejected)) == 'FAILED_RETRY'
    due()
    assert asyncio.run(worker.cycle(profile, confirmed, sent)) == 'FOLLOWUP_SENT'
    assert worker.metrics(profile=profile)['needs_kd'] == 0


def test_confirmation_can_include_unrelated_negative_instructions():
    assert worker.verified('We received your application. Do not reply to this automated message.')
    assert not worker.verified('Application submitted? No, please retry.')


def test_verified_count_cannot_be_undone_by_later_status(profile):
    job(); asyncio.run(worker.cycle(profile, confirmed))
    tracker.update_job_status('one', 'failed')
    assert tracker.get_today_count() == 1
    assert worker.metrics(profile=profile)['applications_verified_submitted'] == 1


def test_smtp_quit_failure_does_not_erase_delivery_receipt(profile, monkeypatch):
    from utils import follow_up
    enable_followups(profile)
    profile['autonomous']['follow_up']['smtp'] = {'host': 'smtp.example.com'}
    job(); asyncio.run(worker.cycle(profile, confirmed))
    client = MagicMock(); client.send_message.return_value = {}
    client.quit.side_effect = follow_up.smtplib.SMTPServerDisconnected('QUIT disconnected')
    monkeypatch.setattr(follow_up.smtplib, 'SMTP', MagicMock(return_value=client))
    assert asyncio.run(worker.cycle(profile, confirmed)) == 'FOLLOWUP_SENT'
    assert worker.metrics(profile=profile)['followups_sent'] == 1


def test_followup_rechecks_employer_status_immediately_before_delivery(profile):
    enable_followups(profile); job()
    asyncio.run(worker.cycle(profile, confirmed))
    async def response_arrives(message, profile, before_send):
        tracker.update_job_status('one', 'interviewing')
        before_send()
        pytest.fail('Do not send a no-response follow-up after an interview response')
    assert asyncio.run(worker.cycle(profile, confirmed, response_arrives)) == 'READY'
    assert asyncio.run(worker.cycle(profile, confirmed, sent)) == 'DONE'
    assert worker.metrics(profile=profile)['followups_sent'] == 0
