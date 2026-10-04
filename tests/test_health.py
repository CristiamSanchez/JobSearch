"""Health/smoke tests: the package imports and configuration works."""

import os

from jobsearch import __version__
from jobsearch.config import Settings, load_env_file, load_settings


def test_package_imports_and_has_version():
    assert __version__ == "0.10.0"


def test_settings_defaults(monkeypatch):
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.delenv("LOG_LEVEL", raising=False)

    settings = Settings.from_env()

    assert settings.environment == "development"
    assert settings.log_level == "INFO"


def test_settings_read_from_environment(monkeypatch):
    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.setenv("LOG_LEVEL", "debug")

    settings = Settings.from_env()

    assert settings.environment == "staging"
    assert settings.log_level == "DEBUG"


def test_invalid_log_level_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "chatty")

    assert Settings.from_env().log_level == "INFO"


def test_load_env_file(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment line\n"
        "APP_ENV=from-file\n"
        "export LOG_LEVEL='WARNING'\n"
        "\n"
        "MALFORMED LINE\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.delenv("LOG_LEVEL", raising=False)

    assert load_env_file(env_file) is True
    assert os.environ["APP_ENV"] == "from-file"
    assert os.environ["LOG_LEVEL"] == "WARNING"


def test_env_file_does_not_override_existing_variables(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("APP_ENV=from-file\n", encoding="utf-8")
    monkeypatch.setenv("APP_ENV", "real-environment")

    assert load_env_file(env_file) is True
    assert os.environ["APP_ENV"] == "real-environment"


def test_missing_env_file_is_ignored(tmp_path):
    assert load_env_file(tmp_path / "does-not-exist.env") is False


def test_load_settings_without_env_file(monkeypatch):
    monkeypatch.delenv("APP_ENV", raising=False)

    settings = load_settings(env_file=None)

    assert settings.environment == "development"


def test_dashboard_settings_defaults(monkeypatch):
    from jobsearch.config import DEFAULT_DASHBOARD_HOST, DEFAULT_DASHBOARD_PORT

    monkeypatch.delenv("DASHBOARD_HOST", raising=False)
    monkeypatch.delenv("DASHBOARD_PORT", raising=False)

    settings = Settings.from_env()

    assert settings.dashboard_host == DEFAULT_DASHBOARD_HOST == "127.0.0.1"
    assert settings.dashboard_port == DEFAULT_DASHBOARD_PORT == 8000


def test_dashboard_settings_read_from_environment(monkeypatch):
    monkeypatch.setenv("DASHBOARD_HOST", "0.0.0.0")
    monkeypatch.setenv("DASHBOARD_PORT", "9123")

    settings = Settings.from_env()

    assert settings.dashboard_host == "0.0.0.0"
    assert settings.dashboard_port == 9123


def test_invalid_dashboard_port_falls_back_to_default(monkeypatch):
    from jobsearch.config import DEFAULT_DASHBOARD_PORT

    for bad_value in ("not-a-port", "-5", "70000", ""):
        monkeypatch.setenv("DASHBOARD_PORT", bad_value)
        assert Settings.from_env().dashboard_port == DEFAULT_DASHBOARD_PORT
