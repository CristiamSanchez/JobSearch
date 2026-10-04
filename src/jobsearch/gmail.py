"""Read-only Gmail adapter (Phase 4A).

Implements :class:`~jobsearch.email_provider.EmailProvider` on top of the
Gmail API and nothing else:

* ``search`` → ``users.messages.list`` with the ``q`` parameter;
* ``get_message`` → ``users.messages.get`` by id.

Only those two read operations are used: the adapter never sends, deletes,
or edits messages and never touches read state, and it authenticates with
the read-only scope in :data:`READONLY_SCOPES`.

The API service object is **injected**, so every call is mockable: unit
tests need no Gmail account, OAuth login, credentials, or internet access.
:func:`build_gmail_service` is the only function that touches the Google
client libraries; it imports them lazily so this module itself depends on
nothing beyond the standard library.

Body extraction is non-destructive: the first ``text/plain`` part wins when
present, otherwise the raw ``text/html`` part is returned as-is — the
original content is preserved for the later AI phase. The stable
``message_id`` from the API is carried through unchanged.
"""

from __future__ import annotations

import base64
import binascii
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from .email_provider import EmailProviderError, source_hint_from_sender
from .models import EmailMessage

__all__ = [
    "GmailEmailProvider",
    "READONLY_SCOPES",
    "build_gmail_service",
    "parse_gmail_message",
]

#: The only OAuth scopes this adapter may request: read-only mailbox access.
READONLY_SCOPES = ("https://www.googleapis.com/auth/gmail.readonly",)

_DEFAULT_MAX_RESULTS = 50


def _header(headers: list, name: str) -> str | None:
    """Case-insensitive header lookup (first non-empty match)."""
    for header in headers:
        if not isinstance(header, dict):
            continue
        header_name = header.get("name")
        value = header.get("value")
        if (
            isinstance(header_name, str)
            and header_name.strip().lower() == name.lower()
            and isinstance(value, str)
            and value.strip()
        ):
            return value.strip()
    return None


def _received_at(data: dict, headers: list) -> datetime:
    """Epoch ``internalDate`` first, then the ``Date`` header, else fail."""
    internal_date = data.get("internalDate")
    if internal_date is not None:
        try:
            millis = int(internal_date)
            return datetime.fromtimestamp(millis / 1000, tz=timezone.utc)
        except (TypeError, ValueError, OverflowError, OSError):
            pass

    date_header = _header(headers, "Date")
    if date_header:
        try:
            parsed = parsedate_to_datetime(date_header)
        except (TypeError, ValueError):
            parsed = None
        if parsed is not None:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed

    raise EmailProviderError(
        "malformed Gmail response: missing or invalid message date"
    )


def _decode_body(body: object) -> str:
    """Decode a Gmail base64url body part; unreadable data yields ``\"\"``."""
    if not isinstance(body, dict):
        return ""
    data = body.get("data")
    if not isinstance(data, str) or not data:
        return ""
    padded = data + "=" * (-len(data) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded)
    except (ValueError, binascii.Error):
        return ""
    return raw.decode("utf-8", errors="replace")


def _collect_parts(part: dict, plain: list[str], html: list[str]) -> None:
    """Recursively gather text/plain and text/html bodies, in order."""
    mime = part.get("mimeType")
    data = _decode_body(part.get("body"))
    if data:
        if isinstance(mime, str) and mime.strip().lower() == "text/html":
            html.append(data)
        else:
            plain.append(data)

    nested = part.get("parts")
    if isinstance(nested, list):
        for child in nested:
            if isinstance(child, dict):
                _collect_parts(child, plain, html)


def _extract_body(payload: object) -> str:
    """Prefer the plain-text body; fall back to raw HTML, verbatim."""
    if not isinstance(payload, dict):
        return ""
    plain: list[str] = []
    html: list[str] = []
    _collect_parts(payload, plain, html)
    if plain:
        return plain[0]
    if html:
        return html[0]
    return ""


def parse_gmail_message(data: object) -> EmailMessage:
    """Parse a ``users.messages.get`` resource into an :class:`EmailMessage`.

    Raises :class:`EmailProviderError` for malformed responses instead of
    inventing defaults for identity, sender, or date.
    """
    if not isinstance(data, dict):
        raise EmailProviderError(
            f"malformed Gmail response: expected an object, got {type(data).__name__}"
        )

    message_id = data.get("id")
    if not isinstance(message_id, str) or not message_id.strip():
        raise EmailProviderError("malformed Gmail response: missing message id")

    thread_id = data.get("threadId", "")
    if thread_id is None:
        thread_id = ""
    if not isinstance(thread_id, str):
        raise EmailProviderError("malformed Gmail response: threadId must be a string")

    payload = data.get("payload")
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise EmailProviderError("malformed Gmail response: payload must be an object")

    headers = payload.get("headers")
    if headers is None:
        headers = []
    if not isinstance(headers, list):
        raise EmailProviderError("malformed Gmail response: headers must be a list")

    sender = _header(headers, "From")
    if sender is None:
        raise EmailProviderError("malformed Gmail response: missing From header")

    subject = _header(headers, "Subject") or ""

    return EmailMessage(
        message_id=message_id,
        thread_id=thread_id,
        sender=sender,
        subject=subject,
        received_at=_received_at(data, headers),
        body=_extract_body(payload),
        source_hint=source_hint_from_sender(sender),
    )


class GmailEmailProvider:
    """Read-only Gmail adapter implementing the ``EmailProvider`` protocol."""

    def __init__(self, service: object, *, max_results: int = _DEFAULT_MAX_RESULTS):
        """``service`` is an authenticated Gmail API service (or a fake)."""
        self._service = service
        self._max_results = max_results

    def search(self, query: str) -> list[EmailMessage]:
        """List messages matching the Gmail ``q`` query, then fetch each one."""
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string")

        response = (
            self._service.users()
            .messages()
            .list(
                userId="me",
                q=query,
                maxResults=self._max_results,
            )
            .execute()
        )
        if not isinstance(response, dict):
            raise EmailProviderError(
                "malformed Gmail response: expected an object, "
                f"got {type(response).__name__}"
            )

        summaries = response.get("messages") or []
        if not isinstance(summaries, list):
            raise EmailProviderError(
                "malformed Gmail response: 'messages' must be a list"
            )

        messages: list[EmailMessage] = []
        for index, summary in enumerate(summaries):
            if (
                not isinstance(summary, dict)
                or not isinstance(summary.get("id"), str)
                or not summary["id"].strip()
            ):
                raise EmailProviderError(
                    f"malformed Gmail response: entry {index} is missing a message id"
                )
            messages.append(self.get_message(summary["id"]))
        return messages

    def get_message(self, message_id: str) -> EmailMessage:
        """Fetch one message by id and parse it (read-only)."""
        if not isinstance(message_id, str) or not message_id.strip():
            raise ValueError("message_id must be a non-empty string")

        response = (
            self._service.users()
            .messages()
            .get(userId="me", id=message_id, format="full")
            .execute()
        )
        return parse_gmail_message(response)


def build_gmail_service(credentials_file: str, token_file: str):
    """Build an authenticated Gmail service using the read-only scope.

    Requires the optional Google client libraries (see ``requirements.txt``)
    and locally stored OAuth files — never committed; see ``.env.example``.
    This is the only function that performs OAuth or network setup, and it
    is never called by the test suite.
    """
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError(
            "Gmail API client libraries are not installed; install the "
            "optional Gmail dependencies listed in requirements.txt"
        ) from exc

    credentials = None
    try:
        credentials = Credentials.from_authorized_user_file(token_file, READONLY_SCOPES)
    except (OSError, ValueError):
        credentials = None

    if not credentials or not credentials.valid:
        if credentials and credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                credentials_file, READONLY_SCOPES
            )
            credentials = flow.run_local_server(port=0)
        from pathlib import Path

        token_path = Path(token_file)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(credentials.to_json(), encoding="utf-8")

    return build("gmail", "v1", credentials=credentials, cache_discovery=False)
