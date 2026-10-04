"""Manual Gmail sync command (Phase 6A) — read-only, on demand, no scheduler.

``python -m jobsearch.gmail_sync`` pulls messages **once**, when you run
it, from a real Gmail account and feeds them through components that
already exist. This module is orchestration only:

    GMAIL_QUERY / GMAIL_MAX_RESULTS          (Settings)
      → GmailEmailProvider.search            Phase 4A, gmail.readonly
      → EmailMessage                         Phase 4A (plain text preferred,
                                             HTML preserved verbatim)
      → detect_job_email                     Phase 4A — NOT_JOB stops before AI
      → process_job_email                    Phase 4B: extract (Phase 3c via
                                             the AI provider) + analyze
                                             (Phases 3a + 3b)
      → JobStore.save                        Phase 4C deduplication, status FOUND
      → Phase 5 Command Center               reads the same SQLite database

Guarantees:

* **Read-only Gmail** — only ``users.messages.list`` /
  ``users.messages.get`` with the ``gmail.readonly`` scope; nothing is
  sent, deleted, modified, or marked as read.
* **Source-agnostic** — sender, domain, and ``source_hint`` are never
  read; classification is content-based (the Phase 4A detector), so the
  mailbox provider is never authoritative for what counts as a job email.
* **Idempotent** — ``message_id`` identity plus the Phase 4C dedup
  hierarchy mean re-running the sync never duplicates emails or jobs, and
  messages already analyzed are skipped *before* extraction, so repeat
  runs make no repeat AI calls.
* **One bad email never ends the run** — each message has its own error
  boundary; failures are counted and listed in the summary, not hidden.
* **Manual workflow untouched** — new jobs keep the Phase 5 default
  ``FOUND``; an application URL never implies ``APPLIED``.
* **Nothing automatic** — no scheduler, no polling, no background worker,
  no cron. This module runs only while the command runs; Notion is not
  involved in any way.

Secrets (OAuth files, ``AI_API_KEY``) come from configuration and are
never printed, logged, or embedded in error messages.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .ai_extractor import AIExtractor
from .config import Settings, load_settings
from .email_pipeline import process_job_email
from .email_provider import EmailProvider
from .extraction import JobDescriptionExtractor
from .gmail import GmailEmailProvider, build_gmail_service
from .ingestion import load_career_profile
from .models import CandidateProfile, EmailProcessingOutcome, JobLikelihood
from .persistence import JobDedupStatus, JobStore

__all__ = [
    "SyncConfigurationError",
    "SyncOperationError",
    "SyncSummary",
    "format_summary",
    "main",
    "run_sync",
    "sync_gmail",
]

#: Builds the authenticated Gmail service: ``(credentials_file, token_file)``.
_ServiceBuilder = Callable[[str, str], object]


class SyncConfigurationError(RuntimeError):
    """Configuration or OAuth problem: the sync cannot start.

    The message is actionable (which variable to set, which file to
    provide) and never contains secret material.
    """


class SyncOperationError(RuntimeError):
    """The sync started but could not complete (provider/network failure)."""


@dataclass(frozen=True)
class SyncSummary:
    """Deterministic counters for one manual sync run.

    Invariants: ``messages_found == messages_processed + reused`` and
    ``messages_processed == not_job + job_likely + possible_job``.
    """

    query: str = ""
    messages_found: int = 0
    messages_processed: int = 0
    #: Messages already stored as ``JOB_ANALYZED`` — skipped, not re-run.
    reused: int = 0
    not_job: int = 0
    job_likely: int = 0
    possible_job: int = 0
    jobs_analyzed: int = 0
    jobs_created: int = 0
    #: Postings matched to an existing job (Phase 4C level 1 or 2).
    duplicates: int = 0
    extraction_failures: int = 0
    persistence_failures: int = 0
    processing_failures: int = 0
    errors: tuple[str, ...] = ()


def sync_gmail(
    provider: EmailProvider,
    extractor: JobDescriptionExtractor,
    profile: CandidateProfile,
    store: JobStore,
    *,
    query: str,
) -> SyncSummary:
    """Search once, then process each message through the existing pipeline.

    Reuses :func:`jobsearch.email_pipeline.process_job_email` verbatim —
    detection, extraction, and analysis are never re-implemented here —
    and persists every result with :meth:`JobStore.save`, which applies
    the Phase 4C deduplication rules unchanged.

    Per message: skip when it is already stored as ``JOB_ANALYZED``
    (idempotency without repeat AI calls); otherwise run the pipeline
    inside a try/except so a single problematic message is counted and
    reported while the remaining messages continue.
    """
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must be a non-empty string")

    messages = provider.search(query)

    processed = 0
    reused = 0
    not_job = 0
    job_likely = 0
    possible_job = 0
    jobs_analyzed = 0
    jobs_created = 0
    duplicates = 0
    extraction_failures = 0
    persistence_failures = 0
    processing_failures = 0
    errors: list[str] = []

    for email in messages:
        if store.get_outcome(email.message_id) is EmailProcessingOutcome.JOB_ANALYZED:
            reused += 1
            continue

        processed += 1
        try:
            result = process_job_email(email, extractor, profile)
        except Exception as exc:  # one bad message must not end the sync
            processing_failures += 1
            errors.append(
                f"{email.message_id}: processing failed: "
                f"{type(exc).__name__}: {exc}"
            )
            continue

        if result.detection.likelihood is JobLikelihood.NOT_JOB:
            not_job += 1
        elif result.detection.likelihood is JobLikelihood.JOB_LIKELY:
            job_likely += 1
        elif result.detection.likelihood is JobLikelihood.POSSIBLE_JOB:
            possible_job += 1

        if result.outcome is EmailProcessingOutcome.JOB_ANALYZED:
            jobs_analyzed += 1
        elif result.outcome is EmailProcessingOutcome.EXTRACTION_FAILED:
            extraction_failures += 1
            errors.append(
                f"{email.message_id}: extraction failed: {result.error}"
            )

        try:
            record = store.save(result)
        except Exception as exc:  # persistence errors are reported, not fatal
            persistence_failures += 1
            errors.append(
                f"{email.message_id}: persistence failed: "
                f"{type(exc).__name__}: {exc}"
            )
            continue

        if record.dedup_status is JobDedupStatus.EXACT_DUPLICATE:
            duplicates += 1
        elif record.dedup_status in (
            JobDedupStatus.NEW,
            JobDedupStatus.POSSIBLE_DUPLICATE,
        ):
            jobs_created += 1

    return SyncSummary(
        query=query,
        messages_found=len(messages),
        messages_processed=processed,
        reused=reused,
        not_job=not_job,
        job_likely=job_likely,
        possible_job=possible_job,
        jobs_analyzed=jobs_analyzed,
        jobs_created=jobs_created,
        duplicates=duplicates,
        extraction_failures=extraction_failures,
        persistence_failures=persistence_failures,
        processing_failures=processing_failures,
        errors=tuple(errors),
    )


def format_summary(summary: SyncSummary) -> str:
    """Render the summary as deterministic fixed-order plain text."""
    lines = [
        "Gmail sync summary",
        f"  Query:                {summary.query}",
        f"  Messages found:       {summary.messages_found}",
        f"  Messages processed:   {summary.messages_processed}",
        f"  Already synced:       {summary.reused}",
        f"  NOT_JOB:              {summary.not_job}",
        f"  JOB_LIKELY:           {summary.job_likely}",
        f"  POSSIBLE_JOB:         {summary.possible_job}",
        f"  Jobs analyzed:        {summary.jobs_analyzed}",
        f"  New jobs created:     {summary.jobs_created}",
        f"  Duplicate jobs:       {summary.duplicates}",
        f"  Extraction failures:  {summary.extraction_failures}",
        f"  Persistence failures: {summary.persistence_failures}",
        f"  Processing failures:  {summary.processing_failures}",
    ]
    if summary.messages_found == 0:
        lines.append(
            "  No messages matched GMAIL_QUERY — nothing to sync "
            "(try widening the query)."
        )
    if summary.errors:
        lines.append("  Errors:")
        lines.extend(f"    - {error}" for error in summary.errors)
    return "\n".join(lines)


def run_sync(
    settings: Settings,
    *,
    service: object | None = None,
    service_builder: _ServiceBuilder | None = None,
    extractor: JobDescriptionExtractor | None = None,
    profile: CandidateProfile | None = None,
    store: JobStore | None = None,
) -> SyncSummary:
    """Validate configuration, build the Gmail service, run one sync.

    Everything is injectable for tests (``service``, ``service_builder``,
    ``extractor``, ``profile``, ``store``); the real path builds the
    read-only service with :func:`jobsearch.gmail.build_gmail_service`
    and the AI provider from ``AI_*`` settings. Static configuration is
    validated first — OAuth credentials, API key, and career profile are
    checked **before** any network call or browser prompt, raising
    :class:`SyncConfigurationError` with an actionable message.
    """
    if service is None:
        credentials_file = Path(settings.gmail_credentials_file)
        if not credentials_file.is_file():
            raise SyncConfigurationError(
                "Google OAuth client credentials not found: "
                f"{credentials_file}. Download an OAuth client ID file "
                "(type: Desktop app) from the Google Cloud console and point "
                "GMAIL_CREDENTIALS_FILE at it; the default location is under "
                "the git-ignored secrets/ directory (see .env.example). "
                "Access is read-only (scope gmail.readonly)."
            )

    if extractor is None:
        if not settings.ai_api_key:
            raise SyncConfigurationError(
                "AI_API_KEY is not set — the sync needs it to extract job "
                "fields from job emails. Set AI_API_KEY in .env (never "
                "commit it) or point AI_BASE_URL at your own endpoint; see "
                ".env.example."
            )
        extractor = AIExtractor(
            settings.ai_api_key,
            base_url=settings.ai_base_url,
            model=settings.ai_model,
            timeout=settings.ai_timeout_seconds,
        )

    if profile is None:
        profile_path = Path(settings.career_profile_path)
        if not profile_path.is_file():
            raise SyncConfigurationError(
                f"career profile not found: {profile_path}. Set "
                "CAREER_PROFILE_PATH to your local career profile JSON "
                "(see data/career_profile.json)."
            )
        try:
            profile = load_career_profile(profile_path)
        except Exception as exc:
            raise SyncConfigurationError(
                f"career profile {profile_path} is invalid: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

    if service is None:
        builder = service_builder or build_gmail_service
        try:
            service = builder(
                settings.gmail_credentials_file, settings.gmail_token_file
            )
        except Exception as exc:
            if "not installed" in str(exc) and "librar" in str(exc).lower():
                raise SyncConfigurationError(
                    "Gmail API client libraries are not installed — install "
                    "the optional Gmail dependencies listed in "
                    "requirements.txt (google-api-python-client, "
                    "google-auth, google-auth-oauthlib), then re-run the "
                    "sync."
                ) from exc
            raise SyncConfigurationError(
                "could not build the read-only Gmail service: "
                f"{type(exc).__name__}: {exc}. Re-check "
                "GMAIL_CREDENTIALS_FILE (a valid Desktop-app OAuth client "
                "JSON); if authorization was revoked, delete the token at "
                f"GMAIL_TOKEN_FILE={settings.gmail_token_file} and re-run "
                "to sign in again."
            ) from exc

    created_store = store is None
    if store is None:
        store = JobStore(settings.database_path)
    try:
        provider = GmailEmailProvider(
            service, max_results=settings.gmail_max_results
        )
        try:
            return sync_gmail(
                provider, extractor, profile, store, query=settings.gmail_query
            )
        except Exception as exc:
            message = (
                f"Gmail sync failed: {type(exc).__name__}: {exc}"
            )
            lowered = str(exc).lower()
            if any(
                token in lowered
                for token in ("401", "403", "unauthorized", "invalid_grant", "token")
            ):
                message += (
                    ". If authorization failed, delete the cached token at "
                    f"{settings.gmail_token_file} and re-run to sign in "
                    "again."
                )
            raise SyncOperationError(message) from exc
    finally:
        if created_store:
            store.close()


def main() -> int:
    """CLI entry point for ``python -m jobsearch.gmail_sync``.

    Exit codes: ``0`` sync completed (failures, if any, are listed in the
    printed summary), ``2`` configuration/OAuth error, ``1`` operational
    or unexpected failure. Error output never contains secrets.
    """
    try:
        settings = load_settings()
        summary = run_sync(settings)
    except SyncConfigurationError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except SyncOperationError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception as exc:
        print(
            f"Sync failed unexpectedly: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1
    print(format_summary(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
