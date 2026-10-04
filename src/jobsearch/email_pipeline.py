"""Email → extraction → job analysis pipeline (Phase 4B).

Connects components that already exist — this module adds **orchestration
only**, never new analysis logic:

* :func:`detect_job_email <jobsearch.job_email_detector.detect_job_email>`
  (Phase 4A) triages the message first: ``NOT_JOB`` stops before any AI;
* :func:`extract_job_posting <jobsearch.extraction.extract_job_posting>`
  (Phase 3c) receives the email body **verbatim** and validates the
  extractor's output into a :class:`~jobsearch.models.JobPosting`;
* :func:`analyze_job <jobsearch.analysis.analyze_job>` (Phases 3a + 3b)
  computes the two independent results.

Guarantees:

* **Source-agnostic** — the sender, its domain, and ``source_hint`` are
  never read; every email follows this same path whatever its origin, and
  no sender or job-board branch exists (asserted by tests).
* **Body preservation** — the original body reaches the extractor
  unchanged: never truncated, rewritten, summarized, or stripped of
  salary, location, or work-authorization sentences, so the Phase 3c
  evidence rules apply unchanged.
* **The AI only extracts** — invalid extractor output or an extractor
  exception yields ``EXTRACTION_FAILED`` with the message preserved;
  eligibility and professional match never run on bad data, no posting is
  fabricated, and nothing is silently swallowed.
* **Separation preserved** — the result wraps a single
  :class:`~jobsearch.models.JobAnalysis` (numeric match beside categorical
  eligibility) and carries no score of its own.

No Gmail, network, or AI API key is involved: tests inject deterministic
fake extractors.
"""

from __future__ import annotations

from .analysis import analyze_job
from .extraction import JobDescriptionExtractor, extract_job_posting
from .job_email_detector import detect_job_email
from .models import (
    CandidateProfile,
    EmailMessage,
    EmailProcessingOutcome,
    EmailProcessingResult,
    JobLikelihood,
)

__all__ = ["process_job_email"]


def process_job_email(
    email: EmailMessage,
    extractor: JobDescriptionExtractor,
    profile: CandidateProfile,
) -> EmailProcessingResult:
    """Run one email through detection → extraction → analysis.

    Outcomes:

    * ``NOT_JOB`` — the detector found no job signals; ``extractor`` is
      never called and no posting or analysis is produced.
    * ``JOB_ANALYZED`` — the preserved body was extracted, validated, and
      evaluated: ``job_posting`` and ``analysis`` are both set.
    * ``EXTRACTION_FAILED`` — invalid extractor output or an extractor
      exception; the error is preserved in ``error`` and neither engine ran.

    Deterministic: the same email with the same extractor output produces
    the same result.
    """
    detection = detect_job_email(email)

    if detection.likelihood is JobLikelihood.NOT_JOB:
        return EmailProcessingResult(
            email=email,
            detection=detection,
            outcome=EmailProcessingOutcome.NOT_JOB,
        )

    try:
        posting = extract_job_posting(email.body, extractor)
    except Exception as exc:
        return EmailProcessingResult(
            email=email,
            detection=detection,
            outcome=EmailProcessingOutcome.EXTRACTION_FAILED,
            error=str(exc) or type(exc).__name__,
        )

    return EmailProcessingResult(
        email=email,
        detection=detection,
        outcome=EmailProcessingOutcome.JOB_ANALYZED,
        job_posting=posting,
        analysis=analyze_job(posting, profile),
    )
