"""Card cleaner stop-gaps from the first-walk re-run, 1 Oct 2026.

Incident 1: a repeatable row over 200 characters was cut mid-word, so its last
service name became a name that does not exist. Over-long values are now
rejected whole, never cut.
Incident 2: a value arrived with JSON bracket debris on its end
("... | only for: |}]}],") and was accepted as-is. Trailing debris is stripped.
"""
import logging

from app import settings_recommend as sr

PAGE = "booking_details"


def _one(name, value):
    return sr.clean(PAGE, [{"name": name, "value": value}])


def test_single_value_201_chars_is_dropped(caplog):
    with caplog.at_level(logging.WARNING):
        assert _one("stay_mode", "1" * 201) is None
    assert PAGE in caplog.text and "stay_mode" in caplog.text
    assert "1" * 50 not in caplog.text


def test_single_value_200_chars_is_kept():
    assert _one("stay_mode", "1" * 200) == [{"name": "stay_mode", "value": "1" * 200}]


def test_repeatable_row_350_chars_kept_whole():
    row = "Label | optional | hint | only for: " + "; ".join(f"Service {i}" for i in range(60))
    row = row[:350].rstrip("; ")
    row = row + "x" * (350 - len(row))
    assert len(row) == 350
    out = _one("field", row)
    assert out == [{"name": "field", "value": row}]


def test_repeatable_row_401_chars_dropped():
    assert _one("field", "L | optional | " + "a" * (401 - 15)) is None


def test_repeatable_row_400_chars_kept():
    row = "L | optional | " + "a" * (400 - 15)
    assert _one("field", row) == [{"name": "field", "value": row}]


def test_debris_tail_stripped_keeps_row():
    # Chosen form: the bracket/comma tail is removed and the row ends at the
    # last meaningful character, "only for: |" (the open 'only for' part stays).
    out = _one("field", "X | optional | hint | only for: |}]}],")
    assert out == [{"name": "field", "value": "X | optional | hint | only for: |"}]


def test_debris_with_space_and_quote_stripped():
    out = _one("field", 'Label | required | hint ", }] ')
    assert out == [{"name": "field", "value": 'Label | required | hint'}]


def test_only_debris_is_dropped():
    assert _one("field", "}]}],") is None
    assert _one("field", ' ]} , " ') is None


def test_normal_endings_untouched():
    for v in ("Label | required | A full stop.", 'Label | required | He said "hi"',
              "Label | required | one, two,", "Label | required | it's"):
        assert _one("field", v) == [{"name": "field", "value": v}]


def test_extra_page_value_over_limit_is_dropped_not_cut(caplog):
    from app import setup_held
    with caplog.at_level(logging.WARNING):
        out = setup_held.clean("voice", [{"name": "agent_name", "value": "w" * 201},
                                         {"name": "sign_off", "value": "Thanks"}])
    assert out == [{"name": "sign_off", "value": "Thanks"}]
    assert "voice" in caplog.text and "agent_name" in caplog.text and "w" * 50 not in caplog.text
    kept = setup_held.clean("voice", [{"name": "agent_name", "value": "w" * 200}])
    assert kept == [{"name": "agent_name", "value": "w" * 200}]


def test_stay_mode_in_words():
    assert sr.display("diary_settings", "stay_mode", "0") == "Booking type: no"
    assert sr.display("diary_settings", "stay_mode", "1") == "Booking type: yes"
    assert sr.display("diary_settings", "stay_mode", "maybe") is None


def test_prose_fields_have_room_for_a_paragraph():
    """Review of the reject-not-cut rule: payment terms of 255 characters and a
    576-character knowledge note are ordinary (both seen on 1 Oct 2026) and must
    survive whole; only something far beyond a paragraph is refused."""
    from app import settings_recommend as sr, setup_held as sh
    terms = "A" * 255
    assert sr.clean("invoice_settings", [{"name": "payment_terms", "value": terms}]) == [{"name": "payment_terms", "value": terms}]
    assert sr.clean("invoice_settings", [{"name": "payment_terms", "value": "A" * 601}]) is None
    note = "Label | " + "x" * 568
    assert sh.clean("knowledge", [{"name": "note", "value": note}])[0]["value"] == note
    assert not sh.clean("knowledge", [{"name": "note", "value": "Label | " + "x" * 1500}])
    assert sr.clean("invoice_settings", [{"name": "footer_note", "value": "F" * 400}])
    assert sh.clean("voice", [{"name": "tone", "value": "t" * 400}]) and not sh.clean("voice", [{"name": "tone", "value": "t" * 401}])
