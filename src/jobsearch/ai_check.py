"""Isolated AI extraction check (development utility).

Validates the existing :class:`~jobsearch.ai_extractor.AIExtractor`
against the configured chat-completions endpoint — for example Gemini
through its OpenAI-compatible URL — using **only** the provider-agnostic
settings ``AI_API_KEY``, ``AI_BASE_URL``, ``AI_MODEL`` and
``AI_TIMEOUT_SECONDS``. Configuration, never code, selects the provider:
no SDK is imported and the transport stays the existing standard-library
``urllib`` POST, so pointing ``AI_BASE_URL`` at any OpenAI-compatible
endpoint needs no change here.

Exactly one thing happens: the extractor is built from those settings and
asked to extract fields from the single hardcoded
:data:`SAMPLE_JOB_DESCRIPTION`, which is sent verbatim.

Guarantees:

* **Only the extractor runs** — no Gmail call, no ``JobStore``, no
  database file is opened or written, no eligibility or professional-match
  engine is consulted, and nothing is saved anywhere.
* **Secrets stay secret** — the report holds the endpoint, the model, a
  field count, and the extracted JSON (derived from the hardcoded sample).
  The API key travels only in the request's ``Authorization`` header and
  is never printed.
* **Extraction only** — the result is shown as-is; no score, no
  eligibility status, and no validation verdict is computed here.

Tests inject a fake opener, so no API key, account, or network access is
ever required.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

from .ai_extractor import AIExtractionError, AIExtractor
from .config import Settings, load_settings

__all__ = [
    "AICheckConfigError",
    "ExtractionReport",
    "SAMPLE_JOB_DESCRIPTION",
    "check_ai_extraction",
    "format_report",
    "main",
]

#: The single hardcoded input: one small job description, sent verbatim.
SAMPLE_JOB_DESCRIPTION = """\
Senior Backend Engineer - Remote

We are hiring a Senior Backend Engineer to design and build REST APIs.

Requirements:
- 5 years of experience with Python
- Experience with PostgreSQL and Docker
- Advanced English (written and spoken)

We offer:
- Salary: 95000 USD per year
- Remote work from LATAM
- Apply before October 31, 2026 at
  https://careers.example.com/jobs/senior-backend-engineer
"""

#: Same transport signature as the extractor's own ``opener`` argument.
_Opener = Callable[[urllib.request.Request, float], bytes]


class AICheckConfigError(RuntimeError):
    """The AI configuration is missing or invalid (exit code 2)."""


@dataclass(frozen=True)
class ExtractionReport:
    """Safe-to-print outcome: no key, no header, no secrets."""

    #: Full chat-completions URL built from ``AI_BASE_URL`` (no secret).
    endpoint: str
    #: The configured model name.
    model: str
    #: Length of the hardcoded sample actually sent.
    input_characters: int
    #: The JSON object the model returned (sample-derived fields only).
    fields: dict[str, object]


def check_ai_extraction(
    settings: Settings,
    *,
    opener: _Opener | None = None,
) -> ExtractionReport:
    """Build the existing extractor from ``AI_*`` settings and run one call.

    ``opener`` exists for tests: ``(request, timeout) -> bytes``. The
    default performs the real HTTP POST with ``urllib``. Raises
    :class:`AICheckConfigError` when the AI configuration is missing or
    invalid, and :class:`~jobsearch.ai_extractor.AIExtractionError` when
    the endpoint fails or answers with something unusable; both are
    actionable and never echo the key.
    """
    if not settings.ai_api_key.strip():
        raise AICheckConfigError(
            "AI_API_KEY is not set — the check needs it to call the "
            "chat-completions endpoint. Set AI_API_KEY in .env (never "
            "commit it) and point AI_BASE_URL / AI_MODEL at your provider "
            "(for Gemini's OpenAI-compatible endpoint see .env.example)."
        )

    try:
        extractor = AIExtractor(
            settings.ai_api_key,
            base_url=settings.ai_base_url,
            model=settings.ai_model,
            timeout=settings.ai_timeout_seconds,
            opener=opener,
        )
    except ValueError as exc:
        raise AICheckConfigError(
            f"invalid AI configuration: {exc} — check AI_BASE_URL, "
            "AI_MODEL and AI_TIMEOUT_SECONDS in .env (see .env.example)."
        ) from exc

    fields = extractor.extract(SAMPLE_JOB_DESCRIPTION)
    return ExtractionReport(
        endpoint=extractor.endpoint,
        model=settings.ai_model,
        input_characters=len(SAMPLE_JOB_DESCRIPTION),
        fields=fields,
    )


def format_report(report: ExtractionReport) -> str:
    """Deterministic, secret-free summary of a successful check."""
    return "\n".join(
        [
            "AI extraction check (isolated, nothing persisted)",
            f"  Endpoint          : {report.endpoint}",
            f"  Model             : {report.model}",
            "  Sample input      : 1 hardcoded job description "
            f"({report.input_characters} characters)",
            f"  Extracted fields  : {len(report.fields)}",
            "  Result            : succeeded (JSON object returned by "
            "the model)",
            "  Extracted JSON    : "
            + json.dumps(report.fields, ensure_ascii=False, sort_keys=True),
            "  Only AIExtractor ran: no Gmail call, no database opened, "
            "nothing saved.",
        ]
    )


def main(
    *,
    settings: Settings | None = None,
    opener: _Opener | None = None,
) -> int:
    """CLI entry point for ``python -m jobsearch.ai_check``.

    Exit codes: ``0`` extraction succeeded, ``2`` missing/invalid AI
    configuration, ``1`` endpoint or response failure. Output never
    contains the API key.
    """
    try:
        report = check_ai_extraction(
            settings if settings is not None else load_settings(),
            opener=opener,
        )
    except AICheckConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except AIExtractionError as exc:
        print(f"AI check failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(
            f"AI check failed unexpectedly: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1
    print(format_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
