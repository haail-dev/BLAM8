"""The product fact sheet — injected into the concierge as DATA, never baked into
its instructions.

This keeps the assistant's system instructions product-agnostic (src/app/concierge.py
carries behaviour rules only); the BLAM8-specific facts live here and are passed
in, the same way the email agent injects a tenant's voice + knowledge. Pricing is
NOT duplicated here — it comes from the single plan catalog (src/app/plans.py), so the
assistant can never quote a price that differs from what billing charges.
"""
from app import plans, slots

# What the product is + how it works + trust. Prose only — NO prices (those are
# rendered from the plan catalog by plans.pricing_blurb()).
PRODUCT_OVERVIEW = """\
BLAM8 is the inbox that books the job and fills the CRM — a UK subscription
service that gives a business an AI agent for its email. You connect your own
Outlook or Gmail mailbox in seconds, teach the agent your voice and facts in
minutes, and it runs one loop on every inbound email: it drafts the reply in
your voice from your real prices and policies, holds the slot so the diary
can't double-book, drops the confirmed job into Outlook or Google Calendar,
and writes the CRM — contact, company, deal, promises and follow-ups —
straight from the thread, with no typing. It is DRAFT-FIRST by default — a
human approves everything before it sends; auto-send is opt-in, per inbox.
Onboarding is configuration, never an IT project.

Capabilities follow actual module entitlements and configuration:
- Email: triage, drafts, business knowledge, voice and thread context.
- Scheduling: capacity, holds, services, configurable booking requirements and calendar sync.
- CRM: contacts, companies, deals, promises, follow-ups, documents and mood flags.
- The Concierge: the owner's typed or voice assistant for permitted business records,
  with supported actions behind confirmation cards. It is not a receptionist for callers.
  Pending cards and uncertain outcomes are not completed actions.
- App help: product explanations and supported settings, separate from business actions.
Exact plan entitlements and prices come only from the current catalogue below.
Trial access never enables an unreleased, disabled or restricted feature.

Industry Operating System modules (transport, trades, practices, shops, hire &
lettings, wholesale, events) exist in the product but are NOT on public sale —
they are being developed hands-on with founding partners. Never pitch them,
quote a price for them, or promise availability; if a visitor asks about that
kind of deeper operations tooling, say the team works on it with founding
partners directly and route them to hello@blam8.com.

Security & trust (built in): draft-first by design; row-level isolation per
company; mailbox tokens encrypted at rest; connected via Microsoft/Google OAuth
(never a password); your data is STORED in the UK (London database); the app is
hosted in the EU (Frankfurt); AI drafting is done by a US sub-processor (Anthropic)
under UK data-transfer safeguards — never say the whole service is "UK only"; the
full sub-processor list and their locations are public in the privacy policy; your
data is never used to train the AI. The agent reads each incoming email in full to
draft a reply, so any personal or sensitive details a customer includes (for
example bank details or health identifiers) are read as part of that message,
stored temporarily along with message and draft text (purged on a retention
schedule), and included in the content sent to the AI sub-processor for drafting.
BLAM8 does not separately extract or repurpose that data, but never claim it is
not stored or processed — it is, as part of the email. Businesses handling
payment, health or other special-category data should review the privacy policy
and raise compliance questions with hello@blam8.com before going live; escalation
keywords flag matching emails for mandatory human review (the agent still drafts,
but those replies never auto-send).

How it works: 1) start the free {{product:trial.days}}-day trial — the FULL product (Pro +
Concierge), released core features open, no card to start; 2) connect your mailbox in one
click — live on a real inbox in days, draft-only, with drafting held until the
owner has checked the setup and says go; 3) pick Starter, Pro or Pro +
Concierge, and add mailboxes as the team grows.

Not offered: BLAM8 does NOT answer phone calls — it answers email and runs the
diary. SMS and missed-call text-back are built and being tested; they switch on
when testing is done. Never say SMS is live, rolling out or has a date.

Any business can use BLAM8 — if a sector isn't named, it still gets the full core
platform; the agent is tailored by the knowledge and voice captured at onboarding.

Support: this assistant can raise a support ticket with the team right here in
the chat — it collects the details, the visitor reviews and approves the ticket,
and gets a short reference (like BLAM8-1042) to quote in any follow-up. Asking
the assistant "what's the status of BLAM8-1042?" (with the email the ticket was
raised under) returns the ticket's live status. Emailing hello@blam8.com also
works and is the fallback whenever the ticket system is unreachable.

Company details — the legal entity name, company registration number, VAT number,
registered office / postal address, and phone number — are NOT yet published.
Never state, invent, or promise any of them, and never commit that the team will
"send them over" (do not confirm which of them exist): the only correct answer is
that those details aren't published yet, and the visitor should email
hello@blam8.com where the team will help directly. For anything else not stated
here — a custom quote, or booking a call — likewise do not guess: say a colleague
will follow up via the contact route (hello@blam8.com)."""


def _signup_availability() -> str:
    """Live sign-up + Gmail-connect availability, rebuilt per call (src/app/slots.py
    caches the slot count ~60s). Sign-in is open to everyone; it's CONNECTING a
    Gmail mailbox that runs invite-only while Google's OAuth verification is
    pending. Without this section the assistant contradicts the connect step and
    tells invite-gated Gmail businesses that connecting is open."""
    s = slots.google_slots()
    if s["verified"]:
        return ("Signing up: click 'Get started' and sign in with a Microsoft or "
                "Google work account (secure OAuth — no password to remember). "
                "Both are open to everyone, and both Outlook and Gmail inboxes "
                "connect with no cap.")
    lines = [
        "Signing up: click 'Get started' and sign in with a Microsoft or Google "
        "work account (secure OAuth — no password to remember). Sign-in is OPEN "
        "TO EVERYONE — no invite, either provider.",
        "- Connecting a MICROSOFT 365 / Outlook mailbox is open to everyone: no "
        "invite code, no cap.",
        f"- Connecting a GMAIL mailbox is INVITE-ONLY right now, while Google "
        f"verifies our app: Google allows 100 founding Gmail connections until "
        f"then ({s['remaining']} of {s['cap']} left), reserved for invited "
        "founding users.",
    ]
    if slots.invite_gate_active():
        lines.append(
            "- With a Gmail invite code: enter it at the connect-your-inbox step "
            "(onboarding or Settings). Without one: use 'Request access' on that "
            "same step, or connect a Microsoft mailbox instead — same product, "
            "no wait.")
    lines.append(
        "- Businesses with Gmail ALREADY connected keep working as normal — the "
        "invite gate only applies to new Gmail connections.")
    return "\n".join(lines)


def sheet() -> str:
    """The full fact sheet injected into the concierge: product overview + live
    sign-up availability + the canonical pricing (single-sourced from the plan
    catalog)."""
    from app.platform_public import resolve
    return (resolve(PRODUCT_OVERVIEW) + "\n\n" + _signup_availability()
            + "\n\n" + plans.pricing_blurb())
