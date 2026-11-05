"""The main loop: find jobs -> filter -> AI score + cover letter -> fill form (not submit) -> email you.
You tap Approve -> it submits.

Safety rules (from the audit):
 * Nothing is ever submitted without your Approve (unless you set mode: auto in config.yaml).
 * Before clicking Submit the attempt is recorded; a job with a recorded attempt can never be submitted again.
 * A click without a confirmation page = "check needed" (submission_unknown) - never retried automatically.
 * The AI never answers factual questions (certificates, checks, work rights...). You answer those once;
   they're saved and reused.
"""
import json
import logging
import os
import threading
from pathlib import Path

from . import db, matcher, notify, resume, sources
from .applier import successfactors
from .settings import DATA, config, env, login_for

log = logging.getLogger("pipeline")
_lock = threading.RLock()
SESSIONS = DATA / "sessions"
SESSIONS.mkdir(exist_ok=True)


def _link(job_id: int) -> str:
    return f"{env('PUBLIC_URL', 'http://localhost:8000').rstrip('/')}/job/{job_id}"


def _hospital(job: dict, cfg: dict) -> dict:
    return next((h for h in cfg["hospitals"] if h["name"] == job["hospital"]), {})


def _load_state(login_env: str) -> dict | None:
    f = SESSIONS / f"{login_env or 'default'}.json"
    try:
        return json.loads(f.read_text()) if f.exists() else None
    except Exception:  # noqa: BLE001
        return None


def _save_state(login_env: str, state: dict | None):
    if state:
        f = SESSIONS / f"{login_env or 'default'}.json"
        f.write_text(json.dumps(state))
        os.chmod(f, 0o600)


def _save_shot(job_id: int, png: bytes, suffix: str = "") -> str:
    if not png:
        return ""
    path = DATA / "screenshots" / f"job_{job_id}{suffix}.png"
    path.write_bytes(png)
    return str(path)


def recover_after_restart():
    """Called at startup. Anything mid-submit when the app stopped is 'check needed', never re-run."""
    for j in db.list_jobs(1000):
        if j["status"] == "submitting" or (j["submit_attempted_at"] and j["status"] == "approved"):
            db.update_job(j["id"], status="submission_unknown",
                          note="The app restarted during submission. Check your account on the hospital site; this will NOT be retried.")
        elif j["status"] in ("approved", "prefilling"):
            db.update_job(j["id"], status="ready_for_review", note="The app restarted before this ran. Tap Approve again if you still want it.")


def check_for_jobs() -> str:
    if not _lock.acquire(blocking=False):
        return "A check is already running"
    try:
        cfg = config()
        if not resume.current():
            return "Upload your resume first"
        report = {}
        listings = sources.fetch_all(cfg, report)
        new = 0
        for j in listings:
            if not sources.title_matches(j["title"], cfg):
                continue
            r = report.get(next((h["name"] for h in cfg["hospitals"] if h.get("login_env") == j.get("login_env")), ""))
            if r is not None:
                r["matches"] = r.get("matches", 0) + 1
            j["job_key"] = sources.job_key(j["url"])
            j["url"] = sources.clean_url(j["url"])
            j["hospital"] = next((h["name"] for h in cfg["hospitals"] if h.get("login_env") == j.get("login_env")), j.get("hospital", ""))
            job_id = db.add_job(j)
            if job_id is None:
                continue  # seen before
            new += 1
            try:
                process(job_id, cfg)
            except Exception as e:  # noqa: BLE001
                log.exception("Processing %s failed", j["url"])
                db.update_job(job_id, status="failed", note=f"Error: {type(e).__name__}")
        hidden = hide_non_matching(cfg)
        retried = retry_failed_assessments(cfg)
        db.set_kv("last_check", db.now())
        db.set_kv("last_report", report)
        return f"Checked {len(listings)} listings, {new} new matching jobs, {retried} retried, {hidden} hidden"
    finally:
        _lock.release()


UNTOUCHED = ("new", "failed", "needs_info", "skipped", "blocked", "ready_for_review", "watch_only")


def hide_non_matching(cfg: dict) -> int:
    """Jobs found under older/looser title rules that no longer match are moved to 'Not a match'.
    Anything approved, being submitted or submitted is never touched."""
    n = 0
    for j in db.list_jobs(2000):
        if j["status"] in UNTOUCHED and not j["submit_attempted_at"] and not sources.title_matches(j["title"] or "", cfg):
            db.update_job(j["id"], status="cancelled", note="Hidden: doesn't match the job titles you're looking for")
            n += 1
    return n


def retry_failed_assessments(cfg: dict, limit: int = 10) -> int:
    """Re-run jobs whose AI check failed (e.g. no credit at the time). Never touches submissions."""
    n = 0
    for j in db.list_jobs(2000):
        if n >= limit:
            break
        if j["status"] == "failed" and j["score"] is None and not j["submit_attempted_at"]:
            db.update_job(j["id"], status="new", note="")
            try:
                process(j["id"], cfg)
            except Exception as e:  # noqa: BLE001
                db.update_job(j["id"], status="failed", note=f"Error: {type(e).__name__}")
            n += 1
    return n


def process(job_id: int, cfg: dict):
    job = db.get_job(job_id)
    if not job["description"]:
        job["description"] = sources.job_description(job["url"])
        db.update_job(job_id, description=job["description"])
    if not job["description"]:
        db.update_job(job_id, status="failed", note="Couldn't read the job ad (it may have been withdrawn).")
        return

    profile = db.get_kv("profile", {})
    rtext = resume.text()
    if not rtext.strip():
        db.update_job(job_id, status="needs_info", note="Your resume couldn't be read. Upload a text-based PDF or Word file.")
        return
    result = matcher.assess(job, rtext, profile, cfg["ai_model"], cfg.get("job_preference", ""))
    if not result["ok"]:
        db.update_job(job_id, status="failed", note=result["reason"])
        return
    db.update_job(job_id, score=result["score"], reason=result["reason"], cover_letter=result["cover_letter"],
                  letter_warnings=json.dumps(result["warnings"]))
    job.update(result)

    if result["score"] < cfg.get("min_match_score", 65):
        db.update_job(job_id, status="skipped", note="Not a strong enough match")
        return
    h = _hospital(job, cfg)
    if h.get("platform") != "successfactors":
        db.update_job(job_id, status="watch_only", note="This site isn't automated - apply via the link")
        notify.send(f"New match ({result['score']}%): {job['title']} - {job['hospital']}",
                    f"{result['reason']}\n\nApply here: {job['url']}\nCover letter: {_link(job_id)}")
        return

    out = _run(job, h, submit=False)            # fill the form, never submit
    if out["status"] == "ready_for_review" and cfg.get("mode") == "auto":
        approve(job_id)


def _run(job: dict, h: dict, submit: bool) -> dict:
    """Fill (submit=False) or submit (submit=True, only from status 'approved') one application."""
    cfg = config()
    login_env = h.get("login_env", "")
    email, password = login_for(login_env)
    if not (email and password):
        db.update_job(job["id"], status="needs_info", note=f"Add your {h.get('name', 'hospital')} login on the dashboard first.")
        return {"status": "needs_info"}
    job = db.get_job(job["id"])
    profile, answers = db.get_kv("profile", {}), db.get_kv("answers", {})

    def before_submit():
        # Recorded BEFORE the click, and only if no attempt exists: this job can never be submitted twice.
        if not db.update_job_if(job["id"], {"status": "approved", "submit_attempted_at": None},
                                status="submitting", submit_attempted_at=db.now()):
            raise RuntimeError("could not claim the submit attempt (already attempted or changed); not clicking")
        job["submit_attempted_at"] = "yes"

    out = successfactors.apply(job, email, password, resume.current(), profile, answers, resume.text(),
                               cfg["ai_model"], submit=submit, state=_load_state(login_env), before_submit=before_submit)
    status, note = out["status"], out["note"]
    if submit and not job.get("submit_attempted_at") and "could not claim" in note:
        return {"status": "refused", "note": "Already being handled"}   # leave the job exactly as it is
    if job.get("submit_attempted_at") and status != "submitted_confirmed":
        status = "submission_unknown"
        note = f"Submit was clicked but not confirmed ({note}). Check your account on the hospital site. This will NOT be retried."
    _save_state(login_env, out.get("state"))
    shot = _save_shot(job["id"], out.get("shot", b""), "_submitted" if submit else "")
    db.update_job(job["id"], status=status, note=note[:1500], questions=json.dumps(out.get("questions", [])),
                  **({"screenshot": shot} if shot else {}))

    head = f"{job['title']} - {job['hospital']}"
    png = out.get("shot") or None
    if status == "submitted_confirmed":
        notify.send(f"Applied: {head}", f"The hospital site confirmed your application.\n\nDetails: {_link(job['id'])}", png)
    elif status == "submission_unknown":
        notify.send(f"Please check: {head}", f"{note}\n\n{_link(job['id'])}", png)
    elif status == "ready_for_review":
        notify.send(f"Ready to submit ({job.get('score')}%): {head}",
                    f"{job.get('reason', '')}\n\nThe form is filled in but NOT submitted. Check the screenshot and tap Approve:\n{_link(job['id'])}", png)
    elif status in ("needs_info", "blocked", "failed"):
        notify.send(f"Needs you: {head}", f"{note}\n\nOpen: {_link(job['id'])}\nJob: {job['url']}", png)
    return {"status": status, "note": note}


def approve(job_id: int) -> str:
    """You tapped 'Approve & submit'."""
    with _lock:
        job = db.get_job(job_id)
        if not job or job["submit_attempted_at"]:
            return "Already attempted - check the hospital site"
        if not db.update_job_if(job_id, {"status": "ready_for_review", "submit_attempted_at": None}, status="approved"):
            return "This job isn't ready to submit"
        return _run(job, _hospital(job, config()), submit=True)["note"]


def retry(job_id: int) -> str:
    """Re-fill the form (never submits), e.g. after you answered a question or edited the letter."""
    with _lock:
        job = db.get_job(job_id)
        if not job or job["submit_attempted_at"] or job["status"] in ("submitting", "submitted_confirmed", "applied_by_user"):
            return "Can't re-fill this job"
        db.update_job(job_id, status="prefilling", note="Re-filling...")
        return _run(job, _hospital(job, config()), submit=False)["note"]


def test_login(login_env: str, careers_url: str) -> dict:
    email, password = login_for(login_env)
    out = successfactors.test_login(careers_url, email, password, _load_state(login_env))
    if out["ok"]:
        _save_state(login_env, out.get("state"))
    shot = DATA / "screenshots" / f"login_{login_env}.png"
    if out.get("shot"):
        shot.write_bytes(out["shot"])
    return {"ok": out["ok"], "note": out["note"], "screenshot": str(shot) if out.get("shot") else ""}


def purge_old_screenshots(days: int = 30):
    import time
    cutoff = time.time() - days * 86400
    for f in Path(DATA / "screenshots").glob("*.png"):
        if f.stat().st_mtime < cutoff:
            f.unlink(missing_ok=True)
