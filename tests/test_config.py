from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from readback.config import Settings, load_settings

ENV_NAMES = (
    "READBACK_DB",
    "READBACK_HOST",
    "READBACK_PORT",
    "READBACK_LLM_BASE_URL",
    "READBACK_LLM_API_KEY",
    "READBACK_LLM_MODEL",
    "READBACK_PASS_THRESHOLD",
    "READBACK_PARENT_PIN",
    "READBACK_SECRET_KEY",
)


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def test_defaults():
    settings = load_settings({})
    assert settings.db_path == Path("./readback.db")
    assert settings.host == "127.0.0.1"
    assert settings.port == 5057
    assert settings.llm_base_url == "http://localhost:11434/v1"
    assert settings.llm_api_key == ""
    assert settings.llm_model == "llama3.1:8b"
    assert settings.pass_threshold == 0.8
    assert settings.parent_pin == ""


def test_defaults_with_empty_mapping_do_not_read_os_environ(monkeypatch):
    monkeypatch.setenv("READBACK_PORT", "9999")
    assert load_settings({}).port == 5057
    assert load_settings().port == 9999


def test_reads_os_environ_when_env_is_none(monkeypatch):
    monkeypatch.setenv("READBACK_DB", "/tmp/custom.db")
    monkeypatch.setenv("READBACK_HOST", "0.0.0.0")
    settings = load_settings()
    assert settings.db_path == Path("/tmp/custom.db")
    assert settings.host == "0.0.0.0"


def test_coerces_types():
    settings = load_settings(
        {
            "READBACK_DB": "/tmp/readback.db",
            "READBACK_PORT": "8080",
            "READBACK_PASS_THRESHOLD": "0.65",
        }
    )
    assert isinstance(settings.db_path, Path)
    assert isinstance(settings.port, int)
    assert isinstance(settings.pass_threshold, float)
    assert settings.port == 8080
    assert settings.pass_threshold == 0.65


def test_passes_through_strings():
    settings = load_settings(
        {
            "READBACK_LLM_BASE_URL": "http://localhost:1234/v1",
            "READBACK_LLM_API_KEY": "sk-test",
            "READBACK_LLM_MODEL": "mistral:7b",
            "READBACK_PARENT_PIN": "4321",
            "READBACK_SECRET_KEY": "fixed-secret",
        }
    )
    assert settings.llm_base_url == "http://localhost:1234/v1"
    assert settings.llm_api_key == "sk-test"
    assert settings.llm_model == "mistral:7b"
    assert settings.parent_pin == "4321"
    assert settings.secret_key == "fixed-secret"


def test_blank_values_fall_back_to_defaults():
    settings = load_settings(
        {
            "READBACK_DB": "   ",
            "READBACK_HOST": "",
            "READBACK_PORT": " ",
            "READBACK_LLM_MODEL": "",
            "READBACK_PASS_THRESHOLD": "",
        }
    )
    assert settings.db_path == Path("./readback.db")
    assert settings.host == "127.0.0.1"
    assert settings.port == 5057
    assert settings.llm_model == "llama3.1:8b"
    assert settings.pass_threshold == 0.8


def test_values_are_stripped():
    settings = load_settings({"READBACK_HOST": " 0.0.0.0 ", "READBACK_PORT": " 5057 "})
    assert settings.host == "0.0.0.0"
    assert settings.port == 5057


def test_threshold_of_one_is_allowed():
    assert load_settings({"READBACK_PASS_THRESHOLD": "1"}).pass_threshold == 1.0


def test_threshold_close_to_zero_is_allowed():
    assert load_settings({"READBACK_PASS_THRESHOLD": "0.001"}).pass_threshold == 0.001


@pytest.mark.parametrize("value", ["0", "0.0", "-0.1", "1.01", "2", "abc", "0,9"])
def test_invalid_threshold_raises(value):
    with pytest.raises(ValueError):
        load_settings({"READBACK_PASS_THRESHOLD": value})


@pytest.mark.parametrize("value", ["eighty", "50.5", "80px"])
def test_non_integer_port_raises(value):
    with pytest.raises(ValueError):
        load_settings({"READBACK_PORT": value})


def test_empty_port_uses_default():
    assert load_settings({"READBACK_PORT": ""}).port == 5057


def test_error_messages_name_the_variable():
    with pytest.raises(ValueError, match="READBACK_PORT"):
        load_settings({"READBACK_PORT": "nope"})
    with pytest.raises(ValueError, match="READBACK_PASS_THRESHOLD"):
        load_settings({"READBACK_PASS_THRESHOLD": "1.5"})


def test_secret_key_is_random_per_process():
    first = load_settings({}).secret_key
    second = load_settings({}).secret_key
    assert first and second
    assert first != second


def test_secret_key_is_env_override():
    settings = load_settings({"READBACK_SECRET_KEY": "override-me"})
    assert settings.secret_key == "override-me"


def test_settings_is_frozen():
    settings = load_settings({})
    with pytest.raises(FrozenInstanceError):
        settings.port = 1234  # type: ignore[misc]


def test_settings_fields_match_spec():
    expected = (
        "db_path",
        "host",
        "port",
        "llm_base_url",
        "llm_api_key",
        "llm_model",
        "pass_threshold",
        "parent_pin",
        "secret_key",
    )
    assert tuple(Settings.__dataclass_fields__) == expected
