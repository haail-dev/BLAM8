"""Setup-prompt wording (first-walk and re-run, 1 Oct 2026): each new sentence is
present in its prompt and the sentence it replaced is gone."""
from app import concierge, kb_website, settings_recommend


def _guidance(step):
    g = concierge.ONBOARDING_STEPS[step]["guidance"]
    return g if isinstance(g, str) else "".join(g)


def _dependants_field():
    return settings_recommend.PAGES["dependants"]["fields"]["field"]


def _booking_field():
    return settings_recommend.PAGES["booking_details"]["fields"]["field"]


def test_website_asked_only_when_empty():
    """F3: the website question was asked although an address was already on the page."""
    g = _guidance("interview")
    assert "ONLY when the `website` field is still empty (never ask when an address is already on the page), also ask, once and on its own turn" in g
    assert "Also ask, once and on its own turn" not in g


def test_voice_rule_stays_out_of_tone():
    """F6: 'always give X' said alongside the tone ended up inside `tone`."""
    g = _guidance("voice")
    assert "is such a rule even when it is said in the same breath as the tone: hold it, and keep it out of `tone`" in g
    assert "`tone` holds the tone alone, in a few words." in g
    assert "on its own line in `voice_notes`" in g


def test_a_rule_is_not_a_row():
    """F9/F13: a rule about how the business works was written as a booking-details row."""
    f = _booking_field()
    assert "A row is a QUESTION the customer answers when booking" in f
    assert "is never a row, it is a knowledge note" in f
    assert "applies to SOME customers only is OPTIONAL" in f
    assert f.index("A row is a QUESTION") < f.index("REQUIRED only for what the agent cannot book without")


def test_expiry_reminds_unless_told_to_block():
    """F16: a lapsed date was given a booking gate when the owner only wanted a reminder."""
    f = _dependants_field()
    assert "Use gate ONLY when the owner says a lapsed or missing date must STOP the booking" in f
    assert "write no gate" in f
    assert "Use gate when the owner requires" not in f


def test_dropdown_type_is_offered():
    """F18: a field whose answer is one of a short named list had no dropdown type."""
    f = _dependants_field()
    assert "TYPE is text, number, date, yes/no, or 'dropdown: Option one, Option two'" in f
    assert "TYPE is text, number, date or yes/no," not in f


def test_reader_keeps_every_rule():
    """F8: the website reader capped itself at 4-12 notes and dropped rules to fit."""
    s = kb_website.SYSTEM
    assert "there is no target count" in s
    assert "keep EVERY distinct rule, price and policy the pages state" in s
    assert "4-12" not in s
    assert kb_website.MAX_ENTRIES >= 30


def test_a_date_that_runs_out_is_always_expires():
    """Round three, 1 Oct 2026 (F16, second half): told when not to gate, the
    assistant made a date that should remind 'confirm each visit', so no
    reminder could ever fire. Reminding is what 'expires N' does."""
    from app import settings_recommend as sr
    d = sr.PAGES["dependants"]["fields"]["field"]
    assert "A date that runs out is ALWAYS 'expires', gated or not" in d
    assert "is 'expires N' with no gate, never 'confirm'" in d
