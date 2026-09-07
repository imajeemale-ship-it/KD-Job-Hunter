from unittest.mock import AsyncMock
import sqlite3
import pytest
from utils.live_safety import is_greenhouse_live_url
from utils import kd_approval_state as state
from adapters.greenhouse import submit_greenhouse_form, GreenhouseSubmissionError

@pytest.mark.parametrize('url,allowed', [
 ('https://boards.greenhouse.io/a/jobs/1', True),
 ('https://job-boards.greenhouse.io/a/jobs/1', True),
 ('https://BOARDS.GREENHOUSE.IO/a', True),
 ('https://boards.greenhouse.io.evil.com/a', False),
 ('https://evil.com/boards.greenhouse.io', False),
 ('https://boards.greenhouse.io@evil.com', False),
 ('https://evil@boards.greenhouse.io/a', False),
 ('http://boards.greenhouse.io/a', False),
 ('https://boards.greenhouse.io:444/a', False),
 ('https://jobs.ashbyhq.com/a', False),
 ('https://linkedin.com/jobs/1', False),
 ('https://indeed.com/a', False),
 ('https://example.myworkdayjobs.com/a', False),
])
def test_hosts(url, allowed):
 assert is_greenhouse_live_url(url) is allowed

@pytest.fixture
def db(tmp_path, monkeypatch):
 monkeypatch.setattr(state, 'DB_PATH', tmp_path/'test.db')
 c=state._db()
 c.execute('CREATE TABLE applications (id TEXT PRIMARY KEY, status TEXT, applied_at TEXT)')
 c.executemany("INSERT INTO applications VALUES (?, 'matched', NULL)", [('a',),('b',)])
 c.commit(); c.close()
 for j in ['a','b']: state.create_waiting(j,j,1)
 return state

def test_claims_and_terminal_states(db):
 assert db.set_decision('a', True)
 assert not db.set_decision('a', True)
 assert db.claim_submission('a')
 db.set_decision('b', True)
 assert not db.claim_submission('a')
 assert not db.claim_submission('b')
 db.mark_submitted('a'); db.mark_failed('a','late error')
 assert db.get_by_job('a')['status']=='submitted'
 assert db.get_by_job('a')['submitted_at']
 assert not db.retry_failed('a')
 assert db.claim_submission('b')
 db.mark_failed('b','Required fields missing; no submit clicked')
 assert not db.claim_submission('b')
 assert db.retry_failed('b')
 assert db.claim_submission('b')
 db.mark_failed('b','Submission outcome unknown')
 assert not db.retry_failed('b')

def test_daily_limit(db):
 c=db._db()
 for i in range(5):
  c.execute("INSERT INTO applications VALUES (?, 'applied', datetime('now'))", (str(i),))
 c.commit(); c.close()
 db.set_decision('a',True)
 assert not db.claim_submission('a',100)

@pytest.mark.asyncio
async def test_telegram_single_attempt(db, monkeypatch):
 import kd_autopilot as worker
 monkeypatch.setattr(worker, 'get_job_by_id', lambda _: {'title':'Role','company':'Company'})
 telegram=AsyncMock()
 monkeypatch.setattr(worker, 'TelegramApproval', lambda _: telegram)
 async def submit(*args):
  db.mark_submitted('a'); return True
 monkeypatch.setattr(worker,'_submit_claimed_job',submit)
 db.set_decision('a',True)
 assert await worker.submit_job({},'a')
 assert not await worker.submit_job({},'a')
 assert telegram.send_status.await_count==2
 assert 'SUBMITTED ✅' in telegram.send_status.call_args.args[0]

@pytest.mark.asyncio
async def test_browser_confirmation_and_validation():
 from playwright.async_api import async_playwright
 async with async_playwright() as p:
  browser=await p.chromium.launch()
  page=await browser.new_page()
  async def load(html):
   await page.route('https://boards.greenhouse.io/**', lambda route: route.fulfill(body=html,content_type='text/html'))
   await page.goto('https://boards.greenhouse.io/test/jobs/1')
  await load('<form><input required><button type="submit">Submit application</button></form>')
  with pytest.raises(GreenhouseSubmissionError,match='no submit clicked'):
   await submit_greenhouse_form(page,1)
  await page.unroute_all()
  await load('''<button onclick="setTimeout(()=>document.body.innerHTML='Thank you for applying',500)">Submit application</button>''')
  assert await submit_greenhouse_form(page,2)
  await page.unroute_all()
  await load('''<button onclick="this.remove()">Submit</button>''')
  with pytest.raises(GreenhouseSubmissionError,match='outcome unknown'):
   await submit_greenhouse_form(page,.3)
  await browser.close()

@pytest.mark.asyncio
async def test_live_router_never_falls_back(monkeypatch):
 from adapters import stagehand_adapter as router
 from adapters import greenhouse
 fallback=AsyncMock(); monkeypatch.setattr(router,'apply_stagehand',fallback)
 adapter=AsyncMock(return_value=False); monkeypatch.setattr(greenhouse,'apply_greenhouse',adapter)
 assert not await router.apply_smart(None,'https://boards.greenhouse.io/a/jobs/1',{},None,dry_run=False)
 fallback.assert_not_called()
 with pytest.raises(RuntimeError,match='unsupported'):
  await router.apply_smart(None,'https://jobs.ashbyhq.com/a',{},None,dry_run=False)
 fallback.assert_not_called()
