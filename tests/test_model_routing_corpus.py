"""Freeze synthetic comparison inputs without calling a provider."""
from collections import Counter
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).parent / "fixtures/model-routing"


def test_comparative_contract_has_separate_splits_and_per_family_minimum():
    contract = json.loads((ROOT / "contract.json").read_text())
    cases = [json.loads(line) for line in (ROOT / "cases.jsonl").read_text().splitlines()]
    assert len({case["id"] for case in cases}) == len(cases)
    counts = Counter((c["family"], c["split"]) for c in cases)
    for family in ("crm_ordinary_fields", "routine_admin_message", "tone_preview"):
        assert counts[family, "held_out"] >= 100
        assert counts[family, "development"] == 20
    assert contract["runs_per_model_per_case"] == 3
    assert contract["gates"]["critical_failures"] == 0
    for case in cases:
        assert case["permitted_actions"] == []
        assert {"send", "booking_hold", "record_write"} <= set(case["forbidden_actions"])
        assert case["application_state"] and case["allowed_omissions"]
        assert "expected_fields" in case and "expected_facts" in case and "forbidden_facts" in case
    assert {c["dimensions"]["industry"] for c in cases} == {None, "unknown", "services", "custom"}
    assert len({hashlib.sha256(json.dumps(c, sort_keys=True).encode()).hexdigest() for c in cases}) == 360
