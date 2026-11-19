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
    """Keep tests hermetic: force-clear every credential/provider variable."""
    for name in (
        "SMARTCART_CHANNEL3_API_KEY",
        "CHANNEL3_API_KEY",
        "SMARTCART_GEMINI_API_KEY",
        "GEMINI_API_KEY",
        "SMARTCART_GEMINI_MODEL",
        "GEMINI_MODEL",
        "SMARTCART_KERNEL_API_KEY",
        "KERNEL_API_KEY",
        "SMARTCART_AI_PROVIDER",
        "AI_PROVIDER",
    ):
        monkeypatch.delenv(name, raising=False)
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


# --------------------------------------------------------------- Gemini / AI


def test_bare_gemini_key_and_model_are_loaded(tmp_path):
    """The deployment .env uses bare GEMINI_API_KEY / GEMINI_MODEL names."""
    env_file = _write_env(
        tmp_path,
        "GEMINI_API_KEY=synthetic-gemini-key\nGEMINI_MODEL=gemini-configured-test\n",
    )
    settings = Settings(_env_file=str(env_file))
    assert settings.gemini_configured is True
    assert settings.effective_ai_provider == "gemini"
    assert settings.gemini_model == "gemini-configured-test"
    assert settings.gemini_api_key.get_secret_value() == "synthetic-gemini-key"


def test_prefixed_gemini_names_work(tmp_path):
    env_file = _write_env(
        tmp_path,
        "SMARTCART_GEMINI_API_KEY=synthetic-gemini-key\n"
        "SMARTCART_GEMINI_MODEL=gemini-prefixed-test\n",
    )
    settings = Settings(_env_file=str(env_file))
    assert settings.gemini_configured is True
    assert settings.gemini_model == "gemini-prefixed-test"


def test_empty_gemini_key_is_not_configured(tmp_path):
    env_file = _write_env(tmp_path, "GEMINI_API_KEY=   \n")
    settings = Settings(_env_file=str(env_file))
    assert settings.gemini_configured is False
    assert settings.effective_ai_provider == "none"


def test_ai_provider_none_disables_gemini_even_with_a_key(tmp_path):
    env_file = _write_env(
        tmp_path, "GEMINI_API_KEY=synthetic-gemini-key\nSMARTCART_AI_PROVIDER=none\n"
    )
    settings = Settings(_env_file=str(env_file))
    assert settings.gemini_configured is False
    assert settings.effective_ai_provider == "none"


def test_default_ai_provider_is_auto_and_reports_none_without_a_key(tmp_path):
    settings = Settings(_env_file=str(_write_env(tmp_path, "SMARTCART_ENV=dev\n")))
    assert settings.ai_provider == "auto"
    assert settings.effective_ai_provider == "none"


def test_gemini_secret_is_not_rendered(tmp_path):
    env_file = _write_env(tmp_path, "GEMINI_API_KEY=synthetic-gemini-secret\n")
    settings = Settings(_env_file=str(env_file))
    assert "synthetic-gemini-secret" not in repr(settings.gemini_api_key)
    assert "synthetic-gemini-secret" not in str(settings.gemini_api_key)


# -------------------------------------------------------------------- KERNEL


def test_bare_kernel_api_key_is_loaded(tmp_path):
    env_file = _write_env(tmp_path, "KERNEL_API_KEY=synthetic-kernel-key\n")
    settings = Settings(_env_file=str(env_file))
    assert settings.kernel_configured is True
    assert settings.kernel_api_key.get_secret_value() == "synthetic-kernel-key"


def test_prefixed_kernel_api_key_is_loaded(tmp_path):
    env_file = _write_env(tmp_path, "SMARTCART_KERNEL_API_KEY=synthetic-kernel-key\n")
    settings = Settings(_env_file=str(env_file))
    assert settings.kernel_configured is True


def test_empty_kernel_key_is_not_configured(tmp_path):
    env_file = _write_env(tmp_path, "KERNEL_API_KEY=\n")
    settings = Settings(_env_file=str(env_file))
    assert settings.kernel_configured is False


def test_kernel_secret_is_not_rendered(tmp_path):
    env_file = _write_env(tmp_path, "KERNEL_API_KEY=synthetic-kernel-secret\n")
    settings = Settings(_env_file=str(env_file))
    assert "synthetic-kernel-secret" not in repr(settings.kernel_api_key)
    assert "synthetic-kernel-secret" not in str(settings.kernel_api_key)


def test_bare_keys_alone_do_not_flip_product_source(tmp_path):
    env_file = _write_env(
        tmp_path,
        "GEMINI_API_KEY=synthetic-gemini-key\nKERNEL_API_KEY=synthetic-kernel-key\n",
    )
    settings = Settings(_env_file=str(env_file))
    assert settings.product_source == "mock"
    assert settings.data_mode.value == "MOCK"
