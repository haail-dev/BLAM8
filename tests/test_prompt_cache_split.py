"""The setup assistants' prompts cache their fixed half (run log F2, plumber
take 2026-09-25: onboarding wrote ~9.8K tokens of cache every turn and read
none — the page's field values sat in the middle of the system prompt — and
the in-app help read the cache on 1 of 5 setup pages).

Run: python -m pytest tests/test_prompt_cache_split.py -q
"""
from app import concierge, llm


def test_onboarding_puts_what_changes_after_the_cached_half():
    one = concierge.onboarding_system("knowledge", {"business_name": "Hartwell & Daughters"}, held=[])
    two = concierge.onboarding_system("knowledge", {"business_name": "Hartwell & Daughters",
                                                    "tone": "friendly"}, held=[])
    assert one[0]["cache_control"] == {"type": "ephemeral"}
    assert one[0]["text"] == two[0]["text"]                   # the same prefix turn after turn
    assert "Hartwell" not in one[0]["text"] and "Hartwell" in one[1]["text"]
    # The single-string form is the same words, for tests and logs.
    assert concierge.build_onboarding_prompt("knowledge", {"business_name": "X"}) == \
        "\n\n".join(b["text"] for b in concierge.onboarding_system("knowledge", {"business_name": "X"}))


def test_help_puts_the_customer_and_the_retrieved_help_after_the_cached_half():
    a = concierge.help_system(facts="F", knowledge="one answer", tenant={"name": "Hartwell"})
    b = concierge.help_system(facts="F", knowledge="another answer", tenant={"name": "Brightwell"})
    assert a[0]["text"] == b[0]["text"] and a[0]["cache_control"] == {"type": "ephemeral"}
    assert "one answer" in a[1]["text"] and "Hartwell" in a[1]["text"]


def test_the_block_system_is_passed_through_untouched(monkeypatch):
    monkeypatch.setattr(llm.settings, "llm_prompt_cache", True)
    blocks = concierge.onboarding_system("voice", {}, held=[])
    out = llm._cached_kwargs({"system": blocks, "messages": [{"role": "user", "content": "hi"}]})
    assert out["system"] is blocks                               # no second stamp on the system
