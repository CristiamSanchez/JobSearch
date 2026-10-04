"""Phase 5 tests: manual workflow status (rules, storage, migration).

All databases are in-memory or temporary (`tmp_path`): no Gmail, no OAuth,
no network, no AI API key. Jobs come from the real Phase 4B pipeline run
with deterministic fake extractors, so these tests prove the workflow
layer only *stores and advances* what earlier phases produced.

Key guarantees covered here:

* every newly analyzed job starts at ``FOUND``, and eligibility or
  professional match never set or advance the status;
* databases created before Phase 5 gain the ``status`` column in place
  (rows kept, defaulting to ``FOUND``) — the database is never recreated;
* exactly the transitions from the Phase 5 specification are allowed;
  terminal states and unknown values are rejected with the stored status
  left untouched;
* Phase 4C deduplication and email preservation keep working unchanged.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from jobsearch.email_pipeline import process_job_email
from jobsearch.models import (
    CandidateProfile,
    EmailMessage,
    EmailProcessingOutcome,
    EligibilityStatus,
)
from jobsearch.persistence import JobStore, SavedRecord
from jobsearch.workflow import (
    ALLOWED_TRANSITIONS,
    WorkflowStatus,
    can_transition,
    next_statuses,
)

NOW = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)

JOB_SUBJECT = "New job alert: Backend Engineer"
RICH_BODY = (
    "Backend Engineer at ACME.\n\n"
    "Requirements: 5 years of experience with Python and PostgreSQL, "
    "Bachelor's degree or equivalent.\n"
    "Salary: USD 90000 per year. Location: Remote - LATAM.\n"
    "Candidates must be authorized to work in Honduras; no US work "
    "authorization is required for this role.\n"
    "Apply at https://careers.example.com/jobs/42"
)

VALID_EXTRACTION = {
    "position": "Backend Engineer",
    "company": "ACME",
    "location": "Remote - LATAM",
    "required_skills": ["Python", "PostgreSQL"],
    "preferred_skills": ["Kubernetes"],
    "years_of_experience": 5,
    "technologies": ["AWS"],
    "remote_mode": "remote",
    "salary": "90000",
    "salary_currency": "USD",
    "salary_period": "year",
}

APPLICATION_URL = "https://boards.example.com/jobs/12345"

PROFILE = CandidateProfile(
    country="Honduras",
    region="LATAM",
    skills=("Python", "PostgreSQL", "Kubernetes"),
    years_experience=6,
    technologies=("Python", "AWS"),
    education=("Bachelor's in Computer Science",),
    languages=("Spanish", "English"),
)

#: Exactly the transitions the Phase 5 specification allows.
EXPECTED_TRANSITIONS = {
    (WorkflowStatus.FOUND, WorkflowStatus.REVIEWED),
    (WorkflowStatus.REVIEWED, WorkflowStatus.SAVED),
    (WorkflowStatus.REVIEWED, WorkflowStatus.CLOSED),
    (WorkflowStatus.SAVED, WorkflowStatus.APPLIED),
    (WorkflowStatus.SAVED, WorkflowStatus.CLOSED),
    (WorkflowStatus.APPLIED, WorkflowStatus.INTERVIEW),
    (WorkflowStatus.APPLIED, WorkflowStatus.REJECTED),
    (WorkflowStatus.APPLIED, WorkflowStatus.CLOSED),
    (WorkflowStatus.INTERVIEW, WorkflowStatus.OFFER),
    (WorkflowStatus.INTERVIEW, WorkflowStatus.REJECTED),
    (WorkflowStatus.INTERVIEW, WorkflowStatus.CLOSED),
    (WorkflowStatus.OFFER, WorkflowStatus.CLOSED),
}

#: Terminal-state starts are reached through the allowed chain first.
TERMINAL_CASES = [
    (WorkflowStatus.REVIEWED, WorkflowStatus.CLOSED),
    (WorkflowStatus.SAVED, WorkflowStatus.CLOSED),
    (WorkflowStatus.APPLIED, WorkflowStatus.REJECTED),
    (WorkflowStatus.APPLIED, WorkflowStatus.CLOSED),
    (WorkflowStatus.INTERVIEW, WorkflowStatus.REJECTED),
    (WorkflowStatus.INTERVIEW, WorkflowStatus.CLOSED),
    (WorkflowStatus.OFFER, WorkflowStatus.CLOSED),
]

INVALID_CASES = [
    (WorkflowStatus.FOUND, WorkflowStatus.SAVED),
    (WorkflowStatus.FOUND, WorkflowStatus.APPLIED),
    (WorkflowStatus.FOUND, WorkflowStatus.INTERVIEW),
    (WorkflowStatus.FOUND, WorkflowStatus.OFFER),
    (WorkflowStatus.FOUND, WorkflowStatus.REJECTED),
    (WorkflowStatus.FOUND, WorkflowStatus.CLOSED),
    (WorkflowStatus.REVIEWED, WorkflowStatus.FOUND),
    (WorkflowStatus.REVIEWED, WorkflowStatus.APPLIED),
    (WorkflowStatus.REVIEWED, WorkflowStatus.OFFER),
    (WorkflowStatus.SAVED, WorkflowStatus.REVIEWED),
    (WorkflowStatus.SAVED, WorkflowStatus.INTERVIEW),
    (WorkflowStatus.APPLIED, WorkflowStatus.SAVED),
    (WorkflowStatus.INTERVIEW, WorkflowStatus.APPLIED),
    (WorkflowStatus.OFFER, WorkflowStatus.REJECTED),
    (WorkflowStatus.OFFER, WorkflowStatus.REVIEWED),
    (WorkflowStatus.REJECTED, WorkflowStatus.REVIEWED),
    (WorkflowStatus.REJECTED, WorkflowStatus.FOUND),
    (WorkflowStatus.CLOSED, WorkflowStatus.FOUND),
    (WorkflowStatus.CLOSED, WorkflowStatus.SAVED),
    (WorkflowStatus.SAVED, WorkflowStatus.SAVED),  # no-op is not allowed
]


class FakeExtractor:
    """Deterministic JobDescriptionExtractor (same shape as earlier phases)."""

    def __init__(self, payload: object):
        self.payload = payload

    def extract(self, description: str) -> dict[str, object]:
        return self.payload  # type: ignore[return-value]


def make_email(message_id: str = "m-1") -> EmailMessage:
    return EmailMessage(
        message_id=message_id,
        sender="someone@example.com",
        received_at=NOW,
        subject=JOB_SUBJECT,
        body=RICH_BODY,
        thread_id=f"thr-{message_id}",
    )


def process(email: EmailMessage, **payload_overrides: object):
    """Run the real Phase 4B pipeline with a deterministic fake extractor."""
    payload = dict(VALID_EXTRACTION)
    payload.update(payload_overrides)
    return process_job_email(email, FakeExtractor(payload), PROFILE)


def save_job(
    store: JobStore, *, message_id: str = "m-1", **payload_overrides: object
) -> SavedRecord:
    result = process(make_email(message_id), **payload_overrides)
    assert result.outcome is EmailProcessingOutcome.JOB_ANALYZED, result.error
    return store.save(result, now=NOW)


def reach(store: JobStore, job_id: int, target: WorkflowStatus) -> None:
    """Walk the allowed chain until the job sits in ``target``."""
    path: dict[WorkflowStatus, list[WorkflowStatus]] = {
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
        WorkflowStatus.CLOSED: [WorkflowStatus.REVIEWED, WorkflowStatus.CLOSED],
    }
    for status in path[target]:
        store.set_status(job_id, status)


def make_store() -> JobStore:
    return JobStore(":memory:")


# --- Workflow rules -----------------------------------------------------------


def test_workflow_status_values_match_phase5_specification():
    assert [status.value for status in WorkflowStatus] == [
        "FOUND",
        "REVIEWED",
        "SAVED",
        "APPLIED",
        "INTERVIEW",
        "OFFER",
        "REJECTED",
        "CLOSED",
    ]


def test_allowed_transitions_match_phase5_specification():
    actual = {
        (current, new)
        for current, targets in ALLOWED_TRANSITIONS.items()
        for new in targets
    }
    assert actual == EXPECTED_TRANSITIONS


def test_terminal_states_allow_no_transitions():
    for terminal in (WorkflowStatus.REJECTED, WorkflowStatus.CLOSED):
        assert next_statuses(terminal) == ()
        for target in WorkflowStatus:
            assert can_transition(terminal, target) is False


def test_can_transition_accepts_strings_and_rejects_unknown_values():
    assert can_transition("FOUND", "REVIEWED") is True
    assert can_transition(WorkflowStatus.FOUND, WorkflowStatus.REVIEWED) is True
    assert can_transition("not-a-status", "FOUND") is False
    assert can_transition(WorkflowStatus.FOUND, "not-a-status") is False


def test_next_statuses_return_canonical_deterministic_order():
    assert next_statuses(WorkflowStatus.REVIEWED) == (
        WorkflowStatus.SAVED,
        WorkflowStatus.CLOSED,
    )
    assert next_statuses(WorkflowStatus.APPLIED) == (
        WorkflowStatus.INTERVIEW,
        WorkflowStatus.REJECTED,
        WorkflowStatus.CLOSED,
    )
    assert next_statuses("FOUND") == (WorkflowStatus.REVIEWED,)


# --- Defaults and migration ---------------------------------------------------


def test_new_job_analysis_defaults_to_found():
    store = make_store()
    record = save_job(store)
    assert record.outcome is EmailProcessingOutcome.JOB_ANALYZED
    assert store.get_status(record.job_id) == WorkflowStatus.FOUND


def test_eligibility_and_match_never_set_the_workflow_status():
    """An ELIGIBLE job with a perfect score still starts at FOUND."""
    store = make_store()
    record = save_job(store)
    analysis = store.get_analysis(record.job_id)
    assert analysis is not None
    assert analysis.eligibility.status is EligibilityStatus.ELIGIBLE
    assert analysis.professional_match.score == 1.0
    assert store.get_status(record.job_id) == WorkflowStatus.FOUND


def test_existing_database_gains_status_column_with_found(tmp_path):
    path = tmp_path / "legacy.db"
    store = JobStore(path)
    record = save_job(store, message_id="legacy-1")
    title_before = store.get_job(record.job_id).title
    store.close()

    # Simulate a Phase 4C database created before Phase 5: no status column.
    connection = sqlite3.connect(path)
    connection.execute("ALTER TABLE jobs DROP COLUMN status")
    connection.commit()
    columns = {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
    assert "status" not in columns
    connection.close()

    # Opening the store migrates in place: rows kept, defaulting to FOUND.
    store = JobStore(path)
    assert store.get_status(record.job_id) == WorkflowStatus.FOUND
    assert store.get_job(record.job_id).title == title_before
    store.close()

    # The migration is committed — a fresh connection still sees the column.
    store = JobStore(path)
    assert store.get_status(record.job_id) == WorkflowStatus.FOUND
    assert len(store.list_job_ids()) == 1
    assert store.get_email("legacy-1") is not None
    store.close()


def test_status_change_persists_across_reopen(tmp_path):
    path = tmp_path / "status.db"
    store = JobStore(path)
    record = save_job(store)
    assert (
        store.set_status(record.job_id, WorkflowStatus.REVIEWED)
        == WorkflowStatus.REVIEWED
    )
    store.close()

    reopened = JobStore(path)
    assert reopened.get_status(record.job_id) == WorkflowStatus.REVIEWED
    assert reopened.get_job(record.job_id) is not None
    assert reopened.get_analysis(record.job_id) is not None
    reopened.close()


# --- Transitions --------------------------------------------------------------


def test_normal_workflow_chain_advances_step_by_step():
    store = make_store()
    record = save_job(store)
    job_id = record.job_id

    chain = [
        WorkflowStatus.REVIEWED,
        WorkflowStatus.SAVED,
        WorkflowStatus.APPLIED,
        WorkflowStatus.INTERVIEW,
        WorkflowStatus.OFFER,
    ]
    for expected in chain:
        assert store.set_status(job_id, expected) == expected
        assert store.get_status(job_id) == expected


@pytest.mark.parametrize(("start", "target"), TERMINAL_CASES)
def test_terminal_transitions_are_allowed(
    start: WorkflowStatus, target: WorkflowStatus
):
    store = make_store()
    record = save_job(store)
    reach(store, record.job_id, start)
    assert store.set_status(record.job_id, target) == target
    assert store.get_status(record.job_id) == target


@pytest.mark.parametrize(("start", "target"), INVALID_CASES)
def test_invalid_transitions_are_rejected(
    start: WorkflowStatus, target: WorkflowStatus
):
    store = make_store()
    record = save_job(store)
    reach(store, record.job_id, start)
    with pytest.raises(ValueError, match="not allowed"):
        store.set_status(record.job_id, target)
    # The rejection preserves the stored status.
    assert store.get_status(record.job_id) == start


def test_set_status_rejects_unknown_job_and_unknown_value():
    store = make_store()
    record = save_job(store)
    with pytest.raises(LookupError):
        store.set_status(999, WorkflowStatus.REVIEWED)
    with pytest.raises(ValueError, match="unknown workflow status"):
        store.set_status(record.job_id, "NOT_A_STATUS")
    assert store.get_status(record.job_id) == WorkflowStatus.FOUND


# --- Phase 4C regression guard -----------------------------------------------


def test_phase4c_deduplication_still_works_with_workflow_status():
    store = make_store()
    first = save_job(store, message_id="a", application_url=APPLICATION_URL)
    second = save_job(
        store,
        message_id="b",
        application_url=APPLICATION_URL + "?utm_source=alerts",
    )
    # One job (canonical URL dedup), two preserved emails, both FOUND.
    assert first.job_id == second.job_id
    assert len(store.list_job_ids()) == 1
    assert store.get_status(first.job_id) == WorkflowStatus.FOUND
    assert store.get_email("a") is not None
    assert store.get_email("b") is not None
