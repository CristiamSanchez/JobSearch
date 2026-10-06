"""Phase 6A tests: AI extraction provider over HTTP (standard library, offline).

The transport opener is injected, so no API key, AI account, or network
access is ever required. The provider only extracts: a valid response is
also fed through the Phase 3c validation used by the real pipeline, and
every failure mode surfaces as :class:`AIExtractionError` (which the
pipeline turns into ``EXTRACTION_FAILED``) without ever leaking the key.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from jobsearch.ai_extractor import AIExtractionError, AIExtractor
from jobsearch.extraction import extract_job_posting
from jobsearch.models import JobPosting

API_KEY = "test-key-123"
BASE_URL = "https://ai.example.test/v1"

VALID_EXTRACTION: dict[str, object] = {
    "position": "QA Automation Engineer",
    "company": "Acme QA Labs",
    "location": "Remote - LATAM",
    "remote_mode": "remote",
    "required_skills": ["Python", "pytest"],
    "years_of_experience": 5,
}


def chat_response(content: str) -> bytes:
    """A chat-completions envelope wrapping ``content``."""
    return json.dumps(
        {"choices": [{"message": {"content": content}}]}
    ).encode("utf-8")


class RecordingOpener:
    """Fake transport: records every request, returns or raises on demand."""

    def __init__(
        self,
        response: bytes | None = None,
        error: Exception | None = None,
    ) -> None:
        self.response = (
            response
            if response is not None
            else chat_response(json.dumps(VALID_EXTRACTION))
        )
        self.error = error
        self.requests: list[tuple[urllib.request.Request, float]] = []

    def __call__(self, request: urllib.request.Request, timeout: float) -> bytes:
        self.requests.append((request, timeout))
        if self.error is not None:
            raise self.error
        return self.response


def make_extractor(opener: RecordingOpener) -> AIExtractor:
    return AIExtractor(
        API_KEY, base_url=BASE_URL, model="test-model", timeout=7, opener=opener
    )


# ---------------------------------------------------------------------------
# Request shape
# ---------------------------------------------------------------------------


def test_extract_posts_json_to_chat_completions_with_bearer_key() -> None:
    opener = RecordingOpener()
    extractor = make_extractor(opener)
    description = "Raw email body, sent verbatim."

    result = extractor.extract(description)

    assert result == VALID_EXTRACTION
    assert len(opener.requests) == 1
    request, timeout = opener.requests[0]
    assert request.full_url == f"{BASE_URL}/chat/completions"
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == f"Bearer {API_KEY}"
    assert timeout == 7.0

    payload = json.loads(request.data.decode("utf-8"))
    assert payload["model"] == "test-model"
    assert payload["temperature"] == 0
    assert payload["messages"][0]["role"] == "system"
    # The body reaches the model verbatim — never truncated or rewritten.
    assert payload["messages"][1] == {"role": "user", "content": description}


def test_extract_accepts_markdown_fenced_json() -> None:
    opener = RecordingOpener(
        chat_response(f"```json\n{json.dumps(VALID_EXTRACTION)}\n```")
    )
    assert make_extractor(opener).extract("body") == VALID_EXTRACTION


def test_openai_compatible_provider_envelope_is_accepted() -> None:
    """A full Gemini-style OpenAI-compatible envelope parses unchanged.

    The endpoint is selected by configuration, so the transport must stay
    tolerant of the extra fields a real provider adds (``id``, ``object``,
    ``created``, ``model``, ``index``, ``finish_reason``, ``usage``) and
    of fenced JSON in the content.
    """
    envelope = {
        "id": "chatcmpl-abc123",
        "object": "chat.completion",
        "created": 1_759_700_000,
        "model": "gemini-3.8-flash",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": f"```json\n{json.dumps(VALID_EXTRACTION)}\n```",
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 120,
            "completion_tokens": 60,
            "total_tokens": 180,
        },
    }
    opener = RecordingOpener(json.dumps(envelope).encode("utf-8"))
    assert make_extractor(opener).extract("body") == VALID_EXTRACTION


def test_extractor_endpoint_property_has_no_secret() -> None:
    extractor = make_extractor(RecordingOpener())
    assert extractor.endpoint == f"{BASE_URL}/chat/completions"
    assert API_KEY not in extractor.endpoint


# ---------------------------------------------------------------------------
# Failure modes: actionable messages, key never leaked
# ---------------------------------------------------------------------------


def test_invalid_json_response_is_reported_with_preview() -> None:
    opener = RecordingOpener(b"<html>oops, not json</html>")
    with pytest.raises(AIExtractionError) as excinfo:
        make_extractor(opener).extract("body")
    message = str(excinfo.value)
    assert "invalid JSON" in message
    assert "oops, not json" in message
    assert API_KEY not in message


def test_non_object_json_is_rejected() -> None:
    opener = RecordingOpener(chat_response("[1, 2, 3]"))
    with pytest.raises(AIExtractionError, match="must be a JSON object"):
        make_extractor(opener).extract("body")


def test_missing_message_content_is_rejected() -> None:
    opener = RecordingOpener(json.dumps({"choices": []}).encode("utf-8"))
    with pytest.raises(AIExtractionError, match="no message content"):
        make_extractor(opener).extract("body")


def test_non_json_model_output_is_rejected() -> None:
    opener = RecordingOpener(chat_response("Sorry, I cannot help."))
    with pytest.raises(AIExtractionError, match="was not JSON"):
        make_extractor(opener).extract("body")


def test_http_error_is_actionable_and_never_leaks_the_key() -> None:
    opener = RecordingOpener(
        error=urllib.error.HTTPError(
            f"{BASE_URL}/chat/completions", 401, "Unauthorized", None, None
        )
    )
    with pytest.raises(AIExtractionError) as excinfo:
        make_extractor(opener).extract("body")
    message = str(excinfo.value)
    assert "HTTP 401" in message
    assert "AI_API_KEY" in message
    assert "AI_BASE_URL" in message
    assert API_KEY not in message


def test_url_error_is_actionable_and_never_leaks_the_key() -> None:
    opener = RecordingOpener(
        error=urllib.error.URLError("name or service not known")
    )
    with pytest.raises(AIExtractionError) as excinfo:
        make_extractor(opener).extract("body")
    message = str(excinfo.value)
    assert BASE_URL in message
    assert "AI_BASE_URL" in message
    assert API_KEY not in message


def test_timeout_is_actionable() -> None:
    opener = RecordingOpener(error=TimeoutError())
    with pytest.raises(AIExtractionError) as excinfo:
        make_extractor(opener).extract("body")
    assert "AI_TIMEOUT_SECONDS" in str(excinfo.value)


def test_empty_description_fails_before_any_request() -> None:
    opener = RecordingOpener()
    with pytest.raises(AIExtractionError, match="non-empty string"):
        make_extractor(opener).extract("   ")
    assert opener.requests == []


# ---------------------------------------------------------------------------
# Construction validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"api_key": ""}, "api_key"),
        ({"api_key": "   "}, "api_key"),
        ({"api_key": API_KEY, "base_url": ""}, "base_url"),
        ({"api_key": API_KEY, "model": ""}, "model"),
        ({"api_key": API_KEY, "timeout": 0}, "timeout"),
    ],
)
def test_constructor_rejects_missing_configuration(
    kwargs: dict, match: str
) -> None:
    defaults: dict = {"base_url": BASE_URL, "model": "test-model", "timeout": 5}
    defaults.update(kwargs)
    with pytest.raises(ValueError, match=match):
        AIExtractor(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Pipeline compatibility (Phase 3c still validates the provider output)
# ---------------------------------------------------------------------------


def test_provider_output_feeds_the_existing_extraction_pipeline() -> None:
    extractor = make_extractor(RecordingOpener())
    posting = extract_job_posting("Raw email body.", extractor)
    assert isinstance(posting, JobPosting)
    assert posting.title == "QA Automation Engineer"
    assert posting.company == "Acme QA Labs"
    assert posting.location == "Remote - LATAM"
    assert posting.remote_mode == "remote"
