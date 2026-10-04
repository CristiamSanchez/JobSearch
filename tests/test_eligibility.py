"""Tests for the rule-based eligibility analyzer (Phase 3a)."""

from dataclasses import fields
from pathlib import Path

import pytest

from jobsearch.eligibility import evaluate_eligibility
from jobsearch.ingestion import load_career_profile
from jobsearch.models import (
    AuthorizationSignal,
    CandidateProfile,
    EligibilityResult,
    EligibilityStatus,
    JobPosting,
)

DATA_DIR = Path(__file__).resolve().parents[1] / "data"

PROFILE = CandidateProfile(country="Honduras", region="LATAM")


def make_posting(location: str = "Remote - LATAM", description: str = "") -> JobPosting:
    # JobPosting requires a non-empty description; an unspecified one becomes
    # an inert placeholder that triggers no eligibility rule.
    return JobPosting(
        title="Backend Engineer",
        company="ACME",
        location=location,
        description=description or "No description provided.",
    )


def evaluate(
    location: str = "Remote - LATAM",
    description: str = "",
    profile: CandidateProfile = PROFILE,
) -> EligibilityResult:
    return evaluate_eligibility(make_posting(location, description), profile)


def joined_reasons(result: EligibilityResult) -> str:
    return " ".join(result.reasons)


# --- Documented location rules ------------------------------------------------


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        # Remote LATAM / Latin America -> eligible
        ("Remote - LATAM", EligibilityStatus.ELIGIBLE),
        ("Remote - Latin America", EligibilityStatus.ELIGIBLE),
        ("Remote - Central America", EligibilityStatus.ELIGIBLE),
        # Remote Americas / Worldwide / Global -> review
        ("Remote - Americas", EligibilityStatus.REVIEW_REQUIRED),
        ("Remote - North America", EligibilityStatus.REVIEW_REQUIRED),
        ("Remote - Worldwide", EligibilityStatus.REVIEW_REQUIRED),
        ("Remote - Global", EligibilityStatus.REVIEW_REQUIRED),
        # US-only remote -> not eligible
        ("Remote - US", EligibilityStatus.NOT_ELIGIBLE),
        ("Remote - USA", EligibilityStatus.NOT_ELIGIBLE),
        ("Remote - United States", EligibilityStatus.NOT_ELIGIBLE),
        # Hybrid / on-site / in-person -> not eligible
        ("Hybrid", EligibilityStatus.NOT_ELIGIBLE),
        ("On-site", EligibilityStatus.NOT_ELIGIBLE),
        ("In-person", EligibilityStatus.NOT_ELIGIBLE),
    ],
)
def test_documented_location_rules(location: str, expected: EligibilityStatus):
    result = evaluate(location=location)

    assert result.status is expected
    assert result.reasons, "every decision must carry evidence"


def test_eligible_remote_latam_carries_satisfied_signal():
    result = evaluate(location="Remote - LATAM")

    assert result.status is EligibilityStatus.ELIGIBLE
    assert AuthorizationSignal.REQUIREMENT_SATISFIED in result.signals
    assert "Remote - LATAM" in joined_reasons(result)


def test_us_only_remote_reports_evidence_and_signal():
    result = evaluate(location="Remote - US")

    assert result.status is EligibilityStatus.NOT_ELIGIBLE
    assert AuthorizationSignal.AUTHORIZATION_REQUIRED in result.signals
    assert "United States" in joined_reasons(result)
    assert "Honduras" in joined_reasons(result)


# --- Both structured location and description are inspected -------------------


def test_description_can_restrict_an_apparently_remote_role():
    result = evaluate(
        location="Remote",
        description="This role is remote within the United States only.",
    )

    assert result.status is EligibilityStatus.NOT_ELIGIBLE
    assert "United States" in joined_reasons(result)


def test_description_can_make_a_remote_role_eligible():
    result = evaluate(
        location="Remote",
        description="Remote role open to candidates in Latin America.",
    )

    assert result.status is EligibilityStatus.ELIGIBLE


def test_work_mode_is_read_from_the_description_when_location_silent():
    result = evaluate(
        location="New York, NY",
        description="This is a remote position open worldwide.",
    )

    assert result.status is EligibilityStatus.REVIEW_REQUIRED


def test_conflicting_location_and_description_evidence_goes_to_review():
    result = evaluate(
        location="Remote - LATAM",
        description="Candidates must be based in the United States.",
    )

    assert result.status is EligibilityStatus.REVIEW_REQUIRED
    assert "Conflicting" in joined_reasons(result)
    assert AuthorizationSignal.AUTHORIZATION_REQUIRED in result.signals


def test_plain_remote_without_scope_goes_to_review_not_a_guess():
    result = evaluate(location="Remote", description="")

    assert result.status is EligibilityStatus.REVIEW_REQUIRED
    assert "not specified" in joined_reasons(result)


def test_us_time_zone_wording_is_not_treated_as_us_only_restriction():
    result = evaluate(location="Remote - US time zone", description="")

    assert result.status is not EligibilityStatus.NOT_ELIGIBLE
    assert result.status is EligibilityStatus.REVIEW_REQUIRED


# --- Authorization / citizenship / sponsorship detection ----------------------


def test_explicit_us_work_authorization_requirement_is_not_eligible():
    result = evaluate(
        location="Remote - LATAM",
        description="Must already have US work authorization.",
    )

    assert result.status is EligibilityStatus.NOT_ELIGIBLE
    assert AuthorizationSignal.AUTHORIZATION_REQUIRED in result.signals
    assert "must already have us work authorization" in joined_reasons(result)


def test_us_citizenship_requirement_is_not_eligible():
    result = evaluate(
        location="Remote - LATAM",
        description="Must be a U.S. citizen to apply.",
    )

    assert result.status is EligibilityStatus.NOT_ELIGIBLE
    assert AuthorizationSignal.AUTHORIZATION_REQUIRED in result.signals


def test_green_card_requirement_is_not_eligible():
    result = evaluate(
        location="Remote - LATAM",
        description="Permanent residency and a valid Green Card required.",
    )

    assert result.status is EligibilityStatus.NOT_ELIGIBLE


def test_authorized_to_work_phrasing_is_not_eligible():
    result = evaluate(
        location="Remote",
        description="Applicants must be authorized to work in the United States.",
    )

    assert result.status is EligibilityStatus.NOT_ELIGIBLE
    assert "authorized to work in the united states" in joined_reasons(result)


def test_sponsorship_available_is_not_an_automatic_rejection():
    result = evaluate(
        location="Remote - LATAM",
        description="Visa sponsorship available.",
    )

    assert result.status is not EligibilityStatus.NOT_ELIGIBLE
    assert result.status is EligibilityStatus.REVIEW_REQUIRED
    assert AuthorizationSignal.SPONSORSHIP_AVAILABLE in result.signals


def test_sponsorship_unavailable_goes_to_review():
    result = evaluate(
        location="Remote - LATAM",
        description="No visa sponsorship is offered.",
    )

    assert result.status is EligibilityStatus.REVIEW_REQUIRED
    assert AuthorizationSignal.SPONSORSHIP_UNAVAILABLE in result.signals


def test_authorization_requirement_with_sponsorship_offer_goes_to_review():
    result = evaluate(
        location="Remote - LATAM",
        description=(
            "Must be authorized to work in the United States. "
            "Visa sponsorship available."
        ),
    )

    assert result.status is EligibilityStatus.REVIEW_REQUIRED
    assert AuthorizationSignal.AUTHORIZATION_REQUIRED in result.signals
    assert AuthorizationSignal.SPONSORSHIP_AVAILABLE in result.signals


def test_explicit_non_requirement_is_satisfied():
    result = evaluate(
        location="Remote - LATAM",
        description="No prior US work authorization required.",
    )

    assert result.status is EligibilityStatus.ELIGIBLE
    assert AuthorizationSignal.REQUIREMENT_SATISFIED in result.signals


def test_unspecified_jurisdiction_is_ambiguous_not_a_rejection():
    # README ambiguous example: jurisdiction is not stated.
    result = evaluate(
        location="Remote - LATAM",
        description="Applicants must be work-authorized.",
    )

    assert result.status is EligibilityStatus.REVIEW_REQUIRED
    assert AuthorizationSignal.AMBIGUOUS in result.signals


def test_mere_term_mentions_do_not_reject():
    result = evaluate(
        location="Remote - LATAM",
        description=(
            "Experience building EAD processing tools is a plus. "
            "We value teamwork and curiosity."
        ),
    )

    assert result.status is EligibilityStatus.ELIGIBLE
    assert AuthorizationSignal.AUTHORIZATION_REQUIRED not in result.signals
    assert AuthorizationSignal.AMBIGUOUS not in result.signals


def test_requirement_is_met_when_candidate_holds_the_status():
    authorized = CandidateProfile(
        country="Honduras",
        region="LATAM",
        us_work_authorization=True,
    )
    result = evaluate(
        location="Remote - LATAM",
        description="Must already have US work authorization.",
        profile=authorized,
    )

    assert result.status is EligibilityStatus.ELIGIBLE
    assert AuthorizationSignal.REQUIREMENT_SATISFIED in result.signals


def test_location_and_authorization_are_evaluated_independently():
    result = evaluate(
        location="Hybrid",
        description="Visa sponsorship available.",
    )

    assert result.status is EligibilityStatus.NOT_ELIGIBLE  # hybrid dominates
    assert AuthorizationSignal.SPONSORSHIP_AVAILABLE in result.signals


# --- Edge cases, evidence, and the no-score guarantee -------------------------


def test_undetermined_work_mode_yields_unknown():
    result = evaluate(
        location="Tegucigalpa, Honduras",
        description="Join our team!",
    )

    assert result.status is EligibilityStatus.UNKNOWN
    assert result.reasons
    assert result.signals == ()


def test_result_never_contains_a_score():
    result = evaluate()

    field_names = {field.name for field in fields(result)}
    assert "score" not in field_names
    assert "status" in field_names


def test_reasons_preserve_concrete_matched_evidence():
    result = evaluate(
        location="Remote",
        description="This role is remote within the United States only.",
    )

    evidence = joined_reasons(result)
    assert "matched" in evidence
    assert "within the united states" in evidence


def test_repository_career_profile_drives_the_same_outcome():
    profile = load_career_profile(DATA_DIR / "career_profile.json")
    result = evaluate_eligibility(make_posting("Remote - LATAM"), profile)

    assert result.status is EligibilityStatus.ELIGIBLE


def test_signals_are_deduplicated():
    result = evaluate(
        location="Remote - US",
        description="Must already have US work authorization.",
    )

    assert result.status is EligibilityStatus.NOT_ELIGIBLE
    assert len(result.signals) == len(set(result.signals))
