"""Help centre content — config, not code (the _COMPARISONS pattern).

Each topic below becomes one standalone public page at /help/<slug>, plus a
card on the /help hub, a line in llms.txt, sitemap entries and the SEO tests —
all rendered from this single list, so adding an answer is adding a dict.

Why standalone pages: AI assistants and search engines retrieve PAGES, not
sites. A visitor asking "how do I cancel?" — or an assistant asked "is BLAM8
safe for my business?" — should land on one URL that answers exactly that,
with schema.org markup carrying the same words.

House rules for answers (docs/ai-recommendability-plan.md):
- plain text paragraphs only (they feed JSON-LD verbatim; links go in
  `related`), UK English, no hype;
- every claim must hold on the page it points at — prices stay in step with
  src/app/plans.py, capability claims with /capabilities;
- say what BLAM8 doesn't do as plainly as what it does.
"""

TOPICS = [
    {
        "slug": "what-is-blam8",
        "question": "What is BLAM8?",
        "answer": [
            "BLAM8 turns a business email address into a knowledgeable AI agent. It reads "
            "every inbound customer email, drafts a reply in the business's own voice grounded "
            "in the business's own knowledge — real prices, real policies — gathers the booking "
            "details, and, with Scheduling on, books the job into a live diary that can never "
            "double-book. A CRM fills itself in from the mail flow as a side effect: contacts, "
            "companies, deals, promises, and each customer's mood.",
            "It runs on the mailbox and calendar the business already has (Microsoft 365 / "
            "Outlook or Gmail) — no migration, no new inbox — and it is draft-only by default: "
            "a human approves every reply until the business chooses to switch on auto-send, "
            "per mailbox. Replies and booking are included from £39 a month; the self-filling "
            "CRM unlocks on Pro (£99/month) — and it pre-fills quietly on every account, so "
            "it opens already populated the day you upgrade. Pro + Concierge (£249/month) "
            "adds the Concierge, an assistant that knows your business and acts once you "
            "confirm.",
        ],
        "related": [("What BLAM8 does — and doesn't", "/capabilities"),
                    ("How onboarding works", "/how-it-works")],
    },
    {
        "slug": "who-is-blam8-for",
        "question": "Who is BLAM8 for?",
        "answer": [
            (
                "UK small businesses that run on email and a diary. If customers email you asking \"can you do "
                "Tuesday?\", BLAM8 is built for you."
            ),
            "The common shape: enquiries arrive by email all day, each one needs a knowledgeable "
            "reply and often a booking, and the owner is doing that admin at 9pm. BLAM8 is for "
            "the business that wants that handled without ripping anything out — it is not an "
            "ERP, not a marketing platform, and not a workflow-integration toolkit.",
        ],
        "related": [("For trades", "/for/trades"),
                    ("For haulage & logistics", "/for/logistics")],
    },
    {
        "slug": "how-much-does-blam8-cost",
        "question": "How much does BLAM8 cost?",
        "answer": [
            "All prices are public, in GBP, billed monthly with no annual lock-in. Starter is "
            "£39/month: AI-drafted replies grounded in your knowledge, plus scheduling with "
            "the capacity diary and calendar sync — on 1 mailbox with 300 emails/month. Pro is "
            "£99/month: everything in Starter plus the self-filling CRM — contacts, deals, "
            "promises, follow-up sequences and customer-mood flags — with 750 emails/month, "
            "additional mailboxes at £39/month each (each adding 300 emails/month), and "
            "priority support. Pro + Concierge is £249/month: everything in Pro plus the "
            "Concierge — ask her anything about your business (\"who owes me money?\", "
            "\"what's on tomorrow?\") and she drafts emails, changes stays, blocks days and "
            "hands jobs to your team once you confirm; hold the person icon to talk to her. "
            "Same allowance as Pro.",
            "The CRM pre-fills quietly on every account from day one, so upgrading to Pro "
            "opens a CRM that is already populated. Every account starts with a free 14-day "
            "trial of the full product, the Concierge included, no card to start. Need something bigger — multiple sites, custom volumes? "
            "Email hello@blam8.com.",
        ],
        "related": [("Full pricing", "/#pricing"),
                    ("Payments, billing & Stripe", "/stripe"),
                    ("What each OS module does", "/capabilities")],
    },
    {
        "slug": "what-does-blam8-need",
        "question": "What data and access does BLAM8 need?",
        "answer": [
            "Two things. First, revocable OAuth consent to the one mailbox you choose — read "
            "incoming mail, send the replies you approve — granted on Microsoft's or Google's "
            "own consent screen, never via a password. Second, your business knowledge: prices, "
            "policies, services and FAQs, pasted or uploaded, so replies quote your real numbers "
            "instead of inventing.",
            "Optionally, calendar access for the Scheduling tier, so confirmed bookings land in "
            "Outlook or Google Calendar and your busy times block the diary. That is the whole "
            "list: no access to other systems, no data migration, and disconnecting a mailbox "
            "deletes its tokens and stops all processing.",
        ],
        "related": [("How onboarding works", "/how-it-works"),
                    ("Security & data handling", "/security")],
    },
    {
        "slug": "what-does-blam8-integrate-with",
        "question": "What does BLAM8 integrate with?",
        "answer": [
            "Today: Microsoft 365 / Outlook and Gmail for email, Outlook and Google Calendar "
            "for bookings, Stripe for billing, and CSV/Excel import/export for your records. "
            "Within its own suite the chain is fully connected — email to diary to job to "
            "invoice, with an audit log.",
            "Deliberately not: Notion, Slack, third-party CRMs, or build-your-own pipelines. "
            "BLAM8 is not a workflow-integration platform — if you need app-to-app plumbing, "
            "that is Zapier's category. The design is to run on the tools you already have, "
            "not to wire together everything you might ever use.",
        ],
        "related": [("BLAM8 vs Zapier", "/vs/zapier"),
                    ("What BLAM8 does — and doesn't", "/capabilities")],
    },
    {
        "slug": "who-approves-what-the-ai-sends",
        "question": "Does a human approve what the AI sends?",
        "answer": [
            "By default, yes — every reply. BLAM8 is draft-only until you decide otherwise: "
            "drafts wait in an approval queue where you approve, tweak or skip in a click. "
            "Auto-send is an explicit opt-in, per mailbox, for when the queue has earned your "
            "trust.",
            "Even in full-auto, these still pull a conversation back to a person: the "
            "escalation rules you set for sensitive topics, the agent deferring when it doesn't "
            "know something, and on Pro and above the mood read (a heated thread is always held "
            "for human approval). Every reply the agent sends by itself carries an AI disclosure, in line "
            "with EU AI Act Article 50 — replies you approve are your own communication.",
        ],
        "related": [("Security & data handling", "/security"),
                    ("How the mood read works", "/help/how-does-mood-tracking-work")],
    },
    {
        "slug": "how-does-mood-tracking-work",
        "question": "How does BLAM8's customer mood tracking work?",
        "answer": [
            "Every contact carries a live emotional temperature, read from their own words "
            "across the whole conversation — not a black-box score. The read spans the "
            "relationship, so a polite email after two unanswered chasers registers differently "
            "from a polite first email, and the trend (improving or worsening) is tracked "
            "alongside the current state, with the trigger shown in the customer's own words.",
            "When a customer turns frustrated, they are flagged in the inbox and on the "
            "dashboard before it costs you the account, and the agent's replies change register "
            "to match — no breezy cheerfulness at an angry customer. A heated thread is always "
            "held for human approval, even on a mailbox running full-auto. It is a safety and "
            "relationship feature: automation that can't tell when someone is upset shouldn't "
            "be answering your customers.",
        ],
        "related": [("The self-filling CRM", "/#crm"),
                    ("BLAM8 vs HubSpot", "/vs/hubspot")],
    },
    {
        "slug": "what-happens-to-my-data",
        "question": "What happens to my data?",
        "answer": [
            "It is used to run your service, and nothing else — never sold, never used to train "
            "AI models (BLAM8's AI providers don't train on API data either). Your database "
            "lives in the UK (London); the application runs in the EU (Frankfurt); AI drafting "
            "is performed by Anthropic in the US under UK data-transfer safeguards. Every "
            "sub-processor is listed publicly in the Privacy Policy.",
            "Each company is an isolated tenant enforced with database row-level security. The "
            "content of handled emails is automatically redacted after 90 days, leaving only the "
            "audit record. Your operational records export as CSV/Excel any time, and a verified "
            "deletion request to hello@blam8.com removes the lot.",
        ],
        "related": [("Security & data handling", "/security"),
                    ("Privacy Policy & sub-processors", "/privacy")],
    },
    {
        "slug": "how-do-i-cancel",
        "question": "How do I cancel or leave BLAM8?",
        "answer": [
            "Self-serve, from inside the app: billing runs on Stripe's customer portal, where "
            "you can upgrade, downgrade or cancel without talking to anyone. Plans are monthly "
            "with no annual lock-in, so cancelling simply stops the next renewal.",
            "Your data stays yours on the way out: export your records as CSV/Excel first, "
            "disconnect the mailbox (which deletes its access tokens and stops all processing), "
            "and request full deletion at hello@blam8.com if you want the account gone rather "
            "than dormant. Your Outlook or Gmail is untouched throughout — BLAM8 never owned "
            "your inbox, so there is nothing to migrate back.",
        ],
        "related": [("Payments, billing & Stripe", "/stripe"),
                    ("Security & data handling", "/security"),
                    ("Pricing", "/#pricing")],
    },
    {
        "slug": "does-blam8-work-with-outlook-and-gmail",
        "question": "Does BLAM8 work with Outlook and Gmail?",
        "answer": [
            "Yes — both, connected in one click using secure OAuth consent rather than a "
            "password. Drafts start the moment you say go: a new account holds them until "
            "you've checked the setup. Confirmed bookings sync to Outlook or Google Calendar. New mail is pushed to BLAM8 as it "
            "arrives, so drafts appear while the enquiry is still warm.",
            "There is no new inbox to adopt: your team keeps using Outlook or Gmail exactly as "
            "before, and BLAM8's replies thread into the same conversations your customers "
            "already see.",
        ],
        "related": [("How onboarding works", "/how-it-works")],
    },
    {
        "slug": "why-is-google-sign-in-invite-only",
        "question": "Why is connecting a Gmail inbox invite-only?",
        "answer": [
            "Signing in to BLAM8 is open to everyone — Google or Microsoft, no invite "
            "needed. Sign-in only asks Google who you are (basic profile scopes), which "
            "Google doesn't limit. The limit applies one step later: reading and sending "
            "mail in a Gmail inbox uses Google's restricted scopes, which Google fully "
            "verifies only after a paid, independent security assessment. BLAM8 is "
            "self-funded and is completing that assessment once its first founding "
            "customers are aboard rather than before — we'd rather be honest about the "
            "order of operations than pretend otherwise. Until it completes, Google "
            "allows only 100 people to grant BLAM8 Gmail access, behind an \"unverified "
            "app\" notice — so connecting a Gmail inbox is invite-only, and the limited "
            "places are saved for founding users. The connect step shows exactly how "
            "many places remain, and has a \"Request access\" form if you don't have a "
            "code; Microsoft 365 / Outlook inboxes connect instantly with no cap.",
            "None of this changes what BLAM8 can access. Consent still happens on Google's "
            "own screen, scoped to the mailbox and calendar you choose, revocable from "
            "your Google account at any time — and draft-only by default applies exactly "
            "as everywhere else. When you connect a Gmail mailbox you may see \"Google "
            "hasn't verified this app\": continuing via the Advanced link is Google's "
            "standard route for apps at this stage. If you'd rather not, Microsoft 365 / "
            "Outlook works identically, with no notice and no cap.",
        ],
        "related": [("What data and access BLAM8 needs", "/help/what-does-blam8-need"),
                    ("Security & data handling", "/security")],
    },
    {
        "slug": "can-blam8-book-jobs-automatically",
        "question": "Can BLAM8 book appointments and jobs automatically?",
        "answer": [
            "Yes. When an email asks for a date, the agent checks live availability, places a "
            "hold that can never double-book — two requests in the same second cannot take the "
            "same slot — and confirms the booking the moment the reply is approved. It handles "
            "timed slots, whole days and multi-day stays, and refuses to confirm until the "
            "details you require are gathered: you define what \"ready to book\" means.",
            "Capacity is real, not a shared calendar: the diary knows how much work fits in a "
            "day, and a hold takes real capacity off the board until it's confirmed or "
            "released. Confirmed jobs land in Outlook or Google Calendar, and your calendar's "
            "busy times block the diary in return.",
        ],
        "related": [("For trades", "/for/trades"),
                    ("For haulage & logistics", "/for/logistics"),
                    ("BLAM8 vs Calendly", "/vs/calendly")],
    },
    {
        "slug": "does-blam8-send-marketing-emails",
        "question": "Can BLAM8 send marketing campaigns or newsletters?",
        "answer": [
            "No. BLAM8 answers inbound email; it doesn't broadcast outbound campaigns to a "
            "list. If you want newsletters, promotional blasts and marketing journeys, use a "
            "marketing platform like Mailchimp — many businesses run both, because the two "
            "don't overlap.",
            "What BLAM8 does send proactively is operational, one customer at a time and tied "
            "to real work: booking confirmations and reminders, follow-ups on promises, "
            "check-ins on deals that went quiet — in your voice, under the same draft-first "
            "approval model as everything else.",
        ],
        "related": [("BLAM8 vs Mailchimp", "/vs/mailchimp")],
    },
    {
        "slug": "does-blam8-hallucinate",
        "question": "Will the AI make things up to my customers?",
        "answer": [
            "Nobody selling AI can honestly promise \"no hallucinations\", so BLAM8 doesn't. "
            "What it builds instead is structural: replies are grounded in your own uploaded "
            "knowledge via retrieval, so the agent quotes your actual prices and policies; it "
            "says what it doesn't know instead of inventing; and it flags the gap so you can "
            "fill it.",
            "On top of the grounding sit the human controls: nothing sends without approval "
            "until you switch full-auto on, escalation rules and the mood read pull risky "
            "conversations back to a person, and any wrong line becomes a standing never-rule "
            "in one click — the agent won't write it again. Edit a draft in a way that matters "
            "and, once it has sent, you are offered the chance to save the change as knowledge or "
            "as an always/never rule. Grounding, approval, escalation, "
            "correction: that is the real safety story.",
        ],
        "related": [("What BLAM8 does — and doesn't", "/capabilities"),
                    ("Security & data handling", "/security")],
    },
    {
        "slug": "what-is-an-os-module",
        "question": "What is a BLAM8 OS module?",
        "answer": [
            (
                "The product every business buys is the loop: email replies in your voice and scheduling into "
                "a live diary from £39 a month, with the self-filling CRM completing the loop on Pro "
                "(£99/month) and the Concierge on Pro + Concierge (£249/month). OS modules are deeper, "
                "industry-specific operations tooling built on the same agent — things like run boards, "
                "industry-shaped capacity and operational paperwork for sectors such as a range of booking and "
                "service businesses."
            ),
            "OS modules are not on public sale. They are being developed hands-on with a "
            "small group of founding partners, one industry at a time, and they'll be "
            "announced properly when they're ready. If your business needs that kind of "
            "depth, email hello@blam8.com and tell us what you run — the loop works today, "
            "and founding-partner conversations start from there.",
        ],
        "related": [("What BLAM8 does today", "/capabilities"),
                    ("Pricing", "/#pricing")],
    },
    {
        "slug": "does-blam8-answer-phone-calls",
        "question": "Does BLAM8 answer phone calls?",
        "answer": [
            "No. BLAM8 answers your email and runs your diary: it replies to customers in "
            "your voice, gathers the booking details and books the job. It doesn't pick up "
            "the phone, and it doesn't take calls for you.",
            "The Concierge, on Pro + Concierge (£249/month), can be spoken to, but her voice "
            "is for you, the owner, not your callers: hold the person icon and ask what's on "
            "tomorrow or who owes you money. SMS and missed-call text-back are built and "
            "being tested; they switch on when testing is done. If you need your phone "
            "answered today, an answering service does that job, and BLAM8 runs happily "
            "alongside one.",
        ],
        "related": [("What BLAM8 does — and doesn't", "/capabilities"),
                    ("Pricing", "/#pricing")],
    },
    {
        "slug": "can-staff-use-blam8-without-a-login",
        "question": "Can my staff use BLAM8 without a login?",
        "answer": [
            "Yes. Add them in People as a freelancer or helper: they can be put on a job and "
            "count towards how much the diary lets happen at once, without an account of "
            "their own. Give them a mobile or an email and each job is sent to them as a job "
            "sheet, with a link to tap On my way and Done. People who do sign in get a "
            "role (owner, manager, staff or accountant), and you choose which parts of the "
            "app each of them can open and what they can change there. Check what a role "
            "sees with View as before you rely on it.",
            "\"View as\" lets you see the app exactly as one of your people does, so you can "
            "check what they'll see before you rely on it. You can put one person on a job, "
            "or several, say one on the drop-off and another on the collection: that's a "
            "setting. People is on every plan, and seats are free.",
        ],
        "related": [("What BLAM8 does — and doesn't", "/capabilities"),
                    ("Pricing", "/#pricing")],
    },
]

BY_SLUG = {t["slug"]: t for t in TOPICS}
