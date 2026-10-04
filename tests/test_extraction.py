"""Tests for AI-assisted extraction (Phase 3c): provider-neutral interface,
deterministic validation, evidence preservation, and the extraction →
engines pipeline. No API key and no network — every test uses a fake AI."""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path

import pytest

import jobsearch.extraction as extraction_module
from jobsearch.analysis import analyze_description, analyze_job
from jobsearch.eligibility import evaluate_eligibility
from jobsearch.extraction import (
    ExtractionValidationError,
    JobDescriptionExtractor,
    JobExtraction,
    extract_job_posting,
)
from jobsearch.models import (
    AuthorizationSignal,
    CandidateProfile,
    EligibilityStatus,
)
from jobsearch.professional_match import evaluate_professional_match

SOURCE = (
    "Backend Engineer at ACME. Remote - LATAM. "
    "Salary: 90000 USD per year. "
    "Must be authorized to work in the US. "
    "Visa sponsorship is not available. "
    "Apply at https://apply.example.com/jobs/42. Deadline: 2026-11-30."
)

PROFILE = CandidateProfile(
    country="Honduras",
    region="LATAM",
    skills=("Python", "PostgreSQL", "Kubernetes"),
    years_experience=6,
    technologies=("AWS",),
    education=("Bachelor's in Computer Science",),
    certifications=("AWS Solutions Architect",),
    languages=("Spanish", "English"),
)


class FakeExtractor:
    """Deterministic stand-in for a provider adapter (no key, no network)."""

    def __init__(self, payload):
        self.payload = payload
        self.calls: list[str] = []

    def extract(self, description: str) -> dict[str, object]:
        self.calls.append(description)
        return self.payload


def complete_payload() -> dict:
    return {
        "position": "Backend Engineer",
        "company": "ACME",
        "location": "Remote - LATAM",
        "description": SOURCE,
        "remote_mode": "Remote",
        "salary": "90000",
        "salary_currency": "usd",
        "salary_period": "Year",
        "required_skills": ["Python", "PostgreSQL"],
        "preferred_skills": ["Kubernetes"],
        "years_of_experience": 5,
        "technologies": ["AWS"],
        "education": ["Bachelor's degree"],
        "certifications": ["AWS Solutions Architect"],
        "languages": ["English"],
        "deadline": "2026-11-30",
        "application_url": "https://apply.example.com/jobs/42",
        "source": "jobboard",
        "provider_extra_field": "ignored by the schema",  # unknown keys tolerated
    }


def minimal_payload() -> dict:
    return {
        "position": "Backend Engineer",
        "company": "ACME",
        "location": "Remote - LATAM",
    }


# --- Provider interface ----------------------------------------------------


def test_extractor_interface_is_structural():
    assert isinstance(FakeExtractor({}), JobDescriptionExtractor)


def test_non_extractor_argument_fails_clearly():
    with pytest.raises(TypeError, match="extractor"):
        extract_job_posting(SOURCE, object())


def test_extractor_receives_the_raw_description_verbatim():
    fake = FakeExtractor(complete_payload())
    extract_job_posting(SOURCE, fake)
    assert fake.calls == [SOURCE]


# --- Complete and partial extraction ---------------------------------------


def test_complete_extraction_builds_structured_posting():
    posting = extract_job_posting(SOURCE, FakeExtractor(complete_payload()))

    assert posting.title == "Backend Engineer"
    assert posting.company == "ACME"
    assert posting.location == "Remote - LATAM"
    assert posting.description == SOURCE
    assert posting.url == "https://apply.example.com/jobs/42"
    assert posting.source == "jobboard"
    assert posting.remote_mode == "remote"  # normalized from "Remote"
    assert posting.salary == "90000"
    assert posting.salary_currency == "USD"  # uppercased from "usd"
    assert posting.salary_period == "year"  # lowercased from "Year"
    assert posting.deadline == "2026-11-30"
    assert posting.required_skills == ("Python", "PostgreSQL")
    assert posting.preferred_skills == ("Kubernetes",)
    assert posting.min_experience_years == 5.0
    assert posting.technologies == ("AWS",)
    assert posting.education == ("Bachelor's degree",)
    assert posting.certifications == ("AWS Solutions Architect",)
    assert posting.languages == ("English",)


def test_missing_optional_fields_use_safe_defaults():
    posting = extract_job_posting(SOURCE, FakeExtractor(minimal_payload()))

    assert posting.url == ""
    assert posting.source == "ai-extraction"
    assert posting.remote_mode == ""
    assert posting.salary == ""
    assert posting.salary_currency == ""
    assert posting.salary_period == ""
    assert posting.deadline == ""
    assert posting.min_experience_years is None
    assert posting.required_skills == ()
    assert posting.preferred_skills == ()
    assert posting.technologies == ()
    assert posting.education == ()
    assert posting.certifications == ()
    assert posting.languages == ()


# --- Required-field and malformed-output validation ------------------------


@pytest.mark.parametrize("field_name", ["position", "company", "location"])
def test_missing_required_extraction_field_fails(field_name):
    payload = complete_payload()
    del payload[field_name]
    with pytest.raises(ExtractionValidationError, match=field_name):
        extract_job_posting(SOURCE, FakeExtractor(payload))


def test_null_location_is_rejected_not_defaulted():
    payload = minimal_payload() | {"location": None}
    with pytest.raises(ExtractionValidationError, match="location is required"):
        extract_job_posting(SOURCE, FakeExtractor(payload))


def test_all_missing_required_fields_reported_together():
    with pytest.raises(ExtractionValidationError) as excinfo:
        extract_job_posting(SOURCE, FakeExtractor({}))
    message = str(excinfo.value)
    assert "position" in message
    assert "company" in message
    assert "location" in message


@pytest.mark.parametrize("raw", [None, [], "just text", 42])
def test_malformed_ai_output_fails_safely(raw):
    with pytest.raises(ExtractionValidationError, match="JSON object"):
        extract_job_posting(SOURCE, FakeExtractor(raw))


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("company", 42),
        ("description", 123),
        ("required_skills", "Python"),
        ("technologies", {"lang": "Python"}),
        ("years_of_experience", "plenty"),
        ("years_of_experience", True),
        ("remote_mode", 42),
        ("salary", ["90000"]),
        ("salary_currency", 7),
        ("application_url", 123),
        ("deadline", ["2026-11-30"]),
        ("source", 9),
    ],
)
def test_invalid_types_fail_validation(field_name, value):
    payload = complete_payload()
    payload[field_name] = value
    with pytest.raises(ExtractionValidationError, match=field_name):
        extract_job_posting(SOURCE, FakeExtractor(payload))


def test_empty_source_description_fails():
    with pytest.raises(ExtractionValidationError, match="non-empty"):
        extract_job_posting("", FakeExtractor(complete_payload()))


# --- Field-by-field extraction ---------------------------------------------


def test_salary_extraction():
    payload = minimal_payload() | {
        "salary": "90000 - 110000",
        "salary_currency": "usd",
        "salary_period": "Yearly",
    }
    posting = extract_job_posting(SOURCE, FakeExtractor(payload))
    assert posting.salary == "90000 - 110000"
    assert posting.salary_currency == "USD"
    assert posting.salary_period == "yearly"


def test_salary_values_are_normalized_but_never_invented():
    numeric = extract_job_posting(
        SOURCE, FakeExtractor(minimal_payload() | {"salary": 120000})
    )
    assert numeric.salary == "120000"

    unknown = extract_job_posting(
        SOURCE, FakeExtractor(minimal_payload() | {"salary": None})
    )
    assert unknown.salary == ""  # no default like "0" is invented


def test_location_extraction_is_verbatim():
    payload = minimal_payload() | {"location": "Hybrid - Tegucigalpa, Honduras"}
    posting = extract_job_posting(SOURCE, FakeExtractor(payload))
    assert posting.location == "Hybrid - Tegucigalpa, Honduras"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Remote", "remote"),
        ("hybrid", "hybrid"),
        ("On-site", "onsite"),
        ("on_site", "onsite"),
        ("Fully Remote", "remote"),
    ],
)
def test_remote_mode_extraction(value, expected):
    payload = minimal_payload() | {"remote_mode": value}
    posting = extract_job_posting(SOURCE, FakeExtractor(payload))
    assert posting.remote_mode == expected


def test_remote_mode_unrecognized_value_fails():
    payload = minimal_payload() | {"remote_mode": "telecommute"}
    with pytest.raises(ExtractionValidationError, match="remote_mode"):
        extract_job_posting(SOURCE, FakeExtractor(payload))


def test_required_and_preferred_skills_stay_separate():
    payload = complete_payload()
    payload["required_skills"] = ["Python"]
    payload["preferred_skills"] = ["Terraform"]
    posting = extract_job_posting(SOURCE, FakeExtractor(payload))
    assert posting.required_skills == ("Python",)
    assert posting.preferred_skills == ("Terraform",)


@pytest.mark.parametrize(
    ("value", "expected"),
    [(5, 5.0), (5.5, 5.5), ("5+", 5.0), ("7", 7.0)],
)
def test_experience_extraction(value, expected):
    payload = minimal_payload() | {"years_of_experience": value}
    posting = extract_job_posting(SOURCE, FakeExtractor(payload))
    assert posting.min_experience_years == expected


@pytest.mark.parametrize("value", ["plenty", -3, [5]])
def test_invalid_experience_fails(value):
    payload = minimal_payload() | {"years_of_experience": value}
    with pytest.raises(ExtractionValidationError, match="years_of_experience"):
        extract_job_posting(SOURCE, FakeExtractor(payload))


@pytest.mark.parametrize(
    "value", ["example.com/jobs/42", "ftp://files.example.com", "https://"]
)
def test_invalid_application_url_fails(value):
    payload = minimal_payload() | {"application_url": value}
    with pytest.raises(ExtractionValidationError, match="application_url"):
        extract_job_posting(SOURCE, FakeExtractor(payload))


def test_no_values_are_invented_for_unknown_fields():
    payload = minimal_payload() | {
        "description": None,
        "remote_mode": None,
        "salary": None,
        "salary_currency": None,
        "salary_period": None,
        "deadline": None,
        "application_url": None,
        "source": None,
        "years_of_experience": None,
        "required_skills": None,
        "preferred_skills": None,
        "technologies": None,
        "education": None,
        "certifications": None,
        "languages": None,
    }
    posting = extract_job_posting(SOURCE, FakeExtractor(payload))

    assert posting.remote_mode == ""
    assert posting.salary == ""
    assert posting.salary_currency == ""
    assert posting.salary_period == ""
    assert posting.deadline == ""
    assert posting.url == ""
    assert posting.min_experience_years is None
    assert posting.required_skills == ()
    assert posting.preferred_skills == ()
    assert posting.technologies == ()
    assert posting.education == ()
    assert posting.certifications == ()
    assert posting.languages == ()
    # The raw description is used verbatim — not rewritten or summarized.
    assert posting.description == SOURCE


def test_omitted_description_keeps_the_raw_source_text():
    payload = complete_payload()
    del payload["description"]
    posting = extract_job_posting(SOURCE, FakeExtractor(payload))
    assert posting.description == SOURCE


# --- Work-authorization evidence preservation ------------------------------


@pytest.mark.parametrize("with_description", [True, False])
def test_work_authorization_evidence_survives_extraction(with_description):
    payload = minimal_payload() | {"location": "Remote - US"}
    if with_description:
        payload["description"] = SOURCE

    posting = extract_job_posting(SOURCE, FakeExtractor(payload))

    assert "Must be authorized to work in the US" in posting.description
    result = evaluate_eligibility(posting, PROFILE)
    assert result.status is EligibilityStatus.NOT_ELIGIBLE
    assert AuthorizationSignal.AUTHORIZATION_REQUIRED in result.signals


def test_stripping_work_authorization_evidence_fails_safely():
    payload = complete_payload()
    payload["description"] = (
        "Backend Engineer at ACME. Remote - LATAM. "
        "Apply at https://apply.example.com/jobs/42."
    )
    with pytest.raises(ExtractionValidationError, match="evidence"):
        extract_job_posting(SOURCE, FakeExtractor(payload))


# --- Determinism -----------------------------------------------------------


def test_validation_errors_are_deterministic():
    payload = minimal_payload() | {"company": 42, "remote_mode": "telecommute"}
    messages = set()
    for _ in range(5):
        with pytest.raises(ExtractionValidationError) as excinfo:
            extract_job_posting(SOURCE, FakeExtractor(payload))
        messages.add(str(excinfo.value))
    assert len(messages) == 1


def test_extraction_results_are_deterministic():
    first = extract_job_posting(SOURCE, FakeExtractor(complete_payload()))
    second = extract_job_posting(SOURCE, FakeExtractor(complete_payload()))
    assert first == second


# --- Separation from the deterministic engines -----------------------------


def test_extraction_schema_carries_no_score_or_status():
    names = {field.name for field in fields(JobExtraction)}
    assert "score" not in names
    assert "status" not in names
    assert "eligibility" not in names


def test_extraction_module_never_calls_the_engines():
    source_text = Path(extraction_module.__file__).read_text(encoding="utf-8")
    assert "evaluate_professional_match(" not in source_text
    assert "evaluate_eligibility(" not in source_text


def test_extracted_posting_feeds_the_deterministic_engines_only():
    posting = extract_job_posting(SOURCE, FakeExtractor(complete_payload()))
    score = evaluate_professional_match(posting, PROFILE).score
    status = evaluate_eligibility(posting, PROFILE).status

    analysis = analyze_description(SOURCE, FakeExtractor(complete_payload()), PROFILE)

    assert analysis.professional_match.score == score
    assert analysis.eligibility.status == status
    assert analysis.professional_match == evaluate_professional_match(posting, PROFILE)
    assert analysis.eligibility == evaluate_eligibility(posting, PROFILE)


def test_not_eligible_job_can_still_extract_a_high_professional_match():
    payload = complete_payload() | {"location": "Remote - US"}
    analysis = analyze_description(SOURCE, FakeExtractor(payload), PROFILE)
    assert analysis.professional_match.score >= 0.9
    assert analysis.eligibility.status is EligibilityStatus.NOT_ELIGIBLE


def test_analyze_description_is_a_thin_composition_layer():
    posting = extract_job_posting(SOURCE, FakeExtractor(complete_payload()))
    analysis = analyze_description(SOURCE, FakeExtractor(complete_payload()), PROFILE)
    assert analysis == analyze_job(posting, PROFILE)
