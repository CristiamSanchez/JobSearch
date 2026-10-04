"""Manual workflow status for opportunities (Phase 5).

Workflow status answers *"what state is this opportunity in my job
search?"* — deliberately separate from:

* **eligibility** — *can this candidate apply at all?* (Phase 3a), and
* **professional match** — *how well does the profile fit?* (Phase 3b).

Nothing in the pipeline sets a workflow status beyond the initial
``FOUND`` that persistence assigns to every analyzed job. Advancing the
status is always a manual decision made by the user in the dashboard;
eligibility and match scores never drive it automatically.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "ALLOWED_TRANSITIONS",
    "WORKFLOW_ORDER",
    "WorkflowStatus",
    "can_transition",
    "next_statuses",
]


class WorkflowStatus(StrEnum):
    """The Phase 5 job-search workflow states."""

    FOUND = "FOUND"  # discovered + analyzed; not yet manually reviewed
    REVIEWED = "REVIEWED"  # manually reviewed; no apply decision yet
    SAVED = "SAVED"  # worth keeping as an application candidate
    APPLIED = "APPLIED"  # the application was actually submitted
    INTERVIEW = "INTERVIEW"  # company invited the candidate
    OFFER = "OFFER"  # offer received
    REJECTED = "REJECTED"  # rejected — terminal for this MVP
    CLOSED = "CLOSED"  # inactive for any other reason — terminal


#: Canonical order used for display and deterministic option lists.
WORKFLOW_ORDER: tuple[WorkflowStatus, ...] = (
    WorkflowStatus.FOUND,
    WorkflowStatus.REVIEWED,
    WorkflowStatus.SAVED,
    WorkflowStatus.APPLIED,
    WorkflowStatus.INTERVIEW,
    WorkflowStatus.OFFER,
    WorkflowStatus.REJECTED,
    WorkflowStatus.CLOSED,
)

#: Exactly the transitions the Phase 5 MVP allows. Everything else —
#: including skipping steps, reopening terminal states, and going
#: backwards — is invalid.
ALLOWED_TRANSITIONS: dict[WorkflowStatus, frozenset[WorkflowStatus]] = {
    WorkflowStatus.FOUND: frozenset({WorkflowStatus.REVIEWED}),
    WorkflowStatus.REVIEWED: frozenset(
        {WorkflowStatus.SAVED, WorkflowStatus.CLOSED}
    ),
    WorkflowStatus.SAVED: frozenset(
        {WorkflowStatus.APPLIED, WorkflowStatus.CLOSED}
    ),
    WorkflowStatus.APPLIED: frozenset(
        {WorkflowStatus.INTERVIEW, WorkflowStatus.REJECTED, WorkflowStatus.CLOSED}
    ),
    WorkflowStatus.INTERVIEW: frozenset(
        {WorkflowStatus.OFFER, WorkflowStatus.REJECTED, WorkflowStatus.CLOSED}
    ),
    WorkflowStatus.OFFER: frozenset({WorkflowStatus.CLOSED}),
    WorkflowStatus.REJECTED: frozenset(),
    WorkflowStatus.CLOSED: frozenset(),
}


def can_transition(current: WorkflowStatus | str, new: WorkflowStatus | str) -> bool:
    """Return whether ``current → new`` is an allowed Phase 5 transition.

    Unknown status values are never allowed. Deterministic: a pure lookup
    in :data:`ALLOWED_TRANSITIONS`, with no scoring or inference.
    """
    try:
        current_status = WorkflowStatus(current)
        new_status = WorkflowStatus(new)
    except ValueError:
        return False
    return new_status in ALLOWED_TRANSITIONS[current_status]


def next_statuses(current: WorkflowStatus | str) -> tuple[WorkflowStatus, ...]:
    """Allowed next statuses for the UI, in canonical order.

    Terminal states (``REJECTED``, ``CLOSED``) return an empty tuple.
    """
    status = WorkflowStatus(current)
    allowed = ALLOWED_TRANSITIONS[status]
    return tuple(item for item in WORKFLOW_ORDER if item in allowed)
