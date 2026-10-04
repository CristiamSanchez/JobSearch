"""Deterministic job-email detector (Phase 4A).

A lightweight, content-based classifier that runs on a normalized
:class:`~jobsearch.models.EmailMessage` *before* any AI extraction:

* ``JOB_LIKELY``   — strong job signals present (score >= 4)
* ``POSSIBLE_JOB`` — one or two job-adjacent signals (score 1..3)
* ``NOT_JOB``      — no job-related signals at all (score 0)

This is a triage signal, **not** a validity decision: it never decides
whether an email truly contains an applicable job — that judgement belongs
to the later phases (AI extraction and the deterministic engines).

Source-agnostic by construction:

* sender addresses and domains are never inspected;
* no job-board or company names are hard-coded anywhere;
* ``source_hint`` is ignored entirely, so an unknown sender with job-like
  content classifies exactly like a known board;
* the message body is only read — never rewritten — so the original
  content stays available for later phases.

Scoring (documented in the README):

===============================  =====  ===================================
Signal                           Points Evidence recorded
===============================  =====  ===================================
Job term in the subject          +2     matched subject terms
Employment terms in the body     +1/+2  matched body terms (1-2 / >=3 terms)
Application URL in the body      +2     first matching URL
Careers URL in the body          +1     first matching URL
Recruiter terminology in body    +1     matched terms
Salary/position terminology      +1     matched terms
===============================  =====  ===================================
"""

from __future__ import annotations

import re

from .models import EmailMessage, JobEmailDetection, JobLikelihood

__all__ = ["JOB_LIKELY_MIN_SCORE", "POSSIBLE_JOB_MIN_SCORE", "detect_job_email"]

JOB_LIKELY_MIN_SCORE = 4
POSSIBLE_JOB_MIN_SCORE = 1

_SUBJECT_JOB_TERMS = (
    "job",
    "jobs",
    "vacancy",
    "vacancies",
    "opening",
    "openings",
    "position",
    "positions",
    "hiring",
    "recruiting",
    "recruitment",
    "opportunity",
    "opportunities",
    "role",
    "roles",
    "career",
    "careers",
    "apply",
    "application",
)

_BODY_EMPLOYMENT_TERMS = (
    "apply",
    "application",
    "applicant",
    "applicants",
    "hiring",
    "candidate",
    "candidates",
    "qualification",
    "qualifications",
    "responsibilities",
    "job description",
    "employment",
    "full-time",
    "part-time",
    "we are looking for",
    "join our team",
    "now hiring",
    "submit your application",
)

_RECRUITER_TERMS = (
    "recruiter",
    "talent acquisition",
    "hiring manager",
    "we received your application",
    "your application",
    "application received",
    "profile matches",
)

_SALARY_POSITION_TERMS = (
    "salary",
    "compensation",
    "per year",
    "per hour",
    "annual salary",
    "years of experience",
    "usd",
    "stipend",
    "base pay",
)

_URL_PATTERN = re.compile(r"https?://[^\s<>\"']+")
_APPLICATION_URL_PATTERN = re.compile(
    r"apply|application|jobs?/|job[-_]?id=|viewjob|/job\b|job=", re.IGNORECASE
)
_CAREERS_URL_PATTERN = re.compile(r"careers?\.|/careers?\b|talent\.", re.IGNORECASE)


def _match_terms(terms: tuple[str, ...], lowered_text: str) -> list[str]:
    """Return the terms present in ``lowered_text`` (already lowercase)."""
    matches: list[str] = []
    for term in terms:
        if " " in term:
            found = term in lowered_text
        else:
            found = re.search(rf"\b{re.escape(term)}\b", lowered_text) is not None
        if found:
            matches.append(term)
    return matches


def detect_job_email(message: EmailMessage) -> JobEmailDetection:
    """Classify ``message`` as ``JOB_LIKELY`` / ``POSSIBLE_JOB`` / ``NOT_JOB``.

    Deterministic and side-effect free: same message, same detection. Only
    ``subject`` and ``body`` are read — sender and ``source_hint`` play no
    part in the result.
    """
    signals: list[str] = []
    score = 0

    subject = (message.subject or "").lower()
    subject_matches = _match_terms(_SUBJECT_JOB_TERMS, subject)
    if subject_matches:
        score += 2
        signals.extend(subject_matches)

    body = (message.body or "").lower()

    body_matches = _match_terms(_BODY_EMPLOYMENT_TERMS, body)
    if len(body_matches) >= 3:
        score += 2
    elif body_matches:
        score += 1
    signals.extend(body_matches)

    urls = _URL_PATTERN.findall(body)
    application_urls = [url for url in urls if _APPLICATION_URL_PATTERN.search(url)]
    careers_urls = [url for url in urls if _CAREERS_URL_PATTERN.search(url)]
    if application_urls:
        score += 2
        signals.append(application_urls[0])
    if careers_urls:
        score += 1
        signals.append(careers_urls[0])

    recruiter_matches = _match_terms(_RECRUITER_TERMS, body)
    if recruiter_matches:
        score += 1
        signals.extend(recruiter_matches)

    salary_matches = _match_terms(_SALARY_POSITION_TERMS, body)
    if salary_matches:
        score += 1
        signals.extend(salary_matches)

    if score >= JOB_LIKELY_MIN_SCORE:
        likelihood = JobLikelihood.JOB_LIKELY
    elif score >= POSSIBLE_JOB_MIN_SCORE:
        likelihood = JobLikelihood.POSSIBLE_JOB
    else:
        likelihood = JobLikelihood.NOT_JOB

    return JobEmailDetection(
        likelihood=likelihood,
        signals=tuple(dict.fromkeys(signals)),
    )
