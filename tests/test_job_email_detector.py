"""Phase 4A tests: deterministic job-email detector + source-agnostic pipeline.

The detector is a lightweight triage step that runs before any AI: it
classifies an email as JOB_LIKELY / POSSIBLE_JOB / NOT_JOB from content
signals only. These tests also prove the intake pipeline has no
source-specific branches: every source enters the same code path.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from jobsearch.email_provider import EmailProviderError
from jobsearch.job_email_detector import detect_job_email
from jobsearch.models import EmailMessage, JobEmailDetection, JobLikelihood

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 28, tzinfo=timezone.utc)

SOURCE_MODULES = (
    "src/jobsearch/models.py",
    "src/jobsearch/email_provider.py",
    "src/jobsearch/gmail.py",
    "src/jobsearch/job_email_detector.py",
    "src/jobsearch/gmail_sync.py",
    "src/jobsearch/ai_extractor.py",
)

JOB_BOARD_NAMES = (
    "linkedin",
    "jobright",
    "glassdoor",
    "indeed",
    "careerbuilder",
    "ziprecruiter",
)


def make_email(subject: str = "", body: str = "", **overrides: object) -> EmailMessage:
    base: dict = {
        "message_id": "m-1",
        "sender": "someone@example.com",
        "received_at": NOW,
        "subject": subject,
        "body": body,
    }
    base.update(overrides)
    return EmailMessage(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# JOB_LIKELY
# ---------------------------------------------------------------------------


def test_detector_job_likely_job_board_alert() -> None:
    message = make_email(
        subject="New job alert: Backend Engineer",
        body=(
            "Apply now - we are looking for a candidate with strong qualifications. "
            "Salary: USD 120,000 per year. "
            "https://www.linkedin.com/jobs/view/123456"
        ),
    )
    detection = detect_job_email(message)
    assert detection.likelihood is JobLikelihood.JOB_LIKELY
    assert "job" in detection.signals
    assert "apply" in detection.signals
    assert "salary" in detection.signals
    assert any(signal.startswith("https://") for signal in detection.signals)


def test_detector_job_likely_unknown_recruiter() -> None:
    """An unknown sender with job-like content is never penalized."""
    message = make_email(
        subject="Role that fits your background",
        body=(
            "Hi, I'm a recruiter working on a position for you. "
            "Please apply and submit your application to get started."
        ),
        sender="jane@randomcorp.io",
        source_hint=None,
    )
    detection = detect_job_email(message)
    assert detection.likelihood is JobLikelihood.JOB_LIKELY
    assert "recruiter" in detection.signals


def test_detector_job_likely_careers_url() -> None:
    message = make_email(
        subject="Senior Designer opening",
        body=(
            "We are hiring. See full details and apply at "
            "https://careers.acme-corp.com/jobs/42"
        ),
    )
    detection = detect_job_email(message)
    assert detection.likelihood is JobLikelihood.JOB_LIKELY
    assert any("careers" in signal for signal in detection.signals)


# ---------------------------------------------------------------------------
# POSSIBLE_JOB
# ---------------------------------------------------------------------------


def test_detector_possible_job_weak_signals() -> None:
    message = make_email(
        subject="Update about your application",
        body="We reviewed your profile. Similar roles may interest you later.",
    )
    detection = detect_job_email(message)
    assert detection.likelihood is JobLikelihood.POSSIBLE_JOB
    assert detection.signals  # the signals that produced it are recorded


def test_detector_possible_job_single_salary_term() -> None:
    message = make_email(
        subject="Market update",
        body="Average salary trends this quarter. Read more.",
    )
    assert detect_job_email(message).likelihood is JobLikelihood.POSSIBLE_JOB


def test_detector_possible_job_body_terms_only() -> None:
    message = make_email(subject="Hello", body="Your candidate profile needs an update.")
    assert detect_job_email(message).likelihood is JobLikelihood.POSSIBLE_JOB


# ---------------------------------------------------------------------------
# NOT_JOB
# ---------------------------------------------------------------------------


def test_detector_not_job_order_confirmation() -> None:
    message = make_email(
        subject="Order 12345 confirmed",
        body=(
            "Thanks for your purchase. Track your package at "
            "https://shop.example.com/t/abc."
        ),
    )
    detection = detect_job_email(message)
    assert detection.likelihood is JobLikelihood.NOT_JOB
    assert detection.signals == ()


def test_detector_not_job_calendar_invite() -> None:
    message = make_email(
        subject="Team sync",
        body="Agenda: weekly sync on Thursday. See you then.",
    )
    assert detect_job_email(message).likelihood is JobLikelihood.NOT_JOB


def test_detector_not_job_plain_newsletter() -> None:
    message = make_email(
        subject="October digest",
        body="Top stories of the week. Unsubscribe to stop these emails.",
    )
    assert detect_job_email(message).likelihood is JobLikelihood.NOT_JOB


# ---------------------------------------------------------------------------
# Detector behaviour: deterministic, non-destructive, source-agnostic
# ---------------------------------------------------------------------------


def test_detector_is_deterministic() -> None:
    message = make_email(subject="Job alert", body="Apply now.")
    assert detect_job_email(message) == detect_job_email(message)


def test_detector_does_not_modify_the_message() -> None:
    raw_html = "<div>Apply at <a href='https://x.example/apply'>x</a></div>"
    message = make_email(subject="Job", body=raw_html)
    detect_job_email(message)
    # original content still preserved for the later AI phase
    assert message.body == raw_html


def test_detector_ignores_source_hint() -> None:
    content = dict(subject="Role update", body="Apply now to the candidate pool.")
    with_hint = make_email(**content, source_hint="linkedin")
    without_hint = make_email(**content, source_hint=None)
    assert detect_job_email(with_hint) == detect_job_email(without_hint)


def test_detector_ignores_sender() -> None:
    content = dict(subject="Role update", body="Apply now to the candidate pool.")
    known = make_email(**content, sender="jobs@linkedin.com")
    unknown = make_email(**content, sender="jane@randomcorp.io")
    assert detect_job_email(known) == detect_job_email(unknown)


def test_detector_signals_are_deduplicated() -> None:
    detection = detect_job_email(make_email(subject="job job job", body="apply apply"))
    assert detection.signals.count("job") == 1
    assert detection.signals.count("apply") == 1


def test_detection_result_validates_likelihood() -> None:
    with pytest.raises(ValueError, match="likelihood"):
        JobEmailDetection(likelihood="JOB_LIKELY")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Integration: fake provider, many sources, one pipeline
# ---------------------------------------------------------------------------


class FakeEmailProvider:
    """Deterministic in-memory provider covering heterogeneous sources."""

    def __init__(self, messages: list[EmailMessage]) -> None:
        self._messages = list(messages)
        self.search_queries: list[str] = []

    def search(self, query: str) -> list[EmailMessage]:
        self.search_queries.append(query)
        return list(self._messages)

    def get_message(self, message_id: str) -> EmailMessage:
        for message in self._messages:
            if message.message_id == message_id:
                return message
        raise EmailProviderError(f"unknown message: {message_id}")


def _inbox() -> list[EmailMessage]:
    """LinkedIn, Jobright, Glassdoor, and an unknown recruiter — same pipeline."""
    return [
        make_email(
            message_id="msg-linkedin",
            sender="jobs@linkedin.com",
            source_hint="linkedin",
            subject="New job alert: Backend Engineer",
            body=(
                "Apply now - we are looking for a candidate. "
                "Salary: USD 120,000 per year. "
                "https://www.linkedin.com/jobs/view/123456"
            ),
        ),
        make_email(
            message_id="msg-jobright",
            sender="alerts@jobright.ai",
            source_hint="jobright",
            subject="5 new jobs for your profile",
            body=(
                "Review these candidate matches and apply now. "
                "Full job description inside. https://apply.jobright.ai/matches/1"
            ),
        ),
        make_email(
            message_id="msg-glassdoor",
            sender="no-reply@glassdoor.com",
            source_hint="glassdoor",
            subject="Software Engineer job you might like",
            body=(
                "The employer is hiring for this position. "
                "Apply to similar jobs today. "
                "https://www.glassdoor.com/job-listing/99"
            ),
        ),
        make_email(
            message_id="msg-recruiter",
            sender="jane@randomcorp.io",
            source_hint=None,
            subject="Role that fits your background",
            body=(
                "Hi, I'm a recruiter working on a position for you. "
                "Please apply and submit your application to get started."
            ),
        ),
    ]


def test_intake_pipeline_is_source_agnostic() -> None:
    provider = FakeEmailProvider(_inbox())
    query = "newer_than:7d"

    # every source enters the identical search -> normalize -> detect pipeline
    messages = provider.search(query)
    detections = [detect_job_email(message) for message in messages]

    assert provider.search_queries == [query]
    assert [m.message_id for m in messages] == [
        "msg-linkedin",
        "msg-jobright",
        "msg-glassdoor",
        "msg-recruiter",
    ]
    # unknown senders are classified on content, exactly like known boards
    assert [d.likelihood for d in detections] == [
        JobLikelihood.JOB_LIKELY,
        JobLikelihood.JOB_LIKELY,
        JobLikelihood.JOB_LIKELY,
        JobLikelihood.JOB_LIKELY,
    ]
    assert all(d.signals for d in detections)


def test_pipeline_result_depends_on_content_not_source_metadata() -> None:
    provider = FakeEmailProvider(_inbox())
    messages = provider.search("newer_than:7d")

    # re-labeling a message's source metadata must never change its detection
    original = messages[0]
    relabeled = EmailMessage(
        message_id=original.message_id,
        thread_id=original.thread_id,
        sender=original.sender,
        subject=original.subject,
        received_at=original.received_at,
        body=original.body,
        source_hint=None,
    )
    assert detect_job_email(relabeled) == detect_job_email(original)


def test_pipeline_retrieves_messages_individually() -> None:
    provider = FakeEmailProvider(_inbox())
    message = provider.get_message("msg-recruiter")
    assert message.message_id == "msg-recruiter"
    with pytest.raises(EmailProviderError):
        provider.get_message("msg-does-not-exist")


# ---------------------------------------------------------------------------
# Structural proof: no source-specific logic in the ingestion path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("module_path", SOURCE_MODULES)
def test_ingestion_path_contains_no_source_specific_logic(module_path: str) -> None:
    source = (ROOT / module_path).read_text(encoding="utf-8").lower()
    for name in JOB_BOARD_NAMES:
        assert name not in source, f"{module_path} must not reference {name!r}"


@pytest.mark.parametrize("module_path", SOURCE_MODULES)
def test_ingestion_path_never_scores_or_decides_eligibility(module_path: str) -> None:
    source = (ROOT / module_path).read_text(encoding="utf-8")
    # Phase 4A must not touch the deterministic engines; AI decides nothing yet
    assert "evaluate_professional_match(" not in source
    assert "evaluate_eligibility(" not in source
    assert "ProfessionalMatch(" not in source
    assert "EligibilityResult(" not in source
