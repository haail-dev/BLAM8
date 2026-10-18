from app import intake, settings_guide
import pytest


@pytest.mark.parametrize("description", [None, "", "logistics", "trades", "Dog boarding kennels",
    "Independent car garage", "Hair salon", "Software / SaaS", "Unlisted business"])
def test_descriptions_and_briefs_cannot_add_checklist_requirements(description, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Checklist must not load profiles")
    monkeypatch.setattr(settings_guide, "match_profiles", forbidden)
    baseline = intake.suggest_for()
    assert [f["label"] for f in baseline] == ["Customer name", "Contact number", "Preferred date / time"]
    assert intake.suggest_for(description, "we run a busy barber shop") == baseline
    assert intake.suggest_for(description, "we care for animals") == baseline
    baseline[0]["label"] = "Changed"
    assert intake.suggest_for()[0]["label"] == "Customer name"
