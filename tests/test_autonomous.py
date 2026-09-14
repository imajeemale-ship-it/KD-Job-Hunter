import asyncio
from types import SimpleNamespace

from utils import tracker, autonomous


def setup_job(tmp_path, monkeypatch, job_id='job-1', score=90):
    monkeypatch.setattr(tracker, 'DB_PATH', tmp_path / 'applications.db')
    job = SimpleNamespace(id=job_id, title='Engineer', company='Example', platform='greenhouse',
                          url='https://example.com/job', apply_url='https://example.com/job',
                          location='Remote', description='Build software', metadata={})
    tracker.log_discovered(job)
    tracker.log_matched(job_id, score, 'Good fit', 'Cover letter')


def test_verified_cycle_is_idempotent_and_queues_follow_up(tmp_path, monkeypatch):
    setup_job(tmp_path, monkeypatch)
    profile = {'preferences': {'min_match_score': 65}, 'autonomous': {'live_submit': True}}
    async def apply(job, dry_run):
        return True, 'Thank you for applying'
    assert asyncio.run(autonomous.cycle(profile, apply)) == 'VERIFIED'
    assert tracker.get_jobs_by_status('applied')[0]['id'] == 'job-1'
    conn = autonomous.db()
    assert conn.execute("SELECT state FROM execution_queue WHERE action='follow_up'").fetchone()['state'] == 'READY'
    conn.close()
    assert asyncio.run(autonomous.cycle(profile, apply)) == 'IDLE'
    assert autonomous.metrics()['applications_verified_submitted'] == 1


def test_dry_run_and_uncertain_submission_never_count(tmp_path, monkeypatch):
    setup_job(tmp_path, monkeypatch)
    async def apply(job, dry_run):
        return True, 'Form completed'
    assert asyncio.run(autonomous.cycle({'autonomous': {}}, apply)) == 'WAITING'
    assert tracker.get_today_count() == 0
    setup_job(tmp_path, monkeypatch, 'job-2')
    assert asyncio.run(autonomous.cycle({'autonomous': {'live_submit': True}}, apply)) == 'NEEDS_KD'
    assert tracker.get_today_count() == 0


def test_retries_cap_and_approval(tmp_path, monkeypatch):
    setup_job(tmp_path, monkeypatch)
    profile = {'autonomous': {'live_submit': True, 'max_attempts': 2}}
    async def fail(job, dry_run):
        return False, ''
    assert asyncio.run(autonomous.cycle(profile, fail)) == 'FAILED_RETRY'
    conn = autonomous.db()
    conn.execute("UPDATE execution_queue SET due_at='2000-01-01' WHERE job_id='job-1'")
    conn.commit(); conn.close()
    assert asyncio.run(autonomous.cycle(profile, fail)) == 'NEEDS_KD'
    setup_job(tmp_path, monkeypatch, 'job-2')
    assert asyncio.run(autonomous.cycle({'autonomous': {'paused': True}}, fail)) == 'PAUSED'
    assert asyncio.run(autonomous.cycle({'autonomous': {'daily_cap': 0}}, fail)) == 'DAILY_CAP'
    assert autonomous.blocker('Do you certify this?', {})
