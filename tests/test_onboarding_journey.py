from pathlib import Path
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from app import concierge_routes, db, onboarding, onboarding_journey as journey
from app.auth import TenantCtx, require_tenant
from app.main import app


@pytest.mark.parametrize("step,target", [("interview","mailbox"),("mailbox","voice"),("voice","knowledge")])
def test_application_order_and_destinations(step,target):
    state = journey.describe(step,connected=True)
    assert list(journey.STEPS) == onboarding.STEPS
    assert state["next"]["href"] == "/onboarding/"+target
    assert state["requires_click"] is True
    assert state["saved_completion"] == {}  # viewing a step does not prove earlier saves


def test_tuning_follows_knowledge_and_uses_actual_target():
    state=journey.describe("knowledge",next_target={"label":"Getting paid","href":"/app/invoices/settings"})
    assert state["next"]["href"] == "/app/invoices/settings"
    assert "do not precede Voice" in state["subsequent_phase"]


@pytest.mark.parametrize("connected",[True,False,None])
def test_navigation_reply_uses_verified_connection(connected):
    text=journey.navigation_reply(journey.describe("mailbox",connected=connected))
    assert "Voice" in text and "Diary" not in text
    assert ("cannot verify" in text) == (connected is not True)


def test_navigation_turn_cannot_invent_order_or_execute(monkeypatch):
    app.dependency_overrides[require_tenant] = lambda: TenantCtx("tenant",None)
    monkeypatch.setattr(concierge_routes.fairuse,"onboarding_allowed",lambda *a,**kw:True)
    monkeypatch.setattr(db,"get_tenant",lambda *a,**kw:{"industry":"Example"})
    monkeypatch.setattr(db,"get_tenant_config",lambda *a,**kw:{"voice_profile_source":None})
    monkeypatch.setattr(db,"get_connection_status",lambda *a,**kw:{"id":"mailbox"})
    monkeypatch.setattr(concierge_routes,"_tenant_context",lambda *a:{})
    async def forbidden(*a,**kw): pytest.fail("Navigation uses authoritative state, not a model reconstruction")
    monkeypatch.setattr(concierge_routes.concierge,"answer_onboarding",forbidden)
    try:
        response=TestClient(app).post("/concierge/onboard",json={"step":"mailbox",
            "question":"The inbox is connected. Please take me to the Voice step so I can look it over."})
        assert response.status_code == 200
        result=response.json()
        assert "Next is Voice" in result["reply"]
        assert "Nothing is submitted" in result["reply"]
        assert result["form_fill"] == {} and result["notes"] == []
        assert result["journey"]["saved_completion"]["mailbox"] is True
        assert result["journey"]["next"]["href"] == "/onboarding/voice"
    finally:
        app.dependency_overrides.clear()


def test_navigation_detection_does_not_swallow_field_changes():
    assert journey.is_navigation_request("That looks right. Please save it and take me to Customer inbox.")
    assert not journey.is_navigation_request("Please change the name, then take me to the next step.")


def test_readiness_and_explicit_click_replay():
    root=Path(__file__).resolve().parents[1]
    node=shutil.which("node")
    assert node,"Node is required for the offline journey replay"
    result=subprocess.run([node,"tests/replays/onboarding_journey_replay.mjs"],cwd=root,capture_output=True,text=True,timeout=30)
    assert result.returncode == 0,result.stdout+result.stderr
