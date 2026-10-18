"""Today's order as a list (Kris 2026-09-13: "a setting cog on the box giving
the option to reorder priorities… a nice drop-down list"). The tenant's own
order of the eight topics — deck_priority.order — ranks the deck outright;
the cog on Today and Settings › Today's order share one list control.

Run: python -m pytest tests/test_deck_order.py -q
"""
from pathlib import Path

from fastapi.testclient import TestClient

from app import deck, db
from app.auth import TenantCtx, require_onboarded, require_tenant
from app.main import app
from app.security import require_csrf

ROOT = Path(__file__).resolve().parents[1]
DEFAULT = ["unhappy", "questions", "drafts", "bookings", "money", "quotes", "records", "promises", "quiet"]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_a_full_order_ranks_the_deck_outright():
    mine = ["money", "quiet", "promises", "bookings", "drafts", "questions", "unhappy", "quotes", "records"]
    cfg = {"deck_priority": {"order": mine}}
    assert deck.explicit_order(cfg) == mine
    assert [t for t, _ in deck.ordered_topics(cfg)] == mine
    # the clock lifts an imminent fact a couple of rungs, never past the top
    assert deck.rank_of("promises", cfg, imminent=True) < deck.rank_of("quiet", cfg)
    assert deck.rank_of("promises", cfg, imminent=True) > deck.rank_of("money", cfg)
    assert deck.summary(cfg).startswith("Leads with money")


def test_a_broken_order_leaves_the_default_and_placements_still_work():
    for junk in (["money"], DEFAULT + ["money"], DEFAULT[:-1] + ["nope"], "money,quiet", None, 7):
        assert deck.explicit_order({"deck_priority": {"order": junk}}) is None
        assert [t for t, _ in deck.ordered_topics({"deck_priority": {"order": junk}})] == DEFAULT
    assert deck.clean_order(list(reversed(DEFAULT))) == list(reversed(DEFAULT))
    # the older first/normal/last placements are untouched
    assert [t for t, _ in deck.ordered_topics({"deck_priority": {"money": "first"}})][0] == "money"


def test_saving_the_list_stores_the_order_and_the_cog_lands_back_on_today(monkeypatch):
    saved = {}
    monkeypatch.setattr(db, "upsert_tenant_config", lambda tid, cfg, client=None: saved.update(cfg))
    for dep in (require_onboarded, require_tenant):
        app.dependency_overrides[dep] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    try:
        c = TestClient(app)
        mine = list(reversed(DEFAULT))
        r = c.post("/app/settings/deck", data={"order": mine, "back": "/app/"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/app/?deck=saved"
        assert saved["deck_priority"] == {"order": mine}
        # from Settings: same store, back to Settings
        r = c.post("/app/settings/deck", data={"order": mine}, follow_redirects=False)
        assert r.headers["location"] == "/app/settings?saved=1"
        # a junk order (a topic missing) stores nothing rather than half an order
        r = c.post("/app/settings/deck", data={"order": mine[:-1]}, follow_redirects=False)
        assert saved["deck_priority"] == {}
        # the old selects still work when no list is posted
        r = c.post("/app/settings/deck", data={"deck_money": "first"}, follow_redirects=False)
        assert saved["deck_priority"] == {"money": "first"}
    finally:
        app.dependency_overrides.clear()


def test_one_list_control_on_today_and_in_settings_and_the_tour_knows_the_cog():
    part = _read("src/app/templates/app/_deck_order.html")
    assert 'data-deck-order-list' in part and '<input type="hidden" name="order" value="{{ topic }}">' in part
    assert 'data-move="up"' in part and 'data-move="down"' in part
    cog = _read("src/app/templates/app/_deck_cog.html")
    assert 'data-deck-order-toggle data-tour="order"' in cog
    assert '<div class="relative flex-none z-[1000]" data-deck-order-root>' in cog     # the header row's right end, over the pile
    assert '{% include "app/_deck_order.html" %}' in cog and '<input type="hidden" name="back" value="/app/">' in cog
    home = _read("src/app/templates/app/home.html")
    # Once, in the Who-needs-you-next header row (Kris 2026-09-21) — no longer
    # inside the All-clear card or the stack's top card (tests/test_today_header_row.py).
    assert home.count('{% include "app/_deck_cog.html" %}') == 1
    header = home[home.index('aria-label="Who needs you next">'):home.index("{% if deck_saved %}")]
    assert '{% include "app/_deck_cog.html" %}' in header and "deck-card" not in header
    assert "window.blam8DeckOrderOpen = setOpen;" in home
    settings = _read("src/app/templates/app/settings.html")
    assert '{% include "app/_deck_order.html" %}' in settings and 'name="deck_{{ topic }}"' not in settings
    js = _read("src/app/static/tour.js")
    order_stop = js.index('sel: \'[data-tour="order"]\'')
    assert js.index('sel: \'[data-tour="next"]\'') < order_stop < js.index('sel: \'[data-tour="emails"]\'')
    assert "passthrough: true" in js and "open: true" not in js                 # never auto-opens under the bubble
    assert "if (stops[idx] && stops[idx].passthrough) return;" in js
    assert 'document.addEventListener("blam8:deck-order"' in js and "bubble.hidden = !!(e.detail && e.detail.open);" in js
    assert 'new CustomEvent("blam8:deck-order", { detail: { open: !!open } })' in home

def test_the_open_list_paints_above_the_pile_and_the_cog_is_arrows():
    """Kris 2026-09-20: "the menu opens underneath the display cards so you can't
    click options" — the landed pile ranks later cards higher (z 1, 2, 3…) and
    restack() rewrites z inline on every scroll. The arrows live in the header
    row now (Kris 2026-09-21), so the root itself outranks every pile z and the
    card-lifting .is-ordering machinery went with the move. And the cog became
    up-and-down arrows (AutoTrader's sort control)."""
    home = _read("src/app/templates/app/home.html")
    assert "is-ordering" not in home and 'root.closest(".deck-card")' not in home
    assert 'c.classList.toggle("is-buried", hidden / h > 0.1);' in home
    cog = _read("src/app/templates/app/_deck_cog.html")
    assert "z-[1000]" in cog.split("data-deck-order-root")[0].rsplit("<div", 1)[1]        # on the root, over the pile
    assert 'd="M3 7.5 7.5 3m0 0L12 7.5M7.5 3v13.5m13.5 0L16.5 21m0 0L12 16.5m4.5 4.5V7.5"' in cog   # arrows up-down
    assert "M9.594 3.94" not in cog                                                             # the cog is gone
    assert "Tap the arrows to set what I put first" in _read("src/app/static/tour.js")
