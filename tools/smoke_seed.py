#!/usr/bin/env python3
"""TEMPORARY visual smoke-test seeder for the Phase 5 Command Center.

This is **test tooling, not production code**: it is never imported by the
application and never runs during normal startup. It exists only to fill a
*temporary* SQLite database with realistic (obviously fictitious) records so
the dashboard can be verified visually against persisted data.

It uses only existing, supported mechanisms:

* the real Phase 4B pipeline (`process_job_email`) with a deterministic
  fake extractor (same pattern as the test suite);
* the real `JobStore.save()` persistence and deduplication path;
* the real `WorkflowStatus` enum advanced exclusively through
  `JobStore.set_status()` — every transition validated, nothing bypassed;
* the real eligibility (3a) and professional-match (3b) engines.

Usage (always pass an explicit temporary path — the production database
`data/jobsearch.db` must never be given here):

    PYTHONPATH=src .venv/bin/python tools/smoke_seed.py /tmp/opencode/phase5_smoke/smoke.db
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

from jobsearch.command_center import list_eligible_jobs
from jobsearch.email_pipeline import process_job_email
from jobsearch.models import (
    CandidateProfile,
    EmailMessage,
    EmailProcessingOutcome,
    EligibilityStatus,
)
from jobsearch.persistence import JobStore
from jobsearch.workflow import WorkflowStatus

NOW = datetime(2026, 10, 2, 9, 0, tzinfo=timezone.utc)

# Test-only profile (mirrors the structure used by the test suite). The
# production data/career_profile.json is NOT read or written by this tool.
TEST_PROFILE = CandidateProfile(
    country="Honduras",
    region="LATAM",
    skills=("Python", "PostgreSQL", "Kubernetes", "Docker", "AWS"),
    years_experience=6,
    technologies=("Python", "AWS", "Docker"),
    education=("Bachelor's in Computer Science",),
    languages=("Spanish", "English"),
)


class FakeExtractor:
    """Deterministic JobDescriptionExtractor (same shape as the tests)."""

    def __init__(self, payload: dict):
        self.payload = payload

    def extract(self, description: str) -> dict[str, object]:
        return self.payload


# Allowed transition chains used to reach each target status through the
# normal `JobStore.set_status()` validation — never written directly.
STATUS_CHAINS: dict[WorkflowStatus, list[WorkflowStatus]] = {
    WorkflowStatus.FOUND: [],
    WorkflowStatus.REVIEWED: [WorkflowStatus.REVIEWED],
    WorkflowStatus.SAVED: [WorkflowStatus.REVIEWED, WorkflowStatus.SAVED],
    WorkflowStatus.APPLIED: [
        WorkflowStatus.REVIEWED,
        WorkflowStatus.SAVED,
        WorkflowStatus.APPLIED,
    ],
    WorkflowStatus.INTERVIEW: [
        WorkflowStatus.REVIEWED,
        WorkflowStatus.SAVED,
        WorkflowStatus.APPLIED,
        WorkflowStatus.INTERVIEW,
    ],
    WorkflowStatus.OFFER: [
        WorkflowStatus.REVIEWED,
        WorkflowStatus.SAVED,
        WorkflowStatus.APPLIED,
        WorkflowStatus.INTERVIEW,
        WorkflowStatus.OFFER,
    ],
    WorkflowStatus.REJECTED: [
        WorkflowStatus.REVIEWED,
        WorkflowStatus.SAVED,
        WorkflowStatus.APPLIED,
        WorkflowStatus.REJECTED,
    ],
    WorkflowStatus.CLOSED: [
        WorkflowStatus.REVIEWED,
        WorkflowStatus.SAVED,
        WorkflowStatus.CLOSED,
    ],
}


def job(
    key: str,
    position: str,
    company: str,
    location: str,
    required: list[str],
    min_exp: int | None,
    *,
    technologies: list[str] | None = None,
    url: str = "",
    target: WorkflowStatus = WorkflowStatus.FOUND,
) -> dict:
    """One demo posting; `target` is reached later via set_status()."""
    remote = "Bogota, Colombia" not in location
    mode_sentence = "This is a remote position.\n" if remote else ""
    body = (
        f"We are hiring a {position} at {company}.\n\n"
        f"{mode_sentence}"
        f"Location: {location}.\n"
        "Requirements: "
        + ", ".join(required)
        + (f". {min_exp} years of experience expected." if min_exp else ".")
        + "\nSalary: 85000 per year. Apply now."
    )
    payload: dict = {
        "position": position,
        "company": company,
        "location": location,
        "required_skills": required,
        "languages": ["Spanish", "English"],
    }
    if remote:
        payload["remote_mode"] = "remote"
    if technologies:
        payload["technologies"] = technologies
    if min_exp:
        payload["years_of_experience"] = min_exp
    if url:
        payload["application_url"] = url
    return {
        "key": key,
        "subject": f"Job opening: {position}",
        "body": body,
        "payload": payload,
        "target": target,
    }


# 8 ELIGIBLE jobs — one per workflow status. Percentages come from the real
# Phase 3b scorer (tuned by required/experience inputs, never overridden).
DEMO_JOBS = [
    # 1. FOUND — also carries an application_url: URL ≠ APPLIED.
    #    "&" in the company exercises HTML escaping.
    job(
        "found",
        "QA Automation Engineer",
        "Acme QA Labs & Co",
        "Remote - Latin America",
        ["Python", "PostgreSQL", "Kubernetes", "Docker", "Terraform"],
        9,
        technologies=["AWS", "Python"],
        url="https://boards.example.com/jobs/201",
        target=WorkflowStatus.FOUND,
    ),
    # 2. REVIEWED
    job(
        "reviewed",
        "Junior DevOps Engineer",
        "NorthStar Software",
        "Latin America (remote)",
        ["Python", "PostgreSQL", "Selenium"],
        10,
        technologies=["AWS", "Docker"],
        target=WorkflowStatus.REVIEWED,
    ),
    # 3. SAVED — application_url while SAVED: URL ≠ APPLIED.
    job(
        "saved",
        "Software QA Analyst",
        "CloudTest Solutions",
        "Central America",
        ["Python", "PostgreSQL", "Kubernetes", "Docker", "AWS"],
        9,
        technologies=["Python", "AWS"],
        url="https://boards.example.com/jobs/203",
        target=WorkflowStatus.SAVED,
    ),
    # 4. APPLIED
    job(
        "applied",
        "QA Engineer (SDET)",
        "BrightPath Digital",
        "Remote - Latin America",
        [
            "Python",
            "PostgreSQL",
            "Kubernetes",
            "Docker",
            "AWS",
            "Jenkins",
            "Selenium",
            "JMeter",
            "Cypress",
        ],
        8,
        url="https://boards.example.com/jobs/204",
        target=WorkflowStatus.APPLIED,
    ),
    # 5. INTERVIEW
    job(
        "interview",
        "Senior QA Engineer",
        "Helio Software Group",
        "Remote - Latin America",
        ["Python", "PostgreSQL", "Kubernetes", "Docker", "AWS", "Jenkins"],
        7,
        technologies=["Python", "AWS"],
        target=WorkflowStatus.INTERVIEW,
    ),
    # 6. OFFER
    job(
        "offer",
        "Performance Test Engineer",
        "BluePeak Technologies",
        "Remote - Latin America",
        ["Python", "PostgreSQL", "Jenkins"],
        8,
        technologies=["AWS", "Docker"],
        target=WorkflowStatus.OFFER,
    ),
    # 7. REJECTED — "&" in the position exercises HTML escaping.
    job(
        "rejected",
        "QA Analyst (C++ & APIs)",
        "Meridian Web Works",
        "Remote - Latin America",
        [
            "Python",
            "PostgreSQL",
            "Kubernetes",
            "Docker",
            "AWS",
            "Grafana",
            "Ansible",
            "Terraform",
            "Splunk",
        ],
        9,
        target=WorkflowStatus.REJECTED,
    ),
    # 8. CLOSED
    job(
        "closed",
        "Manual QA Tester",
        "Delta Forge Software",
        "Remote - Latin America",
        ["Python", "PostgreSQL", "Docker", "AWS", "Jira", "Cypress", "Git"],
        10,
        target=WorkflowStatus.CLOSED,
    ),
    # 9-11. Persisted but never displayed (eligibility filter).
    job(
        "not_eligible",
        "Automation Engineer",
        "Starline Robotics",
        "Remote - US",
        ["Python", "PostgreSQL"],
        5,
        target=WorkflowStatus.FOUND,
    ),
    job(
        "review_required",
        "QA Engineer",
        "Global Hires Inc",
        "Remote - Worldwide",
        ["Python", "PostgreSQL"],
        5,
        target=WorkflowStatus.FOUND,
    ),
    # UNKNOWN: no work mode in the location or description (no mode words).
    job(
        "unknown",
        "Software QA Analyst",
        "Costa Verde Software",
        "Bogota, Colombia",
        ["Manual testing", "Test cases"],
        3,
        target=WorkflowStatus.FOUND,
    ),
]

EXPECTED_ELIGIBILITY = {
    "found": EligibilityStatus.ELIGIBLE,
    "reviewed": EligibilityStatus.ELIGIBLE,
    "saved": EligibilityStatus.ELIGIBLE,
    "applied": EligibilityStatus.ELIGIBLE,
    "interview": EligibilityStatus.ELIGIBLE,
    "offer": EligibilityStatus.ELIGIBLE,
    "rejected": EligibilityStatus.ELIGIBLE,
    "closed": EligibilityStatus.ELIGIBLE,
    "not_eligible": EligibilityStatus.NOT_ELIGIBLE,
    "review_required": EligibilityStatus.REVIEW_REQUIRED,
    "unknown": EligibilityStatus.UNKNOWN,
}


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    path = Path(sys.argv[1])
    if path.name == "jobsearch.db" or "data/" in str(path):
        print("REFUSED: pass a temporary path, never the production database.")
        return 2

    store = JobStore(path)
    rows = []
    for index, spec in enumerate(DEMO_JOBS, start=1):
        email = EmailMessage(
            message_id=f"smoke-{index}",
            sender="demo-alerts@example.com",
            received_at=NOW,
            subject=spec["subject"],
            body=spec["body"],
            thread_id=f"smoke-thread-{index}",
        )
        result = process_job_email(email, FakeExtractor(spec["payload"]), TEST_PROFILE)
        if result.outcome is not EmailProcessingOutcome.JOB_ANALYZED:
            print(f"FAILED pipeline for {spec['key']}: {result.outcome} {result.error}")
            return 1
        record = store.save(result, now=NOW)
        for step in STATUS_CHAINS[spec["target"]]:
            store.set_status(record.job_id, step)
        analysis = store.get_analysis(record.job_id)
        assert analysis is not None
        eligibility = analysis.eligibility.status
        if eligibility is not EXPECTED_ELIGIBILITY[spec["key"]]:
            print(
                f"FAILED eligibility for {spec['key']}: "
                f"{eligibility.value} != {EXPECTED_ELIGIBILITY[spec['key']].value}"
            )
            return 1
        rows.append(
            (
                record.job_id,
                spec["target"],
                eligibility,
                analysis.professional_match.score,
                result.job_posting.company if result.job_posting else "?",
            )
        )

    print(f"Seeded {len(rows)} jobs into {path}")
    print(f"{'ID':>4}  {'STATUS':<10} {'ELIGIBILITY':<17} {'MATCH':>5}  COMPANY")
    for job_id, status, elig, score, company in rows:
        print(
            f"{job_id:>4}  {status.value:<10} {elig.value:<17} "
            f"{round(score * 100):>4}%  {company}"
        )

    shown = list_eligible_jobs(store)
    print(f"\nDashboard will show {len(shown)} of {len(rows)} jobs (ELIGIBLE only).")
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
