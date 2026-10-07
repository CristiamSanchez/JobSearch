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
:root {
  color-scheme: light dark;

  --bg: #f4f6f9; --surface: #fff; --border: #e3e7ee;
  --border-strong: #e3e7ee;
  --text: #182230; --muted: #667085; --accent: #2f5bd8;
  --accent-hover: #2449b8;
  /* Ink on inverted (solid) controls: active chip + primary button. */
  --on-solid: #fff; --on-solid-muted: rgba(255, 255, 255, .78);
  /* Table chrome + elevation. */
  --th-bg: #fafbfc; --row-hover: #fafbfd;
  --shadow: 0 1px 2px rgba(16, 24, 40, .05);
  --badge-ring: inset 0 0 0 1px rgba(16, 24, 40, .06);
  --ok-bg: #e6f6ec;    --ok-ink: #12724a;
  --bad-bg: #fdeceb;   --bad-ink: #b42318;
  --warn-bg: #fdf4e5;  --warn-ink: #8a4b06;
  --info-bg: #eaf0ff;  --info-ink: #2447a8;
  --indigo-bg: #e9ecfd; --indigo-ink: #34409b;
  --violet-bg: #f1edfd; --violet-ink: #5a3fc0;
  --teal-bg: #e4f5f2;  --teal-ink: #0e6b61;
  --neutral-bg: #eef1f5; --neutral-ink: #475467;
}

/* Dark mode: automatic from the OS/browser preference. Same markup, same
   routes, same responsive rules — only the tokens change. */
@media (prefers-color-scheme: dark) {
  :root {
    color-scheme: dark;
    --bg: #0b0f17; --surface: #161c29;
    --border: #333d50; --border-strong: #5b6780;
    --text: #e6ebf3; --muted: #9aa7ba;
    --accent: #7d9bff; --accent-hover: #9db7ff;
    --on-solid: #10151f; --on-solid-muted: rgba(16, 21, 31, .72);
    --th-bg: #1a2130; --row-hover: #1b2331;
    --shadow: none;
    --badge-ring: inset 0 0 0 1px rgba(255, 255, 255, .08);
    --ok-bg: #102820;    --ok-ink: #5fd8a0;
    --bad-bg: #2a1313;   --bad-ink: #ff9b90;
    --warn-bg: #2b1f0d;  --warn-ink: #f3b761;
    --info-bg: #14203f;  --info-ink: #93b4ff;
    --indigo-bg: #1b1f4a; --indigo-ink: #a8b5ff;
    --violet-bg: #241a45; --violet-ink: #c7aaff;
    --teal-bg: #0e2a2b;  --teal-ink: #5cd6cf;
    --neutral-bg: #232b39; --neutral-ink: #adb9cb;
  }
}
*, *::before, *::after { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
       font: 15px/1.55 system-ui, -apple-system, "Segoe UI", Roboto,
             Helvetica, Arial, sans-serif;
       -webkit-text-size-adjust: 100%; }
.wrap { max-width: 74rem; margin: 0 auto; padding: 0 1.25rem; }
main.wrap { padding-top: 1.5rem; padding-bottom: 3rem; }
a { color: var(--accent); text-decoration-thickness: 1px;
    text-underline-offset: 2px; }
a:hover { color: var(--accent-hover); }
a:focus-visible, button:focus-visible, select:focus-visible {
  outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 3px; }
h1 { font-size: 1.2rem; font-weight: 650; margin: 0;
     letter-spacing: -0.015em; }
h2 { font-size: .72rem; font-weight: 700; text-transform: uppercase;
     letter-spacing: .07em; color: var(--muted); margin: 0; }
.muted { color: var(--muted); }
.num-txt { font-variant-numeric: tabular-nums; }

/* Top bar */
.topbar { background: var(--surface); border-bottom: 1px solid var(--border); }
.topbar-inner { display: flex; align-items: baseline; gap: .7rem;
                padding: 1.05rem 1.25rem; flex-wrap: wrap; }
.topbar h1::before { content: ""; display: inline-block; width: .55rem;
                     height: .55rem; border-radius: 3px; background: var(--accent);
                     margin-right: .55rem; }
.topbar-sub { margin: 0; color: var(--muted); font-size: .8rem; }

/* Stat cards */
.stats { display: grid; grid-template-columns:
         repeat(auto-fit, minmax(9.5rem, 1fr)); gap: .7rem;
         margin-bottom: 1.1rem; }
.stat { background: var(--surface); border: 1px solid var(--border);
        border-radius: 10px; padding: .85rem 1rem .9rem;
        box-shadow: var(--shadow); }
.stat-label { display: block; font-size: .66rem; font-weight: 700;
              letter-spacing: .08em; text-transform: uppercase;
              color: var(--muted); }
.stat-value { display: block; font-size: 1.68rem; font-weight: 650;
              line-height: 1.15; font-variant-numeric: tabular-nums;
              letter-spacing: -0.02em; margin-top: .12rem; }
.stat-note { display: block; font-size: .75rem; color: var(--muted);
             margin-top: .1rem; }

/* Filter chips */
.chips { display: flex; flex-wrap: wrap; gap: .4rem; margin: 0 0 1rem; }
.chip { position: relative; display: inline-flex; align-items: center;
        gap: .32rem; background: var(--surface); border: 1px solid var(--border);
        border-radius: 999px; padding: .3rem .72rem; font-size: .82rem; }
.chip a { color: var(--text); text-decoration: none; font-weight: 500; }
.chip:hover { border-color: var(--accent); }
.chip:hover a { color: var(--accent); }
.chip a::after { content: ""; position: absolute; inset: 0;
                 border-radius: 999px; }
.chip a:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.chip-n { color: var(--muted); font-size: .73rem; font-weight: 600;
          font-variant-numeric: tabular-nums; }
.chip.is-active { background: var(--text); border-color: var(--text); }
.chip.is-active a { color: var(--on-solid); }
.chip.is-active:hover { border-color: var(--text); }
.chip.is-active:hover a { color: var(--on-solid); }
.chip.is-active .chip-n { color: var(--on-solid-muted); }

/* Panel + table */
.panel { background: var(--surface); border: 1px solid var(--border);
         border-radius: 12px; overflow: hidden; box-shadow: var(--shadow); }
.panel-head { display: flex; justify-content: space-between; align-items: center;
              gap: 1rem; padding: .8rem 1rem;
              border-bottom: 1px solid var(--border); }
.panel-count { font-size: .78rem; color: var(--muted);
               font-variant-numeric: tabular-nums; }
.table-wrap { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; font-size: .875rem; }
th, td { text-align: left; padding: .75rem .9rem; vertical-align: top;
         border-bottom: 1px solid var(--border); }
th { background: var(--th-bg); font-size: .68rem; font-weight: 700;
     text-transform: uppercase; letter-spacing: .06em; color: var(--muted);
     white-space: nowrap; }
tbody tr:last-child td { border-bottom: 0; }
tbody tr:hover td { background: var(--row-hover); }
td.num, th.num { text-align: right; white-space: nowrap; font-weight: 650;
                 font-variant-numeric: tabular-nums; }

/* Job cell */
.jid { display: inline-block; font-family: ui-monospace, SFMono-Regular,
       Menlo, Consolas, monospace; font-size: .66rem; color: var(--neutral-ink);
       background: var(--neutral-bg); border: 1px solid var(--border);
       border-radius: 4px; letter-spacing: .04em;
       padding: .04rem .34rem; margin-bottom: .18rem; }
.job-title { display: block; font-size: .93rem; font-weight: 650;
             letter-spacing: -0.005em; color: var(--text);
             text-decoration: none; }
.job-title:hover { color: var(--accent-hover); text-decoration: underline; }
.job-sub { display: block; color: var(--muted); font-size: .8rem;
           margin-top: .08rem; }

/* Badges */
.badge { display: inline-block; padding: .18rem .55rem; border-radius: 999px;
         font-size: .68rem; letter-spacing: .05em; white-space: nowrap;
         border: 1px solid transparent; box-shadow: var(--badge-ring); }
.badge strong { font-weight: 700; }
.tone-ok { background: var(--ok-bg); color: var(--ok-ink); }
.tone-bad { background: var(--bad-bg); color: var(--bad-ink); }
.tone-warn { background: var(--warn-bg); color: var(--warn-ink); }
.tone-info { background: var(--info-bg); color: var(--info-ink); }
.tone-indigo { background: var(--indigo-bg); color: var(--indigo-ink); }
.tone-violet { background: var(--violet-bg); color: var(--violet-ink); }
.tone-teal { background: var(--teal-bg); color: var(--teal-ink); }
.tone-neutral { background: var(--neutral-bg); color: var(--neutral-ink); }

/* Status action */
.status-form { display: inline-flex; gap: .35rem; align-items: center; }
.status-form select, .status-form button {
  font: inherit; font-size: .78rem; padding: .28rem .5rem;
  border: 1px solid var(--border-strong); border-radius: 6px;
  background: var(--surface); color: var(--text); }
.status-form select:hover { border-color: var(--muted); }
.status-form button { background: var(--text); border-color: var(--text);
                      color: var(--on-solid); font-weight: 600;
                      padding: .28rem .7rem; cursor: pointer; }
.status-form button:hover { background: var(--accent-hover);
                            border-color: var(--accent-hover);
                            color: var(--on-solid); }
.status-form select:focus-visible, .status-form button:focus-visible {
  outline: 2px solid var(--accent); outline-offset: 1px; }

/* Empty state */
.empty { padding: 3rem 1.5rem; text-align: center; }
.empty-title { margin: 0 0 .5rem; font-size: 1.05rem; font-weight: 650; }
.empty-text { margin: 0 auto .5rem; max-width: 42rem; color: var(--muted);
              font-size: .88rem; line-height: 1.6; }
.empty-hint { margin: 0; font-size: .8rem; }

/* Detail view */
.back { margin: 0 0 .9rem; font-size: .85rem; }
.back a { text-decoration: none; }
.back a:hover { text-decoration: underline; }
.detail-head { background: var(--surface); border: 1px solid var(--border);
               border-radius: 12px; padding: 1.15rem 1.25rem;
               margin-bottom: 1rem; box-shadow: var(--shadow); }
.detail-head h1 { font-size: 1.35rem; margin: .35rem 0 .7rem; }
.badges { display: flex; flex-wrap: wrap; gap: .5rem; align-items: center; }
.cols { display: grid; grid-template-columns: 1fr; gap: 1rem; }
@media (min-width: 900px) { .cols { grid-template-columns: 1.05fr .95fr; } }
.card { background: var(--surface); border: 1px solid var(--border);
        border-radius: 12px; padding: 1.05rem 1.2rem;
        box-shadow: var(--shadow); }
dl { display: grid; grid-template-columns: 10rem 1fr; gap: .5rem 1rem;
     margin: .8rem 0 0; font-size: .875rem; }
dt { color: var(--muted); font-weight: 600; font-size: .72rem;
     text-transform: uppercase; letter-spacing: .06em; padding-top: .2rem; }
dd { margin: 0; word-break: break-word; }
ul { margin: .3rem 0 .9rem; padding-left: 1.15rem; font-size: .875rem; }
li { margin-bottom: .15rem; }
.reasons { margin: .4rem 0 0; padding-left: 1rem; font-size: .78rem;
           line-height: 1.5; color: var(--muted); }
.evidence-head { font-size: .8rem; font-weight: 650; margin: .9rem 0 .1rem;
                 padding-top: .75rem; border-top: 1px solid var(--border); }
.note { font-size: .78rem; color: var(--muted); margin-top: 1rem; }

/* Error page */
.error-card { max-width: 34rem; margin: 4rem auto; text-align: center; }
.error-card h1 { font-size: 1.2rem; margin-bottom: .5rem; }

/* Responsive: rows become stacked cards on small screens */
@media (max-width: 760px) {
  .table-wrap table, .table-wrap tbody, .table-wrap tr,
  .table-wrap td { display: block; width: 100%; }
  .table-wrap thead { display: none; }
  .table-wrap tr { border-bottom: 1px solid var(--border); padding: .45rem 0; }
  .table-wrap tr:last-child { border-bottom: 0; }
  .table-wrap td { border-bottom: 0; padding: .3rem .9rem; display: flex;
                   gap: 1rem; justify-content: space-between;
                   align-items: center; text-align: left; }
  .table-wrap td::before { content: attr(data-label); flex: 0 0 auto;
                           color: var(--muted); font-size: .66rem;
                           font-weight: 700; letter-spacing: .06em;
                           text-transform: uppercase; }
  .table-wrap td.num::before { content: "Match"; }
  .table-wrap td:first-child { display: block; padding-top: .85rem; }
  .table-wrap td:first-child::before { content: none; }
  dl { grid-template-columns: 1fr; }
  dl dt { margin-top: .5rem; }
}
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


# Badge tones — presentation only: a colour never changes eligibility,
# the score, or the workflow status.
_ELIGIBILITY_TONE = {
    "ELIGIBLE": "ok",
    "NOT_ELIGIBLE": "bad",
    "REVIEW_REQUIRED": "warn",
    "UNKNOWN": "neutral",
}

_STATUS_TONE = {
    "FOUND": "info",
    "REVIEWED": "indigo",
    "SAVED": "teal",
    "APPLIED": "violet",
    "INTERVIEW": "warn",
    "OFFER": "ok",
    "REJECTED": "bad",
    "CLOSED": "neutral",
}


def _badge(value: str, mapping: dict[str, str]) -> str:
    """A small status chip; the literal value stays readable inside it."""
    tone = mapping.get(value, "neutral")
    return f'<span class="badge tone-{tone}"><strong>{_esc(value)}</strong></span>'


def _job_subline(job: DashboardJob) -> str:
    """``Company · Location`` under the title, or a dash when neither exists."""
    parts = [_esc(part) for part in (job.company, job.location) if part]
    if not parts:
        return '<span class="muted">—</span>'
    return " · ".join(parts)


def _stat_cards(overview: tuple[DashboardJob, ...]) -> str:
    """Overview counters — counted from the whole eligible set, not a filter."""
    counts: dict[str, int] = {}
    for job in overview:
        counts[job.status.value] = counts.get(job.status.value, 0) + 1
    cards = (
        ("Total", len(overview), "eligible jobs"),
        ("Found", counts.get("FOUND", 0), "awaiting your review"),
        ("Applied", counts.get("APPLIED", 0), "application submitted"),
        ("Interview", counts.get("INTERVIEW", 0), "company responded"),
        ("Offer", counts.get("OFFER", 0), "offer received"),
    )
    cells = "".join(
        '<div class="stat">'
        f'<span class="stat-label">{label}</span>'
        f'<span class="stat-value">{value}</span>'
        f'<span class="stat-note">{note}</span>'
        "</div>"
        for label, value, note in cards
    )
    return f'<section class="stats" aria-label="Summary">{cells}</section>'


def _filter_nav(
    current: WorkflowStatus | None, overview: tuple[DashboardJob, ...]
) -> str:
    """Status chips with live counts; ``/`` is the unfiltered "All"."""
    counts: dict[str, int] = {}
    for job in overview:
        counts[job.status.value] = counts.get(job.status.value, 0) + 1

    def chip(href: str, label: str, count: int, active: bool) -> str:
        wrapper = "chip is-active" if active else "chip"
        aria = ' aria-current="page"' if active else ""
        return (
            f'<span class="{wrapper}">'
            f'<a href="{href}"{aria}>{label}</a>'
            f'<span class="chip-n">{count}</span>'
            "</span>"
        )

    links = [chip("/", "All", len(overview), current is None)]
    for item in WORKFLOW_ORDER:
        links.append(
            chip(
                f"/?status={item.value}",
                item.value.capitalize(),
                counts.get(item.value, 0),
                current is item,
            )
        )
    return (
        '<nav class="chips" aria-label="Filter by workflow status">'
        + "".join(links)
        + "</nav>"
    )


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
    jobs: tuple[DashboardJob, ...],
    current: WorkflowStatus | None = None,
    all_jobs: tuple[DashboardJob, ...] | None = None,
) -> str:
    """The primary view: summary cards, status chips, then the job table.

    ``jobs`` is what to display (the store has already applied the
    eligibility and status filters); ``all_jobs`` is the unfiltered eligible
    set, used only to count the cards and chips so they stay correct while a
    filter is active. It defaults to ``jobs``, so ``render_index(rows)``
    counts exactly what it is given.
    """
    overview = jobs if all_jobs is None else all_jobs
    topbar = (
        '<header class="topbar"><div class="wrap topbar-inner">'
        "<h1>Job Search Command Center</h1>"
        '<p class="topbar-sub">Eligible jobs only · source of truth: SQLite</p>'
        "</div></header>"
    )
    intro = _stat_cards(overview) + _filter_nav(current, overview)
    count_label = "job" if len(jobs) == 1 else "jobs"
    scope = "" if current is None else f" · {current.value}"
    panel_head = (
        '<div class="panel-head"><h2>Jobs</h2>'
        f'<span class="panel-count">{len(jobs)} {count_label}{scope}</span></div>'
    )

    if not jobs:
        if current is None:
            title = "No eligible jobs yet."
            text = (
                '<p class="empty-text">Jobs appear here after the Gmail sync, '
                "AI extraction and analysis — and only when the eligibility "
                "result is <strong>ELIGIBLE</strong>.</p>"
                '<p class="empty-hint muted">Every processed job is still '
                "stored in SQLite with its full analysis; it is simply not "
                "listed here.</p>"
            )
        else:
            title = (
                f"No eligible jobs with status <strong>{current.value}</strong>."
            )
            text = (
                '<p class="empty-text">No eligible job is currently in this '
                "step of the workflow.</p>"
                '<p class="empty-hint muted">Pick another status above, or '
                "select <strong>All</strong> to see the whole list.</p>"
            )
        return _page(
            "Command Center",
            topbar
            + '<main class="wrap">'
            + intro
            + '<div class="panel">'
            + panel_head
            + f'<div class="empty"><p class="empty-title">{title}</p>{text}</div>'
            + "</div></main>",
        )

    rows = []
    for job in jobs:
        rows.append(
            "<tr>"
            '<td data-label="Job">'
            f'<span class="jid">JOB-{job.job_id}</span>'
            f'<a class="job-title" href="/job/{job.job_id}">{_esc(job.title)}</a>'
            f'<span class="job-sub">{_job_subline(job)}</span>'
            "</td>"
            f'<td data-label="Eligibility">'
            f"{_badge(job.eligibility.value, _ELIGIBILITY_TONE)}</td>"
            f'<td class="num">{job.match_percent}%</td>'
            f'<td data-label="Status">'
            f"{_badge(job.status.value, _STATUS_TONE)}</td>"
            f'<td data-label="Change">{_status_form(job)}</td>'
            "</tr>"
        )
    table = (
        '<div class="table-wrap"><table>'
        "<thead><tr>"
        "<th>Job</th><th>Eligibility</th>"
        '<th class="num">Match</th><th>Status</th><th>Change status</th>'
        "</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody>"
        "</table></div>"
    )
    return _page(
        "Command Center",
        topbar
        + '<main class="wrap">'
        + intro
        + '<div class="panel">'
        + panel_head
        + table
        + "</div></main>",
    )


def render_detail(job: DashboardJob) -> str:
    """The job-detail view: inspect first, then change the workflow status."""
    if job.remote_mode:
        remote_mode = _esc(job.remote_mode)
    else:
        remote_mode = '<span class="muted">—</span>'

    reasons = (
        '<ul class="reasons">'
        + "".join(f"<li>{_esc(r)}</li>" for r in job.eligibility_reasons)
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
        '<main class="wrap">'
        f'<p class="back"><a href="/">&larr; Back to dashboard</a></p>'
        '<header class="detail-head">'
        f'<span class="jid">JOB-{job.job_id}</span>'
        f"<h1>{_esc(job.title)}</h1>"
        '<div class="badges">'
        f"{_badge(job.eligibility.value, _ELIGIBILITY_TONE)}"
        f"{_badge(job.status.value, _STATUS_TONE)}"
        f"{_status_form(job)}"
        "</div>"
        "</header>"
        '<div class="cols">'
        '<section class="card"><h2>Overview</h2>'
        "<dl>"
        f"<dt>Job ID</dt><dd>JOB-{job.job_id}</dd>"
        f"<dt>Position</dt><dd>{_esc(job.title)}</dd>"
        f"<dt>Company</dt><dd>{_esc(job.company)}</dd>"
        f"<dt>Location</dt><dd>{_esc(job.location)}</dd>"
        f"<dt>Remote mode</dt><dd>{remote_mode}</dd>"
        f"<dt>Professional Match</dt>"
        f"<dd>{job.match_percent}% <span class=\"muted\">"
        f"(score {job.score})</span></dd>"
        f"<dt>Eligibility</dt><dd>"
        f"{_badge(job.eligibility.value, _ELIGIBILITY_TONE)}{reasons}</dd>"
        f"<dt>Workflow Status</dt>"
        f"<dd>{_badge(job.status.value, _STATUS_TONE)}</dd>"
        f"<dt>Application URL</dt><dd>{_application_url_link(job.application_url)}"
        "</dd>"
        f"<dt>First seen</dt><dd>{_esc(job.first_seen_at)}</dd>"
        f"<dt>Last seen</dt><dd>{_esc(job.last_seen_at)}</dd>"
        "</dl></section>"
        '<section class="card"><h2>Match evidence</h2>'
        f'<p class="evidence-head">Matched skills</p>{matched}'
        f'<p class="evidence-head">Missing required skills</p>{missing}'
        '<p class="note">Status changes are manual; eligibility and match '
        "never change them.</p>"
        "</section>"
        "</div></main>"
    )
    return _page(f"JOB-{job.job_id}", body)


def render_error(message: str) -> str:
    """A minimal error page."""
    return _page(
        "Error",
        '<main class="wrap">'
        '<div class="card error-card">'
        "<h1>Something went wrong</h1>"
        f"<p>{_esc(message)}</p>"
        '<p class="muted"><a href="/">&larr; Back to the dashboard</a></p>'
        "</div></main>",
    )


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
            # One pass over the eligible set: the cards/chips count all of
            # it, the table shows the status-filtered slice of it.
            eligible = list_eligible_jobs(self.server.store)
            jobs = (
                eligible
                if status is None
                else tuple(job for job in eligible if job.status is status)
            )
            self._send(200, render_index(jobs, status, eligible))
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
