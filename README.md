# JobSearch AI

A personal, AI-powered **Job Search Command Center**: one place to collect job
opportunities, analyze them against a career profile, and track every
application from first sight to offer.

## Project purpose

A job search is scattered across job boards, inboxes, spreadsheets, and open
tabs. JobSearch AI consolidates that workflow into a single, automated
pipeline:

- Collect job opportunities from multiple sources (boards, alerts, emails).
- Analyze job descriptions with AI against a structured career profile.
- Rank opportunities with a matching engine.
- Keep the status of every application in one tracker.
- Surface everything on a simple dashboard.

## Current scope

**Phase 6A — Manual Gmail sync with a real Gmail account** (what this
repository contains today): `python -m jobsearch.gmail_sync` fetches job
emails **read-only** from a real mailbox, runs them through the existing
pipeline, and persists them — so the Command Center shows them. The sync
runs only when you invoke it; there is still no scheduler.

**Phase 5 — MVP Job Search Command Center**: a local dashboard over SQLite
that makes the pipeline usable — eligible jobs in a table, manual workflow
statuses, everything persisted.

Earlier phases delivered the foundation, the analysis core, the email
ingestion boundary, the pipeline that produces each result, and the
persistence layer it all lands in:

- Phase 1 delivered the foundation: Python 3.12+ `src/`-based layout,
  environment-based configuration (no hard-coded secrets), pytest, docs.
- Phase 2 delivered the data models and manual JSON ingestion:
  `CandidateProfile`, `JobPosting`, `ProfessionalMatch`, `EligibilityResult`,
  `EligibilityStatus`/`AuthorizationSignal` vocabularies, and
  `load_career_profile()` / `load_jobs()`.
- Phase 3a delivered `src/jobsearch/eligibility.py` — a rule-based
  `evaluate_eligibility(posting, profile)` (`tests/test_eligibility.py`).
- Phase 3b delivered `src/jobsearch/professional_match.py` — a fully
  deterministic, explicitly weighted scorer — plus
  `src/jobsearch/analysis.py` with the thin `analyze_job()`
  (`tests/test_professional_match.py`, `tests/test_analysis.py`).
- Phase 3c delivered `src/jobsearch/extraction.py` — a **provider-neutral**
  AI extraction layer: a `JobDescriptionExtractor` protocol, the
  `JobExtraction` schema with fail-safe validation
  (`ExtractionValidationError`), uncertainty preservation (never invents
  salary/location/work authorization), and work-authorization evidence
  preservation — plus `analyze_description()` as the full text → analysis
  pipeline (`tests/test_extraction.py`).
- Phase 4A delivered the email ingestion boundary: the `EmailMessage`
  model, the provider-neutral `EmailProvider` protocol, a **read-only**
  Gmail adapter (`src/jobsearch/gmail.py`), the deterministic job-email
  detector (`src/jobsearch/job_email_detector.py`), and Gmail
  configuration via environment variables
  (`tests/test_email_intake.py`, `tests/test_job_email_detector.py`).
- Phase 4B delivered the thin orchestration connecting those components
  (`src/jobsearch/email_pipeline.py`, `tests/test_email_pipeline.py`):

  ```
  EmailMessage → detect_job_email() → candidate job email → JobDescriptionExtractor → JobPosting → analyze_job() → ProfessionalMatch + EligibilityResult → JobAnalysis
  ```

  - `NOT_JOB` emails stop before the extractor; `JOB_LIKELY` /
    `POSSIBLE_JOB` emails pass the preserved body **verbatim** through the
    existing validated extraction path (Phase 3c);
  - outcomes are `NOT_JOB`, `JOB_ANALYZED`, or `EXTRACTION_FAILED` — errors
    are preserved in the result, never swallowed;
  - nothing is duplicated or redesigned: no new scoring, no eligibility
    rules, no extraction validation.

**Phase 4C adds** durable local persistence for exactly those results
(`src/jobsearch/persistence.py`, `tests/test_persistence.py`):

- four related entities in SQLite — **Email** (unique `message_id`, body
  verbatim), **Job** (the normalized opportunity), **EmailJob** (how an
  email references a job), and **JobAnalysis** (the complete Phase 3a/3b
  analysis) — written in one transaction;
- **deterministic job deduplication**: canonical application URL →
  normalized job attributes → supporting email evidence only, so several
  emails from different senders can reference one job while uncertain
  records stay separate (possible duplicates are never auto-merged);
- `message_id` idempotency plus `first_seen_at` / `last_seen_at`
  observation tracking (timezone-aware UTC);
- persistence **stores and relates results only** — no scoring, no
  eligibility, no classification, no source-specific branches.

**Phase 5 adds** the first functional MVP dashboard
(`src/jobsearch/workflow.py`, `src/jobsearch/command_center.py`,
`src/jobsearch/dashboard.py`, `tests/test_workflow.py`,
`tests/test_dashboard.py`):

- a **workflow status** for jobs — `FOUND → REVIEWED → SAVED → APPLIED →
  INTERVIEW → OFFER`, plus terminal `REJECTED`/`CLOSED` — stored in SQLite
  with default `FOUND`, advanced **manually only** (eligibility and match
  never change it), with a small deterministic transition validator;
- a read model that shows **only `ELIGIBLE` jobs** (other eligibility
  statuses stay persisted and are merely filtered from the view) and the
  existing professional match score as a **percentage** — informational,
  never used to rank;
- a local, dependency-free web UI (standard-library `http.server`) with a
  status filter, allowed status actions, and a job-detail view, writing
  every status change back to SQLite.

**Phase 6A adds** the real-mailbox manual sync
(`src/jobsearch/gmail_sync.py`, `src/jobsearch/ai_extractor.py`,
`tests/test_gmail_sync.py`, `tests/test_ai_extractor.py`):

- `python -m jobsearch.gmail_sync` fetches messages **once, on demand**,
  from a real Gmail account (read-only, `gmail.readonly`) and reuses the
  existing pipeline unchanged — detector → AI extraction → analysis →
  Phase 4C persistence;
- the concrete AI extractor talks to any OpenAI-compatible
  chat-completions endpoint over **standard-library HTTP** (`AI_*`
  settings — no SDK, no new dependency) and only extracts: validation,
  scoring, and eligibility engines are untouched;
- **idempotent**: already-analyzed messages are skipped before extraction
  and Phase 4C dedup still applies, so re-running never duplicates emails
  or jobs and makes no repeat AI calls;
- one problematic email never ends a run — failures are counted and
  listed in a deterministic summary, never hidden;
- new jobs land in the Command Center with workflow status `FOUND`.

**Jobright is not the primary source: the email itself is the ingestion
boundary.** LinkedIn, Jobright, Glassdoor, Indeed, CareerBuilder, other
boards, recruiters, companies, and sources not yet known all flow through
the same pipeline. **No source-specific parsers or branches exist** — the
test suite asserts the ingestion, orchestration, and persistence modules
contain no job-board references.

Still explicitly **not** included: Notion, GitHub Actions, GitHub Pages,
scraping, browser automation, external database services (the persistence
layer is local SQLite only), web frameworks (e.g. FastAPI), provider SDKs
in the core (the Google client libraries remain an optional, lazily
imported extra used only by `build_gmail_service()`), a scheduler,
cron, or automatic Gmail scanning (the Phase 6A sync is **manual** on
purpose), and any external AI API in the tests. Real Gmail access exists
only through the Phase 6A command, run by you, with credentials and an
API key you provide. Unit
tests require **no API key, no Gmail account, and no network access** —
they use deterministic fakes, and the database tests use in-memory or
temporary SQLite databases, never `data/jobsearch.db`. The Phase 5
Command Center is a **local** MVP UI (loopback only, no auth, no
deployment); a static/remote dashboard remains a future phase.

## Project structure

```
JobSearch/
├── src/
│   └── jobsearch/
│       ├── __init__.py           # Package metadata (version)
│       ├── config.py             # Environment-based configuration
│       ├── models.py             # Profile, posting, match, eligibility types
│       ├── eligibility.py        # Rule-based eligibility analyzer (Phase 3a)
│       ├── professional_match.py # Deterministic match scorer (Phase 3b)
│       ├── extraction.py         # Provider-neutral AI extraction (Phase 3c)
│       ├── analysis.py           # analyze_job()/analyze_description() layer
│       ├── email_provider.py     # EmailProvider protocol + hint helper (4A)
│       ├── gmail.py              # Read-only Gmail adapter (Phase 4A)
│       ├── job_email_detector.py # Deterministic job-email detector (4A)
│       ├── email_pipeline.py     # Email → extraction → analysis (4B)
│       ├── persistence.py        # SQLite persistence + job dedup (4C)
│       ├── workflow.py           # Manual workflow statuses + transitions (5)
│       ├── command_center.py     # Eligible-jobs read model for the UI (5)
│       ├── dashboard.py          # Local MVP web dashboard (Phase 5)
│       ├── ai_extractor.py       # AI extraction over HTTP (Phase 6A)
│       ├── gmail_sync.py         # Manual Gmail sync command (Phase 6A)
│       └── ingestion.py          # Manual JSON ingestion (jobs + profile)
├── data/
│   ├── career_profile.json       # Cristiam's career/eligibility facts
│   ├── jobs.json                 # Manually added job postings
│   └── jobsearch.db              # Local SQLite database (git-ignored)
├── tests/
│   ├── test_health.py            # Health/smoke tests
│   ├── test_models.py            # Data model tests
│   ├── test_ingestion.py         # Manual ingestion tests
│   ├── test_eligibility.py       # Eligibility analyzer tests (Phase 3a)
│   ├── test_professional_match.py# Match scorer tests (Phase 3b)
│   ├── test_analysis.py          # analyze_job() tests (Phase 3b)
│   ├── test_extraction.py        # AI extraction tests (Phase 3c, fake AI)
│   ├── test_email_intake.py      # Email model/adapter/config tests (4A)
│   ├── test_job_email_detector.py# Detector tests (Phase 4A)
│   ├── test_email_pipeline.py    # Email → analysis pipeline tests (4B)
│   ├── test_persistence.py       # SQLite persistence + dedup tests (4C)
│   ├── test_workflow.py          # Workflow status rules + storage tests (5)
│   ├── test_dashboard.py         # Command Center read model + UI tests (5)
│   ├── test_ai_extractor.py      # AI extractor over HTTP tests (6A, fake)
│   └── test_gmail_sync.py        # Manual Gmail sync orchestration tests (6A)
├── .env.example                  # Template for local environment variables
├── .gitignore                    # Python-appropriate ignores (incl. .env, secrets/)
├── pyproject.toml                # Project metadata + pytest configuration
├── requirements.txt              # Runtime dependencies
├── requirements-dev.txt          # Development/test dependencies
└── README.md
```

## Planned architecture

The system is designed as a staged pipeline. Each stage is independent so it
can be added, tested, and replaced incrementally:

```
   sources          ingestion            intelligence                output
+--------------+  +----------------+  +------------------------+  +------------------+
| Job boards   |  | Email intake   |  | Cristiam career profile|  | Notion API       |
| alerts       +->| Gmail, any     +->| eligibility (3a)       +->| application      |
| recruiters   |  | source         |  | match scoring (3b)     |  | tracker          |
| companies    |  | detector (4A)  |  | AI extraction (3c)     |  +------------------+
+--------------+  +----------------+  +------------------------+            |
                                                                    +-------v----------+
                                                                    | GitHub Actions   |
                                                                    | scheduled runs   |
                                                                    +-------+----------+
                                                                    +-------v----------+
                                                                    | GitHub Pages     |
                                                                    | dashboard        |
                                                                    +------------------+
```

Planned components:

- **Cristiam career profile** — a structured, versioned profile (skills,
  experience, preferences) used as the reference for all matching.
  **Implemented in Phase 2** (`data/career_profile.json`).
- **Eligibility analyzer** — rule-based evaluation of location, work
  authorization, citizenship, visa, and residency requirements; strictly
  separate from professional match. **Implemented in Phase 3a**
  (`src/jobsearch/eligibility.py`).
- **Job matching engine** — scores postings against the career profile.
  **Implemented in Phase 3b** (`src/jobsearch/professional_match.py`).
- **AI job description analysis** — extracts requirements, seniority,
  salary, and location from raw postings into a structured `JobPosting`.
  **Implemented in Phase 3c** (`src/jobsearch/extraction.py`) behind a
  provider-neutral protocol; the provider adapter itself is supplied by the
  user (no SDK in the core, no key in the tests).
- **Email intake** — retrieves emails through a provider-neutral protocol
  and normalizes them into `EmailMessage`. **Implemented in Phase 4A**
  (`src/jobsearch/email_provider.py`, `src/jobsearch/gmail.py`,
  `src/jobsearch/job_email_detector.py`), read-only and source-agnostic:
  boards, recruiters, and companies arrive through the same path, with no
  board-specific parsers.
- **Email → job analysis pipeline** — orchestrates detection, extraction,
  and the two deterministic engines over one email. **Implemented in
  Phase 4B** (`src/jobsearch/email_pipeline.py`), source-agnostic and thin:
  it adds no scoring, no eligibility rules, and no extraction validation
  of its own.
- **Persistence layer** — durable local storage for emails, jobs, their
  relationships, and their analyses, with deterministic job
  deduplication. **Implemented in Phase 4C**
  (`src/jobsearch/persistence.py`): standard-library SQLite, one
  transactional `save()`, and a deliberately dumb design — it stores and
  relates results, never scoring, classifying, or deciding eligibility.
- **Command Center dashboard** — a presentation and manual-workflow layer
  over SQLite: eligible jobs only, match as a percentage, and manual
  workflow statuses. **Implemented in Phase 5 (MVP)**
  (`src/jobsearch/workflow.py`, `src/jobsearch/command_center.py`,
  `src/jobsearch/dashboard.py`): a local, dependency-free UI that reads
  and writes the existing database and never re-analyzes anything.
- **Notion API** — canonical application tracker (status, notes, follow-ups).
- **GitHub Actions** — runs the pipeline on a schedule; no server to maintain.
- **GitHub Pages dashboard** — static dashboard generated from pipeline output.

Storage (Notion + generated files), orchestration (GitHub Actions), and
presentation (GitHub Pages) are deliberately decoupled from analysis, so each
can be developed and tested in isolation.

## Candidate eligibility rules

These rules define the behavior of the eligibility analyzer. They are
**implemented in Phase 3a** as the rule-based function
`evaluate_eligibility(posting, profile)` in `src/jobsearch/eligibility.py`,
with tests in `tests/test_eligibility.py`. AI-assisted refinement of the
*inputs* (extraction) arrived in Phase 3c; the status itself is still
computed exclusively by the rule-based engine.

### Candidate profile

| Attribute                  | Value |
|----------------------------|-------|
| Candidate country          | Honduras |
| Candidate region           | LATAM |
| US citizenship             | No |
| US Green Card              | No |
| US OPT                     | No |
| Current US work authorization | No |

The profile lives in `data/career_profile.json` and is loaded with
`load_career_profile()`. Since Phase 3b it also carries professional fields
used **only** by the match scorer, never by eligibility: `skills`,
`years_experience`, `technologies`, `education`, `certifications`, and
`languages`. These ship empty — fill them in with your real data.

### Professional match vs. eligibility

The system must keep these two concepts strictly separate:

|                     | Professional match                       | Eligibility                                             |
|---------------------|------------------------------------------|---------------------------------------------------------|
| Question            | How well do the skills and experience match the job? | Can the candidate realistically apply at all? |
| Basis               | Skills, experience, technologies, education, certifications, languages | Location, work authorization, citizenship, visa, residency, and similar explicit requirements |
| Representation      | Numeric score (0.0–1.0) in `ProfessionalMatch` | Categorical status in `EligibilityResult` — **never a numeric score** |

They must **never** be represented as the same score, merged into a single
metric, or compared with one another. `EligibilityResult` deliberately has no
score field; only `ProfessionalMatch` is score-bearing. Work-authorization
wording in a posting never affects the professional score, and professional
gaps never affect the eligibility status.

### Eligibility statuses

- `ELIGIBLE`
- `NOT_ELIGIBLE`
- `REVIEW_REQUIRED`
- `UNKNOWN`

### Location rules (current candidate)

| Posting location            | Result |
|-----------------------------|--------|
| Remote — LATAM              | `ELIGIBLE` |
| Remote — Latin America      | `ELIGIBLE` |
| Remote — Americas           | Potentially eligible; `REVIEW_REQUIRED` — review if restrictions exist |
| Remote — worldwide/global   | Potentially eligible → `REVIEW_REQUIRED` |
| US-only remote              | `NOT_ELIGIBLE` |
| Hybrid                      | `NOT_ELIGIBLE` |
| On-site / in-person         | `NOT_ELIGIBLE` |

### What the analyzer must inspect

- The **structured `location` field** of a posting **and** the **complete job
  description** — a job can say "Remote" while restricting candidates to a
  specific country or region. This is why the extraction layer (Phase 3c)
  must never strip evidence out of the description.

### Work-authorization, citizenship, and residency detection

The analyzer must detect explicit requirements, including variations such as:

- US citizen / U.S. citizen; US citizenship required
- Green Card / permanent resident
- US work authorization; authorized to work in the United States
- employment authorization; EAD; OPT; CPT
- H-1B; visa requirements; sponsorship requirements

**Not every occurrence of these terms is an automatic rejection.** The
analyzer must interpret each requirement, distinguishing:

| Signal                     | Meaning                                   | Example wording                        |
|----------------------------|-------------------------------------------|----------------------------------------|
| `AUTHORIZATION_REQUIRED`   | Candidate must already hold authorization  | "Must already have US work authorization." |
| `SPONSORSHIP_AVAILABLE`    | Employer offers sponsorship               | "Visa sponsorship available."          |
| `SPONSORSHIP_UNAVAILABLE`  | Employer does not sponsor                 | "No visa sponsorship is offered."      |
| `REQUIREMENT_SATISFIED`    | Requirement is explicitly met by the candidate | "No prior US work authorization required." |
| `AMBIGUOUS`                | Requirement unclear, unstated, or conflicting | "Applicants must be work-authorized" (jurisdiction unstated) |

"Visa sponsorship available" is **not** the same as "Must already have US work
authorization."

When evidence is ambiguous, the analyzer must return `REVIEW_REQUIRED` or
`UNKNOWN` instead of an unsupported decision. `NOT_ELIGIBLE` is reserved for
explicit, unambiguous exclusions (e.g. US-only remote, hybrid, on-site) where
the restriction clearly applies to this candidate.

### Using the analyzer

```python
from jobsearch.eligibility import evaluate_eligibility
from jobsearch.ingestion import load_career_profile, load_jobs

profile = load_career_profile("data/career_profile.json")
jobs = load_jobs("data/jobs.json")

result = evaluate_eligibility(jobs[0], profile)
result.status   # EligibilityStatus: ELIGIBLE / NOT_ELIGIBLE / REVIEW_REQUIRED / UNKNOWN
result.reasons   # concrete evidence, including the matched wording
result.signals   # AuthorizationSignal values — never a score
```

## Professional match scoring (Phase 3b)

Implemented in `src/jobsearch/professional_match.py` as
`evaluate_professional_match(posting, profile)` → `ProfessionalMatch`.
The scorer is **fully deterministic and standard-library only — no AI or
LLM is involved.** The score always lies between `0.0` and `1.0`.

Both evaluations run independently and are only ever placed side by side:

```
JobPosting
   |
   +--> evaluate_eligibility()         -> EligibilityResult    (categorical)
   |
   +--> evaluate_professional_match()  -> ProfessionalMatch    (numeric 0.0–1.0)
```

### Scoring dimensions and weights

Each dimension compares one requirement list from the posting against the
matching profile data:

| Dimension | Weight | Posting field | Profile field |
|-----------|-------:|---------------|---------------|
| Required skills | 0.35 | `required_skills` | `skills` + `technologies` |
| Preferred / nice-to-have skills | 0.10 | `preferred_skills` | `skills` + `technologies` |
| Experience | 0.20 | `min_experience_years` | `years_experience` |
| Technologies / tools | 0.15 | `technologies` | `technologies` + `skills` |
| Education | 0.08 | `education` | `education` |
| Certifications | 0.05 | `certifications` | `certifications` |
| Languages | 0.07 | `languages` | `languages` |
| **Total** | **1.00** | | |

### The formula

```
score = Σ (weight_d × subscore_d) / Σ weight_d        over assessed dimensions
```

1. For every dimension the posting states, compute a subscore `s_d` in
   `[0.0, 1.0]` (rules below).
2. Sum `weight_d × s_d` over the **assessed** dimensions and divide by the
   sum of those dimensions' weights (weights are renormalized).
3. Round to 4 decimals; clamp defensively to `[0.0, 1.0]`.

Per-dimension subscore rules:

- **Item dimensions** (required skills, preferred skills, technologies,
  certifications, languages): each job-listed item scores **1.0 exact /
  0.5 partial / 0.0 missing**, and the subscore is the mean over the job's
  items. Matching is case-insensitive after normalizing separators
  (`- _ . /` → space) and applying a small deterministic alias table
  (`K8s` → `kubernetes`, `Postgres` → `postgresql`, `Golang` → `go`, …).
  A *partial* match means the two strings share a token of ≥ 3 characters,
  or one contains the other (≥ 4 characters).
- **Experience**: `min(1.0, years_experience / min_experience_years)`
  (`1.0` when the posting requires 0 years).
- **Education**: level hierarchy `associate < bachelor < master < doctorate`.
  A higher degree fully satisfies a lower requirement (1.0); the immediately
  adjacent lower degree counts as partial (0.5); anything lower scores 0.0.
  If either side is not a recognized level (e.g. "Bootcamp"), fall back to
  item matching (1.0 / 0.5 / 0.0).

### How missing information is handled

1. **The posting does not state a dimension** → the dimension is *not
   assessed*: its weight is removed and the remaining weights are
   renormalized. A posting that states only required skills is scored purely
   on required skills.
2. **The posting states a dimension the profile cannot demonstrate** → that
   subscore is **0.0**. Missing data never earns credit and never blocks a
   dimension from being assessed.
3. **The posting states no professional requirements at all** → the score is
   `0.0` with the rationale *"no professional requirements stated"*.
4. **Partial information within a dimension** → a partial item match counts
   as 0.5, never as a pass or a failure.

### Required vs. preferred skills

- **Required skills** are the posting's must-haves: they carry weight 0.35,
  each miss drops the subscore directly, and every miss is recorded in
  `missing_required_skills` **and** turned into an `interview_topics` entry
  ("Missing required skill: …").
- **Preferred (nice-to-have) skills** carry weight 0.10: misses only affect
  that smaller dimension, are recorded in `missing_preferred_skills`, and are
  **not** interview topics. One extra missing preferred skill costs at most
  `0.10 × 1.0 = 0.10` on the final score (typically `0.05` for a half match).

### Evidence preserved in `ProfessionalMatch`

| Field | Contents |
|-------|----------|
| `matched_skills` | Exact matches among required/preferred skills and technologies |
| `partial_matches` | Partial matches (0.5) from the same sources |
| `missing_required_skills` | Unmatched required skills **and** technologies (tooling is required tooling) |
| `missing_preferred_skills` | Unmatched preferred skills |
| `experience_matches` | e.g. "6 years of experience meets the 5 years required" |
| `cv_keywords` | ATS keywords from the posting: required + preferred + technologies, in order, deduplicated |
| `interview_topics` | Every gap to prepare: missing required skills/tools, experience shortfall, unproven education/certifications/languages |
| `rationale` | The literal per-dimension formula breakdown, including excluded dimensions |

### Examples (both asserted by the test suite)

**High professional match + `NOT_ELIGIBLE`** — a US-only remote job the
candidate fits perfectly on paper:

```python
profile = CandidateProfile(
    country="Honduras", region="LATAM",
    skills=("Python", "FastAPI", "PostgreSQL", "Docker", "Kubernetes"),
    years_experience=6,
    technologies=("Python", "AWS", "Docker"),
    education=("Bachelor's in Computer Science",),
    certifications=("AWS Solutions Architect",),
    languages=("Spanish", "English"),
)
posting = JobPosting(
    title="Backend Engineer", company="ACME", location="Remote - US",
    description="Build backend services.",
    required_skills=("Python", "FastAPI", "PostgreSQL"),
    preferred_skills=("Kubernetes", "Terraform"),
    min_experience_years=5,
    technologies=("Python", "AWS"),
    education=("Bachelor's degree",),
    certifications=("AWS Solutions Architect",),
    languages=("English",),
)

match = evaluate_professional_match(posting, profile)
elig  = evaluate_eligibility(posting, profile)

match.score        # 0.95  (missing only the preferred "Terraform")
elig.status        # EligibilityStatus.NOT_ELIGIBLE  (US-only remote)
```

**Low professional match + `ELIGIBLE`** — an eligible LATAM job in a
completely different stack:

```python
posting = JobPosting(
    title="Mainframe Developer", company="OLD", location="Remote - LATAM",
    description="COBOL maintenance.",
    required_skills=("COBOL", "Mainframe", "SAP"),
    min_experience_years=12,
    education=("PhD",),
    languages=("German",),
)

match = evaluate_professional_match(posting, profile)   # same profile as above
elig  = evaluate_eligibility(posting, profile)

match.score        # 0.1429
elig.status        # EligibilityStatus.ELIGIBLE
```

### analyze_job(): thin composition only

`src/jobsearch/analysis.py` runs both evaluators and returns their results
side by side:

```python
from jobsearch.analysis import analyze_job

analysis = analyze_job(posting, profile)
analysis.professional_match   # ProfessionalMatch — numeric, no eligibility content
analysis.eligibility          # EligibilityResult — categorical, never a score
```

`JobAnalysis` has exactly these two fields. **There is no combined score:
`ProfessionalMatch` and `EligibilityResult` are never merged, averaged,
weighted together, or compared** — no code path in this repository computes
one, and the test suite asserts the field sets of both results.

## AI-assisted extraction (Phase 3c)

Implemented in `src/jobsearch/extraction.py`. **The AI extracts and
structures information; the deterministic engines remain the only
calculators of eligibility and match score.**

```
Raw job description
        ↓
AI extraction              JobDescriptionExtractor.extract(text) — provider-neutral
        ↓
validated data             JobExtraction.from_raw()  → ExtractionValidationError on bad output
        ↓
structured JobPosting      JobExtraction.to_job_posting() — description evidence preserved
        ↓
evaluate_eligibility()                → EligibilityResult   (categorical)
        ↓
evaluate_professional_match()         → ProfessionalMatch   (numeric 0.0–1.0)
        ↓
JobAnalysis                           analyze_job() / analyze_description() — thin layers
```

### Provider interface

The core domain never names OpenAI, Anthropic, Gemini, or any other
provider. Providers plug in through a small protocol:

```python
class JobDescriptionExtractor(Protocol):
    def extract(self, description: str) -> dict[str, object]:
        """Raw job-description text in, extracted JSON object out."""
        ...
```

A provider adapter wraps its SDK and prompt; the core validates whatever
comes back. Tests use deterministic fake extractors, so **no API key is
needed to run the suite.**

### Extraction schema

| AI field | → JobPosting field | Validation / normalization | Required |
|----------|--------------------|----------------------------|:--------:|
| `position` | `title` | non-empty string | ✅ |
| `company` | `company` | non-empty string | ✅ |
| `location` | `location` | non-empty string, never inferred | ✅ |
| `description` | `description` | non-empty when provided; must retain the source's work-authorization sentences; `null` → raw text used verbatim | no |
| `remote_mode` | `remote_mode` | normalized to `remote` / `hybrid` / `onsite` | no |
| `salary` | `salary` | string or finite number → verbatim string; never inferred | no |
| `salary_currency` | `salary_currency` | uppercased (e.g. `USD`) | no |
| `salary_period` | `salary_period` | lowercased (e.g. `year`) | no |
| `years_of_experience` | `min_experience_years` | non-negative number or `"5+"` → float; `null` → `None` | no |
| `required_skills` | `required_skills` | list of non-empty strings → tuple | no |
| `preferred_skills` | `preferred_skills` | list of non-empty strings → tuple | no |
| `technologies` | `technologies` | list of non-empty strings → tuple | no |
| `education` | `education` | list of non-empty strings → tuple | no |
| `certifications` | `certifications` | list of non-empty strings → tuple | no |
| `languages` | `languages` | list of non-empty strings → tuple | no |
| `deadline` | `deadline` | verbatim string | no |
| `application_url` | `url` | must be a valid `http(s)://` URL | no |
| `source` | `source` | string; `null` → `ai-extraction` | no |

Unknown extra keys in the AI output are ignored (providers may add their
own).

### Validation behavior

- AI output must be a JSON **object**; anything else (`null`, list, string,
  number) is rejected.
- Every problem is reported together in a single `ExtractionValidationError`
  (a `ValueError`) in fixed field order — the same invalid input always
  produces the same message (deterministic validation).
- Wrong types, empty required fields, unrecognized `remote_mode`, negative
  experience, and malformed URLs all fail safely instead of being coerced.

### Uncertainty is preserved (no hallucination)

- Undetermined optional fields are `null` → empty/None defaults on the
  posting: `salary=""`, `salary_currency=""`, `salary_period=""`,
  `remote_mode=""`, `deadline=""`, `url=""`, `min_experience_years=None`,
  list fields `()`.
- **Never inferred:** salary, location, work authorization. A missing
  `position`/`company`/`location` raises a validation error — it is never
  replaced by a guess.

### Work-authorization evidence stays in the description

- AI omits `description` → the **raw source text is used verbatim**.
- AI provides `description` → every work-authorization sentence of the source
  (citizenship, Green Card, work authorization, visa/sponsorship, EAD, OPT,
  CPT, H-1B, …) must still appear in it; otherwise validation fails with a
  clear "evidence was removed" error. Adapters that cannot guarantee
  verbatim preservation should return `description: null` and let the raw
  text flow through.

### The AI never calculates results

- The AI **must not** calculate `ProfessionalMatch.score` — only
  `evaluate_professional_match()` does.
- The AI **must not** calculate or modify `EligibilityResult.status` — only
  `evaluate_eligibility()` does.
- `extraction.py` contains no call to either engine (asserted by a test),
  `JobExtraction` has no score/status fields (asserted), and
  `analyze_description()` is a thin wrapper: extract → validate →
  `analyze_job()`.

### Using the pipeline

```python
from jobsearch.analysis import analyze_description
from jobsearch.extraction import extract_job_posting

raw_text = open("posting.txt", encoding="utf-8").read()

# Step 1: AI extraction → validated, structured JobPosting
posting = extract_job_posting(raw_text, my_extractor)

# Step 2: deterministic engines only (or one call for both)
analysis = analyze_description(raw_text, my_extractor, profile)
analysis.professional_match.score   # from evaluate_professional_match()
analysis.eligibility.status         # from evaluate_eligibility()
```

## Generic email intake (Phase 4A)

Gmail is **one provider of raw emails, not the primary source of jobs**.
The ingestion boundary is the email itself, so every current or future
source — job boards, alerts, recruiters, companies, unknown senders —
enters through the same path. No source-specific parsers exist anywhere in
the ingestion code (a test asserts the ingestion modules contain no
job-board references).

```
 Gmail API (read-only)                     normalized, preserved
 users.messages.list ── q query ──┐
 users.messages.get  ── id ───────┤
                                  ▼
 raw email ──parse──>  EmailMessage  ──detect──>  JOB_LIKELY / POSSIBLE_JOB / NOT_JOB
                                  │
                                  └─ body verbatim ──> (later phase: AI extraction → deterministic engines)
```

### The email model: `EmailMessage` (`src/jobsearch/models.py`)

| Field | Required | Notes |
|-------|:--------:|-------|
| `message_id` | ✅ | stable identity and future dedup key; the subject is **never** an identity |
| `thread_id` | — | default `""` |
| `sender` | ✅ | `From` header, verbatim |
| `subject` | — | default `""` (subjects may legitimately be empty) |
| `received_at` | ✅ | timezone-aware `datetime`: Gmail `internalDate` first, then the `Date` header |
| `body` | — | **original content preserved**: plain-text part preferred, otherwise the raw HTML part as-is — never stripped or rewritten |
| `source_hint` | — | optional, best-effort, `None` when the sender is unknown; **never authoritative** |

`source_hint` is derived with one generic rule for *any* domain — the
organization label of the sender address (`jobs@example.com` → `example`) —
and is never used for classification or routing.

### The provider interface: `EmailProvider` (`src/jobsearch/email_provider.py`)

```python
class EmailProvider(Protocol):
    def search(self, query: str) -> list[EmailMessage]: ...
    def get_message(self, message_id: str) -> EmailMessage: ...
```

Provider-neutral: Gmail implements it today, and any other mailbox backend
can implement it later without touching the domain. Malformed provider
responses raise `EmailProviderError` instead of being coerced into data.

### The Gmail adapter (`src/jobsearch/gmail.py`) — read-only

- Uses only `users.messages.list` (search via the `q` parameter) and
  `users.messages.get` (retrieval by id).
- **Never sends, deletes, or modifies messages and never changes read
  state** — a test asserts the adapter source contains none of those
  operations, and the only OAuth scope is `gmail.readonly`.
- The API service object is **injected**, so every call is mockable: tests
  need no Gmail account, OAuth login, credentials, or internet access.
- `build_gmail_service()` is the single function that touches the Google
  client libraries (imported lazily; the packages are optional, listed
  commented in `requirements.txt`).

### Search strategy: configurable, not source-specific

Nothing searches for a particular job board. The Gmail query is plain
Gmail search syntax read from the environment — change `.env`, not Python,
to broaden or narrow intake:

```bash
GMAIL_QUERY=newer_than:7d        # default
# GMAIL_QUERY=after:2026/09/01
# GMAIL_QUERY=newer_than:30d is:unread
```

The configured query is passed verbatim to the API's `q` parameter.

### Job-email detector (`src/jobsearch/job_email_detector.py`)

A lightweight, deterministic triage step that runs **before** any AI. It
does **not** decide whether an email truly contains an applicable job — it
only classifies:

| Result | Score | Meaning |
|--------|------:|---------|
| `JOB_LIKELY` | ≥ 4 | strong job signals in the content |
| `POSSIBLE_JOB` | 1 – 3 | one or two job-adjacent signals |
| `NOT_JOB` | 0 | no job-related signals at all |

Signals come from subject and body content only — sender addresses,
domains, and `source_hint` are never inspected, unknown senders are never
rejected, and no job-board domain is required:

| Signal | Points |
|--------|-------:|
| Job term in the subject (`job`, `vacancy`, `opening`, `position`, `hiring`, `role`, `opportunity`, `career`, `apply`, …) | +2 |
| Employment terminology in the body (`apply`, `application`, `candidate`, `qualifications`, `responsibilities`, `job description`, `full-time`, `we are looking for`, …) — 1–2 terms / ≥ 3 terms | +1 / +2 |
| Application URL in the body (`…/jobs/`, `apply.…`, `viewjob`, …) | +2 |
| Careers URL in the body (`careers.…`, `…/careers`, `talent.…`) | +1 |
| Recruiter terminology (`recruiter`, `talent acquisition`, `hiring manager`, …) | +1 |
| Salary/position terminology (`salary`, `compensation`, `per year`, `years of experience`, …) | +1 |

The matched evidence is recorded in `JobEmailDetection.signals`, and the
message body is only read — never modified — so the original content stays
available for the later AI extraction phase.

### Deduplication foundation (full job dedup deliberately later)

`message_id` is preserved unchanged end to end and asserted across
searches and retrievals. Two messages with the same subject stay two
distinct messages; `message_id` is what a later phase will use to avoid
processing the same email twice. Job-level deduplication is **not**
attempted in this phase — it arrived in Phase 4C (see "SQLite
persistence and deduplication" below).

### Using the intake

```python
from jobsearch.config import load_settings
from jobsearch.gmail import GmailEmailProvider, build_gmail_service
from jobsearch.job_email_detector import detect_job_email

settings = load_settings()
service = build_gmail_service(
    settings.gmail_credentials_file, settings.gmail_token_file
)
provider = GmailEmailProvider(service, max_results=settings.gmail_max_results)

messages = provider.search(settings.gmail_query)
for message in messages:
    detection = detect_job_email(message)  # JOB_LIKELY / POSSIBLE_JOB / NOT_JOB
    original_body = message.body           # preserved verbatim for the AI phase
```

### Credentials stay outside Git

OAuth client secrets and cached tokens live under `secrets/` (git-ignored,
default paths in `.env.example`). `.env.example` contains configuration
placeholders only — never a key, secret, or token — and a test asserts that
no credential-like values are committed.

## Email → job analysis pipeline (Phase 4B)

Implemented in `src/jobsearch/email_pipeline.py` as
`process_job_email(email, extractor, profile)` → `EmailProcessingResult`.
This phase **connects existing components** — detection (4A), validated
extraction (3c), and the deterministic engines (3a/3b) — without
redesigning, duplicating, or replacing any of them.

```
EmailMessage
    ↓ detect_job_email()                        (Phase 4A, deterministic)
NOT_JOB ─────────────────────────► EmailProcessingResult(NOT_JOB)
    │                                        extractor never called
    ↓ JOB_LIKELY / POSSIBLE_JOB
extract_job_posting(email.body, extractor)      (Phase 3c, validated)
    ├─ invalid output / extractor error ─► EmailProcessingResult(EXTRACTION_FAILED)
    ↓                                        error preserved, engines never ran
JobPosting
    ↓ analyze_job(posting, profile)             (Phases 3a + 3b)
JobAnalysis ────────────────────────► EmailProcessingResult(JOB_ANALYZED)
```

### Outcomes

| Outcome | Meaning | `job_posting` | `analysis` | `error` |
|---------|---------|:---:|:---:|---------|
| `NOT_JOB` | detector found no job signals — extractor never called | `None` | `None` | `""` |
| `JOB_ANALYZED` | extraction validated and both engines ran | `JobPosting` | `JobAnalysis` | `""` |
| `EXTRACTION_FAILED` | invalid extractor output or extractor exception — engines never ran on it | `None` | `None` | preserved message |

### Rules this pipeline enforces

- **Source-agnostic:** the sender, its domain, and `source_hint` are never
  read — every email follows the same path, whatever its origin (asserted
  by tests over several senders plus a source-name scan of the module).
- **Body preservation:** `email.body` reaches the extractor **verbatim** —
  no truncation, rewriting, summarizing, or removal of salary, location,
  or work-authorization sentences — so the Phase 3c evidence rules apply
  unchanged.
- **The AI only extracts:** invalid output and extractor exceptions stop
  before `analyze_job()`, so no score or status is ever computed from bad
  data, no `JobPosting` is fabricated, and errors are preserved in the
  result rather than swallowed.
- **Separation preserved:** the result wraps one `JobAnalysis` with exactly
  `professional_match` (numeric) beside `eligibility` (categorical). The
  orchestration result itself has **no score field** and there is no
  combined score anywhere.
- **Deterministic:** the same email with the same extractor output always
  produces the same result.

### Using the pipeline

```python
from jobsearch.email_pipeline import process_job_email

result = process_job_email(email, extractor, profile)

result.outcome      # NOT_JOB | JOB_ANALYZED | EXTRACTION_FAILED
result.detection    # JobEmailDetection from Phase 4A
result.job_posting  # JobPosting | None — set only when JOB_ANALYZED
result.analysis     # JobAnalysis | None: professional_match + eligibility
result.error        # str — non-empty only for EXTRACTION_FAILED
```

Real Gmail access runs through the Phase 6A manual sync
(`python -m jobsearch.gmail_sync`); the Phase 4B tests and examples above
use fake providers and fake extractors, and no credentials exist in the
repository.

## SQLite persistence and deduplication (Phase 4C)

Implemented in `src/jobsearch/persistence.py` as `JobStore.save(result)`
→ `SavedRecord`. This phase persists **what Phase 4B produced** — it
consumes the `EmailProcessingResult` unchanged and adds no scoring, no
eligibility rules, no extraction changes, and no classification of its
own.

### Database

- Standard-library `sqlite3` only: no ORM, no PostgreSQL, no server, no
  Docker, no external service.
- Location from `DATABASE_PATH` (default `data/jobsearch.db`), which is
  **git-ignored** (`data/*.db`, `data/*.db-*` including journal/WAL
  sidecars). Tests always use in-memory or temporary databases and never
  touch the real file.
- Foreign keys are enforced on every connection; `message_id` is UNIQUE;
  a non-empty canonical application URL has a unique index for efficient
  identity lookup; normalized attributes and the subject/sender evidence
  columns are indexed.

| Table | Key | Contents |
|-------|-----|----------|
| `emails` | `email_id` PK, `message_id` UNIQUE | every inbound email: `thread_id`, `sender`, `received_at`, subject, **body verbatim**, `source_hint`, plus the latest outcome and extraction error for diagnostics |
| `jobs` | `job_id` PK | the normalized `JobPosting` — all existing fields, tuples/lists as JSON — plus `canonical_url`, `attribute_key`, `first_seen_at`, `last_seen_at` (Phase 5 added the `status` workflow column — see the Command Center section) |
| `email_jobs` | (`email_id`, `job_id`) PK | how one email references one job: `relationship` + `matched_by` |
| `job_analyses` | `job_id` PK | the **complete** `ProfessionalMatch` and `EligibilityResult` serialized as JSON — never just the score/status — plus `analyzed_at` |

### Conceptual model: an Email is not a Job

An email is inbound evidence; a job is a normalized employment
opportunity. Several emails — from a board, an alert digest, a recruiter,
a company — may all refer to **one** job:

```
Email A (message_id a) ──┐
Email B (message_id b) ──┼──► 1 Job (job_id) ──► 1 JobAnalysis
Email C (message_id c) ──┘
```

- `message_id` identifies the **Email**: processing the same message
  twice never creates a second row, and an email is never discarded just
  because it points at an existing job.
- `job_id` identifies the **Job**: never the sender, provider,
  `source_hint`, subject, or `message_id`. Source/provider is metadata,
  not identity — the module contains no platform-specific branch, and a
  test scans it for job-board names.
- The stored email body is never rewritten or truncated.

### Job URL vs application URL

`JobPosting.url` is the **application URL** — the destination where the
candidate can actually apply — and it is the only URL used for job
identity today. A source/job URL (the page where the opportunity was
*found*) is conceptually different; a future phase may introduce separate
`job_url` / `source_urls` / `application_urls` fields, so no large URL
architecture is introduced in this phase, and a source URL is never
assumed to be the direct application destination.

### Deduplication rules (deterministic, conservative)

**Level 1 — canonical application URL.** The URL is normalized before
comparison: scheme and host preserved (lowercased), fragment removed,
known tracking parameters removed (`utm_source`, `utm_medium`,
`utm_campaign`, `utm_term`, `utm_content`, `fbclid`, `gclid`), and every
**other** parameter kept — some application URLs require them — then
sorted so equal logical URLs compare equal. Paths are untouched.

```
https://boards.greenhouse.io/acme/jobs/12345?utm_source=linkedin
    → https://boards.greenhouse.io/acme/jobs/12345
```

Two postings with the same canonical URL are the same job: an exact
duplicate.

**Level 2 — normalized job attributes.** Only when the posting has no
usable application URL: `company`, `title`, `location`, and
`remote_mode` are compared case-insensitively with collapsed whitespace.
A full four-field match is an exact duplicate. No fuzzy matching, no
semantics, no AI in the persistence layer.

**Level 3 — email evidence (supporting only).** An already-linked email
with the same normalized subject *and* sender may be recorded as
supporting evidence, but it **never** merges two jobs. It only adds a
possible-duplicate edge while the new job row is still created.

| Dedup status | Evidence | What happens |
|--------------|----------|--------------|
| `NEW` | nothing matched | job created (`first_seen_at` = `last_seen_at` = now); link `CREATED` (`matched_by=new_job`) |
| `EXACT_DUPLICATE` | canonical URL (level 1) or all four normalized attributes (level 2) | existing job reused; link `DUPLICATE_REFERENCE` (`matched_by=application_url` / `job_attributes`) |
| `POSSIBLE_DUPLICATE` | same subject + sender on an earlier email (level 3) | **both jobs kept**; extra edge `POSSIBLE_DUPLICATE_REFERENCE` (`matched_by=email_subject_sender`) to the similar job |

**Data preservation wins:** possible duplicates are never auto-merged, and
when evidence is insufficient to prove two records are the same job, they
remain separate jobs.

### Idempotency and first/last seen

- The same `EmailMessage` processed twice → still one `emails` row, one
  job, one link, one analysis (`message_id` guarantees email-level
  idempotency, enforced by a UNIQUE constraint).
- The same canonical URL arriving from different emails → one job, one
  link per email.
- `first_seen_at` is written once and **never overwritten**;
  `last_seen_at` advances only when a **new** email references the job.
  Reprocessing an existing message is not a new observation. Timestamps
  are timezone-aware UTC.
- The latest analysis of a job replaces the previous row (`analyzed_at`
  updated) so each job keeps one current analysis.

### What gets persisted per outcome

| Phase 4B outcome | Email | Job | Link | Analysis |
|------------------|:-----:|:---:|:----:|:--------:|
| `NOT_JOB` | ✅ | — | — | — |
| `EXTRACTION_FAILED` | ✅ + error for diagnostics | — | — | — |
| `JOB_ANALYZED` | ✅ | ✅ | ✅ | ✅ |

All four writes for a `JOB_ANALYZED` result happen in **one
transaction**: if any write fails, everything rolls back and the error
propagates — no partially persisted job, nothing silently swallowed.

### Using persistence

```python
from jobsearch.email_pipeline import process_job_email
from jobsearch.persistence import JobStore

with JobStore("data/jobsearch.db") as store:        # or JobStore(":memory:")
    result = process_job_email(email, extractor, profile)
    record = store.save(result)                     # one atomic transaction

    record.email_id                                 # the persisted email
    record.job_id                                   # None unless JOB_ANALYZED
    record.dedup_status                             # NEW | EXACT_DUPLICATE | POSSIBLE_DUPLICATE
    record.possible_duplicate_job_id                # level-3 edge, when present

    store.get_email(email.message_id)               # EmailMessage — body verbatim
    store.get_job(record.job_id)                    # JobPosting — every field
    store.get_analysis(record.job_id)               # JobAnalysis — full evidence
```

### What Phase 4C does NOT implement

- **No Gmail OAuth, no scheduler, no cron, no background workers** —
  automatic Gmail scanning and scheduled runs are a **future phase**;
  nothing polls any mailbox today;
- no Notion integration, no job-board API (LinkedIn/Jobright/any
  board), no scraping — and no dashboard UI either; the local MVP
  Command Center arrived in Phase 5;
- no automatic job application and no notifications; the **manual**
  workflow tracker is part of Phase 5;
- no new eligibility rules, no new ProfessionalMatch rules, no changes to
  extraction behavior or the job-email detector — persistence stores and
  relates results; it never decides whether a job is professionally
  suitable or eligible.

## MVP Command Center (Phase 5)

The first functional dashboard: a **local, human-facing view over the
existing SQLite database** that makes the whole system usable today.
Implemented in `src/jobsearch/workflow.py` (status vocabulary and
transition rules), `src/jobsearch/command_center.py` (the eligible-jobs
read model), and `src/jobsearch/dashboard.py` (a standard-library
`http.server` UI), with tests in `tests/test_workflow.py` and
`tests/test_dashboard.py`.

```
SQLite ──► eligible jobs ──► dashboard ──► manual status update ──► SQLite
```

The processing pipeline remains the source of job information and
analysis; the dashboard is a presentation and workflow layer. It reads
what Phases 3a/3b/4B/4C produced and writes back workflow status — it
never re-detects, re-extracts, re-scores, or re-evaluates anything.

### Eligibility filter (primary view)

- The primary table shows **only** `EligibilityStatus = ELIGIBLE` jobs.
- `NOT_ELIGIBLE`, `REVIEW_REQUIRED`, and `UNKNOWN` jobs are **not
  displayed** — but never deleted or altered: filtering is presentation
  behavior only, and every record stays in SQLite.
- The `All` filter still applies this rule; no view exposes the
  excluded statuses.
- Eligibility itself is unchanged — Phase 3a remains the only authority.

### Professional match: informational percentage

- The existing `ProfessionalMatch.score` is shown as a percentage
  (`0.87 → 87%`).
- No new scoring system: the Phase 3b calculation is untouched.
- The score is never used to rank, tier, or exclude jobs — no "best
  jobs", no top lists, no priority labels. Rows appear in job-id order;
  the percentage only helps direct the user's attention.

### Eligibility vs workflow status (separate concepts)

| | Question | Comes from | Representation |
|---|---|---|---|
| Eligibility | Can this candidate reasonably apply? | Phase 3a rules | categorical status |
| Professional match | How well does the profile fit? | Phase 3b formula | score 0.0–1.0, shown as % |
| Workflow status | What is the state of this opportunity in my job search? | **manual, human decisions** | `status` column on the job |

Example: `ELIGIBLE` + `87%` + `FOUND` — eligible, a strong professional
fit, and not yet manually reviewed. The three never influence one
another: eligibility and score never set or advance the workflow status,
and the workflow status never changes eligibility or score.

### Workflow statuses

| Status | Meaning | How it is reached |
|--------|---------|-------------------|
| `FOUND` | The system discovered and analyzed the job; not yet manually reviewed | automatic — initial status of every `JOB_ANALYZED` job, and of every pre-Phase-5 row after migration |
| `REVIEWED` | "I have reviewed this opportunity" — no apply decision yet | manual: `FOUND → REVIEWED` |
| `SAVED` | Worth keeping as an application candidate; not yet applied | manual: `REVIEWED → SAVED` |
| `APPLIED` | The application was actually submitted | manual: `SAVED → APPLIED` |
| `INTERVIEW` | The company invited the candidate to an interview | manual: `APPLIED → INTERVIEW` |
| `OFFER` | An offer was received | manual: `INTERVIEW → OFFER` |
| `REJECTED` | The application/opportunity was rejected — terminal for this MVP | manual |
| `CLOSED` | No longer active (position closed, expired, withdrawn, no longer relevant) — terminal | manual |

### Allowed transitions

```
FOUND → REVIEWED → SAVED → APPLIED → INTERVIEW → OFFER
```

Each step is manual. Terminal moves (all manual):

| From | Allowed next statuses |
|------|----------------------|
| `FOUND` | `REVIEWED` |
| `REVIEWED` | `SAVED`, `CLOSED` |
| `SAVED` | `APPLIED`, `CLOSED` |
| `APPLIED` | `INTERVIEW`, `REJECTED`, `CLOSED` |
| `INTERVIEW` | `OFFER`, `REJECTED`, `CLOSED` |
| `OFFER` | `CLOSED` |
| `REJECTED` | *(terminal — no transitions)* |
| `CLOSED` | *(terminal — no transitions)* |

Validation is one small deterministic function
(`jobsearch.workflow.can_transition()`), enforced on every write:
anything outside this table — skipping steps, going backwards, reopening
terminal states, or a no-op re-submit — is rejected and the stored
status stays untouched. No state-machine framework exists.

### Manual workflow only

The dashboard is where **the user** advances the opportunity. Nothing
infers whether a job was reviewed, saved, applied to, interviewed for,
or offered — not eligibility, not the match score, not the presence of an
application URL. An `application_url` only means an application
destination exists: *URL present + status = `FOUND`* is a completely
valid state.

### The dashboard

The primary table: **ID · Position · Company · Location · Match ·
Status**, plus an action form offering only the currently allowed next
statuses:

| ID | Position | Company | Match | Status |
|----|----------|---------|-------|--------|
| JOB-123 | QA Automation Engineer | Acme | 87% | FOUND |
| JOB-124 | QA Engineer | Example | 81% | REVIEWED |
| JOB-125 | Software Tester | Company X | 74% | SAVED |
| JOB-126 | QA Analyst | Company Y | 69% | APPLIED |

- **Filters:** `All`, `Found`, `Reviewed`, `Saved`, `Applied`,
  `Interview`, `Offer`, `Rejected`, `Closed` — all within the eligible
  set.
- **Status actions:** change the status from the table or the detail
  view; each change is validated against the transition table and
  persisted to SQLite, so refreshing the dashboard preserves it.
- **Job-detail view:** Job ID, position, company, location, remote mode,
  professional match, eligibility with its reasons, workflow status,
  application URL (when present), first/last seen, and compact match
  evidence (matched and missing skills).
- The visible job ID is the Phase 4C `job_id` (`JOB-123`); email ids are
  never used as the job identity.

### SQLite is the source of truth

- The `jobs` table gained one column:
  `status TEXT NOT NULL DEFAULT 'FOUND'` — the entire schema change.
- Databases created before Phase 5 are **migrated in place** when first
  opened (the column is added with the `FOUND` default): no record is
  lost, and the database is never recreated.
- Statuses live only in SQLite: refreshing, restarting, or reading from
  any other code path sees the same persisted state — nothing is kept in
  memory.

### UI technology and how to run it

Chosen because the repository needs no frontend stack: the Python
standard library (`http.server` with minimal inline HTML/CSS) — no web
framework, no CSS framework, no build step, no new dependency, no
authentication, no deployment. It binds to loopback
(`127.0.0.1:8000` by default; override with `DASHBOARD_HOST` /
`DASHBOARD_PORT`):

```bash
PYTHONPATH=src .venv/bin/python -m jobsearch.dashboard
# → JobSearch AI Command Center → http://127.0.0.1:8000
```

Extracted content (titles, companies, URLs) is HTML-escaped before
rendering, and application URLs render only for `http(s)://` targets.

### What Phase 5 does NOT implement

- **No Notion integration** — the Command Center operates directly
  against SQLite; a future phase may synchronize SQLite with Notion or
  another presentation layer (no Notion credentials or dependencies
  exist);
- **no scheduler, cron, background worker, or automatic Gmail scanning**
  — the dashboard shows what is already persisted; automatic discovery
  is a future phase (the read-only Gmail provider architecture is
  unchanged);
- no ranking, tiers, "best jobs", or any new score; no automatic or
  inferred status changes; no deletion or cleanup of excluded jobs;
- no authentication, user accounts, cloud database, Docker, or
  deployment infrastructure — a local MVP, not a production service.

## Phase 6A — Manual Gmail Sync

Makes the existing pipeline operational against a **real Gmail account**,
with one command that you run yourself — nothing runs in the background.

```
Gmail  →  EmailMessage  →  detect_job_email  →  AI extraction  →  analysis  →  SQLite  →  Command Center
   GMAIL_QUERY /              Phase 4A          Phase 6A          Phase 3a/3b   Phase 4C     Phase 5
   GMAIL_MAX_RESULTS          (NOT_JOB stops)   (Phase 3c schema)               (dedup,      (same DB:
   gmail.readonly                                                     status FOUND) eligible only)
```

### 1. What the sync does

`python -m jobsearch.gmail_sync` performs one pass: search with
`GMAIL_QUERY` (respecting `GMAIL_MAX_RESULTS`), fetch each message,
normalize it into `EmailMessage` (plain text preferred, HTML preserved
verbatim), run the existing Phase 4B `process_job_email()` — detector
first, then AI extraction and analysis for `JOB_LIKELY` /
`POSSIBLE_JOB` — and persist every result with `JobStore.save()`. Emails
already analyzed are skipped before extraction, so re-running is free of
duplicate records and repeat AI calls. One problematic email is reported
and skipped; it never ends the run.

### 2. Gmail access is read-only

The only scope requested is
`https://www.googleapis.com/auth/gmail.readonly`, and the only calls made
are `users.messages.list` and `users.messages.get`. The sync never sends,
deletes, modifies, marks as read, archives, moves messages, or changes
labels. The mailbox provider is never authoritative for classification:
`GMAIL_QUERY` only selects which messages are fetched — the content-based
Phase 4A detector decides what looks like a job email.

### 3. Required Google OAuth credentials

- In the Google Cloud console: enable the **Gmail API** for your project,
  create an **OAuth client ID of type "Desktop app"**, and download the
  JSON file.
- Place it at `secrets/gmail_credentials.json` (the default path; the
  whole `secrets/` directory is git-ignored) or point
  `GMAIL_CREDENTIALS_FILE` at another location outside Git.
- On the first run, the standard consent screen opens in your browser;
  the cached token is written to `secrets/gmail_token.json` (also
  git-ignored). You never type your password into this application, and
  no credential is ever printed, logged, or committed.

### 4. Configuration variables

| Variable | Purpose |
|----------|---------|
| `GMAIL_QUERY` | Which messages to fetch (any Gmail syntax) |
| `GMAIL_CREDENTIALS_FILE` | OAuth client JSON path (git-ignored) |
| `GMAIL_TOKEN_FILE` | Cached OAuth token path (git-ignored) |
| `GMAIL_MAX_RESULTS` | Maximum messages per search |
| `AI_API_KEY` | Key for the AI endpoint (set in `.env` only; never committed) |
| `AI_BASE_URL` / `AI_MODEL` | OpenAI-compatible endpoint and model used for extraction |
| `AI_TIMEOUT_SECONDS` | Per-request AI timeout |
| `CAREER_PROFILE_PATH` | Career profile analyzed during the sync |
| `DATABASE_PATH` | SQLite file the sync writes and the dashboard reads |

See [Configuration](#configuration) for defaults; `.env.example` documents
each one with safe placeholders.

### 5. How to run the manual sync

```bash
# one-time setup: install the Google client libraries listed in requirements.txt
pip install -r requirements.txt

PYTHONPATH=src .venv/bin/python -m jobsearch.gmail_sync
```

Exit codes: `0` the sync completed (any per-message failures are listed in
the printed summary), `2` a configuration/OAuth error (the message says
exactly what to fix — credentials, `AI_API_KEY`, profile, or missing
libraries), `1` an operational failure.

### 6. What the sync summary means

| Summary line | Meaning |
|--------------|---------|
| `Messages found` | Messages returned for `GMAIL_QUERY` |
| `Messages processed` | Run through the pipeline this time |
| `Already synced` | Previously analyzed — skipped, no AI call |
| `NOT_JOB` | Detector found no job signals; the extractor never ran |
| `JOB_LIKELY` / `POSSIBLE_JOB` | Detector buckets (Phase 4A) |
| `Jobs analyzed` | Extraction **and** analysis succeeded |
| `New jobs created` | New job rows (workflow status `FOUND`) |
| `Duplicate jobs` | Posting matched an existing job (Phase 4C dedup) |
| `Extraction failures` | AI extraction failed for those messages — listed under `Errors`, the run continued |
| `Persistence failures` / `Processing failures` | Per-message errors, listed under `Errors`, never hidden |

If no messages match, the summary says so explicitly
(`No messages matched GMAIL_QUERY`) so you can widen the query.

### 7. Where the resulting jobs appear

In the same SQLite database the Phase 5 Command Center already reads:
refresh `http://127.0.0.1:8000` and eligible jobs appear in the main
table (`ELIGIBLE` only — `NOT_ELIGIBLE`, `REVIEW_REQUIRED`, and `UNKNOWN`
are persisted but stay hidden from the dashboard). New jobs start at
workflow status `FOUND` and change only through manual transitions; an
application URL never implies `APPLIED`.

### 8. Scheduling is NOT implemented

There is no cron, systemd service, background worker, polling loop, or
GitHub Action. The sync happens only while the command runs. Automatic
scheduling remains a future phase.

### 9. Notion integration is NOT implemented

Nothing is synchronized to Notion; the sync's destination is the local
SQLite database only. Notion remains a separate future phase (5B).

### 10. Validate Gmail OAuth before configuring the AI key

`src/jobsearch/gmail_check.py` is a small development utility — not part
of the production sync, which is unchanged — for validating real OAuth
authorization and read-only Gmail connectivity **without** `AI_API_KEY`:

```bash
PYTHONPATH=src .venv/bin/python -m jobsearch.gmail_check
```

It reuses the existing `build_gmail_service()`: on the first run a browser
opens for consent and `secrets/gmail_token.json` is created (later runs
reuse the token). Then it issues exactly one read-only request,
`users.messages.list(userId="me", maxResults=1)`, and prints only safe
information: authentication status, API status, and the reference count.

It never fetches message bodies, never runs AI extraction, never opens the
database, and requires no `AI_API_KEY`. Exit codes: `0` verified, `2`
configuration/OAuth problem, `1` API or unexpected failure. Offline
coverage lives in `tests/test_gmail_check.py` (mocked service and OAuth
flow).

### 11. Validate the AI extractor in isolation (for example Gemini)

`src/jobsearch/ai_check.py` is a small development utility — also not
part of the production sync — that exercises **only** the existing
`AIExtractor`: it builds it from the provider-agnostic `AI_API_KEY`,
`AI_BASE_URL`, `AI_MODEL` and `AI_TIMEOUT_SECONDS` settings and asks it
to extract fields from one hardcoded sample job description. No Gmail
call, no database, nothing persisted:

```bash
PYTHONPATH=src .venv/bin/python -m jobsearch.ai_check
```

The provider is chosen purely by configuration, so validating Gemini
needs no code change and no SDK: point the check at Gemini's
OpenAI-compatible endpoint. Keep the key in `.env` (never on the command
line, where it would land in your shell history) and override only the
two non-secret values:

```bash
# .env must contain:  AI_API_KEY=<your Gemini API key>
AI_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai \
AI_MODEL=gemini-3.8-flash \
PYTHONPATH=src .venv/bin/python -m jobsearch.ai_check
```

Exit codes: `0` extraction succeeded, `2` missing/invalid AI
configuration, `1` endpoint or response failure. The report never
contains the API key. Offline coverage lives in `tests/test_ai_check.py`
(fake transport) plus the provider-envelope test in
`tests/test_ai_extractor.py`.

### What Phase 6A does NOT implement

No scheduler, no Notion, no application entity, no dashboard changes, no
notifications, no email sending, no ranking, no new scoring or eligibility
rules, no Docker, no cloud deployment, and no Command Center
authentication. No real Gmail account has been connected from this
repository yet — the orchestration is fully verified by the offline test
suite (fake Gmail service, scripted extractor); the OAuth consent step
happens on your machine when you run it for the first time.

## Future phases

| Phase | Deliverable | Status |
|-------|-------------|--------|
| 1 | Project foundation: layout, configuration, tests, docs | Done |
| 2 | Career profile data model and manual job posting ingestion | Done |
| 3a | Eligibility engine | Done |
| 3b | Professional match engine | Done |
| 3c | AI extraction | Done |
| 4A | Generic Gmail/email intake | Done |
| 4B | Email → extraction → job analysis | Done |
| 4C | Persistence + deduplication | Done |
| 5 | MVP Command Center: local dashboard over SQLite + manual workflow statuses | Done |
| 5B | Notion Command Center: synchronize SQLite with Notion | Planned |
| 6A | Real Gmail intake: manual, read-only sync | Done (current) |
| 6 | GitHub Actions / scheduling | Planned |
| 7 | Dashboard / GitHub Pages | Planned |

Each phase starts only after the previous one is stable.

## Getting started

Everything below runs locally: no container, no database server, and no
cloud service. The test suite needs nothing beyond `pytest`.

### Requirements

- Python 3.12+ (developed and tested on 3.12.3)

### Installation and setup

```bash
cd JobSearch
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
```

`requirements.txt` lists the optional Google client libraries used only by
the real Gmail sync (`jobsearch.gmail.build_gmail_service` imports them
lazily); they are enabled there. AI extraction needs **no package at all**
— it is standard-library HTTP. `requirements-dev.txt` holds `pytest`, the
only development dependency.

### Python virtual environment

All commands assume the environment created above. The package is **not
installed into it** (the `src/` layout is used directly), which means:

- `pytest` works out of the box — `pyproject.toml` puts `src/` on the
  test path;
- every manual command must be prefixed with `PYTHONPATH=src`, for
  example `PYTHONPATH=src .venv/bin/python -m jobsearch.dashboard`.

### Configure `.env`

```bash
cp .env.example .env   # then edit values as needed
```

`.env` is git-ignored and holds local values and secrets only. Variables
already present in the real environment always take precedence over
values in `.env`. See [Configuration](#configuration) for the complete
variable table.

### Configure Gemini (OpenAI-compatible endpoint)

AI extraction calls any OpenAI-compatible chat-completions endpoint over
standard-library HTTP — no SDK and no vendor lock-in. Gemini exposes such
an endpoint, so it is configured, never coded:

```bash
# in .env  (never committed)
# .env does NOT support inline comments — keep comments on their own line
#
# Get a key at https://aistudio.google.com/apikey
AI_API_KEY=PASTE_YOUR_GEMINI_API_KEY
AI_MODEL=gemini-3.8-flash
# raise the timeout if the model thinks for a while (1-600 seconds)
AI_TIMEOUT_SECONDS=60
```

```bash
AI_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai \
PYTHONPATH=src .venv/bin/python -m jobsearch.ai_check
```

- Keep `AI_API_KEY` **in `.env` only**: never pass it on the command
  line, where it would land in your shell history.
- Inline variables override `.env`, so `AI_BASE_URL` can stay at its
  default locally and be pointed at Gemini for one run, as above.
- Validate the connection with the isolated check before running a sync
  (see *Phase 6A, section 11* for details and failure modes).

### Set up Gmail OAuth (read-only)

One-time setup in the Google Cloud console:

1. Create a project and **enable the Gmail API**.
2. Configure the **OAuth consent screen** and add your Google account as
   a **test user** while the app is in *Testing*.
3. Create an **OAuth client ID of type "Desktop app"** and download the
   JSON file.
4. Save it as `secrets/gmail_credentials.json` (the default path, kept
   outside Git) or point `GMAIL_CREDENTIALS_FILE` at it.

The only scope requested is
`https://www.googleapis.com/auth/gmail.readonly`: nothing is ever sent,
deleted, modified, or marked as read. The first command that needs it
opens your browser for consent and writes the cached token to
`secrets/gmail_token.json` (also git-ignored). Step-by-step detail in
*Phase 6A, section 3*.

### Run the tests

```bash
pytest                        # with the virtual environment activated
.venv/bin/python -m pytest    # equivalent, without activating
```

The suite is fully offline: Gmail, OAuth, and the AI endpoint are always
mocked, and tests use in-memory or temporary databases. No API key,
credential, or network access is ever required.

### Run the isolated AI check

Exercises **only** the AI extractor against the configured endpoint with
one hardcoded sample job description: no Gmail call, no database, nothing
persisted.

```bash
PYTHONPATH=src .venv/bin/python -m jobsearch.ai_check
# 0 = extracted · 2 = missing/invalid AI configuration · 1 = endpoint failure
```

### Run the Gmail OAuth connectivity check

Validates real OAuth authorization and read-only Gmail connectivity
**without** `AI_API_KEY`: on the first run the browser opens for
consent, then exactly one `users.messages.list(userId="me", maxResults=1)`
request is issued. No message bodies are read.

```bash
PYTHONPATH=src .venv/bin/python -m jobsearch.gmail_check
# 0 = verified · 2 = configuration/OAuth problem · 1 = API failure
```

### Run the Gmail sync

Pulls the messages matching `GMAIL_QUERY` **once, on demand**, through
the whole pipeline (detect → extract → analyze → persist, workflow
status `FOUND`):

```bash
PYTHONPATH=src .venv/bin/python -m jobsearch.gmail_sync
# 0 = completed · 2 = configuration/OAuth error · 1 = operational failure
```

Nothing runs in the background: there is no scheduler, cron, worker, or
polling loop. Re-running it is idempotent (already-analyzed messages are
skipped before extraction). See *Phase 6A, section 5*.

### Run the dashboard

```bash
PYTHONPATH=src .venv/bin/python -m jobsearch.dashboard
# → JobSearch AI Command Center → http://127.0.0.1:8000
```

Binds to loopback only; override with `DASHBOARD_HOST` / `DASHBOARD_PORT`.
Stop it with `Ctrl+C`. See *MVP Command Center (Phase 5)*.

### Security notes

| Asset | Location | Protection |
|-------|----------|------------|
| `.env` (API key, paths) | repository root | git-ignored (`.env`, `.env.*`); never commit it and never pass secrets on the command line; `AI_API_KEY` is excluded from `repr()` output |
| Gmail OAuth client JSON | `secrets/gmail_credentials.json` | the whole `secrets/` directory is git-ignored; its contents are never printed or logged |
| Gmail OAuth token | `secrets/gmail_token.json` | git-ignored (also `*.token.json`); delete it to revoke and sign in again; issued for the read-only scope only |
| SQLite database | `data/jobsearch.db` | `data/*.db` and `data/*.db-*` are git-ignored; the test suite never touches it (in-memory or temporary databases instead) |

- `.env.example` contains placeholders only, and the test suite scans it
  for credential-like markers.
- No credential, key, or token is ever printed, logged, or committed:
  error messages name configuration variables, never values, and the
  `Settings` representation hides `AI_API_KEY`.
- The dashboard is a local MVP — loopback bind, no authentication, no
  deployment infrastructure.

## Manual job ingestion

Add postings manually to `data/jobs.json` as a JSON array of objects:

```json
[
  {
    "title": "Backend Engineer",
    "company": "ACME",
    "location": "Remote - LATAM",
    "description": "Full text of the posting goes here.",
    "url": "https://example.com/jobs/123",
    "source": "manual",
    "required_skills": ["Python", "PostgreSQL"],
    "preferred_skills": ["Kubernetes"],
    "min_experience_years": 5,
    "technologies": ["AWS"],
    "education": ["Bachelor's degree"],
    "certifications": ["AWS Solutions Architect"],
    "languages": ["English"],
    "remote_mode": "remote",
    "salary": "90000",
    "salary_currency": "USD",
    "salary_period": "year",
    "deadline": "2026-11-30"
  }
]
```

- Required fields: `title`, `company`, `location`, `description`.
- Optional metadata: `url` (default `""`), `source` (default `"manual"`).
- Optional professional requirements (Phase 3b): `required_skills`,
  `preferred_skills`, `technologies`, `education`, `certifications`,
  `languages` (arrays of non-empty strings) and `min_experience_years`
  (non-negative number). Omitting one means the posting does not state that
  requirement — see the scoring rules above.
- Optional extracted metadata (Phase 3c): `remote_mode`, `salary`,
  `salary_currency`, `salary_period`, `deadline` (strings, default `""`).
- Invalid or incomplete entries raise `ValueError` with a clear message.

Load them in Python:

```python
from jobsearch.ingestion import load_career_profile, load_jobs

profile = load_career_profile("data/career_profile.json")
jobs = load_jobs("data/jobs.json")
```

## Configuration

Settings are read from environment variables at runtime; nothing is
hard-coded. Copy `.env.example` to `.env` for local development (`.env` is
git-ignored and must never be committed). Variables already present in the
environment always take precedence over values in `.env`.

| Variable | Default | Description |
|----------|---------|----------------------------------------------------------|
| `APP_ENV` | `development` | Runtime environment label: `development`/`staging`/`production` |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL` |
| `GMAIL_QUERY` | `newer_than:7d` | Gmail search query used by the manual sync `python -m jobsearch.gmail_sync` (any Gmail syntax) |
| `GMAIL_CREDENTIALS_FILE` | `secrets/gmail_credentials.json` | OAuth client secrets path — outside Git |
| `GMAIL_TOKEN_FILE` | `secrets/gmail_token.json` | Cached OAuth token path — outside Git |
| `GMAIL_MAX_RESULTS` | `50` | Maximum messages returned per search |
| `AI_API_KEY` | *(empty)* | Key for the AI extraction endpoint — set in `.env` only, never committed; excluded from `repr()` output |
| `AI_BASE_URL` | `https://api.openai.com/v1` | Any OpenAI-compatible chat-completions base URL (standard-library HTTP, no SDK) |
| `AI_MODEL` | `gpt-4o-mini` | Model used for job-field extraction |
| `AI_TIMEOUT_SECONDS` | `60` | Per-request AI timeout in seconds (1–600) |
| `CAREER_PROFILE_PATH` | `data/career_profile.json` | Career profile analyzed during the manual sync |
| `DATABASE_PATH` | `data/jobsearch.db` | SQLite database file (Phase 4C) — git-ignored; tests use in-memory/temporary databases instead |
| `DASHBOARD_HOST` | `127.0.0.1` | Bind address for the local Command Center (Phase 5) — keep it on loopback |
| `DASHBOARD_PORT` | `8000` | Port for the local Command Center (Phase 5) |

Credentials (the Gmail OAuth files today, other providers later) live
outside Git: `.env` and the `secrets/` directory are git-ignored, and
`.env.example` holds placeholders only. The test suite never reads them.
