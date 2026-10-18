from app.textfmt import to_plain_text


def test_strips_bold_and_keeps_content():
    assert to_plain_text("Your reference is **BK-1020**.") == "Your reference is BK-1020."
    assert to_plain_text("A __£20 deposit__ secures it.") == "A £20 deposit secures it."


def test_bullets_normalised_to_dash():
    md = "Here's a summary:\n* 4 July\n+ 11 July\n- 18 July"
    assert to_plain_text(md) == "Here's a summary:\n- 4 July\n- 11 July\n- 18 July"


def test_headings_stripped():
    assert to_plain_text("## Booking reference: BK-1024") == "Booking reference: BK-1024"


def test_links_and_code_and_strike():
    assert to_plain_text("See [our site](https://x.io) now") == "See our site (https://x.io) now"
    assert to_plain_text("call `book()` please") == "call book() please"
    assert to_plain_text("~~old price~~ new") == "old price new"


def test_italic_emphasis_only_not_maths_or_identifiers():
    assert to_plain_text("that's *really* good") == "that's really good"
    # maths and bare asterisks are left alone (spaces inside the pair)
    assert to_plain_text("£22/night * 2 dogs * 3 nights") == "£22/night * 2 dogs * 3 nights"


def test_plain_text_is_byte_identical():
    s = "Hi Dave,\n\nThe MOT is £54.85 (guide price). See you Tuesday!\n\nCheers,\nThe team"
    assert to_plain_text(s) == s


def test_none_and_empty():
    assert to_plain_text(None) == ""
    assert to_plain_text("") == ""
