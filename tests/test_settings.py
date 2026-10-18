import pytest

from resolveai.settings import LiveModelBlocked, SettingsError, ensure_fake_provider, load_settings


def test_defaults_are_local_and_fake() -> None:
    settings = load_settings({})
    assert settings.model_provider == "fake"
    assert settings.llm_budget_usd == 0
    assert settings.model_api_key == ""
    assert "127.0.0.1:55432" in settings.database_url
    assert settings.database_url.endswith("/resolveai_demo")


def test_live_provider_and_budget_are_refused() -> None:
    with pytest.raises(LiveModelBlocked):
        load_settings({"MODEL_PROVIDER": "openai", "MODEL_NAME": "gpt", "MODEL_API_KEY": "sk-test"})
    with pytest.raises(LiveModelBlocked):
        load_settings({"LLM_BUDGET_USD": "5"})
    with pytest.raises(LiveModelBlocked):
        ensure_fake_provider(load_settings({}).model_copy(update={"embedding_provider": "openai"}))


def test_remote_database_is_refused() -> None:
    with pytest.raises(SettingsError):
        load_settings(
            {
                "DATABASE_URL": "postgresql+psycopg://user:secret@db.example.com/resolveai_demo",
            }
        )


def test_owner_live_settings_require_an_explicit_process_flag() -> None:
    from resolveai.settings import load_owner_live_settings

    with pytest.raises(LiveModelBlocked):
        load_owner_live_settings({"RESOLVEAI_ALLOW_LIVE": "0"})
    settings, key = load_owner_live_settings(
        {
            "RESOLVEAI_ALLOW_LIVE": "1",
            "MODEL_PROVIDER": "openai",
            "MODEL_NAME": "gpt-6-luna",
            "LLM_BUDGET_USD": "2",
            "OPENAI_API_KEY": "sk-test",
            "MODEL_API_KEY": "",
        }
    )
    assert settings.allow_live is True
    assert settings.model_api_key == ""
    assert key == "sk-test"
    blocked = load_settings({})
    assert blocked.model_provider == "fake"
    assert blocked.allow_live is False


def test_live_api_ignores_a_browser_field(tmp_path) -> None:
    from resolveai.agent.live_models import OpenAILunaProvider
    from resolveai.api.app import create_api_app
    from resolveai.settings import load_owner_live_settings

    settings, key = load_owner_live_settings(
        {
            "RESOLVEAI_ALLOW_LIVE": "1",
            "MODEL_PROVIDER": "openai",
            "MODEL_NAME": "gpt-6-luna",
            "LLM_BUDGET_USD": "2",
            "OPENAI_API_KEY": "sk-test",
        }
    )
    app = create_api_app(settings, live_api_key=key, ledger_path=tmp_path / "ledger.json")
    assert app.state.allow_live is True
    assert isinstance(app.state.provider_factory(), OpenAILunaProvider)
    ordinary = create_api_app(load_settings({}))
    assert ordinary.state.allow_live is False
    assert ordinary.state.provider_factory is not None


def test_health_reports_the_live_process_without_the_credential(tmp_path) -> None:
    from starlette.testclient import TestClient

    from resolveai.api.app import create_api_app
    from resolveai.settings import load_owner_live_settings

    settings, key = load_owner_live_settings(
        {
            "RESOLVEAI_ALLOW_LIVE": "1",
            "MODEL_PROVIDER": "openai",
            "MODEL_NAME": "gpt-6-luna",
            "LLM_BUDGET_USD": "2",
            "OPENAI_API_KEY": "sk-test",
        }
    )
    app = create_api_app(settings, live_api_key=key, ledger_path=tmp_path / "ledger.json")
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code in {200, 503}
    data = response.json()["data"]
    assert data["allow_live"] is True
    assert data["model_provider"] == "openai"
    assert data["model_name"] == "gpt-6-luna"
    assert data["llm_budget_usd"] == "2"
    assert "sk-test" not in response.text
    settings = load_settings(
        {
            "DATABASE_URL": (
                "postgresql+psycopg://resolveai_demo:resolveai_local_demo"
                "@127.0.0.1:55432/resolveai_pytest"
            )
        }
    )
    assert settings.database_url.endswith("/resolveai_pytest")
