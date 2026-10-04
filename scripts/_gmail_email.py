"""
_gmail_email.py — shared Gmail SMTP sender for every Clairvoyance email
script (auto_lock_settle.py's locks/settlement emails, generate_social_cards.
py's daily card email, send_adhoc_email.py's one-off sends).

Replaces Resend: Resend's sandbox mode only lets the `from` address send to
the account owner's own verified email, and full domain verification wasn't
worth the DNS setup + propagation wait for this volume (a handful of emails
a day). Gmail SMTP sends from/to any address once you hold an App Password
for the sending account -- no domain verification needed at all.

GMAIL_USER is the sending account (also the account the App Password was
generated for -- these are tightly coupled, changing one without the other
breaks auth, so this is a plain constant rather than a second secret).

Every caller wraps its own html_body in EMAIL_WRAP_OPEN/EMAIL_WRAP_CLOSE
before passing it to send_email() -- a plain white body band where the
caller's actual content (picks, results, whatever) renders. No logo/
image header: a styled-text version looked flat and wrong-font in a
real inbox (text-shadow and custom fonts are both unreliable in HTML
email), and the StandardLogo.png image version that replaced it didn't
look right either (mostly-empty 1080x1080 canvas scaled down small).
Simplest thing that actually looks right: no header art at all, just
the subject line and a clean white body.

Usage:
  from _gmail_email import send_email, EMAIL_WRAP_OPEN, EMAIL_WRAP_CLOSE
  ok, msg = send_email("Subject", "recipient@example.com", EMAIL_WRAP_OPEN + "<p>html body</p>" + EMAIL_WRAP_CLOSE)
  ok, msg = send_email("Subject", ["a@x.com", "b@x.com"], EMAIL_WRAP_OPEN + "<p>html body</p>" + EMAIL_WRAP_CLOSE)  # BCC'd -- see note below

Added ahead of real subscriber growth (2026-09-02 engine audit -- at
zero real subscribers neither of these has ever fired, so this is
prep, not a fix for an observed failure):
  - Batches recipients into groups of MAX_RECIPIENTS_PER_SEND. A plain
    (non-Workspace) Gmail account -- which clairvoyanceengine@gmail.com
    reads as -- caps at ~500 recipients (To+Cc+Bcc combined) per single
    message; every product's locks email BCCs its whole subscriber list
    in one send_email() call, so a product's list crossing that number
    would otherwise silently fail the entire send for every subscriber
    on it, not just the ones past #500. Kept a safety margin under the
    real 500 cap rather than the exact number.
  - Retries each batch up to MAX_SEND_ATTEMPTS times with a short
    backoff before giving up -- there was no retry at all before this;
    a single transient SMTP hiccup (network blip, a momentary Gmail-
    side throttle) used to just fail the whole send with nothing
    automatically trying again.
A multi-batch send can partially succeed (e.g. batch 1 of 2 delivers,
batch 2 hits a transient error even after retries) -- the return
message says exactly how many of the total recipients were actually
reached rather than collapsing that down to a single ok/fail bit, so a
caller logging the result can tell a full failure from a partial one.
"""
from __future__ import annotations
import os
import smtplib
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from pathlib import Path

GMAIL_USER = "clairvoyanceengine@gmail.com"
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")

MAX_RECIPIENTS_PER_SEND = 450
MAX_SEND_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = (2, 6)

# Shared chrome every email renders inside -- plain white body, no
# header art. Kept as OPEN/CLOSE (not a single wrap-everything helper)
# so callers can still build multi-part content around it.
EMAIL_WRAP_OPEN = (
    '<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;'
    'max-width:640px;margin:0 auto;background:#ffffff;color:#1a1a2e;padding:20px 16px">'
)
EMAIL_WRAP_CLOSE = '</div>'

# Same copy as clairvoyance-landing's footer disclaimer (docs/index.html
# .footer-disc) -- kept word-for-word so the legal language reads
# identically wherever a subscriber encounters it (site footer, every
# picks/receipt email). Every email that reaches a paying subscriber
# should close through EMAIL_WRAP_CLOSE_DISCLOSED rather than the plain
# EMAIL_WRAP_CLOSE, so it's structurally present, not something each
# caller has to remember to append. Internal-only emails (settlement
# summary, ad-hoc admin sends) can keep using plain EMAIL_WRAP_CLOSE.
# White text -- this closes the picks/locks email specifically (the
# receipt has its own separately-styled black disclaimer, built inline
# in _subscribers.py's send_receipt_email()). Given its own dark
# #14001f bar (same background every matchup card in that email uses)
# rather than sitting directly on EMAIL_WRAP_OPEN's plain white page --
# white text straight on that white background would be invisible.
DISCLAIMER_HTML = (
    '<div style="margin-top:28px;background:#14001f;border-radius:6px;'
    'padding:14px 18px;font-size:11px;line-height:1.5;color:#ffffff;font-weight:600">'
    'Clairvoyance Engine outputs are probabilistic projections for informational and '
    'analytical purposes only. Model outputs do not constitute financial or betting advice. '
    'Past model performance does not guarantee future results.'
    '</div>'
)
EMAIL_WRAP_CLOSE_DISCLOSED = DISCLAIMER_HTML + EMAIL_WRAP_CLOSE

# Subscriber-facing footer (locks emails, expiry reminders): who to contact / how to stop the emails, and where the public site is. Owner-only emails keep the plain disclaimer close.
SUBSCRIBER_FOOTER_HTML = (
    '<div style="margin-top:12px;font-size:12px;line-height:1.6;color:#666;text-align:center">'
    'Questions, or want to stop these emails? Just reply to this message. · '
    '<a href="https://clairvoyanceengine.info" style="color:#a000b8">clairvoyanceengine.info</a>'
    '</div>'
)
EMAIL_WRAP_CLOSE_SUBSCRIBER = DISCLAIMER_HTML + SUBSCRIBER_FOOTER_HTML + EMAIL_WRAP_CLOSE


def _build_message(subject: str, batch: list[str], html_body: str, attachments: list[Path] | None) -> MIMEMultipart:
    msg = MIMEMultipart()
    msg["From"] = f"Clairvoyance Engine <{GMAIL_USER}>"
    msg["To"] = batch[0] if len(batch) == 1 else GMAIL_USER
    msg["Subject"] = subject
    msg.attach(MIMEText(html_body, "html"))

    for path in attachments or []:
        path = Path(path)
        with open(path, "rb") as f:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(f.read())
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", f"attachment; filename={path.name}")
        msg.attach(part)
    return msg


def _send_batch_with_retry(batch: list[str], msg: MIMEMultipart) -> tuple[bool, str]:
    """One batch, retried up to MAX_SEND_ATTEMPTS times with a short
    backoff -- a fresh SMTP connection + login per attempt rather than
    reusing one across retries, since a flaky login/connection is
    exactly the kind of thing a retry needs to actually get past, not
    just resend the same request over a connection that's the problem."""
    last_err = ""
    for attempt in range(MAX_SEND_ATTEMPTS):
        if attempt > 0:
            time.sleep(RETRY_BACKOFF_SECONDS[min(attempt - 1, len(RETRY_BACKOFF_SECONDS) - 1)])
        try:
            with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as server:
                server.starttls()
                server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
                server.sendmail(GMAIL_USER, batch, msg.as_string())
            return True, "sent"
        except Exception as exc:
            last_err = str(exc)
    return False, last_err


def send_email(subject: str, to: str | list[str], html_body: str, attachments: list[Path] | None = None) -> tuple[bool, str]:
    """Never raises -- returns (success, message) so callers can log the
    result themselves the same way they logged Resend's HTTP response.

    `to` as a list (multiple paying subscribers on the same sport) sends
    to every address via BCC -- the To: header itself stays the sending
    account, so subscribers never see each other's email addresses. A
    single string still populates a real To: header as before.

    A large list is split into batches of MAX_RECIPIENTS_PER_SEND and
    each batch retried independently (see module docstring) -- for the
    common case (well under that count) this is exactly one batch, one
    attempt, same behavior as before batching existed."""
    if not GMAIL_APP_PASSWORD:
        return False, "GMAIL_APP_PASSWORD not set"
    recipients = [to] if isinstance(to, str) else list(dict.fromkeys(r for r in to if r))
    if not recipients:
        return False, "no recipient"

    batches = [recipients[i:i + MAX_RECIPIENTS_PER_SEND] for i in range(0, len(recipients), MAX_RECIPIENTS_PER_SEND)]
    sent_count = 0
    batch_errors: list[str] = []
    for batch in batches:
        msg = _build_message(subject, batch, html_body, attachments)
        ok, err = _send_batch_with_retry(batch, msg)
        if ok:
            sent_count += len(batch)
        else:
            batch_errors.append(f"{len(batch)} recipient(s): {err}")

    if not batch_errors:
        suffix = f" across {len(batches)} batches" if len(batches) > 1 else ""
        return True, f"sent to {sent_count} recipient(s){suffix}"
    if sent_count == 0:
        return False, "; ".join(batch_errors)
    return False, f"partial send -- {sent_count}/{len(recipients)} delivered; failures: {'; '.join(batch_errors)}"
