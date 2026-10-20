"""Every page BLAM8 serves, grouped — the data behind /pages (Kris 2026-09-20).

One entry per human-facing GET route. tests/test_pages_index.py walks the FastAPI
route table and fails when a route is neither listed here nor excluded below with
a reason, so the index can't silently fall behind the app. The dynamic families
(help topics, worked setups, the vs pages) are built from their own source lists.

Vocabulary: `route` is the FastAPI path pattern (what the test matches against the
route table); `href` is what the index links to — None for a per-record page,
which is shown as a pattern with the page it is reached from (`via`).
"""
from __future__ import annotations

# What each access level means, as the page says it.
ACCESS = {
    "public": "Open to everyone",
    "customer": "Opened from a link BLAM8 sends your customer",
    "signed-in": "Sign in required",
    "founding": "Founding-access Operating System module",
    "partner": "Partners — the same sign-in, matched to a partner record",
    "admin": "Platform admins only",
}


_UNSET = object()


def _p(route: str, label: str, note: str = "", *, href=_UNSET,
       via: str | None = None) -> dict:
    """One page. A route with a {parameter} is a per-record page: no link of its
    own, shown as the pattern plus the listing page it is opened from. An explicit
    href=None does the same for a page that needs a query string to mean anything."""
    if href is _UNSET:
        href = None if "{" in route else route
    return {"route": route, "label": label, "note": note, "href": href, "via": via}


def _g(title: str, access: str, pages: list[dict], blurb: str = "") -> dict:
    return {"title": title, "access": access, "pages": pages, "blurb": blurb}


def build(comparisons: list[dict], help_topics: list[dict], setups: list[dict]) -> list[dict]:
    """The sections in reading order. Called per request with the three source lists
    (they live in app.main / app.help_center / app.setups) so a new help answer or
    worked setup appears here the moment it exists."""
    return [
        {"key": "site", "title": "The public site",
         "blurb": "What a visitor, a search engine or an AI assistant can read before signing up.",
         "groups": [
             _g("Front door", "public", [
                 _p("/", "Home", "the loop walkthrough, pricing and the FAQ"),
                 _p("/for-businesses", "For businesses", "the business entry point — what the AI front desk does"),
                 _p("/for-customers", "For customers", "find a business's booking page by name, or a booking by its reference"),
                 _p("/how-it-works", "How it works", "sign-in to first approved reply, and what BLAM8 needs access to"),
                 _p("/capabilities", "What it does and doesn't", "the honest capability list"),
                 _p("/security", "Security & data handling", "draft-only default, OAuth-only access, where data lives"),
                 _p("/stripe", "Payments, billing & Stripe", "your subscription, your customers paying you, what is stored"),
                 _p("/architecture", "The architecture", "the whole pipeline drawn node by node"),
                 _p("/partners", "Partner programme", "refer a business, earn 20% of what BLAM8 collects, for life"),
                 _p("/pages", "Every page", "this index"),
                 _p("/privacy", "Privacy Policy", "roles, lawful bases, retention, the sub-processor list"),
                 _p("/terms", "Terms of Service", "the contract"),
                 _p("/for/trades", "For trades", "a vertical page kept live for inbound links"),
                 _p("/for/logistics", "For haulage & logistics", "a vertical page kept live for inbound links"),
             ]),
             _g("Compare", "public",
                [_p("/compare", "BLAM8 vs the alternatives", "the comparison hub")]
                + [_p(f"/vs/{c['slug']}", f"vs {c['name']}", c.get("hook") or "")
                   for c in comparisons]),
             _g("Help centre", "public",
                [_p("/help", "Help centre", "one question answered per page")]
                + [_p("/help/{slug}", t["question"], href=f"/help/{t['slug']}")
                   for t in help_topics]),
             _g("Worked setups", "public",
                [_p("/setups", "All worked setups", "how BLAM8 is set up for a given business type")]
                + [_p("/setups/{slug}", s.get("business") or s["slug"],
                      (s.get("industry") or "").replace("_", " "), href=f"/setups/{s['slug']}")
                   for s in setups]),
         ]},

        {"key": "in", "title": "Getting in",
         "blurb": "Sign-in, invitations, the first-run onboarding and your plan.",
         "groups": [
             _g("Sign in & invitations", "public", [
                 _p("/login", "Sign in", "Microsoft or Google, passwordless"),
                 _p("/join/{token}", "Join a business", "a staff invitation link — says which business and who invited you",
                    via="/app/people"),
                 _p("/oauth/invite/{code}", "Gmail invite code", "the invite gate while Google verification completes",
                    via="/login"),
             ]),
             _g("Onboarding", "signed-in", [
                 _p("/onboarding", "Onboarding", "resumes at your next incomplete step"),
                 _p("/onboarding/industry", "Your business type", "the sector, honestly, so the diary and CRM fit"),
                 _p("/onboarding/mailbox", "Connect a mailbox", "Microsoft 365 / Outlook or Gmail, by consent"),
                 _p("/onboarding/interview", "The interview", "the AI asks what it needs to know to answer for you"),
                 _p("/onboarding/voice", "Your voice", "learned from sent emails, on one click"),
                 _p("/onboarding/knowledge", "Your knowledge", "prices, policies, FAQs — or start from your website"),
                 _p("/onboarding/knowledge/review/{review_id}", "Review Knowledge", "check source evidence, overlaps and unanswered details", via="Your knowledge"),
                 _p("/onboarding/data", "Your records", "bring contacts and history across"),
             ]),
             _g("Plan & billing", "signed-in", [
                 _p("/billing/plans", "Plans & billing", "choose or change your plan; the Customer Portal lives here"),
                 _p("/billing/success", "Back from checkout", "Stripe returns you here after subscribing"),
                 _p("/billing/cancel", "Checkout cancelled", "Stripe returns you here if you back out; nothing charged"),
             ]),
         ]},

        {"key": "app", "title": "The app",
         "blurb": "Every page behind sign-in, in the order the menu reads.",
         "groups": [
             _g("Today & Inbox", "signed-in", [
                 _p("/app/", "Today", "the notification deck — what needs you, in order"),
                 _p("/app/conversations", "Inbox", "every customer conversation, with the smart labels"),
                 _p("/app/conversations/view", "One conversation", "the thread, the draft and the booking pin",
                    href=None, via="/app/conversations"),
                 _p("/app/drafts/{log_id}", "Review a draft", "approve, edit or hold one reply", via="/app/conversations"),
                 _p("/app/inbox/{message_id}", "One message", "a single handled email", via="/app/conversations"),
                 _p("/app/sent", "Sent", "what went out, by whom"),
                 _p("/app/ignored", "Ignored", "what the agent set aside, and why"),
                 _p("/app/agent-log", "Agent log", "everything the agent did, filterable"),
                 _p("/app/concierge", "Concierge", "ask about your own business — inbox, diary, money, CRM"),
                 _p("/app/outreach", "Outreach", "manage customer outreach"),
                 _p("/app/plan", "Scheduled plan", "owners and managers: plan emails, texts, quotes, invoices, booking holds and tasks ahead of time; starts paused"),
                 _p("/app/requests", "Requests & reports", "what you have asked BLAM8 for, and support tickets"),
                 _p("/app/channels", "Channels", "email, SMS and the other ways customers reach you"),
                 _p("/app/phone", "Phone", "owner-only inbound setup, outbound approval and call records; live verification required"),
             ]),
             _g("Diary", "signed-in", [
                 _p("/app/diary", "Diary", "the capacity diary that can't double-book"),
                 _p("/app/diary/{booking_id:uuid}", "One booking", "the job, its customer and its thread", via="/app/diary"),
                 _p("/app/diary/settings", "Diary settings", "hours, notice, holds, capacity, courses, cancellations"),
                 _p("/app/diary/intake", "Booking requirements", "what every booking must gather before it is held"),
                 _p("/app/diary/dependants", "Dependants", "the named subjects that bookings are about"),
             ]),
             _g("Money", "signed-in", [
                 _p("/app/money", "Money", "quoted, accepted, owed, overdue, collected"),
                 _p("/app/money/overdue", "Overdue", "the chase board — debt by age, who owes what"),
                 _p("/app/money/collected", "Collected", "the collection story, month by month"),
                 _p("/app/quotes", "Quotes", "every quote and where it stands"),
                 _p("/app/quotes/new", "New quote", "standalone, or pre-filled from a booking"),
                 _p("/app/quotes/{quote_id}", "One quote", "lines, deposit, acceptance", via="/app/quotes"),
                 _p("/app/invoices", "Invoices", "issued, paid, overdue; the accountant's export"),
                 _p("/app/invoices/new", "New invoice", "any tenant can bill anything"),
                 _p("/app/invoices/{invoice_id}", "One invoice", "the record, the card payment, refunds", via="/app/invoices"),
                 _p("/app/invoices/settings", "Getting paid", "deposits, balances, VAT, terms, bank details, Stripe"),
                 _p("/app/payments", "Card payments", "balance, payouts, fees per payment, refunds — from your own Stripe"),
             ]),
             _g("CRM", "signed-in", [
                 _p("/app/crm", "Contacts", "everyone BLAM8 has met, newest activity first"),
                 _p("/app/crm/companies", "Companies", "the businesses behind the contacts"),
                 _p("/app/outreach", "Outreach", "your own outreach campaigns, evidence and drafts for approval"),
                 _p("/app/crm/contacts/{contact_id}", "One contact", "who they are, their deals, the whole timeline", via="/app/crm"),
                 _p("/app/crm/pipeline", "Pipeline", "deals under your stages"),
                 _p("/app/crm/tasks", "Tasks", "what is due, from promises and follow-ups"),
                 _p("/app/crm/sequences", "Sequences", "follow-up sequences and who is enrolled"),
                 _p("/app/crm/reports", "Reports", "pipeline, activity, won and lost"),
                 _p("/app/crm/fields", "Fields", "what to track per contact, company and deal"),
                 _p("/app/crm/settings", "CRM settings", "stages, quiet days, what staff can see"),
             ]),
             _g("Settings, people & knowledge", "signed-in", [
                 _p("/app/settings", "Settings", "auto-send, notifications, the installable app, everything else"),
                 _p("/app/mailboxes", "Mailboxes", "every connected mailbox, by department"),
                 _p("/app/mailboxes/add", "Add a mailbox", "the price, before the consent screen"),
                 _p("/app/mailboxes/{mailbox_id}/settings", "One mailbox", "its labels, its role, its persona", via="/app/mailboxes"),
                 _p("/app/knowledge", "Knowledge", "the prices, policies and rules the agent answers from"),
                 _p("/app/people", "People", "everyone on this business, roles and invitations"),
                 _p("/app/people/settings", "People settings", "one person or several people per job"),
                 _p("/app/people/{user_id}", "One person", "their doors, abilities and calendar", via="/app/people"),
                 _p("/app/people/team/{person_id}", "Someone who doesn't sign in", "what they do, how to reach them, give them a login", via="/app/people"),
                 _p("/app/view-as", "View as", "the owner sees BLAM8 as one of their people — read-only"),
                 _p("/app/modules", "Modules", "what is live for this business, what is next"),
                 _p("/app/import", "Import", "bring records in from a spreadsheet or Xero"),
                 _p("/app/usage", "Usage", "handled emails against your allowance; the top-up"),
             ]),
         ]},

        {"key": "os", "title": "Operating System modules",
         "blurb": "Founding-access add-ons for a sector. Each has its own home and its own daily surfaces.",
         "groups": [
             _g("Transport OS", "founding", [
                 _p("/app/tms", "TMS cockpit", "fleet and capacity, the transport desk's landing page"),
                 _p("/app/board", "Transport board", "the day's loads across the fleet"),
                 _p("/app/driver", "Driver run", "a driver's own run for the day"),
                 _p("/app/tms/pricing", "Pricing", "the rate card the agent quotes from"),
                 _p("/app/tms/pod", "Proof of delivery", "signatures, photos, exceptions"),
                 _p("/app/tms/tracking", "Tracking", "the customer tracking links you have issued"),
                 _p("/app/tms/compliance", "Compliance", "driver hours and vehicle checks"),
                 _p("/app/fleet", "Fleet", "branches and vehicles — the capacity the agent sells"),
                 _p("/app/fleet/import", "Fleet import", "vehicles in from a spreadsheet"),
                 _p("/app/fleet/vehicles/{vehicle_id}/day/{day}", "Vehicle day manifest", "everything on one vehicle for one day", via="/app/fleet"),
                 _p("/app/fleet/vehicles/{vehicle_id}/day/{day}/route", "Vehicle day route", "the same day on a map", via="/app/fleet"),
             ]),
             _g("Field Service OS", "founding", [
                 _p("/app/fsm", "Engineer cockpit", "a rolling week of each engineer's load"),
                 _p("/app/fsm/engineers", "Engineers", "who does what, and where"),
                 _p("/app/fsm/job-types", "Job types", "what a job is, how long it takes"),
                 _p("/app/fsm/pricing", "Pricing", "call-outs, labour, parts"),
             ]),
             _g("Practice OS", "founding", [
                 _p("/app/practice", "Practice today", "today's appointments, recalls and notes"),
                 _p("/app/practice/recall", "Recall", "appointments due a recall"),
                 _p("/app/practice/appointments/{booking_id}", "One appointment", "service, practitioner, client, notes", via="/app/practice"),
             ]),
             _g("Storefront OS", "founding", [
                 _p("/app/shop", "Shop today", "collections, reservations, low stock"),
                 _p("/app/shop/products", "Products", "the catalogue, with low-stock filter"),
                 _p("/app/shop/stock-in", "Stock in", "photograph a delivery, stock it in"),
                 _p("/app/shop/transactions", "Transactions", "orders and reservations by status"),
             ]),
             _g("Wholesale OS", "founding", [
                 _p("/app/wholesale", "Wholesale home", "standing orders due, credit risk, orders to plan"),
                 _p("/app/wholesale/orders", "Orders", "every order and where it is"),
                 _p("/app/wholesale/orders/{deal_id}", "One order", "lines, credit, delivery", via="/app/wholesale/orders"),
                 _p("/app/wholesale/credit", "Credit", "every buyer with a limit, exposure against it"),
                 _p("/app/wholesale/plan", "Plan", "the week's orders in day columns, with shortages"),
                 _p("/app/wholesale/standing", "Standing orders", "the regulars, and when they next fire"),
             ]),
             _g("Events OS", "founding", [
                 _p("/app/events", "Events", "this week's events as live rollup cards"),
                 _p("/app/events/{event_id}", "One event", "the deal, the guest list, the dietaries", via="/app/events"),
                 _p("/app/events/{event_id}/run-of-show", "Run of show", "the assembled sheet and its gaps", via="/app/events"),
             ]),
             _g("Assets OS", "founding", [
                 _p("/app/assets", "Assets board", "today's handovers, units out and coming back"),
                 _p("/app/assets/units", "Units", "every hireable asset with its rate card and deposit"),
                 _p("/app/assets/damage", "Damage cases", "open, in review, resolved"),
                 _p("/app/assets/damage/{case_id}", "One case", "the evidence bundle, side by side", via="/app/assets/damage"),
             ]),
         ]},

        {"key": "customers", "title": "For your customers",
         "blurb": "Pages a customer opens from a link in an email BLAM8 sent. No sign-in; the link is the key.",
         "groups": [
             _g("Customer pages", "customer", [
                 _p("/c/{slug}", "Booking & questions portal", "one per business — book, ask, look up a booking by reference",
                    via="/for-customers"),
                 _p("/q/{quote_id}", "A quote", "read it, accept it by typing a name", via="/app/quotes"),
                 _p("/pay/{invoice_id}", "An invoice", "the invoice, Pay Now, bank details, pay several at once", via="/app/invoices"),
                 _p("/pay/{invoice_id}/done", "After paying", "Stripe returns the customer here", via="/app/invoices"),
                 _p("/track/{token}", "Delivery tracking", "where the load is, without an account", via="/app/tms/tracking"),
                 _p("/capture/{token}", "Condition capture", "photograph a hired unit going out or coming back", via="/app/assets"),
                 _p("/j/{token}", "A job sheet", "sent to someone on the job who doesn't sign in — map, call, On my way, Done",
                    via="/app/people"),
             ]),
         ]},

        {"key": "partners", "title": "Partners",
         "blurb": "The referral programme: the public explainer and the partner's own portal.",
         "groups": [
             _g("Partner portal", "partner", [
                 _p("/partner", "Partner portal", "your code, your referrals, your earnings, statements"),
                 _p("/partner/join", "Become a partner", "sign in, sign the agreement, get your code"),
                 _p("/partner/terms", "Partner terms", "the agreement the click-wrap accepts"),
             ]),
         ]},

        {"key": "admin", "title": "Operator console",
         "blurb": "BLAM8's own back office. Links show only when you are signed in as a platform admin.",
         "groups": [
             _g("Admin", "admin", [
                 _p("/admin", "Overview", "every tenant, the funnel, where each one stalled"),
                 _p("/admin/login", "Admin sign-in", "the console's own door"),
                 _p("/admin/live-allowance", "Live Concierge allowance", "rollout and per-tier connection-minute limits"),
                 _p("/admin/outreach", "Outreach", "manage platform outreach"),
                 _p("/admin/tenants/{tenant_id}", "One tenant", "their journey, visits, margin, modules", via="/admin"),
                 _p("/admin/invites", "Gmail invites", "mint, track and revoke invite codes"),
                 _p("/admin/llm", "LLM calls", "every model call, grouped into exchanges"),
                 _p("/admin/llm/{call_id}", "One call", "the envelope and, when kept, the prompt", via="/admin/llm"),
                 _p("/admin/os-access", "Founding access", "the OS vetting queue"),
                 _p("/admin/partners", "Partners", "every partner and their ledger; payouts"),
                 _p("/admin/requests", "Requests & reports", "every tenant's, newest first"),
                 _p("/admin/src", "Sources", "which short produced which click and which signup"),
                 _p("/admin/voice-crm", "Voice CRM", "prospects, next action first"),
                 _p("/admin/voice-crm/new", "New prospect", "paste a link, record, review, approve"),
                 _p("/admin/voice-crm/add", "Add by hand", "a partner or prospect with no call or link"),
                 _p("/admin/voice-crm/{prospect_id}", "One prospect", "every field and the call history", via="/admin/voice-crm"),
             ]),
         ]},
    ]


# GET routes that are not pages, with the reason. The test refuses an entry here that
# no longer exists, so this list can't rot either.
EXCLUDED = {
    "/device-sync/{channel}": "signed-in device synchronisation state for onboarding and assistant clients, JSON",
    "/concierge/business/status/{proposal_id}": "one confirmed-action receipt, JSON",
    "/admin/": "alias of /admin",
    "/concierge/voice/gptlive/events/{session_id}": "polled JSON events for the GPT-Live screen, signed-in only",
    "/onboarding/": "alias of /onboarding",
    "/partner/": "alias of /partner",
    "/logout": "an action: signs you out",
    "/admin/logout": "an action: signs the admin out",
    "/auth/sso/{provider}": "SSO leg: redirects to the provider",
    "/auth/callback": "SSO leg: the provider returns here",
    "/oauth/connect": "mailbox consent leg",
    "/oauth/callback": "mailbox consent leg (Microsoft)",
    "/oauth/google/callback": "mailbox consent leg (Google)",
    "/oauth/my-calendar": "calendar consent leg for one person",
    "/oauth/status": "JSON for the settings page",
    "/payments/connect/callback": "Stripe Connect returns here",
    "/app/payments/connect": "redirects to Stripe's consent screen",
    "/app/conversations/fragment": "live fragment of the Inbox",
    "/app/conversations/view/fragment": "live fragment of one conversation",
    "/app/nav/badge": "the pending-draft count fragment",
    "/billing/status": "polled status fragment",
    "/onboarding/voice/status": "polled status for the voice screen",
    "/onboarding/mailbox/import-status": "polled mailbox import status, JSON",
    "/onboarding/knowledge/reviews": "pending Knowledge review and coverage fragments, JSON",
    "/concierge/ask-limit": "public assistant allowance, JSON",
    "/app/settings/voice-status": "polled status for Settings",
    "/app/drafts/{log_id}/sources": "the content behind a draft's grounding pills",
    "/app/knowledge/note": "one note's text for the in-place editor",
    "/app/quotes/deals": "the quote form's deal picker",
    "/app/search": "the masthead search's suggestions, JSON",
    "/concierge/live": "dedicated installable live Concierge entry, same protected panel",
    "/app/concierge/panel": "the Concierge modal's panel, an HTML fragment",
    "/app/practice/patients/{contact_id}/notes": "decrypted notes as JSON",
    "/concierge/business/history": "the Concierge transcript, JSON",
    "/concierge/business/waiting": "the Concierge's unanswered cards (waiting chips), JSON",
    "/concierge/business/status/{proposal_id}": "scoped read-only proposal recovery receipt, JSON",
    "/concierge/business/done": "the Concierge's done actions of the last day (done chip), JSON",
    "/concierge/business/tasks": "the state of task lists on screen (the panel's catch-up poll), JSON",
    "/concierge/business/status": "the full-screen Concierge page's status column: work ledger and allowances, JSON",
    "/concierge/business/around": "the status column's wider-day box (follow-ups, diary, inbox), JSON",
    "/concierge/business/session-tasks/{voice_session}": "requester-scoped current delegated results for a voice session, JSON",
    "/concierge/voice/context": "the note the server adds to the next voice question (Context view), JSON",
    "/concierge/notifications": "the notification feed, JSON",
    "/concierge/setup-status": "the guided hand-off state, JSON",
    "/concierge/setup-cards/{page}": "the current setup proposal, JSON",
    "/concierge/setup-cards": "current setup proposals and computed previews, JSON",
    "/concierge/setup-cards/{page}/history": "setup proposal revisions and apply receipts, JSON",
    "/app/conversations/open": "redirects a sender to their conversation",
    "/app/crm/open": "redirects an address to its CRM card",
    "/app/crm/documents/{doc_id}": "opens one stored file",
    "/app/invoices/{invoice_id}/pdf": "the invoice as a PDF, inline (the paperclip on its bubble)",
    "/app/inbox": "retired: redirects to the Inbox",
    "/app/fsm/invoices": "redirects to Invoices",
    "/app/fsm/invoices/{invoice_id}": "redirects to one invoice",
    "/app/tms/invoices": "redirects to Invoices",
    "/app/tms/invoices/{invoice_id}": "redirects to one invoice",
    "/app/stream": "the live-update event stream",
    "/webhook": "Microsoft Graph change notifications",
    "/health": "the health check",
    "/offline": "the installed app's offline fallback",
    "/admin/memstat": "the process memory read-out, JSON with numbers only, operator only",
}

# Anything with one of these suffixes is a download or a machine file, never a page.
MACHINE_SUFFIXES = (".txt", ".xml", ".json", ".js", ".csv", ".xlsx", ".pdf", ".zip", ".html")


def listed_routes(sections: list[dict]) -> set[str]:
    return {p["route"] for s in sections for g in s["groups"] for p in g["pages"]}


def count(sections: list[dict]) -> int:
    return sum(len(g["pages"]) for s in sections for g in s["groups"])
