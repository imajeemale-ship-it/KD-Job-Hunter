"""
Greenhouse Adapter — Automates applications on Greenhouse ATS forms.

Greenhouse forms are semi-standardized:
- Standard fields: name, email, phone, resume, cover letter, LinkedIn, website
- Custom questions: varies per company
"""

import random
from playwright.async_api import Page
from utils.brain import ClaudeBrain
from utils.answers import trusted_answer
from utils.autonomous import Blocked, blocker


async def apply_greenhouse(
    page: Page,
    job_url: str,
    profile: dict,
    brain: ClaudeBrain,
    cover_letter: str = "",
    dry_run: bool = True
):
    """
    Fill and submit a Greenhouse application form.

    Args:
        page: Playwright page
        job_url: Full URL to the job posting (with #app anchor)
        profile: Parsed profile.yaml dict
        brain: ClaudeBrain instance
        cover_letter: Pre-generated cover letter text
        dry_run: If True, fill the form but don't click submit
    """
    personal = profile["personal"]
    common = profile.get("common_answers", {})

    print(f"  📝 Navigating to application...")
    await page.goto(job_url, wait_until="networkidle")
    await page.wait_for_timeout(2000)

    # Scroll to application form
    app_form = await page.query_selector("#application, #app, form")
    if app_form:
        await app_form.scroll_into_view_if_needed()
    await page.wait_for_timeout(1000)

    # --- Standard Fields ---
    print(f"  📝 Filling standard fields...")

    field_map = {
        '#first_name': personal.get('first_name', ''),
        '#last_name': personal.get('last_name', ''),
        '#email': personal.get('email', ''),
        '#phone': personal.get('phone', ''),
    }

    for selector, value in field_map.items():
        try:
            el = await page.query_selector(selector)
            if el:
                await el.fill(value)
                await page.wait_for_timeout(random.randint(200, 500))
        except Exception:
            pass

    # --- Resume Upload ---
    print(f"  📎 Uploading resume...")
    try:
        resume_input = await page.query_selector(
            'input[type="file"][name*="resume"], '
            'input[type="file"][id*="resume"], '
            'input[type="file"]:first-of-type'
        )
        if resume_input:
            await resume_input.set_input_files(profile["resume_path"])
            await page.wait_for_timeout(1000)
        else:
            # Try the "Attach" button approach
            attach_btn = await page.query_selector(
                'button:has-text("Attach"), a:has-text("Attach")'
            )
            if attach_btn:
                # Greenhouse sometimes uses a click-to-upload pattern
                file_input = await page.query_selector('input[type="file"]')
                if file_input:
                    await file_input.set_input_files(profile["resume_path"])
    except Exception as e:
        print(f"  ⚠ Resume upload failed: {e}")

    # --- Cover Letter ---
    if cover_letter:
        print(f"  ✉️  Adding cover letter...")
        try:
            cover_textarea = await page.query_selector(
                'textarea[name*="cover_letter"], '
                '#cover_letter, '
                'textarea[id*="cover"]'
            )
            if cover_textarea:
                await cover_textarea.fill(cover_letter)
        except Exception as e:
            print(f"  ⚠ Cover letter field not found: {e}")

    # --- LinkedIn & Website ---
    for field_id, value in [
        ('#job_application_answers_attributes_0_text_value', personal.get('linkedin', '')),
        ('input[name*="linkedin"]', personal.get('linkedin', '')),
        ('input[name*="website"]', personal.get('portfolio', '')),
        ('input[name*="github"]', personal.get('github', '')),
    ]:
        try:
            el = await page.query_selector(field_id)
            if el and value:
                await el.fill(value)
                await page.wait_for_timeout(random.randint(200, 400))
        except Exception:
            pass

    # --- Custom Questions ---
    print(f"  🤔 Handling custom questions...")
    custom_fields = await page.query_selector_all(
        '.field:not(#first_name):not(#last_name):not(#email):not(#phone), '
        '.custom-question, '
        '[class*="custom_fields"] .field'
    )

    for field_el in custom_fields:
        try:
            # Get the label
            label_el = await field_el.query_selector('label')
            if not label_el:
                continue
            label_text = (await label_el.inner_text()).strip()
            if not label_text or len(label_text) < 3:
                continue

            input_el = await field_el.query_selector('input:not([type="file"]), textarea, select')
            if not input_el:
                continue
            reason = blocker(label_text, profile)
            answer = trusted_answer(label_text, profile)
            if reason or answer is None:
                raise Blocked(reason or f"Question: {label_text}. Required action: provide an exact verified applicant answer.")
            tag = await input_el.evaluate('el => el.tagName.toLowerCase()')
            if tag == 'select':
                await input_el.select_option(label=answer)
            else:
                await input_el.fill(answer)

        except Blocked:
            raise
        except Exception as e:
            print(f"    ⚠ Custom field error: {e}")

    await page.wait_for_timeout(1000)

    # --- Submit ---
    if dry_run:
        print(f"  🏁 DRY RUN — Form filled but NOT submitted")
        print(f"     Review the form in the browser window")
        return True
    else:
        print(f"  🚀 Submitting application...")
        submit_btn = await page.query_selector(
            'input[type="submit"], '
            'button[type="submit"], '
            'button:has-text("Submit"), '
            '#submit_app'
        )
        if submit_btn:
            await submit_btn.click()
            await page.wait_for_timeout(3000)

            # Check for success
            success = await page.query_selector(
                '.flash-success, .confirmation, [class*="success"], [class*="thank"]'
            )
            if success:
                print(f"  ✅ Application submitted successfully!")
                return True
            else:
                print(f"  ⚠ Submit clicked but no confirmation detected")
                return False
        else:
            print(f"  ⚠ Submit button not found!")
            return False


async def _select_best_option(select_el, target_value: str):
    """Try to select the best matching option in a dropdown."""
    try:
        # First try exact value match
        await select_el.select_option(value=target_value)
    except Exception:
        try:
            # Try label match
            await select_el.select_option(label=target_value)
        except Exception:
            # Try partial text match
            options = await select_el.evaluate(
                'el => Array.from(el.options).map(o => ({value: o.value, text: o.text}))'
            )
            target_lower = target_value.lower()
            for opt in options:
                if target_lower in opt['text'].lower() or target_lower in opt['value'].lower():
                    await select_el.select_option(value=opt['value'])
                    return
