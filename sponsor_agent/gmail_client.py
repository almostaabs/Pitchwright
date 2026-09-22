"""Thin Gmail wrapper: authorise once, send, read threads.

Nothing here decides policy. It sends what it is told to send and reports
what it sees; the graph decides whether that should happen.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import Any

from sponsor_agent import config

SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
]

_service = None


def service():
    """Cached Gmail service. Runs the browser consent flow only on first use."""
    global _service
    if _service is not None:
        return _service

    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    if config.GMAIL_TOKEN.exists():
        creds = Credentials.from_authorized_user_file(str(config.GMAIL_TOKEN), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not config.GMAIL_CREDENTIALS.exists():
                raise FileNotFoundError(
                    f"Gmail OAuth client not found at {config.GMAIL_CREDENTIALS}. "
                    "See the Gmail setup section of the README."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(config.GMAIL_CREDENTIALS), SCOPES
            )
            creds = flow.run_local_server(port=0)
        config.GMAIL_TOKEN.parent.mkdir(parents=True, exist_ok=True)
        config.GMAIL_TOKEN.write_text(creds.to_json(), encoding="utf-8")

    _service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    return _service


def my_address() -> str:
    return service().users().getProfile(userId="me").execute()["emailAddress"]


def send_email(
    to: str, subject: str, body: str, thread_id: str | None = None
) -> dict[str, Any]:
    """Send plain text. Raises on any API error - the caller decides what that means."""
    message = EmailMessage()
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)

    payload: dict[str, Any] = {
        "raw": base64.urlsafe_b64encode(message.as_bytes()).decode()
    }
    if thread_id:
        payload["threadId"] = thread_id

    sent = service().users().messages().send(userId="me", body=payload).execute()
    return {"id": sent.get("id", ""), "threadId": sent.get("threadId", "")}


def _header(message: dict, name: str) -> str:
    for h in message.get("payload", {}).get("headers", []):
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def find_reply(thread_id: str, me: str | None = None) -> dict[str, Any] | None:
    """The newest message in this thread that we did not send, or None.

    "Not sent by us" is decided by the SENT label first (authoritative) and by
    the From header second, so an alias or send-as address still reads correctly.
    """
    if not thread_id:
        return None
    me = (me or my_address()).lower()
    thread = (
        service().users().threads().get(userId="me", id=thread_id, format="full").execute()
    )

    replies = []
    for message in thread.get("messages", []):
        if "SENT" in message.get("labelIds", []):
            continue
        if me and me in _header(message, "From").lower():
            continue
        replies.append(message)

    if not replies:
        return None

    newest = max(replies, key=lambda m: int(m.get("internalDate", 0)))
    epoch_ms = int(newest.get("internalDate", 0))
    received = (
        datetime.fromtimestamp(epoch_ms / 1000, timezone.utc)
        .astimezone()
        .isoformat(timespec="seconds")
        if epoch_ms
        else ""
    )
    return {
        "message_id": newest.get("id", ""),
        "from": _header(newest, "From"),
        "received_at": received,
        "snippet": (newest.get("snippet", "") or "")[:500],
    }
