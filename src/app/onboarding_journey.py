"""Application-owned wizard order and action descriptions; no execution authority."""
import re

STEPS = ("interview", "mailbox", "voice", "knowledge")
LABELS = {"interview": "About you", "mailbox": "Customer inbox", "voice": "Voice", "knowledge": "Knowledge"}


def describe(step, *, connected=None, saved=None, next_target=None):
    step = "interview" if step == "industry" else step
    if step not in STEPS:
        return None
    index = STEPS.index(step)
    next_step = STEPS[index+1] if index+1 < len(STEPS) else None
    target = ({"label": LABELS[next_step], "href": "/onboarding/"+next_step} if next_step else
              next_target or {"label": "the application's remaining tuning steps", "href": None})
    return {"phase": "initial_wizard", "current": step, "current_label": LABELS[step],
            "order": [{"id": s, "label": LABELS[s]} for s in STEPS],
            "subsequent_phase": "The application's tuning steps follow Knowledge; they do not precede Voice.",
            "saved_completion": saved or {}, "connected": connected, "next": target,
            "effect": ("Open Voice for review; do not start sent-email learning." if step == "mailbox" else
                       "Submit the reviewed form using its normal validation; this does not connect a mailbox." if step == "interview" else
                       "Save the reviewed voice choices; continue to Knowledge." if step == "voice" else
                       "Continue only through the existing Knowledge review and pending-draft safeguards."),
            "requires_click": True}


def is_navigation_request(message):
    text = message.casefold()
    if re.search(r"\b(fill|change|correct|rewrite|set|add|replace|remove)\b", text):
        return False
    return bool(re.search(r"\b(take me|next (step|page)|please continue|save (it|this|that)|move (on|to)|go (on|to))\b", text))


def navigation_reply(journey):
    current, target = journey["current_label"], journey["next"]["label"]
    if journey["current"] == "mailbox" and journey["connected"] is not True:
        return ("You are on Customer inbox. Voice is next after a mailbox is connected. "
                "I cannot verify an available Continue action for this connection. You can pause here without connecting.")
    return (f"You are on {current}. Next is {target}. Review this page, then use its Continue action "
            f"when you choose. {journey['effect']} Nothing is submitted by this reply.")
