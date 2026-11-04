"""Tiny SQLite store for jobs and profile."""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime

from .settings import DATA

DB_PATH = DATA / "autoapply.db"

# Job status flow:
#   new -> skipped (low match) | needs_info (a question only you can answer) | blocked | failed | watch_only
#   new -> ready_for_review (form filled, NOT submitted) -> [you tap Approve] -> approved -> submitting
#        -> submitted_confirmed | submission_unknown (clicked, no confirmation: never retried)
#   you can also: applied_by_user ("I applied myself"), cancelled ("skip")


@contextmanager
def conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init():
    with conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT UNIQUE,
                hospital TEXT,
                title TEXT,
                location TEXT,
                posted TEXT,
                description TEXT,
                status TEXT DEFAULT 'new',
                score INTEGER,
                reason TEXT,
                cover_letter TEXT,
                screenshot TEXT,
                note TEXT,
                questions TEXT,
                letter_warnings TEXT,
                job_key TEXT UNIQUE,
                submit_attempted_at TEXT,
                found_at TEXT,
                updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
            """
        )


def now() -> str:
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("Australia/Melbourne")).strftime("%a %d %b, %-I:%M %p")


def add_job(job: dict) -> int | None:
    """Insert if unseen. Returns new id, or None if we've seen this vacancy before (same URL or job key)."""
    with conn() as c:
        try:
            cur = c.execute(
                "INSERT INTO jobs (url, job_key, hospital, title, location, posted, description, found_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (job["url"], job.get("job_key"), job["hospital"], job["title"], job.get("location", ""),
                 job.get("posted", ""), job.get("description", ""), now(), now()),
            )
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None


def update_job(job_id: int, **fields):
    fields["updated_at"] = now()
    cols = ", ".join(f"{k}=?" for k in fields)
    with conn() as c:
        c.execute(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), job_id))


def update_job_if(job_id: int, where: dict, **fields) -> bool:
    """Update only if the row still matches `where` (None = IS NULL). Returns False if it didn't."""
    fields["updated_at"] = now()
    cols = ", ".join(f"{k}=?" for k in fields)
    conds = " AND ".join(f"{k} IS NULL" if v is None else f"{k}=?" for k, v in where.items())
    vals = [v for v in where.values() if v is not None]
    with conn() as c:
        cur = c.execute(f"UPDATE jobs SET {cols} WHERE id=? AND {conds}", (*fields.values(), job_id, *vals))
        return cur.rowcount == 1


def get_job(job_id: int) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return dict(r) if r else None


def list_jobs(limit: int = 200) -> list[dict]:
    with conn() as c:
        rows = c.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]


def get_kv(k: str, default=None):
    with conn() as c:
        r = c.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return json.loads(r["v"]) if r else default


def set_kv(k: str, v):
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO kv (k, v) VALUES (?, ?)", (k, json.dumps(v)))
