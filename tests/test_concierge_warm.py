"""Priming the voice Concierge after a deploy (src/app/concierge_warm.py)."""
import pytest
from app import concierge_data, concierge_draft, concierge_tasks, concierge_delegation, concierge_warm


@pytest.mark.parametrize("enabled", [False, True])
def test_it_warms_exactly_the_tool_sets_voice_sends(monkeypatch, enabled):
    monkeypatch.setattr(concierge_delegation, "enabled", lambda: enabled)
    sets = dict(concierge_warm.tool_sets())
    assert set(sets) == {"owner", "owner-starter", "staff"}
    names = [t["name"] for t in sets["owner"]]
    assert names == [t["name"] for t in concierge_data.tools_for(True, None)] + [
        concierge_draft.DRAFT_TOOL["name"], concierge_draft.SHOW_TOOL["name"]] + [
            tool["name"] for tool in concierge_tasks.foreground_tools()]
    assert ("control_task" in names) is enabled
    assert len(sets["staff"]) < len(sets["owner"])        # staff doors narrow the catalogue


def test_only_on_render(monkeypatch):
    monkeypatch.delenv("RENDER", raising=False)
    assert concierge_warm.should_warm() is False
    src = open("src/app/main.py", encoding="utf-8").read()
    assert "concierge_warm.start()" in src
    route = open("src/app/concierge_routes.py", encoding="utf-8").read()
    # If voice ever sends a different tool list, warm the new one too.
    assert "*concierge_tasks.foreground_tools()" in route
