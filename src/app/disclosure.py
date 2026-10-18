"""EU AI Act Art 50(1) disclosure for AUTO-SENT outbound messages.

An auto-sent, AI-composed message IS the agent interacting directly with a
person, so it discloses itself (tenant_config.ai_disclosure, ON by default —
the Settings toggle shipped with the auto-reply footer, ebe61b7). Human-approved
sends never carry the forced footer: the deliberate approval makes the message
the business's own communication (docs/security/eu-ai-act-compliance.md §3).

Callers wrap ONLY the outbound copy — the stored email_log draft stays clean,
so the inbox shows what the agent wrote, not the compliance chrome.

One function, used by the reply pipeline, the missed-call text-back, and every
proactive job's auto branch (reminders, recalls, chases, specials, deposits),
so the wording can never drift between send paths.
"""


def wrap(cfg: dict | None, text: str, channel: str = "email", kind: str = "message") -> str:
    """Append the AI-disclosure footer to an AUTO-SENT outbound body.

    `kind` names the message in the footer ("reply" for inbound-triggered
    replies, the default "message" for proactive sends). SMS gets the one-line
    form — the email footer would double a text's length.
    """
    if not (cfg or {}).get("ai_disclosure", True):
        return text
    if channel == "sms":
        return text.rstrip() + f"\n(Automated {kind} from our AI assistant)"
    return (text.rstrip()
            + f"\n\n--\nThis automated {kind} was written by an AI assistant on our behalf.")
