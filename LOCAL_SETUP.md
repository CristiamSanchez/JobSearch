# JobSearch Local Setup

A practical, step-by-step guide to run JobSearch on your own machine.
Everything here runs **locally**: no cloud deployment, no Docker, no
scheduler, no external services.

Commands assume you are in the repository root. Every manual command is
prefixed with `PYTHONPATH=src`, because the package is used directly from
`src/` and is not installed into the virtual environment.

---

## Requirements

| Requirement | Why |
|-------------|-----|
| Python 3.12+ | the project targets `>=3.12` (`pyproject.toml`) |
| Git | to clone the repository |
| A Gmail account | the read-only sync reads job emails from it |
| Google Cloud OAuth credentials (Gmail API, *Desktop app* client) | local OAuth consent; stored outside Git |
| An API key for an OpenAI-compatible AI endpoint | job-field extraction (OpenAI, Gemini via its OpenAI-compatible endpoint, …) |
| `pip` + `venv` | virtual environment and dependency installation |

Nothing else is required: the application core is Python standard library
(`http.server`, `sqlite3`, `urllib`, `json`, `email`).

---

## 1. Clone repository

```bash
git clone https://github.com/CristiamSanchez/JobSearch.git
cd JobSearch
```

---

## 2. Create Python virtual environment

```bash
python3.12 -m venv .venv
source .venv/bin/activate
```

On Windows (PowerShell): `py -3.12 -m venv .venv` then
`.\.venv\Scripts\Activate.ps1`.

---

## 3. Install dependencies

```bash
pip install -r requirements.txt -r requirements-dev.txt
```

- `requirements.txt` — the **optional** Google client libraries used only by
  the real Gmail sync (`google-api-python-client`, `google-auth`,
  `google-auth-oauthlib`). They are imported lazily, so nothing else needs
  them. AI extraction needs **no package at all** (standard-library HTTP).
- `requirements-dev.txt` — `pytest`, the only development dependency, used
  by the test suite (see **Testing** in [README.md](README.md)).

The test suite itself runs with nothing but `pytest`: Gmail, OAuth and the
AI endpoint are always mocked.

---

## 4. Create `.env`

```bash
cp .env.example .env
```

`.env` is git-ignored and holds your local values only. Keep comments on
their own line (no inline comments). Real environment variables always
override `.env`.

| Variable | Meaning |
|----------|---------|
| `APP_ENV` | Runtime label: `development` / `staging` / `production` |
| `LOG_LEVEL` | `DEBUG` / `INFO` / `WARNING` / `ERROR` / `CRITICAL` |
| `GMAIL_QUERY` | Gmail search syntax that selects which messages are fetched (default `newer_than:7d`) |
| `GMAIL_CREDENTIALS_FILE` | Path to the OAuth client JSON (default `secrets/gmail_credentials.json`, git-ignored) |
| `GMAIL_TOKEN_FILE` | Path to the cached OAuth token (default `secrets/gmail_token.json`, git-ignored) |
| `GMAIL_MAX_RESULTS` | Maximum messages returned per search (default `50`) |
| `AI_API_KEY` | API key for the AI endpoint — **leave empty in `.env.example` and set it only here, never commit it** |
| `AI_BASE_URL` | OpenAI-compatible chat-completions base URL (default `https://api.openai.com/v1`) |
| `AI_MODEL` | Model used for extraction (default `gpt-4o-mini`) — must match the provider of `AI_BASE_URL` |
| `AI_TIMEOUT_SECONDS` | Per-request AI timeout in seconds, 1–600 (default `60`) |
| `CAREER_PROFILE_PATH` | Career profile analyzed during the sync (default `data/career_profile.json`) |
| `DATABASE_PATH` | SQLite database file (default `data/jobsearch.db`, git-ignored) |
| `DASHBOARD_HOST` / `DASHBOARD_PORT` | Dashboard bind address and port (defaults `127.0.0.1` / `8000`) |

---

## 5. Create local career profile

```bash
cp data/career_profile.example.json data/career_profile.json
```

- `data/career_profile.example.json` is the **committed, sanitized example**
  (generic professional facts, no personal data).
- `data/career_profile.json` is your **local, real profile**. It is
  **intentionally ignored by Git** (`.gitignore` rule
  `data/career_profile.json`) and **must never be committed**.
- Edit it with your own skills, years of experience, technologies,
  education, certifications and languages — the professional match engine
  reads it through `CAREER_PROFILE_PATH`.
- The eligibility facts (country, region, work-authorization flags) live in
  the same file and drive the eligibility engine.

---

## 6. Configure Google Gmail OAuth

One-time setup, entirely local:

1. In the Google Cloud console: create (or pick) a project and **enable the
   Gmail API**.
2. Configure the **OAuth consent screen** and add your Google account as a
   **test user** while the app is in *Testing*.
3. Create an **OAuth client ID of type “Desktop app”** and download the
   JSON file.
4. Store it **outside Git** at the default path
   `secrets/gmail_credentials.json` (the whole `secrets/` directory is
   git-ignored), or place it anywhere local and point
   `GMAIL_CREDENTIALS_FILE` at it.

How authorization behaves at runtime:

- The **first execution** that needs Gmail (`gmail_check`, `gmail_sync`)
  opens your **local browser** for the standard consent screen
  (`InstalledAppFlow.run_local_server`) and writes the cached token to
  `secrets/gmail_token.json` (also git-ignored).
- **Subsequent executions reuse that token** and refresh it automatically
  when it has expired — no browser needed afterwards.
- The only scope requested is
  `https://www.googleapis.com/auth/gmail.readonly`. The application only
  ever calls `users.messages.list` and `users.messages.get`: it never
  sends, deletes, modifies, marks as read, archives or relabels anything.
- You never type your password into the application, and no credential,
  token or key is ever printed, logged or committed.

---

## 7. Configure AI

Set these in `.env` (never on the command line, where they would land in
your shell history):

```dotenv
AI_API_KEY=PASTE_YOUR_OWN_KEY_HERE
AI_BASE_URL=https://api.openai.com/v1
AI_MODEL=gpt-4o-mini
AI_TIMEOUT_SECONDS=60
```

Using **Gemini** through its OpenAI-compatible endpoint needs no SDK and no
code change — just keep both values consistent:

```dotenv
AI_API_KEY=PASTE_YOUR_OWN_KEY_HERE
AI_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai
AI_MODEL=gemini-3.8-flash
```

Notes:

- `AI_API_KEY` stays empty in `.env.example` and lives only in your local
  `.env` (git-ignored). **Never commit a real key.**
- `AI_BASE_URL` and `AI_MODEL` must come from the **same provider**.
- Raise `AI_TIMEOUT_SECONDS` (up to `600`) if the model is slow.

---

## 8. Validate Gmail

```bash
PYTHONPATH=src .venv/bin/python -m jobsearch.gmail_check
```

Read-only connectivity check: first run opens the browser consent, then
issues exactly one `users.messages.list(userId="me", maxResults=1)` request.
It reads no message bodies and needs **no** `AI_API_KEY`.

Exit codes: `0` verified · `2` configuration/OAuth problem · `1` API failure.

---

## 9. Validate AI

```bash
PYTHONPATH=src .venv/bin/python -m jobsearch.ai_check
```

Exercises only the AI extractor against the configured endpoint with one
hardcoded sample job description: no Gmail call, no database, nothing
persisted, and the report never contains your key.

Exit codes: `0` extraction succeeded · `2` missing/invalid AI configuration
· `1` endpoint or response failure.

To check a Gemini configuration for a single run (values still come from
`.env`, only the two non-secret ones are overridden):

```bash
AI_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai \
AI_MODEL=gemini-3.8-flash \
PYTHONPATH=src .venv/bin/python -m jobsearch.ai_check
```

---

## 10. Run Gmail synchronization

```bash
PYTHONPATH=src .venv/bin/python -m jobsearch.gmail_sync
```

One manual pass: search with `GMAIL_QUERY`, fetch each message, run the
pipeline (detector → AI extraction → eligibility + professional match) and
persist everything to SQLite. Nothing runs in the background — re-run it
whenever you want. It is idempotent: already-analyzed messages are skipped
before extraction, so no duplicates and no repeat AI calls.

- **`GMAIL_QUERY`** — plain Gmail search syntax selecting which messages
  are fetched (default `newer_than:7d`). The query only *selects*
  messages; the deterministic detector decides which ones look like jobs.
- **`GMAIL_MAX_RESULTS`** — maximum messages per search (default `50`).

For your **first real run**, limit it to a single message (an inline
override for that one command — do not hardcode it in the application):

```bash
GMAIL_MAX_RESULTS=1 PYTHONPATH=src .venv/bin/python -m jobsearch.gmail_sync
```

Exit codes: `0` completed (per-message failures are listed in the summary)
· `2` configuration/OAuth error (credentials, `AI_API_KEY`, career profile,
missing libraries) · `1` operational failure. If nothing matches the query
the summary prints `No messages matched GMAIL_QUERY`.

---

## 11. Run dashboard

```bash
PYTHONPATH=src .venv/bin/python -m jobsearch.dashboard
# → JobSearch AI Command Center → http://127.0.0.1:8000
```

Binds to loopback only (`127.0.0.1:8000`); override with `DASHBOARD_HOST`
/ `DASHBOARD_PORT`. Stop it with `Ctrl+C` — it is a foreground local
server, nothing runs in the background.

---

## 12. Open dashboard

<http://127.0.0.1:8000>

- `http://127.0.0.1:8000/` — job list (eligible jobs only)
- `http://127.0.0.1:8000/?status=FOUND` — filtered by workflow status
- `http://127.0.0.1:8000/job/1` — job detail page (requires a synced
  database with at least one eligible job)

---

## 13. Troubleshooting

Covering only problems the application reports itself:

| Symptom | What it means | Fix |
|---------|---------------|-----|
| `Configuration error: Google OAuth client credentials not found: …` (exit `2`) | the OAuth client JSON is missing at `GMAIL_CREDENTIALS_FILE` | download a **Desktop app** OAuth client JSON and place it at `secrets/gmail_credentials.json`, or point `GMAIL_CREDENTIALS_FILE` at its location |
| Browser consent never appears / consent is rejected (exit `2`) | no usable token yet, or the account is not a **test user** on the OAuth consent screen | add your account as a test user in the Google Cloud console; if the token is invalid or revoked, delete `secrets/gmail_token.json` and run `gmail_check` again to re-authorize |
| `AI_API_KEY is not set …` (exit `2`) | the AI endpoint has no key | set `AI_API_KEY` in your local `.env` |
| `CAREER_PROFILE_PATH …` profile error (exit `2`) | the career profile file does not exist or has invalid fields | `cp data/career_profile.example.json data/career_profile.json` and keep valid JSON |
| AI check/sync fails with an endpoint or response error (exit `1`) | wrong base URL/model pair, bad key, or timeout | verify `AI_BASE_URL` and `AI_MODEL` come from the **same provider**, check the key, and raise `AI_TIMEOUT_SECONDS` if needed |
| `No messages matched GMAIL_QUERY` | the query selected nothing | widen `GMAIL_QUERY` (for example `newer_than:30d`) or check the mailbox |
| Professional match shows `0.0` / `0%` | the posting states no professional requirements, or your profile has no data for the dimensions the posting states | fill your local `data/career_profile.json` (skills, technologies, experience, …); a posting with no stated requirements always scores `0.0` by design |
| The dashboard does not start (port already in use) | another process holds port `8000` | stop the other process (`Ctrl+C` in its terminal) or start on another port: `DASHBOARD_PORT=8001 PYTHONPATH=src .venv/bin/python -m jobsearch.dashboard` |
| Dashboard page is empty (`No eligible jobs yet.`) | jobs exist but none is `ELIGIBLE`, or nothing has been synced yet | run the Gmail sync (step 10); non-eligible jobs stay in SQLite, they are simply not listed |

---

Back to [README.md](README.md).
