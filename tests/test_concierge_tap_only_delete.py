"""A delete card is confirmed by a tap on the card, and by nothing else.

Kris, 16 October 2026: "Make delete cards tap-only in Standard voice." PR #49 adds four
confirm-card kinds; the older Standard voice client treats a spoken "yes" as the confirm
for whatever card is waiting, so a spoken yes could have run invoice_delete_draft or
quote_delete_draft. The rule now lives on the SERVER, at the one route every surface
confirms through (POST /concierge/business/execute): for a tap-only kind the request must
carry the marker only the card's own button sends (confirm_via = "tap"). A missing marker
is not a tap. The refusal says one plain sentence and leaves the card waiting, so the
tap that follows still works.

The store below is the in-memory one from tests/test_concierge_draft_edit_delete.py with a
concierge_proposals table added, so the REAL claim, the REAL route and the REAL executors
run end to end; only the database is replaced.

Run: python -m pytest tests/test_concierge_tap_only_delete.py -q
"""
import copy
import json
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import concierge_actions, concierge_routes, db, security
from app.auth import require_onboarded, TenantCtx
from app.main import app
from app.security import require_csrf_header
from tests.test_concierge_draft_edit_delete import (  # noqa: F401  (fixtures and helpers)
    INV_1002, INV_1004, INV_9001, T1, _Query, _invoice_row, _lines_of, _stage, rig)
from tests.test_concierge_draft_edit_delete_quotes import (  # noqa: F401
    QUO_1003, QUO_1005, _quote_row, _qlines, qrig)

ROOT = Path(__file__).resolve().parent.parent
SENTENCE = "That is a delete: tap Confirm on the card."
ORIGINS = ("typed", "voice", "live", "unknown")

DELETES = {
    "invoice_delete_draft": {"kind": "invoice_delete_draft", "invoice_id": INV_1004},
    "quote_delete_draft": {"kind": "quote_delete_draft", "quote_id": QUO_1003},
}
EDITS = {
    "invoice_edit_draft": {"kind": "invoice_edit_draft", "invoice_id": INV_1002, "amount": "200"},
    "quote_edit_draft": {"kind": "quote_edit_draft", "quote_id": QUO_1003, "amount": "200"},
}


# ── the store, the route and the owner ────────────────────────────────────────
def _val(row, col):
    """A PostgREST column, including the JSON paths the claim and the receipt use."""
    if col == "result->execution->>id":
        return ((row.get("result") or {}).get("execution") or {}).get("id")
    if col.startswith("payload->"):
        return (row.get("payload") or {}).get(col.split("->", 1)[1])
    return row.get(col)


class _ProposalQuery(_Query):
    def eq(self, col, value):
        self.filters.append(lambda r, c=col, v=value: _val(r, c) == v)
        return self

    def in_(self, col, values):
        self.filters.append(lambda r, c=col, v=tuple(values): _val(r, c) in v)
        return self

    def is_(self, col, value):
        assert value == "null"
        self.filters.append(lambda r, c=col: _val(r, c) is None)
        return self

    def gte(self, col, value):
        self.filters.append(lambda r, c=col, v=value: _val(r, c) is not None and _val(r, c) >= v)
        return self


@pytest.fixture
def world(qrig, monkeypatch):
    """The rig (invoices and quotes) plus the proposals table, and the owner of tenant-1
    holding a TestClient on the real app."""
    store = qrig.store
    store.s["concierge_proposals"] = []
    store.table = lambda name: (_ProposalQuery if name == "concierge_proposals" else _Query)(store, name)
    monkeypatch.setattr(security.settings, "rate_limit_enabled", False)
    monkeypatch.setattr(db, "get_member", lambda *a, **k: None)
    qrig.saved, qrig.traces = [], []
    monkeypatch.setattr(db, "add_concierge_message", lambda tid, role, content, **kw: qrig.saved.append(content))
    monkeypatch.setattr(concierge_routes.trace, "emit",
                        lambda tid, trace_type, **kw: qrig.traces.append((trace_type, kw)))
    monkeypatch.setattr(concierge_routes.concierge_tasks, "settle_card", lambda *a, **k: None)
    monkeypatch.setattr(concierge_routes.concierge_actions, "mailbox_refusal", lambda *a, **k: None)
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id=T1, client=store, user_id="u1")
    app.dependency_overrides[require_csrf_header] = lambda: None
    qrig.http = TestClient(app)
    yield qrig
    app.dependency_overrides.clear()


def _waiting(world, action):
    """Stage the card through the real stager and file its row as pending, as the
    database would hold it."""
    card = _stage(world, action)
    saved = world.proposals[-1]
    world.store.s["concierge_proposals"].append({
        "id": card["proposal_id"], "tenant_id": T1, "user_id": "u1", "kind": saved["kind"],
        "summary": saved["summary"], "payload": saved["payload"], "status": "pending", "result": {},
        "executed_at": None, "created_at": datetime.now(timezone.utc).isoformat()})
    return card


def _row(world, card):
    return next(r for r in world.store.s["concierge_proposals"] if r["id"] == card["proposal_id"])


def _post(world, card, **body):
    return world.http.post("/concierge/business/execute", json={"proposal_id": card["proposal_id"], **body})


def _delete_writes(world):
    return [w for w in world.store.writes if w[1] == "delete"]


def _target_exists(world, kind):
    return (_invoice_row(world, INV_1004) if kind == "invoice_delete_draft" else _quote_row(world, QUO_1003)) is not None


def _assert_refused_and_untouched(world, card, res, kind=None):
    assert res.status_code == 200, res.text
    data = res.json()
    assert (data["ok"], data["state"], data["summary"], data["proposal_id"]) == \
        (False, "pending", SENTENCE, card["proposal_id"])
    row = _row(world, card)
    assert row["status"] == "pending" and not row["result"] and row["executed_at"] is None   # not claimed
    assert not _delete_writes(world) and not world.events and not world.saved                # nothing ran
    if kind:
        assert _target_exists(world, kind)
    assert ("concierge_confirm", "confirmed") not in [(t, kw.get("outcome")) for t, kw in world.traces]


# ═════════════════════════════════════════════════════════════════════════════
# the rule: no tap, no delete
# ═════════════════════════════════════════════════════════════════════════════
def test_the_tap_only_kinds_are_exactly_the_two_deletes_and_are_always_confirm():
    assert concierge_actions.TAP_ONLY_KINDS == frozenset({"invoice_delete_draft", "quote_delete_draft"})
    assert concierge_actions.TAP_ONLY_KINDS <= concierge_actions.ALWAYS_CONFIRM_KINDS
    # an edit stays as it was: a spoken yes may confirm it, as for the other kinds
    assert not {"invoice_edit_draft", "quote_edit_draft"} & concierge_actions.TAP_ONLY_KINDS
    assert concierge_actions.TAP_ONLY_SENTENCE == SENTENCE


NOT_A_TAP = [
    pytest.param({}, id="no-marker"),
    pytest.param({"confirm_via": "words"}, id="words"),
    pytest.param({"confirm_via": "unknown"}, id="unknown"),
    # what a spoken or typed answer could look like: the route never reads words, so no
    # casing or punctuation can talk it round
    pytest.param({"confirm_via": "words", "draft_text": "YES."}, id="YES."),
    pytest.param({"confirm_via": "words", "draft_text": "yes please delete it"}, id="yes-please-delete-it"),
    pytest.param({"draft_text": "go ahead", "selected": [0]}, id="go-ahead-and-no-marker"),
    pytest.param({"confirm_via": "words", "selected": [0, 1, 2]}, id="words-with-a-selection"),
]


@pytest.mark.parametrize("body", NOT_A_TAP)
@pytest.mark.parametrize("kind", list(DELETES))
def test_a_confirm_that_is_not_a_tap_is_refused_and_the_delete_card_stays_waiting(world, kind, body):
    card = _waiting(world, DELETES[kind])
    res = _post(world, card, **body)
    _assert_refused_and_untouched(world, card, res, kind)
    # the card was not used up: the tap that follows still deletes
    done = _post(world, card, confirm_via="tap").json()
    assert (done["ok"], done["state"]) == (True, "completed") and done["summary"].startswith("Done — draft")
    assert not _target_exists(world, kind) and _row(world, card)["status"] == "executed"


@pytest.mark.parametrize("marker", [{}, {"confirm_via": "words"}])
@pytest.mark.parametrize("origin", ORIGINS)
@pytest.mark.parametrize("kind", list(DELETES))
def test_no_surface_can_confirm_a_delete_by_words(world, kind, origin, marker):
    """Standard voice, GPT-Live, the typed panel and the Concierge page all confirm through
    this route, and they differ only in display_origin. None of them gets a delete done
    without the tap marker."""
    card = _waiting(world, DELETES[kind])
    res = _post(world, card, display_origin=origin, **marker)
    _assert_refused_and_untouched(world, card, res, kind)


@pytest.mark.parametrize("marker", ["TAP", "Tap", "tap ", "tapped", "yes", "", True, 1, ["tap"], None])
@pytest.mark.parametrize("kind", list(DELETES))
def test_a_marker_that_is_not_exactly_tap_never_counts(world, kind, marker):
    card = _waiting(world, DELETES[kind])
    res = _post(world, card, confirm_via=marker)
    assert res.status_code in (200, 422)                    # a malformed marker is turned away or refused
    if res.status_code == 200:
        _assert_refused_and_untouched(world, card, res, kind)
    assert not _delete_writes(world) and _target_exists(world, kind)
    assert _row(world, card)["status"] == "pending" and not _row(world, card)["result"]


@pytest.mark.parametrize("origin", ORIGINS)
@pytest.mark.parametrize("kind", list(DELETES))
def test_a_tap_confirm_runs_the_delete_through_the_real_route(world, kind, origin):
    """The body the card's own button sends (concierge_panel.js): the tap marker, the
    origin of the screen the card is on, an empty draft and no selection."""
    card = _waiting(world, DELETES[kind])
    res = _post(world, card, confirm_via="tap", display_origin=origin, draft_text="", selected=None)
    assert res.status_code == 200, res.text
    data = res.json()
    assert (data["ok"], data["state"]) == (True, "completed")
    assert data["summary"].startswith("Done — draft") and "can't be brought back" in data["summary"]
    assert not _target_exists(world, kind) and _row(world, card)["status"] == "executed"
    assert len(_delete_writes(world)) == 1
    if kind == "invoice_delete_draft":
        assert _invoice_row(world, INV_1002) is not None and _lines_of(world, INV_1004) == []
    else:
        assert _quote_row(world, QUO_1005) is not None and _qlines(world, QUO_1003) == []
    assert any(data["summary"] == s for s in world.saved)           # the outcome joins the transcript
    assert ("concierge_confirm", "confirmed") in [(t, kw.get("outcome")) for t, kw in world.traces]


@pytest.mark.parametrize("kind", list(DELETES))
def test_a_delete_card_a_background_task_prepared_is_just_as_tap_only(world, kind, monkeypatch):
    """A card a task list prepared is claimed through the delegation machinery, which would
    otherwise take a spoken yes: the rule is asked before that path is reached."""
    from app import concierge_delegation
    monkeypatch.setattr(concierge_delegation.settings, "concierge_delegation_enabled", True)

    def forbidden(*a, **k):
        raise AssertionError("a words confirm must be refused before the delegation claim")
    monkeypatch.setattr(concierge_delegation, "transition", forbidden)
    card = _waiting(world, DELETES[kind])
    _row(world, card)["payload"] = {**_row(world, card)["payload"],
                                    "_delegation": {"task_id": "t1", "revision": 1, "attempt_id": "a1", "scope": "s"}}
    _assert_refused_and_untouched(world, card, _post(world, card, confirm_via="words"), kind)


@pytest.mark.parametrize("kind", list(DELETES))
def test_a_refusal_leaves_a_trace_and_a_second_refusal_is_the_same(world, kind):
    card = _waiting(world, DELETES[kind])
    for _ in range(2):
        _assert_refused_and_untouched(world, card, _post(world, card, confirm_via="words"), kind)
    refused = [(t, kw) for t, kw in world.traces if t == "concierge_confirm" and kw.get("outcome") == "refused"]
    assert len(refused) == 2
    assert refused[0][1]["proposal_id"] == card["proposal_id"]
    assert refused[0][1]["detail"] == {"kind": kind, "reason": "tap_only"}


@pytest.mark.parametrize("kind", list(DELETES))
def test_a_spoken_yes_after_the_tap_reports_the_real_outcome_and_deletes_nothing_twice(world, kind):
    card = _waiting(world, DELETES[kind])
    assert _post(world, card, confirm_via="tap").json()["state"] == "completed"
    writes = len(_delete_writes(world))
    again = _post(world, card, confirm_via="words").json()
    assert again["state"] == "completed" and again["summary"] != SENTENCE      # its receipt, not a refusal
    assert len(_delete_writes(world)) == writes == 1


@pytest.mark.parametrize("kind", list(DELETES))
def test_the_bin_still_cancels_a_delete_card(world, kind):
    card = _waiting(world, DELETES[kind])
    res = world.http.post("/concierge/business/cancel", json={"proposal_id": card["proposal_id"]})
    assert res.json()["state"] == "cancelled" and _row(world, card)["status"] == "cancelled"
    assert _target_exists(world, kind) and not _delete_writes(world)


# ── edits are not touched by the rule ────────────────────────────────────────
@pytest.mark.parametrize("body", [{}, {"confirm_via": "words"}, {"confirm_via": "unknown"}, {"confirm_via": "tap"}],
                         ids=["no-marker", "words", "unknown", "tap"])
@pytest.mark.parametrize("kind", list(EDITS))
def test_an_edit_card_is_still_confirmable_the_old_way(world, kind, body):
    card = _waiting(world, EDITS[kind])
    assert "tap_only" not in card
    data = _post(world, card, **body).json()
    assert (data["ok"], data["state"]) == (True, "completed") and data["summary"].startswith("Done — ")
    assert _row(world, card)["status"] == "executed"
    expect = [("Work as discussed", 200.0)] if kind == "invoice_edit_draft" else None
    if expect:
        assert _lines_of(world, INV_1002) == expect
    else:
        assert _quote_row(world, QUO_1003)["total"] == 200.0


# ═════════════════════════════════════════════════════════════════════════════
# a bulk card cannot be the way round
# ═════════════════════════════════════════════════════════════════════════════
BULK_WITH_A_DELETE = {"kind": "bulk", "items": [
    {"kind": "invoice_delete_draft", "invoice_id": INV_1004},
    {"kind": "invoice_edit_draft", "invoice_id": INV_1002, "amount": "200"}]}


@pytest.mark.parametrize("body", [{}, {"confirm_via": "words"}, {"confirm_via": "words", "selected": [0, 1]},
                                  {"confirm_via": "words", "selected": [1]}],
                         ids=["no-marker", "words", "words-all-ticked", "words-delete-unticked"])
def test_a_bulk_card_carrying_a_delete_is_refused_to_words_and_runs_nothing(world, body):
    card = _waiting(world, BULK_WITH_A_DELETE)
    assert card["kind"] == "bulk" and [i["ok"] for i in card["items"]] == [True, True]
    res = _post(world, card, **body)
    _assert_refused_and_untouched(world, card, res)
    assert _invoice_row(world, INV_1004) is not None
    assert _lines_of(world, INV_1002) == [("Work as discussed", 252.5)]      # not even the edit beside it
    # and the tap runs the ticked items, as before
    done = _post(world, card, confirm_via="tap", selected=[0, 1]).json()
    assert done["state"] == "completed" and done["summary"].startswith("2 of 2 items completed.")
    assert _invoice_row(world, INV_1004) is None and _lines_of(world, INV_1002) == [("Work as discussed", 200.0)]


def test_a_bulk_of_edits_is_confirmed_by_words_as_before(world):
    card = _waiting(world, {"kind": "bulk", "items": [
        {"kind": "invoice_edit_draft", "invoice_id": INV_1002, "amount": "200"},
        {"kind": "quote_edit_draft", "quote_id": QUO_1003, "amount": "200"}]})
    assert card["kind"] == "bulk" and "tap_only" not in card
    done = _post(world, card, confirm_via="words").json()
    assert done["state"] == "completed" and done["summary"].startswith("2 of 2 items completed.")


def test_a_delete_that_could_not_be_prepared_does_not_make_its_bulk_tap_only(world):
    """Only items that can actually run count: another business's invoice stays unticked
    on the card with its reason and can never run, so the edit beside it is confirmed as
    any edit is."""
    card = _waiting(world, {"kind": "bulk", "items": [
        {"kind": "invoice_delete_draft", "invoice_id": INV_9001},
        {"kind": "invoice_edit_draft", "invoice_id": INV_1002, "amount": "200"}]})
    assert [i["ok"] for i in card["items"]] == [False, True] and "tap_only" not in card
    done = _post(world, card, confirm_via="words").json()
    assert done["state"] == "completed" and _invoice_row(world, INV_9001) is not None


def test_a_bulk_of_one_delete_is_that_delete_card_and_tap_only(world):
    card = _waiting(world, {"kind": "bulk", "items": [{"kind": "invoice_delete_draft", "invoice_id": INV_1004}]})
    assert card["kind"] == "invoice_delete_draft" and card["tap_only"] is True
    _assert_refused_and_untouched(world, card, _post(world, card, confirm_via="words"), "invoice_delete_draft")


# ═════════════════════════════════════════════════════════════════════════════
# the card says it, so no client has to guess
# ═════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("kind", list(DELETES))
def test_a_delete_card_says_it_is_tap_only(world, kind):
    assert _stage(world, DELETES[kind])["tap_only"] is True


def test_a_bulk_card_with_a_runnable_delete_says_it_is_tap_only(world):
    assert _stage(world, BULK_WITH_A_DELETE)["tap_only"] is True


def test_needs_tap_reads_the_kind_and_only_the_prepared_items_of_a_bulk():
    needs = concierge_actions.needs_tap
    assert needs("invoice_delete_draft") and needs("quote_delete_draft")
    assert not any(needs(k) for k in ("invoice_edit_draft", "quote_edit_draft", "invoice_send", "draft_reply", "", None))
    assert needs("bulk", [{"kind": "x", "ok": True}, {"kind": "quote_delete_draft", "ok": True}])
    assert not needs("bulk", [{"kind": "quote_delete_draft", "ok": False}, {"kind": "invoice_edit_draft", "ok": True}])
    assert not needs("bulk", None) and not needs("bulk", "junk") and not needs("bulk", [None, 3])
    assert not needs("invoice_edit_draft", [{"kind": "invoice_delete_draft", "ok": True}])   # items only matter to a bulk
    assert concierge_actions.row_needs_tap({"kind": "bulk", "payload": {"items": [
        {"kind": "invoice_delete_draft", "ok": True}]}}) is True
    assert concierge_actions.row_needs_tap({"kind": "invoice_edit_draft", "payload": {}}) is False
    assert concierge_actions.row_needs_tap({}) is False


# ═════════════════════════════════════════════════════════════════════════════
# the claim: refused before anything is written
# ═════════════════════════════════════════════════════════════════════════════
def test_the_claim_refuses_a_pending_proposal_its_caller_will_not_run_and_writes_nothing(world):
    card = _waiting(world, DELETES["invoice_delete_draft"])
    before = copy.deepcopy(world.store.s["concierge_proposals"])
    with pytest.raises(db.ClaimRefused):
        db.claim_concierge_proposal(card["proposal_id"], T1, client=world.store, refuse_if=lambda row: True)
    assert world.store.s["concierge_proposals"] == before
    # a rule that says no one's business changes nothing
    row = db.claim_concierge_proposal(card["proposal_id"], T1, client=world.store, refuse_if=lambda row: False)
    assert row["status"] == "executing"


def test_the_claim_leaves_a_proposal_that_is_not_pending_to_report_its_own_state(world):
    card = _waiting(world, DELETES["invoice_delete_draft"])
    _row(world, card)["status"] = "executed"
    assert db.claim_concierge_proposal(card["proposal_id"], T1, client=world.store, refuse_if=lambda row: True) is None


# ═════════════════════════════════════════════════════════════════════════════
# there is no other way in
# ═════════════════════════════════════════════════════════════════════════════
def _calls(pattern):
    """Where in app/ the pattern is called (not defined)."""
    hits = []
    for path in sorted((ROOT / "src/app").rglob("*.py")):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(pattern, line) and not line.lstrip().startswith(("def ", "async def ", "#")):
                hits.append(f"{path.relative_to(ROOT).as_posix()}:{n}")
    return hits


def test_the_confirm_route_is_the_only_thing_that_claims_or_runs_a_proposal():
    """Standard voice, GPT-Live (a spoken answer is sent back to the card), the typed panel
    and the Concierge page all end at /concierge/business/execute. If a second place ever
    claims or executes a proposal - a server-side 'bare yes' - this fails and the new door
    needs the same tap rule."""
    assert [h.split(":")[0] for h in _calls(r"\bclaim_concierge_proposal\(")] == ["src/app/concierge_routes.py"]
    assert [h.split(":")[0] for h in _calls(r"concierge_actions\.execute\(")] == ["src/app/concierge_routes.py"]
    # a typed message only ever reaches the model: the typed path never posts to the execute route
    panel = (ROOT / "src/app/static/concierge_panel.js").read_text(encoding="utf-8")
    ask = panel[panel.index("async function ask("):panel.index("// ── live voice (moved from")]
    assert "/concierge/business/execute" not in ask and "submitProposal" not in ask


def test_the_confirm_route_passes_the_rule_to_the_claim_for_everything_that_is_not_a_tap():
    src = (ROOT / "src/app/concierge_routes.py").read_text(encoding="utf-8")
    route = src[src.index("async def business_execute"):src.index("class BusinessCancelRequest")]
    assert 'confirm_via == "tap"' in route and "refuse_if" in route and "concierge_actions.row_needs_tap" in route
    assert "except db.ClaimRefused" in route
    assert route.index("except db.ClaimRefused") < route.index("except Exception")      # not read as a lost claim response
    model = src[src.index("class BusinessExecuteRequest"):src.index("class BusinessExecuteResponse")]
    assert 'confirm_via: Literal["tap", "words", "unknown"] = "unknown"' in model         # a missing marker is not a tap


# ═════════════════════════════════════════════════════════════════════════════
# the clients: the card's button sends the tap, a spoken yes sends words
# ═════════════════════════════════════════════════════════════════════════════
def test_the_card_button_sends_the_tap_marker_and_a_spoken_yes_sends_words():
    node = shutil.which("node")
    assert node, "Node is required for the offline browser replay"
    done = subprocess.run([node, "tests/replays/tap_only_replay.mjs"], cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stdout + done.stderr


def test_the_voice_screen_does_not_invite_a_yes_for_a_delete_card():
    panel = (ROOT / "src/app/static/concierge_panel.js").read_text(encoding="utf-8")
    voice = (ROOT / "src/app/static/voice-live.js").read_text(encoding="utf-8")
    assert "p.tap_only ? 'Back — tap Confirm on the card' : 'Back — say yes to confirm'" in panel
    assert ('finalPayload.proposal.tap_only ? "1 change ready — tap Confirm on the card" '
            ': "1 change ready — say yes to confirm"') in voice
    # the resurfaced card of a background task is built by the route, so it carries the flag too
    src = (ROOT / "src/app/concierge_routes.py").read_text(encoding="utf-8")
    resurface = src[src.index("def business_resurface"):src.index('"/business/dismiss"')]
    assert "concierge_actions.tap_flag(" in resurface
