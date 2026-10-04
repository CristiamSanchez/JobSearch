"""AI job-extraction provider over HTTP (Phase 6A), standard library only.

Concrete implementation of the Phase 3c
:class:`~jobsearch.extraction.JobDescriptionExtractor` protocol: the raw
job-description text (the email body, verbatim) is sent to any
OpenAI-compatible chat-completions endpoint and the JSON object that comes
back is returned for the existing validation layer to check.

Boundaries preserved:

* **Extraction only** — this provider returns structured fields and never
  computes a score or an eligibility status; the deterministic engines
  remain the only calculators, and :class:`~jobsearch.extraction.JobExtraction`
  still validates every field before a :class:`~jobsearch.models.JobPosting`
  is built.
* **Body verbatim** — the text arrives unchanged from the pipeline; it is
  not truncated or rewritten before the call, and the prompt requires
  ``null`` for anything not stated, so nothing is invented.
* **No SDK, no dependency** — a ``urllib`` POST with a timeout. The
  endpoint, model, and key come from configuration (``AI_BASE_URL``,
  ``AI_MODEL``, ``AI_API_KEY``); never from code.
* **Secrets stay secret** — the API key travels only in the request's
  ``Authorization`` header; error messages mention configuration variable
  names and never echo the key.
* **Fails safely** — network, HTTP, or malformed-response problems raise
  :class:`AIExtractionError`, which the pipeline turns into
  ``EXTRACTION_FAILED`` with the message preserved: one bad response never
  fabricates a posting and never crashes a sync.

Tests inject a fake opener, so no API key, account, or network access is
ever required.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable

from .extraction import JobDescriptionExtractor

__all__ = ["AIExtractionError", "AIExtractor"]

#: Instructions sent as the system message. The field list mirrors the
#: Phase 3c extraction schema exactly — unknown values must come back as
#: ``null`` instead of being guessed.
_SYSTEM_PROMPT = """\
You extract structured job-posting fields from an email body.

Respond with ONLY a single JSON object: no prose, no explanations, no
markdown code fences.

Keys (use null whenever the text does not state the value; never invent):
- "position": job title (string).
- "company": hiring organization (string).
- "location": job location as stated, e.g. "Remote - LATAM" (string).
- "description": full job description (string or null). Leave null to keep
  the original text untouched.
- "remote_mode": one of "remote", "hybrid", "onsite" (string or null).
- "salary": verbatim salary text as stated (string or null).
- "salary_currency": e.g. "USD" (string or null).
- "salary_period": e.g. "year" (string or null).
- "deadline": verbatim application deadline as stated (string or null).
- "application_url": full http(s) apply URL when present (string or null).
- "required_skills", "preferred_skills", "technologies", "education",
  "certifications", "languages": arrays of strings, or null.
- "years_of_experience": minimum years of experience as a number, or null.

Keep every sentence about work authorization, citizenship, residency, or
visas exactly as written if you provide "description".
"""

#: One POST → the raw response body. Injectable so tests stay offline.
_Opener = Callable[[urllib.request.Request, float], bytes]

_JSON_FENCE_PATTERN = re.compile(r"^```[a-zA-Z]*\s*|\s*```$", re.MULTILINE)


class AIExtractionError(RuntimeError):
    """AI extraction could not complete (network, HTTP, or bad response).

    Subclasses :class:`RuntimeError` so the Phase 4B pipeline catches it
    like any extractor exception and records ``EXTRACTION_FAILED`` with
    this message — never a fabricated posting.
    """


def _default_opener(request: urllib.request.Request, timeout: float) -> bytes:
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _preview(text: str, limit: int = 200) -> str:
    """Short, single-line excerpt of a response for error messages."""
    flattened = " ".join(text.split())
    return flattened if len(flattened) <= limit else flattened[:limit] + "..."


class AIExtractor:
    """``JobDescriptionExtractor`` backed by a chat-completions endpoint.

    ``opener`` exists for tests: ``(request, timeout) -> bytes``. The
    default performs the real HTTP POST with ``urllib``.
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str,
        model: str,
        timeout: float = 60,
        opener: _Opener | None = None,
    ) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("api_key must be a non-empty string")
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError("base_url must be a non-empty string")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model must be a non-empty string")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self._api_key = api_key.strip()
        self._base_url = base_url.strip().rstrip("/")
        self._model = model.strip()
        self._timeout = float(timeout)
        self._opener: _Opener = opener or _default_opener

    @property
    def endpoint(self) -> str:
        """The full chat-completions URL (no secret material)."""
        return f"{self._base_url}/chat/completions"

    def extract(self, description: str) -> dict[str, object]:
        """Extract structured fields from ``description`` (sent verbatim)."""
        if not isinstance(description, str) or not description.strip():
            raise AIExtractionError("job description must be a non-empty string")

        payload = json.dumps(
            {
                "model": self._model,
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": description},
                ],
            }
        ).encode("utf-8")

        request = urllib.request.Request(
            self.endpoint,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
            method="POST",
        )

        try:
            raw = self._opener(request, self._timeout)
        except urllib.error.HTTPError as exc:
            raise AIExtractionError(
                f"AI endpoint returned HTTP {exc.code} {exc.reason} at "
                f"{self._base_url} — check AI_API_KEY and AI_BASE_URL"
            ) from exc
        except urllib.error.URLError as exc:
            raise AIExtractionError(
                f"could not reach the AI endpoint {self._base_url}: "
                f"{exc.reason} — check AI_BASE_URL and network connectivity"
            ) from exc
        except TimeoutError as exc:
            raise AIExtractionError(
                f"AI endpoint timed out after {self._timeout:g}s at "
                f"{self._base_url} — check AI_TIMEOUT_SECONDS and connectivity"
            ) from exc
        except OSError as exc:
            raise AIExtractionError(
                f"could not reach the AI endpoint {self._base_url}: {exc} "
                "— check AI_BASE_URL and network connectivity"
            ) from exc

        return self._parse_response(raw)

    def _parse_response(self, raw: object) -> dict[str, object]:
        """Chat-completions envelope → the extracted JSON object."""
        if isinstance(raw, (bytes, bytearray)):
            text = bytes(raw).decode("utf-8", errors="replace")
        elif isinstance(raw, str):
            text = raw
        else:
            raise AIExtractionError(
                "AI endpoint returned an unexpected response type: "
                f"{type(raw).__name__}"
            )

        try:
            envelope = json.loads(text)
        except json.JSONDecodeError as exc:
            raise AIExtractionError(
                f"AI endpoint returned invalid JSON ({exc.msg} at line "
                f"{exc.lineno}): {_preview(text)}"
            ) from exc

        content = None
        if isinstance(envelope, dict):
            choices = envelope.get("choices")
            if isinstance(choices, list) and choices:
                first = choices[0]
                if isinstance(first, dict):
                    message = first.get("message")
                    if isinstance(message, dict):
                        content = message.get("content")

        if not isinstance(content, str) or not content.strip():
            raise AIExtractionError(
                "AI endpoint response has no message content: "
                f"{_preview(text)}"
            )

        cleaned = _JSON_FENCE_PATTERN.sub("", content.strip()).strip()
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise AIExtractionError(
                f"AI extraction was not JSON ({exc.msg}): {_preview(cleaned)}"
            ) from exc
        if not isinstance(data, dict):
            raise AIExtractionError(
                "AI extraction must be a JSON object, got "
                f"{type(data).__name__}: {_preview(cleaned)}"
            )
        return data
