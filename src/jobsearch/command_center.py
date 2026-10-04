"""Read model for the MVP Command Center (Phase 5).

Turns what is already persisted into dashboard rows:

* **eligibility filter** — only ``EligibilityStatus.ELIGIBLE`` jobs are
  ever returned; the other statuses stay untouched in SQLite and are
  simply not displayed (presentation filtering, never deletion);
* **professional match** — the existing ``ProfessionalMatch.score``
  rendered as a display percentage (0.87 → 87), informational only: never
  used to rank, filter, or exclude anything;
* **workflow status** — the manual Phase 5 status stored alongside the
  job (``FOUND`` by default), never derived from eligibility or score.

Rows come back in job-id (insertion) order — there is deliberately no
"best jobs" ordering and no new scoring of any kind.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import EligibilityStatus
from .persistence import JobStore
from .workflow import WorkflowStatus

__all__ = [
    "DashboardJob",
    "get_job_detail",
    "list_eligible_jobs",
    "match_percent",
]


def match_percent(score: float) -> int:
    """Format the existing match score as a display percentage.

    ``0.87 → 87``. This is presentation of the Phase 3b score — it never
    recomputes or re-weights anything.
    """
    return round(float(score) * 100)


@dataclass(frozen=True)
class DashboardJob:
    """One dashboard row, and the job-detail view built from the same data.

    Holds presentation state only: stored analysis output plus the manual
    workflow status. It carries no score of its own beyond formatting the
    existing ``ProfessionalMatch.score``, and eligibility remains the
    categorical status produced by Phase 3a.
    """

    job_id: int
    title: str
    company: str
    location: str
    remote_mode: str
    score: float
    match_percent: int
    status: WorkflowStatus
    eligibility: EligibilityStatus
    eligibility_reasons: tuple[str, ...]
    matched_skills: tuple[str, ...]
    missing_required_skills: tuple[str, ...]
    application_url: str
    first_seen_at: str
    last_seen_at: str


def _build_job(
    store: JobStore,
    job_id: int,
    status_filter: WorkflowStatus | None,
) -> DashboardJob | None:
    """Build one row, or ``None`` when it must not be displayed.

    A job is hidden when it is missing anything, when its stored
    eligibility is not ``ELIGIBLE``, or when it does not match the
    requested workflow-status filter.
    """
    posting = store.get_job(job_id)
    analysis = store.get_analysis(job_id)
    status = store.get_status(job_id)
    seen = store.get_seen(job_id)
    if posting is None or analysis is None or status is None or seen is None:
        return None
    if analysis.eligibility.status is not EligibilityStatus.ELIGIBLE:
        return None
    if status_filter is not None and status is not status_filter:
        return None

    match = analysis.professional_match
    return DashboardJob(
        job_id=job_id,
        title=posting.title,
        company=posting.company,
        location=posting.location,
        remote_mode=posting.remote_mode,
        score=match.score,
        match_percent=match_percent(match.score),
        status=status,
        eligibility=analysis.eligibility.status,
        eligibility_reasons=analysis.eligibility.reasons,
        matched_skills=match.matched_skills,
        missing_required_skills=match.missing_required_skills,
        application_url=posting.url,
        first_seen_at=seen[0],
        last_seen_at=seen[1],
    )


def list_eligible_jobs(
    store: JobStore, status: WorkflowStatus | str | None = None
) -> tuple[DashboardJob, ...]:
    """Eligible jobs, optionally narrowed to one workflow status.

    ``status=None`` ("All") still applies the primary eligibility rule —
    only ``ELIGIBLE`` jobs are returned. An unknown status string raises
    ``ValueError``. Never sorted by score.
    """
    status_filter = WorkflowStatus(status) if status is not None else None
    rows: list[DashboardJob] = []
    for job_id in store.list_job_ids():
        job = _build_job(store, job_id, status_filter)
        if job is not None:
            rows.append(job)
    return tuple(rows)


def get_job_detail(store: JobStore, job_id: int) -> DashboardJob | None:
    """The job-detail view for one job, or ``None`` if not displayed.

    Ineligible jobs return ``None``: the eligibility filter applies to the
    detail view as well, while the records themselves remain in SQLite.
    """
    return _build_job(store, job_id, None)
