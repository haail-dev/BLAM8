"""Step 1 capture contracts; fake model output is not a live model-quality eval."""
import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import concierge, concierge_routes, db, onboarding
from app.auth import TenantCtx, require_tenant
from app.main import app


@pytest.mark.parametrize("industry,brief,goals", [
    ("Residential property management", "Fictional/test-only agency in England serving landlords and tenants.",
     "Handle tenancy questions and gather maintenance reports; human review of every reply. Our team schedules contractors; no unauthorised promises."),
    ("Repairs", "Fictional demo workshop serving local households; no emergency work.",
     "Gather repair enquiries for human review; our team agrees appointments and prices."),
    ("Music teaching", "Test-only music tuition for adults, online only.",
     "Answer lesson enquiries and collect availability; the tutor confirms lesson times. Keep sending off."),
])
def test_interview_turn_transports_separate_fields_without_applying_settings(monkeypatch, industry, brief, goals):
    config = {"auto_send": False, "draft_hold": True, "setup_held": []}
    writes = []
    monkeypatch.setattr(db, "get_tenant_config", lambda *a, **kw: dict(config))
    monkeypatch.setattr(db, "upsert_tenant_config", lambda *a, **kw: writes.append((a, kw)))
    monkeypatch.setattr(concierge_routes.fairuse, "onboarding_allowed", lambda *a, **kw: True)
    monkeypatch.setattr(concierge_routes.usage, "record_anthropic", lambda *a, **kw: None)
    monkeypatch.setattr(concierge, "client", lambda: None)
    fields = {"business_name": "Example (Rig Demo)", "website": "example.test",
              "industry": industry, "business_brief": brief, "goals": goals}
    history = [{"role": "user", "content": f"We're Example (Rig Demo), in {industry}. {brief} Our website is example.test."},
               {"role": "assistant", "content": "What would you like help with?"}]
    question = f"{goals} Please fill that in, but let me check it before we go on."

    async def fake_model(**kwargs):
        prompt = "\n".join(b["text"] for b in kwargs["system"])
        # Assert the contract at the actual model boundary, not an unused prompt.
        for instruction in ("Capture EVERYTHING already supplied", "Goals must not appear only in the brief",
                            "fictional/test-only", "do not require extra scheduling",
                            "set ready_to_advance FALSE", "not a business setting to store",
                            "not applied controls", "never invent a held field"):
            assert instruction in prompt
        rules = prompt.split("RULES —", 1)[1].split("This step:", 1)[0]
        assert "Pausing is a supported workflow choice" in rules
        assert "ask them to check it and press the step's button" not in rules
        assert kwargs["messages"][-1]["content"] == question
        assert kwargs["messages"][:-1] == history
        assert set(fields) == set(kwargs["output_config"]["format"]["schema"]["properties"]["form_fill"]["properties"])
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps({
            "reply": "Filled for review. I'll stop here.", "form_fill": fields,
            "ready_to_advance": False, "held": []}))], usage=None, model="fake")

    monkeypatch.setattr(concierge.llm, "create", fake_model)
    app.dependency_overrides[require_tenant] = lambda: TenantCtx(tenant_id="capture-test", client=None)
    try:
        # No context manager: never start background workers.
        client = TestClient(app)
        response = client.post("/concierge/onboard", json={"step": "interview", "question": question,
                                                          "history": history})
        assert response.status_code == 200
        result = response.json()
        assert result["form_fill"] == fields
        assert result["ready_to_advance"] is False
        assert result["held_for"] == [] and result["notes"] == [] and result["review_link"] is None
        assert writes == []
        # Only the user's later explicit form submission persists these values.
        monkeypatch.setattr(db, "set_tenant_name", lambda *a, **kw: None)
        monkeypatch.setattr(db, "set_tenant_industry", lambda *a, **kw: None)
        client.cookies.set("bl_csrf", "capture-test-csrf")
        submitted = client.post("/onboarding/interview", data={**fields, "csrf_token": "capture-test-csrf"},
                                follow_redirects=False)
        assert submitted.status_code == 303
        assert len(writes) == 1
        saved = writes[0][0][1]
        assert saved == {f: fields[f] for f in onboarding.INTERVIEW_CFG_FIELDS}
        assert config == {"auto_send": False, "draft_hold": True, "setup_held": []}
    finally:
        app.dependency_overrides.clear()


def test_interview_review_receipt_and_fill_replay():
    node = shutil.which("node")
    assert node, "Node is required for the offline onboarding replay"
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([node, "tests/replays/onboarding_capture_replay.mjs"],
                            cwd=root, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
