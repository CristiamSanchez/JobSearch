"""AI-assisted job description extraction (Phase 3c), provider-neutral.

Architecture rule: **the AI extracts and structures information only.**

* A provider implements :class:`JobDescriptionExtractor` — a small interface
  that accepts raw job-description text and returns the extracted data as a
  JSON object.
* :meth:`JobExtraction.from_raw` validates that data; invalid or incomplete
  output fails safely with :class:`ExtractionValidationError`.
* :func:`extract_job_posting` turns the validated extraction into a
  structured :class:`~jobsearch.models.JobPosting`.

Deterministic-only responsibilities (never this module):

* ``ProfessionalMatch.score`` is produced exclusively by
  :func:`jobsearch.professional_match.evaluate_professional_match`.
* ``EligibilityResult.status`` is produced exclusively by
  :func:`jobsearch.eligibility.evaluate_eligibility`.

Uncertainty: fields the AI cannot determine must be ``null``. Nothing is
guessed — no salary, location, or work-authorization values are invented;
missing required fields (``position``, ``company``, ``location``) raise a
clear validation error instead of being defaulted. Work-authorization
language stays in the posting description: the raw source text is used
verbatim when the AI omits ``description``, and when the AI supplies one it
must retain the source's work-authorization sentences or validation fails.

No API key, provider SDK, network access, scraping, or browser automation —
tests use deterministic fake extractors.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit

from .models import JobPosting

__all__ = [
    "ExtractionValidationError",
    "JobDescriptionExtractor",
    "JobExtraction",
    "extract_job_posting",
]

#: Fields the AI must determine — there is no safe default for them.
REQUIRED_EXTRACTION_FIELDS = ("position", "company", "location")

#: List-valued extraction fields (null means "not stated").
LIST_EXTRACTION_FIELDS = (
    "required_skills",
    "preferred_skills",
    "technologies",
    "education",
    "certifications",
    "languages",
)

#: Closed set of remote modes the extractor may report (normalized).
REMOTE_MODES = ("remote", "hybrid", "onsite")

# Work-authorization / citizenship / residency evidence that must never be
# stripped out of the description (mirrors what the eligibility analyzer
# inspects; see the README's eligibility rules).
_EVIDENCE_PATTERN = re.compile(
    r"u\.?s\.?\s*citizen|citizenship|green\s*card|permanent\s*resident|"
    r"work\s*authorization|work\s*authorisation|authori[sz]ed\s*to\s*work|"
    r"employment\s*authorization|work\s*permit|\bvisa\b|sponsorship|"
    r"\bsponsor|\bead\b|\bopt\b|\bcpt\b|\bh-?1b\b",
    re.IGNORECASE,
)


class ExtractionValidationError(ValueError):
    """Raised when AI extraction output is invalid or incomplete.

    Subclasses :class:`ValueError` so existing validation handling applies.
    """


@runtime_checkable
class JobDescriptionExtractor(Protocol):
    """Provider-neutral interface for the AI extraction step.

    Implementations wrap whatever provider is in use (OpenAI, Anthropic,
    Gemini, a local model, ...) without the core domain knowing which one.
    ``extract`` receives the raw job-description text and must return a
    JSON object of extracted fields; the core validates that object — the
    provider is never trusted to have validated its own output.
    """

    def extract(self, description: str) -> dict[str, object]:
        """Extract structured fields from ``description``."""
        ...


def _normalize(text: str) -> str:
    """Whitespace- and case-normalized form used for evidence comparison."""
    return re.sub(r"\s+", " ", text).strip().lower()


def _required_string(raw: Mapping, name: str, errors: list[str]) -> str:
    value = raw.get(name)
    if value is None:
        errors.append(f"{name} is required (missing or null)")
        return ""
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{name} must be a non-empty string")
        return ""
    return value.strip()


def _optional_string(
    raw: Mapping, name: str, errors: list[str], *, default: str = ""
) -> str:
    value = raw.get(name)
    if value is None:
        return default
    if not isinstance(value, str):
        errors.append(f"{name} must be a string")
        return default
    return value.strip() or default


def _optional_description(raw: Mapping, errors: list[str]) -> str | None:
    value = raw.get("description")
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        errors.append("description must be a non-empty string when provided")
        return None
    return value.strip()


def _classify_remote_mode(value: str) -> str | None:
    """Normalize a stated remote mode; ``None`` means unrecognized."""
    normalized = re.sub(r"[\s\-_]+", "", value.lower())
    if not normalized:
        return ""
    if normalized in REMOTE_MODES:
        return normalized
    # Tolerate wording like "Fully Remote" or "Hybrid (3 days onsite)".
    if "hybrid" in normalized:
        return "hybrid"
    if "remote" in normalized:
        return "remote"
    if "onsite" in normalized:
        return "onsite"
    return None


def _parse_remote_mode(raw: Mapping, errors: list[str]) -> str:
    value = raw.get("remote_mode")
    if value is None:
        return ""
    if not isinstance(value, str):
        errors.append("remote_mode must be a string")
        return ""
    mode = _classify_remote_mode(value)
    if mode is None:
        errors.append(
            f"remote_mode must be one of remote, hybrid, onsite (got {value!r})"
        )
        return ""
    return mode


def _parse_salary(raw: Mapping, errors: list[str]) -> str:
    value = raw.get("salary")
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        errors.append("salary must be a string or a finite number")
        return ""
    if not math.isfinite(value):
        errors.append("salary must be a string or a finite number")
        return ""
    return str(int(value)) if float(value).is_integer() else str(value)


def _parse_application_url(raw: Mapping, errors: list[str]) -> str:
    value = raw.get("application_url")
    if value is None:
        return ""
    if not isinstance(value, str):
        errors.append("application_url must be a string")
        return ""
    url = value.strip()
    if not url:
        return ""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        errors.append("application_url must be a valid http(s) URL")
        return ""
    return url


def _parse_years_of_experience(raw: Mapping, errors: list[str]) -> float | None:
    value = raw.get("years_of_experience")
    if value is None:
        return None
    if isinstance(value, bool):
        errors.append(
            "years_of_experience must be a non-negative number or numeric "
            "string (e.g. 5 or '5+')"
        )
        return None
    if isinstance(value, (int, float)):
        if not math.isfinite(value) or value < 0:
            errors.append("years_of_experience must be a non-negative number")
            return None
        return float(value)
    if isinstance(value, str):
        match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*\+?\s*", value)
        if match:
            return float(match.group(1))
        errors.append(
            "years_of_experience must be a non-negative number or numeric "
            "string (e.g. 5 or '5+')"
        )
        return None
    errors.append(
        "years_of_experience must be a non-negative number or numeric "
        "string (e.g. 5 or '5+')"
    )
    return None


def _string_tuple(raw: Mapping, name: str, errors: list[str]) -> tuple[str, ...]:
    value = raw.get(name)
    if value is None:
        return ()
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        errors.append(f"{name} must be a list of non-empty strings")
        return ()
    return tuple(item.strip() for item in value)


@dataclass(frozen=True)
class JobExtraction:
    """Validated structured data extracted from a job description.

    This is the extraction schema: exactly the fields the AI may provide,
    after type/required-field validation and deterministic normalization.
    It holds **no score and no status** — it cannot compute either; the
    deterministic engines produce those from the built :class:`JobPosting`.
    """

    position: str
    company: str
    location: str
    description: str | None = None  # None -> keep the raw source text
    remote_mode: str = ""  # normalized: "remote" | "hybrid" | "onsite" | ""
    salary: str = ""  # verbatim salary text as stated; never inferred
    salary_currency: str = ""  # uppercased, e.g. "USD"; never inferred
    salary_period: str = ""  # lowercased, e.g. "year"; never inferred
    deadline: str = ""  # verbatim deadline text as stated
    application_url: str = ""  # validated http(s) URL
    source: str = "ai-extraction"
    required_skills: tuple[str, ...] = ()
    preferred_skills: tuple[str, ...] = ()
    years_of_experience: float | None = None
    technologies: tuple[str, ...] = ()
    education: tuple[str, ...] = ()
    certifications: tuple[str, ...] = ()
    languages: tuple[str, ...] = ()

    @classmethod
    def from_raw(cls, raw: object) -> "JobExtraction":
        """Validate raw AI output, or raise :class:`ExtractionValidationError`.

        All problems found are reported together, in a fixed field order, so
        the same invalid input always produces the same error message.
        Unknown extra keys are ignored (providers may add their own).
        """
        if not isinstance(raw, Mapping):
            raise ExtractionValidationError(
                "invalid AI extraction: output must be a JSON object, "
                f"got {type(raw).__name__}"
            )

        errors: list[str] = []

        position = _required_string(raw, "position", errors)
        company = _required_string(raw, "company", errors)
        location = _required_string(raw, "location", errors)
        description = _optional_description(raw, errors)
        remote_mode = _parse_remote_mode(raw, errors)
        salary = _parse_salary(raw, errors)
        salary_currency = _optional_string(raw, "salary_currency", errors).upper()
        salary_period = _optional_string(raw, "salary_period", errors).lower()
        deadline = _optional_string(raw, "deadline", errors)
        application_url = _parse_application_url(raw, errors)
        source = _optional_string(raw, "source", errors, default="ai-extraction")
        required_skills = _string_tuple(raw, "required_skills", errors)
        preferred_skills = _string_tuple(raw, "preferred_skills", errors)
        years_of_experience = _parse_years_of_experience(raw, errors)
        technologies = _string_tuple(raw, "technologies", errors)
        education = _string_tuple(raw, "education", errors)
        certifications = _string_tuple(raw, "certifications", errors)
        languages = _string_tuple(raw, "languages", errors)

        if errors:
            raise ExtractionValidationError(
                "invalid AI extraction: " + "; ".join(errors)
            )

        return cls(
            position=position,
            company=company,
            location=location,
            description=description,
            remote_mode=remote_mode,
            salary=salary,
            salary_currency=salary_currency,
            salary_period=salary_period,
            deadline=deadline,
            application_url=application_url,
            source=source,
            required_skills=required_skills,
            preferred_skills=preferred_skills,
            years_of_experience=years_of_experience,
            technologies=technologies,
            education=education,
            certifications=certifications,
            languages=languages,
        )

    def to_job_posting(self, *, source_description: str) -> JobPosting:
        """Build a :class:`JobPosting`, preserving eligibility evidence.

        - ``description is None`` → the raw source text is used verbatim,
          so work-authorization language always survives extraction.
        - a provided ``description`` must retain every work-authorization
          sentence of the source, or :class:`ExtractionValidationError` is
          raised (fail safely rather than silently dropping evidence).
        """
        description = self.description
        if description is None:
            description = source_description
        else:
            _require_evidence_preserved(source_description, description)

        return JobPosting(
            title=self.position,
            company=self.company,
            location=self.location,
            description=description,
            url=self.application_url,
            source=self.source,
            required_skills=self.required_skills,
            preferred_skills=self.preferred_skills,
            min_experience_years=self.years_of_experience,
            technologies=self.technologies,
            education=self.education,
            certifications=self.certifications,
            languages=self.languages,
            remote_mode=self.remote_mode,
            salary=self.salary,
            salary_currency=self.salary_currency,
            salary_period=self.salary_period,
            deadline=self.deadline,
        )


def _require_evidence_preserved(source: str, extracted: str) -> None:
    """Fail if the AI's description dropped work-authorization evidence."""
    normalized_extracted = _normalize(extracted)
    for segment in re.split(r"[.!?\n;]+", source):
        segment = segment.strip()
        if not segment or not _EVIDENCE_PATTERN.search(segment):
            continue
        if _normalize(segment) not in normalized_extracted:
            raise ExtractionValidationError(
                "invalid AI extraction: work-authorization evidence was removed "
                "from description (preserve the original sentences, or omit the "
                "description field to keep the raw text)"
            )


def extract_job_posting(
    description: str, extractor: JobDescriptionExtractor
) -> JobPosting:
    """Raw description in, validated :class:`JobPosting` out.

    Runs the provider's extraction, validates its output
    (:meth:`JobExtraction.from_raw`), then builds the posting
    (:meth:`JobExtraction.to_job_posting`). Scoring and eligibility are
    deliberately left to the deterministic engines.
    """
    if not isinstance(description, str) or not description.strip():
        raise ExtractionValidationError("job description must be a non-empty string")
    if not isinstance(extractor, JobDescriptionExtractor):
        raise TypeError(
            "extractor must implement JobDescriptionExtractor "
            "(extract(description) -> dict)"
        )

    raw = extractor.extract(description)
    extraction = JobExtraction.from_raw(raw)
    return extraction.to_job_posting(source_description=description)
