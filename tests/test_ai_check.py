"""Isolated AI extraction check tests (offline: fake transport, no network).

No API key, AI account, or network access is ever required. Covered: the
check builds the existing ``AIExtractor`` from the provider-agnostic
``AI_*`` settings, sends exactly one request carrying the single hardcoded
sample verbatim, reports only secret-free information, exits with the
right code, and never touches Gmail, the store, or the database.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from jobsearch import ai_check
from jobsearch.ai_extractor import AIExtractionError, AIExtractor
from jobsearch.config import Settings

#: Canary key: tests assert it is never printed. Never a real credential.
API_KEY = "sk-fake-gemini-key-123"
BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
MODEL = "gemini-3.8-flash"

#: Usage patterns from other subsystems this module must never contain.
FORBIDDEN_REFERENCES = (
    "build_gmail_service",
    "sync_gmail",
    "JobStore(",
    "from .persistence",
    "load_career_profile",
    "detect_job_email",
    "process_job_email",
    "extract_job_posting",
    "evaluate_eligibility(",
    "evaluate_professional_match(",
    "ProfessionalMatch(",
    "EligibilityResult(",
)

VALID_EXTRACTION: dict[str, object] = {
    "position": "Senior Backend Engineer",
    "company": "Acme Platforms",
    "location": "Remote - LATAM",
    "remote_mode": "remote",
    "salary": "95000 USD per year",
    "required_skills": ["Python", "PostgreSQL", "Docker"],
    "years_of_experience": 5,
}


def chat_response(content: str) -> bytes:
    """A chat-completions envelope wrapping ``content``."""
    return json.dumps({"choices": [{"message": {"content": content}}]}).encode(
        "utf-8"
    )


class RecordingOpener:
    """Fake transport: records every request, returns or raises on demand."""

    def __init__(
        self, response: bytes | None = None, error: Exception | None = None
    ) -> None:
        self.response = (
            response if response is not None else chat_response(
                json.dumps(VALID_EXTRACTION)
            )
        )
        self.error = error
        self.requests: list[tuple[urllib.request.Request, float]] = []

    def __call__(self, request: urllib.request.Request, timeout: float) -> bytes:
        self.requests.append((request, timeout))
        if self.error is not None:
            raise self.error
        return self.response


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    """Settings for an isolated run; the AI key is the only secret."""
    values: dict = dict(
        ai_api_key=API_KEY,
        ai_base_url=BASE_URL,
        ai_model=MODEL,
        ai_timeout_seconds=30,
        database_path=str(tmp_path / "jobsearch.db"),
    )
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Reuse of the existing implementation
# ---------------------------------------------------------------------------


def test_check_binds_the_existing_ai_extractor() -> None:
    assert ai_check.AIExtractor is AIExtractor
    assert ai_check.AIExtractionError is AIExtractionError


def test_sample_job_description_is_hardcoded_and_non_empty() -> None:
    sample = ai_check.SAMPLE_JOB_DESCRIPTION
    assert isinstance(sample, str)
    assert sample.strip()
    # One fixed input, not something read from files, the network, or args.
    assert "Senior Backend Engineer" in sample


# ---------------------------------------------------------------------------
# Exactly one request, sample sent verbatim, no secret in the report
# ---------------------------------------------------------------------------


def test_check_posts_the_hardcoded_sample_exactly_once(
    tmp_path: Path,
) -> None:
    opener = RecordingOpener()
    ai_check.check_ai_extraction(make_settings(tmp_path), opener=opener)

    assert len(opener.requests) == 1
    request, timeout = opener.requests[0]
    assert request.full_url == f"{BASE_URL}/chat/completions"
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == f"Bearer {API_KEY}"
    assert timeout == 30.0

    payload = json.loads(request.data.decode("utf-8"))
    assert payload["model"] == MODEL
    assert payload["temperature"] == 0
    assert payload["messages"][0]["role"] == "system"
    assert payload["messages"][1] == {
        "role": "user",
        "content": ai_check.SAMPLE_JOB_DESCRIPTION,
    }


def test_report_has_endpoint_model_and_fields_but_never_the_key(
    tmp_path: Path,
) -> None:
    report = ai_check.check_ai_extraction(
        make_settings(tmp_path), opener=RecordingOpener()
    )
    assert report.endpoint == f"{BASE_URL}/chat/completions"
    assert report.model == MODEL
    assert report.input_characters == len(ai_check.SAMPLE_JOB_DESCRIPTION)
    assert report.fields == VALID_EXTRACTION
    assert API_KEY not in report.endpoint
    assert API_KEY not in report.model
    assert API_KEY not in json.dumps(report.fields)


# ---------------------------------------------------------------------------
# Configuration errors: actionable, no request sent
# ---------------------------------------------------------------------------


def test_missing_api_key_is_actionable_and_sends_nothing(tmp_path: Path) -> None:
    opener = RecordingOpener()
    with pytest.raises(ai_check.AICheckConfigError) as excinfo:
        ai_check.check_ai_extraction(
            make_settings(tmp_path, ai_api_key=""), opener=opener
        )
    assert "AI_API_KEY" in str(excinfo.value)
    assert opener.requests == []


def test_invalid_base_url_is_a_configuration_error(tmp_path: Path) -> None:
    opener = RecordingOpener()
    with pytest.raises(ai_check.AICheckConfigError) as excinfo:
        ai_check.check_ai_extraction(
            make_settings(tmp_path, ai_base_url=""), opener=opener
        )
    assert "AI_BASE_URL" in str(excinfo.value)
    assert opener.requests == []


def test_endpoint_failure_propagates_without_leaking_the_key(
    tmp_path: Path,
) -> None:
    opener = RecordingOpener(
        error=urllib.error.HTTPError(
            f"{BASE_URL}/chat/completions", 401, "Unauthorized", None, None
        )
    )
    with pytest.raises(AIExtractionError) as excinfo:
        ai_check.check_ai_extraction(make_settings(tmp_path), opener=opener)
    message = str(excinfo.value)
    assert "HTTP 401" in message
    assert "AI_API_KEY" in message
    assert API_KEY not in message


# ---------------------------------------------------------------------------
# Isolation: no store, no database, no other subsystem
# ---------------------------------------------------------------------------


def test_check_never_opens_the_database(tmp_path: Path) -> None:
    database = tmp_path / "jobsearch.db"
    ai_check.check_ai_extraction(
        make_settings(tmp_path), opener=RecordingOpener()
    )
    assert not database.exists()


def test_module_references_no_gmail_store_or_engine_symbols() -> None:
    source = Path(ai_check.__file__).read_text(encoding="utf-8")
    for symbol in FORBIDDEN_REFERENCES:
        assert symbol not in source, f"ai_check.py must not reference {symbol!r}"


# ---------------------------------------------------------------------------
# CLI: exit codes and secret-free output
# ---------------------------------------------------------------------------


def test_main_returns_zero_with_safe_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = ai_check.main(
        settings=make_settings(tmp_path), opener=RecordingOpener()
    )
    captured = capsys.readouterr()
    assert code == 0
    assert f"Endpoint          : {BASE_URL}/chat/completions" in captured.out
    assert f"Model             : {MODEL}" in captured.out
    assert "Result            : succeeded" in captured.out
    assert "Extracted JSON" in captured.out
    assert "no Gmail call" in captured.out
    combined = captured.out + captured.err
    assert API_KEY not in combined


def test_main_missing_key_exits_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = ai_check.main(
        settings=make_settings(tmp_path, ai_api_key=""),
        opener=RecordingOpener(),
    )
    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    assert "Configuration error" in captured.err
    assert "AI_API_KEY" in captured.err


def test_main_endpoint_failure_exits_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    opener = RecordingOpener(
        error=urllib.error.URLError("name or service not known")
    )
    code = ai_check.main(settings=make_settings(tmp_path), opener=opener)
    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert "AI check failed" in captured.err
    assert API_KEY not in captured.err


def test_main_loads_settings_through_load_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(ai_check, "load_settings", lambda: make_settings(tmp_path))
    assert ai_check.main(opener=RecordingOpener()) == 0
