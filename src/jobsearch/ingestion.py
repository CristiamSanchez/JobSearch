"""Manual ingestion helpers (stdlib only).

Parses JSON files (``data/*.json``) into the models in
:mod:`jobsearch.models`. Files are edited by hand — there is no external
data source yet.

``load_job_posting`` accepts either a dict already in memory or a path to a
JSON file; ``load_jobs`` and ``load_career_profile`` take a path.

Accepted job fields: ``title``, ``company``, ``location``, ``description``
(required), plus optional ``url``, ``source`` and the structured
professional requirements from Phase 3b: ``required_skills``,
``preferred_skills``, ``min_experience_years``, ``technologies``,
``education``, ``certifications``, ``languages``.

Accepted career-profile fields: ``country``, ``region`` and the four US
work-authorization flags (required), plus the optional professional fields
``skills``, ``years_experience``, ``technologies``, ``education``,
``certifications``, ``languages``.
"""

from __future__ import annotations

import json
from pathlib import Path

from .models import CandidateProfile, JobPosting

__all__ = ["load_jobs", "load_job_posting", "load_career_profile"]

PROFILE_FIELDS = (
    "country",
    "region",
    "us_citizenship",
    "us_green_card",
    "us_opt",
    "us_work_authorization",
)
JOB_REQUIRED_FIELDS = ("title", "company", "location", "description")


def _read_json(path: str | Path) -> object:
    """Read raw JSON from ``path`` or raise a helpful error."""
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")
    return json.loads(file_path.read_text(encoding="utf-8"))


def _load_object(source: dict | str | Path, label: str) -> dict:
    """Return a JSON object from an in-memory dict or a file path."""
    if isinstance(source, dict):
        return source
    if isinstance(source, (str, Path)):
        file_path = Path(source)
        data = _read_json(file_path)
        if not isinstance(data, dict):
            raise ValueError(f"{file_path} must contain a JSON object")
        return data
    raise ValueError(f"{label} must be a JSON object")


def _string_list(data: dict, name: str) -> tuple[str, ...]:
    """Read an optional list of non-empty strings (missing -> empty tuple)."""
    value = data.get(name)
    if value is None:
        return ()
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise ValueError(f"{name} must be a list of non-empty strings")
    return tuple(value)


def _optional_number(data: dict, name: str) -> float | None:
    """Read an optional non-negative number (missing/None -> None)."""
    value = data.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ValueError(f"{name} must be a non-negative number")
    return float(value)


def _optional_string(data: dict, name: str) -> str:
    """Read an optional string (missing/None -> empty string)."""
    value = data.get(name)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    return value


def load_job_posting(source: dict | str | Path) -> JobPosting:
    """Load a single job posting from a dict or a JSON file path."""
    data = _load_object(source, "job posting")

    missing = [field for field in JOB_REQUIRED_FIELDS if field not in data]
    if missing:
        raise ValueError(f"missing required fields: {', '.join(missing)}")

    return JobPosting(
        title=data["title"],
        company=data["company"],
        location=data["location"],
        description=data["description"],
        url=data.get("url", ""),
        source=data.get("source", "manual"),
        required_skills=_string_list(data, "required_skills"),
        preferred_skills=_string_list(data, "preferred_skills"),
        min_experience_years=_optional_number(data, "min_experience_years"),
        technologies=_string_list(data, "technologies"),
        education=_string_list(data, "education"),
        certifications=_string_list(data, "certifications"),
        languages=_string_list(data, "languages"),
        remote_mode=_optional_string(data, "remote_mode"),
        salary=_optional_string(data, "salary"),
        salary_currency=_optional_string(data, "salary_currency"),
        salary_period=_optional_string(data, "salary_period"),
        deadline=_optional_string(data, "deadline"),
    )


def load_jobs(path: str | Path) -> list[JobPosting]:
    """Load a list of job postings from a JSON file (must be a JSON array)."""
    data = _read_json(path)
    if not isinstance(data, list):
        raise ValueError(f"{path} must contain a JSON array")
    postings: list[JobPosting] = []
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError(f"Entry {index} in {path} must be a JSON object")
        try:
            postings.append(JobPosting(**item))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid job entry {index} in {path}: {exc}") from exc
    return postings


def load_career_profile(source: dict | str | Path) -> CandidateProfile:
    """Load the career profile from a dict or a JSON file path."""
    data = _load_object(source, "career profile")

    missing = [field for field in PROFILE_FIELDS if field not in data]
    if missing:
        raise ValueError(f"missing required fields: {', '.join(missing)}")

    return CandidateProfile(
        country=data["country"],
        region=data["region"],
        us_citizenship=data["us_citizenship"],
        us_green_card=data["us_green_card"],
        us_opt=data["us_opt"],
        us_work_authorization=data["us_work_authorization"],
        skills=_string_list(data, "skills"),
        years_experience=_optional_number(data, "years_experience"),
        technologies=_string_list(data, "technologies"),
        education=_string_list(data, "education"),
        certifications=_string_list(data, "certifications"),
        languages=_string_list(data, "languages"),
    )
