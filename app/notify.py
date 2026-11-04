"""Notifications: Gmail email and/or free phone push via ntfy.sh.

Some new cloud servers have email ports blocked; phone push works over normal HTTPS, so it's a
reliable backup. Every attempt's result is kept in LAST so the Settings page can say what went wrong.
"""
import logging
import re
import smtplib
import ssl
from email.message import EmailMessage

from .settings import env

log = logging.getLogger("notify")
LAST = {"email": "", "push": ""}


def _explain_smtp(e: Exception) -> str:
    m = str(e).lower()
    if isinstance(e, smtplib.SMTPAuthenticationError) or "username and password not accepted" in m or "535" in m:
        return ("Gmail rejected the login. Use a Gmail *app password* (16 letters from myaccount.google.com → "
                "Security → App passwords), not your normal password, and check the Gmail address.")
    if isinstance(e, (TimeoutError, ConnectionRefusedError, OSError)) and ("timed out" in m or "refused" in m or "unreachable" in m):
        return ("Couldn't reach Gmail: your server's email ports look blocked (common on new Vultr accounts). "
                "Use phone notifications instead, or ask Vultr support to unblock SMTP.")
    if isinstance(e, smtplib.SMTPRecipientsRefused):
        return "Gmail refused the 'send notifications to' address. Check it for typos."
    return f"{type(e).__name__}: {str(e)[:150]}"


def _email(subject: str, body: str, png: bytes | None) -> bool:
    user, pw = env("GMAIL_ADDRESS").strip(), env("GMAIL_APP_PASSWORD").replace(" ", "").strip()
    to = (env("NOTIFY_TO") or user).strip()
    if not (user and pw and to):
        LAST["email"] = "Email isn't set up (Gmail address or app password missing)."
        return False
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, f"PulseApply <{user}>", to
    msg.set_content(body)
    if png:
        msg.add_attachment(png, maintype="image", subtype="png", filename="application.png")
    err = None
    for port in (465, 587):
        try:
            if port == 465:
                s = smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20, context=ssl.create_default_context())
            else:
                s = smtplib.SMTP("smtp.gmail.com", 587, timeout=20)
                s.starttls(context=ssl.create_default_context())
            with s:
                s.login(user, pw)
                s.send_message(msg)
            LAST["email"] = "ok"
            return True
        except smtplib.SMTPAuthenticationError as e:
            err = e
            break                      # wrong password: trying another port won't help
        except Exception as e:  # noqa: BLE001
            err = e
    LAST["email"] = _explain_smtp(err)
    log.warning("Email failed: %s", LAST["email"])
    return False


def _push(subject: str, body: str) -> bool:
    topic = env("NTFY_TOPIC").strip()
    if not topic:
        LAST["push"] = "Phone notifications aren't set up."
        return False
    import httpx
    link = re.search(r"https?://\S+", body)
    headers = {"Title": subject.encode("utf-8").decode("latin-1", "ignore")[:200], "Tags": "hospital"}
    if link:
        headers["Click"] = link.group(0)
    try:
        r = httpx.post(f"https://ntfy.sh/{topic}", content=body[:1500].encode(), headers=headers, timeout=20)
        r.raise_for_status()
        LAST["push"] = "ok"
        return True
    except Exception as e:  # noqa: BLE001
        LAST["push"] = f"Couldn't send the phone notification ({type(e).__name__})."
        return False


def send(subject: str, body: str, attachment=None) -> bool:
    """attachment: PNG bytes, a file path, or None. True if at least one channel delivered."""
    png = None
    if isinstance(attachment, (bytes, bytearray)):
        png = bytes(attachment)
    elif isinstance(attachment, str) and attachment:
        try:
            with open(attachment, "rb") as f:
                png = f.read()
        except OSError:
            png = None
    sent_push = _push(subject, body) if env("NTFY_TOPIC") else False
    sent_mail = _email(subject, body, png) if env("GMAIL_ADDRESS") else False
    return sent_push or sent_mail
