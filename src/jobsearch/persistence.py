"""SQLite persistence and deterministic job deduplication (Phase 4C).

Turns the Phase 4B :class:`~jobsearch.models.EmailProcessingResult` into
durable local records while keeping three entities distinct:

* **Email** — an inbound message (evidence). ``message_id`` is unique, so
  processing the same message twice never creates a second row, and the
  original body is stored verbatim.
* **Job** — a normalized employment opportunity. Its identity comes from
  the deduplication rules below; it is **never** derived from the email's
  sender, provider, subject, ``message_id``, or ``source_hint``.
* **JobAnalysis** — the complete Phase 3a/3b analysis of a job, with the
  full ``ProfessionalMatch`` evidence and categorical ``EligibilityResult``
  (never just the score or status).

Many emails may reference one job: each email is preserved, and the
link table records how the email relates to the job.

Deduplication hierarchy (deterministic — no AI, no fuzzy matching, no
semantic judgments):

1. **Canonical application URL** — scheme and host preserved (lowercased),
   fragments removed, known tracking parameters (``utm_*``, ``fbclid``,
   ``gclid``) removed, every other parameter kept (some application URLs
   need them) and sorted so equal logical URLs compare equal. Two postings
   with the same canonical URL are the same job: an exact duplicate.
2. **Normalized attributes** — when the posting has no usable application
   URL, ``company``/``title``/``location``/``remote_mode`` are compared
   case-insensitively with collapsed whitespace. A full match is an exact
   duplicate.
3. **Email evidence (supporting only)** — an already-linked email with the
   same normalized subject *and* sender never merges jobs. It only records
   a ``POSSIBLE_DUPLICATE_REFERENCE`` edge while the new job row is still
   created, so uncertain records stay separate. Data preservation always
   wins over aggressive deduplication.

The ``source`` / ``source_hint`` values are stored as metadata only and
never participate in identity. Persistence stores and relates results; it
never classifies, scores, or decides eligibility — the deterministic
engines remain the only calculators.

Phase 5 added one column, ``jobs.status`` (the **manual** workflow state,
default ``FOUND``, added in place for pre-Phase-5 databases). ``get_status``
reads it and ``set_status`` writes it only after
:func:`jobsearch.workflow.can_transition` approves the transition;
persistence never derives a status from eligibility, scores, or emails.

Configured with ``DATABASE_PATH`` (default ``data/jobsearch.db``,
git-ignored); tests use in-memory or temporary databases. Standard library
``sqlite3`` only — no ORM, no server, no external service.

Phase 6A added the read-only :meth:`JobStore.get_outcome` lookup so the
manual Gmail sync can skip already-analyzed messages; it adds no write
path and changes no rule above.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .config import DEFAULT_DATABASE_PATH
from .models import (
    AuthorizationSignal,
    EligibilityResult,
    EligibilityStatus,
    EmailMessage,
    EmailProcessingOutcome,
    EmailProcessingResult,
    JobAnalysis,
    JobPosting,
    ProfessionalMatch,
)
from .workflow import WorkflowStatus, can_transition

__all__ = [
    "JobDedupStatus",
    "JobStore",
    "SavedRecord",
    "canonicalize_url",
]

# Explicit schema: keys, uniqueness, referential integrity, and lookup
# indexes. Foreign keys are enforced per connection.
SCHEMA = """
CREATE TABLE IF NOT EXISTS emails (
    email_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id  TEXT    NOT NULL UNIQUE,
    thread_id   TEXT    NOT NULL DEFAULT '',
    sender      TEXT    NOT NULL,
    received_at TEXT    NOT NULL,
    subject     TEXT    NOT NULL DEFAULT '',
    body        TEXT    NOT NULL DEFAULT '',
    source_hint TEXT,
    subject_key TEXT    NOT NULL DEFAULT '',
    sender_key  TEXT    NOT NULL DEFAULT '',
    outcome     TEXT    NOT NULL DEFAULT '',
    error       TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_emails_subject_sender
    ON emails(subject_key, sender_key);

CREATE TABLE IF NOT EXISTS jobs (
    job_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_url       TEXT    NOT NULL DEFAULT '',
    attribute_key       TEXT    NOT NULL DEFAULT '',
    title               TEXT    NOT NULL,
    company             TEXT    NOT NULL,
    location            TEXT    NOT NULL,
    description         TEXT    NOT NULL,
    url                 TEXT    NOT NULL DEFAULT '',
    source              TEXT    NOT NULL DEFAULT '',
    required_skills     TEXT    NOT NULL DEFAULT '[]',
    preferred_skills    TEXT    NOT NULL DEFAULT '[]',
    min_experience_years REAL,
    technologies        TEXT    NOT NULL DEFAULT '[]',
    education           TEXT    NOT NULL DEFAULT '[]',
    certifications      TEXT    NOT NULL DEFAULT '[]',
    languages           TEXT    NOT NULL DEFAULT '[]',
    remote_mode         TEXT    NOT NULL DEFAULT '',
    salary              TEXT    NOT NULL DEFAULT '',
    salary_currency     TEXT    NOT NULL DEFAULT '',
    salary_period       TEXT    NOT NULL DEFAULT '',
    deadline            TEXT    NOT NULL DEFAULT '',
    first_seen_at       TEXT    NOT NULL,
    last_seen_at        TEXT    NOT NULL,
    status              TEXT    NOT NULL DEFAULT 'FOUND'
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_canonical_url
    ON jobs(canonical_url) WHERE canonical_url <> '';
CREATE INDEX IF NOT EXISTS idx_jobs_attribute_key ON jobs(attribute_key);

CREATE TABLE IF NOT EXISTS email_jobs (
    email_id     INTEGER NOT NULL REFERENCES emails(email_id) ON DELETE CASCADE,
    job_id       INTEGER NOT NULL REFERENCES jobs(job_id)     ON DELETE CASCADE,
    relationship TEXT    NOT NULL,
    matched_by   TEXT    NOT NULL,
    PRIMARY KEY (email_id, job_id)
);
CREATE INDEX IF NOT EXISTS idx_email_jobs_job ON email_jobs(job_id);

CREATE TABLE IF NOT EXISTS job_analyses (
    job_id             INTEGER PRIMARY KEY
        REFERENCES jobs(job_id) ON DELETE CASCADE,
    professional_match TEXT NOT NULL,
    eligibility        TEXT NOT NULL,
    analyzed_at        TEXT NOT NULL
);
"""

# Email ↔ Job relationship vocabulary (persisted in email_jobs).
LINK_CREATED = "CREATED"
LINK_DUPLICATE = "DUPLICATE_REFERENCE"
LINK_POSSIBLE_DUPLICATE = "POSSIBLE_DUPLICATE_REFERENCE"

# How a link was determined (persisted in email_jobs.matched_by).
MATCH_NEW_JOB = "new_job"
MATCH_APPLICATION_URL = "application_url"
MATCH_ATTRIBUTES = "job_attributes"
MATCH_EMAIL_EVIDENCE = "email_subject_sender"

# Query parameters that only tag outbound links; safe to strip for identity.
TRACKING_QUERY_PARAMS = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "fbclid",
        "gclid",
    }
)


class JobDedupStatus(StrEnum):
    """How a save related the email's posting to existing jobs."""

    NEW = "NEW"  # no prior record matched: a new job row was created
    EXACT_DUPLICATE = "EXACT_DUPLICATE"  # deterministic identity evidence
    POSSIBLE_DUPLICATE = "POSSIBLE_DUPLICATE"  # similar, not merged


@dataclass(frozen=True)
class SavedRecord:
    """What one :meth:`JobStore.save` call persisted.

    ``job_id``/``dedup_status`` are set only for ``JOB_ANALYZED`` results;
    ``possible_duplicate_job_id`` additionally points at the similar-but-not-
    merged job when the supporting email evidence (level 3) fired.
    """

    email_id: int
    outcome: EmailProcessingOutcome
    job_id: int | None = None
    dedup_status: JobDedupStatus | None = None
    possible_duplicate_job_id: int | None = None


def canonicalize_url(url: str) -> str:
    """Normalize an application URL for identity comparison.

    Keeps the scheme and host (lowercased), drops the fragment, removes
    known tracking parameters (``utm_*``, ``fbclid``, ``gclid``), keeps
    every other parameter — some application URLs require them — and sorts
    the remaining pairs so equal logical URLs compare equal. Paths are left
    untouched. Returns ``""`` when the value is not a usable ``http(s)``
    address, which means identity falls back to normalized attributes.
    """
    text = (url or "").strip()
    if not text:
        return ""
    try:
        parts = urlsplit(text)
    except ValueError:
        return ""
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        return ""
    query_pairs = sorted(
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in TRACKING_QUERY_PARAMS
    )
    return urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            parts.path,
            urlencode(query_pairs),
            "",
        )
    )


def _normalize_text(value: str) -> str:
    """Case-insensitive, whitespace-collapsed form used for comparisons."""
    return re.sub(r"\s+", " ", (value or "").strip()).lower()


def _attribute_key(posting: JobPosting) -> str:
    """Level-2 identity: the four normalized job attributes, in order."""
    fields = (
        posting.company,
        posting.title,
        posting.location,
        posting.remote_mode,
    )
    return "\x1f".join(_normalize_text(field) for field in fields)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _dump_strings(values: tuple[str, ...]) -> str:
    return json.dumps(list(values), separators=(",", ":"))


def _load_strings(raw: str) -> tuple[str, ...]:
    return tuple(json.loads(raw))


def _load_string_list(values: object) -> tuple[str, ...]:
    """JSON array → tuple of strings (anything unexpected → empty)."""
    if isinstance(values, list):
        return tuple(str(value) for value in values)
    return ()


def _dump_analysis(analysis: JobAnalysis) -> tuple[str, str]:
    """Serialize the full match and eligibility structures as JSON."""
    match = json.dumps(
        asdict(analysis.professional_match), separators=(",", ":")
    )
    eligibility = json.dumps(
        asdict(analysis.eligibility), separators=(",", ":")
    )
    return match, eligibility


def _load_match(raw: str) -> ProfessionalMatch:
    data = json.loads(raw)
    return ProfessionalMatch(
        score=float(data["score"]),
        rationale=str(data.get("rationale", "")),
        matched_skills=_load_string_list(data.get("matched_skills")),
        partial_matches=_load_string_list(data.get("partial_matches")),
        missing_required_skills=_load_string_list(
            data.get("missing_required_skills")
        ),
        missing_preferred_skills=_load_string_list(
            data.get("missing_preferred_skills")
        ),
        experience_matches=_load_string_list(data.get("experience_matches")),
        cv_keywords=_load_string_list(data.get("cv_keywords")),
        interview_topics=_load_string_list(data.get("interview_topics")),
    )


def _load_eligibility(raw: str) -> EligibilityResult:
    data = json.loads(raw)
    return EligibilityResult(
        status=EligibilityStatus(data["status"]),
        reasons=_load_string_list(data.get("reasons")),
        signals=tuple(
            AuthorizationSignal(value) for value in data.get("signals", [])
        ),
    )


class JobStore:
    """Minimal SQLite repository: save Phase 4B results, read them back.

    One class with a handful of methods — deliberately no repository/
    service/DAO layers. Every connection enforces foreign keys, and each
    :meth:`save` runs in a single transaction: if anything fails, nothing
    is persisted and the error propagates (never swallowed).
    """

    def __init__(self, path: str | Path = DEFAULT_DATABASE_PATH) -> None:
        self._path = str(path)
        self._connection = sqlite3.connect(self._path)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Bring a pre-existing database up to the current schema.

        Phase 5 added the ``status`` column: databases created earlier
        keep every row and simply gain the column, so jobs persisted
        before Phase 5 default to ``FOUND``. The database is never
        recreated or wiped.
        """
        columns = {
            row["name"]
            for row in self._connection.execute("PRAGMA table_info(jobs)")
        }
        if "status" not in columns:
            self._connection.execute(
                "ALTER TABLE jobs ADD COLUMN status TEXT NOT NULL DEFAULT 'FOUND'"
            )

    @property
    def connection(self) -> sqlite3.Connection:
        """The underlying connection (tests and diagnostics read from it)."""
        return self._connection

    @property
    def path(self) -> str:
        return self._path

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "JobStore":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    # -- write ------------------------------------------------------------

    def save(
        self,
        result: EmailProcessingResult,
        *,
        now: datetime | None = None,
    ) -> SavedRecord:
        """Persist one Phase 4B result atomically.

        The email is always stored (body verbatim, ``message_id`` unique).
        Only ``JOB_ANALYZED`` results create a job, an email ↔ job link,
        and a job analysis; ``NOT_JOB`` and ``EXTRACTION_FAILED`` never do.
        The entire save is one transaction — a failure rolls back every
        write and re-raises, leaving no partially persisted job.
        """
        observed_at = _as_utc(now if now is not None else datetime.now(timezone.utc))
        stamp = observed_at.isoformat()

        with self._connection:
            email_id, email_inserted = self._store_email(result)

            if result.outcome is not EmailProcessingOutcome.JOB_ANALYZED:
                return SavedRecord(email_id=email_id, outcome=result.outcome)

            posting = result.job_posting
            analysis = result.analysis
            if posting is None or analysis is None:
                raise ValueError(
                    "JOB_ANALYZED result requires job_posting and analysis"
                )

            job_id, dedup_status, relationship, matched_by = (
                self._resolve_job(posting, stamp, update_last_seen=email_inserted)
            )

            self._link(email_id, job_id, relationship, matched_by)

            possible_job_id: int | None = None
            if dedup_status is JobDedupStatus.NEW:
                possible_job_id = self._find_possible_duplicate(
                    email_id, result.email
                )
                if possible_job_id is not None:
                    dedup_status = JobDedupStatus.POSSIBLE_DUPLICATE
                    self._link(
                        email_id,
                        possible_job_id,
                        LINK_POSSIBLE_DUPLICATE,
                        MATCH_EMAIL_EVIDENCE,
                    )

            self._write_analysis(job_id, analysis, stamp)

            return SavedRecord(
                email_id=email_id,
                outcome=result.outcome,
                job_id=job_id,
                dedup_status=dedup_status,
                possible_duplicate_job_id=possible_job_id,
            )

    def _store_email(self, result: EmailProcessingResult) -> tuple[int, bool]:
        """Insert the email (or reuse the row) and refresh its diagnostics.

        Returns ``(email_id, inserted)``. Existing rows keep their original
        content — the body is never rewritten — while the latest outcome and
        extraction error are updated.
        """
        email = result.email
        cursor = self._connection.execute(
            """
            INSERT OR IGNORE INTO emails (
                message_id, thread_id, sender, received_at, subject, body,
                source_hint, subject_key, sender_key, outcome, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                email.message_id,
                email.thread_id,
                email.sender,
                email.received_at.isoformat(),
                email.subject,
                email.body,
                email.source_hint,
                _normalize_text(email.subject),
                _normalize_text(email.sender),
                result.outcome.value,
                result.error,
            ),
        )
        inserted = cursor.rowcount == 1

        row = self._connection.execute(
            "SELECT email_id FROM emails WHERE message_id = ?",
            (email.message_id,),
        ).fetchone()
        if row is None:  # pragma: no cover - the row was just written
            raise sqlite3.DatabaseError("email row missing after insert")
        email_id = int(row["email_id"])

        if not inserted:
            self._connection.execute(
                "UPDATE emails SET outcome = ?, error = ? WHERE email_id = ?",
                (result.outcome.value, result.error, email_id),
            )
        return email_id, inserted

    def _resolve_job(
        self, posting: JobPosting, stamp: str, *, update_last_seen: bool
    ) -> tuple[int, JobDedupStatus, str, str]:
        """Find the posting's job (level 1 → level 2) or create it.

        ``last_seen_at`` moves forward only for a newly observed email —
        reprocessing the same message never rewrites observation times,
        and ``first_seen_at`` is never updated after creation.
        """
        canonical_url = canonicalize_url(posting.url)
        attribute_key = _attribute_key(posting)

        job_id: int | None = None
        matched_by = MATCH_NEW_JOB
        if canonical_url:
            job_id = self._find_by_canonical_url(canonical_url)
            matched_by = MATCH_APPLICATION_URL
        elif attribute_key:
            job_id = self._find_by_attribute_key(attribute_key)
            matched_by = MATCH_ATTRIBUTES

        if job_id is None:
            new_id = self._insert_job(posting, canonical_url, attribute_key, stamp)
            return new_id, JobDedupStatus.NEW, LINK_CREATED, MATCH_NEW_JOB

        if update_last_seen:
            self._connection.execute(
                "UPDATE jobs SET last_seen_at = ? WHERE job_id = ?",
                (stamp, job_id),
            )
        return job_id, JobDedupStatus.EXACT_DUPLICATE, LINK_DUPLICATE, matched_by

    def _find_by_canonical_url(self, canonical_url: str) -> int | None:
        row = self._connection.execute(
            "SELECT job_id FROM jobs WHERE canonical_url = ? "
            "ORDER BY job_id LIMIT 1",
            (canonical_url,),
        ).fetchone()
        return int(row["job_id"]) if row else None

    def _find_by_attribute_key(self, attribute_key: str) -> int | None:
        row = self._connection.execute(
            "SELECT job_id FROM jobs WHERE attribute_key = ? "
            "ORDER BY job_id LIMIT 1",
            (attribute_key,),
        ).fetchone()
        return int(row["job_id"]) if row else None

    def _find_possible_duplicate(
        self, email_id: int, email: EmailMessage
    ) -> int | None:
        """Level 3: supporting evidence only — never merges jobs.

        Looks for another email with the same normalized subject and sender
        that is already linked to a job. Returns that job id so a
        ``POSSIBLE_DUPLICATE_REFERENCE`` edge can be recorded; the posting's
        own new job row is always kept.
        """
        subject_key = _normalize_text(email.subject)
        sender_key = _normalize_text(email.sender)
        if not subject_key or not sender_key:
            return None
        row = self._connection.execute(
            """
            SELECT ej.job_id
              FROM email_jobs ej
              JOIN emails e ON e.email_id = ej.email_id
             WHERE e.subject_key = ?
               AND e.sender_key = ?
               AND e.email_id <> ?
               AND ej.job_id NOT IN (
                   SELECT job_id FROM email_jobs WHERE email_id = ?
               )
             ORDER BY ej.job_id
             LIMIT 1
            """,
            (subject_key, sender_key, email_id, email_id),
        ).fetchone()
        return int(row["job_id"]) if row else None

    def _insert_job(
        self,
        posting: JobPosting,
        canonical_url: str,
        attribute_key: str,
        stamp: str,
    ) -> int:
        cursor = self._connection.execute(
            """
            INSERT INTO jobs (
                canonical_url, attribute_key, title, company, location,
                description, url, source, required_skills, preferred_skills,
                min_experience_years, technologies, education, certifications,
                languages, remote_mode, salary, salary_currency, salary_period,
                deadline, first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                canonical_url,
                attribute_key,
                posting.title,
                posting.company,
                posting.location,
                posting.description,
                posting.url,
                posting.source,
                _dump_strings(posting.required_skills),
                _dump_strings(posting.preferred_skills),
                posting.min_experience_years,
                _dump_strings(posting.technologies),
                _dump_strings(posting.education),
                _dump_strings(posting.certifications),
                _dump_strings(posting.languages),
                posting.remote_mode,
                posting.salary,
                posting.salary_currency,
                posting.salary_period,
                posting.deadline,
                stamp,
                stamp,
            ),
        )
        job_id = cursor.lastrowid
        if job_id is None:  # pragma: no cover - the row was just written
            raise sqlite3.DatabaseError("jobs insert returned no row id")
        return int(job_id)

    def _link(
        self, email_id: int, job_id: int, relationship: str, matched_by: str
    ) -> None:
        self._connection.execute(
            """
            INSERT OR IGNORE INTO email_jobs
                (email_id, job_id, relationship, matched_by)
            VALUES (?, ?, ?, ?)
            """,
            (email_id, job_id, relationship, matched_by),
        )

    def _write_analysis(
        self, job_id: int, analysis: JobAnalysis, stamp: str
    ) -> None:
        """Persist the complete analysis; the latest one replaces the old."""
        match_json, eligibility_json = _dump_analysis(analysis)
        self._connection.execute(
            """
            INSERT INTO job_analyses
                (job_id, professional_match, eligibility, analyzed_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
                professional_match = excluded.professional_match,
                eligibility = excluded.eligibility,
                analyzed_at = excluded.analyzed_at
            """,
            (job_id, match_json, eligibility_json, stamp),
        )

    # -- read -------------------------------------------------------------

    def list_job_ids(self) -> tuple[int, ...]:
        """All job ids in insertion order — never ordered by score."""
        rows = self._connection.execute(
            "SELECT job_id FROM jobs ORDER BY job_id"
        ).fetchall()
        return tuple(int(row["job_id"]) for row in rows)

    def get_status(self, job_id: int) -> WorkflowStatus | None:
        """The stored workflow status; ``None`` for an unknown job."""
        row = self._connection.execute(
            "SELECT status FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        return WorkflowStatus(row["status"]) if row else None

    def get_seen(self, job_id: int) -> tuple[str, str] | None:
        """``(first_seen_at, last_seen_at)`` as stored ISO-8601 strings."""
        row = self._connection.execute(
            "SELECT first_seen_at, last_seen_at FROM jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        if row is None:
            return None
        return row["first_seen_at"], row["last_seen_at"]

    def set_status(
        self, job_id: int, status: WorkflowStatus | str
    ) -> WorkflowStatus:
        """Persist one manual workflow transition, validating it first.

        Raises ``LookupError`` for an unknown ``job_id`` and ``ValueError``
        for an unknown status value or a transition outside the allowed
        Phase 5 workflow; the stored status is left untouched in those
        cases. Eligibility and match scores never influence this.
        """
        try:
            new_status = WorkflowStatus(status)
        except ValueError:
            raise ValueError(f"unknown workflow status {status!r}") from None

        row = self._connection.execute(
            "SELECT status FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        if row is None:
            raise LookupError(f"unknown job_id {job_id}")

        current_status = WorkflowStatus(row["status"])
        if not can_transition(current_status, new_status):
            raise ValueError(
                f"transition {current_status.value} -> {new_status.value} "
                "is not allowed"
            )

        with self._connection:
            self._connection.execute(
                "UPDATE jobs SET status = ? WHERE job_id = ?",
                (new_status.value, job_id),
            )
        return new_status

    def get_email(self, message_id: str) -> EmailMessage | None:
        row = self._connection.execute(
            "SELECT * FROM emails WHERE message_id = ?", (message_id,)
        ).fetchone()
        if row is None:
            return None
        return EmailMessage(
            message_id=row["message_id"],
            sender=row["sender"],
            received_at=datetime.fromisoformat(row["received_at"]),
            thread_id=row["thread_id"],
            subject=row["subject"],
            body=row["body"],
            source_hint=row["source_hint"],
        )

    def get_outcome(self, message_id: str) -> EmailProcessingOutcome | None:
        """The stored pipeline outcome for one message; ``None`` if unknown.

        Added in Phase 6A as a **read-only** helper: the manual Gmail sync
        uses it to skip messages already analyzed instead of re-running
        extraction. It never writes, never changes what is stored, and has
        no influence on the deduplication rules or on workflow status.
        """
        row = self._connection.execute(
            "SELECT outcome FROM emails WHERE message_id = ?", (message_id,)
        ).fetchone()
        if row is None or not row["outcome"]:
            return None
        try:
            return EmailProcessingOutcome(row["outcome"])
        except ValueError:
            return None

    def get_job(self, job_id: int) -> JobPosting | None:
        row = self._connection.execute(
            "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        if row is None:
            return None
        return JobPosting(
            title=row["title"],
            company=row["company"],
            location=row["location"],
            description=row["description"],
            url=row["url"],
            source=row["source"],
            required_skills=_load_strings(row["required_skills"]),
            preferred_skills=_load_strings(row["preferred_skills"]),
            min_experience_years=row["min_experience_years"],
            technologies=_load_strings(row["technologies"]),
            education=_load_strings(row["education"]),
            certifications=_load_strings(row["certifications"]),
            languages=_load_strings(row["languages"]),
            remote_mode=row["remote_mode"],
            salary=row["salary"],
            salary_currency=row["salary_currency"],
            salary_period=row["salary_period"],
            deadline=row["deadline"],
        )

    def get_analysis(self, job_id: int) -> JobAnalysis | None:
        row = self._connection.execute(
            "SELECT * FROM job_analyses WHERE job_id = ?", (job_id,)
        ).fetchone()
        if row is None:
            return None
        return JobAnalysis(
            professional_match=_load_match(row["professional_match"]),
            eligibility=_load_eligibility(row["eligibility"]),
        )
