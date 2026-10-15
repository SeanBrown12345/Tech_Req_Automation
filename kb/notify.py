"""Email SMEs about the rows assigned to them: newly assigned rows, or a reminder of the ones still open, with
when they're due. Sent when the bid manager asks (Admin > Notifications), one email per SME per draft, linking
to their task board.

Two ways to send; the webhook wins when both are set:

  NOTIFY_WEBHOOK_URL  a Power Automate flow ("When a Teams webhook request is received" -> "Send an email
                      (V2)") that receives {"to", "subject", "body"} (body is HTML) and sends it from the flow
                      owner's mailbox. Set-up: docs/email-notifications.md
  SMTP_HOST         relay host
  SMTP_PORT         default 587
  SMTP_USERNAME / SMTP_PASSWORD   if the relay needs a login
  SMTP_FROM         sender address (default SMTP_USERNAME)
  SMTP_STARTTLS     default true; false for a relay that doesn't do TLS

  REQFILL_BASE_URL  the app's address, e.g. https://<RFP Pilot URL>/ReqFill, for the link in the email
"""

import json
import os
import smtplib
import urllib.error
import urllib.request
from datetime import date
from email.message import EmailMessage
from html import escape
from urllib.parse import urlencode

from kb import settings  # noqa: F401 (loads .env before the variables below are read)

WEBHOOK_URL = os.environ.get("NOTIFY_WEBHOOK_URL") or None
HOST = os.environ.get("SMTP_HOST") or None
PORT = int(os.environ.get("SMTP_PORT") or 587)
USERNAME = os.environ.get("SMTP_USERNAME") or None
PASSWORD = os.environ.get("SMTP_PASSWORD") or None
SENDER = os.environ.get("SMTP_FROM") or USERNAME
STARTTLS = (os.environ.get("SMTP_STARTTLS") or "true").lower() not in ("0", "false", "no")
BASE_URL = (os.environ.get("REQFILL_BASE_URL") or "").rstrip("/")

NEW, REMINDER = "new", "reminder"  # the kinds of email

# Errors send() can raise when it can't send at all (rather than failing for one recipient).
SEND_ERRORS = (OSError, smtplib.SMTPException)


def configured() -> bool:
    return bool(WEBHOOK_URL or (HOST and SENDER))


def board_link(person: str) -> str | None:
    """The person's task board, or None when the app's address isn't set."""
    return f"{BASE_URL}/tasks?{urlencode({'person': person})}" if BASE_URL else None


def due_text(due: date | None, today: date | None = None) -> str | None:
    """'Due by Monday 20 October 2026', or 'These were due on ...' once it has passed."""
    if not due:
        return None
    when = f"{due:%A} {due.day} {due:%B %Y}"
    return f"These were due on {when}." if due < (today or date.today()) else f"Due by {when}."


def message(kind: str, person: str, email: str, draft_name: str, sheets: dict[str, int], open_total: int,
            due: date | None = None) -> EmailMessage:
    """kind: new (rows newly assigned; sheets counts those) or reminder (sheets counts every open row).
    open_total: all the person's open rows in the draft."""
    count = sum(sheets.values())
    rows = f"{count:,} row{'s' * (count != 1)}"
    link = board_link(person)
    lines = [f"{n:,} on {s}" for s, n in sheets.items()] if len(sheets) > 1 else []
    if kind == NEW:
        intro = f"You've been assigned {rows} to review in {draft_name}."
        intro_html = f"You've been assigned <b>{rows}</b> to review in <b>{escape(draft_name)}</b>."
        if open_total > count:
            intro += f" You have {open_total:,} open in total."
            intro_html += f" You have {open_total:,} open in total."
        subject = f"{rows} to review: {draft_name}"
    else:
        intro = f"A reminder that you have {rows} still to review in {draft_name}."
        intro_html = f"A reminder that you have <b>{rows}</b> still to review in <b>{escape(draft_name)}</b>."
        subject = f"Reminder: {rows} to review: {draft_name}"
    deadline = due_text(due)
    if due:
        subject += f" (due {due:%b} {due.day})"

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["To"] = email
    if SENDER and not WEBHOOK_URL:
        msg["From"] = SENDER
    msg.set_content("\n".join([
        f"Hi {person},", "", intro,
        *(f"  - {line}" for line in lines),
        *(["", deadline] if deadline else []), "",
        f"Open your task board: {link}" if link else "Open ReqFill and pick your name on the task board.",
        "", "Sent by ReqFill.",
    ]))
    msg.add_alternative(
        f"<p>Hi {escape(person)},</p><p>{intro_html}</p>"
        + (f"<ul>{''.join(f'<li>{escape(line)}</li>' for line in lines)}</ul>" if lines else "")
        + (f"<p><b>{escape(deadline)}</b></p>" if deadline else "")
        + (f'<p><a href="{escape(link)}">Open your task board</a></p>' if link
           else "<p>Open ReqFill and pick your name on the task board.</p>")
        + "<p style='color:#888'>Sent by ReqFill.</p>", subtype="html")
    return msg


def send(messages: list[EmailMessage]) -> dict[int, str]:
    """Send them all. Returns position in `messages` -> error for those that failed (empty = all sent)."""
    return _send_webhook(messages) if WEBHOOK_URL else _send_smtp(messages)


def _send_webhook(messages: list[EmailMessage]) -> dict[int, str]:
    """One POST per email; the flow answers 202 Accepted once it has the request."""
    failed = {}
    for i, msg in enumerate(messages):
        body = json.dumps({"to": msg["To"], "subject": str(msg["Subject"]),
                           "body": msg.get_body(("html",)).get_content()}).encode()
        request = urllib.request.Request(WEBHOOK_URL, data=body, method="POST",
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=30):
                pass
        except urllib.error.HTTPError as e:  # the flow refused this one: report it, carry on with the rest
            failed[i] = f"HTTP {e.code}: {e.read()[:200].decode(errors='replace')}"
    return failed


def _send_smtp(messages: list[EmailMessage]) -> dict[int, str]:
    """Over one SMTP connection."""
    failed = {}
    with smtplib.SMTP(HOST, PORT, timeout=30) as smtp:
        if STARTTLS:
            smtp.starttls()
        if USERNAME and PASSWORD:
            smtp.login(USERNAME, PASSWORD)
        for i, msg in enumerate(messages):
            try:
                smtp.send_message(msg)
            except smtplib.SMTPException as e:
                failed[i] = str(e)
    return failed
