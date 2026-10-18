"""Contact identity — the inbox shows people, not addresses (2026-07-02).

Three layers per email_log row: from_name (From-header display name, captured at
ingest), contact_name / contact_company (agent-stated, rule 6 self-report).
Pins:
  * the draft schema requires the identity fields and _parse_draft trims/caps them;
  * list_conversations aggregates last-NON-EMPTY-wins (a later email that doesn't
    restate the name never blanks the thread's identity) and computes display_name
    with stated-name > header-name > None (template falls back to the address);
  * the Today queue renders the friendly name with the address as the tooltip.

Run: python -m pytest tests/test_contact_identity.py -q
"""
from app import db
from app.agent import DRAFT_SCHEMA, _parse_draft


# ── schema + parse ────────────────────────────────────────────────────────────
def test_schema_requires_contact_identity():
    for f in ("contact_name", "contact_company"):
        assert f in DRAFT_SCHEMA["properties"]
        assert f in DRAFT_SCHEMA["required"]


def test_parse_draft_trims_and_caps_identity():
    out = _parse_draft(
        '{"reply":"Hi Sarah","deferred":false,"missing":"","scheduling_requested":false,'
        '"thread_state":"in_progress","request_summary":"","action_summary":"",'
        '"contact_name":"  Sarah Jones  ","contact_company":"' + "A" * 200 + '"}')
    assert out["contact_name"] == "Sarah Jones"
    assert len(out["contact_company"]) == 120                  # capped
    # absent fields default to "" (legacy payloads, safety-refusal fallback)
    assert _parse_draft('{"reply":"hi"}')["contact_name"] == ""
    assert _parse_draft("not json")["contact_company"] == ""


# ── list_conversations aggregation ────────────────────────────────────────────
class _Table:
    def __init__(self, rows):
        self.rows = rows

    def select(self, *_a, **_k): return self
    def eq(self, *_a): return self
    def in_(self, *_a): return self
    def gte(self, *_a): return self
    def order(self, *_a, **_k): return self
    def range(self, *_a): return self

    def execute(self):
        return type("R", (), {"data": self.rows})()


class _FakeClient:
    def __init__(self, email_rows, bookings=()):
        self._tables = {"email_log": list(email_rows), "bookings": list(bookings)}

    def table(self, name):
        return _Table(self._tables.get(name, []))


def _row(contact, **kw):
    return {"from_address": contact, "subject": "s", "body_in": "b", "status": "pending",
            "created_at": "2026-07-02T09:00:00+00:00", **kw}


def test_identity_last_non_empty_wins_and_display_priority():
    rows = [   # oldest → newest, as the query returns them
        _row("a@x.com", from_name="Sarah Jones"),                       # header name only
        _row("a@x.com", contact_name="Sarah", contact_company="Acme Ltd"),  # stated later
        _row("a@x.com"),                                                # says nothing — must not blank
        _row("b@y.com"),                                                # never identified
    ]
    convos = {c["contact"]: c for c in db.list_conversations("t1", client=_FakeClient(rows))}
    a = convos["a@x.com"]
    assert a["from_name"] == "Sarah Jones"
    assert a["contact_name"] == "Sarah" and a["contact_company"] == "Acme Ltd"
    assert a["display_name"] == "Sarah"            # stated beats the header name
    b = convos["b@y.com"]
    assert b["display_name"] is None               # template falls back to the address


def test_header_name_alone_is_the_display_name():
    rows = [_row("a@x.com", from_name="Dave Smith")]
    (c,) = db.list_conversations("t1", client=_FakeClient(rows))
    assert c["display_name"] == "Dave Smith" and not c.get("contact_company")
