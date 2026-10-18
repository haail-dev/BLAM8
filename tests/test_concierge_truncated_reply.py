"""A structured concierge reply cut at max_tokens must never reach the panel as
raw JSON (live 2026-09-04: the diary recommendation and a knowledge card both
rendered as braces). The parsers salvage the readable reply prefix."""
from app import concierge


TRUNCATED = ('{"reply":"Perfect — here are the diary settings I\'d recommend.",'
             '"nav":"diary_settings","recommend":[{"page":"diary_settings",'
             '"values":[{"name":"open_time","value":"08:00"},{"name":"serv')


def test_parse_help_salvages_the_reply_prefix():
    reply, nav, submit, recommend = concierge._parse_help(TRUNCATED)
    assert reply.startswith("Perfect — here are the diary settings")
    assert reply.endswith("…")
    assert "{" not in reply and '"reply"' not in reply
    assert nav == "" and submit is None and recommend is None


def test_salvage_returns_none_without_a_reply():
    assert concierge._salvage_reply('{"nav":"diary_settings","recommend":[') is None
    assert concierge._salvage_reply("plain prose, no json") is None


def test_salvage_unescapes_and_keeps_a_closed_reply_plain():
    text = '{"reply":"Line one\\nsays \\"hi\\"","nav":""}'
    assert concierge._salvage_reply(text) == 'Line one\nsays "hi"'


def test_help_and_onboarding_calls_carry_the_wider_cap():
    import inspect
    src = inspect.getsource(concierge)
    for op in ("concierge_onboard", "concierge_help"):
        i = src.index(f'operation="{op}",')
        assert "max_tokens=4000" in src[i:i + 250], op
