"""Raw tool-call markup in model text — never a reply.

Live 2026-09-29 (Concierge, typed): a model turn that meant to call a lookup
came back as plain TEXT — `<invoke name="list_bookings"><parameter
name="contact">…</parameter>…` — with stop_reason "tool_use" but no tool_use
block. The typed loop took a tool-less text turn as the reply, so the owner saw
the markup. One shared detector, used wherever model text can become something
a person reads or hears (owner replies, customer drafts)."""
import re

_MARKUP = re.compile(
    r"</?\s*(?:antml:)?(?:invoke|parameter|function_calls?|tool_calls?|tool_use|"
    r"tool_results?|function_results?)\b",
    re.IGNORECASE)


def has_tool_markup(text) -> bool:
    """True when `text` carries tool-call / function-call XML markup."""
    return isinstance(text, str) and bool(_MARKUP.search(text))


def any_tool_markup(value) -> bool:
    """`has_tool_markup` over every string inside a str / list / dict."""
    if isinstance(value, str):
        return has_tool_markup(value)
    if isinstance(value, dict):
        return any(any_tool_markup(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(any_tool_markup(v) for v in value)
    return False


# Sent back to the model once, in place of a reply that was markup.
CORRECTION = ("[system] Your last message contained raw tool-call markup as text "
              "instead of making the call. Never write tool calls as text: call "
              "the tool properly if you still need it, otherwise deliver your "
              "finished answer to the owner in plain sentences.")

# Shown when the corrective retry is also markup and no tool ran this turn.
HONEST_FAILURE = ("I couldn't finish that one — something went wrong on my side. "
                  "Nothing was sent or changed. Could you ask it again?")

# Shown when tools already executed this turn (a mutation may have happened),
# so "Nothing was sent or changed" would be a false claim.
HONEST_FAILURE_AFTER_TOOLS = ("I couldn't finish that one — something went wrong "
                              "on my side. Could you check what happened and ask "
                              "again if needed?")
