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
