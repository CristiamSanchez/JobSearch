"""Tests for the core data models (Phase 2)."""

from dataclasses import fields

import pytest

from jobsearch.models import (
    AuthorizationSignal,
    CandidateProfile,
    EligibilityResult,
    EligibilityStatus,
    JobPosting,
    ProfessionalMatch,
)


def test_eligibility_status_has_exactly_the_required_values():
    assert set(EligibilityStatus) == {
        EligibilityStatus.ELIGIBLE,
        EligibilityStatus.NOT_ELIGIBLE,
        EligibilityStatus.REVIEW_REQUIRED,
        EligibilityStatus.UNKNOWN,
    }


def test_authorization_signal_has_exactly_the_required_values():
    assert set(AuthorizationSignal) == {
        AuthorizationSignal.AUTHORIZATION_REQUIRED,
        AuthorizationSignal.SPONSORSHIP_AVAILABLE,
        AuthorizationSignal.SPONSORSHIP_UNAVAILABLE,
        AuthorizationSignal.REQUIREMENT_SATISFIED,
        AuthorizationSignal.AMBIGUOUS,
    }


def test_eligibility_is_categorical_and_never_a_score():
    result = EligibilityResult(
        status=EligibilityStatus.REVIEW_REQUIRED,
        reasons=("remote role may restrict region",),
        signals=(AuthorizationSignal.AMBIGUOUS,),
    )
    eligibility_fields = {field.name for field in fields(result)}

    assert "status" in eligibility_fields
    assert "score" not in eligibility_fields
    # Professional match is the only score-bearing evaluation concept.
    assert "score" in {field.name for field in fields(ProfessionalMatch)}


def test_professional_match_score_must_be_between_zero_and_one():
    assert ProfessionalMatch(score=0.0).score == 0.0
    assert ProfessionalMatch(score=1.0).score == 1.0

    with pytest.raises(ValueError):
        ProfessionalMatch(score=1.5)
    with pytest.raises(ValueError):
        ProfessionalMatch(score=-0.01)
    with pytest.raises(ValueError):
        ProfessionalMatch(score="high")


def test_candidate_profile_defaults_have_no_us_authorization():
    profile = CandidateProfile(country="Honduras", region="LATAM")

    assert profile.country == "Honduras"
    assert profile.region == "LATAM"
    assert profile.us_citizenship is False
    assert profile.us_green_card is False
    assert profile.us_opt is False
    assert profile.us_work_authorization is False


def test_candidate_profile_rejects_empty_or_non_string_values():
    with pytest.raises(ValueError):
        CandidateProfile(country="  ", region="LATAM")
    with pytest.raises(ValueError):
        CandidateProfile(country="Honduras", region="")


def test_candidate_profile_rejects_non_boolean_flags():
    with pytest.raises(ValueError):
        CandidateProfile(country="Honduras", region="LATAM", us_citizenship="yes")


def test_job_posting_requires_non_empty_core_fields():
    valid = dict(title="Engineer", company="ACME", location="Remote", description="Do things.")

    with pytest.raises(ValueError):
        JobPosting(**{**valid, "title": ""})
    with pytest.raises(ValueError):
        JobPosting(**{**valid, "description": "   "})
    with pytest.raises(ValueError):
        JobPosting(**{**valid, "title": 42})


def test_job_posting_optional_fields_default_to_empty():
    posting = JobPosting(
        title="Engineer",
        company="ACME",
        location="Remote - LATAM",
        description="Do things.",
    )

    assert posting.url == ""
    assert posting.source == "manual"
