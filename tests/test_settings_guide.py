"""Per-business settings recipes (src/app/settings_guide.py) + their injection into the in-app
help assistant. Pure — no API calls."""
from app import settings_guide as sg
from app.concierge import build_help_prompt


# ── profile matching ──────────────────────────────────────────────────────────
def test_known_industry_ids_map_to_defaults():
    assert sg.match_profiles("trades")[0]["id"] == "field_service"
    assert sg.match_profiles("logistics")[0]["id"] == "logistics"
    assert sg.match_profiles("hire")[0]["id"] == "hire"
    assert sg.match_profiles("events")[0]["id"] == "venue"


def test_other_industry_matches_on_brief_keywords():
    assert sg.match_profiles("other", "we run a boarding kennels for dogs")[0]["id"] == "kennels"
    assert sg.match_profiles("Pet grooming & boarding", "")[0]["id"] == "kennels"
    assert sg.match_profiles("other", "a hair salon and barber")[0]["id"] == "salon"
    assert sg.match_profiles("other", "italian restaurant, table bookings")[0]["id"] == "restaurant"
    assert sg.match_profiles("other", "self storage units")[0]["id"] == "storage"


def test_no_match_returns_empty():
    assert sg.match_profiles("other", "something totally unlike anything") == []


def test_stated_industry_not_hijacked_by_trades_mentioned_in_brief():
    # The BLAM8-tenant shape: a SaaS industry whose BRIEF describes the customers the
    # product serves. The brief must stay a fallback for vague industries only — a stated
    # industry that matches no profile means NO recipe, not another sector's.
    brief = ("AI email assistant that answers a business's email and books the job — "
             "built for garages, hauliers, plumbers and other trades")
    assert sg.match_profiles("Software / SaaS — AI email automation", brief) == []
    # And 'automation'/'automotive'-style words never substring-match garage's 'auto'.
    assert sg.match_profiles("Marketing automation agency") == []


def test_industry_keywords_win_over_brief_keywords():
    # A real garage whose brief mentions other trades matches on ITS industry only.
    hits = sg.match_profiles("Independent car garage", "we also fit tyres for local hauliers")
    assert [h["id"] for h in hits] == ["garage"]


# ── config status ─────────────────────────────────────────────────────────────
def test_config_status_reads_what_is_set():
    empty = sg.config_status({})
    assert "Already set up: nothing yet" in empty
    assert "Services & durations" in empty and "Resource types" in empty  # all in 'not set'
    cfg = {"service_types": [{"name": "Cut", "minutes": 30}],
           "resource_types": [{"name": "Chair", "count": 2}],
           "buffer_minutes": 15}
    st = sg.config_status(cfg)
    assert "Already set up:" in st
    set_line = st.splitlines()[0]
    assert "Services & durations" in set_line and "Resource types" in set_line
    assert "Gap between bookings" in set_line


# ── full guide ────────────────────────────────────────────────────────────────
def test_guide_for_kennels_includes_recipe_base_and_status():
    g = sg.guide_for("other", "boarding kennels", {"service_types": []})
    assert "Foundation (every business" in g          # base
    assert "Overnight stays" in g and "kennel" in g.lower()  # kennels recipe
    assert "Their current diary setup:" in g          # status


def test_guide_for_unmatched_offers_catalogue():
    g = sg.guide_for("other", "an unrecognisable trade", None)
    assert "wasn't an exact match" in g
    assert "Salon" in g and "Kennels" in g            # catalogue of labels to choose from


# ── injection into the help prompt ────────────────────────────────────────────
def test_help_prompt_injects_customer_and_guide():
    tenant = {"name": "Happy Tails", "industry": "other", "brief": "boarding kennels",
              "modules": ["scheduling"]}
    guide = sg.guide_for(tenant["industry"], tenant["brief"], {})
    p = build_help_prompt(tenant=tenant, settings_guide=guide)
    assert "<your_customer>" in p and "Happy Tails" in p and "boarding kennels" in p
    assert "<settings_guide>" not in p and "never fill a default" in p
    assert "diary_settings:" in p                      # the new nav destination is offered
    # bare prompt (no tenant) is unchanged — no customer/guide blocks
    bare = build_help_prompt()
    assert "<your_customer>" not in bare and "<settings_guide>" not in bare
