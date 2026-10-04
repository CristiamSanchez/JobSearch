"""Phase 5 tests: the MVP Command Center read model and dashboard.

All databases are in-memory or temporary (`tmp_path`): no Gmail, no OAuth,
no network, no AI API key. The HTTP test talks only to a loopback server
bound to an ephemeral port. Jobs come from the real Phase 4B pipeline with
deterministic fake extractors.

Key guarantees covered here:

* the primary dashboard shows **only** ``ELIGIBLE`` jobs — the other
  eligibility statuses are excluded from the view but never deleted;
* the professional match score is displayed as a percentage of the
  existing Phase 3b score and never used to rank or filter;
* workflow status changes are manual, offered only for allowed
  transitions, persisted to SQLite, and preserved across refreshes — an
  application URL never implies an application;
* extracted content is HTML-escaped before rendering.
"""

from __future__ import annotations

import http.client
import threading
from datetime import datetime, timezone

import pytest

from jobsearch.command_center import (
    get_job_detail,
    list_eligible_jobs,
    match_percent,
)
from jobsearch.dashboard import (
    DashboardHTTPServer,
    render_detail,
    render_index,
)
from jobsearch.email_pipeline import process_job_email
from jobsearch.models import (
    CandidateProfile,
    EmailMessage,
    EmailProcessingOutcome,
    EligibilityStatus,
)
from jobsearch.persistence import JobStore, SavedRecord
from jobsearch.workflow import WorkflowStatus

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)

PROFILE = CandidateProfile(
    country="Honduras",
    region="LATAM",
    skills=("Python", "PostgreSQL", "Kubernetes"),
    years_experience=6,
    technologies=("Python", "AWS"),
    education=("Bachelor's in Computer Science",),
    languages=("Spanish", "English"),
)

#: One job per eligibility status — all four are valid pipeline outputs.
ELIGIBLE_JOB = {
    "subject": "Job opening: QA Automation Engineer",
    "body": (
        "We are hiring a QA Automation Engineer. Location: Remote - LATAM. "
        "Requirements: 3 years of experience with Python. "
        "Salary: 85000 per year. Apply now."
    ),
    "payload": {
        "position": "QA Automation Engineer",
        "company": "EligibleCo",
        "location": "Remote - LATAM",
        "required_skills": ["Python", "PostgreSQL"],
        "remote_mode": "remote",
        "application_url": "https://boards.example.com/jobs/101",
    },
}

BLOCKED_JOB = {
    "subject": "Job opening: QA Engineer",
    "body": (
        "We are hiring a QA Engineer. Location: Remote - US. "
        "Requirements: 4 years of experience with Python. "
        "Salary: 90000 per year. Apply now."
    ),
    "payload": {
        "position": "QA Engineer",
        "company": "BlockedCo",
        "location": "Remote - US",
        "required_skills": ["Python"],
        "remote_mode": "remote",
        "application_url": "https://boards.example.com/jobs/102",
    },
}

REVIEW_JOB = {
    "subject": "Job opening: Senior QA Engineer",
    "body": (
        "We are hiring a Senior QA Engineer. Location: Remote - Worldwide. "
        "Requirements: 4 years of experience with Python and Docker. "
        "Salary: 85000 per year. Apply now."
    ),
    "payload": {
        "position": "Senior QA Engineer",
        "company": "ReviewCo",
        "location": "Remote - Worldwide",
        "required_skills": ["Python"],
        "remote_mode": "remote",
        "application_url": "https://boards.example.com/jobs/103",
    },
}

UNKNOWN_JOB = {
    "subject": "Job opening: QA Analyst",
    "body": (
        "We are hiring a QA Analyst. Location: Madrid, Spain. "
        "Requirements: 3 years of experience. "
        "Salary: 60000 per year. Apply today."
    ),
    "payload": {
        "position": "QA Analyst",
        "company": "UnknownCo",
        "location": "Madrid, Spain",
        "required_skills": ["Testing"],
        "application_url": "https://boards.example.com/jobs/104",
    },
}

#: Eligible but a deliberately poor professional fit — low score, saved first.
WEAK_MATCH_JOB = {
    "subject": "Job opening: Mainframe Tester",
    "body": (
        "We are hiring a Mainframe Tester. Location: Remote - LATAM. "
        "Requirements: 8 years of experience with COBOL. "
        "Salary: 70000 per year. Apply now."
    ),
    "payload": {
        "position": "Mainframe Tester",
        "company": "LegacyCo",
        "location": "Remote - LATAM",
        "required_skills": ["COBOL", "RPG"],
        "remote_mode": "remote",
        "application_url": "https://boards.example.com/jobs/105",
    },
}

#: Hostile content arriving through AI extraction must be escaped on render.
EVIL_JOB = {
    "subject": "Job opening: QA Engineer",
    "body": (
        "We are hiring a QA Engineer. Location: Remote - LATAM. "
        "Requirements: 3 years of experience with Python. "
        "Salary: 80000 per year. Apply now."
    ),
    "payload": {
        "position": "QA Engineer",
        "company": "<script>alert(1)</script>",
        "location": "Remote - LATAM",
        "required_skills": ["Python"],
        "remote_mode": "remote",
        "application_url": "https://boards.example.com/jobs/106",
    },
}


class FakeExtractor:
    """Deterministic JobDescriptionExtractor (same shape as earlier phases)."""

    def __init__(self, payload: object):
        self.payload = payload

    def extract(self, description: str) -> dict[str, object]:
        return self.payload  # type: ignore[return-value]


def save(store: JobStore, spec: dict, message_id: str) -> SavedRecord:
    """Run the real Phase 4B pipeline and persist the result."""
    email = EmailMessage(
        message_id=message_id,
        sender="recruiter@example.com",
        received_at=NOW,
        subject=spec["subject"],
        body=spec["body"],
        thread_id=f"thr-{message_id}",
    )
    result = process_job_email(email, FakeExtractor(dict(spec["payload"])), PROFILE)
    assert result.outcome is EmailProcessingOutcome.JOB_ANALYZED, result.error
    return store.save(result, now=NOW)


@pytest.fixture()
def store(tmp_path):
    database = JobStore(tmp_path / "dashboard.db")
    yield database
    database.close()


@pytest.fixture()
def job_ids(store):
    """One job per eligibility status, plus their job ids."""
    return {
        "eligible": save(store, ELIGIBLE_JOB, "m-eligible").job_id,
        "blocked": save(store, BLOCKED_JOB, "m-blocked").job_id,
        "review": save(store, REVIEW_JOB, "m-review").job_id,
        "unknown": save(store, UNKNOWN_JOB, "m-unknown").job_id,
    }


# --- Eligibility filtering ----------------------------------------------------


def test_dashboard_returns_only_eligible_jobs(store, job_ids):
    rows = list_eligible_jobs(store)
    assert [row.job_id for row in rows] == [job_ids["eligible"]]
    assert rows[0].eligibility is EligibilityStatus.ELIGIBLE


def test_not_eligible_jobs_are_excluded_but_kept_in_sqlite(store, job_ids):
    rows = list_eligible_jobs(store)
    assert all(row.company != "BlockedCo" for row in rows)
    # Filtering is presentation only — the record remains persisted.
    assert store.get_job(job_ids["blocked"]) is not None
    analysis = store.get_analysis(job_ids["blocked"])
    assert analysis is not None
    assert analysis.eligibility.status is EligibilityStatus.NOT_ELIGIBLE


def test_review_required_jobs_are_excluded_but_kept_in_sqlite(store, job_ids):
    rows = list_eligible_jobs(store)
    assert all(row.company != "ReviewCo" for row in rows)
    assert store.get_job(job_ids["review"]) is not None
    analysis = store.get_analysis(job_ids["review"])
    assert analysis is not None
    assert analysis.eligibility.status is EligibilityStatus.REVIEW_REQUIRED


def test_unknown_jobs_are_excluded_but_kept_in_sqlite(store, job_ids):
    rows = list_eligible_jobs(store)
    assert all(row.company != "UnknownCo" for row in rows)
    assert store.get_job(job_ids["unknown"]) is not None
    analysis = store.get_analysis(job_ids["unknown"])
    assert analysis is not None
    assert analysis.eligibility.status is EligibilityStatus.UNKNOWN


def test_all_filter_still_respects_the_eligibility_rule(store, job_ids):
    for status_filter in (None, "FOUND"):
        rows = list_eligible_jobs(store, status_filter)
        assert [row.job_id for row in rows] == [job_ids["eligible"]]


def test_status_filter_narrows_visible_jobs(store, job_ids):
    assert [
        row.job_id for row in list_eligible_jobs(store, "FOUND")
    ] == [job_ids["eligible"]]

    store.set_status(job_ids["eligible"], WorkflowStatus.REVIEWED)
    assert list_eligible_jobs(store, "FOUND") == ()
    assert [
        row.job_id for row in list_eligible_jobs(store, "REVIEWED")
    ] == [job_ids["eligible"]]
    # "All" still shows the job — and still only eligible ones.
    assert len(list_eligible_jobs(store)) == 1

    with pytest.raises(ValueError):
        list_eligible_jobs(store, "NOT_A_STATUS")


# --- Professional match as a percentage ---------------------------------------


def test_match_score_is_formatted_as_a_percentage():
    assert match_percent(0.87) == 87
    assert match_percent(0.81) == 81
    assert match_percent(0.69) == 69
    assert match_percent(0.0) == 0
    assert match_percent(1.0) == 100


def test_rendered_percentage_matches_the_stored_score(store, job_ids):
    row = list_eligible_jobs(store)[0]
    assert row.match_percent == round(row.score * 100)
    page = render_index(list_eligible_jobs(store))
    assert f'<td class="num">{row.match_percent}%</td>' in page


def test_jobs_are_never_ranked_by_score(store):
    weak = save(store, WEAK_MATCH_JOB, "m-weak")  # saved first, lower score
    strong = save(store, ELIGIBLE_JOB, "m-strong")  # saved second, higher score
    weak_score = store.get_analysis(weak.job_id).professional_match.score
    strong_score = store.get_analysis(strong.job_id).professional_match.score
    assert weak_score < strong_score  # ranking by score would reorder these…

    rows = list_eligible_jobs(store)
    assert [row.job_id for row in rows] == [weak.job_id, strong.job_id]  # …it doesn't


# --- Application URL ----------------------------------------------------------


def test_application_url_does_not_change_status(store, job_ids):
    row = get_job_detail(store, job_ids["eligible"])
    assert row is not None
    assert row.application_url == ELIGIBLE_JOB["payload"]["application_url"]
    # An application destination existing is not proof of an application.
    assert row.status is WorkflowStatus.FOUND
    assert store.get_status(job_ids["eligible"]) == WorkflowStatus.FOUND


# --- Job detail view ----------------------------------------------------------


def test_detail_view_contains_the_required_fields(store, job_ids):
    detail = get_job_detail(store, job_ids["eligible"])
    assert detail is not None
    page = render_detail(detail)
    for label in (
        "Job ID",
        "Position",
        "Company",
        "Location",
        "Remote mode",
        "Professional Match",
        "Eligibility",
        "Workflow Status",
        "Application URL",
        "First seen",
        "Last seen",
    ):
        assert label in page
    assert f"JOB-{job_ids['eligible']}" in page
    assert "QA Automation Engineer" in page
    assert "EligibleCo" in page
    assert "Remote - LATAM" in page
    assert "ELIGIBLE" in page
    assert "FOUND" in page
    assert detail.application_url in page
    assert detail.first_seen_at in page
    assert detail.last_seen_at in page


def test_detail_view_is_none_for_excluded_jobs(store, job_ids):
    assert get_job_detail(store, job_ids["blocked"]) is None
    assert get_job_detail(store, job_ids["review"]) is None
    assert get_job_detail(store, job_ids["unknown"]) is None
    assert get_job_detail(store, 999) is None


# --- Rendering ----------------------------------------------------------------


def test_rendered_index_hides_non_eligible_companies(store, job_ids):
    page = render_index(list_eligible_jobs(store))
    assert "EligibleCo" in page
    for hidden in ("BlockedCo", "ReviewCo", "UnknownCo"):
        assert hidden not in page


def test_rendered_index_includes_the_status_filter_links(store, job_ids):
    page = render_index(list_eligible_jobs(store))
    for label in (
        "All",
        "Found",
        "Reviewed",
        "Saved",
        "Applied",
        "Interview",
        "Offer",
        "Rejected",
        "Closed",
    ):
        assert f">{label}</a>" in page
    assert "/?status=FOUND" in page
    assert "/?status=APPLIED" in page


def test_rendered_index_escapes_extracted_content(store):
    save(store, EVIL_JOB, "m-evil")
    page = render_index(list_eligible_jobs(store))
    assert "<script>alert" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page


def test_status_form_offers_only_allowed_transitions(store, job_ids):
    page = render_index(list_eligible_jobs(store))
    assert 'option value="REVIEWED"' in page  # FOUND → REVIEWED is allowed
    assert 'option value="APPLIED"' not in page  # FOUND cannot skip to APPLIED
    assert 'option value="CLOSED"' not in page  # FOUND has no terminal jump

    store.set_status(job_ids["eligible"], WorkflowStatus.REVIEWED)
    store.set_status(job_ids["eligible"], WorkflowStatus.CLOSED)
    page = render_index(list_eligible_jobs(store), WorkflowStatus.CLOSED)
    assert "terminal" in page
    assert 'class="status-form"' not in page


def test_empty_dashboard_renders_a_friendly_message():
    assert "No eligible jobs yet." in render_index(())
    filtered = render_index((), WorkflowStatus.FOUND)
    assert "No eligible jobs with status" in filtered


def test_status_change_is_preserved_for_a_dashboard_refresh(tmp_path):
    path = tmp_path / "refresh.db"
    store = JobStore(path)
    record = save(store, ELIGIBLE_JOB, "m-refresh")
    store.set_status(record.job_id, WorkflowStatus.REVIEWED)
    store.set_status(record.job_id, WorkflowStatus.SAVED)
    page = render_index(list_eligible_jobs(store))
    assert "<strong>SAVED</strong>" in page
    store.close()

    reopened = JobStore(path)
    rows = list_eligible_jobs(reopened)
    assert [row.status for row in rows] == [WorkflowStatus.SAVED]
    assert "<strong>SAVED</strong>" in render_index(rows)
    reopened.close()


# --- HTTP end-to-end (loopback only) ------------------------------------------


def test_dashboard_http_end_to_end(tmp_path):
    """Server and store share one thread; a second connection verifies writes.

    The SQLite connection is thread-affine, so the store is created in the
    same thread that serves requests (exactly like ``python -m
    jobsearch.dashboard`` does). Persisted state is then asserted from this
    thread through its own independent connection.
    """
    path = tmp_path / "http.db"
    context: dict = {}
    ready = threading.Event()

    def serve():
        store = JobStore(path)
        record = save(store, ELIGIBLE_JOB, "m-http")
        server = DashboardHTTPServer(("127.0.0.1", 0), store)
        context.update(server=server, job_id=record.job_id)
        ready.set()
        server.serve_forever()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    assert ready.wait(timeout=10)

    server = context["server"]
    job_id = context["job_id"]
    port = server.server_address[1]
    verify = JobStore(path)

    def request(method, path, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        headers = {}
        if body is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        payload = response.read().decode("utf-8")
        result = (response.status, payload, dict(response.getheaders()))
        connection.close()
        return result

    try:
        # The table renders with the job id and default status.
        status, body, _ = request("GET", "/")
        assert status == 200
        assert f"JOB-{job_id}" in body
        assert "<strong>FOUND</strong>" in body

        # A manual status action persists to SQLite (redirect back).
        status, _, headers = request("POST", f"/job/{job_id}/status",
                                     body="status=REVIEWED")
        assert status == 303
        assert headers["Location"] == f"/job/{job_id}"
        assert verify.get_status(job_id) == WorkflowStatus.REVIEWED

        # Refreshing the dashboard preserves the status.
        status, body, _ = request("GET", "/")
        assert status == 200
        assert "<strong>REVIEWED</strong>" in body

        # The status filter applies within the eligible set.
        status, body, _ = request("GET", "/?status=REVIEWED")
        assert status == 200 and f"JOB-{job_id}" in body
        status, body, _ = request("GET", "/?status=FOUND")
        assert status == 200 and "No eligible jobs" in body

        # Invalid transitions and unknown statuses are rejected, unchanged.
        status, body, _ = request("POST", f"/job/{job_id}/status",
                                  body="status=OFFER")
        assert status == 400
        assert verify.get_status(job_id) == WorkflowStatus.REVIEWED
        status, _, _ = request("POST", f"/job/{job_id}/status",
                               body="status=GARBAGE")
        assert status == 400
        status, _, _ = request("POST", f"/job/{job_id}/status",
                               body="other=1")
        assert status == 400

        # Detail view, missing records, and bad filters.
        status, body, _ = request("GET", f"/job/{job_id}")
        assert status == 200 and "REVIEWED" in body
        status, _, _ = request("GET", "/job/999")
        assert status == 404
        status, _, _ = request("POST", "/job/999/status", body="status=REVIEWED")
        assert status == 404
        status, _, _ = request("GET", "/?status=GARBAGE")
        assert status == 400
        status, _, _ = request("GET", "/no-such-page")
        assert status == 404
    finally:
        verify.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
