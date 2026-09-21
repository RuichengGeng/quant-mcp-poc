import pytest

from quant_mcp.agent.model import build_model


@pytest.fixture
def model_options(monkeypatch):
    options = {}

    def factory(**kwargs):
        options.update(kwargs)
        return options

    monkeypatch.setattr("smolagents.OpenAIModel", factory)
    return options


def test_generic_settings_override_provider_defaults(model_options, monkeypatch):
    monkeypatch.setenv("SMOLAGENTS_API_KEY", "generic-test-key")
    monkeypatch.setenv("SMOLAGENTS_API_BASE", "https://office.invalid/v1")
    monkeypatch.setenv("SMOLAGENTS_MODEL_ID", "office-model")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "provider-test-key")
    assert build_model(20) is model_options
    assert model_options == {
        "model_id": "office-model", "api_base": "https://office.invalid/v1",
        "api_key": "generic-test-key", "client_kwargs": {"timeout": 20, "max_retries": 1},
    }


def test_provider_settings_and_timeout_cap(model_options, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "provider-test-key")
    monkeypatch.setenv("DEEPSEEK_THINKING", "enabled")
    monkeypatch.setenv("SMOLAGENTS_MODEL_TIMEOUT_SECONDS", "5")
    build_model(30)
    assert model_options["api_key"] == "provider-test-key"
    assert model_options["extra_body"] == {"thinking": {"type": "enabled"}}
    assert model_options["client_kwargs"]["timeout"] == 5


def test_missing_credentials_fail_before_model_initialization(model_options):
    with pytest.raises(ValueError, match="API_KEY"):
        build_model(30)
    assert model_options == {}


def test_invalid_thinking_setting(model_options, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("DEEPSEEK_THINKING", "invalid")
    with pytest.raises(ValueError, match="enabled or disabled"):
        build_model(30)
    assert model_options == {}
