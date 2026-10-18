"""The platform does not expose the archived development and filming tools."""
import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.mark.parametrize("path", [
    "/admin/marketing-queue",
    "/admin/tenants/00000000-0000-0000-0000-000000000001/seed",
    "/app/dev/voice-harness",
    "/static/voice-live-harness.html",
    "/static/voice-live-rig.html",
])
def test_development_and_filming_endpoints_are_not_served(path):
    response = TestClient(app).get(path, follow_redirects=False)
    assert response.status_code == 404


@pytest.mark.parametrize("path", [
    "/rig/pending", "/rig/approve", "/rig/redraft", "/rig/push",
    "/rig/balances", "/rig/brief",
    "/concierge/voice/rig/token", "/concierge/voice/rig/chat", "/concierge/voice/rig/tts",
])
def test_external_rig_commands_are_not_platform_apis(path):
    response = TestClient(app).post(path, json={"k": "not-a-real-secret"})
    assert response.status_code == 404
