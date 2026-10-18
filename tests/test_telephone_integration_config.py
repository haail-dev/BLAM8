"""Carrier configuration is explicit, disabled and secret-safe by default."""
from app.config import Settings
import pytest


def test_live_transport_requires_explicit_runtime_and_deployment_facts():
    config=Settings(_env_file=None)
    assert config.telephone_live_enabled is False
    assert config.telephone_pilot_provider=="simulated"
    assert config.telephone_webhook_base_url==""
    assert config.telephone_realtime_model==""
    assert config.telephone_openai_project_id==""
    assert config.telephone_openai_webhook_secret==""


def test_reused_credentials_and_new_webhook_secret_are_not_represented():
    config=Settings(_env_file=None,openai_api_key="secret-openai-marker",
        twilio_auth_token="secret-twilio-marker",
        telephone_openai_webhook_secret="secret-webhook-marker")
    for marker in ("secret-openai-marker","secret-twilio-marker","secret-webhook-marker"):
        assert marker not in repr(config)
    assert config.openai_api_key=="secret-openai-marker"
    assert config.twilio_auth_token=="secret-twilio-marker"


@pytest.mark.parametrize("base",["http://phone.example.test","https://","https://user:pass@phone.example.test",
    "https://phone.example.test/path","https://phone.example.test?token=secret","https://phone.example.test#fragment"])
def test_readiness_rejects_noncanonical_callback_origin_without_provider_requests(monkeypatch,base):
    from app.config import settings
    from app.telephone_live import readiness
    for name in ("twilio_account_sid","twilio_auth_token","openai_api_key","telephone_realtime_model",
                 "telephone_openai_project_id","telephone_openai_webhook_secret"):
        monkeypatch.setattr(settings,name,"offline-configured")
    monkeypatch.setattr(settings,"telephone_webhook_base_url",base)
    result=readiness()
    assert result["configured"] is False
    assert result["carrier_verified"] is False and result["live_tested"] is False
    assert "secret" not in repr(result)


def test_present_configuration_is_not_live_account_verification(monkeypatch):
    from app.config import settings
    from app.telephone_live import readiness
    for name in ("twilio_account_sid","twilio_auth_token","openai_api_key","telephone_realtime_model",
                 "telephone_openai_project_id","telephone_openai_webhook_secret"):
        monkeypatch.setattr(settings,name,"offline-configured")
    monkeypatch.setattr(settings,"telephone_webhook_base_url","https://phone.example.test")
    monkeypatch.setattr(settings,"telephone_live_enabled",False)
    assert readiness()==dict(enabled=False,configured=True,missing=[],carrier_verified=False,live_tested=False)
