"""Settings credential-loading tests.

Regression tests for the .env loading path discovered during the
live-integration audit: the root deployment ``.env`` stores the Channel3 key
under the bare name ``CHANNEL3_API_KEY``, while the documented name is
``SMARTCART_CHANNEL3_API_KEY``. Settings must accept both. Dummy values are
synthetic; real credentials are never referenced.
"""
from __future__ import annotations

import pytest

from app.config import Settings


@pytest.fixture(autouse=True)
def _clean_credential_env(monkeypatch):
    """Keep tests hermetic: force-clear both credential variable names."""
    monkeypatch.delenv("SMARTCART_CHANNEL3_API_KEY", raising=False)
    monkeypatch.delenv("CHANNEL3_API_KEY", raising=False)
    yield


def _write_env(tmp_path, content: str):
    env_file = tmp_path / ".env"
    env_file.write_text(content, encoding="utf-8")
    return env_file


def test_bare_channel3_api_key_name_is_loaded(tmp_path):
    """The name actually used by the deployment .env must load."""
    env_file = _write_env(tmp_path, "CHANNEL3_API_KEY=synthetic-key-value\n")
    settings = Settings(_env_file=str(env_file))
    assert settings.channel3_configured is True
    assert settings.channel3_api_key.get_secret_value() == "synthetic-key-value"


def test_prefixed_channel3_api_key_name_is_loaded(tmp_path):
    """The documented SMARTCART_ prefixed name keeps working."""
    env_file = _write_env(tmp_path, "SMARTCART_CHANNEL3_API_KEY=synthetic-key-value\n")
    settings = Settings(_env_file=str(env_file))
    assert settings.channel3_configured is True


def test_empty_channel3_api_key_is_not_configured(tmp_path):
    """An empty value must not fake a valid configuration."""
    env_file = _write_env(tmp_path, "CHANNEL3_API_KEY=\n")
    settings = Settings(_env_file=str(env_file))
    assert settings.channel3_configured is False


def test_rounded_empty_whitespace_value_is_ignored(tmp_path):
    env_file = _write_env(tmp_path, "CHANNEL3_API_KEY=   \n")
    settings = Settings(_env_file=str(env_file))
    assert settings.channel3_configured is False


def test_missing_key_defaults_to_none(tmp_path):
    env_file = _write_env(tmp_path, "SMARTCART_ENV=dev\n")
    settings = Settings(_env_file=str(env_file))
    assert settings.channel3_api_key is None


def test_bare_key_alone_does_not_flip_data_mode(tmp_path):
    """Loading a key must not silently switch the product source from mock."""
    env_file = _write_env(tmp_path, "CHANNEL3_API_KEY=synthetic-key-value\n")
    settings = Settings(_env_file=str(env_file))
    assert settings.product_source == "mock"
    assert settings.data_mode.value == "MOCK"


def test_secret_repr_does_not_leak(tmp_path):
    """Pydantic SecretStr must never render the value via repr/str."""
    env_file = _write_env(tmp_path, "CHANNEL3_API_KEY=synthetic-key-value\n")
    settings = Settings(_env_file=str(env_file))
    assert "synthetic-key-value" not in repr(settings.channel3_api_key)
    assert "synthetic-key-value" not in str(settings.channel3_api_key)
