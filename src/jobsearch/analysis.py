"""Thin composition layer (Phase 3b).

Runs the two independent evaluations of a posting and returns them side by
side. There is deliberately **no combined score** here: professional match
(:class:`ProfessionalMatch`) and eligibility (:class:`EligibilityResult`)
are never merged, averaged, or compared.
"""

from __future__ import annotations

from .eligibility import evaluate_eligibility
from .extraction import JobDescriptionExtractor, extract_job_posting
from .models import CandidateProfile, JobAnalysis, JobPosting
from .professional_match import evaluate_professional_match

__all__ = ["analyze_job", "analyze_description"]


def analyze_job(posting: JobPosting, profile: CandidateProfile) -> JobAnalysis:
    """Evaluate ``posting`` against ``profile`` along both independent axes.

    Returns a :class:`JobAnalysis` holding:

    * ``professional_match`` — numeric fit score (0.0–1.0), never eligibility;
    * ``eligibility`` — categorical status, never a score.

    Both results are preserved exactly as their evaluators produce them.
    """
    return JobAnalysis(
        professional_match=evaluate_professional_match(posting, profile),
        eligibility=evaluate_eligibility(posting, profile),
    )


def analyze_description(
    description: str,
    extractor: JobDescriptionExtractor,
    profile: CandidateProfile,
) -> JobAnalysis:
    """Full Phase 3c pipeline: raw text → extraction → JobPosting → JobAnalysis.

    Thin composition only: the AI (``extractor``) supplies structured data,
    :func:`extract_job_posting` validates it into a :class:`JobPosting`,
    and :func:`analyze_job` delegates every evaluation to the deterministic
    engines. The AI never sees — let alone computes — the score or status.
    """
    return analyze_job(extract_job_posting(description, extractor), profile)
