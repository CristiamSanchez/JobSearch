"""Core data models for JobSearch AI.

The models keep two evaluation concepts deliberately separate:

* ``ProfessionalMatch`` — numeric fit score (0.0–1.0) for skills, experience,
  and related dimensions, with structured evidence about the match.
* ``EligibilityResult`` — categorical status for whether the candidate can
  apply at all (location, work authorization, citizenship, visa, residency).

Eligibility must never be represented as, or merged with, a score; see the
"Candidate eligibility rules" and "Professional match scoring" sections of
the README.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class EligibilityStatus(StrEnum):
    """Whether the candidate can realistically apply for a posting."""

    ELIGIBLE = "ELIGIBLE"
    NOT_ELIGIBLE = "NOT_ELIGIBLE"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    UNKNOWN = "UNKNOWN"


class AuthorizationSignal(StrEnum):
    """How an explicit work-authorization requirement was interpreted."""

    AUTHORIZATION_REQUIRED = "AUTHORIZATION_REQUIRED"
    SPONSORSHIP_AVAILABLE = "SPONSORSHIP_AVAILABLE"
    SPONSORSHIP_UNAVAILABLE = "SPONSORSHIP_UNAVAILABLE"
    REQUIREMENT_SATISFIED = "REQUIREMENT_SATISFIED"
    AMBIGUOUS = "AMBIGUOUS"


def _require_text(name: str, value: object, *, non_empty: bool = True) -> None:
    """Validate that ``value`` is a string (optionally non-empty)."""
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    if non_empty and not value.strip():
        raise ValueError(f"{name} must not be empty")


def _require_str_tuple(name: str, value: object) -> None:
    """Validate that ``value`` is a tuple of non-empty strings."""
    if not isinstance(value, tuple):
        raise ValueError(f"{name} must be a tuple of strings")
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{name} must contain only non-empty strings")


def _require_optional_years(name: str, value: object) -> None:
    """Validate an optional non-negative number (e.g. years of experience)."""
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a non-negative number or None")
    if value < 0:
        raise ValueError(f"{name} must be a non-negative number or None")


@dataclass(frozen=True)
class CandidateProfile:
    """Career facts about the candidate: eligibility flags + professional dimensions."""

    country: str
    region: str
    us_citizenship: bool = False
    us_green_card: bool = False
    us_opt: bool = False
    us_work_authorization: bool = False

    # Professional dimensions (Phase 3b). Empty/None means "not stated".
    skills: tuple[str, ...] = ()
    years_experience: float | None = None
    technologies: tuple[str, ...] = ()
    education: tuple[str, ...] = ()
    certifications: tuple[str, ...] = ()
    languages: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_text("country", self.country)
        _require_text("region", self.region)
        for name in (
            "us_citizenship",
            "us_green_card",
            "us_opt",
            "us_work_authorization",
        ):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be a boolean")
        for name in (
            "skills",
            "technologies",
            "education",
            "certifications",
            "languages",
        ):
            _require_str_tuple(name, getattr(self, name))
        _require_optional_years("years_experience", self.years_experience)


@dataclass(frozen=True)
class JobPosting:
    """A single job posting from a JSON file or AI extraction (Phase 3c)."""

    title: str
    company: str
    location: str  # structured location field, e.g. "Remote - LATAM"
    description: str  # complete job description text (eligibility evidence)
    url: str = ""
    source: str = "manual"

    # Structured professional requirements (Phase 3b). Empty/None means the
    # posting does not state that requirement (see scoring rules in README).
    required_skills: tuple[str, ...] = ()
    preferred_skills: tuple[str, ...] = ()
    min_experience_years: float | None = None
    technologies: tuple[str, ...] = ()
    education: tuple[str, ...] = ()
    certifications: tuple[str, ...] = ()
    languages: tuple[str, ...] = ()

    # Extracted metadata (Phase 3c). Empty string means "not stated";
    # these carry no scoring or eligibility meaning on their own.
    remote_mode: str = ""  # normalized by extraction: remote/hybrid/onsite
    salary: str = ""  # verbatim salary text as stated in the posting
    salary_currency: str = ""  # e.g. "USD"; never inferred
    salary_period: str = ""  # e.g. "year"; never inferred
    deadline: str = ""  # verbatim deadline text as stated

    def __post_init__(self) -> None:
        _require_text("title", self.title)
        _require_text("company", self.company)
        _require_text("location", self.location)
        _require_text("description", self.description)
        _require_text("url", self.url, non_empty=False)
        _require_text("source", self.source, non_empty=False)
        for name in (
            "remote_mode",
            "salary",
            "salary_currency",
            "salary_period",
            "deadline",
        ):
            _require_text(name, getattr(self, name), non_empty=False)
        for name in (
            "required_skills",
            "preferred_skills",
            "technologies",
            "education",
            "certifications",
            "languages",
        ):
            _require_str_tuple(name, getattr(self, name))
        _require_optional_years("min_experience_years", self.min_experience_years)


@dataclass(frozen=True)
class ProfessionalMatch:
    """Skills/experience fit for a job — numeric and independent of eligibility.

    ``score`` is always between 0.0 and 1.0. The evidence fields record how
    the score was reached: matched skills (required, preferred, and tooling),
    partial matches, missing requirements, experience comparisons, CV keywords
    derived from the posting, and interview topics to prepare.
    """

    score: float  # 0.0 .. 1.0
    rationale: str = ""
    matched_skills: tuple[str, ...] = ()
    partial_matches: tuple[str, ...] = ()
    missing_required_skills: tuple[str, ...] = ()
    missing_preferred_skills: tuple[str, ...] = ()
    experience_matches: tuple[str, ...] = ()
    cv_keywords: tuple[str, ...] = ()
    interview_topics: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if isinstance(self.score, bool) or not isinstance(self.score, (int, float)):
            raise ValueError("score must be a number between 0.0 and 1.0")
        if not 0.0 <= float(self.score) <= 1.0:
            raise ValueError("score must be between 0.0 and 1.0")
        _require_text("rationale", self.rationale, non_empty=False)
        for name in (
            "matched_skills",
            "partial_matches",
            "missing_required_skills",
            "missing_preferred_skills",
            "experience_matches",
            "cv_keywords",
            "interview_topics",
        ):
            _require_str_tuple(name, getattr(self, name))


@dataclass(frozen=True)
class EligibilityResult:
    """Eligibility outcome — a categorical status with evidence, never a score."""

    status: EligibilityStatus
    reasons: tuple[str, ...] = ()
    signals: tuple[AuthorizationSignal, ...] = ()


@dataclass(frozen=True)
class JobAnalysis:
    """Both independent evaluations of one posting, side by side.

    Deliberately contains no combined score: professional match and
    eligibility are never merged, compared, or averaged.
    """

    professional_match: ProfessionalMatch
    eligibility: EligibilityResult


class JobLikelihood(StrEnum):
    """Triage classification of an email (Phase 4A) — not a job decision."""

    JOB_LIKELY = "JOB_LIKELY"
    POSSIBLE_JOB = "POSSIBLE_JOB"
    NOT_JOB = "NOT_JOB"


@dataclass(frozen=True)
class JobEmailDetection:
    """Detector output: a likelihood plus the signals that produced it."""

    likelihood: JobLikelihood
    signals: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.likelihood, JobLikelihood):
            raise ValueError("likelihood must be a JobLikelihood value")
        _require_str_tuple("signals", self.signals)


@dataclass(frozen=True)
class EmailMessage:
    """A normalized inbound email — the ingestion boundary (Phase 4A).

    Source-agnostic: the fields describe the email itself and nothing in
    the pipeline branches on who sent it. ``message_id`` is the stable
    identity that will later prevent the same email from being processed
    twice (the subject is never used as an identity). ``source_hint`` is
    optional, best-effort metadata — ``None`` when the sender is unknown —
    and is never authoritative for any decision.

    ``body`` preserves the original content verbatim: the plain-text part
    when one exists, otherwise the raw HTML part as-is — never rewritten,
    so later phases receive the preserved content.
    """

    message_id: str
    sender: str
    received_at: datetime
    thread_id: str = ""
    subject: str = ""
    body: str = ""
    source_hint: str | None = None

    def __post_init__(self) -> None:
        _require_text("message_id", self.message_id)
        _require_text("sender", self.sender)
        if not isinstance(self.received_at, datetime):
            raise ValueError("received_at must be a datetime")
        _require_text("thread_id", self.thread_id, non_empty=False)
        _require_text("subject", self.subject, non_empty=False)
        _require_text("body", self.body, non_empty=False)
        if self.source_hint is not None and (
            not isinstance(self.source_hint, str) or not self.source_hint.strip()
        ):
            raise ValueError("source_hint must be a non-empty string or None")


class EmailProcessingOutcome(StrEnum):
    """What the Phase 4B pipeline did with one email."""

    NOT_JOB = "NOT_JOB"
    JOB_ANALYZED = "JOB_ANALYZED"
    EXTRACTION_FAILED = "EXTRACTION_FAILED"


@dataclass(frozen=True)
class EmailProcessingResult:
    """Outcome of running one email through the Phase 4B pipeline.

    Carries the source email and its detection, plus — only when extraction
    succeeded — the produced posting and analysis. Deliberately has **no
    score of its own**: the numeric score lives inside
    ``analysis.professional_match``, eligibility stays categorical in
    ``analysis.eligibility``, and the two are never merged here.

    Invariants (enforced): ``outcome`` is ``NOT_JOB`` exactly when the
    detection says so; ``NOT_JOB`` and ``EXTRACTION_FAILED`` never carry a
    posting or analysis; ``JOB_ANALYZED`` always carries both and never an
    error; a failed extraction always preserves a non-empty ``error``.
    """

    email: EmailMessage
    detection: JobEmailDetection
    outcome: EmailProcessingOutcome
    job_posting: JobPosting | None = None
    analysis: JobAnalysis | None = None
    error: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.email, EmailMessage):
            raise ValueError("email must be an EmailMessage")
        if not isinstance(self.detection, JobEmailDetection):
            raise ValueError("detection must be a JobEmailDetection")
        if not isinstance(self.outcome, EmailProcessingOutcome):
            raise ValueError("outcome must be an EmailProcessingOutcome value")
        if self.job_posting is not None and not isinstance(
            self.job_posting, JobPosting
        ):
            raise ValueError("job_posting must be a JobPosting or None")
        if self.analysis is not None and not isinstance(self.analysis, JobAnalysis):
            raise ValueError("analysis must be a JobAnalysis or None")
        _require_text("error", self.error, non_empty=False)

        detection_is_not_job = self.detection.likelihood is JobLikelihood.NOT_JOB
        outcome_is_not_job = self.outcome is EmailProcessingOutcome.NOT_JOB
        if detection_is_not_job != outcome_is_not_job:
            raise ValueError(
                "outcome must be NOT_JOB exactly when the detection is NOT_JOB"
            )

        if self.outcome is EmailProcessingOutcome.NOT_JOB:
            if self.job_posting is not None or self.analysis is not None:
                raise ValueError(
                    "outcome NOT_JOB must not carry a job_posting or analysis"
                )
        elif self.outcome is EmailProcessingOutcome.JOB_ANALYZED:
            if self.job_posting is None or self.analysis is None:
                raise ValueError(
                    "outcome JOB_ANALYZED requires job_posting and analysis"
                )
            if self.error:
                raise ValueError("outcome JOB_ANALYZED must not carry an error")
        else:
            if self.job_posting is not None or self.analysis is not None:
                raise ValueError(
                    "outcome EXTRACTION_FAILED must not carry a job_posting "
                    "or analysis"
                )
            if not self.error:
                raise ValueError("outcome EXTRACTION_FAILED requires an error message")
