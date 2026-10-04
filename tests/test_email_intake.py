"""Phase 4A tests: email model, provider protocol, Gmail adapter, config, secrets.

Everything here is deterministic and offline: the Gmail API service object
is a fake, so no Gmail account, OAuth login, credentials, or network access
is ever required.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from pathlib import Path

import pytest

from jobsearch.config import Settings
from jobsearch.email_provider import EmailProvider, EmailProviderError, source_hint_from_sender
from jobsearch.gmail import READONLY_SCOPES, GmailEmailProvider, parse_gmail_message
from jobsearch.models import EmailMessage

ROOT = Path(__file__).resolve().parents[1]

NOW = datetime(2026, 9, 28, tzinfo=timezone.utc)
RECEIVED_MS = 1_761_712_800_000  # 2025-10-29T02:00:00Z (arbitrary fixed instant)


# ---------------------------------------------------------------------------
# Helpers: fake Gmail API service and message resources
# ---------------------------------------------------------------------------


def _encode(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def gmail_resource(
    *,
    message_id: str = "m-1",
    thread_id: str = "t-1",
    sender: str = "Jobs <jobs@board-example.com>",
    subject: str | None = "Your application",
    internal_date: str | None = str(RECEIVED_MS),
    plain: str | None = "Plain body",
    html: str | None = None,
    date_header: str | None = "Mon, 28 Sep 2026 10:00:00 +0000",
    include_from: bool = True,
) -> dict:
    """Build a ``users.messages.get``-shaped resource for tests."""
    headers: list[dict] = []
    if include_from:
        headers.append({"name": "From", "value": sender})
    if subject is not None:
        headers.append({"name": "Subject", "value": subject})
    if date_header is not None:
        headers.append({"name": "Date", "value": date_header})

    parts: list[dict] = []
    if plain is not None:
        parts.append({"mimeType": "text/plain", "body": {"data": _encode(plain)}})
    if html is not None:
        parts.append({"mimeType": "text/html", "body": {"data": _encode(html)}})

    resource: dict = {"id": message_id, "threadId": thread_id}
    if internal_date is not None:
        resource["internalDate"] = internal_date
    resource["payload"] = {
        "headers": headers,
        "mimeType": "multipart/alternative",
        "parts": parts,
    }
    return resource


class _Exec:
    def __init__(self, response: object) -> None:
        self._response = response

    def execute(self) -> object:
        return self._response


class FakeGmailService:
    """Chainable stand-in for the Gmail API service (users().messages().*)."""

    def __init__(self, *, list_response: object = None, store: dict | None = None):
        self.list_response = {} if list_response is None else list_response
        self.store = store or {}
        self.list_calls: list[dict] = []
        self.get_calls: list[dict] = []

    def users(self) -> "FakeGmailService":
        return self

    def messages(self) -> "FakeGmailService":
        return self

    def list(self, **kwargs: object) -> _Exec:
        self.list_calls.append(kwargs)
        return _Exec(self.list_response)

    def get(self, **kwargs: object) -> _Exec:
        self.get_calls.append(kwargs)
        message_id = str(kwargs.get("id"))
        if message_id not in self.store:
            raise KeyError(f"unexpected get for id {message_id!r}")
        return _Exec(self.store[message_id])


# ---------------------------------------------------------------------------
# EmailMessage model validation
# ---------------------------------------------------------------------------


def _message(**overrides: object) -> EmailMessage:
    base: dict = {"message_id": "m-1", "sender": "jane@example.com", "received_at": NOW}
    base.update(overrides)
    return EmailMessage(**base)  # type: ignore[arg-type]


def test_email_model_requires_message_id() -> None:
    with pytest.raises(ValueError, match="message_id"):
        EmailMessage(message_id="", sender="jane@example.com", received_at=NOW)


def test_email_model_requires_non_empty_sender() -> None:
    with pytest.raises(ValueError, match="sender"):
        EmailMessage(message_id="m-1", sender="   ", received_at=NOW)


def test_email_model_requires_datetime_received_at() -> None:
    with pytest.raises(ValueError, match="received_at"):
        EmailMessage(message_id="m-1", sender="jane@example.com", received_at="2026-09-28")


def test_email_model_optional_fields_default_to_empty_and_none() -> None:
    message = EmailMessage(message_id="m-1", sender="jane@example.com", received_at=NOW)
    assert message.thread_id == ""
    assert message.subject == ""
    assert message.body == ""
    # source_hint is optional: absent means None, never an error
    assert message.source_hint is None


def test_email_model_accepts_source_hint() -> None:
    assert _message(source_hint="linkedin").source_hint == "linkedin"


@pytest.mark.parametrize("bad_hint", ["", "   ", 42])
def test_email_model_rejects_invalid_source_hint(bad_hint: object) -> None:
    with pytest.raises(ValueError, match="source_hint"):
        _message(source_hint=bad_hint)


def test_email_model_preserves_body_verbatim() -> None:
    raw_html = "<div>Original <b>content</b> stays intact.</div>"
    assert _message(body=raw_html).body == raw_html


# ---------------------------------------------------------------------------
# Known / unknown senders -> optional source hint
# ---------------------------------------------------------------------------


def test_known_sender_derives_generic_source_hint() -> None:
    assert source_hint_from_sender("jobs@linkedin.com") == "linkedin"
    assert source_hint_from_sender("Jobs <jobs@glassdoor.com>") == "glassdoor"
    assert source_hint_from_sender("alerts@jobright.ai") == "jobright"


def test_source_hint_derivation_is_generic_domain_label() -> None:
    # The rule is the same for every domain: organization label of the sender.
    assert source_hint_from_sender("no-reply@mail.google.com") == "google"
    assert source_hint_from_sender("jane@randomcorp.io") == "randomcorp"


def test_unknown_sender_without_domain_yields_no_hint() -> None:
    assert source_hint_from_sender("Recruiting Team") is None
    assert source_hint_from_sender("alerts@localhost") is None


def test_unknown_sender_is_still_ingested() -> None:
    message = parse_gmail_message(
        gmail_resource(sender="jane@randomcorp.io", subject=None, internal_date=None)
    )
    assert message.sender == "jane@randomcorp.io"
    assert message.subject == ""
    assert message.source_hint == "randomcorp"


def test_parsed_message_carries_derived_hint() -> None:
    message = parse_gmail_message(gmail_resource(sender="Jobs <jobs@linkedin.com>"))
    assert message.source_hint == "linkedin"


# ---------------------------------------------------------------------------
# Provider interface behaviour
# ---------------------------------------------------------------------------


def test_gmail_adapter_satisfies_provider_protocol() -> None:
    assert isinstance(GmailEmailProvider(FakeGmailService()), EmailProvider)


def test_custom_provider_satisfies_provider_protocol() -> None:
    class MinimalProvider:
        def search(self, query: str) -> list[EmailMessage]:
            return []

        def get_message(self, message_id: str) -> EmailMessage:
            raise EmailProviderError("not found")

    assert isinstance(MinimalProvider(), EmailProvider)


def test_provider_rejects_empty_query() -> None:
    provider = GmailEmailProvider(FakeGmailService())
    with pytest.raises(ValueError, match="query"):
        provider.search("")
    with pytest.raises(ValueError, match="query"):
        provider.search("   ")


def test_provider_rejects_empty_message_id() -> None:
    provider = GmailEmailProvider(FakeGmailService())
    with pytest.raises(ValueError, match="message_id"):
        provider.get_message("")


# ---------------------------------------------------------------------------
# Search and retrieval parsing
# ---------------------------------------------------------------------------


def test_search_parses_results_in_order() -> None:
    service = FakeGmailService(
        list_response={"messages": [{"id": "m-1"}, {"id": "m-2", "threadId": "t-2"}]},
        store={
            "m-1": gmail_resource(message_id="m-1", subject="First"),
            "m-2": gmail_resource(message_id="m-2", thread_id="t-2", subject="Second"),
        },
    )
    messages = GmailEmailProvider(service).search("newer_than:7d")

    assert [m.message_id for m in messages] == ["m-1", "m-2"]
    assert [m.subject for m in messages] == ["First", "Second"]
    assert service.list_calls == [
        {"userId": "me", "q": "newer_than:7d", "maxResults": 50}
    ]
    assert len(service.get_calls) == 2
    assert all(call["format"] == "full" for call in service.get_calls)
    assert all(call["userId"] == "me" for call in service.get_calls)


def test_search_with_no_results_returns_empty_list() -> None:
    service = FakeGmailService(list_response={})
    assert GmailEmailProvider(service).search("newer_than:7d") == []
    assert service.get_calls == []


def test_get_message_parses_all_fields() -> None:
    service = FakeGmailService(store={"m-1": gmail_resource(plain="Hello, apply now")})
    message = GmailEmailProvider(service).get_message("m-1")

    assert message.message_id == "m-1"
    assert message.thread_id == "t-1"
    assert message.sender == "Jobs <jobs@board-example.com>"
    assert message.subject == "Your application"
    assert message.body == "Hello, apply now"
    assert message.received_at == datetime.fromtimestamp(
        RECEIVED_MS / 1000, tz=timezone.utc
    )
    assert message.received_at.tzinfo is not None


def test_date_header_used_when_internal_date_missing() -> None:
    resource = gmail_resource(internal_date=None)
    message = parse_gmail_message(resource)
    assert message.received_at == datetime(2026, 9, 28, 10, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# HTML / plain-text body extraction (non-destructive)
# ---------------------------------------------------------------------------


def test_plain_text_body_preferred_over_html() -> None:
    html = "<html><body><p>Apply <b>now</b></p></body></html>"
    resource = gmail_resource(plain="Apply now", html=html)
    assert parse_gmail_message(resource).body == "Apply now"


def test_html_only_body_preserved_verbatim() -> None:
    html = '<html><body><a href="https://example.com/apply">Apply</a></body></html>'
    resource = gmail_resource(plain=None, html=html)
    message = parse_gmail_message(resource)
    # raw HTML kept as-is: nothing is stripped or rewritten
    assert message.body == html


def test_top_level_single_part_body_is_decoded() -> None:
    resource = gmail_resource()
    resource["payload"] = {
        "headers": [
            {"name": "From", "value": "jane@example.com"},
            {"name": "Subject", "value": "Hello"},
        ],
        "mimeType": "text/plain",
        "body": {"data": _encode("Top-level body")},
    }
    assert parse_gmail_message(resource).body == "Top-level body"


def test_message_without_body_parts_yields_empty_body() -> None:
    resource = gmail_resource(plain=None, html=None)
    assert parse_gmail_message(resource).body == ""


def test_undecodable_body_data_yields_empty_body() -> None:
    resource = gmail_resource(plain=None, html=None)
    resource["payload"]["parts"] = [
        {"mimeType": "text/plain", "body": {"data": "!!!not-base64!!!"}}
    ]
    assert parse_gmail_message(resource).body == ""


# ---------------------------------------------------------------------------
# Malformed provider responses
# ---------------------------------------------------------------------------


def test_malformed_search_response_raises() -> None:
    provider = GmailEmailProvider(FakeGmailService(list_response="not-a-dict"))
    with pytest.raises(EmailProviderError, match="expected an object"):
        provider.search("newer_than:7d")


def test_malformed_messages_field_raises() -> None:
    provider = GmailEmailProvider(
        FakeGmailService(list_response={"messages": {"id": "m-1"}})
    )
    with pytest.raises(EmailProviderError, match="'messages' must be a list"):
        provider.search("newer_than:7d")


def test_malformed_list_entry_raises() -> None:
    provider = GmailEmailProvider(
        FakeGmailService(list_response={"messages": [{"threadId": "t-1"}]})
    )
    with pytest.raises(EmailProviderError, match="missing a message id"):
        provider.search("newer_than:7d")


def test_malformed_message_resource_raises() -> None:
    with pytest.raises(EmailProviderError, match="expected an object"):
        parse_gmail_message(None)
    with pytest.raises(EmailProviderError, match="missing message id"):
        parse_gmail_message({"threadId": "t-1"})
    with pytest.raises(EmailProviderError, match="payload must be an object"):
        parse_gmail_message({"id": "m-1", "payload": "nope"})
    with pytest.raises(EmailProviderError, match="headers must be a list"):
        parse_gmail_message({"id": "m-1", "payload": {"headers": "nope"}})


def test_message_without_from_header_raises() -> None:
    with pytest.raises(EmailProviderError, match="missing From header"):
        parse_gmail_message(gmail_resource(include_from=False))


def test_message_without_any_date_raises() -> None:
    with pytest.raises(EmailProviderError, match="invalid message date"):
        parse_gmail_message(
            gmail_resource(internal_date=None, date_header=None)
        )


# ---------------------------------------------------------------------------
# Gmail query configuration (env-driven, no code changes)
# ---------------------------------------------------------------------------


def test_gmail_settings_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "GMAIL_QUERY",
        "GMAIL_CREDENTIALS_FILE",
        "GMAIL_TOKEN_FILE",
        "GMAIL_MAX_RESULTS",
    ):
        monkeypatch.delenv(var, raising=False)
    settings = Settings.from_env()
    assert settings.gmail_query == "newer_than:7d"
    assert settings.gmail_credentials_file == "secrets/gmail_credentials.json"
    assert settings.gmail_token_file == "secrets/gmail_token.json"
    assert settings.gmail_max_results == 50


def test_gmail_query_configurable_without_code_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GMAIL_QUERY", "after:2026/09/01")
    monkeypatch.setenv("GMAIL_MAX_RESULTS", "10")
    settings = Settings.from_env()
    assert settings.gmail_query == "after:2026/09/01"

    service = FakeGmailService(list_response={})
    provider = GmailEmailProvider(service, max_results=settings.gmail_max_results)
    provider.search(settings.gmail_query)

    assert service.list_calls[0]["q"] == "after:2026/09/01"
    assert service.list_calls[0]["maxResults"] == 10


def test_empty_gmail_query_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GMAIL_QUERY", "   ")
    assert Settings.from_env().gmail_query == "newer_than:7d"


@pytest.mark.parametrize("value", ["abc", "0", "-3", ""])
def test_invalid_max_results_falls_back(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("GMAIL_MAX_RESULTS", value)
    assert Settings.from_env().gmail_max_results == 50


def test_credential_paths_configurable_without_code_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GMAIL_CREDENTIALS_FILE", "/tmp/my-client.json")
    monkeypatch.setenv("GMAIL_TOKEN_FILE", "/tmp/my-token.json")
    settings = Settings.from_env()
    assert settings.gmail_credentials_file == "/tmp/my-client.json"
    assert settings.gmail_token_file == "/tmp/my-token.json"


# ---------------------------------------------------------------------------
# Deduplication foundation: stable message_id identity
# ---------------------------------------------------------------------------


def test_message_id_preserved_across_retrievals() -> None:
    service = FakeGmailService(store={"m-1": gmail_resource(message_id="m-1")})
    provider = GmailEmailProvider(service)
    first = provider.get_message("m-1")
    second = provider.get_message("m-1")
    assert first.message_id == second.message_id == "m-1"
    assert first is not second


def test_identity_is_message_id_not_subject() -> None:
    subject = "Jobs you may like"
    first = parse_gmail_message(gmail_resource(message_id="id-1", subject=subject))
    second = parse_gmail_message(gmail_resource(message_id="id-2", subject=subject))
    # same subject, different emails: both preserved, never collapsed
    assert first.subject == second.subject == subject
    assert first.message_id != second.message_id
    assert first != second


def test_search_preserves_message_ids_from_listing() -> None:
    service = FakeGmailService(
        list_response={"messages": [{"id": "abc123"}]},
        store={"abc123": gmail_resource(message_id="abc123")},
    )
    messages = GmailEmailProvider(service).search("newer_than:7d")
    assert [m.message_id for m in messages] == ["abc123"]


# ---------------------------------------------------------------------------
# Read-only adapter and no committed secrets
# ---------------------------------------------------------------------------


def test_gmail_adapter_only_uses_read_operations() -> None:
    source = (ROOT / "src/jobsearch/gmail.py").read_text(encoding="utf-8")
    for forbidden in (
        "messages().send",
        "messages().delete",
        "messages().modify",
        "messages().trash",
        "messages().untrash",
        "messages().batchDelete",
        "messages().batchModify",
        "messages().stop",
        "removeLabelIds",
        "addLabelIds",
        "markAsRead",
    ):
        assert forbidden not in source, f"forbidden operation in adapter: {forbidden}"
    assert ".list(" in source
    assert ".get(" in source
    assert READONLY_SCOPES == ("https://www.googleapis.com/auth/gmail.readonly",)


def test_no_secrets_committed() -> None:
    gitignore_lines = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".env" in gitignore_lines
    assert "secrets/" in gitignore_lines

    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    for marker in ("AIza", "ya29.", "-----BEGIN", "client_secret=", "refresh_token="):
        assert marker not in env_example, f"credential-like marker in .env.example: {marker}"
    assert "GMAIL_QUERY" in env_example

    # default credential locations live under the git-ignored directory
    assert Settings().gmail_credentials_file.startswith("secrets/")
    assert Settings().gmail_token_file.startswith("secrets/")
