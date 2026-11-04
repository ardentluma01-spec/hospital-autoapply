"""Fills in (and optionally submits) an application on a SuccessFactors careers site.

Every hospital customises its form a little, so this works by *reading* the
form rather than hard-coding each one:
  * upload fields   -> your resume / the AI cover letter
  * required fields -> your CONFIRMED answers; AI only for non-factual questions
    (eligibility/fact questions are never answered by the AI - see matcher.ELIGIBILITY_RE)
  * anything it can't answer confidently -> it stops and asks you (status "needs_info")

It always saves a full-page screenshot so you can see exactly what it filled in.
"""
import logging
import re
import tempfile
from pathlib import Path

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

from .. import matcher

TMP = Path(tempfile.gettempdir()) / "autoapply"
TMP.mkdir(exist_ok=True)

log = logging.getLogger("applier")

APPLY_RE = re.compile(r"^\s*(apply( now| for this job)?|start application)\s*$", re.I)
SIGNIN_RE = re.compile(r"^\s*(sign in|log ?in|login|continue)\s*$", re.I)
SUBMIT_RE = re.compile(r"^\s*(submit( application)?|apply|send application|finish)\s*$", re.I)
DONE_RE = re.compile(r"(thank you for (your )?appl|application (has been )?(submitted|received)|successfully applied)", re.I)

# JS that finds the human-readable question for a form field
LABEL_JS = """
el => {
  const t = s => (s || '').replace(/\\s+/g, ' ').trim();
  if (el.getAttribute('aria-label')) return t(el.getAttribute('aria-label'));
  const lb = el.getAttribute('aria-labelledby');
  if (lb) { const n = lb.split(' ').map(i => document.getElementById(i)).filter(Boolean).map(n => n.innerText).join(' '); if (t(n)) return t(n); }
  if (el.id) { const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]'); if (l) return t(l.innerText); }
  const c = el.closest('label'); if (c) return t(c.innerText);
  let p = el.parentElement;
  for (let i = 0; i < 4 && p; i++, p = p.parentElement) { const s = t(p.innerText); if (s && s.length < 300) return s; }
  return t(el.name || el.id || '');
}
"""


def _click_text(page, pattern: re.Pattern, timeout=4000) -> bool:
    for role in ("button", "link"):
        loc = page.get_by_role(role, name=pattern)
        if loc.count():
            try:
                loc.first.click(timeout=timeout)
                return True
            except Exception as e:  # noqa: BLE001
                log.debug("click on %s %r failed: %s", role, pattern.pattern, e)
                continue
    return False


def _same_page(a: str, b: str) -> bool:
    from urllib.parse import urlsplit
    ua, ub = urlsplit(a), urlsplit(b)
    return ua.hostname == ub.hostname and ua.path.rstrip("/").lower() == ub.path.rstrip("/").lower()


def _cover_letter_file(job: dict) -> Path:
    from docx import Document
    path = TMP / f"cover_letter_{job['id']}.docx"
    doc = Document()
    for para in (job.get("cover_letter") or "").split("\n"):
        doc.add_paragraph(para)
    doc.save(path)
    return path


COOKIE_RE = re.compile(r"^\s*(accept all cookies|accept all|accept cookies|allow all cookies|confirm my choices|i accept cookies)\s*$", re.I)
SIGNIN_EXACT_RE = re.compile(r"^\s*(candidate )?(sign ?in|log ?in|login)\s*$", re.I)
PROFILE_MENU_RE = re.compile(r"^\s*(view profile|my profile|profile|my account|account)\s*$", re.I)
NEXT_RE = re.compile(r"^\s*(next|continue|sign ?in|log ?in)\s*$", re.I)
EMAIL_SEL = ('input[type="email"]:visible, input[name*="user" i]:visible, input[id*="user" i]:visible, '
             'input[name*="email" i]:visible, input[id*="email" i]:visible, input[autocomplete="username"]:visible')


def _dismiss_cookies(page):
    """Cookie banners cover the page and swallow clicks. Only cookie-specific buttons are pressed."""
    for _ in range(2):
        clicked = False
        for role in ("button", "link"):
            loc = page.get_by_role(role, name=COOKIE_RE)
            for i in range(min(loc.count(), 3)):
                try:
                    if loc.nth(i).is_visible():
                        loc.nth(i).click(timeout=3000)
                        clicked = True
                        page.wait_for_timeout(600)
                        break
                except Exception:  # noqa: BLE001
                    continue
            if clicked:
                break
        if not clicked:
            return


def _visible_click(page, pattern) -> bool:
    for role in ("link", "button", "menuitem"):
        loc = page.get_by_role(role, name=pattern)
        for i in range(min(loc.count(), 5)):
            try:
                if loc.nth(i).is_visible():
                    loc.nth(i).click(timeout=5000)
                    return True
            except Exception:  # noqa: BLE001
                continue
    return False


def _has_login_form(page) -> bool:
    if page.locator('input[type="password"]:visible').count():
        return True
    # two-step sign-in pages show only the email box first
    return page.locator(EMAIL_SEL).count() > 0 and page.get_by_role("button", name=NEXT_RE).count() > 0


def _open_signin(page) -> bool:
    """Get from any careers page to a visible sign-in form (handles 'View profile' -> 'Sign In' menus)."""
    _dismiss_cookies(page)
    for _ in range(4):
        if _has_login_form(page):
            return True
        if _visible_click(page, SIGNIN_EXACT_RE):
            pass
        elif _visible_click(page, PROFILE_MENU_RE):
            page.wait_for_timeout(800)
            if _visible_click(page, SIGNIN_EXACT_RE):
                pass
        else:
            link = page.locator('a[href*="login" i]:visible, a[href*="signin" i]:visible').first
            try:
                link.click(timeout=4000)
            except Exception:  # noqa: BLE001
                return _has_login_form(page)
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:  # noqa: BLE001
            pass
        page.wait_for_timeout(800)
        _dismiss_cookies(page)
    return _has_login_form(page)


def _what_i_see(page) -> str:
    """Short description of the page for error messages (no personal data)."""
    from urllib.parse import urlsplit
    u = urlsplit(page.url)
    labels = []
    try:
        for el in page.locator("button:visible, a:visible").all()[:40]:
            t = (el.inner_text() or "").strip().replace("\n", " ")
            if t and len(t) < 40 and t not in labels:
                labels.append(t)
    except Exception:  # noqa: BLE001
        pass
    return f"page: {u.hostname}{u.path} | buttons/links: {', '.join(labels[:12]) or 'none'}"


def _login_if_needed(page, email: str, password: str) -> bool:
    _dismiss_cookies(page)
    pw = page.locator('input[type="password"]:visible')
    if not pw.count():
        # two-step sign-in: email first, then a password page
        user = page.locator(EMAIL_SEL)
        if user.count() and email and page.get_by_role("button", name=NEXT_RE).count():
            user.first.fill(email)
            _visible_click(page, NEXT_RE)
            page.wait_for_timeout(2500)
            pw = page.locator('input[type="password"]:visible')
            if not pw.count():
                return False      # asked for the email but never showed a password box
        if not pw.count():
            return True
    if not (email and password):
        return False
    user = page.locator(EMAIL_SEL)
    if user.count():
        user.first.fill(email)
    pw.first.fill(password)
    # press the button in the SAME form as the password box, else Enter
    form_btn = pw.first.locator("xpath=ancestor::form[1]").locator('button[type="submit"], input[type="submit"], button:not([type])')
    try:
        if form_btn.count():
            form_btn.first.click(timeout=8000)
        elif not _visible_click(page, SIGNIN_RE):
            pw.first.press("Enter")
    except Exception:  # noqa: BLE001
        pw.first.press("Enter")
    try:
        page.wait_for_load_state("networkidle", timeout=30000)
    except Exception:  # noqa: BLE001
        pass
    page.wait_for_timeout(2000)
    return page.locator('input[type="password"]:visible').count() == 0


def _upload_files(page, resume: Path, letter: Path):
    inputs = page.locator('input[type="file"]')
    for i in range(inputs.count()):
        el = inputs.nth(i)
        try:
            label = el.evaluate(LABEL_JS).lower()
        except Exception:  # noqa: BLE001
            label = ""
        target = letter if "cover" in label else resume
        try:
            el.set_input_files(str(target))
            log.info("Uploaded %s into '%s'", target.name, label[:60])
        except Exception as e:  # noqa: BLE001
            log.info("Upload into '%s' failed: %s", label[:60], e)


def _answer(q, opts, profile, saved, resume_text, model, sources):
    ans, src = matcher.answer_question(q, opts, profile, saved, resume_text, model)
    if ans is not None:
        sources[q] = src
    return ans


def _fill_required(page, job, profile, resume_text, model, saved: dict, sources: dict) -> list[str]:
    """Fill empty required fields. Returns questions we couldn't answer."""
    unanswered = []

    fields = page.locator(
        'input[required]:visible, input[aria-required="true"]:visible, '
        'select[required]:visible, select[aria-required="true"]:visible, '
        'textarea[required]:visible, textarea[aria-required="true"]:visible'
    )
    for i in range(fields.count()):
        el = fields.nth(i)
        try:
            tag = el.evaluate("e => e.tagName.toLowerCase()")
            typ = (el.get_attribute("type") or "").lower()
            if typ in ("file", "hidden", "password", "submit"):
                continue
            q = el.evaluate(LABEL_JS)
            if typ in ("radio", "checkbox"):
                continue  # handled below as groups
            if tag == "select":
                if el.evaluate("e => e.selectedIndex > 0"):
                    continue
                opts = [o.strip() for o in el.locator("option").all_inner_texts() if o.strip()]
                opts = [o for o in opts if not re.match(r"^(select|choose|--|please)", o, re.I)]
                ans = _answer(q, opts, profile, saved, resume_text, model, sources)
                if ans and ans in opts:
                    el.select_option(label=ans)
                else:
                    unanswered.append(q)
            else:
                if el.input_value().strip():
                    continue
                if "cover" in q.lower() and tag == "textarea":
                    el.fill(job.get("cover_letter") or "")
                    continue
                ans = _answer(q, [], profile, saved, resume_text, model, sources)
                if ans:
                    el.fill(ans)
                else:
                    unanswered.append(q)
        except Exception as e:  # noqa: BLE001
            log.info("Field %d skipped: %s", i, e)

    # Radio-button questions: group by name, answer any group with nothing ticked
    names = set(page.locator('input[type="radio"]:visible').evaluate_all("els => els.map(e => e.name)"))
    for name in names:
        group = page.locator(f'input[type="radio"][name="{name}"]')
        if group.evaluate_all("els => els.some(e => e.checked)"):
            continue
        labels = [group.nth(k).evaluate(LABEL_JS) for k in range(group.count())]
        q = group.first.evaluate(
            "e => { const f = e.closest('fieldset'); const l = f && f.querySelector('legend');"
            " let p = e.closest('[role=radiogroup]') || (f || e.parentElement.parentElement);"
            " return ((l && l.innerText) || (p && p.innerText) || e.name).replace(/\\s+/g,' ').trim().slice(0,300); }"
        )
        ans = _answer(q, labels, profile, saved, resume_text, model, sources)
        if ans and ans in labels:
            group.nth(labels.index(ans)).check()
        else:
            unanswered.append(q)
    return unanswered


SIGNIN_LINK_RE = re.compile(r"(candidate )?(sign ?in|log ?in|login|my profile|view profile)", re.I)
SIGNED_IN_RE = re.compile(r"(sign ?out|log ?out|my profile|my applications|welcome,)", re.I)


VERIFY_RE = re.compile(
    r"(captcha|i'?m not a robot|verify (that )?you are (a )?human|are you a robot|verification code|one[- ]time (pass)?(code|password)"
    r"|enter the code|\d-digit code|code (we|that we|we've|we have) (sent|emailed|texted)|security (check|challenge)"
    r"|confirm you are not a bot|human verification)", re.I)
VERIFY_SEL = ('iframe[src*="captcha" i], iframe[title*="captcha" i], [class*="captcha" i], [id*="captcha" i], '
              '.cf-turnstile, iframe[src*="challenges.cloudflare.com"], iframe[title*="challenge" i], '
              '[id*="arkose" i], [class*="arkose" i], iframe[src*="arkoselabs" i], [id*="funcaptcha" i], '
              'input[autocomplete="one-time-code"], input[name*="otp" i]')

# Hosts where the robot may type the applicant's hospital password: the job's own host, plus
# SAP SuccessFactors' candidate sign-in domains (CSB sites may hand sign-in to these).
AUTH_HOST_SUFFIXES = (".successfactors.com", ".successfactors.eu", ".sapsf.com", ".sapsf.eu")


def _host(url: str) -> str:
    from urllib.parse import urlsplit
    return (urlsplit(url).hostname or "").lower()


def _trusted_page(page, job_url: str) -> bool:
    h = _host(page.url)
    return h == _host(job_url) or h.endswith(AUTH_HOST_SUFFIXES)


def _blocked_by_verification(page) -> bool:
    try:
        if page.locator(VERIFY_SEL).count():
            return True
        return bool(VERIFY_RE.search(page.inner_text("body")[:20000]))
    except Exception:  # noqa: BLE001
        return False


def _confirmed(before_text: str, page, before_url: str, submit_btn) -> bool:
    """Success only if a confirmation phrase APPEARS (wasn't on the form already) AND the form is gone
    or the page changed. Pre-existing text like "once your application has been submitted..." is ignored."""
    try:
        after = page.inner_text("body")
    except Exception:  # noqa: BLE001
        return False
    old = {ln.strip() for ln in before_text.splitlines() if ln.strip()}
    new_text = "\n".join(ln for ln in after.splitlines() if ln.strip() and ln.strip() not in old)
    if not DONE_RE.search(new_text):
        return False
    try:
        button_gone = submit_btn.count() == 0 or not submit_btn.is_visible()
    except Exception:  # noqa: BLE001
        button_gone = True
    return button_gone or page.url != before_url


def _open(p, state: dict | None):
    browser = p.chromium.launch(headless=True)
    ctx = browser.new_context(storage_state=state or None, viewport={"width": 1280, "height": 1800})
    return browser, ctx, ctx.new_page()


def _shot(page) -> bytes:
    try:
        return page.screenshot(full_page=True)
    except Exception:  # noqa: BLE001
        return b""


def test_login(careers_url: str, email: str, password: str, state: dict | None) -> dict:
    """Signs in on the careers site. Returns {"ok", "note", "shot": png bytes, "state": session}."""
    if not (email and password):
        return {"ok": False, "note": "No email/password saved for this hospital yet", "shot": b"", "state": state}
    with sync_playwright() as p:
        browser, ctx, page = _open(p, None)  # always test from a clean session
        try:
            page.goto(careers_url, wait_until="networkidle", timeout=60000)
            if not _open_signin(page):
                return {"ok": False, "shot": _shot(page), "state": state,
                        "note": "Couldn't find the sign-in form. " + _what_i_see(page)}
            ok = _login_if_needed(page, email, password)
            page.wait_for_timeout(2000)
            body = page.inner_text("body").lower()
            shot = _shot(page)
            if not ok:
                if re.search(r"incorrect|invalid|not recogni|failed|try again", body):
                    return {"ok": False, "note": "The site said the email or password is wrong", "shot": shot, "state": state}
                return {"ok": False, "note": "Still on the sign-in page after entering the details. " + _what_i_see(page), "shot": shot, "state": state}
            if re.search(r"verification code|one[- ]time|enter the code|verify your", body):
                return {"ok": False, "shot": shot, "state": state,
                        "note": "The site asked for a verification code, which the robot can't receive yet."}
            return {"ok": True, "note": "Signed in successfully", "shot": shot, "state": ctx.storage_state()}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "note": f"Error: {str(e)[:200]} | {_what_i_see(page)}", "shot": _shot(page), "state": state}
        finally:
            browser.close()


def apply(job: dict, email: str, password: str, resume: Path, profile: dict, answers: dict,
          resume_text: str, model: str, submit: bool, state: dict | None,
          before_submit=None) -> dict:
    """Fill (and, if submit=True, submit) one application.

    Returns {"status", "note", "questions", "answer_sources", "shot", "state"} where status is one of
    ready_for_review (filled, not submitted), needs_info, submitted_confirmed, submission_unknown,
    blocked, failed, skipped.

    before_submit() is called immediately before the Submit click and MUST durably record the
    attempt; if it raises, nothing is clicked. A click without a recognised confirmation page is
    reported as submission_unknown - never as success, and never retried automatically.
    """
    letter = _cover_letter_file(job)
    with sync_playwright() as p:
        browser, ctx, page = _open(p, state)

        sources: dict = {}

        def done(status, note, questions=None, keep_state=True):
            return {"status": status, "note": note, "questions": questions or [], "answer_sources": sources, "shot": _shot(page),
                    "state": ctx.storage_state() if keep_state else state}

        try:
            page.goto(job["url"], wait_until="networkidle", timeout=60000)
            _dismiss_cookies(page)
            if "position has been filled" in page.content().lower():
                return done("skipped", "Position already filled")

            if not _click_text(page, APPLY_RE):
                page.locator('a.dialogApplyBtn, a[class*="apply" i], button[class*="apply" i]').first.click(timeout=8000)
            page.wait_for_load_state("networkidle", timeout=30000)

            if _blocked_by_verification(page):
                return done("blocked", "The site asked for a CAPTCHA or verification step. The robot never bypasses these - please apply yourself.")
            if not _trusted_page(page, job["url"]):
                return done("blocked", f"The application link went to an unexpected site ({_host(page.url)}). Nothing was entered.", keep_state=False)
            if not _login_if_needed(page, email, password):
                return done("blocked", "Couldn't log in - check this hospital's login in the app", keep_state=False)

            # NOTE: no consent/"accept" clicking and no second "Apply" click on the application page:
            # on some sites those buttons SEND the application. The only exception is being bounced
            # back to the job ad itself after signing in.
            if page.locator('input[type="file"]').count() == 0 and _same_page(page.url, job["url"]):
                _click_text(page, APPLY_RE, timeout=3000)
                page.wait_for_load_state("networkidle", timeout=30000)
            if not _trusted_page(page, job["url"]):
                return done("blocked", f"The application went to an unexpected site ({_host(page.url)}). Nothing was entered.")
            if page.locator('input[type="file"]').count() == 0:
                return done("blocked", "The application form wasn't recognised, so the robot stopped without clicking anything. Please apply yourself.")

            _upload_files(page, resume, letter)
            if _blocked_by_verification(page):
                return done("blocked", "The site asked for a CAPTCHA or verification step. The robot never bypasses these - please apply yourself.")
            unanswered = list(dict.fromkeys(_fill_required(page, job, profile, resume_text, model, answers, sources)))

            if unanswered:
                return done("needs_info", "Needs your answer:\n- " + "\n- ".join(unanswered), unanswered)
            if not submit:
                return done("ready_for_review", "Form filled - not submitted")

            submit_btn = None
            for role in ("button", "link"):
                loc = page.get_by_role(role, name=SUBMIT_RE)
                if loc.count() == 1:
                    submit_btn = loc.first
                    break
            if submit_btn is None:
                return done("blocked", "Couldn't find exactly one Submit button - nothing was submitted")
            before_text, before_url = page.inner_text("body"), page.url
            before_submit()  # durable "attempted" marker; raises -> we never click
            submit_btn.click(timeout=15000)
            page.wait_for_load_state("networkidle", timeout=60000)
            page.wait_for_timeout(3000)
            if _confirmed(before_text, page, before_url, submit_btn):
                return done("submitted_confirmed", "The hospital site showed a submission confirmation")
            return done("submission_unknown", "Submit was clicked but no confirmation was shown. Check your account on the hospital site; this will NOT be retried automatically.")
        except PWTimeout as e:
            return done("failed", f"Page timed out: {e}")
        except Exception as e:  # noqa: BLE001
            log.exception("Apply failed")
            return done("failed", f"Error: {e}")
        finally:
            browser.close()
