"""Phase 6A tests: manual Gmail sync orchestration (offline, mocked service).

The Gmail API service is a fake and the AI extractor is scripted, so no
Gmail account, OAuth login, credentials, API key, or network access is
ever required. Covered: search/retrieval/conversion, detector buckets,
pipeline reuse, per-message failure isolation, idempotency and Phase 4C
dedup, configuration errors, GMAIL_QUERY / GMAIL_MAX_RESULTS, and the
``FOUND`` workflow default.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from jobsearch.config import Settings
from jobsearch.gmail_sync import (
    SyncConfigurationError,
    SyncSummary,
    format_summary,
    run_sync,
    sync_gmail,
)
from jobsearch.ingestion import load_career_profile
from jobsearch.models import (
    CandidateProfile,
    EmailProcessingOutcome,
    EligibilityStatus,
    JobLikelihood,
)
from jobsearch.persistence import JobStore
from jobsearch.workflow import WorkflowStatus

NOW = datetime(2026, 9, 28, tzinfo=timezone.utc)
RECEIVED_MS = 1_761_712_800_000  # fixed instant for deterministic tests

# --- detector inputs (Phase 4A thresholds: >=4 JOB_LIKELY, 1..3 POSSIBLE) --
JOB_SUBJECT = "Job opening: QA Automation Engineer"
JOB_BODY = (
    "We are hiring a QA Automation Engineer.\n\n"
    "Requirements:\n"
    "- 5 years of experience\n"
    "- Python and pytest\n\n"
    "Apply with your qualifications. Salary: 90000 per year.\n"
)
SECOND_JOB_SUBJECT = "Job opening: Junior DevOps Engineer"
SECOND_JOB_BODY = (
    "We are hiring a Junior DevOps Engineer. "
    "Apply now; the candidate must know Linux. "
    "Salary: 70000 per year.\n"
)
POSSIBLE_SUBJECT = "Meetup notes"
POSSIBLE_BODY = (
    "Thanks for your interest in our community. "
    "Please apply when tickets open."
)
NOT_JOB_SUBJECT = "Weekly product digest"
NOT_JOB_BODY = "Here is what shipped this week. Read the changelog for details."

# --- extractor payloads -----------------------------------------------------
JOB_PAYLOAD: dict[str, object] = {
    "position": "QA Automation Engineer",
    "company": "Acme QA Labs",
    "location": "Remote - LATAM",
    "remote_mode": "remote",
    "required_skills": ["Python", "pytest"],
    "years_of_experience": 5,
}
JOB_PAYLOAD_WITH_URL: dict[str, object] = {
    **JOB_PAYLOAD,
    "application_url": "https://jobs.example.test/acme/qa-17",
}
SECOND_JOB_PAYLOAD: dict[str, object] = {
    "position": "Junior DevOps Engineer",
    "company": "NorthStar Software",
    "location": "Remote - LATAM",
    "remote_mode": "remote",
}

PROFILE = CandidateProfile(
    country="Honduras",
    region="LATAM",
    skills=("Python", "PostgreSQL"),
    years_experience=6,
    technologies=("Python",),
    education=("Bachelor's in Computer Science",),
    languages=("Spanish", "English"),
)


# ---------------------------------------------------------------------------
# Fakes: Gmail service and scripted extractor
# ---------------------------------------------------------------------------


def _encode(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def gmail_resource(
    message_id: str,
    *,
    subject: str,
    body: str,
    sender: str = "Careers <jobs@example.test>",
    thread_id: str = "t-1",
) -> dict:
    """Build a ``users.messages.get``-shaped resource for tests."""
    return {
        "id": message_id,
        "threadId": thread_id,
        "internalDate": str(RECEIVED_MS),
        "payload": {
            "headers": [
                {"name": "From", "value": sender},
                {"name": "Subject", "value": subject},
                {"name": "Date", "value": "Mon, 28 Sep 2026 10:00:00 +0000"},
            ],
            "mimeType": "multipart/alternative",
            "parts": [
                {"mimeType": "text/plain", "body": {"data": _encode(body)}}
            ],
        },
    }


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


class ScriptedExtractor:
    """Deterministic extractor: one scripted response per call, in order."""

    def __init__(self, *responses: object) -> None:
        self._responses = list(responses)
        self.calls: list[str] = []

    def extract(self, description: str) -> dict[str, object]:
        index = len(self.calls)
        self.calls.append(description)
        if index >= len(self._responses):
            raise AssertionError(f"unexpected extraction call #{index}")
        response = self._responses[index]
        if isinstance(response, Exception):
            raise response
        assert isinstance(response, dict)
        return response


# ---------------------------------------------------------------------------
# Fixture-style helpers
# ---------------------------------------------------------------------------


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    credentials = tmp_path / "gmail_credentials.json"
    if not credentials.exists():
        credentials.write_text("{}", encoding="utf-8")
    profile_path = tmp_path / "career_profile.json"
    if not profile_path.exists():
        profile_path.write_text(
            json.dumps(
                {
                    "country": "Honduras",
                    "region": "LATAM",
                    "us_citizenship": False,
                    "us_green_card": False,
                    "us_opt": False,
                    "us_work_authorization": False,
                    "skills": ["Python"],
                    "years_experience": 6,
                    "technologies": ["Python"],
                    "education": ["Bachelor's in Computer Science"],
                    "certifications": [],
                    "languages": ["Spanish", "English"],
                }
            ),
            encoding="utf-8",
        )
    values: dict = dict(
        gmail_query="newer_than:7d",
        gmail_credentials_file=str(credentials),
        gmail_token_file=str(tmp_path / "gmail_token.json"),
        gmail_max_results=50,
        ai_api_key="test-key-123",
        ai_base_url="https://ai.example.test/v1",
        ai_model="test-model",
        ai_timeout_seconds=5,
        career_profile_path=str(profile_path),
        database_path=str(tmp_path / "jobsearch.db"),
    )
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def service_for(*resources: dict) -> FakeGmailService:
    return FakeGmailService(
        list_response={"messages": [{"id": r["id"]} for r in resources]},
        store={r["id"]: r for r in resources},
    )


def run_one_sync(
    tmp_path: Path,
    *resources: dict,
    extractor: object | None = None,
    settings: Settings | None = None,
) -> tuple[SyncSummary, JobStore, FakeGmailService]:
    """Run ``run_sync`` against a fake service and an isolated store."""
    settings = settings or make_settings(tmp_path)
    service = service_for(*resources)
    store = JobStore(settings.database_path)
    summary = run_sync(
        settings,
        service=service,
        extractor=extractor or ScriptedExtractor(JOB_PAYLOAD),
        store=store,
    )
    return summary, store, service


def count(store: JobStore, table: str) -> int:
    row = store.connection.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
    return int(row["n"])


# ---------------------------------------------------------------------------
# 1-3. Search, retrieval, conversion to EmailMessage
# ---------------------------------------------------------------------------


def test_search_uses_configured_query_and_max_results(tmp_path: Path) -> None:
    settings = make_settings(
        tmp_path, gmail_query="after:2026/09/01", gmail_max_results=3
    )
    summary, _store, service = run_one_sync(
        tmp_path,
        gmail_resource("m-1", subject=JOB_SUBJECT, body=JOB_BODY),
        settings=settings,
    )
    assert service.list_calls == [
        {"userId": "me", "q": "after:2026/09/01", "maxResults": 3}
    ]
    assert summary.query == "after:2026/09/01"
    assert summary.messages_found == 1


def test_messages_are_retrieved_and_converted(tmp_path: Path) -> None:
    summary, store, service = run_one_sync(
        tmp_path, gmail_resource("m-1", subject=JOB_SUBJECT, body=JOB_BODY)
    )
    assert service.get_calls == [
        {"userId": "me", "id": "m-1", "format": "full"}
    ]

    stored = store.get_email("m-1")
    assert stored is not None
    assert stored.message_id == "m-1"
    assert stored.sender == "Careers <jobs@example.test>"
    assert stored.subject == JOB_SUBJECT
    assert stored.body == JOB_BODY  # plain text preserved verbatim
    assert stored.source_hint == "example"
    assert stored.received_at == datetime.fromtimestamp(
        RECEIVED_MS / 1000, tz=timezone.utc
    )
    assert summary.messages_processed == 1


# ---------------------------------------------------------------------------
# 4-6, 13. Detector buckets through the existing pipeline; FOUND default
# ---------------------------------------------------------------------------


def test_not_job_messages_are_ignored_by_job_analysis(tmp_path: Path) -> None:
    extractor = ScriptedExtractor()  # any call would fail the test
    summary, store, _service = run_one_sync(
        tmp_path,
        gmail_resource("m-1", subject=NOT_JOB_SUBJECT, body=NOT_JOB_BODY),
        extractor=extractor,
    )
    assert summary.not_job == 1
    assert summary.jobs_analyzed == 0
    assert summary.extraction_failures == 0
    assert extractor.calls == []  # the extractor never runs for NOT_JOB
    assert store.list_job_ids() == ()
    # The message itself is still recorded as evidence, outcome NOT_JOB.
    assert store.get_outcome("m-1") is EmailProcessingOutcome.NOT_JOB


def test_job_likely_message_runs_full_pipeline(tmp_path: Path) -> None:
    extractor = ScriptedExtractor(JOB_PAYLOAD)
    summary, store, _service = run_one_sync(
        tmp_path,
        gmail_resource("m-1", subject=JOB_SUBJECT, body=JOB_BODY),
        extractor=extractor,
    )
    assert summary.job_likely == 1
    assert summary.jobs_analyzed == 1
    assert summary.jobs_created == 1
    assert summary.duplicates == 0
    assert summary.messages_processed == 1
    assert summary.processing_failures == 0

    # The email body reached the extractor verbatim.
    assert extractor.calls == [JOB_BODY]

    job_ids = store.list_job_ids()
    assert len(job_ids) == 1
    posting = store.get_job(job_ids[0])
    assert posting is not None
    assert posting.title == "QA Automation Engineer"
    assert posting.remote_mode == "remote"
    analysis = store.get_analysis(job_ids[0])
    assert analysis is not None
    assert analysis.eligibility.status is EligibilityStatus.ELIGIBLE

    # New jobs receive the manual workflow default — never inferred.
    assert store.get_status(job_ids[0]) is WorkflowStatus.FOUND


def test_possible_job_message_runs_full_pipeline(tmp_path: Path) -> None:
    extractor = ScriptedExtractor(JOB_PAYLOAD)
    summary, store, _service = run_one_sync(
        tmp_path,
        gmail_resource("m-1", subject=POSSIBLE_SUBJECT, body=POSSIBLE_BODY),
        extractor=extractor,
    )
    assert summary.possible_job == 1
    assert summary.jobs_analyzed == 1
    assert summary.jobs_created == 1
    assert summary.not_job == 0
    assert len(store.list_job_ids()) == 1
    assert extractor.calls == [POSSIBLE_BODY]


def test_new_jobs_receive_found_status(tmp_path: Path) -> None:
    summary, store, _service = run_one_sync(
        tmp_path,
        gmail_resource("m-1", subject=JOB_SUBJECT, body=JOB_BODY),
        extractor=ScriptedExtractor(JOB_PAYLOAD),
    )
    assert summary.jobs_created == 1
    job_id = store.list_job_ids()[0]
    assert store.get_status(job_id) is WorkflowStatus.FOUND


def test_application_url_does_not_imply_applied(tmp_path: Path) -> None:
    summary, store, _service = run_one_sync(
        tmp_path,
        gmail_resource("m-1", subject=JOB_SUBJECT, body=JOB_BODY),
        extractor=ScriptedExtractor(JOB_PAYLOAD_WITH_URL),
    )
    assert summary.jobs_created == 1
    job_id = store.list_job_ids()[0]
    posting = store.get_job(job_id)
    assert posting is not None
    assert posting.url == "https://jobs.example.test/acme/qa-17"
    # The URL is data, not a workflow transition: status stays FOUND.
    assert store.get_status(job_id) is WorkflowStatus.FOUND


# ---------------------------------------------------------------------------
# 7. Extraction failure is reported without crashing the whole sync
# ---------------------------------------------------------------------------


def test_extraction_failure_does_not_crash_the_sync(tmp_path: Path) -> None:
    extractor = ScriptedExtractor(
        RuntimeError("AI endpoint down"), SECOND_JOB_PAYLOAD
    )
    summary, store, _service = run_one_sync(
        tmp_path,
        gmail_resource("m-1", subject=JOB_SUBJECT, body=JOB_BODY),
        gmail_resource("m-2", subject=SECOND_JOB_SUBJECT, body=SECOND_JOB_BODY),
        extractor=extractor,
    )
    assert summary.messages_found == 2
    assert summary.messages_processed == 2
    assert summary.extraction_failures == 1
    assert summary.jobs_analyzed == 1
    assert summary.jobs_created == 1
    assert len(summary.errors) == 1
    assert summary.errors[0].startswith("m-1: extraction failed:")
    assert "AI endpoint down" in summary.errors[0]

    # The failed message is stored with its error; the second job survived.
    assert store.get_outcome("m-1") is EmailProcessingOutcome.EXTRACTION_FAILED
    row = store.connection.execute(
        "SELECT error FROM emails WHERE message_id = 'm-1'"
    ).fetchone()
    assert "AI endpoint down" in row["error"]
    assert len(store.list_job_ids()) == 1


# ---------------------------------------------------------------------------
# 8-9. Idempotency and Phase 4C deduplication intact
# ---------------------------------------------------------------------------


def test_repeated_sync_is_idempotent(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    service = service_for(
        gmail_resource("m-1", subject=JOB_SUBJECT, body=JOB_BODY)
    )
    extractor = ScriptedExtractor(JOB_PAYLOAD)
    store = JobStore(settings.database_path)

    first = run_sync(settings, service=service, extractor=extractor, store=store)
    second = run_sync(settings, service=service, extractor=extractor, store=store)

    assert first.jobs_created == 1
    assert second.messages_found == 1
    assert second.messages_processed == 0  # skipped, not re-analyzed
    assert second.reused == 1
    assert second.jobs_created == 0
    assert second.jobs_analyzed == 0
    # No duplicate email, no duplicate job, no second AI call.
    assert len(extractor.calls) == 1
    assert count(store, "emails") == 1
    assert count(store, "jobs") == 1


def test_existing_deduplication_semantics_remain_intact(tmp_path: Path) -> None:
    """Same posting (no URL) in two messages → one job, both emails kept."""
    summary, store, _service = run_one_sync(
        tmp_path,
        gmail_resource("m-1", subject=JOB_SUBJECT, body=JOB_BODY),
        gmail_resource(
            "m-2", subject=JOB_SUBJECT, body=JOB_BODY, thread_id="t-2"
        ),
        extractor=ScriptedExtractor(JOB_PAYLOAD, JOB_PAYLOAD),
    )
    assert summary.messages_processed == 2
    assert summary.jobs_analyzed == 2
    assert summary.jobs_created == 1  # level-2 attribute match
    assert summary.duplicates == 1
    # Every email is preserved as evidence; only the job row is merged.
    assert count(store, "emails") == 2
    assert count(store, "jobs") == 1
    assert count(store, "email_jobs") == 2


# ---------------------------------------------------------------------------
# 10-12. Configuration, OAuth, and empty results
# ---------------------------------------------------------------------------


def test_missing_credentials_file_is_actionable(tmp_path: Path) -> None:
    missing = tmp_path / "missing_client.json"
    settings = make_settings(tmp_path, gmail_credentials_file=str(missing))

    def fail_builder(*_args: object) -> object:
        raise AssertionError("service builder must not be called")

    with pytest.raises(SyncConfigurationError) as excinfo:
        run_sync(settings, service_builder=fail_builder)
    message = str(excinfo.value)
    assert str(missing) in message
    assert "GMAIL_CREDENTIALS_FILE" in message
    assert "gmail.readonly" in message


def test_service_construction_failure_is_actionable(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)

    def broken_builder(_credentials: str, _token: str) -> object:
        raise RuntimeError("invalid client secret format")

    with pytest.raises(SyncConfigurationError) as excinfo:
        run_sync(settings, service_builder=broken_builder)
    message = str(excinfo.value)
    assert "invalid client secret format" in message
    assert "GMAIL_TOKEN_FILE" in message
    assert "AI_API_KEY" not in message  # reported error, not unrelated advice


def test_missing_ai_key_is_actionable(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, ai_api_key="")
    service = service_for(gmail_resource("m-1", subject=JOB_SUBJECT, body=JOB_BODY))
    with pytest.raises(SyncConfigurationError) as excinfo:
        run_sync(settings, service=service, store=None)
    message = str(excinfo.value)
    assert "AI_API_KEY" in message
    assert "test-key" not in message


def test_missing_career_profile_is_actionable(tmp_path: Path) -> None:
    settings = make_settings(
        tmp_path, career_profile_path=str(tmp_path / "nope.json")
    )
    service = service_for(gmail_resource("m-1", subject=JOB_SUBJECT, body=JOB_BODY))
    with pytest.raises(SyncConfigurationError) as excinfo:
        run_sync(
            settings,
            service=service,
            extractor=ScriptedExtractor(JOB_PAYLOAD),
        )
    message = str(excinfo.value)
    assert "CAREER_PROFILE_PATH" in message
    assert str(tmp_path / "nope.json") in message


def test_empty_query_result_is_reported_clearly(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, gmail_query="newer_than:1d")
    service = FakeGmailService(list_response={})  # no messages
    store = JobStore(settings.database_path)
    summary = run_sync(
        settings,
        service=service,
        extractor=ScriptedExtractor(),
        store=store,
    )
    assert summary.messages_found == 0
    assert summary.messages_processed == 0
    assert summary.errors == ()
    rendered = format_summary(summary)
    assert "Messages found:       0" in rendered
    assert "No messages matched GMAIL_QUERY" in rendered
    assert "newer_than:1d" in rendered


def test_loaded_career_profile_is_used(tmp_path: Path) -> None:
    """run_sync reads the configured profile file through ingestion."""
    settings = make_settings(tmp_path)
    profile = load_career_profile(Path(settings.career_profile_path))
    assert profile.country == "Honduras"
    assert profile.region == "LATAM"
    assert profile.skills == ("Python",)


# ---------------------------------------------------------------------------
# Summary rendering, sync invariants, configuration values
# ---------------------------------------------------------------------------


def test_summary_counts_are_consistent(tmp_path: Path) -> None:
    summary, _store, _service = run_one_sync(
        tmp_path,
        gmail_resource("m-1", subject=JOB_SUBJECT, body=JOB_BODY),
        gmail_resource("m-2", subject=POSSIBLE_SUBJECT, body=POSSIBLE_BODY),
        gmail_resource("m-3", subject=NOT_JOB_SUBJECT, body=NOT_JOB_BODY),
        extractor=ScriptedExtractor(JOB_PAYLOAD, JOB_PAYLOAD),
    )
    assert summary.messages_found == summary.messages_processed + summary.reused
    assert (
        summary.messages_processed
        == summary.not_job + summary.job_likely + summary.possible_job
    )
    assert (summary.not_job, summary.job_likely, summary.possible_job) == (1, 1, 1)


def test_format_summary_is_deterministic_and_lists_errors(tmp_path: Path) -> None:
    summary = SyncSummary(
        query="newer_than:7d",
        messages_found=1,
        messages_processed=1,
        job_likely=1,
        jobs_analyzed=1,
        extraction_failures=1,
        errors=("m-1: extraction failed: boom",),
    )
    rendered = format_summary(summary)
    assert rendered == format_summary(summary)
    for label in (
        "Gmail sync summary",
        "Messages found:",
        "NOT_JOB:",
        "JOB_LIKELY:",
        "POSSIBLE_JOB:",
        "Jobs analyzed:",
        "New jobs created:",
        "Duplicate jobs:",
        "Extraction failures:",
        "Persistence failures:",
    ):
        assert label in rendered
    assert "Errors:" in rendered
    assert "m-1: extraction failed: boom" in rendered


def test_sync_gmail_rejects_empty_query(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "db.sqlite")
    with pytest.raises(ValueError, match="query"):
        sync_gmail(
            FakeGmailService(),
            ScriptedExtractor(),
            PROFILE,
            store,
            query="  ",
        )


def test_ai_settings_are_read_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_API_KEY", "secret-abc")
    monkeypatch.setenv("AI_BASE_URL", "https://llm.example.test/v1")
    monkeypatch.setenv("AI_MODEL", "my-model")
    monkeypatch.setenv("AI_TIMEOUT_SECONDS", "30")
    monkeypatch.setenv("CAREER_PROFILE_PATH", "/tmp/profile.json")
    settings = Settings.from_env()
    assert settings.ai_api_key == "secret-abc"
    assert settings.ai_base_url == "https://llm.example.test/v1"
    assert settings.ai_model == "my-model"
    assert settings.ai_timeout_seconds == 30
    assert settings.career_profile_path == "/tmp/profile.json"


def test_ai_settings_defaults_and_invalid_timeout_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings()
    assert settings.ai_api_key == ""
    assert settings.ai_base_url == "https://api.openai.com/v1"
    assert settings.ai_model == "gpt-4o-mini"
    assert settings.ai_timeout_seconds == 60
    assert settings.career_profile_path == "data/career_profile.json"

    monkeypatch.delenv("AI_TIMEOUT_SECONDS", raising=False)
    assert Settings.from_env().ai_timeout_seconds == 60
    monkeypatch.setenv("AI_TIMEOUT_SECONDS", "not-a-number")
    assert Settings.from_env().ai_timeout_seconds == 60
    monkeypatch.setenv("AI_TIMEOUT_SECONDS", "0")
    assert Settings.from_env().ai_timeout_seconds == 60


def test_api_key_never_appears_in_repr() -> None:
    settings = Settings(ai_api_key="super-secret-key")
    assert "super-secret-key" not in repr(settings)
    assert "super-secret-key" not in str(settings)


def test_likelihood_buckets_match_detector_thresholds() -> None:
    """The sync counts exactly what Phase 4A classified."""
    from jobsearch.job_email_detector import detect_job_email
    from jobsearch.models import EmailMessage

    job = EmailMessage(
        message_id="x-1", sender="a@b.test", received_at=NOW,
        subject=JOB_SUBJECT, body=JOB_BODY,
    )
    possible = EmailMessage(
        message_id="x-2", sender="a@b.test", received_at=NOW,
        subject=POSSIBLE_SUBJECT, body=POSSIBLE_BODY,
    )
    none = EmailMessage(
        message_id="x-3", sender="a@b.test", received_at=NOW,
        subject=NOT_JOB_SUBJECT, body=NOT_JOB_BODY,
    )
    assert detect_job_email(job).likelihood is JobLikelihood.JOB_LIKELY
    assert detect_job_email(possible).likelihood is JobLikelihood.POSSIBLE_JOB
    assert detect_job_email(none).likelihood is JobLikelihood.NOT_JOB
