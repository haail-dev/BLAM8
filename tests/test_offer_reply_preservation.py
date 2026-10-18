"""The deadline guard must leave the customer's actual email intact."""
import pytest

from app.offer_deadlines import enforce

LINE = "I'm holding these for you until 8pm tonight."
TIMES = ("Thanks for getting in touch. We have three slots tomorrow and Thursday: "
         "Monday morning, Thursday afternoon or Friday morning.")
REPLY = (f"Hi Holly,\n\n{TIMES}\n\n{LINE} Just reply with the one that suits."
         "\n\nKind regards,\nDan")


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_correct_reply_is_completely_unchanged(newline):
    reply = REPLY.replace("\n", newline)
    assert enforce(reply, [LINE]) == reply


@pytest.mark.parametrize("paraphrase", ["We'll keep them until tonight.",
                                       "These slots expire at 9pm tomorrow.",
                                       "I'm holding these for you until Friday."])
def test_paraphrased_deadline_is_replaced_without_reflow(paraphrase):
    assert enforce(REPLY.replace(LINE, paraphrase), [LINE]) == REPLY


def test_missing_deadline_goes_after_times_before_closing():
    reply = (f"Hi Holly,\n\n{TIMES}\n\nJust reply with the one that suits."
             "\n\nKind regards,\nDan")
    expected = reply.replace(TIMES, TIMES + "\n\n" + LINE)
    assert enforce(reply, [LINE]) == expected


def test_slots_tomorrow_and_non_deadline_hold_sentence_survive():
    paragraph = "We have two slots tomorrow. We can hold a slot for your appointment tomorrow."
    reply = f"Hi Holly,\n\n{paragraph}\n\nKind regards,\nDan"
    assert enforce(reply, [LINE]) == reply.replace(paragraph, paragraph + "\n\n" + LINE)


@pytest.mark.parametrize("closing", ["Kind regards,\nDan", "Thanks,\nDan", "Dan"])
def test_without_times_insert_before_signoff_or_bare_signature(closing):
    reply = f"Hi Holly,\n\nJust reply with the one that suits.\n\n{closing}"
    expected = f"Hi Holly,\n\nJust reply with the one that suits.\n\n{LINE}\n\n{closing}"
    assert enforce(reply, [LINE]) == expected


def test_missing_deadline_preserves_crlf_and_extra_paragraph_spacing():
    reply = f"Hi Holly,\r\n\r\n{TIMES}\r\n\r\n\r\nKind regards,\r\nDan\r\n"
    assert enforce(reply, [LINE]) == reply.replace(TIMES, TIMES + "\r\n\r\n" + LINE)


def test_partial_exact_lines_stay_in_place_without_duplication():
    second = "Thursday afternoon is held until 4:59pm tomorrow."
    reply = f"Hi Holly,\n\n{TIMES}\n\n{LINE}\n\nKind regards,\nDan"
    result = enforce(reply, [LINE, second])
    assert result == reply.replace(TIMES, TIMES + "\n\n" + second)
    assert result.count(LINE) == result.count(second) == 1


def test_standalone_wrong_deadline_reuses_its_paragraph():
    reply = f"Hi Holly,\n\n{TIMES}\n\nWe'll keep them until tonight.\n\nKind regards,\nDan"
    assert enforce(reply, [LINE]) == reply.replace("We'll keep them until tonight.", LINE)


@pytest.mark.parametrize("closing", ["Kind regards,\nDan", "Dan"])
def test_signature_without_blank_line_still_stays_last(closing):
    reply = f"Monday morning or Thursday afternoon.\n{closing}"
    result = enforce(reply, [LINE])
    assert result.endswith(closing)
    assert result.index(LINE) < result.index(closing)
