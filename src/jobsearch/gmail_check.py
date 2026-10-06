"""Gmail OAuth + read-only connectivity check (development utility).

A deliberately small, standalone probe that answers "does real Gmail
OAuth authorization work, and can we read the mailbox?" **without any AI
configuration**. It exists so the first OAuth authorization can be
validated before ``AI_API_KEY`` is set; it is *not* part of the production
sync — :func:`jobsearch.gmail_sync.run_sync` is untouched.

Exactly two steps, in this order:

1. :func:`jobsearch.gmail.build_gmail_service` — the existing Phase 4A
   helper (imported, never re-implemented): it validates
   ``GMAIL_CREDENTIALS_FILE``, opens the browser for consent when
   ``GMAIL_TOKEN_FILE`` does not exist yet, and stores the token there.
   It authenticates with the unchanged read-only scope
   ``https://www.googleapis.com/auth/gmail.readonly``.
2. a single ``users.messages.list(userId="me", maxResults=1)`` request
   that returns message *references* only. ``users.messages.get`` is never
   called, so no subject, sender, body, or label is ever read.

Guarantees:

* **No AI required** — the extractor, the AI API key setting, and the
  career profile are never referenced here.
* **No side effects** — the database is never opened, no email is
  processed, and nothing is sent, modified, labeled, or deleted.
* **Secret-free output** — the report holds only status lines, a count,
  local file paths, and the scope name; credentials, tokens, and message
  content are never printed.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .config import Settings, load_settings
from .gmail import build_gmail_service

__all__ = [
    "ConnectivityReport",
    "GmailCheckConfigError",
    "GmailCheckError",
    "MAX_RESULTS",
    "check_gmail_connectivity",
    "format_report",
    "main",
]

#: Exactly one list request, at most one message reference.
MAX_RESULTS = 1


class GmailCheckError(RuntimeError):
    """The single read-only request failed (exit code 1). Secret-free."""


class GmailCheckConfigError(GmailCheckError):
    """OAuth credentials or the OAuth flow failed (exit code 2)."""


@dataclass(frozen=True)
class ConnectivityReport:
    """Safe-to-print outcome: counts and local paths only, never secrets."""

    #: ``True`` when no token file existed before this run (consent ran).
    fresh_authorization: bool
    #: Local path of the OAuth token (``GMAIL_TOKEN_FILE``).
    token_file: str
    #: How many message references the single list request returned.
    message_references: int


def check_gmail_connectivity(
    settings: Settings,
    *,
    service_builder: Callable[[str, str], object] | None = None,
) -> ConnectivityReport:
    """Authorize (existing OAuth flow) and issue one read-only list request.

    ``service_builder`` defaults to the real
    :func:`jobsearch.gmail.build_gmail_service`; tests inject a fake so no
    account, token, credentials, or network access is ever needed. Raises
    :class:`GmailCheckConfigError` when OAuth setup or authorization fails
    and :class:`GmailCheckError` when the read request fails; both
    messages are actionable and never contain secrets.
    """
    credentials_file = Path(settings.gmail_credentials_file)
    if not credentials_file.is_file():
        raise GmailCheckConfigError(
            f"Google OAuth client credentials not found: {credentials_file}. "
            "Point GMAIL_CREDENTIALS_FILE at a Desktop-app OAuth client ID "
            "JSON downloaded from the Google Cloud console (the default "
            "location is the git-ignored secrets/ directory; see "
            ".env.example). Access is read-only (scope gmail.readonly)."
        )

    token_file = Path(settings.gmail_token_file)
    fresh_authorization = not token_file.is_file()

    builder = service_builder or build_gmail_service
    try:
        service = builder(settings.gmail_credentials_file, settings.gmail_token_file)
    except Exception as exc:
        raise GmailCheckConfigError(
            "could not build the read-only Gmail service: "
            f"{type(exc).__name__}: {exc}. Re-check "
            "GMAIL_CREDENTIALS_FILE (a valid Desktop-app OAuth client "
            "JSON); if authorization was revoked, delete the token at "
            f"GMAIL_TOKEN_FILE={settings.gmail_token_file} and re-run "
            "to sign in again. Access is read-only (scope gmail.readonly)."
        ) from exc

    try:
        response = (
            service.users()
            .messages()
            .list(userId="me", maxResults=MAX_RESULTS)
            .execute()
        )
    except Exception as exc:
        raise GmailCheckError(
            f"read-only Gmail request failed: {type(exc).__name__}: {exc}. "
            "The OAuth scope is gmail.readonly; verify the test user in "
            "the Google Cloud console and re-run the check."
        ) from exc

    if not isinstance(response, dict):
        raise GmailCheckError(
            "malformed Gmail response: expected an object, got "
            f"{type(response).__name__}"
        )
    summaries = response.get("messages") or []
    if not isinstance(summaries, list):
        raise GmailCheckError(
            "malformed Gmail response: 'messages' must be a list"
        )

    references = sum(
        1
        for summary in summaries
        if isinstance(summary, dict)
        and isinstance(summary.get("id"), str)
        and summary["id"].strip()
    )
    return ConnectivityReport(
        fresh_authorization=fresh_authorization,
        token_file=settings.gmail_token_file,
        message_references=references,
    )


def format_report(report: ConnectivityReport) -> str:
    """Deterministic, secret-free summary of a successful check."""
    authorization = (
        "succeeded (new authorization: browser consent completed)"
        if report.fresh_authorization
        else "succeeded (existing token reused, no browser needed)"
    )
    return "\n".join(
        [
            "Gmail OAuth connectivity check (read-only)",
            f"  OAuth authentication     : {authorization}",
            f"  OAuth token stored at    : {report.token_file}",
            "  Gmail API connectivity   : succeeded "
            "(users.messages.list(userId='me', maxResults=1))",
            f"  Message references found : {report.message_references}",
            "  Scope                    : "
            "https://www.googleapis.com/auth/gmail.readonly",
            "  Message bodies were not read; no AI extraction; "
            "no database changes.",
        ]
    )


def main(
    *,
    settings: Settings | None = None,
    service_builder: Callable[[str, str], object] | None = None,
) -> int:
    """CLI entry point for ``python -m jobsearch.gmail_check``.

    Exit codes: ``0`` OAuth + read-only connectivity verified, ``2``
    configuration/OAuth problem, ``1`` API or unexpected failure. Output
    never contains credentials, tokens, or message content.
    """
    try:
        report = check_gmail_connectivity(
            settings if settings is not None else load_settings(),
            service_builder=service_builder,
        )
    except GmailCheckConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except GmailCheckError as exc:
        print(f"Gmail check failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(
            f"Gmail check failed unexpectedly: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1
    print(format_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
