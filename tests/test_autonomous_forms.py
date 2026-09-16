"""End-to-end browser fixtures with HTTP blocked and synthetic applicant data."""
from pathlib import Path
from urllib.parse import quote
from unittest.mock import MagicMock

import pytest

from adapters.stagehand_adapter import apply_stagehand, _fill_trusted_fields
from utils.autonomous import Blocked


@pytest.fixture
async def local_page():
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        await context.route('http://**/*', lambda route: route.abort())
        await context.route('https://**/*', lambda route: route.abort())
        page = await context.new_page()
        yield page
        await browser.close()


def fixture_url(question='', confirmation='Thank you for applying!'):
    html = '''<html><body><p>Competitive compensation; work with legal and assessment teams.</p>
    <form onsubmit="event.preventDefault(); window.submits=(window.submits||0)+1; document.body.innerHTML='CONFIRM';">
    <label for="first">First Name</label><input id="first" required>
    <label for="email">Email</label><input id="email" required>
    QUESTION
    <button type="submit">Submit Application</button></form></body></html>'''
    return 'data:text/html,' + quote(html.replace('QUESTION', question).replace('CONFIRM', confirmation))


def applicant():
    return {'personal': {'first_name': 'Test', 'email': 'test@example.com'}, 'autonomous': {}}


async def test_actual_form_with_posting_keywords_reaches_confirmed(local_page):
    calls = []
    brain = MagicMock()
    result = await apply_stagehand(local_page, fixture_url(), applicant(), brain,
                                  dry_run=False, before_submit=lambda url: calls.append(url))
    assert result is True
    assert len(calls) == 1
    assert await local_page.evaluate('window.submits') == 1
    brain.answer_question.assert_not_called()


async def test_actual_dry_run_fills_without_click(local_page):
    def cannot_submit(url):
        pytest.fail('Dry-run must not cross submit fence')
    result = await apply_stagehand(local_page, fixture_url(), applicant(), MagicMock(), before_submit=cannot_submit)
    assert result is True
    assert await local_page.locator('#first').input_value() == 'Test'
    assert await local_page.evaluate('window.submits || 0') == 0


@pytest.mark.parametrize('label', ['Do you certify this?', 'Years of Kubernetes experience', 'Name of your supervisor'])
async def test_real_unknown_question_blocks_before_click(local_page, label):
    custom = f'<label for="custom">{label}</label><input id="custom" required>'
    with pytest.raises(Blocked, match=label.replace('?', r'\?')):
        await apply_stagehand(local_page, fixture_url(custom), applicant(), MagicMock(), dry_run=False,
                              before_submit=lambda url: pytest.fail('Must not submit unknown answer'))
    assert await local_page.evaluate('window.submits || 0') == 0


async def test_exact_answer_allows_custom_question(local_page):
    profile = applicant()
    profile['verified_answers'] = {'Years of Kubernetes experience': '0'}
    custom = '<label for="custom">Years of Kubernetes experience</label><input id="custom" required>'
    assert await apply_stagehand(local_page, fixture_url(custom), profile, MagicMock(),
                                 dry_run=False, before_submit=lambda url: None)


async def test_unclear_post_click_is_not_verified_or_reclicked(local_page):
    assert not await apply_stagehand(local_page, fixture_url(confirmation='Application submitted? No, please retry.'),
        applicant(), MagicMock(), dry_run=False, before_submit=lambda url: None)
    assert await local_page.evaluate('window.submits') == 1


async def test_visible_verification_control_is_a_blocker(local_page):
    custom = '<input autocomplete="one-time-code" aria-label="Verification code">'
    with pytest.raises(Blocked, match='CAPTCHA/MFA'):
        await apply_stagehand(local_page, fixture_url(custom), applicant(), MagicMock(),
            dry_run=False, before_submit=lambda url: pytest.fail('Must not submit'))


async def test_untrusted_prefilled_value_is_not_accepted(local_page):
    custom = '<label for="custom">Security clearance</label><input id="custom" value="Top Secret">'
    with pytest.raises(Blocked, match='Security clearance'):
        await apply_stagehand(local_page, fixture_url(custom), applicant(), MagicMock(),
            dry_run=False, before_submit=lambda url: pytest.fail('Must not submit'))


async def test_nonmatching_dropdown_never_selects_first_option(local_page):
    profile = applicant(); profile['common_answers'] = {'salary_expectation': '100000'}
    custom = '<label for="salary">Expected compensation</label><select id="salary"><option>50000</option><option>200000</option></select>'
    with pytest.raises(Blocked, match='exact allowed option'):
        await apply_stagehand(local_page, fixture_url(custom), profile, MagicMock(),
            dry_run=False, before_submit=lambda url: pytest.fail('Must not submit'))


async def test_discover_score_tailor_apply_verify_log_followup(local_page, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from utils import autonomous as worker, tracker
    from utils.resume_tailor import prepare_application
    import scheduler
    from utils.brain import ClaudeBrain
    from utils import discovery, resume_parser

    monkeypatch.setattr(tracker, 'DB_PATH', tmp_path / 'db.sqlite')
    resume = tmp_path / 'resume.txt'; resume.write_text('Test applicant. Python skills.')
    profile = applicant()
    profile.update({'resume_path': str(resume), 'skills': {'primary': ['Python']},
                    'preferences': {'min_match_score': 65}})
    profile['autonomous'] = {'live_submit': True, 'follow_up_days': 0,
        'follow_up': {'enabled': True, 'contacts': {'fixture': 'hiring@example.com'}, 'max_messages': 1}}
    url = fixture_url()
    listing = SimpleNamespace(id='fixture', title='Engineer', company='Fixture', platform='test', url=url,
        apply_url=url, location='Remote', description='Build Python systems', metadata={})
    async def discover(profile):
        return [listing]
    monkeypatch.setattr(discovery, 'discover_all_jobs', discover)
    monkeypatch.setattr(scheduler, 'get_profile', lambda: profile)
    monkeypatch.setattr(resume_parser, 'extract_resume_text', lambda path: resume.read_text())
    monkeypatch.setattr(ClaudeBrain, '__init__', lambda self, *a, **k: None)
    monkeypatch.setattr(ClaudeBrain, 'match_job', lambda *a, **k: {'score': 90, 'reasoning': 'Fixture skill match'})
    await scheduler.scheduled_discover()
    await scheduler.scheduled_score()
    async def tailor(job, profile):
        return prepare_application(job, profile)
    async def apply(job, dry_run, before_submit):
        result = await apply_stagehand(local_page, url, profile, MagicMock(), cover_letter=job['cover_letter'],
            dry_run=dry_run, before_submit=before_submit)
        return result, await local_page.locator('body').inner_text()
    assert await worker.cycle(profile, apply, tailor=tailor) == 'VERIFIED'
    async def follow(message, profile, before_send):
        before_send()
        return True
    assert await worker.cycle(profile, apply, follow) == 'FOLLOWUP_SENT'
    metrics = worker.metrics(profile=profile)
    assert metrics['jobs_discovered'] == metrics['jobs_qualified'] == 1
    assert metrics['applications_attempted'] == metrics['applications_verified_submitted'] == 1
    assert metrics['followups_sent'] == 1
    assert metrics['applications_failed'] == metrics['needs_kd'] == 0
    assert tracker.get_tailored_resume('fixture')['emphasis_areas'] == ['Python']


async def test_generic_adapter_cannot_use_generated_factual_value(local_page):
    from adapters.generic import apply_generic
    brain = MagicMock()
    brain.ask_json.return_value = {'status': 'submit', 'fields': [
        {'action': 'fill', 'selector': '#custom', 'value': 'Invented clearance', 'note': 'Security clearance'}]}
    custom = '<label for="custom">Security clearance</label><input id="custom">'
    with pytest.raises(Blocked, match='Security clearance'):
        await apply_generic(local_page, fixture_url(custom), applicant(), brain)
    assert await local_page.locator('#custom').input_value() == ''


async def test_greenhouse_custom_select_does_not_ask_ai(local_page):
    from adapters.greenhouse import apply_greenhouse
    brain = MagicMock()
    custom = '<div class="field"><label for="custom">Security clearance</label><select id="custom"><option>None</option><option>Top secret</option></select></div>'
    with pytest.raises(Blocked, match='Security clearance'):
        await apply_greenhouse(local_page, fixture_url(custom), applicant(), brain)
    brain.ask.assert_not_called()
    brain.answer_question.assert_not_called()


async def test_submit_type_next_continues_safely_without_approval(local_page, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from utils import tracker, autonomous as worker
    monkeypatch.setattr(tracker, 'DB_PATH', tmp_path / 'wizard.db')
    html = '''<html><body><form id="step1"><label for="first">First Name</label><input id="first" required>
      <button type="submit">Next</button></form><script>
      document.querySelector('form').onsubmit = e => {
        e.preventDefault(); window.nextClicks = (window.nextClicks || 0) + 1;
        document.body.innerHTML = '<form id="step2"><label for="email">Email</label><input id="email" required><button type="submit">Submit Application</button></form>';
        document.querySelector('form').onsubmit = e => {e.preventDefault(); window.submits = (window.submits || 0) + 1; document.body.innerHTML='Thank you for applying!';};
      };</script></body></html>'''
    url = 'data:text/html,' + quote(html)
    profile = applicant(); profile['autonomous']['live_submit'] = True
    tracker.log_discovered(SimpleNamespace(id='wizard', title='Engineer', company='Example', platform='fixture', url=url,
        apply_url=url, location='Remote', description='Build software', metadata={}))
    tracker.log_matched('wizard', 90, 'Fixture', '')
    async def apply(job, dry_run, before_submit):
        result = await apply_stagehand(local_page, url, profile, MagicMock(), dry_run=dry_run, before_submit=before_submit)
        return result, await local_page.locator('body').inner_text()
    assert await worker.cycle(profile, apply) == 'VERIFIED'
    assert await local_page.evaluate('window.nextClicks') == 1
    assert await local_page.evaluate('window.submits') == 1
    assert await worker.cycle(profile, apply) == 'IDLE'


def setup_fence_wizard(tmp_path, monkeypatch, next_label='Next', next_type='submit'):
    from types import SimpleNamespace
    from utils import tracker
    monkeypatch.setattr(tracker, 'DB_PATH', tmp_path / 'wizard-fence.db')
    html = '''<html><body><form id="step1"><label for="first">First Name</label><input id="first" required>
      <button id="next" type="NEXT_TYPE">NEXT_LABEL</button></form><script>
      document.querySelector('#next').onclick = e => {
        e.preventDefault();
        document.body.innerHTML = '<form id="step2"><label for="email">Email</label><input id="email" required><button id="final" type="submit">Submit Application</button></form>';
        document.querySelector('form').onsubmit = e => {
          e.preventDefault(); document.body.innerHTML='Thank you for applying!';
        };
      };</script></body></html>'''
    url = 'data:text/html,' + quote(html.replace('NEXT_TYPE', next_type).replace('NEXT_LABEL', next_label))
    profile = applicant()
    profile['autonomous']['live_submit'] = True
    tracker.log_discovered(SimpleNamespace(id='wizard', title='Engineer', company='Example', platform='fixture',
        url=url, apply_url=url, location='Remote', description='Build software', metadata={}))
    tracker.log_matched('wizard', 90, 'Fixture', '')
    return profile, url


def fence_state():
    from utils import autonomous as worker
    conn = worker.db()
    try:
        item = dict(conn.execute("SELECT * FROM execution_queue WHERE action='apply'").fetchone())
        fences = conn.execute('SELECT COUNT(*) FROM submission_fences').fetchone()[0]
        return item, fences
    finally:
        conn.close()


@pytest.mark.parametrize('next_label', ['Next', 'Continue', 'Save & Continue'])
@pytest.mark.parametrize('failure', ['timeout', 'crash'])
@pytest.mark.parametrize('interrupt_at', ['next', 'final'])
async def test_wizard_fence_boundary_and_interruption_recovery(
    local_page, tmp_path, monkeypatch, next_label, failure, interrupt_at,
):
    import asyncio
    from playwright.async_api import Locator
    from utils import autonomous as worker

    profile, url = setup_fence_wizard(tmp_path, monkeypatch, next_label)
    real_click = Locator.click
    clicks = {'next': 0, 'final': 0}
    interrupted = False

    async def click(control, *args, **kwargs):
        nonlocal interrupted
        control_id = await control.get_attribute('id')
        # Inspect real persisted state immediately BEFORE the real browser click.
        item, fences = fence_state()
        assert fences == (1 if control_id == 'final' else 0)
        assert item['phase'] == ('submitting' if control_id == 'final' else 'preparing')
        await real_click(control, *args, **kwargs)
        clicks[control_id] += 1
        if control_id == interrupt_at and not interrupted:
            interrupted = True
            if failure == 'timeout':
                raise TimeoutError('Simulated lost browser response after click')
            # Escape the worker's exception handler, leaving durable IN_PROGRESS
            # state just as a stopped process does. The next cycle recovers it.
            raise asyncio.CancelledError('Simulated worker termination after click')

    monkeypatch.setattr(Locator, 'click', click)

    async def apply(job, dry_run, before_submit):
        result = await apply_stagehand(local_page, url, profile, MagicMock(),
            dry_run=dry_run, before_submit=before_submit)
        return result, await local_page.locator('body').inner_text()

    expected = 'FAILED_RETRY' if interrupt_at == 'next' else 'NEEDS_KD'
    if failure == 'crash':
        with pytest.raises(asyncio.CancelledError):
            await worker.cycle(profile, apply)
        assert fence_state()[0]['state'] == 'IN_PROGRESS'
        assert await worker.cycle(profile, apply) == 'IDLE'
    else:
        assert await worker.cycle(profile, apply) == expected
    item, fences = fence_state()
    assert item['state'] == expected
    assert fences == (1 if interrupt_at == 'final' else 0)
    assert worker.metrics(profile=profile)['applications_verified_submitted'] == 0

    conn = worker.db()
    if interrupt_at == 'next':
        conn.execute("UPDATE execution_queue SET due_at='2000-01-01' WHERE action='apply'")
    else:
        # Even an accidental requeue cannot bypass the real final-submit fence.
        assert await worker.cycle(profile, apply) == 'IDLE'
        conn.execute("UPDATE execution_queue SET state='READY',due_at='2000-01-01' WHERE action='apply'")
    conn.commit()
    conn.close()
    assert await worker.cycle(profile, apply) == ('VERIFIED' if interrupt_at == 'next' else 'NEEDS_KD')
    assert clicks == {'next': 2 if interrupt_at == 'next' else 1, 'final': 1}
    assert worker.metrics(profile=profile)['needs_kd'] == (0 if interrupt_at == 'next' else 1)


@pytest.mark.parametrize('next_type', ['button', 'submit'])
async def test_wizard_dry_run_preserves_navigation_policy_without_fences(local_page, tmp_path, monkeypatch, next_type):
    from utils import autonomous as worker
    profile, url = setup_fence_wizard(tmp_path, monkeypatch, next_type=next_type)
    profile['autonomous']['live_submit'] = False

    async def apply(job, dry_run, before_submit):
        result = await apply_stagehand(local_page, url, profile, MagicMock(),
            dry_run=dry_run, before_submit=before_submit)
        return result, await local_page.locator('body').inner_text()

    assert await worker.cycle(profile, apply) == ('WAITING' if next_type == 'button' else 'FAILED_RETRY')
    assert fence_state()[1] == 0
    assert await local_page.locator('#final' if next_type == 'button' else '#next').count() == 1
    assert worker.metrics(profile=profile)['applications_verified_submitted'] == 0
