"""Local MVP dashboard for the Job Search Command Center (Phase 5).

A tiny standard-library HTTP UI over SQLite — chosen because the
repository has no frontend stack and this is a local MVP, not a deployed
product: no framework, no build step, no dependencies, no auth. Run it
with::

    python -m jobsearch.dashboard

Presentation and manual workflow only:

* shows **eligible jobs only** (the other eligibility statuses remain
  persisted in SQLite and are simply filtered out of the view);
* displays the existing professional match score as a percentage —
  informational, never used to rank or exclude;
* lets the user advance the **manual** workflow status
  (``FOUND → REVIEWED → SAVED → APPLIED → INTERVIEW → OFFER``, plus the
  allowed terminal transitions), persisting each change back to SQLite.

Everything dynamic is HTML-escaped: titles, companies, and URLs come from
AI extraction and are treated as untrusted text.
"""

from __future__ import annotations

import html
import re
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit

from .command_center import DashboardJob, get_job_detail, list_eligible_jobs
from .config import load_settings
from .persistence import JobStore
from .workflow import WORKFLOW_ORDER, WorkflowStatus, next_statuses

__all__ = [
    "DashboardHandler",
    "DashboardHTTPServer",
    "main",
    "render_detail",
    "render_error",
    "render_index",
    "run_dashboard",
]

_STATUS_PATH = re.compile(r"/job/(\d+)")
_STATUS_ACTION_PATH = re.compile(r"/job/(\d+)/status")

_STYLE = """
  body { font-family: system-ui, sans-serif; margin: 2rem auto; max-width: 70rem;
         padding: 0 1rem; color: #1a1a2e; }
  h1 { font-size: 1.4rem; }
  h2 { font-size: 1.1rem; }
  a { color: #0b5ed7; }
  table { border-collapse: collapse; width: 100%; margin-top: 1rem; }
  th, td { border: 1px solid #d0d0da; padding: 0.45rem 0.6rem; text-align: left; }
  th { background: #f2f3f7; }
  td.num { text-align: right; white-space: nowrap; }
  nav a { margin-right: 0.8rem; text-decoration: none; }
  nav a.active { font-weight: bold; text-decoration: underline; }
  .muted { color: #666; }
  .status-form select, .status-form button { font: inherit; padding: 0.15rem 0.3rem; }
  dl { display: grid; grid-template-columns: 12rem 1fr; gap: 0.35rem 1rem; }
  dt { font-weight: 600; }
  ul { margin: 0.2rem 0 0.6rem; padding-left: 1.2rem; }
"""


def _esc(value: object) -> str:
    """HTML-escape any dynamic value (quotes included)."""
    return html.escape(str(value), quote=True)


def _application_url_link(url: str) -> str:
    """Render the application URL as a link — only for http(s) targets."""
    if not url:
        return '<span class="muted">—</span>'
    if url.startswith(("http://", "https://")):
        return f'<a href="{_esc(url)}">{_esc(url)}</a>'
    return _esc(url)


def _status_form(job: DashboardJob) -> str:
    """Status actions for one job: only the currently allowed transitions."""
    options = next_statuses(job.status)
    if not options:
        return '<span class="muted">terminal</span>'
    option_html = "".join(
        f'<option value="{item.value}">{item.value}</option>' for item in options
    )
    return (
        f'<form class="status-form" method="post" '
        f'action="/job/{job.job_id}/status">'
        f'<select name="status">{option_html}</select>'
        f'<button type="submit">Set</button>'
        f"</form>"
    )


def _filter_nav(current: WorkflowStatus | None) -> str:
    links = ['<a href="/"{}>All</a>'.format(
        ' class="active"' if current is None else ""
    )]
    for item in WORKFLOW_ORDER:
        active = ' class="active"' if current is item else ""
        links.append(
            f'<a href="/?status={item.value}"{active}>'
            f"{item.value.capitalize()}</a>"
        )
    return "<nav>" + "".join(links) + "</nav>"


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html><html lang=\"en\"><head>"
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{_esc(title)}</title>"
        f"<style>{_STYLE}</style>"
        "</head><body>"
        f"{body}"
        "</body></html>"
    )


def render_index(
    jobs: tuple[DashboardJob, ...], current: WorkflowStatus | None = None
) -> str:
    """The primary table: eligible jobs, ID → Position → Company → … → Status."""
    heading = (
        "<h1>Job Search Command Center</h1>"
        f"{_filter_nav(current)}"
        '<p class="muted">Eligible jobs only · source of truth: SQLite</p>'
    )

    if not jobs:
        empty = (
            "No eligible jobs yet."
            if current is None
            else f'No eligible jobs with status <strong>{current.value}</strong>.'
        )
        return _page("Command Center", heading + f"<p>{empty}</p>")

    rows = []
    for job in jobs:
        rows.append(
            "<tr>"
            f'<td><a href="/job/{job.job_id}">JOB-{job.job_id}</a></td>'
            f'<td><a href="/job/{job.job_id}">{_esc(job.title)}</a></td>'
            f"<td>{_esc(job.company)}</td>"
            f"<td>{_esc(job.location)}</td>"
            f'<td class="num">{job.match_percent}%</td>'
            f"<td><strong>{job.status.value}</strong></td>"
            f"<td>{_status_form(job)}</td>"
            "</tr>"
        )
    table = (
        "<table>"
        "<thead><tr><th>ID</th><th>Position</th><th>Company</th>"
        "<th>Location</th><th>Match</th><th>Status</th><th>Change status</th>"
        "</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody>"
        "</table>"
    )
    return _page("Command Center", heading + table)


def render_detail(job: DashboardJob) -> str:
    """The job-detail view: inspect first, then change the workflow status."""
    if job.remote_mode:
        remote_mode = _esc(job.remote_mode)
    else:
        remote_mode = '<span class="muted">—</span>'

    reasons = (
        "<ul>" + "".join(f"<li>{_esc(r)}</li>" for r in job.eligibility_reasons)
        + "</ul>"
        if job.eligibility_reasons
        else ""
    )
    matched = (
        "<ul>" + "".join(f"<li>{_esc(s)}</li>" for s in job.matched_skills) + "</ul>"
        if job.matched_skills
        else '<p class="muted">None recorded.</p>'
    )
    missing = (
        "<ul>"
        + "".join(f"<li>{_esc(s)}</li>" for s in job.missing_required_skills)
        + "</ul>"
        if job.missing_required_skills
        else '<p class="muted">None.</p>'
    )

    body = (
        '<p><a href="/">&larr; Dashboard</a></p>'
        f"<h1>JOB-{job.job_id} · {_esc(job.title)}</h1>"
        "<dl>"
        f"<dt>Job ID</dt><dd>JOB-{job.job_id}</dd>"
        f"<dt>Position</dt><dd>{_esc(job.title)}</dd>"
        f"<dt>Company</dt><dd>{_esc(job.company)}</dd>"
        f"<dt>Location</dt><dd>{_esc(job.location)}</dd>"
        f"<dt>Remote mode</dt><dd>{remote_mode}</dd>"
        f"<dt>Professional Match</dt>"
        f"<dd>{job.match_percent}% <span class=\"muted\">"
        f"(score {job.score})</span></dd>"
        f"<dt>Eligibility</dt><dd><strong>{job.eligibility.value}</strong>"
        f"{reasons}</dd>"
        f"<dt>Workflow Status</dt><dd><strong>{job.status.value}</strong> "
        f"{_status_form(job)}</dd>"
        f"<dt>Application URL</dt><dd>{_application_url_link(job.application_url)}"
        "</dd>"
        f"<dt>First seen</dt><dd>{_esc(job.first_seen_at)}</dd>"
        f"<dt>Last seen</dt><dd>{_esc(job.last_seen_at)}</dd>"
        "</dl>"
        "<h2>Match evidence</h2>"
        f"<p><strong>Matched skills</strong>{matched}</p>"
        f"<p><strong>Missing required skills</strong>{missing}</p>"
        '<p class="muted">Status changes are manual; eligibility and match '
        "never change them.</p>"
    )
    return _page(f"JOB-{job.job_id}", body)


def render_error(message: str) -> str:
    """A minimal error page."""
    return _page("Error", f"<h1>Error</h1><p>{_esc(message)}</p>")


class DashboardHTTPServer(HTTPServer):
    """HTTP server carrying the open :class:`JobStore` connection.

    Deliberately single-threaded: the store's SQLite connection is
    thread-affine, and a local MVP dashboard has one user — requests are
    handled sequentially in the same thread that created the store, so no
    cross-thread connection use can ever occur.
    """

    def __init__(self, server_address: tuple[str, int], store: JobStore):
        self.store = store
        super().__init__(server_address, DashboardHandler)


class DashboardHandler(BaseHTTPRequestHandler):
    """GET the views, POST the manual status changes, write back to SQLite."""

    protocol_version = "HTTP/1.1"
    server: DashboardHTTPServer

    # -- routing ----------------------------------------------------------

    def do_GET(self) -> None:
        try:
            self._handle_get()
        except ValueError as exc:
            self._send(400, render_error(str(exc)))
        except LookupError:
            self._send(404, render_error("Not found."))

    def do_POST(self) -> None:
        try:
            self._handle_post()
        except ValueError as exc:
            self._send(400, render_error(str(exc)))
        except LookupError:
            self._send(404, render_error("Not found."))

    def _handle_get(self) -> None:
        parts = urlsplit(self.path)
        if parts.path == "/":
            query = parse_qs(parts.query)
            raw_status = query.get("status", [None])[0]
            status = WorkflowStatus(raw_status) if raw_status else None
            jobs = list_eligible_jobs(self.server.store, status)
            self._send(200, render_index(jobs, status))
            return

        match = _STATUS_PATH.fullmatch(parts.path)
        if match:
            job = get_job_detail(self.server.store, int(match.group(1)))
            if job is None:
                raise LookupError(parts.path)
            self._send(200, render_detail(job))
            return

        raise LookupError(parts.path)

    def _handle_post(self) -> None:
        parts = urlsplit(self.path)
        match = _STATUS_ACTION_PATH.fullmatch(parts.path)
        if not match:
            raise LookupError(parts.path)
        job_id = int(match.group(1))

        length = int(self.headers.get("Content-Length") or 0)
        raw_body = self.rfile.read(length) if length > 0 else b""
        fields = parse_qs(raw_body.decode("utf-8", "replace"))
        values = fields.get("status")
        if not values:
            raise ValueError("missing status field")

        # Validates the transition (ValueError → 400) and the job
        # (LookupError → 404), then persists the change to SQLite.
        self.server.store.set_status(job_id, values[0])
        self._redirect(self._redirect_target(job_id))

    # -- responses --------------------------------------------------------

    def _send(self, code: int, page: str) -> None:
        payload = page.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)

    def _redirect(self, location: str) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()

    def _redirect_target(self, job_id: int) -> str:
        """Send the user back where the action came from when sensible."""
        referer_path = urlsplit(self.headers.get("Referer", "")).path
        if referer_path == "/" or _STATUS_PATH.fullmatch(referer_path):
            return referer_path
        return f"/job/{job_id}"


def run_dashboard(store: JobStore, host: str, port: int) -> None:
    """Serve the dashboard until interrupted (Ctrl-C)."""
    server = DashboardHTTPServer((host, port), store)
    bound_host, bound_port = server.server_address[:2]
    print(
        f"JobSearch AI Command Center → http://{bound_host}:{bound_port}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main() -> None:
    """Entry point: ``python -m jobsearch.dashboard``."""
    settings = load_settings()
    store = JobStore(settings.database_path)
    try:
        run_dashboard(store, settings.dashboard_host, settings.dashboard_port)
    finally:
        store.close()


if __name__ == "__main__":
    main()
