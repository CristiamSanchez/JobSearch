"""Tests for manual job and career-profile ingestion from JSON files."""

import fnmatch
import json
import re
from pathlib import Path

import pytest

from jobsearch.ingestion import load_career_profile, load_job_posting, load_jobs
from jobsearch.models import JobPosting

DATA_DIR = Path(__file__).resolve().parents[1] / "data"


def test_load_job_posting_with_optional_fields():
    posting = load_job_posting(
        {
            "title": "Backend Engineer",
            "company": "ACME",
            "location": "Remote - LATAM",
            "description": "Build APIs.",
        }
    )

    assert isinstance(posting, JobPosting)
    assert posting.title == "Backend Engineer"
    assert posting.location == "Remote - LATAM"
    assert posting.url == ""
    assert posting.source == "manual"


def test_load_job_posting_with_optional_fields_set():
    posting = load_job_posting(
        {
            "title": "Backend Engineer",
            "company": "ACME",
            "location": "Remote - LATAM",
            "description": "Build APIs.",
            "url": "https://example.com/jobs/1",
            "source": "jobboard",
        }
    )

    assert posting.url == "https://example.com/jobs/1"
    assert posting.source == "jobboard"


def test_load_job_posting_rejects_missing_required_fields():
    with pytest.raises(ValueError, match="missing required fields"):
        load_job_posting({"title": "Engineer"})


def test_load_job_posting_rejects_non_object():
    with pytest.raises(ValueError, match="JSON object"):
        load_job_posting(["not", "an", "object"])


def test_load_jobs_from_array(tmp_path):
    jobs_file = tmp_path / "jobs.json"
    jobs_file.write_text(
        json.dumps(
            [
                {
                    "title": "First",
                    "company": "A",
                    "location": "Remote - LATAM",
                    "description": "One",
                },
                {
                    "title": "Second",
                    "company": "B",
                    "location": "Hybrid",
                    "description": "Two",
                    "source": "referral",
                },
            ]
        ),
        encoding="utf-8",
    )

    jobs = load_jobs(jobs_file)

    assert [job.title for job in jobs] == ["First", "Second"]
    assert jobs[1].source == "referral"


def test_load_jobs_accepts_empty_array(tmp_path):
    jobs_file = tmp_path / "jobs.json"
    jobs_file.write_text("[]", encoding="utf-8")

    assert load_jobs(jobs_file) == []


def test_load_jobs_rejects_non_array_file(tmp_path):
    jobs_file = tmp_path / "jobs.json"
    jobs_file.write_text('{"title": "Engineer"}', encoding="utf-8")

    with pytest.raises(ValueError, match="JSON array"):
        load_jobs(jobs_file)


def test_load_jobs_reports_malformed_json(tmp_path):
    jobs_file = tmp_path / "jobs.json"
    jobs_file.write_text("{not valid json", encoding="utf-8")

    with pytest.raises(json.JSONDecodeError):
        load_jobs(jobs_file)


def test_load_jobs_reports_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_jobs(tmp_path / "does-not-exist.json")


def test_repository_career_profile_matches_candidate_rules():
    # The repository ships the sanitized example; the real profile is
    # git-ignored and never part of a fresh clone.
    profile = load_career_profile(DATA_DIR / "career_profile.example.json")

    assert profile.country == "Honduras"
    assert profile.region == "LATAM"
    assert profile.us_citizenship is False
    assert profile.us_green_card is False
    assert profile.us_opt is False
    assert profile.us_work_authorization is False


def test_load_career_profile_rejects_missing_fields(tmp_path):
    profile_file = tmp_path / "career_profile.json"
    profile_file.write_text('{"country": "Honduras"}', encoding="utf-8")

    with pytest.raises(ValueError, match="missing required fields"):
        load_career_profile(profile_file)


def test_load_career_profile_rejects_non_boolean_flags(tmp_path):
    profile_file = tmp_path / "career_profile.json"
    profile_file.write_text(
        json.dumps(
            {
                "country": "Honduras",
                "region": "LATAM",
                "us_citizenship": "no",
                "us_green_card": False,
                "us_opt": False,
                "us_work_authorization": False,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="must be a boolean"):
        load_career_profile(profile_file)


def test_load_career_profile_rejects_non_object_file(tmp_path):
    profile_file = tmp_path / "career_profile.json"
    profile_file.write_text("[]", encoding="utf-8")

    with pytest.raises(ValueError, match="JSON object"):
        load_career_profile(profile_file)


def test_repository_jobs_file_is_a_valid_empty_list():
    assert load_jobs(DATA_DIR / "jobs.json") == []


def test_load_job_posting_with_structured_requirements(tmp_path):
    job_file = tmp_path / "job.json"
    job_file.write_text(
        json.dumps(
            {
                "title": "Backend Engineer",
                "company": "ACME",
                "location": "Remote - LATAM",
                "description": "Build backend services.",
                "required_skills": ["Python", "PostgreSQL"],
                "preferred_skills": ["Kubernetes"],
                "min_experience_years": 5,
                "technologies": ["AWS"],
                "education": ["Bachelor's degree"],
                "certifications": ["AWS Solutions Architect"],
                "languages": ["English"],
            }
        ),
        encoding="utf-8",
    )

    posting = load_job_posting(job_file)

    assert posting.required_skills == ("Python", "PostgreSQL")
    assert posting.preferred_skills == ("Kubernetes",)
    assert posting.min_experience_years == 5.0
    assert posting.technologies == ("AWS",)
    assert posting.education == ("Bachelor's degree",)
    assert posting.certifications == ("AWS Solutions Architect",)
    assert posting.languages == ("English",)


def test_load_job_posting_requirements_default_to_empty(tmp_path):
    job_file = tmp_path / "job.json"
    job_file.write_text(
        json.dumps(
            {
                "title": "Backend Engineer",
                "company": "ACME",
                "location": "Remote - LATAM",
                "description": "Build backend services.",
            }
        ),
        encoding="utf-8",
    )

    posting = load_job_posting(job_file)

    assert posting.required_skills == ()
    assert posting.preferred_skills == ()
    assert posting.min_experience_years is None
    assert posting.technologies == ()
    assert posting.education == ()
    assert posting.certifications == ()
    assert posting.languages == ()


def test_load_job_posting_rejects_invalid_requirements(tmp_path):
    job_file = tmp_path / "job.json"
    job_file.write_text(
        json.dumps(
            {
                "title": "Backend Engineer",
                "company": "ACME",
                "location": "Remote - LATAM",
                "description": "Build backend services.",
                "required_skills": ["Python", ""],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="required_skills must be a list"):
        load_job_posting(job_file)


def test_load_job_posting_rejects_negative_experience(tmp_path):
    job_file = tmp_path / "job.json"
    job_file.write_text(
        json.dumps(
            {
                "title": "Backend Engineer",
                "company": "ACME",
                "location": "Remote - LATAM",
                "description": "Build backend services.",
                "min_experience_years": -2,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="min_experience_years must be a non-negative"):
        load_job_posting(job_file)


def test_load_career_profile_with_professional_fields(tmp_path):
    profile_file = tmp_path / "career_profile.json"
    profile_file.write_text(
        json.dumps(
            {
                "country": "Honduras",
                "region": "LATAM",
                "us_citizenship": False,
                "us_green_card": False,
                "us_opt": False,
                "us_work_authorization": False,
                "skills": ["Python"],
                "years_experience": 6,
                "technologies": ["AWS"],
                "education": ["Bachelor's in Systems"],
                "certifications": [],
                "languages": ["Spanish", "English"],
            }
        ),
        encoding="utf-8",
    )

    profile = load_career_profile(profile_file)

    assert profile.skills == ("Python",)
    assert profile.years_experience == 6.0
    assert profile.technologies == ("AWS",)
    assert profile.education == ("Bachelor's in Systems",)
    assert profile.certifications == ()
    assert profile.languages == ("Spanish", "English")


def test_repository_career_profile_exposes_professional_fields():
    profile = load_career_profile(DATA_DIR / "career_profile.example.json")
    # The shipped example is fully populated so the professional matcher can
    # score a job instead of reporting "nothing to assess" everywhere.
    assert profile.skills != ()
    assert profile.years_experience is not None
    assert profile.years_experience > 0
    assert profile.technologies != ()
    assert profile.education != ()
    assert profile.certifications != ()
    assert profile.languages != ()


def test_example_career_profile_is_portfolio_safe():
    """The committed example carries only professional, non-personal facts."""
    example_path = DATA_DIR / "career_profile.example.json"
    profile = load_career_profile(example_path)
    raw = json.loads(example_path.read_text(encoding="utf-8"))

    # Every field the application expects is present and populated.
    assert {
        "country",
        "region",
        "us_citizenship",
        "us_green_card",
        "us_opt",
        "us_work_authorization",
        "skills",
        "years_experience",
        "technologies",
        "education",
        "certifications",
        "languages",
    } <= set(raw)
    assert isinstance(profile.years_experience, (int, float))
    assert not isinstance(profile.years_experience, bool)
    for name in ("skills", "technologies", "education", "certifications", "languages"):
        assert getattr(profile, name)
        assert all(isinstance(item, str) and item.strip() for item in getattr(profile, name))

    # No contact details or credentials ever ship to a public repository.
    forbidden_keys = {"password", "api_key", "token", "secret", "email", "phone", "address"}
    assert not forbidden_keys & set(raw)
    text = json.dumps(raw)
    assert "@" not in text
    assert re.search(r"\+\d[\d\s-]{6,}", text) is None


def test_example_profile_is_shipped_and_real_profile_stays_local():
    """Only the example travels to Git; the real profile stays on this machine."""
    ignore_lines = [
        line.strip()
        for line in (DATA_DIR.parent / ".gitignore").read_text(encoding="utf-8").splitlines()
    ]
    assert "data/career_profile.json" in ignore_lines

    example = "data/career_profile.example.json"
    patterns = [line for line in ignore_lines if line and not line.startswith("#")]
    assert example not in patterns
    assert not any(fnmatch.fnmatch(example, pattern) for pattern in patterns)


def test_load_job_posting_with_extracted_metadata(tmp_path):
    job_file = tmp_path / "job.json"
    job_file.write_text(
        json.dumps(
            {
                "title": "Backend Engineer",
                "company": "ACME",
                "location": "Remote - LATAM",
                "description": "Build backend services.",
                "remote_mode": "remote",
                "salary": "90000",
                "salary_currency": "USD",
                "salary_period": "year",
                "deadline": "2026-11-30",
            }
        ),
        encoding="utf-8",
    )

    posting = load_job_posting(job_file)

    assert posting.remote_mode == "remote"
    assert posting.salary == "90000"
    assert posting.salary_currency == "USD"
    assert posting.salary_period == "year"
    assert posting.deadline == "2026-11-30"


def test_load_job_posting_rejects_non_string_metadata(tmp_path):
    job_file = tmp_path / "job.json"
    job_file.write_text(
        json.dumps(
            {
                "title": "Backend Engineer",
                "company": "ACME",
                "location": "Remote - LATAM",
                "description": "Build backend services.",
                "salary": 90000,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="salary must be a string"):
        load_job_posting(job_file)
