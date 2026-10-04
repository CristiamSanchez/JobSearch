"""Configuration for JobSearch AI.

Settings are read from environment variables so that no secrets are ever
hard-coded or committed. For local development a ``.env`` file (see
``.env.example``) can be loaded; variables already present in the real
environment always take precedence over values from that file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ENV_FILE_NAME = ".env"

VALID_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

DEFAULT_ENVIRONMENT = "development"
DEFAULT_LOG_LEVEL = "INFO"

# Gmail settings (Phase 4A). The query is plain Gmail search syntax and can
# be widened or narrowed per environment without touching Python code.
DEFAULT_GMAIL_QUERY = "newer_than:7d"
DEFAULT_GMAIL_CREDENTIALS_FILE = "secrets/gmail_credentials.json"
DEFAULT_GMAIL_TOKEN_FILE = "secrets/gmail_token.json"
DEFAULT_GMAIL_MAX_RESULTS = 50

# AI extraction (Phase 6A). The manual Gmail sync calls any
# OpenAI-compatible chat-completions endpoint over standard-library HTTP —
# no SDK. The API key is a secret: read from the environment only, never
# hard-coded, never logged, and hidden from ``repr(Settings())``.
DEFAULT_AI_BASE_URL = "https://api.openai.com/v1"
DEFAULT_AI_MODEL = "gpt-4o-mini"
DEFAULT_AI_TIMEOUT_SECONDS = 60

# Career profile analyzed against during the sync (Phase 6A). The file is
# local JSON edited by hand (see data/career_profile.json).
DEFAULT_CAREER_PROFILE_PATH = "data/career_profile.json"

# SQLite persistence (Phase 4C). The file lives under data/ and is
# git-ignored; tests always use in-memory or temporary databases instead.
DEFAULT_DATABASE_PATH = "data/jobsearch.db"

# Local MVP dashboard (Phase 5). Binds to loopback only by default — the
# dashboard is a local tool, not a deployed service.
DEFAULT_DASHBOARD_HOST = "127.0.0.1"
DEFAULT_DASHBOARD_PORT = 8000


def load_env_file(path: str | Path = ENV_FILE_NAME, *, override: bool = False) -> bool:
    """Load ``KEY=VALUE`` pairs from ``path`` into ``os.environ``.

    Blank lines and lines starting with ``#`` are ignored, an optional
    ``export `` prefix is accepted, and single or double quotes around a
    value are stripped. Inline comments are not supported.

    Existing environment variables are preserved unless ``override`` is
    ``True``. Returns ``True`` if the file was found and parsed, and
    ``False`` if it does not exist.
    """
    file_path = Path(path)
    if not file_path.is_file():
        return False

    for raw_line in file_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue

        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]

        if override or key not in os.environ:
            os.environ[key] = value

    return True


def _resolve_log_level(raw: str) -> str:
    level = raw.strip().upper()
    return level if level in VALID_LOG_LEVELS else DEFAULT_LOG_LEVEL


def _resolve_gmail_max_results(raw: str) -> int:
    try:
        value = int(raw.strip())
    except ValueError:
        return DEFAULT_GMAIL_MAX_RESULTS
    return value if value >= 1 else DEFAULT_GMAIL_MAX_RESULTS


def _resolve_dashboard_port(raw: str) -> int:
    try:
        value = int(raw.strip())
    except ValueError:
        return DEFAULT_DASHBOARD_PORT
    return value if 1 <= value <= 65535 else DEFAULT_DASHBOARD_PORT


def _resolve_ai_timeout_seconds(raw: str) -> int:
    try:
        value = int(raw.strip())
    except ValueError:
        return DEFAULT_AI_TIMEOUT_SECONDS
    return value if 1 <= value <= 600 else DEFAULT_AI_TIMEOUT_SECONDS


@dataclass(frozen=True)
class Settings:
    """Runtime settings sourced from environment variables."""

    environment: str = DEFAULT_ENVIRONMENT
    log_level: str = DEFAULT_LOG_LEVEL
    gmail_query: str = DEFAULT_GMAIL_QUERY
    gmail_credentials_file: str = DEFAULT_GMAIL_CREDENTIALS_FILE
    gmail_token_file: str = DEFAULT_GMAIL_TOKEN_FILE
    gmail_max_results: int = DEFAULT_GMAIL_MAX_RESULTS
    #: AI API key (Phase 6A) — a secret: excluded from ``repr`` so it can
    #: never leak through logging or error output.
    ai_api_key: str = field(default="", repr=False)
    ai_base_url: str = DEFAULT_AI_BASE_URL
    ai_model: str = DEFAULT_AI_MODEL
    ai_timeout_seconds: int = DEFAULT_AI_TIMEOUT_SECONDS
    career_profile_path: str = DEFAULT_CAREER_PROFILE_PATH
    database_path: str = DEFAULT_DATABASE_PATH
    dashboard_host: str = DEFAULT_DASHBOARD_HOST
    dashboard_port: int = DEFAULT_DASHBOARD_PORT

    @classmethod
    def from_env(cls) -> "Settings":
        """Build settings from the current environment."""
        return cls(
            environment=os.getenv("APP_ENV", "").strip() or DEFAULT_ENVIRONMENT,
            log_level=_resolve_log_level(os.getenv("LOG_LEVEL", "")),
            gmail_query=os.getenv("GMAIL_QUERY", "").strip() or DEFAULT_GMAIL_QUERY,
            gmail_credentials_file=(
                os.getenv("GMAIL_CREDENTIALS_FILE", "").strip()
                or DEFAULT_GMAIL_CREDENTIALS_FILE
            ),
            gmail_token_file=(
                os.getenv("GMAIL_TOKEN_FILE", "").strip() or DEFAULT_GMAIL_TOKEN_FILE
            ),
            gmail_max_results=_resolve_gmail_max_results(
                os.getenv("GMAIL_MAX_RESULTS", "")
            ),
            ai_api_key=os.getenv("AI_API_KEY", "").strip(),
            ai_base_url=(
                os.getenv("AI_BASE_URL", "").strip() or DEFAULT_AI_BASE_URL
            ),
            ai_model=os.getenv("AI_MODEL", "").strip() or DEFAULT_AI_MODEL,
            ai_timeout_seconds=_resolve_ai_timeout_seconds(
                os.getenv("AI_TIMEOUT_SECONDS", "")
            ),
            career_profile_path=(
                os.getenv("CAREER_PROFILE_PATH", "").strip()
                or DEFAULT_CAREER_PROFILE_PATH
            ),
            database_path=(
                os.getenv("DATABASE_PATH", "").strip()
                or DEFAULT_DATABASE_PATH
            ),
            dashboard_host=(
                os.getenv("DASHBOARD_HOST", "").strip()
                or DEFAULT_DASHBOARD_HOST
            ),
            dashboard_port=_resolve_dashboard_port(
                os.getenv("DASHBOARD_PORT", "")
            ),
        )


def load_settings(env_file: str | Path | None = ENV_FILE_NAME) -> Settings:
    """Load ``.env`` (when present), then read settings from the environment."""
    if env_file is not None:
        load_env_file(env_file)
    return Settings.from_env()
