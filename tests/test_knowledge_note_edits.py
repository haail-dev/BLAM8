"""Bounded wording deletion through actual guide, correction and admission callers."""

from copy import deepcopy
import json
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from app import knowledge_note_edits as edits, knowledge_source_review as review
from app import (
    knowledge_review,
    knowledge_fidelity,
    knowledge_source_evidence,
    setup_cards,
)
from tests.test_knowledge_source_review import seeded
from tests.knowledge_review_fixture import review_store

REQUEST = "The last paragraph repeats the advice about mentioning an earlier report. Please remove that final paragraph and keep the rest."
FIRST = "When a request relates to an earlier enquiry, include its reference."
MIDDLE = "We review incoming enquiries with the owner."
LAST = "Include the earlier enquiry reference when reporting the same issue."


def scoped(content, *, unit=None, other=None):
    row, source = seeded()
    row["payload"]["entries"] = [dict(row["payload"]["entries"][0], content=content)]
    row["payload"]["source_units"] = knowledge_fidelity.inventory(
        [{"url": "https://example.test/advice", "text": unit or LAST}]
    )
    ctx = {"version": "v1", "reviews": [row], "sources": [], "brief": ""}
    form = {"version": "v1"}
    if other:
        row["payload"]["entries"].append(
            dict(row["payload"]["entries"][0], id="other", label="Other", content=other)
        )
        form["action_other"] = "keep"
    identity = row["payload"]["entries"][0]["id"]
    card = edits.scope(
        review.draft_card(row, source), ctx, row, form, identity, REQUEST
    )
    ref = card["knowledge_edit_scope"]["paragraphs"][-1]["reference"]
    raw = {
        "revision": 1,
        "changes": [
            {
                "id": identity,
                "content_hash": knowledge_source_evidence.digest(content),
                "insertions": [],
                "citations": [],
                "removals": [ref],
            }
        ],
        "overlaps": [],
        "questions": [],
        "edit_scope": edits.binding(card),
    }
    return row, source, card, raw


def test_exact_duplicate_removal_keeps_source_coverage_without_exclusion():
    row, source, card, raw = scoped(LAST + "\n\n" + MIDDLE + "\n\n" + LAST)
    result = review.validate(card, source, raw)["changes"][0]
    assert result["after"] == LAST + "\n\n" + MIDDLE
    assert result["source_choices"] == [] and all(value=="" for value in result["patches"].values())
    payload = review.corrected_payload(
        row, source, raw, owner_request=REQUEST, reviewer="owner", card=card
    )
    assert payload["entries"][0]["content"] == result["after"]
    assert payload["source_units"] == row["payload"]["source_units"]
    assert payload["source_review_history"][-1]["owner_request"] == REQUEST
    assert "".join(
        s["text"] for s in payload["entries"][0]["content_segments"]
    ).replace("\n", "") == result["after"].replace("\n", "")


def test_removal_preserves_other_retained_destination():
    row, source, card, raw = scoped(FIRST + "\n\n" + LAST, other=LAST)
    unit = row["payload"]["source_units"][0]
    card["knowledge_edit_scope"]["form"]["source_" + unit["id"]] = (
        "note:" + card["knowledge_edit_scope"]["id"]
    )
    raw["edit_scope"] = edits.binding(card)
    change = review.validate(card, source, raw)["changes"][0]
    assert change["patches"]["source_" + unit["id"]] == "note:other"
    assert change["source_choices"][0]["action"] == "retained"
    assert change["source_choices"][0]["destinations"] == ["Other"]


@pytest.mark.parametrize('original,changed', [
    ('Bookings are available Monday and Friday.', 'Bookings are available Monday to Friday.'),
    ('Deliveries from Camden to Islington.', 'Deliveries to Camden from Islington.'),
])
def test_different_relationship_cannot_justify_harmless_duplicate_removal(original, changed):
    row, source, card, raw = scoped(FIRST + '\n\n' + original, unit=original, other=changed)
    change = review.validate(card, source, raw)['changes'][0]
    choice = change['source_choices'][0]
    assert choice['action'] == 'excluded' and choice['text'] == original
    assert change['patches']['source_' + choice['id']] == 'exclude'
    assert change['patches']['source_reason_' + choice['id']] == REQUEST
    assert row['payload']['entries'][0]['content'].endswith(original)


@pytest.mark.parametrize(
    "unit,content",
    [
        (FIRST + " " + LAST, FIRST + "\n\n" + LAST),
        (
            "Urgent safety guidance must not wait for an approval request.",
            FIRST + "\n\nUrgent safety guidance must not wait for an approval request.",
        ),
        ("Fictional test profile only.", FIRST + "\n\nFictional test profile only."),
    ],
)
def test_partial_units_unique_constraints_and_caveats_cannot_be_silently_removed(
    unit, content
):
    row, source, card, raw = scoped(content, unit=unit)
    if unit == FIRST + " " + LAST:
        card["knowledge_edit_scope"]["units"] = [
            {
                "id": "mixed",
                "text": unit,
                "sources": ["https://example.test/advice"],
                "kind": "statement",
            }
        ]
    with pytest.raises(setup_cards.Invalid):
        review.validate(card, source, raw)
    assert row["payload"]["entries"][0]["content"] == content


@pytest.mark.parametrize(
    "tamper", ["ref", "hash", "request", "form", "scope", "all", "replacement"]
)
def test_fabricated_stale_cross_context_and_total_deletions_fail_closed(tamper):
    row, source, card, raw = scoped(FIRST + "\n\n" + LAST)
    if tamper == "ref":
        raw["changes"][0]["removals"] = ["invented"]
    elif tamper == "hash":
        raw["changes"][0]["content_hash"] = "bad"
    elif tamper == "request":
        card["knowledge_edit_scope"]["owner_request"] = "Different instruction"
    elif tamper == "form":
        card["knowledge_edit_scope"]["form"]["action_other"] = "keep"
    elif tamper == "scope":
        card.pop("knowledge_edit_scope")
    elif tamper == "all":
        raw["changes"][0]["removals"] = [
            p["reference"] for p in card["knowledge_edit_scope"]["paragraphs"]
        ]
    else:
        raw["changes"][0]["replacement"] = "Invented fact"
    with pytest.raises((setup_cards.Invalid, setup_cards.Stale)):
        review.validate(card, source, raw)


@pytest.mark.parametrize(
    "lose_response", [False, True], ids=["applied-response", "lost-response-reload"]
)
def test_natural_correction_preview_apply_unapproved_then_explicit_keep_and_save(
    review_store, monkeypatch, lose_response
):
    from app import (
        concierge,
        llm,
        usage,
        db,
        knowledge,
        knowledge_source_routes as routes,
        setup_card_knowledge,
        setup_held,
    )
    from app.auth import TenantCtx, require_tenant
    from app.security import require_csrf, require_csrf_header
    from app.main import app
    from app import security

    monkeypatch.setattr(security, "_BUCKETS", {})
    source = knowledge_source_evidence.build(
        [{"url": "https://example.test/advice", "text": FIRST + "\n\n" + LAST}]
    )
    units = knowledge_fidelity.inventory(source["pages"])
    original = FIRST + "\n\n" + MIDDLE + "\n\n" + LAST
    row = review_store.stage(
        "tenant", [{"label": "Following up", "content": original}], source_units=units
    )
    row["payload"].update(source_snapshot=source["id"], draft_revision=1)
    identity = row["payload"]["entries"][0]["id"]
    path = "/onboarding/knowledge/review/" + row["id"]
    ctx = TenantCtx(tenant_id="tenant", user_id="owner", client=None)
    app.dependency_overrides[require_tenant] = lambda: ctx
    app.dependency_overrides[require_csrf] = lambda: None
    app.dependency_overrides[require_csrf_header] = lambda: None
    monkeypatch.setattr(routes.settings, "knowledge_source_review_enabled", True)
    monkeypatch.setattr(db, "get_member", lambda *a, **kw: None)
    monkeypatch.setattr(routes.people, "can_do", lambda *a: True)
    monkeypatch.setattr(knowledge_source_evidence, "load", lambda *a: deepcopy(source))
    monkeypatch.setattr(routes.fairuse, "onboarding_allowed", lambda *a, **kw: True)
    monkeypatch.setattr(
        concierge, "client", lambda: SimpleNamespace(with_options=lambda **kw: None)
    )
    monkeypatch.setattr(usage, "record_anthropic", lambda *a, **kw: None)
    monkeypatch.setattr(usage, "record_voyage", lambda *a, **kw: None)
    monkeypatch.setattr(knowledge, "is_configured", lambda: True)
    monkeypatch.setattr(setup_held, "mark_used", lambda *a, **kw: None)

    async def prepare(raw):
        return {
            "notes": [
                {**n, "chunks": [{"content": n["content"], "embedding": []}]}
                for n in raw["notes"]
            ]
        }, 0

    monkeypatch.setattr(setup_card_knowledge, "prepare", prepare)
    calls = []

    def rpc(name, args):
        calls.append(name)
        assert args["p_expected"] == review_store.load("tenant")["version"]
        if name == "checkpoint_knowledge_review":
            row["payload"]["draft_review_choices"]=deepcopy(args["p_choices"])
        elif name == "revise_knowledge_review_sources":
            row["payload"] = deepcopy(args["p_payload"])
        else:
            assert name == "admit_knowledge_review"
            row["payload"] = deepcopy(args["p_payload"])
            row["state"] = "saved"
            review_store.sources.extend(
                {
                    "source": n["label"],
                    "content": n["chunks"][0]["content"],
                    "mailbox_id": None,
                }
                for n in args["p_notes"]
            )
        return SimpleNamespace(execute=lambda: SimpleNamespace(data=review_store.load("tenant") if name=="checkpoint_knowledge_review" else True))

    monkeypatch.setattr(db, "db", lambda: SimpleNamespace(rpc=rpc))

    async def provider(**kwargs):
        schema = kwargs["output_config"]["format"]["schema"]
        if "intent" in schema["properties"]:
            raw = {
                "intent": "correct",
                "reason": REQUEST,
                "answer": "",
                "target": "",
                "explanation": "Correct the duplicate.",
            }
        else:
            payload = json.loads(kwargs["messages"][0]["content"])[
                "drafts_and_evidence"
            ]
            line = next(l for l in payload["lines"] if l["id"] == identity)
            refs = schema["properties"]["changes"]["items"]["properties"]["removals"][
                "items"
            ]["enum"]
            assert refs == [
                p["reference"] for p in payload["current_note_edit"]["paragraphs"]
            ]
            raw = {
                "revision": payload["revision"],
                "changes": [
                    {
                        "id": identity,
                        "content_hash": line["content_hash"],
                        "insertions": [],
                        "citations": [],
                        "removals": [refs[-1]],
                    }
                ],
                "overlaps": [],
                "questions": [],
            }
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=json.dumps(raw))],
            model="offline",
            usage={},
        )

    monkeypatch.setattr(llm, "create", provider)
    try:
        client = TestClient(app)
        form = {"version": review_store.load("tenant")["version"]}
        initial = client.post(path + "/guide", json={"form": form}).json()
        handoff = client.post(
            path + "/guide",
            json={
                "form": form,
                "question": REQUEST,
                "focus_hash": initial["focus_hash"],
            },
        ).json()
        assert (
            handoff["correction_requested"]
            and not handoff.get("patches")
            and calls == []
        )
        body = {
            "version": form["version"],
            "edits": {},
            "form": form,
            "question": REQUEST,
            **handoff["correction_note"],
        }
        response = client.post(path + "/corrections", json=body)
        assert response.status_code == 200, response.text
        preview = response.json()
        change = preview["changes"][0]
        assert (
            change["after"] == FIRST + "\n\n" + MIDDLE
            and LAST in change["removals"][0]["quote"]
        )
        choice = change["source_choices"][0]
        assert (
            choice["text"] == LAST
            and choice["action"] == "excluded"
            and choice["reason"] == REQUEST
            and choice["destinations"] == []
        )
        assert (
            calls == []
            and not review_store.sources
            and row["payload"]["entries"][0]["content"] == original
        )
        applybody = {k: v for k, v in body.items() if k != "question"}
        applybody.update(review=preview["review"], owner_request=REQUEST)
        applied = client.post(path + "/corrections/apply", json=applybody)
        assert applied.status_code == 200, applied.text
        assert (
            calls == ["revise_knowledge_review_sources"]
            and not review_store.sources
            and row["state"] == "pending"
        )
        reload_view = knowledge_review.review_view(review_store.load("tenant"), row)
        assert reload_view["form"]["source_" + units[-1]["id"]] == "exclude"
        assert reload_view["form"]["source_reason_" + units[-1]["id"]] == REQUEST
        assert reload_view["form"]["action_" + identity] == ""
        # Ordinary reload/lost HTTP response recovers explicit pending choices,
        # while never recovering the old note approval.
        page = client.get(path)
        assert page.status_code == 200
        assert 'value="exclude"' in page.text and 'value="' + REQUEST + '"' in page.text
        if lose_response:
            form = knowledge_review.review_form(
                row, {"version": review_store.load("tenant")["version"]}
            )
        else:
            form.update(applied.json()["patches"])

        form["content_" + identity] = applied.json()["entries"][0]["content"]
        assert (
            form["action_" + identity] == ""
            and form["source_reason_" + units[-1]["id"]] == REQUEST
        )
        assert row["payload"]["source_units"] == units and source["pages"][0][
            "text"
        ].endswith(LAST)
        form["version"] = review_store.load("tenant")["version"]
        assert client.post(path, data={**form, "operation": "save"}).status_code == 400
        shown = client.post(path + "/guide", json={"form": form}).json()
        if shown.get("checkpoint"):form["version"]=shown["checkpoint"]["version"]
        confirmed = client.post(
            path + "/guide",
            json={
                "form": form,
                "question": "Keep this note",
                "focus_hash": shown["focus_hash"],
            },
        ).json()
        assert (
            confirmed["patches"] == {"action_" + identity: "keep"}
            and not review_store.sources
        )
        form.update(confirmed["patches"])
        if confirmed.get("checkpoint"):form["version"]=confirmed["checkpoint"]["version"]
        saved = client.post(
            path, data={**form, "operation": "save"}, follow_redirects=False
        )
        assert saved.status_code == 303, saved.text
        assert (
            [c for c in calls if c!="checkpoint_knowledge_review"] == ["revise_knowledge_review_sources", "admit_knowledge_review"]
            and row["state"] == "saved"
        )
        assert (
            len(review_store.sources) == 1
            and LAST
            not in review_store.sources[0]["content"].split("\n\nSource evidence:")[0]
        )
        assert row["payload"]["source_decisions"][-1]["reason"] == REQUEST
    finally:
        app.dependency_overrides.clear()


def test_relative_paragraphs_are_displayed_body_not_unrelated_retained_tail():
    row, source = seeded()
    row["payload"]["entries"] = [
        dict(row["payload"]["entries"][0], content=FIRST + "\n\n" + MIDDLE)
    ]
    row["payload"]["source_units"] = knowledge_fidelity.inventory(
        [{"url": "https://example.test/advice", "text": LAST}]
    )
    identity = row["payload"]["entries"][0]["id"]
    unit = row["payload"]["source_units"][0]
    ctx = {"version": "v1", "reviews": [row], "sources": [], "brief": ""}
    form = {"version": "v1", "source_" + unit["id"]: "note:" + identity}
    card = edits.scope(
        review.draft_card(row, source), ctx, row, form, identity, REQUEST
    )
    assert card["lines"][0]["fields"]["content"] == FIRST + "\n\n" + MIDDLE
    edit = review.context(card, source)["current_note_edit"]
    assert edit["paragraphs"][-1]["quote"] == MIDDLE
    assert LAST not in edit["retention_context_only"] and LAST not in edit["displayed_body"]
    raw = {
        "revision": 1,
        "changes": [
            {
                "id": identity,
                "content_hash": knowledge_source_evidence.digest(
                    FIRST + "\n\n" + MIDDLE
                ),
                "insertions": [],
                "citations": [],
                "removals": [edit["paragraphs"][-1]["reference"]],
            }
        ],
        "overlaps": [],
        "questions": [],
        "edit_scope": edits.binding(card),
    }
    changed = review.validate(card, source, raw)["changes"][0]
    assert changed["after"] == FIRST and not changed["source_choices"]
    assert "source_" + unit["id"] not in changed["patches"]


def test_unpreviewed_retention_is_not_materialized_by_other_note_correction():
    row, source, card, raw = scoped(FIRST + "\n\n" + MIDDLE, other="A different note.")
    unit = row["payload"]["source_units"][0]
    identity = card["knowledge_edit_scope"]["id"]
    form = card["knowledge_edit_scope"]["form"]
    form["source_" + unit["id"]] = "note:" + identity
    ctx = {"version": "v1", "reviews": [row], "sources": [], "brief": ""}
    card = edits.scope(
        review.draft_card(row, source), ctx, row, form, identity, REQUEST
    )
    other = next(l for l in card["lines"] if l["id"] == "other")
    quote = "Fictional test profile only."
    ref = knowledge_source_evidence.reference(source, 0, quote)
    raw.update(
        edit_scope=edits.binding(card),
        changes=[
            {
                "id": "other",
                "content_hash": knowledge_source_evidence.digest(
                    other["fields"]["content"]
                ),
                "insertions": [{"position": "before", "reference": ref}],
                "citations": [],
                "removals": [],
            }
        ],
    )
    original = deepcopy(row["payload"]["entries"][0])
    payload = review.corrected_payload(
        row, source, raw, card=card, owner_request=REQUEST
    )
    assert payload["entries"][0] == original
    assert LAST not in payload["entries"][0]["content"]


def test_removed_claim_citations_move_to_audit_and_cannot_be_newly_attested():
    row, source, card, raw = scoped(FIRST + "\n\n" + LAST)
    claim = {"source": "https://example.test/advice", "excerpt": LAST, "claim": LAST}
    row["payload"]["entries"][0]["provenance"].append(claim)
    payload = review.corrected_payload(
        row, source, raw, card=card, owner_request=REQUEST
    )
    assert claim not in payload["entries"][0]["provenance"]
    assert (
        claim in payload["source_review_history"][-1]["changes"][0]["removed_citations"]
    )
    raw["changes"][0]["citations"] = [
        {
            "claim": LAST,
            "reference": knowledge_source_evidence.reference(
                source, 0, "Fictional test profile only."
            ),
        }
    ]
    with pytest.raises(setup_cards.Invalid, match="surviving"):
        review.validate(card, source, raw)
