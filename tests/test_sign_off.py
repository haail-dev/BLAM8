"""The configured email sign-off is applied by code (src/app/sign_off.py).

Pure module, no I/O. Pins: the stored value is normalised (CRLF, trailing
whitespace, the ' / ' alternatives case); the sign-off lands exactly once and is
idempotent; a closing the model wrote anyway is replaced, but real content is never
deleted; empty text (a hold), SMS and an unconfigured sign-off pass through.

Run: python -m pytest tests/test_sign_off.py -q
"""
import pytest

from app import sign_off as so

TEAM_CRLF = "Best regards,\r\nThe Example Team \r\nhello@example.invalid\r\n"
TEAM = "Best regards,\nThe Example Team\nhello@example.invalid"
ONE_LINE = "Alex, the team"
TWO_ALT = "Many thanks,\nSam / Kind regards,\nSam Jones"
PLAIN = "Thanks"

BODY = "Hi Sam,\n\nTuesday works."

# Realistic (text, configured sign-off) pairs used by the idempotence check.
TAILS = [
    (BODY, ONE_LINE),
    (BODY, TEAM_CRLF),
    (BODY + "\n\n" + ONE_LINE, ONE_LINE),
    (BODY + "\r\n\r\n" + TEAM_CRLF, TEAM_CRLF),
    (BODY + "\n\nThanks,\nSam", ONE_LINE),
    (BODY + "\n\nWarm regards,\nThe Example Team", TEAM_CRLF),
    (BODY + "\n\nKind regards,\nSam\nExample & Sons Services", "Cheers,\nSam\nExample & Sons Services"),
    (BODY + "\n\nCheers,", ONE_LINE),
    (BODY, TWO_ALT),
    (BODY + "\n\nThanks for your message.", PLAIN),
    (BODY + "\n\nThanks", PLAIN),
    ("Thanks for your patience — we'll be in touch Monday.", ONE_LINE),
]


def test_normalise_crlf_trailing_space_and_blank_lines():
    assert so.normalise(TEAM_CRLF) == TEAM
    assert so.normalise("A\n\n\n\nB") == "A\n\nB"
    assert so.normalise("A\r\rB") == "A\n\nB"
    assert so.normalise(None) == ""
    assert so.normalise("  \r\n ") == ""


def test_normalise_keeps_the_first_of_two_alternatives():
    assert so.normalise(TWO_ALT) == "Many thanks,\nSam"
    # A slash that is part of a name is kept: the part after it is not a closing.
    assert so.normalise("Sales / Support team") == "Sales / Support team"


def test_a_reply_body_is_never_split_on_a_slash():
    """normalise() cuts alternatives out of a stored sign-off value; a reply body
    that happens to contain ' / best ...' or ' / thanks' must keep every word."""
    body = "We have a standard / best value option.\n\nI can book it for Tuesday."
    out, applied = so.apply(body, {"sign_off": ONE_LINE})
    assert out == body + "\n\n" + ONE_LINE
    assert applied == ONE_LINE
    assert so.ends_with(body + "\n\n" + ONE_LINE, ONE_LINE)


def test_apply_appends_after_one_blank_line():
    assert so.apply("Hi Alex,\n\nTuesday works.", {"sign_off": ONE_LINE}) == (
        "Hi Alex,\n\nTuesday works.\n\n" + ONE_LINE, ONE_LINE)


def test_apply_normalises_the_stored_value_before_appending():
    out, applied = so.apply(BODY, {"sign_off": TEAM_CRLF})
    assert out == BODY + "\n\n" + TEAM
    assert applied == TEAM
    out, applied = so.apply(BODY, {"sign_off": TWO_ALT})
    assert out == BODY + "\n\nMany thanks,\nSam"
    assert applied == "Many thanks,\nSam"
    assert "\r" not in out


@pytest.mark.parametrize("text,sign_off", TAILS)
def test_apply_is_idempotent(text, sign_off):
    cfg = {"sign_off": sign_off}
    once, applied = so.apply(text, cfg)
    twice, applied_again = so.apply(once, cfg)
    assert twice == once
    assert applied_again == applied
    assert once.endswith(so.normalise(sign_off))


def test_apply_leaves_an_exact_sign_off_alone():
    cfg = {"sign_off": TEAM_CRLF}
    for text in (BODY + "\n\n" + TEAM, BODY + "\r\n\r\n" + TEAM_CRLF):
        out, applied = so.apply(text, cfg)
        assert out.endswith("\n\n" + TEAM)
        assert out.count(TEAM) == 1
        assert applied == TEAM


@pytest.mark.parametrize("text,sign_off,expected_tail,gone", [
    (BODY + "\n\nThanks,\nSam", ONE_LINE, "\n\n" + ONE_LINE, "Thanks,"),
    (BODY + "\n\nWarm regards,\nThe Example Team", TEAM_CRLF, "\n\n" + TEAM, "Warm regards"),
    (BODY + "\n\nKind regards,\nSam\nExample & Sons Services",
     "Cheers,\nSam\nExample & Sons Services", "\n\nCheers,\nSam\nExample & Sons Services",
     "Kind regards"),
    (BODY + "\n\nCheers,", ONE_LINE, "\n\n" + ONE_LINE, "Cheers,"),
    (BODY + "\n\nThanks!", ONE_LINE, "\n\n" + ONE_LINE, "Thanks!"),
    # The one-line shapes ("Thanks, Sam") and the other common phrases (review of 15 Oct).
    (BODY + "\n\nThanks, Sam", ONE_LINE, "\n\n" + ONE_LINE, "Thanks, Sam"),
    (BODY + "\n\nBest, Sam", ONE_LINE, "\n\n" + ONE_LINE, "Best, Sam"),
    (BODY + "\n\nBest, Sam Jones", ONE_LINE, "\n\n" + ONE_LINE, "Sam Jones"),
    (BODY + "\n\nCheers, The Example Team", ONE_LINE, "\n\n" + ONE_LINE, "The Example Team"),
    (BODY + "\n\nThanks so much,\nSam", ONE_LINE, "\n\n" + ONE_LINE, "Thanks so much"),
    (BODY + "\n\nThank you so much,\nSam", ONE_LINE, "\n\n" + ONE_LINE, "Thank you so much"),
    (BODY + "\n\nMany thanks,\nSam", ONE_LINE, "\n\n" + ONE_LINE, "Many thanks"),
    (BODY + "\n\nWarmly,\nSam", ONE_LINE, "\n\n" + ONE_LINE, "Warmly"),
    (BODY + "\n\nSincerely,\nSam", ONE_LINE, "\n\n" + ONE_LINE, "Sincerely"),
    (BODY + "\n\nKindest regards,\nSam", ONE_LINE, "\n\n" + ONE_LINE, "Kindest regards"),
    (BODY + "\n\nBest wishes,\nSam", ONE_LINE, "\n\n" + ONE_LINE, "Best wishes"),
    (BODY + "\n\nCheers,\nSam Jones\nExample Team", ONE_LINE, "\n\n" + ONE_LINE, "Example Team"),
    (BODY + "\n\nKind regards,\nSam\nAcme Ltd.", ONE_LINE, "\n\n" + ONE_LINE, "Acme Ltd."),
    # A labelled contact line after the closing is part of the signature.
    (BODY + "\n\nBest regards,\nSam\nTel: 01234 567890", ONE_LINE, "\n\n" + ONE_LINE, "01234"),
    (BODY + "\n\nBest regards,\nSam\nEmail: sam@example.invalid", ONE_LINE, "\n\n" + ONE_LINE,
     "sam@example.invalid"),
    (BODY + "\n\nBest regards,\nSam\nTel. 01234 567890", ONE_LINE, "\n\n" + ONE_LINE, "01234"),
])
def test_apply_strips_a_model_written_closing(text, sign_off, expected_tail, gone):
    out, _ = so.apply(text, {"sign_off": sign_off})
    assert out.endswith(expected_tail)
    assert out.startswith(BODY)
    assert gone not in out


def test_apply_keeps_an_exact_match_instead_of_stripping_it():
    text = BODY + "\n\nBest regards,\nThe Example Team\nhello@example.invalid"
    out, applied = so.apply(text, {"sign_off": TEAM_CRLF})
    assert out == text
    assert applied == TEAM


@pytest.mark.parametrize("text", [
    "Thanks for your patience — we'll be in touch Monday.",
    "Many thanks for the photos.\nWe open at 9.",
    "Thanks,\nPlease bring:\nYour ID\nProof of address",
    "Thanks,",
    "Hello Sam,\n\nThanks,\nA\nB\nC\nD",
    "Hello Sam,\n\nThanks,\nWe look forward to seeing you again very soon",
    "Hello Sam,\n\nThanks,\nSee you at 9.",
    # The five shapes the 15 Oct review showed being deleted: short lines with no full stop.
    "All booked for Tuesday.\n\nThanks,\nSam\nP.S. Bring your ID",
    "All set.\n\nThanks,\nSee you Tuesday at 10",
    "All set.\n\nThanks,\nCall us on 01234 567890",
    "All set.\n\nThanks,\nPlease bring your ID",
    "Please bring\n\nThanks,\nYour ID\nProof of address",
    # A postscript or a note is something the writer meant to say, however short.
    "All set.\n\nThanks,\nP.S. Bring your ID",
    "All set.\n\nThanks,\nPS bring your ID",
    "All set.\n\nThanks,\nP.P.S. Bring your ID",
    "All set.\n\nThanks,\nNB Bring your ID",
    "All set.\n\nThanks,\nN.B. Bring your ID",
    "All set.\n\nThanks,\nSam\nP.S. Bring your ID",
    # Lines that look like names but open like a sentence, or carry digits or a day.
    "All set.\n\nThanks,\nYour ID",
    "All set.\n\nThanks,\nOur Team Will Call",
    "All set.\n\nThanks,\nMonday",
    "All set.\n\nThanks,\nRoom 4",
    "All set.\n\nThanks,\nSam\nacme limited",
    # A link the closing block does not repeat is never the thing that goes missing.
    "All set.\n\nThanks,\nSam\nhttps://example.invalid/pay/1",
    "All set.\n\nThanks,\nhttps://example.invalid/pay/1",
    # More than three lines after a closing is not a signature.
    "All set.\n\nThanks,\nSam\nAcme Ltd\nTel: 01234 567890\nEmail: sam@example.invalid",
])
def test_apply_never_deletes_content(text):
    out, applied = so.apply(text, {"sign_off": ONE_LINE})
    assert out == so._tidy(text) + "\n\n" + ONE_LINE
    assert applied == ONE_LINE


def test_apply_is_a_no_op_for_sms_empty_and_unconfigured():
    cfg = {"sign_off": ONE_LINE}
    assert so.apply("Hi", cfg, "sms") == ("Hi", "")
    assert so.apply("", cfg) == ("", "")
    assert so.apply("   ", cfg)[1] == ""
    assert so.apply(None, cfg) == ("", "")
    assert so.apply("Hi", {}) == ("Hi", "")
    assert so.apply("Hi", None) == ("Hi", "")
    assert so.apply("Hi", {"sign_off": " \r\n"}) == ("Hi", "")
    assert so.apply("Hi", {"sign_off": None}) == ("Hi", "")


def test_a_closing_word_as_the_sign_off_still_appends_after_a_sentence():
    # "Thanks" configured: a reply ending "Thanks for your message." is not a
    # closing line, so the sign-off is appended; a reply already ending "Thanks"
    # is left alone.
    assert so.apply("Got it.\n\nThanks for your message.", {"sign_off": PLAIN})[0] == (
        "Got it.\n\nThanks for your message.\n\nThanks")
    assert so.apply("Got it.\n\nThanks", {"sign_off": PLAIN})[0] == "Got it.\n\nThanks"


def test_ends_with_compares_normalised_forms():
    assert so.ends_with("Hi\r\n\r\n" + TEAM_CRLF, TEAM)
    assert so.ends_with(TEAM, TEAM_CRLF)
    assert not so.ends_with("Hi", "")
    assert not so.ends_with("Hi\n\nThanks Sam", "Sam")


# ── The configured block is the only thing that can end up doubled or missing ───
BLOCK = "Kind regards,\nLeah\nTel: 01234 567890"


def test_a_closing_that_repeats_the_configured_contact_lines_is_not_doubled():
    """The model's own copy of a multi-line sign-off, down to the labelled lines, is
    replaced by the configured block exactly once (review of 15 Oct, shape 6)."""
    cfg = {"sign_off": BLOCK}
    out, _ = so.apply("Tuesday works.\n\nBest regards,\nLeah\nTel: 01234 567890", cfg)
    assert out == "Tuesday works.\n\n" + BLOCK
    assert out.count("Tel: 01234 567890") == 1 and out.count("Leah") == 1
    # The same when the model's last line is a web address the configured block carries.
    cfg = {"sign_off": "Kind regards,\nLeah\nhttps://example.invalid"}
    out, _ = so.apply("Tuesday works.\n\nBest,\nLeah\nhttps://example.invalid", cfg)
    assert out == "Tuesday works.\n\nKind regards,\nLeah\nhttps://example.invalid"
    # ...or one the reply already gave above: nothing is lost by dropping the copy.
    cfg = {"sign_off": "Kind regards,\nLeah"}
    text = "Pay here: https://example.invalid/pay/1\n\nThanks,\nSam\nhttps://example.invalid/pay/1"
    out, _ = so.apply(text, cfg)
    assert out == "Pay here: https://example.invalid/pay/1\n\nKind regards,\nLeah"


def test_a_sign_off_already_followed_by_a_postscript_or_a_link_is_not_added_twice():
    cfg = {"sign_off": "Kind regards,\nLeah"}
    for tail in ("\n\nP.S. Bring your ID.", "\nhttps://example.invalid/pay/1",
                 "\nOffice hours 9 to 5", "\n\nP.S. Bring your ID\nTa"):
        text = "Tuesday works.\n\nKind regards,\nLeah" + tail
        out, applied = so.apply(text, cfg)
        assert out == text and out.count("Kind regards,") == 1 and applied == "Kind regards,\nLeah"
        assert so.apply(out, cfg)[0] == out                      # idempotent
    # Further than the last eight lines from the end: it is not "already there".
    far = "Kind regards,\nLeah\n" + "\n".join("Line %d" % n for n in range(9))
    out, _ = so.apply("Tuesday works.\n\n" + far, cfg)
    assert out.endswith("\n\nKind regards,\nLeah") and out.count("Kind regards,") == 2
    # A one-word sign-off that is the model's own closing line is still replaced, name and all.
    assert so.apply("Done.\n\nThanks\n\nSam", {"sign_off": "Thanks"})[0] == "Done.\n\nThanks"


def test_strip_closing_defaults_keep_every_line_it_cannot_prove_is_a_signature():
    text = "Done.\n\nThanks,\nSam\nCall us on Monday"
    assert so.strip_closing(text) == text                         # unchanged: not a signature
    assert so.strip_closing("Done.\n\nThanks,\nSam") == "Done."
    assert so.strip_closing("Done.\n\nThanks, Sam") == "Done."
    assert so.strip_closing("Thanks, Sam") == "Thanks, Sam"       # nothing before it


def test_a_phrase_that_only_starts_like_a_closing_is_not_one():
    for line in ("Thanks, Sam is on his way", "Thanks, sam", "Best of luck", "Cheers, everyone",
                 "Thanks, Sam Jones Brown Smith", "Many thanks for the photos", "Warmly welcome"):
        text = "Done.\n\n" + line
        assert so.strip_closing(text) == text, line


# ── Speed: nothing here may be slow on hostile text (a model reply is untrusted) ─
def _slowest_ms(fn, *args):
    import time
    best = None
    for _ in range(3):
        start = time.perf_counter()
        fn(*args)
        took = (time.perf_counter() - start) * 1000
        best = took if best is None else min(best, took)
    return best


HOSTILE = {
    "spaces": " " * 10000,
    "newlines": "\n" * 10000,
    "commas": "," * 10000,
    "crlf": "\r\n" * 5000,
    "thanks repeated": "thanks " * 2000,
    "thanks comma repeated": "thanks, " * 2000,
    "thanks lines": "thanks\n" * 2000,
    "slash thanks": " / thanks" * 1500,
    "slash thanks lines": "Sam / Thanks,\n" * 1000,
    "one long line": "word " * 2000,
    "one long line of dots": "a." * 5000,
    "closing then 10k tail": "Hello.\n\nThanks,\n" + "Sam " * 2500,
    "closing then 10k spaces": "Hello.\n\nThanks," + " " * 10000 + "x",
    "closing comma name 10k": "Hello.\n\nThanks, " + "A" * 10000,
    "capitals": "A " * 5000,
    "tel labels": "Tel: 1\n" * 2000,
    "urls": "https://example.invalid/" + "a" * 10000,
}


@pytest.mark.parametrize("name", sorted(HOSTILE))
def test_hostile_text_is_handled_in_milliseconds(name):
    text = HOSTILE[name]
    cfg = {"sign_off": "Kind regards,\nLeah"}
    assert _slowest_ms(so.apply, text + "\n\nThanks,\nSam", cfg) < 50
    assert _slowest_ms(so.apply, text, cfg) < 50
    assert _slowest_ms(so.strip_closing, text + "\n\nThanks,\nSam", "Kind regards,\nLeah") < 50
    assert _slowest_ms(so.normalise, text) < 50
    assert _slowest_ms(so.ends_with, text, "Kind regards,\nLeah") < 50


# ── A " / " in a stored sign-off is cut only before a bare closing LINE ────────
@pytest.mark.parametrize("value", [
    "Alex / Best Price Removals",
    "Sam / Cheers Bar",
    "Sam / best@example.invalid",
    "Alex / Thanks Brothers Ltd",
    "Sam / Best, Price Removals",
    "Sales / Support team",
    "Cheers,\nSam / Example Team",
])
def test_a_slash_that_is_part_of_a_name_is_kept_whole(value):
    assert so.normalise(value) == value
    assert so.apply(BODY, {"sign_off": value})[0] == BODY + "\n\n" + value
    assert so.normalise(so.normalise(value)) == so.normalise(value)


@pytest.mark.parametrize("value,expected", [
    ("Many thanks,\nKristie / Kind regards,\nKristie Hughes", "Many thanks,\nKristie"),
    ("Many thanks,\r\nKristie / Kind regards,\r\nKristie Hughes", "Many thanks,\nKristie"),
    ("Cheers / Thanks", "Cheers"),
    ("Sam / Best regards", "Sam"),
    ("Sam / Warmly,\nSam Jones", "Sam"),
])
def test_two_alternative_closings_still_reduce_to_the_first(value, expected):
    assert so.normalise(value) == expected
