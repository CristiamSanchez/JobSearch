"""Gmail OAuth connectivity check tests (offline: mocked service + OAuth).

No Google account, OAuth login, credentials, API key, or network access is
ever required. Covered: the check reuses the existing
``build_gmail_service``, issues exactly one read-only list request with
``maxResults=1``, fetches no message bodies, needs no ``AI_API_KEY``,
never opens the database, and prints only secret-free information.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jobsearch import gmail_check
from jobsearch.config import Settings
from jobsearch.gmail import build_gmail_service

#: Stands in for the credentials file contents; must never be printed.
CREDENTIALS_CANARY = "SECRET-CANARY-DO-NOT-PRINT"

#: Symbols from other subsystems this module must never reference.
FORBIDDEN_REFERENCES = (
    "settings.ai_api_key",
    "AIExtractor",
    "load_career_profile",
    "JobStore",
    "detect_job_email",
    "process_job_email",
    "sync_gmail",
)


class _Exec:
    def __init__(self, response: object) -> None:
        self._response = response

    def execute(self) -> object:
        return self._response


class FakeService:
    """Chainable stand-in for ``users().messages().list`` (read-only)."""

    def __init__(self, response: object | None = None) -> None:
        self.response: object = {} if response is None else response
        self.list_calls: list[dict] = []
        self.get_calls: list[dict] = []

    def users(self) -> "FakeService":
        return self

    def messages(self) -> "FakeService":
        return self

    def list(self, **kwargs: object) -> _Exec:
        self.list_calls.append(kwargs)
        return _Exec(self.response)

    def get(self, **kwargs: object) -> _Exec:
        self.get_calls.append(kwargs)
        raise AssertionError("message bodies must never be fetched")


class RecordingBuilder:
    """Fake ``build_gmail_service``: records paths and returns a fake."""

    def __init__(self, service: object) -> None:
        self._service = service
        self.calls: list[tuple[str, str]] = []

    def __call__(self, credentials_file: str, token_file: str) -> object:
        self.calls.append((credentials_file, token_file))
        return self._service


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    """Settings for an isolated run: no AI key, no real DB, fake paths."""
    credentials = tmp_path / "gmail_credentials.json"
    if not credentials.exists():
        credentials.write_text(CREDENTIALS_CANARY, encoding="utf-8")
    values: dict = dict(
        gmail_credentials_file=str(credentials),
        gmail_token_file=str(tmp_path / "gmail_token.json"),
        ai_api_key="",  # deliberately unset: the check must not need it
        database_path=str(tmp_path / "jobsearch.db"),
    )
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def run_check(
    tmp_path: Path, service: object | None = None, **overrides: object
) -> tuple[gmail_check.ConnectivityReport, RecordingBuilder]:
    builder = RecordingBuilder(service or FakeService({"messages": [{"id": "m-1"}]}))
    report = gmail_check.check_gmail_connectivity(
        make_settings(tmp_path, **overrides), service_builder=builder
    )
    return report, builder


# ---------------------------------------------------------------------------
# Reuse of the existing implementation
# ---------------------------------------------------------------------------


def test_module_binds_the_existing_gmail_build_gmail_service() -> None:
    assert gmail_check.build_gmail_service is build_gmail_service


def test_check_calls_the_builder_with_the_configured_paths(tmp_path: Path) -> None:
    report, builder = run_check(tmp_path)
    settings = make_settings(tmp_path)
    assert builder.calls == [
        (settings.gmail_credentials_file, settings.gmail_token_file)
    ]
    assert report.token_file == settings.gmail_token_file
    assert report.message_references == 1


# ---------------------------------------------------------------------------
# Minimal, read-only API usage
# ---------------------------------------------------------------------------


def test_single_list_request_is_minimal_and_read_only(tmp_path: Path) -> None:
    service = FakeService({"messages": [{"id": "m-1"}]})
    run_check(tmp_path, service)
    assert service.list_calls == [{"userId": "me", "maxResults": 1}]
    assert service.get_calls == []  # bodies are never fetched


def test_empty_mailbox_reports_zero_references(tmp_path: Path) -> None:
    report, _ = run_check(tmp_path, FakeService({}))
    assert report.message_references == 0


def test_malformed_response_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(gmail_check.GmailCheckError) as excinfo:
        run_check(tmp_path, FakeService(["not-an-object"]))
    assert "expected an object" in str(excinfo.value)


def test_fresh_authorization_is_detected_from_the_token_file(
    tmp_path: Path,
) -> None:
    report, _ = run_check(tmp_path)
    assert report.fresh_authorization is True

    (tmp_path / "gmail_token.json").write_text("{}", encoding="utf-8")
    reused, _ = run_check(tmp_path)
    assert reused.fresh_authorization is False


# ---------------------------------------------------------------------------
# No AI, no orchestration, no persistence
# ---------------------------------------------------------------------------


def test_check_needs_no_ai_api_key(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    assert settings.ai_api_key == ""

    report, builder = run_check(tmp_path)
    assert report.message_references == 1
    assert builder.calls  # the OAuth/service step still ran


def test_module_references_no_ai_extraction_or_persistence_symbols() -> None:
    source = Path(gmail_check.__file__).read_text(encoding="utf-8")
    for symbol in FORBIDDEN_REFERENCES:
        assert symbol not in source, f"gmail_check.py must not reference {symbol!r}"


def test_check_never_opens_the_database(tmp_path: Path) -> None:
    database = tmp_path / "jobsearch.db"
    run_check(tmp_path)
    assert not database.exists()


# ---------------------------------------------------------------------------
# Error handling: actionable, secret-free, correct exit codes
# ---------------------------------------------------------------------------


def test_missing_credentials_file_is_actionable_and_stops_early(
    tmp_path: Path,
) -> None:
    settings = make_settings(
        tmp_path, gmail_credentials_file=str(tmp_path / "absent.json")
    )
    builder = RecordingBuilder(FakeService())
    with pytest.raises(gmail_check.GmailCheckConfigError) as excinfo:
        gmail_check.check_gmail_connectivity(settings, service_builder=builder)
    assert builder.calls == []  # stopped before any OAuth/network step
    assert "absent.json" in str(excinfo.value)
    assert "gmail.readonly" in str(excinfo.value)


def test_oauth_failure_is_reported_as_configuration_error(tmp_path: Path) -> None:
    def failing_builder(credentials_file: str, token_file: str) -> object:
        raise RuntimeError("oauth flow exploded")

    with pytest.raises(gmail_check.GmailCheckConfigError) as excinfo:
        gmail_check.check_gmail_connectivity(
            make_settings(tmp_path), service_builder=failing_builder
        )
    message = str(excinfo.value)
    assert "oauth flow exploded" in message
    assert "gmail.readonly" in message
    assert "gmail_token" in message  # points at the token to delete/re-create


def test_list_failure_is_an_operational_error(tmp_path: Path) -> None:
    class FailingService(FakeService):
        def list(self, **kwargs: object) -> _Exec:
            raise RuntimeError("403 forbidden")

    with pytest.raises(gmail_check.GmailCheckError) as excinfo:
        run_check(tmp_path, FailingService())
    assert type(excinfo.value) is gmail_check.GmailCheckError
    assert "gmail.readonly" in str(excinfo.value)


def test_main_returns_zero_with_a_safe_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    service = FakeService({"messages": [{"id": "m-1"}]})
    code = gmail_check.main(
        settings=make_settings(tmp_path),
        service_builder=RecordingBuilder(service),
    )
    captured = capsys.readouterr()
    assert code == 0
    assert "OAuth authentication" in captured.out
    assert "succeeded" in captured.out
    assert "Message references found : 1" in captured.out
    assert "no AI extraction" in captured.out
    combined = captured.out + captured.err
    assert CREDENTIALS_CANARY not in combined  # credentials stay unread
    assert "m-1" not in combined  # only the count is reported, never ids


def test_main_configuration_error_exits_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = make_settings(
        tmp_path, gmail_credentials_file=str(tmp_path / "absent.json")
    )
    code = gmail_check.main(settings=settings, service_builder=RecordingBuilder(FakeService()))
    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    assert "Configuration error" in captured.err


def test_main_api_failure_exits_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    class FailingService(FakeService):
        def list(self, **kwargs: object) -> _Exec:
            raise RuntimeError("403 forbidden")

    code = gmail_check.main(
        settings=make_settings(tmp_path),
        service_builder=RecordingBuilder(FailingService()),
    )
    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert "Gmail check failed" in captured.err


def test_main_loads_settings_through_load_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        gmail_check, "load_settings", lambda: make_settings(tmp_path)
    )
    code = gmail_check.main(service_builder=RecordingBuilder(FakeService()))
    assert code == 0
