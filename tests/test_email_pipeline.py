"""Phase 4B tests: the email → extraction → job analysis orchestration.

All fakes are deterministic: no Gmail, no OAuth, no network, no AI API.
The tests prove the pipeline only *connects* the existing components —
detection (4A), validated extraction (3c), and the deterministic engines
(3a/3b) — and that it never branches on who sent the email.
"""

from __future__ import annotations

from dataclasses import fields
from datetime import datetime, timezone
from pathlib import Path

import pytest

from jobsearch.analysis import analyze_job
from jobsearch.email_pipeline import process_job_email
from jobsearch.job_email_detector import detect_job_email
from jobsearch.models import (
    CandidateProfile,
    EmailMessage,
    EmailProcessingOutcome,
    EmailProcessingResult,
    EligibilityResult,
    EligibilityStatus,
    JobAnalysis,
    JobEmailDetection,
    JobLikelihood,
    JobPosting,
    ProfessionalMatch,
)

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 30, tzinfo=timezone.utc)

JOB_SUBJECT = "New job alert: Backend Engineer"

# Job content spanning every category the extraction must preserve:
# requirements, salary, remote/location, and work-authorization language.
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

POSSIBLE_SUBJECT = "Update about your application"
POSSIBLE_BODY = "Similar roles may interest you later."

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
)


class FakeExtractor:
    """Deterministic JobDescriptionExtractor that records every call."""

    def __init__(self, payload: object = None, error: Exception | None = None):
        self.payload = VALID_EXTRACTION if payload is None else payload
        self.error = error
        self.calls: list[str] = []

    def extract(self, description: str) -> dict[str, object]:
        self.calls.append(description)
        if self.error is not None:
            raise self.error
        return self.payload


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


def possible_job_email(**overrides: object) -> EmailMessage:
    return make_email(subject=POSSIBLE_SUBJECT, body=POSSIBLE_BODY, **overrides)


# ---------------------------------------------------------------------------
# 1. NOT_JOB: the extractor is never called
# ---------------------------------------------------------------------------


def test_not_job_email_is_not_processed() -> None:
    extractor = FakeExtractor()
    email = not_job_email()

    result = process_job_email(email, extractor, PROFILE)

    assert result.outcome is EmailProcessingOutcome.NOT_JOB
    assert extractor.calls == []
    assert result.job_posting is None
    assert result.analysis is None
    assert result.error == ""
    assert result.email is email
    assert result.detection.likelihood is JobLikelihood.NOT_JOB


# ---------------------------------------------------------------------------
# 2-3. JOB_LIKELY: full pipeline through the existing analysis path
# ---------------------------------------------------------------------------


def test_job_likely_email_runs_full_pipeline() -> None:
    extractor = FakeExtractor()
    email = job_email()

    result = process_job_email(email, extractor, PROFILE)

    assert result.outcome is EmailProcessingOutcome.JOB_ANALYZED
    assert len(extractor.calls) == 1
    assert isinstance(result.detection, JobEmailDetection)
    assert result.detection.likelihood is JobLikelihood.JOB_LIKELY

    assert isinstance(result.job_posting, JobPosting)
    assert result.job_posting.title == "Backend Engineer"
    assert result.job_posting.company == "ACME"
    assert result.job_posting.location == "Remote - LATAM"

    assert isinstance(result.analysis, JobAnalysis)
    assert isinstance(result.analysis.professional_match, ProfessionalMatch)
    assert isinstance(result.analysis.eligibility, EligibilityResult)
    assert 0.0 <= result.analysis.professional_match.score <= 1.0
    assert result.analysis.eligibility.status is EligibilityStatus.ELIGIBLE
    assert result.error == ""


def test_job_analysis_matches_existing_analyze_job_path() -> None:
    result = process_job_email(job_email(), FakeExtractor(), PROFILE)

    assert isinstance(result.job_posting, JobPosting)
    assert result.analysis == analyze_job(result.job_posting, PROFILE)


# ---------------------------------------------------------------------------
# 4. POSSIBLE_JOB: same pipeline behavior as JOB_LIKELY
# ---------------------------------------------------------------------------


def test_possible_job_runs_the_same_pipeline() -> None:
    extractor = FakeExtractor()
    email = possible_job_email()

    result = process_job_email(email, extractor, PROFILE)

    assert result.detection.likelihood is JobLikelihood.POSSIBLE_JOB
    assert result.outcome is EmailProcessingOutcome.JOB_ANALYZED
    assert len(extractor.calls) == 1
    assert isinstance(result.job_posting, JobPosting)
    assert isinstance(result.analysis, JobAnalysis)
    assert result.error == ""


def test_job_email_with_empty_body_fails_safely() -> None:
    extractor = FakeExtractor()
    email = make_email(subject="Job opportunity", body="")

    result = process_job_email(email, extractor, PROFILE)

    assert result.detection.likelihood is JobLikelihood.POSSIBLE_JOB
    assert result.outcome is EmailProcessingOutcome.EXTRACTION_FAILED
    assert "non-empty" in result.error
    assert result.job_posting is None
    assert result.analysis is None


# ---------------------------------------------------------------------------
# 5. Source-agnostic orchestration
# ---------------------------------------------------------------------------


def test_pipeline_is_source_agnostic_across_senders() -> None:
    extractor = FakeExtractor()
    emails = [
        job_email(
            message_id="msg-linkedin",
            sender="jobs@linkedin.com",
            source_hint="linkedin",
        ),
        job_email(
            message_id="msg-jobright",
            sender="alerts@jobright.ai",
            source_hint="jobright",
        ),
        job_email(
            message_id="msg-glassdoor",
            sender="no-reply@glassdoor.com",
            source_hint="glassdoor",
        ),
        job_email(
            message_id="msg-recruiter",
            sender="jane@randomcorp.io",
            source_hint=None,
        ),
    ]

    results = [process_job_email(email, extractor, PROFILE) for email in emails]

    assert [r.outcome for r in results] == [EmailProcessingOutcome.JOB_ANALYZED] * 4
    assert len(extractor.calls) == 4
    # identical content → identical postings and analyses, whatever the sender
    assert all(r.job_posting == results[0].job_posting for r in results)
    assert all(r.analysis == results[0].analysis for r in results)
    assert [r.email.message_id for r in results] == [
        "msg-linkedin",
        "msg-jobright",
        "msg-glassdoor",
        "msg-recruiter",
    ]


def test_orchestration_contains_no_source_specific_logic() -> None:
    for module_name in ("email_pipeline.py", "models.py"):
        source = (ROOT / "src" / "jobsearch" / module_name).read_text(
            encoding="utf-8"
        ).lower()
        for board in BOARD_NAMES:
            assert board not in source, f"{module_name} must not reference {board!r}"


def test_orchestration_never_reads_sender_identity() -> None:
    source = (ROOT / "src" / "jobsearch" / "email_pipeline.py").read_text(
        encoding="utf-8"
    )
    for attribute in ("email.sender", "email.source_hint", "message.sender"):
        assert attribute not in source, f"pipeline must not read {attribute}"


# ---------------------------------------------------------------------------
# 6. Extraction validation failure / extractor exception
# ---------------------------------------------------------------------------


def test_invalid_extractor_output_fails_safely() -> None:
    extractor = FakeExtractor(payload={"company": "ACME"})

    result = process_job_email(job_email(), extractor, PROFILE)

    assert result.outcome is EmailProcessingOutcome.EXTRACTION_FAILED
    assert "invalid AI extraction" in result.error
    assert "position" in result.error
    assert result.job_posting is None
    assert result.analysis is None
    assert len(extractor.calls) == 1


def test_extractor_exception_is_preserved() -> None:
    extractor = FakeExtractor(error=RuntimeError("provider exploded"))

    result = process_job_email(job_email(), extractor, PROFILE)

    assert result.outcome is EmailProcessingOutcome.EXTRACTION_FAILED
    assert "provider exploded" in result.error
    assert result.job_posting is None
    assert result.analysis is None
    assert len(extractor.calls) == 1


def test_analysis_runs_only_after_successful_extraction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[JobPosting] = []
    real_analyze_job = analyze_job

    def spy(posting: JobPosting, profile: CandidateProfile) -> JobAnalysis:
        calls.append(posting)
        return real_analyze_job(posting, profile)

    monkeypatch.setattr("jobsearch.email_pipeline.analyze_job", spy)

    ok = process_job_email(job_email(), FakeExtractor(), PROFILE)
    assert ok.outcome is EmailProcessingOutcome.JOB_ANALYZED
    assert len(calls) == 1

    failed = process_job_email(
        job_email(), FakeExtractor(payload={"company": "ACME"}), PROFILE
    )
    assert failed.outcome is EmailProcessingOutcome.EXTRACTION_FAILED
    assert len(calls) == 1

    not_job = process_job_email(not_job_email(), FakeExtractor(), PROFILE)
    assert not_job.outcome is EmailProcessingOutcome.NOT_JOB
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# 7. Email body preservation
# ---------------------------------------------------------------------------


def test_body_reaches_extractor_verbatim() -> None:
    extractor = FakeExtractor()
    email = job_email()

    process_job_email(email, extractor, PROFILE)

    assert extractor.calls == [email.body]
    received = extractor.calls[0]
    for fragment in (
        "5 years of experience with Python and PostgreSQL",
        "Salary: USD 90000 per year",
        "Remote - LATAM",
        "no US work authorization is required",
        "https://careers.example.com/jobs/42",
    ):
        assert fragment in received


def test_posting_description_preserves_original_body() -> None:
    email = job_email()

    result = process_job_email(email, FakeExtractor(), PROFILE)

    assert isinstance(result.job_posting, JobPosting)
    assert result.job_posting.description == email.body
    assert "no US work authorization is required" in result.job_posting.description


# ---------------------------------------------------------------------------
# 8. ProfessionalMatch and EligibilityResult stay separate
# ---------------------------------------------------------------------------


def test_result_keeps_match_and_eligibility_separate() -> None:
    result = process_job_email(job_email(), FakeExtractor(), PROFILE)

    assert isinstance(result.analysis, JobAnalysis)
    assert tuple(field.name for field in fields(result.analysis)) == (
        "professional_match",
        "eligibility",
    )
    assert isinstance(result.analysis.professional_match, ProfessionalMatch)
    assert isinstance(result.analysis.eligibility, EligibilityResult)
    assert not hasattr(result.analysis.eligibility, "score")
    assert isinstance(result.analysis.eligibility.status, EligibilityStatus)

    assert tuple(field.name for field in fields(result)) == (
        "email",
        "detection",
        "outcome",
        "job_posting",
        "analysis",
        "error",
    )


# ---------------------------------------------------------------------------
# 9. Determinism
# ---------------------------------------------------------------------------


def test_pipeline_is_deterministic() -> None:
    email = job_email()

    first = process_job_email(email, FakeExtractor(), PROFILE)
    second = process_job_email(email, FakeExtractor(), PROFILE)

    assert first == second


# ---------------------------------------------------------------------------
# Result-model invariants
# ---------------------------------------------------------------------------


def test_result_rejects_not_job_outcome_without_not_job_detection() -> None:
    email = job_email()
    detection = detect_job_email(email)
    assert detection.likelihood is not JobLikelihood.NOT_JOB

    with pytest.raises(ValueError, match="exactly when the detection is NOT_JOB"):
        EmailProcessingResult(
            email=email,
            detection=detection,
            outcome=EmailProcessingOutcome.NOT_JOB,
        )


def test_result_rejects_analysis_on_not_job_outcome() -> None:
    ok = process_job_email(job_email(), FakeExtractor(), PROFILE)
    email = not_job_email()
    detection = detect_job_email(email)
    assert detection.likelihood is JobLikelihood.NOT_JOB

    with pytest.raises(ValueError, match="must not carry"):
        EmailProcessingResult(
            email=email,
            detection=detection,
            outcome=EmailProcessingOutcome.NOT_JOB,
            job_posting=ok.job_posting,
            analysis=ok.analysis,
        )


def test_result_requires_error_when_extraction_failed() -> None:
    email = job_email()
    detection = detect_job_email(email)

    with pytest.raises(ValueError, match="requires an error"):
        EmailProcessingResult(
            email=email,
            detection=detection,
            outcome=EmailProcessingOutcome.EXTRACTION_FAILED,
        )


def test_result_requires_posting_and_analysis_when_analyzed() -> None:
    email = job_email()
    detection = detect_job_email(email)

    with pytest.raises(ValueError, match="requires job_posting and analysis"):
        EmailProcessingResult(
            email=email,
            detection=detection,
            outcome=EmailProcessingOutcome.JOB_ANALYZED,
        )


def test_result_rejects_invalid_field_types() -> None:
    email = job_email()
    detection = detect_job_email(email)

    with pytest.raises(ValueError, match="email must be an EmailMessage"):
        EmailProcessingResult(
            email="not-an-email",  # type: ignore[arg-type]
            detection=detection,
            outcome=EmailProcessingOutcome.NOT_JOB,
        )
    with pytest.raises(ValueError, match="outcome must be an EmailProcessingOutcome"):
        EmailProcessingResult(
            email=email,
            detection=detection,
            outcome="JOB_ANALYZED",  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="detection must be a JobEmailDetection"):
        EmailProcessingResult(
            email=email,
            detection="JOB_LIKELY",  # type: ignore[arg-type]
            outcome=EmailProcessingOutcome.NOT_JOB,
        )
