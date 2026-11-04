"""Email notifications through Gmail."""
import logging
import smtplib
from email.message import EmailMessage

from .settings import env

log = logging.getLogger("notify")


def send(subject: str, body: str, attachment: str | None = None) -> bool:
    user, pw, to = env("GMAIL_ADDRESS"), env("GMAIL_APP_PASSWORD"), env("NOTIFY_TO") or env("GMAIL_ADDRESS")
    if not (user and pw and to):
        log.warning("Email not configured; would have sent: %s", subject)
        return False
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    msg.set_content(body)
    if attachment:
        try:
            with open(attachment, "rb") as f:
                msg.add_attachment(f.read(), maintype="image", subtype="png", filename="application.png")
        except OSError:
            pass
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
            s.login(user, pw)
            s.send_message(msg)
        return True
    except Exception as e:  # noqa: BLE001
        log.exception("Email failed: %s", e)
        return False
