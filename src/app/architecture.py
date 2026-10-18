"""Content for the public /architecture page (src/app/templates/architecture.html).

The canvas itself is drawn client-side by src/app/static/architecture.js from its
own copy of this data (nodes also carry grid positions, icons and edge lists
there). This module is the server-rendered truth: the template loops over
GROUPS to emit the crawlable/screen-reader fallback beneath the canvas, the
same way src/app/help_center.py feeds /help.

tests/test_architecture_page.py cross-checks every label and sublabel here
against architecture.js, so the two copies cannot drift silently. When a node
changes, change it in both places — the test will hold the door.

Each node is (label, sublabel, badge) — badge is the little status pill shown
on the canvas ("human", "planned", …), or "" for none.
"""

GROUPS: list[dict] = [
    {"title": "Inbound", "tagline": "four ways in", "nodes": [
        ("Graph notification", "constant-time clientState", ""),
        ("Gmail push", "ACK 204, draft after", ""),
        ("Gmail poller", "shares the push backoff", ""),
        ("Twilio voice + SMS", "HMAC-SHA1 verified", ""),
        ("You press draft", "never auto-sends", ""),
    ]},
    {"title": "Guards", "tagline": "before a single token is spent", "nodes": [
        ("No-reply / bounce", "mailer-daemon · postmaster", ""),
        ("Payment receipt", "links invoice, stops", ""),
        ("Ignored contact", "dropped entirely", ""),
        ("Self-loop guard", "own + sibling mailboxes", ""),
        ("Suppression", "3 ignored drafts → stop", ""),
        ("Trial gate", "stops before the spend", ""),
    ]},
    {"title": "Context", "tagline": "what it is allowed to know", "nodes": [
        ("Effective config", "tenant ⊕ mailbox", ""),
        ("Knowledge retrieval", "Voyage · pgvector", ""),
        ("Thread history", "provider thread scoped", ""),
        ("Their bookings", "reference, don’t re-offer", ""),
        ("CRM context", "deals · open promises", ""),
        ("Portal activity", "what they did on your site", ""),
        ("Stock + purchases", "storefront tenants", ""),
        ("Credit limit", "arithmetic, not vibes", ""),
        ("Industry vocabulary", "config, not code", ""),
        ("Tool injection", "active modules only", ""),
    ]},
    {"title": "The draft", "tagline": "one call, four refusals", "nodes": [
        ("Structured output", "schema-enforced", ""),
        ("Tool loop", "diary · handoff · shop", ""),
        ("Corrective retry", "one, then it gives up", ""),
        ("Say-vs-do guard", "“booked” with no hold", ""),
        ("Knowledge gap", "admits it, flags it", ""),
        ("Empty after retry", "queues with no text", ""),
        ("Markdown stripped", "plain text out", ""),
    ]},
    {"title": "Label", "tagline": "computed truth overrides the model", "nodes": [
        ("Model category", "needs reply · fyi · auto", ""),
        ("Computed override", "escalation wins", ""),
        ("Informational → skipped", "no queue, no push", ""),
        ("Legacy derived", "at read time", ""),
    ]},
    {"title": "Diary and holds", "tagline": "the only thing that can say yes", "nodes": [
        ("15-minute grid", "96 blocks a day", "source of truth"),
        ("Window modes", "all-day · am/pm · slots", ""),
        ("Required intake", "no hold until answered", ""),
        ("Atomic capacity RPC", "six variants, Postgres-side", ""),
        ("Hold placed", "proposed, linked to draft", ""),
        ("Duplicate webhook", "this run’s holds cancelled", ""),
        ("Offered slots", "first to accept wins", ""),
        ("Pre-expiry nudge", "every 30 min", ""),
        ("Expiry sweep", "capacity comes back", ""),
        ("Reference minted", "Postgres, not the model", ""),
    ]},
    {"title": "Approval", "tagline": "the gate", "nodes": [
        ("Send", "as-is or edited", "human"),
        ("Skip", "releases the hold", ""),
        ("Redraft", "", ""),
        ("Correct", "teaches the chunk", ""),
        ("Flag a gap", "files it against the KB", ""),
        ("Grounded-on", "shows its sources", ""),
    ]},
    {"title": "Auto-send", "tagline": "off by default", "nodes": [
        ("Two axes required", "config AND caller", ""),
        ("Tri-state per mailbox", "orders@ auto, complaints@ never", ""),
        ("Escalation keyword", "forces the queue", ""),
        ("Angry sentiment", "held, Core and up", ""),
        ("Dup · sibling · credit", "held", ""),
        ("EU AI Act footer", "auto-sent only", ""),
    ]},
    {"title": "After it goes", "tagline": "the part that fills itself in", "nodes": [
        ("Holds confirmed", "proposed → booked", ""),
        ("Calendar push", "only its own events", ""),
        ("CRM ingest, zero LLM", "every tier, even Starter", ""),
        ("Deals clustered", "one Haiku call", ""),
        ("Promises both ways", "yours and theirs", ""),
    ]},
    {"title": "Quote to cash", "tagline": "a second money spine", "nodes": [
        ("Quote auto-drafts", "from the priced reply", ""),
        ("Public accept page", "accept · decline · ask", ""),
        ("Chase job", "day 3, day 7, expiry", ""),
        ("Invoice + PDF", "deposit netted off", ""),
        ("Xero / QuickBooks", "integrate, don’t compete", ""),
    ]},
    {"title": "Money", "tagline": "three rails, three accounts", "nodes": [
        ("Subscriptions", "platform · hosted Checkout", ""),
        ("They get paid", "direct charge, their account", ""),
        ("Booking deposits", "platform account", "known gap"),
        ("Commission ledger", "earned = collected", ""),
        ("Refund clawback", "automatic", ""),
        ("Partner payout", "bank transfer, not Stripe", ""),
    ]},
    {"title": "Seeing what it did", "tagline": "four logs", "nodes": [
        ("Every model call", "prompt + tool rounds", ""),
        ("Execution trace", "proposals · retrievals", ""),
        ("Your agent log", "tenant-facing narrative", ""),
        ("Cost metering", "per call, exportable", ""),
        ("Retention purge", "90 days, 03:30", ""),
    ]},
    {"title": "Your customer’s side", "tagline": "no login", "nodes": [
        ("Portal at /c/you", "public, no login", ""),
        ("Identity", "ref code, or name + email", ""),
        ("Same brain, their side", "model never sets tenant", ""),
        ("Reschedule mode", "apply now, or ask you", ""),
    ]},
    {"title": "Concierge", "tagline": "five modes, one engine", "nodes": [
        ("Pre-login Q&A", "rate-limited, stateless", ""),
        ("Onboarding co-pilot", "", ""),
        ("Setup help", "", ""),
        ("Diagnose and repair", "", ""),
        ("Business questions", "fixed data catalogue", ""),
        ("Proposal re-validated", "ids owned, windows fit", ""),
        ("Single-use card", "five-minute expiry", "human"),
        ("Support ticket", "secrets redacted", ""),
    ]},
    {"title": "Three more personas", "tagline": "handoff, keeps your voice", "nodes": [
        ("Transport specialist", "ask_transport handoff", ""),
        ("Engineer desk", "the trades twin", ""),
        ("The 4pm planner", "proposes consolidations", ""),
        ("Planner self-apply", "a safe class, if you allow", ""),
    ]},
    {"title": "Built, but dark", "tagline": "waiting on someone else", "nodes": [
        ("Missed call → text", "built, being tested", "dark"),
        ("Gmail", "100 sign-ups till verified", "capped"),
        ("Partner credit", "off behind a flag", "off"),
    ]},
    {"title": "Not built", "tagline": "shown because hiding it would be lying", "nodes": [
        ("Morning debrief", "no code — jobs are global UTC", "planned"),
        ("WhatsApp · Telegram", "no code, no table", "planned"),
        ("Auto-enrol on triggers", "metadata, no engine", "planned"),
        ("Gone-quiet outreach", "a badge, not a sweep", "planned"),
    ]},
]
