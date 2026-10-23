"""Regression tests for the real LLM client configuration (no network, dummy keys).

Mocked-LLM tests can't catch client-side configuration errors, which have happened before
(a model rejecting temperature=0 broke every live request)."""
import pytest

from app.graph import IntentResult, SQLResult, get_llm, structured


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ("LLM_PROVIDER", "LLM_MODEL", "LLM_TEMPERATURE", "GOOGLE_API_KEY", "GEMINI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "dummy-gemini-key")


def test_default_is_gemini_3_5_flash_lite_with_retries_and_provider_default_temperature():
    llm = get_llm()
    assert type(llm).__name__ == "ChatGoogleGenerativeAI"
    assert llm.model.endswith("gemini-3.5-flash-lite") and llm.max_retries == 4
    assert llm.temperature in (None, 1.0)          # Gemini 3.x: provider default, not forced to 0


def test_gemini_2_keeps_temperature_zero(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "gemini-2.5-flash")
    assert get_llm().temperature == 0


def test_display_names_are_normalised_to_model_ids(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "Gemini 3.5 Flash Lite")
    assert get_llm().model.endswith("gemini-3.5-flash-lite")


def test_model_is_configurable(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "gemini-2.5-pro")
    assert get_llm().model.endswith("gemini-2.5-pro")


def test_gemini_api_key_alias_is_accepted(monkeypatch):
    import os
    monkeypatch.delenv("GOOGLE_API_KEY")
    monkeypatch.setenv("GEMINI_API_KEY", "alias-key")
    get_llm()
    assert os.environ["GOOGLE_API_KEY"] == "alias-key"


def test_structured_output_schemas_bind_for_gemini():
    llm = get_llm()
    for schema in (IntentResult, SQLResult):
        assert structured(llm, schema) is not None


def test_structured_falls_back_for_objects_without_method_kwarg():
    class Plain:
        def with_structured_output(self, model):
            return ("plain", model)
    assert structured(Plain(), SQLResult) == ("plain", SQLResult)


def test_anthropic_still_supported_and_temperature_opt_in(monkeypatch):
    pytest.importorskip("langchain_anthropic")
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("LLM_MODEL", "claude-sonnet-5-5")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy")
    from langchain_core.messages import HumanMessage
    payload = get_llm()._get_request_payload([HumanMessage("hi")])
    assert "temperature" not in payload or payload["temperature"] is None
