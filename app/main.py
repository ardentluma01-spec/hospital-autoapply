"""Web dashboard + background scheduler.  Run:  uvicorn app.main:app --host 0.0.0.0 --port 8000"""
import logging
import os
import re
import secrets
import threading
import time
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

from . import authn, db, pipeline, resume, vault
from .settings import ROOT, config, env

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
db.init()

app = FastAPI(title="PulseApply")
from fastapi.staticfiles import StaticFiles  # noqa: E402
app.mount("/static", StaticFiles(directory=str(ROOT / "app" / "static")), name="static")
templates = Jinja2Templates(directory=str(ROOT / "app" / "templates"))
security = HTTPBasic(auto_error=False)

LABELS = {"new": "found", "skipped": "not a match", "needs_info": "needs your answer", "ready_for_review": "ready - tap Approve",
          "prefilling": "filling form...", "approved": "approved", "submitting": "submitting...", "submitted_confirmed": "submitted (confirmed)",
          "submission_unknown": "check needed", "not_submitted": "not submitted", "applied_by_user": "you applied", "blocked": "blocked",
          "failed": "failed", "cancelled": "skipped by you", "watch_only": "apply via link"}

PROFILE_FIELDS = [
    ("full_name", "Full name"),
    ("email", "Email"),
    ("phone", "Mobile"),
    ("suburb", "Suburb"),
    ("work_rights", "Work rights (e.g. Australian citizen / PR / visa type)"),
    ("cert3_hsa", "Certificate III Health Services Assistance? (yes/no + year)"),
    ("ahpra", "AHPRA registration (number + type, or 'none')"),
    ("wwcc", "Working With Children Check (Employee, number, expiry, or 'none')"),
    ("police_check", "Police check (date, or 'willing to obtain')"),
    ("drivers_licence", "Driver's licence (yes/no)"),
    ("availability", "Availability (days, nights, weekends, start date)"),
    ("employment_type", "Looking for (casual / part-time / full-time)"),
    ("extra", "Anything else the bot should know"),
]


# ---------- first-run setup (phone-friendly: no terminal needed) ----------
PLACEHOLDER_PW = "change-this-to-something-long"
_setup_failures = {"n": 0}


def configured() -> bool:
    pw = env("DASHBOARD_PASSWORD")
    return bool(pw) and pw != PLACEHOLDER_PW


def _write_env(updates: dict):
    """Update keys in .env (keeping everything else) and in this running process."""
    path = ROOT / ".env"
    lines = path.read_text().splitlines() if path.exists() else []
    done = set()
    for i, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if key in updates:
            lines[i] = f"{key}={updates[key]}"
            done.add(key)
    lines += [f"{k}={v}" for k, v in updates.items() if k not in done]
    path.write_text("\n".join(lines) + "\n")
    os.chmod(path, 0o600)
    os.environ.update({k: str(v) for k, v in updates.items()})


@app.middleware("http")
async def require_setup(request: Request, call_next):
    if not configured() and request.url.path not in ("/setup", "/health") and not request.url.path.startswith("/static/"):
        return RedirectResponse("/setup", 303)
    return await call_next(request)


@app.get("/setup")
def setup_page(request: Request, msg: str = ""):
    if configured():
        return RedirectResponse("/", 303)
    return templates.TemplateResponse(request, "setup.html", {"msg": msg})


@app.post("/setup")
def setup_save(setup_code: str = Form(...), dashboard_password: str = Form(...), anthropic_key: str = Form(...),
               gmail: str = Form(...), gmail_app_password: str = Form(...), notify_to: str = Form("")):
    if configured():
        return RedirectResponse("/", 303)
    expected = env("SETUP_CODE")
    if _setup_failures["n"] >= 10:
        return RedirectResponse("/setup?msg=Too many wrong codes. Restart the server from Vultr to try again.", 303)
    if not expected or not secrets.compare_digest(setup_code.strip(), expected):
        _setup_failures["n"] += 1
        time.sleep(2)
        return RedirectResponse("/setup?msg=That setup code is wrong (it's the one you typed in the Vultr startup script)", 303)
    if len(dashboard_password) < 10:
        return RedirectResponse("/setup?msg=Choose a dashboard password of at least 10 characters", 303)
    if not anthropic_key.strip().startswith("sk-ant-"):
        return RedirectResponse("/setup?msg=That doesn't look like a Claude API key (it starts with sk-ant-)", 303)
    _write_env({
        "DASHBOARD_USER": "me", "DASHBOARD_PASSWORD": dashboard_password, "ANTHROPIC_API_KEY": anthropic_key.strip(),
        "GMAIL_ADDRESS": gmail.strip(), "GMAIL_APP_PASSWORD": gmail_app_password.replace(" ", "").strip(),
        "NOTIFY_TO": (notify_to or gmail).strip(), "SETUP_CODE": "",
    })
    return RedirectResponse("/login?msg=All set! Sign in with the password you just chose.", 303)


def _password_ok(pw: str) -> bool:
    real = env("DASHBOARD_PASSWORD", "")
    return bool(real) and secrets.compare_digest(pw or "", real)


def auth(request: Request, creds: HTTPBasicCredentials | None = Depends(security)):
    """Signed in = valid 'unlocked' cookie for a remembered device (or HTTP basic, kept for scripts)."""
    if creds and secrets.compare_digest(creds.username, env("DASHBOARD_USER", "me")) and _password_ok(creds.password):
        return
    dev = request.cookies.get(authn.DEVICE_COOKIE)
    if authn.session_ok(request.cookies.get(authn.SESSION_COOKIE), dev):
        return
    nxt = request.url.path if request.method == "GET" else "/"
    where = "/unlock" if authn.device_ok(dev) and authn.pin_set() else "/login"
    raise HTTPException(303, headers={"Location": f"{where}?next={nxt}"})


def _https(request: Request) -> bool:
    return request.headers.get("x-forwarded-proto", request.url.scheme) == "https"


def _signed_in(resp: RedirectResponse, request: Request, device: str, new_device: bool):
    sec = _https(request)
    if new_device:
        resp.set_cookie(authn.DEVICE_COOKIE, device, max_age=authn.DEVICE_DAYS * 86400, httponly=True, secure=sec, samesite="lax")
    resp.set_cookie(authn.SESSION_COOKIE, authn.session_value(device), max_age=authn.UNLOCK_HOURS * 3600,
                    httponly=True, secure=sec, samesite="lax")
    return resp


def _safe_next(nxt: str) -> str:
    return nxt if nxt.startswith("/") and not nxt.startswith("//") else "/"


_login_fails = {"n": 0}


@app.get("/login")
def login_page(request: Request, next: str = "/", msg: str = ""):
    return templates.TemplateResponse(request, "login.html", {"next": _safe_next(next), "msg": msg, "mode": "password"})


@app.post("/login")
def login_submit(request: Request, password: str = Form(...), next: str = Form("/")):
    if not _password_ok(password):
        _login_fails["n"] += 1
        time.sleep(min(5, _login_fails["n"]))
        return RedirectResponse(f"/login?msg=Wrong password&next={_safe_next(next)}", 303)
    _login_fails["n"] = 0
    authn.forget_device(request.cookies.get(authn.DEVICE_COOKIE))
    dev = authn.new_device(request.headers.get("user-agent", "device"))
    dest = _safe_next(next) if authn.pin_set() else "/settings?msg=Welcome! Set a PIN below so next time you only need the PIN.#pin"
    return _signed_in(RedirectResponse(dest, 303), request, dev, True)


@app.get("/unlock")
def unlock_page(request: Request, next: str = "/", msg: str = ""):
    if not (authn.device_ok(request.cookies.get(authn.DEVICE_COOKIE)) and authn.pin_set()):
        return RedirectResponse(f"/login?next={_safe_next(next)}", 303)
    return templates.TemplateResponse(request, "login.html", {"next": _safe_next(next), "msg": msg, "mode": "pin"})


@app.post("/unlock")
def unlock_submit(request: Request, pin: str = Form(...), next: str = Form("/")):
    dev = request.cookies.get(authn.DEVICE_COOKIE)
    ok, left = authn.check_pin(pin.strip(), dev or "")
    if ok:
        return _signed_in(RedirectResponse(_safe_next(next), 303), request, dev, False)
    time.sleep(1)
    if left <= 0:
        return RedirectResponse("/login?msg=Too many wrong PINs - please use your password", 303)
    return RedirectResponse(f"/unlock?msg=Wrong PIN ({left} tries left)&next={_safe_next(next)}", 303)


@app.post("/logout")
def logout(request: Request):
    authn.forget_device(request.cookies.get(authn.DEVICE_COOKIE))
    resp = RedirectResponse("/login?msg=Signed out", 303)
    resp.delete_cookie(authn.DEVICE_COOKIE)
    resp.delete_cookie(authn.SESSION_COOKIE)
    return resp


def _background(fn, *args):
    threading.Thread(target=fn, args=args, daemon=True).start()


# ---------- scheduler ----------
scheduler = BackgroundScheduler()


@app.on_event("startup")
def start_scheduler():
    pipeline.recover_after_restart()
    minutes = int(config().get("check_every_minutes", 60))
    scheduler.add_job(pipeline.check_for_jobs, "interval", minutes=minutes, id="check", replace_existing=True)
    scheduler.add_job(pipeline.purge_old_screenshots, "interval", hours=24, id="purge", replace_existing=True)
    scheduler.start()


# ---------- pages ----------
def _page(request: Request, tab: str, msg: str):
    cfg = config()
    r = resume.current()
    jobs = db.list_jobs()
    counts = {}
    for j in jobs:
        counts[j["status"]] = counts.get(j["status"], 0) + 1
    profile = db.get_kv("profile", {})
    logins = vault.summary()
    enabled = [h for h in cfg["hospitals"] if h.get("enabled")]
    steps = [
        ("Upload your resume", bool(r), "/profile#resume"),
        ("Fill in your profile", bool(profile.get("work_rights") and profile.get("phone")), "/profile#profile"),
        ("Add a hospital login", any((logins.get(h.get("login_env")) or {}).get("has_password") for h in enabled), "/logins"),
        ("Run your first check", db.get_kv("last_check") is not None, "/"),
    ]
    version = (ROOT / "VERSION").read_text().strip() if (ROOT / "VERSION").exists() else "dev"
    return templates.TemplateResponse(request, f"{tab}.html", {
        "tab": tab, "cfg": cfg, "resume": r.name if r else None, "jobs": jobs, "counts": counts,
        "profile": profile, "fields": PROFILE_FIELDS, "steps": steps, "setup_done": all(s[1] for s in steps), "version": version,
        "last_check": db.get_kv("last_check", "never"), "msg": msg,
        "logins": logins, "label": LABELS, "update": db.get_kv("update_status"),
        "pin_set": authn.pin_set(), "push_topic": env("NTFY_TOPIC"), "topic_suggestion": "pulseapply-" + secrets.token_hex(6),
        "notify_last": __import__("app.notify", fromlist=["LAST"]).LAST,
        "keys": {"ai": (lambda k: f"sk-ant-…{k[-4:]}" if k else "")(env("ANTHROPIC_API_KEY")),
                 "gmail": env("GMAIL_ADDRESS"), "gmail_pw": bool(env("GMAIL_APP_PASSWORD")), "notify_to": env("NOTIFY_TO")},
        "tests": {h.get("login_env"): db.get_kv(f"login_test:{h.get('login_env')}") for h in cfg["hospitals"]},
        "env_logins": {h.get("login_env"): bool(env(f"{h.get('login_env')}_PASSWORD")) for h in cfg["hospitals"]},
    })


@app.get("/", dependencies=[Depends(auth)])
def home(request: Request, msg: str = ""):
    return _page(request, "home", msg)


@app.get("/profile", dependencies=[Depends(auth)])
def profile_page(request: Request, msg: str = ""):
    return _page(request, "profile", msg)


@app.get("/settings", dependencies=[Depends(auth)])
def settings_page(request: Request, msg: str = ""):
    return _page(request, "settings", msg)


@app.post("/settings/keys", dependencies=[Depends(auth)])
def save_keys(anthropic_key: str = Form(""), gmail: str = Form(""), gmail_app_password: str = Form(""), notify_to: str = Form(""),
              ntfy_topic: str = Form("")):
    from . import matcher
    upd = {}
    key = anthropic_key.strip()
    if key:
        if not key.startswith("sk-ant-"):
            return RedirectResponse("/settings?msg=That doesn't look like a Claude API key (it starts with sk-ant-)#keys", 303)
        upd["ANTHROPIC_API_KEY"] = key
    if gmail.strip():
        upd["GMAIL_ADDRESS"] = gmail.strip()
    if gmail_app_password.strip():
        upd["GMAIL_APP_PASSWORD"] = gmail_app_password.replace(" ", "").strip()
    upd["NOTIFY_TO"] = (notify_to or gmail or env("GMAIL_ADDRESS")).strip()
    if ntfy_topic.strip():
        upd["NTFY_TOPIC"] = re.sub(r"[^A-Za-z0-9_-]", "", ntfy_topic)[:64]
    _write_env(upd)
    matcher._client = None      # use the new key straight away
    return RedirectResponse("/settings?msg=Saved. Tap Test Claude key to check it works.#keys", 303)


@app.post("/settings/test-ai", dependencies=[Depends(auth)])
def test_ai():
    from . import matcher
    try:
        matcher._client = None
        matcher.client().messages.create(model=config()["ai_model"], max_tokens=5, messages=[{"role": "user", "content": "Say OK"}])
        msg = "✓ The Claude key works."
    except Exception as e:  # noqa: BLE001
        msg = "Claude key problem: " + matcher.explain_ai_error(e)
    return RedirectResponse(f"/settings?msg={msg}#keys", 303)


@app.post("/settings/test-email", dependencies=[Depends(auth)])
def test_email():
    from . import notify
    from urllib.parse import quote
    ok = notify._email("PulseApply test email", "If you're reading this, email notifications work.", None)
    msg = f"✓ Test email sent to {env('NOTIFY_TO') or env('GMAIL_ADDRESS')}" if ok else "Email failed: " + notify.LAST["email"]
    return RedirectResponse(f"/settings?msg={quote(msg)}#keys", 303)


@app.post("/settings/test-push", dependencies=[Depends(auth)])
def test_push():
    from urllib.parse import quote
    from . import notify
    ok = notify._push("PulseApply test", "Phone notifications work. Tap to open your dashboard: " + env("PUBLIC_URL", ""))
    msg = "✓ Sent - check your phone" if ok else notify.LAST["push"]
    return RedirectResponse(f"/settings?msg={quote(msg)}#push", 303)


@app.post("/settings/password", dependencies=[Depends(auth)])
def change_password(current: str = Form(...), new: str = Form(...)):
    if not secrets.compare_digest(current, env("DASHBOARD_PASSWORD", "")):
        return RedirectResponse("/settings?msg=Current password is wrong#password", 303)
    if len(new) < 10:
        return RedirectResponse("/settings?msg=New password must be at least 10 characters#password", 303)
    _write_env({"DASHBOARD_PASSWORD": new})
    authn.forget_all_devices()
    return RedirectResponse("/login?msg=Password changed - sign in with the new one", 303)


@app.post("/settings/pin", dependencies=[Depends(auth)])
def save_pin(pin: str = Form(""), pin2: str = Form(""), remove: str = Form("")):
    if remove:
        authn.clear_pin()
        return RedirectResponse("/settings?msg=PIN removed - you'll use your password each time#pin", 303)
    if not authn.valid_pin(pin.strip()):
        return RedirectResponse("/settings?msg=The PIN must be 4 to 6 digits#pin", 303)
    if pin.strip() != pin2.strip():
        return RedirectResponse("/settings?msg=The two PINs didn't match#pin", 303)
    authn.set_pin(pin.strip())
    return RedirectResponse("/settings?msg=PIN saved. Next time on this phone, just enter your PIN.#pin", 303)


@app.get("/logins", dependencies=[Depends(auth)])
def logins_page(request: Request, msg: str = ""):
    return _page(request, "logins", msg)


def _do_update():
    from . import updater
    db.set_kv("update_status", {"state": "running", "at": db.now()})
    try:
        v = updater.update_from_github()
        db.set_kv("update_status", {"state": "done", "version": v, "at": db.now()})
    except Exception as e:  # noqa: BLE001
        db.set_kv("update_status", {"state": "failed", "error": str(e)[:200], "at": db.now()})
        return
    time.sleep(1)
    os._exit(0)   # systemd restarts the app on the new version within ~10 s


@app.post("/search", dependencies=[Depends(auth)])
def save_search(include: str = Form(""), exclude: str = Form(""), min_score: int = Form(65)):
    import yaml
    def lst(t):
        out = []
        for x in t.replace("\n", ",").split(","):
            x = x.strip().lower()
            if x and x not in out:
                out.append(x[:60])
        return out[:60]
    inc = lst(include)
    if not inc:
        return RedirectResponse("/settings?msg=Add at least one job title to look for#search", 303)
    path = ROOT / "config.yaml"
    cfg = yaml.safe_load(path.read_text())
    cfg["include_keywords"], cfg["exclude_keywords"] = inc, lst(exclude)
    cfg["min_match_score"] = max(0, min(100, int(min_score)))
    path.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True))
    hidden = pipeline.hide_non_matching(cfg)
    return RedirectResponse(f"/settings?msg=Saved. {hidden} job(s) that don't match were moved to 'Not a match'.#search", 303)


@app.post("/update", dependencies=[Depends(auth)])
def update_app():
    _background(_do_update)
    return RedirectResponse("/settings?msg=Updating - this takes 1-3 minutes, then refresh the page#app", 303)


@app.post("/resume", dependencies=[Depends(auth)])
async def upload_resume(file: UploadFile = File(...)):
    try:
        resume.save(file.filename, await file.read())
    except ValueError as e:
        return RedirectResponse(f"/profile?msg={e}#resume", 303)
    return RedirectResponse("/profile?msg=Resume uploaded#resume", 303)


@app.get("/resume/download", dependencies=[Depends(auth)])
def download_resume():
    r = resume.current()
    if not r:
        raise HTTPException(404)
    return FileResponse(r, filename=r.name)


@app.post("/profile", dependencies=[Depends(auth)])
async def save_profile(request: Request):
    form = await request.form()
    db.set_kv("profile", {k: str(form.get(k, "")).strip() for k, _ in PROFILE_FIELDS})
    return RedirectResponse("/profile?msg=Profile saved#profile", 303)


@app.post("/check", dependencies=[Depends(auth)])
def check_now():
    _background(pipeline.check_for_jobs)
    return RedirectResponse("/?msg=Checking now - refresh in a few minutes", 303)


@app.get("/job/{job_id}", dependencies=[Depends(auth)])
def job_page(request: Request, job_id: int, msg: str = ""):
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404)
    import json as _json
    questions = _json.loads(job["questions"] or "[]") if job["status"] == "needs_info" else []
    warnings = _json.loads(job["letter_warnings"] or "[]")
    return templates.TemplateResponse(request, "job.html", {
        "job": job, "questions": questions, "warnings": warnings, "label": LABELS, "msg": msg, "tab": "job", "can_approve": job["status"] == "ready_for_review" and not job["submit_attempted_at"],
        "counts": {},
        "has_shot": bool(job["screenshot"] and Path(job["screenshot"]).exists()),
    })


@app.get("/job/{job_id}/screenshot", dependencies=[Depends(auth)])
def screenshot(job_id: int):
    job = db.get_job(job_id)
    if not job or not job["screenshot"] or not Path(job["screenshot"]).exists():
        raise HTTPException(404)
    return FileResponse(job["screenshot"])


@app.post("/job/{job_id}/letter", dependencies=[Depends(auth)])
def save_letter(job_id: int, cover_letter: str = Form(...)):
    db.update_job(job_id, cover_letter=cover_letter)
    return RedirectResponse(f"/job/{job_id}?msg=Cover letter saved", 303)


@app.post("/job/{job_id}/approve", dependencies=[Depends(auth)])
def approve(job_id: int):
    job = db.get_job(job_id)
    if not job or job["status"] != "ready_for_review" or job["submit_attempted_at"]:
        return RedirectResponse(f"/job/{job_id}?msg=This job can't be submitted right now", 303)
    _background(pipeline.approve, job_id)
    return RedirectResponse(f"/job/{job_id}?msg=Submitting - refresh in a minute; you'll also get an email", 303)


@app.post("/job/{job_id}/checked", dependencies=[Depends(auth)])
def checked(job_id: int, outcome: str = Form(...)):
    """After a 'check needed' result you looked at the hospital site and tell us what happened."""
    job = db.get_job(job_id)
    if job and job["status"] == "submission_unknown":
        db.update_job(job_id, status="applied_by_user" if outcome == "yes" else "not_submitted",
                      note="You confirmed it was submitted" if outcome == "yes" else "You confirmed it was NOT submitted. Apply on the hospital site if you still want it.")
    return RedirectResponse(f"/job/{job_id}?msg=Thanks - updated", 303)


@app.post("/job/{job_id}/answers", dependencies=[Depends(auth)])
async def save_answers(request: Request, job_id: int):
    form = await request.form()
    saved = db.get_kv("answers", {})
    for k, v in form.items():
        if k.startswith("q::") and str(v).strip():
            saved[k[3:]] = str(v).strip()
    db.set_kv("answers", saved)
    _background(pipeline.retry, job_id)
    return RedirectResponse(f"/job/{job_id}?msg=Saved - retrying now", 303)


@app.post("/job/{job_id}/retry", dependencies=[Depends(auth)])
def retry(job_id: int):
    _background(pipeline.retry, job_id)
    return RedirectResponse(f"/job/{job_id}?msg=Retrying", 303)


@app.post("/job/{job_id}/skip", dependencies=[Depends(auth)])
def skip(job_id: int):
    job = db.get_job(job_id)
    if job and job["status"] not in ("approved", "submitting", "submitted_confirmed", "submission_unknown"):
        db.update_job(job_id, status="cancelled", note="Skipped by you")
    return RedirectResponse("/?msg=Job skipped", 303)


@app.post("/job/{job_id}/mark-applied", dependencies=[Depends(auth)])
def mark_applied(job_id: int):
    job = db.get_job(job_id)
    if job and job["status"] not in ("approved", "submitting"):
        db.update_job(job_id, status="applied_by_user", note="You applied yourself")
    return RedirectResponse(f"/job/{job_id}?msg=Marked as applied", 303)


# ---------- hospital logins ----------
def _hospital(login_env: str) -> dict:
    h = next((h for h in config()["hospitals"] if h.get("login_env") == login_env), None)
    if not h:
        raise HTTPException(404)
    return h


@app.post("/login/{login_env}", dependencies=[Depends(auth)])
def save_login(login_env: str, email: str = Form(""), password: str = Form("")):
    _hospital(login_env)
    vault.save_login(login_env, email, password or None)
    db.set_kv(f"login_test:{login_env}", None)
    return RedirectResponse(f"/logins?msg=Login saved for {login_env.title()} - tap Test login to check it", 303)


def _run_login_test(login_env: str):
    h = _hospital(login_env)
    db.set_kv(f"login_test:{login_env}", {"ok": None, "note": "Testing...", "at": db.now()})
    out = pipeline.test_login(login_env, h["careers_url"])
    out["at"] = db.now()
    db.set_kv(f"login_test:{login_env}", out)


@app.post("/login/{login_env}/test", dependencies=[Depends(auth)])
def test_login(login_env: str, email: str = Form(""), password: str = Form("")):
    h = _hospital(login_env)
    if email:  # save whatever was typed before testing
        vault.save_login(login_env, email, password or None)
    if h["platform"] != "successfactors" or "PASTE" in h["careers_url"]:
        return RedirectResponse("/logins?msg=Login testing isn't available for this hospital yet", 303)
    _background(_run_login_test, login_env)
    return RedirectResponse("/logins?msg=Testing login - refresh in about 30 seconds", 303)


@app.get("/login/{login_env}/screenshot", dependencies=[Depends(auth)])
def login_screenshot(login_env: str):
    t = db.get_kv(f"login_test:{login_env}") or {}
    if not t.get("screenshot") or not Path(t["screenshot"]).exists():
        raise HTTPException(404)
    return FileResponse(t["screenshot"])


@app.get("/health")
def health():
    return {"ok": True}
