"""Punt-while-missing guard (docs/todo-booking-missing-fields-checklist.md fix 2)."""
from app import pipeline

FIELDS = [{"label": "Postcode", "required": True}, {"label": "Vehicle reg", "required": True}]
PUNT = "Thanks for that. One of the team will be in touch shortly to confirm."


def test_punt_with_fields_missing_is_flagged():
    got = pipeline._punt_while_missing(PUNT, True, FIELDS, [])
    assert "Vehicle reg" in got


def test_punt_naming_the_variants():
    for txt in ("A colleague will confirm this.", "Someone will be in touch.",
                "We'll get back to you soon.", "A member of the team will contact you."):
        assert pipeline._punt_while_missing(txt, True, FIELDS, []), txt


def test_no_flag_when_everything_is_gathered():
    have = [{"label": "Postcode", "value": "OX1 1AA"}, {"label": "Vehicle reg", "value": "AB12 CDE"}]
    assert pipeline._punt_while_missing(PUNT, True, FIELDS, have) == []


def test_no_flag_when_asking_for_the_fields():
    ask = "To get you booked in I just need your postcode and vehicle reg."
    assert pipeline._punt_while_missing(ask, True, FIELDS, []) == []


def test_only_on_the_gather_path():
    assert pipeline._punt_while_missing(PUNT, False, FIELDS, []) == []
    assert pipeline._punt_while_missing(PUNT, True, [], []) == []
    assert pipeline._punt_while_missing("", True, FIELDS, []) == []


def test_inbound_wiring_reads_settings_without_a_client_name_error():
    import inspect
    src = inspect.getsource(pipeline.draft_for_email)
    assert "_PUNT_RX.search" in src and "punt_missing" in src
    assert "get_scheduling_config(tenant_id, client=client)" not in src.split("punt_missing =")[1][:400]
