"""Draft-quality stamps + operator rates (approval / edit / auto share).

The edit-rate signal is captured AT SEND TIME (the human's edit overwrites the
agent's draft in place, so the comparison must happen before persisting), and
the line-ending normalisation matters: browsers submit textarea content with
CRLF while the stored draft uses LF — that round-trip must never count as an
edit. Legacy rows sent before the stamps existed are excluded from the splits,
never guessed.

Run: python -m pytest tests/ -q   (from the project root)
"""
from app import db
from app.dashboard import _draft_edited


# ── edit detection (dashboard approve path) ───────────────────────────────────
def test_verbatim_send_is_not_an_edit():
    assert _draft_edited("Hi Chris,\nAll good.", "Hi Chris,\nAll good.") is False


def test_browser_crlf_roundtrip_is_not_an_edit():
    assert _draft_edited("Hi Chris,\r\nAll good.\r\n", "Hi Chris,\nAll good.") is False


def test_trailing_whitespace_is_not_an_edit():
    assert _draft_edited("Hi Chris.\n\n", "Hi Chris.") is False


def test_changed_text_is_an_edit():
    assert _draft_edited("Hi Chris, price is £50.", "Hi Chris, price is £45.") is True


def test_emptied_draft_counts_as_an_edit():
    assert _draft_edited("", "Hi Chris.") is True


# ── aggregation (operator console card) ───────────────────────────────────────
class _LogQuery:
    def __init__(self, rows):
        self.rows = rows
        self.filters = []

    def table(self, _n):
        return self

    def select(self, _c):
        return self

    def eq(self, col, val):
        self.filters.append((col, val)); return self

    def gte(self, col, val):
        self.filters.append((col, val)); return self

    def order(self, _c, **_k):
        return self

    def range(self, start, end):
        # the 1000-row paging loop slices; honouring it ends the loop in one page
        self._slice = (start, end + 1)
        return self

    def execute(self):
        rows = self.rows[slice(*getattr(self, "_slice", (0, None)))]
        return type("Res", (), {"data": rows})()


def _row(status, via=None, edited=None):
    return {"status": status, "sent_via": via, "edited": edited}


def test_quality_rates_and_splits():
    rows = [
        _row("sent", "human", False),   # approved verbatim
        _row("sent", "human", True),    # approved after editing
        _row("sent", "human", True),
        _row("sent", "auto", False),    # full-auto send
        _row("sent"),                   # legacy send — no stamps
        _row("skipped"),
        _row("skipped"),
        _row("pending"),
    ]
    q = db.email_log_quality("t1", "2026-06-01", client=_LogQuery(rows))
    assert q["handled"] == 8
    assert q["sent"] == 5 and q["skipped"] == 2 and q["pending"] == 1
    assert q["approval_pct"] == 5 / 7 * 100          # sent / (sent + skipped)
    assert q["human_sent"] == 3 and q["auto_sent"] == 1
    assert q["edited"] == 2 and q["verbatim"] == 1
    assert round(q["edit_pct"], 1) == round(2 / 3 * 100, 1)  # of stamp-known human sends


def test_quality_with_no_decisions_yields_none_rates():
    q = db.email_log_quality("t1", "2026-06-01", client=_LogQuery([_row("pending")]))
    assert q["approval_pct"] is None and q["edit_pct"] is None


def test_quality_legacy_only_has_approval_but_no_edit_rate():
    rows = [_row("sent"), _row("skipped")]
    q = db.email_log_quality("t1", "2026-06-01", client=_LogQuery(rows))
    assert q["approval_pct"] == 50.0
    assert q["edit_pct"] is None      # nothing stamp-known — never guessed
    assert q["human_sent"] == 0 and q["auto_sent"] == 0


def test_quality_scopes_to_tenant_and_period():
    fake = _LogQuery([])
    db.email_log_quality("tenant-9", "2026-06-05T00:00:00", client=fake)
    assert ("tenant_id", "tenant-9") in fake.filters
    assert ("created_at", "2026-06-05T00:00:00") in fake.filters
