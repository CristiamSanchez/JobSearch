"""Tests for the deterministic professional matching engine (Phase 3b)."""

from __future__ import annotations

from dataclasses import fields

import pytest

from jobsearch.eligibility import evaluate_eligibility
from jobsearch.models import (
    CandidateProfile,
    EligibilityStatus,
    JobPosting,
    ProfessionalMatch,
)
from jobsearch.professional_match import DIMENSION_WEIGHTS, evaluate_professional_match


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


def match(posting: JobPosting, profile: CandidateProfile) -> ProfessionalMatch:
    return evaluate_professional_match(posting, profile)


# --- Formula structure -----------------------------------------------------


def test_documented_weights_sum_to_one():
    assert sum(DIMENSION_WEIGHTS.values()) == pytest.approx(1.0)


def test_fully_matched_job_scores_exactly_one():
    profile = make_profile(
        skills=("Python", "Docker"),
        years_experience=4,
        technologies=("Python",),
        education=("Bachelor's in Computer Science",),
        certifications=("AWS SAA",),
        languages=("English",),
    )
    posting = make_posting(
        required_skills=("Python",),
        preferred_skills=("Docker",),
        min_experience_years=3,
        technologies=("Python",),
        education=("Bachelor's degree",),
        certifications=("AWS SAA",),
        languages=("English",),
    )
    result = match(posting, profile)
    assert result.score == 1.0
    assert "assessed dimensions" in result.rationale


def test_posting_without_requirements_scores_zero():
    result = match(make_posting(), make_profile(skills=("Python",)))
    assert result.score == 0.0
    assert "no professional requirements" in result.rationale
    assert result.interview_topics == ()


def test_score_is_always_within_bounds():
    profiles = [
        make_profile(),
        make_profile(skills=("Python",), years_experience=1),
        make_profile(
            skills=("Python", "Docker", "Kubernetes"),
            years_experience=20,
            technologies=("AWS",),
            education=("PhD in Computer Science",),
            certifications=("CKA",),
            languages=("English", "Spanish"),
        ),
    ]
    postings = [
        make_posting(),
        make_posting(required_skills=("COBOL", "Fortran"), min_experience_years=15),
        make_posting(
            required_skills=("Python", "React"),
            preferred_skills=("Terraform",),
            min_experience_years=2,
            technologies=("AWS",),
            education=("Master's degree",),
            certifications=("CKA",),
            languages=("German",),
        ),
        make_posting(required_skills=("Python",), education=("Bachelor's degree",)),
    ]
    for profile in profiles:
        for posting in postings:
            result = match(posting, profile)
            assert 0.0 <= result.score <= 1.0


def test_single_stated_dimension_equals_dimension_score():
    # Only required_skills is stated: renormalization means the job score is
    # exactly the dimension subscore (1 exact + 1 missing = 0.5).
    profile = make_profile(skills=("Python",))
    posting = make_posting(required_skills=("Python", "Java"))
    assert match(posting, profile).score == 0.5


def test_stated_but_unproven_dimension_scores_zero():
    profile = make_profile()  # no skills, no certifications
    posting = make_posting(required_skills=("Python",), certifications=("AWS SAA",))
    result = match(posting, profile)
    assert result.score == 0.0
    assert "Missing required skill: Python" in result.interview_topics
    assert "Certification requirement not demonstrated: AWS SAA" in result.interview_topics


# --- Dimensions ------------------------------------------------------------


def test_strong_professional_match():
    profile = make_profile(
        skills=("Python", "FastAPI", "PostgreSQL", "Docker", "Kubernetes"),
        years_experience=6,
        technologies=("Python", "AWS", "Docker"),
        education=("Bachelor's in Computer Science",),
        certifications=("AWS Solutions Architect",),
        languages=("Spanish", "English"),
    )
    posting = make_posting(
        required_skills=("Python", "FastAPI", "PostgreSQL"),
        preferred_skills=("Kubernetes", "Terraform"),
        min_experience_years=5,
        technologies=("Python", "AWS"),
        education=("Bachelor's degree",),
        certifications=("AWS Solutions Architect",),
        languages=("English",),
    )
    result = match(posting, profile)
    assert result.score == pytest.approx(0.95)
    assert result.score >= 0.9
    assert "Python" in result.matched_skills
    assert "Terraform" in result.missing_preferred_skills
    assert result.missing_required_skills == ()
    assert "6 years of experience meets the 5 years required" in result.experience_matches
    assert "Terraform" in result.cv_keywords
    assert not any("Terraform" in topic for topic in result.interview_topics)


def test_partial_match_scores_half():
    profile = make_profile(skills=("React Native",))
    posting = make_posting(required_skills=("React",))
    result = match(posting, profile)
    assert result.score == 0.5
    assert result.partial_matches == ("React",)
    assert result.matched_skills == ()


def test_skill_aliases_match_exactly():
    profile = make_profile(skills=("Kubernetes", "PostgreSQL"))
    posting = make_posting(required_skills=("K8s", "Postgres"))
    result = match(posting, profile)
    assert result.score == 1.0
    assert result.matched_skills == ("K8s", "Postgres")


def test_missing_required_skills_penalize_and_become_topics():
    profile = make_profile(skills=("Python",))
    posting = make_posting(required_skills=("Python", "Cobol"))
    result = match(posting, profile)
    assert result.score == 0.5
    assert result.missing_required_skills == ("Cobol",)
    assert "Missing required skill: Cobol" in result.interview_topics


def test_missing_preferred_skills_have_small_documented_impact():
    base = dict(
        required_skills=("Python",),
        min_experience_years=3,
        technologies=("Python",),
        education=("Bachelor's degree",),
        certifications=("AWS SAA",),
        languages=("English",),
    )
    profile = make_profile(
        skills=("Python", "Kubernetes"),
        years_experience=5,
        technologies=("Python",),
        education=("Bachelor's in CS",),
        certifications=("AWS SAA",),
        languages=("English",),
    )
    with_one_preferred = match(
        make_posting(preferred_skills=("Kubernetes",), **base), profile
    )
    with_two_preferred = match(
        make_posting(preferred_skills=("Kubernetes", "Terraform"), **base), profile
    )
    # One extra missing preferred skill costs exactly weight 0.10 x 0.5 = 0.05.
    assert round(with_one_preferred.score - with_two_preferred.score, 4) == 0.05
    assert with_two_preferred.missing_preferred_skills == ("Terraform",)
    assert not any(
        "Terraform" in topic for topic in with_two_preferred.interview_topics
    )


@pytest.mark.parametrize(
    ("required_years", "expected"),
    [(2, 1.0), (5, 1.0), (6, 1.0), (8, 0.75), (12, 0.5)],
)
def test_different_experience_levels(required_years, expected):
    profile = make_profile(years_experience=6)
    posting = make_posting(min_experience_years=required_years)
    assert match(posting, profile).score == pytest.approx(expected)


def test_experience_not_stated_in_profile_scores_zero():
    profile = make_profile()
    posting = make_posting(min_experience_years=5)
    result = match(posting, profile)
    assert result.score == 0.0
    assert "Years of experience not stated in the profile" in result.interview_topics
    assert result.experience_matches == ()


def test_experience_shortfall_is_partial_and_a_topic():
    profile = make_profile(years_experience=3)
    posting = make_posting(min_experience_years=5)
    result = match(posting, profile)
    assert result.score == pytest.approx(0.6)
    assert "Experience shortfall: 3 of 5 years required" in result.interview_topics
    assert result.experience_matches == ()


@pytest.mark.parametrize(
    ("required_education", "profile_education", "expected"),
    [
        ("Bachelor's degree", "Master's in Computer Science", 1.0),
        ("Bachelor's degree", "Bachelor's in Systems", 1.0),
        ("Master's degree", "Bachelor's in Systems", 0.5),
        ("PhD", "Bachelor's in Systems", 0.0),
    ],
)
def test_education_cases(required_education, profile_education, expected):
    profile = make_profile(education=(profile_education,))
    posting = make_posting(education=(required_education,))
    result = match(posting, profile)
    assert result.score == pytest.approx(expected)


def test_education_not_stated_in_profile_scores_zero_with_topic():
    profile = make_profile()
    posting = make_posting(education=("Bachelor's degree",))
    result = match(posting, profile)
    assert result.score == 0.0
    assert any("profile lists no education" in topic for topic in result.interview_topics)


def test_certification_match_and_missing():
    matched = match(
        make_posting(certifications=("AWS Solutions Architect",)),
        make_profile(certifications=("AWS Solutions Architect",)),
    )
    assert matched.score == 1.0

    missing = match(
        make_posting(certifications=("AWS Solutions Architect",)),
        make_profile(certifications=("CKA",)),
    )
    assert missing.score == 0.0
    assert (
        "Certification requirement not demonstrated: AWS Solutions Architect"
        in missing.interview_topics
    )


def test_language_match_and_missing():
    matched = match(
        make_posting(languages=("English",)),
        make_profile(languages=("Spanish", "English")),
    )
    assert matched.score == 1.0

    missing = match(make_posting(languages=("German",)), make_profile(languages=("English",)))
    assert missing.score == 0.0
    assert "Language requirement not demonstrated: German" in missing.interview_topics


def test_technologies_count_as_required_tooling():
    profile = make_profile(skills=("Python",))
    posting = make_posting(required_skills=("Python",), technologies=("Kubernetes",))
    result = match(posting, profile)
    # assessed: required_skills 0.35x1.00 + technologies 0.15x0.00 -> 0.70
    assert result.score == 0.7
    assert "Kubernetes" in result.missing_required_skills
    assert "Missing required tool: Kubernetes" in result.interview_topics


def test_technologies_demonstrated_through_skills_count():
    profile = make_profile(skills=("Python", "AWS"))
    posting = make_posting(technologies=("Python", "AWS"))
    assert match(posting, profile).score == 1.0


def test_cv_keywords_derived_from_posting_requirements():
    profile = make_profile()
    posting = make_posting(
        required_skills=("Python", "Docker"),
        preferred_skills=("Terraform",),
        technologies=("AWS", "Python"),
    )
    result = match(posting, profile)
    assert result.cv_keywords == ("Python", "Docker", "Terraform", "AWS")


# --- Determinism and independence ------------------------------------------


def test_deterministic_repeated_results():
    profile = make_profile(skills=("Python",), years_experience=4)
    posting = make_posting(required_skills=("Python", "Go"), min_experience_years=5)
    results = [match(posting, profile) for _ in range(20)]
    assert all(result == results[0] for result in results)


def test_result_has_no_eligibility_fields():
    result = match(make_posting(), make_profile())
    names = {field.name for field in fields(result)}
    assert "score" in names
    assert "eligibility" not in names
    assert "status" not in names


def test_eligibility_phrasing_does_not_leak_into_the_score():
    profile = make_profile(skills=("Python",), years_experience=6)
    plain = match(make_posting(required_skills=("Python",)), profile)
    with_auth_text = match(
        make_posting(
            required_skills=("Python",),
            description="Must be authorized to work in the US. No sponsorship.",
        ),
        profile,
    )
    assert plain.score == 1.0
    assert with_auth_text.score == 1.0


def test_not_eligible_job_can_have_high_professional_match():
    profile = make_profile(
        skills=("Python", "FastAPI", "PostgreSQL", "Docker", "Kubernetes"),
        years_experience=6,
        technologies=("Python", "AWS", "Docker"),
        education=("Bachelor's in Computer Science",),
        certifications=("AWS Solutions Architect",),
        languages=("Spanish", "English"),
    )
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
    result = match(posting, profile)
    eligibility = evaluate_eligibility(posting, profile)
    assert result.score == pytest.approx(0.95)
    assert eligibility.status is EligibilityStatus.NOT_ELIGIBLE


def test_eligible_job_can_have_low_professional_match():
    profile = make_profile(
        skills=("Python",),
        years_experience=6,
        education=("Bachelor's in Systems",),
        languages=("Spanish", "English"),
    )
    posting = make_posting(
        location="Remote - LATAM",
        required_skills=("COBOL", "Mainframe", "SAP"),
        min_experience_years=12,
        education=("PhD",),
        languages=("German",),
    )
    result = match(posting, profile)
    eligibility = evaluate_eligibility(posting, profile)
    assert result.score <= 0.3
    assert result.score == pytest.approx(0.1429)
    assert eligibility.status is EligibilityStatus.ELIGIBLE
