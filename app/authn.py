"""Dashboard sign-in: password once per device, then a PIN.

* "Device" cookie (180 days): a random token; only its hash is stored on the server.
* "Unlocked" cookie (12 hours): HMAC-signed, tied to the device.
* PIN: 4-6 digits, stored as a salted PBKDF2 hash. 5 wrong PINs -> that device is forgotten
  and needs the full password again.
* Changing the password signs out every device.
"""
import hashlib
import hmac
import os
import secrets
import time

from . import db
from .settings import DATA

DEVICE_COOKIE, SESSION_COOKIE = "pa_device", "pa_unlock"
DEVICE_DAYS, UNLOCK_HOURS, MAX_PIN_TRIES = 180, 12, 5


def _key() -> bytes:
    f = DATA / "session.key"
    if not f.exists():
        f.write_bytes(secrets.token_bytes(32))
        os.chmod(f, 0o600)
    return f.read_bytes()


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _devices() -> dict:
    return db.get_kv("devices", {})


def new_device(label: str) -> str:
    token = secrets.token_urlsafe(32)
    d = _devices()
    d[_h(token)] = {"label": label[:80], "created": db.now(), "pin_fails": 0}
    db.set_kv("devices", d)
    return token


def device_ok(token: str | None) -> bool:
    return bool(token) and _h(token) in _devices()


def forget_device(token: str | None):
    d = _devices()
    if token and d.pop(_h(token), None) is not None:
        db.set_kv("devices", d)


def forget_all_devices():
    db.set_kv("devices", {})


def session_value(token: str) -> str:
    exp = str(int(time.time()) + UNLOCK_HOURS * 3600)
    sig = hmac.new(_key(), f"{exp}|{_h(token)}".encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def session_ok(value: str | None, token: str | None) -> bool:
    if not (value and token and device_ok(token)) or "." not in value:
        return False
    exp, sig = value.split(".", 1)
    good = hmac.new(_key(), f"{exp}|{_h(token)}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, good) and exp.isdigit() and int(exp) > time.time()


# ---------- PIN ----------
def pin_set() -> bool:
    return bool(db.get_kv("pin"))


def set_pin(pin: str):
    salt = secrets.token_hex(16)
    db.set_kv("pin", {"salt": salt, "hash": hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt), 200_000).hex()})


def clear_pin():
    db.set_kv("pin", None)


def check_pin(pin: str, token: str) -> tuple[bool, int]:
    """Returns (ok, tries_left). Too many wrong tries forgets this device."""
    p = db.get_kv("pin")
    d = _devices()
    rec = d.get(_h(token))
    if not p or rec is None:
        return False, 0
    ok = hmac.compare_digest(hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(p["salt"]), 200_000).hex(), p["hash"])
    if ok:
        rec["pin_fails"] = 0
        db.set_kv("devices", d)
        return True, MAX_PIN_TRIES
    rec["pin_fails"] = rec.get("pin_fails", 0) + 1
    left = MAX_PIN_TRIES - rec["pin_fails"]
    if left <= 0:
        d.pop(_h(token), None)
    db.set_kv("devices", d)
    return False, max(left, 0)


def valid_pin(pin: str) -> bool:
    return pin.isdigit() and 4 <= len(pin) <= 6
