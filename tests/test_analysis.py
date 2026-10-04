"""Tests for analyze_job() — the thin composition layer (Phase 3b)."""

from __future__ import annotations

from dataclasses import fields

from jobsearch.analysis import analyze_job
from jobsearch.eligibility import evaluate_eligibility
from jobsearch.models import CandidateProfile, EligibilityStatus, JobPosting
from jobsearch.professional_match import evaluate_professional_match


def make_profile(**overrides) -> CandidateProfile:
    defaults = {"country": "Honduras", "region": "LATAM"}
    defaults.update(overrides)
    return CandidateProfile(**defaults)


def make_posting(**overrides) -> JobPosting:
    defaults = {
        "title": "Backend Engineer",
        "company": "ACME",
        "location": "Remote - LATAM",
        "description": "Build and maintain backend services.",
    }
    defaults.update(overrides)
    return JobPosting(**defaults)


STRONG_PROFILE = make_profile(
    skills=("Python", "FastAPI", "PostgreSQL", "Docker", "Kubernetes"),
    years_experience=6,
    technologies=("Python", "AWS", "Docker"),
    education=("Bachelor's in Computer Science",),
    certifications=("AWS Solutions Architect",),
    languages=("Spanish", "English"),
)

STRONG_POSTING = make_posting(
    required_skills=("Python", "FastAPI", "PostgreSQL"),
    preferred_skills=("Kubernetes", "Terraform"),
    min_experience_years=5,
    technologies=("Python", "AWS"),
    education=("Bachelor's degree",),
    certifications=("AWS Solutions Architect",),
    languages=("English",),
)


def test_analyze_job_returns_both_results_unchanged():
    result = analyze_job(STRONG_POSTING, STRONG_PROFILE)
    assert result.professional_match == evaluate_professional_match(
        STRONG_POSTING, STRONG_PROFILE
    )
    assert result.eligibility == evaluate_eligibility(STRONG_POSTING, STRONG_PROFILE)


def test_analyze_job_has_no_combined_score():
    result = analyze_job(STRONG_POSTING, STRONG_PROFILE)
    assert {field.name for field in fields(result)} == {
        "professional_match",
        "eligibility",
    }
    assert 0.0 <= result.professional_match.score <= 1.0
    assert not hasattr(result.eligibility, "score")


def test_analyze_job_not_eligible_with_high_professional_match():
    posting = make_posting(
        location="Remote - US",
        required_skills=("Python", "FastAPI", "PostgreSQL"),
        preferred_skills=("Kubernetes", "Terraform"),
        min_experience_years=5,
        technologies=("Python", "AWS"),
        education=("Bachelor's degree",),
        certifications=("AWS Solutions Architect",),
        languages=("English",),
    )
    result = analyze_job(posting, STRONG_PROFILE)
    assert result.professional_match.score >= 0.9
    assert result.eligibility.status is EligibilityStatus.NOT_ELIGIBLE


def test_analyze_job_eligible_with_low_professional_match():
    posting = make_posting(
        required_skills=("COBOL", "Mainframe", "SAP"),
        min_experience_years=12,
        education=("PhD",),
        languages=("German",),
    )
    profile = make_profile(
        skills=("Python",),
        years_experience=6,
        education=("Bachelor's in Systems",),
        languages=("Spanish", "English"),
    )
    result = analyze_job(posting, profile)
    assert result.professional_match.score <= 0.3
    assert result.eligibility.status is EligibilityStatus.ELIGIBLE


def test_analyze_job_is_deterministic():
    first = analyze_job(STRONG_POSTING, STRONG_PROFILE)
    second = analyze_job(STRONG_POSTING, STRONG_PROFILE)
    assert first == second
