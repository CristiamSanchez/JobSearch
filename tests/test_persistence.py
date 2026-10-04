"""Phase 4C tests: SQLite persistence and deterministic job deduplication.

All databases are in-memory or temporary (`tmp_path`): no Gmail, no OAuth,
no network, no AI API key. Results come from the existing Phase 4B
pipeline run with deterministic fake extractors, so these tests prove the
store only *stores and relates* what earlier phases produced.

Key guarantees covered here:

* email-level idempotency via the unique `message_id`;
* job identity via canonical application URL → normalized attributes,
  with email evidence (subject/sender) allowed only as a non-merging
  possible-duplicate edge;
* first/last seen tracking, transactional atomicity, and source-agnostic
  behavior — no platform names or sender-based identity anywhere.
"""

from __future__ import annotations

import fnmatch
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from jobsearch.config import DEFAULT_DATABASE_PATH, Settings
from jobsearch.email_pipeline import process_job_email
from jobsearch.models import (
    CandidateProfile,
    EmailMessage,
    EmailProcessingOutcome,
    EmailProcessingResult,
    EligibilityStatus,
)
from jobsearch.persistence import (
    JobDedupStatus,
    JobStore,
    SavedRecord,
    canonicalize_url,
)

ROOT = Path(__file__).resolve().parents[1]

NOW = datetime(2026, 9, 30, tzinfo=timezone.utc)
T1 = datetime(2026, 9, 30, 10, 0, tzinfo=timezone.utc)
T2 = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
T3 = datetime(2026, 9, 30, 14, 0, tzinfo=timezone.utc)

JOB_SUBJECT = "New job alert: Backend Engineer"

RICH_BODY = (
    "Backend Engineer at ACME.\n\n"
    "Requirements: 5 years of experience with Python and PostgreSQL, "
    "Bachelor's degree or equivalent.\n"
    "Salary: USD 90000 per year. Location: Remote - LATAM.\n"
    "Candidates must be authorized to work in Honduras; no US work "
    "authorization is required for this role.\n"
    "Apply at https://careers.example.com/jobs/42"
)

NOT_JOB_SUBJECT = "Your package shipped"
NOT_JOB_BODY = "Order 123 confirmed. Tracking details are attached."

VALID_EXTRACTION = {
    "position": "Backend Engineer",
    "company": "ACME",
    "location": "Remote - LATAM",
    "required_skills": ["Python", "PostgreSQL"],
    "preferred_skills": ["Kubernetes"],
    "years_of_experience": 5,
    "technologies": ["AWS"],
    "remote_mode": "remote",
    "salary": "90000",
    "salary_currency": "USD",
    "salary_period": "year",
}

APPLICATION_URL = "https://boards.greenhouse.io/acme/jobs/12345"

PROFILE = CandidateProfile(
    country="Honduras",
    region="LATAM",
    skills=("Python", "PostgreSQL", "Kubernetes"),
    years_experience=6,
    technologies=("Python", "AWS"),
    education=("Bachelor's in Computer Science",),
    languages=("Spanish", "English"),
)

BOARD_NAMES = (
    "linkedin",
    "jobright",
    "glassdoor",
    "indeed",
    "careerbuilder",
    "ziprecruiter",
    "workana",
    "upwork",
    "fiverr",
)


class FakeExtractor:
    """Deterministic JobDescriptionExtractor (same shape as Phase 4B tests)."""

    def __init__(self, payload: object):
        self.payload = payload

    def extract(self, description: str) -> dict[str, object]:
        return self.payload  # type: ignore[return-value]


def make_email(
    subject: str = JOB_SUBJECT, body: str = RICH_BODY, **overrides: object
) -> EmailMessage:
    base: dict = {
        "message_id": "m-1",
        "sender": "someone@example.com",
        "received_at": NOW,
        "subject": subject,
        "body": body,
    }
    base.update(overrides)
    return EmailMessage(**base)  # type: ignore[arg-type]


def job_email(**overrides: object) -> EmailMessage:
    return make_email(**overrides)


def not_job_email(**overrides: object) -> EmailMessage:
    return make_email(subject=NOT_JOB_SUBJECT, body=NOT_JOB_BODY, **overrides)


def process(
    email: EmailMessage,
    *,
    payload: dict | None = None,
    **payload_overrides: object,
) -> EmailProcessingResult:
    """Run the real Phase 4B pipeline with a deterministic fake extractor."""
    base: dict = dict(VALID_EXTRACTION) if payload is None else dict(payload)
    base.update(payload_overrides)
    return process_job_email(email, FakeExtractor(base), PROFILE)


def table_count(store: JobStore, table: str) -> int:
    row = store.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    assert row is not None
    return int(row[0])


def links_for(store: JobStore, email_id: int) -> list[tuple[int, str, str]]:
    return [
        (int(row["job_id"]), row["relationship"], row["matched_by"])
        for row in store.connection.execute(
            "SELECT job_id, relationship, matched_by FROM email_jobs "
            "WHERE email_id = ? ORDER BY job_id, relationship",
            (email_id,),
        )
    ]


def job_timestamps(store: JobStore, job_id: int) -> tuple[datetime, datetime]:
    row = store.connection.execute(
        "SELECT first_seen_at, last_seen_at FROM jobs WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    assert row is not None
    return (
        datetime.fromisoformat(row["first_seen_at"]),
        datetime.fromisoformat(row["last_seen_at"]),
    )


def dump_tables(store: JobStore) -> list[tuple]:
    rows: list[tuple] = []
    for table in ("emails", "jobs", "email_jobs", "job_analyses"):
        rows.extend(
            tuple(row)
            for row in store.connection.execute(
                f"SELECT * FROM {table} ORDER BY 1"
            )
        )
    return rows


# ---------------------------------------------------------------------------
# 1. Database initialization
# ---------------------------------------------------------------------------


def test_database_initialization_creates_schema() -> None:
    with JobStore(":memory:") as store:
        tables = {
            row["name"]
            for row in store.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {"emails", "jobs", "email_jobs", "job_analyses"} <= tables

        indexes = {
            row["name"]
            for row in store.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
        assert {
            "idx_emails_subject_sender",
            "idx_jobs_canonical_url",
            "idx_jobs_attribute_key",
            "idx_email_jobs_job",
        } <= indexes

        # Foreign key enforcement is on for this connection.
        pragma = store.connection.execute("PRAGMA foreign_keys").fetchone()
        assert pragma is not None and int(pragma[0]) == 1
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            store.connection.execute(
                "INSERT INTO email_jobs "
                "(email_id, job_id, relationship, matched_by) "
                "VALUES (999, 999, 'CREATED', 'new_job')"
            )


def test_database_path_is_configurable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("DATABASE_PATH", raising=False)
    assert Settings.from_env().database_path == DEFAULT_DATABASE_PATH
    assert DEFAULT_DATABASE_PATH == "data/jobsearch.db"

    monkeypatch.setenv("DATABASE_PATH", "  tmp/custom.db  ")
    assert Settings.from_env().database_path == "tmp/custom.db"

    monkeypatch.setenv("DATABASE_PATH", "   ")
    assert Settings.from_env().database_path == "data/jobsearch.db"

    # A file-backed store creates its database file on disk (temporary in tests).
    db_file = tmp_path / "custom.db"
    assert not db_file.exists()
    with JobStore(db_file) as store:
        assert store.path == str(db_file)
        assert table_count(store, "emails") == 0
    assert db_file.is_file()


def test_default_database_file_is_git_ignored() -> None:
    ignore_lines = [
        line.strip()
        for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    ]
    assert "data/*.db" in ignore_lines
    assert "data/*.db-*" in ignore_lines
    # The default location and its journal/WAL sidecars match the patterns.
    assert fnmatch.fnmatch(DEFAULT_DATABASE_PATH, "data/*.db")
    assert fnmatch.fnmatch(f"{DEFAULT_DATABASE_PATH}-journal", "data/*.db-*")
    assert fnmatch.fnmatch(f"{DEFAULT_DATABASE_PATH}-wal", "data/*.db-*")


# ---------------------------------------------------------------------------
# 2-3. Email persistence + message_id uniqueness
# ---------------------------------------------------------------------------


def test_email_is_persisted_verbatim_and_retrievable() -> None:
    body = "Order 123 confirmed.\n\nTracking details are attached.\n"
    email = make_email(
        subject=NOT_JOB_SUBJECT,
        body=body,
        source_hint="example",
        thread_id="thread-9",
    )
    result = process(email)
    assert result.outcome is EmailProcessingOutcome.NOT_JOB

    with JobStore(":memory:") as store:
        record = store.save(result, now=T1)

        stored = store.get_email(email.message_id)
        assert stored == email
        assert stored is not None
        assert stored.body == email.body  # verbatim, newlines included
        assert "\n\n" in stored.body
        assert stored.source_hint == "example"

        row = store.connection.execute(
            "SELECT outcome, error FROM emails WHERE message_id = ?",
            (email.message_id,),
        ).fetchone()
        assert row is not None
        assert row["outcome"] == "NOT_JOB"
        assert row["error"] == ""
        assert record.email_id >= 1


def test_message_id_guarantees_email_uniqueness() -> None:
    email = job_email()
    result = process(email)

    with JobStore(":memory:") as store:
        first = store.save(result, now=T1)
        second = store.save(result, now=T2)

        assert table_count(store, "emails") == 1
        assert first.email_id == second.email_id

        # A different message_id is a different email.
        other = process(job_email(message_id="m-2"))
        third = store.save(other, now=T2)
        assert third.email_id != first.email_id
        assert table_count(store, "emails") == 2

        # The database itself rejects duplicate message ids.
        with pytest.raises(sqlite3.IntegrityError):
            store.connection.execute(
                "INSERT INTO emails (message_id, sender, received_at) "
                "VALUES (?, ?, ?)",
                ("m-1", "another@example.com", NOW.isoformat()),
            )


# ---------------------------------------------------------------------------
# 4-5. Job and JobAnalysis persistence
# ---------------------------------------------------------------------------


def test_job_posting_round_trips_completely() -> None:
    email = job_email()
    result = process(
        email,
        application_url=APPLICATION_URL,
        education=["Bachelor's degree"],
        certifications=["AWS Solutions Architect"],
        languages=["English", "Spanish"],
        deadline="2026-11-30",
    )

    with JobStore(":memory:") as store:
        record = store.save(result, now=T1)

        persisted = store.get_job(record.job_id)
        assert persisted == result.job_posting
        assert persisted is not None
        assert persisted.description == email.body  # full text preserved
        assert persisted.required_skills == ("Python", "PostgreSQL")
        assert persisted.min_experience_years == 5.0
        assert persisted.salary_currency == "USD"
        assert persisted.url == APPLICATION_URL
        assert store.get_job(999_999) is None


def test_analysis_is_persisted_completely() -> None:
    result = process(job_email())

    with JobStore(":memory:") as store:
        record = store.save(result, now=T1)
        assert record.dedup_status is JobDedupStatus.NEW

        analysis = store.get_analysis(record.job_id)
        assert analysis == result.analysis  # full dataclass equality
        assert analysis is not None

        # Evidence fields survive — not just the score and the status.
        match = analysis.professional_match
        assert 0.0 <= match.score <= 1.0
        assert match.matched_skills
        assert match.rationale
        assert match.interview_topics is not None

        eligibility = analysis.eligibility
        assert eligibility.status is EligibilityStatus.ELIGIBLE
        assert not hasattr(eligibility, "score")  # never a score field
        assert analysis.eligibility == result.analysis.eligibility

        assert store.get_analysis(999_999) is None


# ---------------------------------------------------------------------------
# 6-7. Email ↔ Job relationship / multiple emails, one job
# ---------------------------------------------------------------------------


def test_email_job_relationship_is_recorded() -> None:
    result = process(job_email(), application_url=APPLICATION_URL)

    with JobStore(":memory:") as store:
        record = store.save(result, now=T1)

        assert links_for(store, record.email_id) == [
            (record.job_id, "CREATED", "new_job")
        ]
        assert table_count(store, "email_jobs") == 1


def test_multiple_emails_reference_one_job() -> None:
    email_a = job_email(message_id="a", sender="alerts@one.example")
    email_b = job_email(
        message_id="b",
        sender="careers@two.example",
        subject="Backend Engineer at ACME — apply now",
    )

    with JobStore(":memory:") as store:
        first = store.save(
            process(email_a, application_url=APPLICATION_URL), now=T1
        )
        second = store.save(
            process(email_b, application_url=APPLICATION_URL), now=T2
        )

        assert first.dedup_status is JobDedupStatus.NEW
        assert second.dedup_status is JobDedupStatus.EXACT_DUPLICATE
        assert first.job_id == second.job_id
        assert table_count(store, "jobs") == 1
        assert table_count(store, "emails") == 2
        assert links_for(store, first.email_id) == [
            (first.job_id, "CREATED", "new_job")
        ]
        assert links_for(store, second.email_id) == [
            (second.job_id, "DUPLICATE_REFERENCE", "application_url")
        ]


# ---------------------------------------------------------------------------
# 8-10. Canonical application URL and tracking parameters
# ---------------------------------------------------------------------------


def test_same_email_processed_twice_is_idempotent() -> None:
    result = process(job_email(), application_url=APPLICATION_URL)

    with JobStore(":memory:") as store:
        first = store.save(result, now=T1)
        replay = store.save(result, now=T3)  # same message, later time

        assert replay.email_id == first.email_id
        assert replay.job_id == first.job_id
        assert table_count(store, "emails") == 1
        assert table_count(store, "jobs") == 1
        assert table_count(store, "email_jobs") == 1
        assert table_count(store, "job_analyses") == 1

        # Reprocessing the same email is not a new observation of the job.
        seen_first, seen_last = job_timestamps(store, first.job_id)
        assert seen_first == T1
        assert seen_last == T1

        # The body is never rewritten by a replay.
        assert store.get_email(result.email.message_id) == result.email


def test_same_canonical_url_from_different_emails() -> None:
    email_a = job_email(message_id="a")
    email_b = job_email(
        message_id="b",
        subject="Backend Engineer — ACME",
    )

    with JobStore(":memory:") as store:
        first = store.save(
            process(email_a, application_url=APPLICATION_URL), now=T1
        )
        second = store.save(
            process(
                email_b,
                application_url=(
                    APPLICATION_URL
                    + "?utm_source=alerts&utm_campaign=weekly"
                    + "&fbclid=abc123&gclid=xyz789"
                ),
            ),
            now=T2,
        )

        assert first.job_id == second.job_id
        assert second.dedup_status is JobDedupStatus.EXACT_DUPLICATE
        assert table_count(store, "jobs") == 1
        # The second email keeps its own row with its own content.
        assert store.get_email("b") == email_b


def test_canonical_url_strips_tracking_parameters() -> None:
    assert (
        canonicalize_url(
            "https://boards.greenhouse.io/acme/jobs/12345"
            "?utm_source=linkedin&utm_medium=email&utm_campaign=alert"
            "&utm_term=backend&utm_content=card&fbclid=abc123&gclid=xyz"
        )
        == "https://boards.greenhouse.io/acme/jobs/12345"
    )


def test_canonical_url_keeps_needed_parameters() -> None:
    # Non-tracking parameters are preserved (some application URLs need
    # them) and sorted so equal logical URLs compare equal.
    assert (
        canonicalize_url("https://example.com/apply?ref=email&jobId=99&gclid=x")
        == "https://example.com/apply?jobId=99&ref=email"
    )
    assert (
        canonicalize_url("https://example.com/apply?jobId=99&ref=email")
        == canonicalize_url("https://example.com/apply?ref=email&jobId=99")
    )


def test_canonical_url_normalizes_case_and_fragment() -> None:
    assert (
        canonicalize_url("HTTPS://Example.COM/Path/To-Job#apply-section")
        == "https://example.com/Path/To-Job"
    )


def test_canonical_url_rejects_unusable_values() -> None:
    assert canonicalize_url("") == ""
    assert canonicalize_url("   ") == ""
    assert canonicalize_url("not a url") == ""
    assert canonicalize_url("ftp://example.com/jobs/1") == ""
    assert canonicalize_url("mailto:someone@example.com") == ""


# ---------------------------------------------------------------------------
# 11-12. first_seen_at / last_seen_at
# ---------------------------------------------------------------------------


def test_first_seen_at_is_preserved() -> None:
    with JobStore(":memory:") as store:
        first = store.save(
            process(job_email(message_id="a"), application_url=APPLICATION_URL),
            now=T1,
        )
        second = store.save(
            process(job_email(message_id="b"), application_url=APPLICATION_URL),
            now=T2,
        )

        seen_first, _ = job_timestamps(store, second.job_id)
        assert seen_first == T1  # never overwritten by later observations
        assert first.job_id == second.job_id


def test_last_seen_at_is_updated_by_new_emails() -> None:
    with JobStore(":memory:") as store:
        store.save(
            process(job_email(message_id="a"), application_url=APPLICATION_URL),
            now=T1,
        )
        second = store.save(
            process(job_email(message_id="b"), application_url=APPLICATION_URL),
            now=T2,
        )
        third = store.save(
            process(job_email(message_id="c"), application_url=APPLICATION_URL),
            now=T3,
        )

        seen_first, seen_last = job_timestamps(store, third.job_id)
        assert second.job_id == third.job_id
        assert seen_first == T1
        assert seen_last == T3

        # Replaying an existing email does not move the observation time,
        # even when replayed at a much later time.
        store.save(
            process(job_email(message_id="b"), application_url=APPLICATION_URL),
            now=datetime(2026, 9, 30, 23, 0, tzinfo=timezone.utc),
        )
        _, seen_last = job_timestamps(store, second.job_id)
        assert seen_last == T3


# ---------------------------------------------------------------------------
# 13-14. New job creation and exact duplicates
# ---------------------------------------------------------------------------


def test_new_job_created_with_new_status() -> None:
    result = process(job_email(), application_url=APPLICATION_URL)

    with JobStore(":memory:") as store:
        record = store.save(result, now=T1)

        assert record.dedup_status is JobDedupStatus.NEW
        assert record.job_id is not None
        assert record.possible_duplicate_job_id is None
        assert table_count(store, "jobs") == 1
        assert links_for(store, record.email_id) == [
            (record.job_id, "CREATED", "new_job")
        ]


def test_exact_duplicate_by_canonical_url() -> None:
    with JobStore(":memory:") as store:
        first = store.save(
            process(job_email(message_id="a"), application_url=APPLICATION_URL),
            now=T1,
        )
        second = store.save(
            process(
                job_email(
                    message_id="b",
                    subject="Your application to ACME",
                    sender="apply@acme.example",
                ),
                application_url=APPLICATION_URL + "?utm_source=alerts",
            ),
            now=T2,
        )

        assert second.dedup_status is JobDedupStatus.EXACT_DUPLICATE
        assert second.job_id == first.job_id
        assert table_count(store, "jobs") == 1
        assert links_for(store, second.email_id) == [
            (second.job_id, "DUPLICATE_REFERENCE", "application_url")
        ]


def test_exact_duplicate_by_normalized_attributes() -> None:
    # No application URLs: identity falls back to the four normalized
    # attributes, compared case-insensitively with collapsed whitespace.
    email_a = job_email(message_id="a", subject="Backend Engineer at ACME")
    email_b = job_email(message_id="b", subject="ACME is hiring")

    with JobStore(":memory:") as store:
        first = store.save(
            process(
                email_a,
                company="ACME",
                position="Backend Engineer",
                location="Remote - LATAM",
                remote_mode="remote",
            ),
            now=T1,
        )
        second = store.save(
            process(
                email_b,
                company="  acme",
                position="backend  engineer",
                location="remote - latam",
                remote_mode="Remote",
            ),
            now=T2,
        )

        assert first.dedup_status is JobDedupStatus.NEW
        assert second.dedup_status is JobDedupStatus.EXACT_DUPLICATE
        assert second.job_id == first.job_id
        assert table_count(store, "jobs") == 1
        assert links_for(store, second.email_id) == [
            (second.job_id, "DUPLICATE_REFERENCE", "job_attributes")
        ]


# ---------------------------------------------------------------------------
# 15. Possible duplicates are never merged
# ---------------------------------------------------------------------------


def test_possible_duplicate_is_not_merged() -> None:
    # Same sender and subject, but different posting attributes and no
    # application URL: evidence is insufficient → both jobs are kept and a
    # possible-duplicate edge is recorded instead of a silent merge.
    email_a = job_email(message_id="a", sender="alerts@digest.example")
    email_b = job_email(message_id="b", sender="alerts@digest.example")

    with JobStore(":memory:") as store:
        first = store.save(
            process(
                email_a,
                company="ACME",
                position="Backend Engineer",
                location="Remote - LATAM",
            ),
            now=T1,
        )
        second = store.save(
            process(
                email_b,
                company="Globex",
                position="Data Engineer",
                location="Remote - US",
            ),
            now=T2,
        )

        assert second.dedup_status is JobDedupStatus.POSSIBLE_DUPLICATE
        assert second.job_id != first.job_id
        assert second.possible_duplicate_job_id == first.job_id

        # Data preservation: two jobs, two analyses, no merge.
        assert table_count(store, "jobs") == 2
        assert table_count(store, "job_analyses") == 2
        assert store.get_analysis(first.job_id) is not None
        assert store.get_analysis(second.job_id) is not None

        # The new email links its own job (CREATED) plus the possible
        # duplicate edge to the other job — deterministic and explicit.
        assert set(links_for(store, second.email_id)) == {
            (second.job_id, "CREATED", "new_job"),
            (first.job_id, "POSSIBLE_DUPLICATE_REFERENCE", "email_subject_sender"),
        }
        assert links_for(store, first.email_id) == [
            (first.job_id, "CREATED", "new_job")
        ]


# ---------------------------------------------------------------------------
# 16-18. NOT_JOB / EXTRACTION_FAILED / JOB_ANALYZED persistence behavior
# ---------------------------------------------------------------------------


def test_not_job_does_not_create_a_job() -> None:
    email = not_job_email()
    result = process(email)
    assert result.outcome is EmailProcessingOutcome.NOT_JOB

    with JobStore(":memory:") as store:
        record = store.save(result, now=T1)

        assert record.outcome is EmailProcessingOutcome.NOT_JOB
        assert record.job_id is None
        assert record.dedup_status is None
        assert table_count(store, "emails") == 1
        assert table_count(store, "jobs") == 0
        assert table_count(store, "email_jobs") == 0
        assert table_count(store, "job_analyses") == 0
        assert store.get_email(email.message_id) == email


def test_extraction_failed_does_not_create_a_job() -> None:
    email = job_email()
    result = process(email, payload={"company": "ACME"})
    assert result.outcome is EmailProcessingOutcome.EXTRACTION_FAILED

    with JobStore(":memory:") as store:
        record = store.save(result, now=T1)

        assert record.job_id is None
        assert table_count(store, "emails") == 1
        assert table_count(store, "jobs") == 0
        assert table_count(store, "email_jobs") == 0
        assert table_count(store, "job_analyses") == 0

        # The extraction error is preserved for diagnostics.
        row = store.connection.execute(
            "SELECT outcome, error FROM emails WHERE message_id = ?",
            (email.message_id,),
        ).fetchone()
        assert row is not None
        assert row["outcome"] == "EXTRACTION_FAILED"
        assert "invalid AI extraction" in row["error"]
        assert "position" in row["error"]


def test_job_analyzed_persists_everything() -> None:
    email = job_email()
    result = process(email, application_url=APPLICATION_URL)
    assert result.outcome is EmailProcessingOutcome.JOB_ANALYZED

    with JobStore(":memory:") as store:
        record = store.save(result, now=T1)

        assert record.outcome is EmailProcessingOutcome.JOB_ANALYZED
        assert record.job_id is not None
        assert record.dedup_status is JobDedupStatus.NEW
        assert table_count(store, "emails") == 1
        assert table_count(store, "jobs") == 1
        assert table_count(store, "email_jobs") == 1
        assert table_count(store, "job_analyses") == 1

        assert store.get_email(email.message_id) == email
        assert store.get_job(record.job_id) == result.job_posting
        assert store.get_analysis(record.job_id) == result.analysis
        assert links_for(store, record.email_id) == [
            (record.job_id, "CREATED", "new_job")
        ]


# ---------------------------------------------------------------------------
# 19. Transaction rollback
# ---------------------------------------------------------------------------


def test_transaction_rolls_back_on_persistence_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = process(job_email(), application_url=APPLICATION_URL)

    with JobStore(":memory:") as store:
        def boom(job_id: int, analysis: object, stamp: str) -> None:
            raise RuntimeError("disk full")

        monkeypatch.setattr(store, "_write_analysis", boom)

        # The error propagates — never swallowed — and nothing is left
        # behind: not even the email or the job written before the failure.
        with pytest.raises(RuntimeError, match="disk full"):
            store.save(result, now=T1)

        assert table_count(store, "emails") == 0
        assert table_count(store, "jobs") == 0
        assert table_count(store, "email_jobs") == 0
        assert table_count(store, "job_analyses") == 0


# ---------------------------------------------------------------------------
# 20-21. Source-agnostic behavior, no platform-specific logic
# ---------------------------------------------------------------------------


def test_persistence_is_source_agnostic_across_providers() -> None:
    # Five emails from five kinds of senders — including well-known
    # platforms, a recruiter, and a company — all describing the same
    # opportunity: one job, five preserved emails, five links.
    emails = [
        job_email(
            message_id="m-linkedin",
            sender="jobs@linkedin.com",
            source_hint="linkedin",
            subject="New job alert: Backend Engineer",
        ),
        job_email(
            message_id="m-jobright",
            sender="alerts@jobright.ai",
            source_hint="jobright",
            subject="Backend Engineer at ACME matches your profile",
        ),
        job_email(
            message_id="m-indeed",
            sender="no-reply@indeed.com",
            source_hint="indeed",
            subject="Job opening: Backend Engineer",
        ),
        job_email(
            message_id="m-recruiter",
            sender="ana@recruiter.example",
            source_hint=None,
            subject="Backend Engineer role — interested?",
        ),
        job_email(
            message_id="m-company",
            sender="careers@acme.example",
            source_hint="acme",
            subject="Your application at ACME",
        ),
    ]
    urls = [
        APPLICATION_URL,
        APPLICATION_URL + "?utm_source=board",
        APPLICATION_URL + "?fbclid=zzz",
        APPLICATION_URL + "?gclid=qqq",
        APPLICATION_URL + "?utm_campaign=direct",
    ]

    with JobStore(":memory:") as store:
        records = [
            store.save(process(email, application_url=url), now=T1)
            for email, url in zip(emails, urls, strict=True)
        ]

        job_ids = {record.job_id for record in records}
        assert len(job_ids) == 1  # every source references the same job
        assert table_count(store, "jobs") == 1
        assert table_count(store, "emails") == 5
        assert table_count(store, "email_jobs") == 5

        assert records[0].dedup_status is JobDedupStatus.NEW
        for record in records[1:]:
            assert record.dedup_status is JobDedupStatus.EXACT_DUPLICATE
        for record in records[1:]:
            assert links_for(store, record.email_id) == [
                (record.job_id, "DUPLICATE_REFERENCE", "application_url")
            ]

        # source_hint is stored as metadata, not identity.
        hints = [
            row["source_hint"]
            for row in store.connection.execute(
                "SELECT source_hint FROM emails ORDER BY email_id"
            )
        ]
        assert hints == ["linkedin", "jobright", "indeed", None, "acme"]


def test_persistence_contains_no_platform_specific_logic() -> None:
    source = (ROOT / "src" / "jobsearch" / "persistence.py").read_text(
        encoding="utf-8"
    ).lower()
    for board in BOARD_NAMES:
        assert board not in source, f"persistence must not reference {board!r}"


def test_sender_and_source_alone_do_not_identify_jobs() -> None:
    # Same sender and same source_hint, but different subjects and
    # different job attributes → two separate jobs, and no possible-
    # duplicate edge (subject evidence does not match either).
    email_a = job_email(
        message_id="s-1",
        sender="jobs@alerts.example",
        source_hint="alerts",
        subject="Weekly opportunities digest",
    )
    email_b = job_email(
        message_id="s-2",
        sender="jobs@alerts.example",
        source_hint="alerts",
        subject="More roles you may like",
    )

    with JobStore(":memory:") as store:
        first = store.save(
            process(email_a, company="ACME", position="Backend Engineer"),
            now=T1,
        )
        second = store.save(
            process(email_b, company="Globex", position="Data Engineer"),
            now=T2,
        )

        assert first.dedup_status is JobDedupStatus.NEW
        assert second.dedup_status is JobDedupStatus.NEW
        assert second.job_id != first.job_id
        assert second.possible_duplicate_job_id is None
        assert table_count(store, "jobs") == 2
        assert table_count(store, "email_jobs") == 2


# ---------------------------------------------------------------------------
# 22. Determinism
# ---------------------------------------------------------------------------


def test_save_is_deterministic() -> None:
    # Identical inputs (same result object, same observation time) into two
    # fresh databases produce byte-for-byte identical tables.
    result = process(job_email(), application_url=APPLICATION_URL)

    with JobStore(":memory:") as first_store:
        first_record = first_store.save(result, now=T1)
        first_dump = dump_tables(first_store)
    with JobStore(":memory:") as second_store:
        second_record = second_store.save(result, now=T1)
        second_dump = dump_tables(second_store)

    assert first_record == second_record
    assert first_dump == second_dump


def test_saved_record_model_shape() -> None:
    # The save result stays a small, explicit dataclass — no score, no
    # combined metrics, only persistence facts.
    result = process(job_email(), application_url=APPLICATION_URL)

    with JobStore(":memory:") as store:
        record = store.save(result, now=T1)

    assert isinstance(record, SavedRecord)
    assert tuple(field for field in record.__dataclass_fields__) == (
        "email_id",
        "outcome",
        "job_id",
        "dedup_status",
        "possible_duplicate_job_id",
    )
    assert record.outcome is EmailProcessingOutcome.JOB_ANALYZED
    assert isinstance(record.dedup_status, JobDedupStatus)
