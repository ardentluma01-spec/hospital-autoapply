"""Stores your hospital logins encrypted on the server.

Passwords are encrypted with a key kept in data/secret.key (created automatically,
readable only by the app). They are never shown back in the dashboard.
"""
import os

from cryptography.fernet import Fernet

from . import db
from .settings import DATA

KEY_FILE = DATA / "secret.key"


def _fernet() -> Fernet:
    if not KEY_FILE.exists():
        KEY_FILE.write_bytes(Fernet.generate_key())
        os.chmod(KEY_FILE, 0o600)
    return Fernet(KEY_FILE.read_bytes())


def save_login(login_env: str, email: str, password: str | None):
    """password=None keeps the existing saved password."""
    logins = db.get_kv("logins", {})
    entry = logins.get(login_env, {})
    entry["email"] = email.strip()
    if password:
        entry["password"] = _fernet().encrypt(password.encode()).decode()
    logins[login_env] = entry
    db.set_kv("logins", logins)


def get_login(login_env: str) -> tuple[str, str]:
    entry = db.get_kv("logins", {}).get(login_env, {})
    pw = ""
    if entry.get("password"):
        try:
            pw = _fernet().decrypt(entry["password"].encode()).decode()
        except Exception:  # noqa: BLE001
            pw = ""
    return entry.get("email", ""), pw


def summary() -> dict:
    """{login_env: {"email": ..., "has_password": bool}} for the dashboard."""
    return {k: {"email": v.get("email", ""), "has_password": bool(v.get("password"))}
            for k, v in db.get_kv("logins", {}).items()}
