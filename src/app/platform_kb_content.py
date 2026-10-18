"""Seed content for the platform knowledge base — the concierge's recall layer.

These are the help/capability facts the website + in-app assistant retrieves to
answer "what can BLAM8 do?" and "how do I…?". They are ingested into the reserved
sentinel platform tenant by concierge.seed_platform_kb() — run
scripts/knowledge/seed_platform_kb.py after deploy (and whenever this file changes).

DELIBERATELY NO PRICES HERE. Pricing is single-sourced from the plan catalog
(src/app/plans.py) and injected as facts, so it can never drift. KB entries describe
the billing *model* and point to the Plans page instead of quoting numbers.

Each entry is one source (one topic); re-seeding replaces a source by name, so
edits here are safe to re-run. Everything is drawn from README.md + docs/.
"""

SOURCES: dict[str, str] = {
    "BLAM8 — what it is and who it's for": (
        "BLAM8 is the inbox that books the job and fills the CRM — a UK subscription "
        "service that gives a business an AI agent for its email. You connect your own "
        "Outlook or Gmail mailbox, teach the agent your voice and your facts, and it "
        "runs one loop on every inbound email: it drafts the reply in your voice with "
        "your real prices, holds the slot so your diary can't double-book, drops the "
        "confirmed job into your calendar, and writes the CRM record — contact, deal, "
        "promises — straight from the thread, with no typing. Draft-first by default, "
        "so a human approves everything before it sends. The Starter plan covers "
        "replies and booking; the self-filling CRM completes the loop on the Pro "
        "plan; and the Pro + Concierge plan adds the Concierge, an assistant that "
        "knows your business and acts for you once you confirm. Any business can use it — if your sector isn't listed you still get the "
        "full platform, tailored by the knowledge and voice you give it during "
        "onboarding."),

    "Email automation — the base module": (
        "Email automation is the core of BLAM8. When a message arrives in a connected "
        "mailbox, the agent drafts a reply in your business's voice using the facts "
        "you've taught it. It triages incoming mail and threads conversations, "
        "remembering the whole exchange so replies have context. It only ever uses your "
        "approved knowledge for concrete facts like prices, dates and policies — if it "
        "doesn't know something it says a colleague will follow up rather than guessing. "
        "You can set escalation keywords so sensitive emails are always held for a human."),

    "Draft-first, approval and auto-send": (
        "BLAM8 is draft-first by default: every reply waits in your approval queue, where "
        "you can approve and send, edit then send, or skip it. Nothing reaches a customer "
        "without you. When you're confident, you can switch on auto-send per inbox so "
        "routine replies go out automatically — escalation matches are always held, and "
        "mail from your own connected mailboxes is drafted but never auto-sent so the "
        "agents can't loop. The inbox and conversation views always show exactly what the "
        "agent did."),

    "Teaching the agent — the knowledge base": (
        "The knowledge base is how the agent learns your facts. Upload files (PDF, Word, "
        "Excel, CSV), paste notes, or add a web link, and BLAM8 turns them into "
        "searchable knowledge it retrieves while drafting. Add the things customers ask "
        "about: pricing, policies, opening hours, delivery areas, FAQs. If the agent has "
        "to defer a fact it didn't have, it flags it as a knowledge gap so you can add it "
        "in one click. If a draft ever gets a fact wrong, you can trace it back to the "
        "source knowledge and correct it."),

    "Scheduling — the capacity diary": (
        "The Scheduling module gives the agent a real diary to book against, so 'can you "
        "do Friday?' becomes an actual booking instead of a deferral. You set your "
        "capacity — whole-day, morning/afternoon, or real time slots — and the agent "
        "checks availability and places a real hold before confirming, so racing emails "
        "can't double-book. You can add and manage bookings yourself in the month/day "
        "Diary, and connect Outlook or Google Calendar so confirmed bookings appear there "
        "and your existing calendar events block out capacity."),

    "Field OS · Transport and Fleet": (
        "For hauliers and distribution, Field OS · Transport models your "
        "branches and vehicles (pallet spaces and weight limits) and books each order "
        "onto a specific vehicle-day with real capacity enforcement — racing emails can't "
        "overload a van, and the agent never promises a split it can't do. It includes a "
        "fleet board, route capacity, a customer tracking portal, and a rate card that "
        "turns a booking into a quote. The agent talks to customers in dates and capacity "
        "only; how you organise vehicles stays internal. IMPORTANT: Field OS and the "
        "other industry OS modules are NOT on public sale — they are developed hands-on "
        "with founding partners, one industry at a time. Never pitch them or promise "
        "availability; a business that needs this depth should email hello@blam8.com. "
        "The core loop is what the public plans sell today: replies and live-diary "
        "booking on Starter, with the self-filling CRM on Pro, and the Concierge on "
        "Pro + Concierge."),

    "Connecting your mailbox": (
        "You connect your own mailbox with one click using Microsoft (Outlook / Microsoft "
        "365) or Google (Gmail) sign-in — BLAM8 never sees or stores your password, and "
        "the access tokens are encrypted at rest. Microsoft mailboxes receive new mail "
        "through a secure push notification; Gmail is polled regularly. You can be live "
        "on a real inbox in days, in draft-only mode, and disconnect at any time. A new "
        "account holds drafting until you've checked the setup: drafts start the moment "
        "you say go (Settings). Email "
        "hosted with a web host (IONOS, GoDaddy, 123-reg — IMAP only) cannot connect "
        "directly: either set up forwarding from the host into a Gmail/Outlook account "
        "and connect that, or move the domain's email to Google Workspace / Microsoft "
        "365 for the native setup."),

    "The self-filling CRM": (
        "BLAM8 includes a CRM that fills itself in from your email — no data entry, "
        "nothing to import. New enquiries become contacts; several people at the same "
        "firm roll up into one company; phone numbers, postcodes and the fields you "
        "choose to track are read straight out of the thread. Related quotes cluster "
        "into deals with a stage and a value, and every promise — what you owe them "
        "and what they owe you — is logged the moment it's written down, with "
        "one-click follow-ups. When a lead goes quiet, the cold deal is flagged and a "
        "check-in is drafted for you. The agent also reads each customer's mood from "
        "their own words: frustrated customers are flagged before it costs the "
        "account, replies change register to match, and a heated thread always waits "
        "for human approval, even on auto-send. The CRM is included on the Pro and "
        "Pro + Concierge plans, and the free trial opens it too (the trial is the full "
        "product). On a paid Starter plan it quietly pre-fills from the first email — so "
        "when that business upgrades, the CRM opens already populated — but it cannot "
        "be opened or used until then."),

    "Calendar connect and sync": (
        "With Calendar Connect, confirmed bookings are pushed into your Outlook or Google "
        "calendar as real events, and your existing calendar events are imported as busy "
        "blocks so the agent won't book over them. It keeps the BLAM8 diary and your real "
        "calendar in step both ways."),

    "Multiple mailboxes and enterprise": (
        "A single BLAM8 account can run many mailboxes — for example orders@, sales@ and "
        "separate depot or branch inboxes — each with its own persona and rules while "
        "sharing company-wide knowledge. You can let one inbox run full-auto while another "
        "stays draft-only, and target knowledge to a specific mailbox or the whole "
        "company. This is how franchises and multi-branch operations run everything from "
        "one account."),

    "Security and data handling": (
        "Security is built in. Each company's data is isolated with row-level security, "
        "mailbox tokens are encrypted at rest, and you connect via Microsoft or Google "
        "sign-in rather than a password. Your data is stored in the UK (London "
        "database); the app is hosted in the EU (Frankfurt); AI drafting is done by a "
        "US sub-processor (Anthropic) under UK data-transfer safeguards — the full "
        "sub-processor list and locations are public in the privacy policy. Your data "
        "is never used to train the AI. The agent is draft-first by design, actions "
        "are auditable, and message and draft text are stored temporarily and purged "
        "on a retention schedule. Because the agent reads each incoming email in full, "
        "any sensitive personal data a customer includes (bank details, health "
        "details) is read, stored temporarily and sent to the AI sub-processor for "
        "drafting, like the rest of the message. Some details can also be picked out "
        "of emails and kept on the customer's record — see \"Health, allergy and "
        "other sensitive details\" for exactly what, and how to turn each off. "
        "Businesses handling payment, health or other special-category data should "
        "review the privacy policy and email hello@blam8.com with compliance "
        "questions before going live; escalation keywords flag matching emails for "
        "mandatory human review (the agent still drafts, but those replies never "
        "auto-send)."),

    "Health, allergy and other sensitive details": (
        "BLAM8 reads every email in full, so health details a customer writes are "
        "seen by the agent and sent to the AI sub-processor to draft the reply. Some "
        "of them can also be kept and used again. There are three ways this "
        "happens. "
        "1. Your CRM fields (CRM → Fields). BLAM8 fills your fields in from emails "
        "and keeps the values on the customer's record. Those values go into every "
        "later reply draft for that customer. A field about health, medical details, "
        "medication or allergies is different. It is never filled in or suggested "
        "from an email, the agent never asks the customer for it, and it is never "
        "put into a reply draft, the customer's portal chat or the Concierge: the "
        "Concierge only says that a health detail is on record, and you read or "
        "change it on the customer's page. You type it in by hand. To stop a field "
        "being filled in, "
        "press Remove next to it. Values already stored stay on the records until "
        "you clear them. "
        "2. What a booking is about (Diary → Dependants). If you name a kind of "
        "thing here and list what you need to know about each one, BLAM8 reads "
        "those facts from emails, and from documents kept on the record, and saves "
        "them on the customer's record. If one of your facts is a note such as a "
        "medication or an access need, it is read too. These facts go into later "
        "reply drafts and the portal chat, and the agent may say back in one short "
        "clause that a note is on record. To stop it, press Remove on the fact or "
        "Remove this kind. Tick You confirm on a fact and what the customer sends "
        "becomes a chip for you to accept, never a silent change. "
        "3. Event guest lists (only when the events module is on your plan). Guests' "
        "dietary needs, including allergies, are read from emails and kept on that "
        "event's guest list, not on a customer record. They appear as counts on the "
        "event board and the run sheet. They are not put into reply drafts. This "
        "has no separate switch: email hello@blam8.com to have it turned off. "
        "Nothing here is used to train the AI."),

    "Getting started — onboarding": (
        "Setting up BLAM8 takes a few minutes: pick your industry (any sector works — the "
        "core platform is the same), connect your mailbox, teach the agent your voice "
        "(how it should sound and sign off), add some knowledge, and tell us which "
        "systems you run on. Every step after connecting your mailbox is optional and can "
        "be refined later in Settings. You'll land in draft-only mode on a real inbox, "
        "with drafting held until you've checked the setup and say go."),

    "Usage and billing": (
        "BLAM8 is a subscription with three plans: Starter, Pro and Pro + Concierge. "
        "Starter covers replies in your voice and scheduling with calendar sync; Pro "
        "adds the self-filling CRM (contacts, deals, promises, follow-up sequences "
        "and customer-mood flags) plus more monthly email volume and priority "
        "support; Pro + Concierge is everything in Pro plus the Concierge, with the "
        "same email allowance as Pro. "
        "One flat price per month; each plan includes "
        "one connected mailbox, and extra mailboxes are a flat monthly add-on that "
        "also raises the email allowance. There's a free 14-day trial of the FULL "
        "product — Pro + Concierge, every feature open, the CRM and the Concierge "
        "included — on one mailbox; it ends at 14 days or 100 handled emails, "
        "whichever comes first. No card to start, and no free-forever tier. Bigger operations (multiple sites, custom volumes) aren't "
        "sold self-serve — email hello@blam8.com. The Usage page shows how many emails "
        "you've handled against your allowance, and you can change or cancel your "
        "subscription (upgrade, downgrade, update card) through the secure Customer "
        "Portal from the billing page. For current prices, see the Plans page or just "
        "ask."),

    "Customer portal — how customers book online": (
        "Every BLAM8 business has a customer portal at "
        "blam8.com/c/{business-slug} — the exact link is shown in Settings under "
        "Customer portal. It is invite only: your customers, the ones your emails "
        "already reach, can check and move their bookings, book again and ask "
        "questions by conversation there. They get in with the reference code on every email, "
        "or a code sent to the address you hold. Strangers are turned away with "
        "'This page is for your business's customers' and never reach the AI, and "
        "there are no 'Book now' buttons or snippets for a website. Booking is "
        "conversational: the AI "
        "checks the real diary, offers real slots, gathers the business's required "
        "intake details, holds the slot and creates the booking in the diary and "
        "CRM. No customer login is needed — the reference code from any BLAM8 "
        "email identifies the customer; a customer who has lost it can type the "
        "email address the business has for them and the code is emailed there. "
        "Someone who isn't a customer yet can't sign up on the portal — they "
        "email the business, and the reply carries their code and link. Anything the "
        "AI can't handle is passed to the business's inbox, and the business gets "
        "a push notification when a customer books, reschedules or escalates. The "
        "portal is customisable in Settings: business name, description, service "
        "area, welcome message, and a 'quote prices in the portal' toggle for "
        "businesses that would rather take details for a personal quote."),

    "Sharing your customer portal link": (
        "Your customer portal lives at blam8.com/c/{your-business-slug} — the "
        "exact link and slug are in Settings under Customer portal. The portal is "
        "invite only, so there's nothing to publish: every email BLAM8 sends "
        "already includes the portal link and the customer's reference code in "
        "the sign-off, and that is the invitation. Don't put the link on your "
        "website, Google Business Profile or social bios — a visitor without a "
        "code can't get in. New customers reach you by email as usual, and your "
        "reply invites them."),

    "Reference codes — how customers access their bookings": (
        "Every CRM contact gets a unique reference code, generated automatically "
        "from the business name plus four characters (for example ACME-7K2M). The "
        "code is included in the sign-off of every email BLAM8 sends, alongside "
        "the portal link. Customers use it on the customer portal "
        "(blam8.com/c/{business-slug}) to check their booking, reschedule, or ask "
        "questions — without calling the business and without any account or "
        "password. The code is the invitation: the portal is invite only. If a "
        "customer doesn't have their code, they type the email address the "
        "business has for them and it is emailed there."),

    "Smart inbox labels — how emails are categorised": (
        "Every incoming email is automatically categorised the moment BLAM8 "
        "ingests it — no rules to set up and no extra processing cost. The six "
        "labels: Needs Reply (a customer wrote something that needs an answer), "
        "Draft Ready (BLAM8 has drafted a reply, waiting for approval), Replied "
        "(approved and sent, awaiting the customer's response), Booked (a booking "
        "is held or confirmed on the thread), FYI (receipts, newsletters, "
        "confirmations — no action needed), and Automated (machine-generated "
        "system mail, correctly skipped). Labels transition automatically as the "
        "conversation progresses — a new enquiry starts as Needs Reply, becomes "
        "Draft Ready when a reply is drafted, then Replied when it's approved "
        "and sent, and flips back to Needs Reply if the customer answers. The "
        "inbox filter bar shows live counts per category, and clicking the "
        "category chip on any conversation changes the label manually."),

    "The Concierge — the assistant that takes actions": (
        (
            "The Concierge is BLAM8's built-in AI assistant for the business owner and their team (not for "
            "customers). The person icon opens her; hold it for a second to talk. She answers from your "
            "records — look someone up by a dependant's name, which stays are in on a given day, which "
            "invoices are overdue, which documents are running out. She also sets up action cards: draft "
            "an email or send one that's waiting, block or reopen days, change a stay, update a "
            "dependant's details, ask a customer for a renewal, and hand a job to someone on your team. "
            "She can also move or cancel a booking, update a deal, add a follow-up, a contact or a "
            "knowledge entry, and chase quotes and invoices. Every action is confirm-first: she proposes, "
            "you see exactly what will happen, you confirm, it runs — nothing is sent, moved or created "
            "without your approval, and everything is audit-logged. A card you didn't answer waits with a "
            "⏳ and comes back checked against today's data; what's done is listed with a ✓. She knows who "
            "is asking and what they may do. The morning brief opens straight into her. The Concierge is "
            "part of the Pro + Concierge plan, and open during the free trial. Starter and Pro don't "
            "include her; they keep App help, reached from the search box, for how-to-use-the-app "
            "questions."
        )),

    "Installable app and push notifications": (
        "BLAM8 installs as an app — a Progressive Web App — on your phone, "
        "desktop or tablet, straight from the browser: no app store, no download "
        "page. In Chrome or Edge, open blam8.com, sign in, and use the 'Install "
        "BLAM8 on this device' card at the top of Settings (or the browser "
        "menu's own Install option); on iPhone/iPad Safari it's Share → Add to "
        "Home Screen. Once installed, BLAM8 opens in its own window, sits in "
        "your dock or taskbar with right-click shortcuts to Inbox, CRM, Diary "
        "and Concierge, and sends push notifications — when a draft is ready "
        "for your approval, and when a portal customer books, reschedules or "
        "escalates. Enable notifications from the prompt after logging in, or "
        "from Settings. The app shell loads from cache, so it opens instantly "
        "and shows a clear banner if you're offline."),

    "Finding your way around the dashboard": (
        "From the dashboard menu you can reach: your Inbox and approval queue (drafts "
        "waiting for you), Knowledge (teach the agent facts), the Diary (scheduling) and, "
        "with Field OS · Transport, its cockpit, transport board and Fleet, plus "
        "Mailboxes (on enterprise accounts), Usage, Settings (voice, rules, auto-send) and "
        "your Ignored-contacts list. Tell the assistant where you want to go and it can "
        "point you there."),

    "Feature requests and reporting a problem": (
        "You can ask for new features and report anything that isn't working right, "
        "straight from inside BLAM8. The quickest way is to tell the assistant (this "
        "chat): describe the idea or the problem and it drafts a feature request or an "
        "issue report for you — nothing is sent to the team until you press Submit on "
        "the draft. There is also a Requests & reports page in the dashboard menu with "
        "a form, and it lists everything you've sent with its status (new, reviewing, "
        "planned or done). The roadmap is built from what operators actually ask for, "
        "so both kinds of feedback go directly to the product team."),

    "People — your team, with or without a login": (
        (
            "Adding staff, another team member, a partner or a helper happens in People (Settings → "
            "People), and it is free on every plan — it does NOT need another mailbox or a different plan. "
            "On the People page, 'Invite someone' sends a sign-in link (email, a copy-and-paste link or a "
            "QR code) for a person who will use BLAM8, and '+ Add a freelancer or helper' adds a person "
            "who just does the work and won't use the app — an external helper, a partner, a helper: name, "
            "what they do, and a mobile or email. Each job they're put on is sent to them as a job sheet "
            "(by text when the business has an SMS number, else by email) with a private link where they "
            "tap On my way (the customer is told the arrival time) and Done; if the job moves or is "
            "cancelled they're told. An extra mailbox is only for a separate email address customers write "
            "to (sales@, a second business), never for adding a person. People is where your team lives, "
            "on every plan, and seats are free. The team list can include people who never sign in: add "
            "their name and they can be put on a job, and what they do counts towards how much the diary "
            "lets happen at once. People who do sign in get a role — owner, manager, staff or accountant — "
            "and the owner chooses which parts of the app each person can open and what they can change "
            "there. 'View as' lets the owner see the app exactly as one of their people does, read-only, "
            "so they can check before relying on it. A job can have one person on it or several (one on "
            "the drop-off, another on the collection, say); which is a setting in the diary. Being on a "
            "job puts it on that person's Today and Diary."
        )),

    "Will the AI overload my team? Capacity, hours and who does what": (
        "The AI does not take on more work than your team can do, because it books against the "
        "capacity you set, not against what customers ask for. In the diary you say how much can "
        "happen at once (how many jobs, how many people, or how many places), which days and hours "
        "you work, and how much notice you need. The agent checks that diary before it offers or "
        "confirms anything and places a real hold, so two customers can't both get the last slot. "
        "It never assumes nine-to-five: a business that works seven days a week, split shifts or "
        "irregular hours sets exactly that. Days you're closed are treated as closed, not full. "
        "The people in your team count towards the capacity: what each person does adds to how much "
        "the diary lets happen at once. Every reply is draft-first by default, so nothing goes to a "
        "customer until you approve it; auto-send is something you switch on later, once you trust "
        "the setup. Who does which job stays in your hands. You put a person on a job (on the job "
        "page, or by asking the Concierge, for example 'put Em on Thursday's job'); BLAM8 does "
        "not pick the person for you. A job can have one person or several, which is a setting. "
        "Someone put on a job sees it on their own Today and Diary, and can be sent a job sheet by "
        "text or email with a link to tap On my way and Done. The real risk is not that the AI "
        "overloads your team but that it fills your diary faster than you're used to, if the "
        "capacity numbers are set too generously. Set them honestly, start with drafts you approve, "
        "and loosen it as you gain confidence."),

    "When the diary is full — other dates and the waiting list": (
        "When a customer asks for a date or time that is full, the agent does not overbook and does "
        "not just say no. What it does is the business's choice, in the diary settings under 'When "
        "it's full'. Other dates has three options: off, offer, or offer and hold. 'Offer' names the "
        "nearest free dates and holds nothing, so they may be gone by the time the customer says yes. "
        "'Offer and hold' names them and holds them for a limited time; this is the default. The "
        "waiting list can be switched on separately: the customer is put on a list for the dates they "
        "asked for, and when a place frees up (a cancellation, or a held offer that lapses) the "
        "business either approves the offer first or lets it go out automatically, and the place is "
        "held for a set number of hours. With both on, one reply offers the waiting list first and "
        "the other dates second. A customer who says they're flexible ('any weekend works') skips "
        "the list and is simply offered dates. With both off, the reply says you're full and offers "
        "nothing else. The agent tells 'full' apart from 'closed', 'too soon' (past your cut-off or "
        "notice time) and 'too far ahead' (beyond how far ahead the diary opens), and says the true "
        "one; bank holidays close normal work. While the agent is still gathering a missing detail, "
        "the slot is held for that customer so someone else can't take it in the meantime."),

    "What BLAM8 does not do for your team": (
        "BLAM8's People area is for delegating work, keeping records and sharing information about "
        "jobs. It is deliberately not an HR or workforce tool. It does not build rotas or plan "
        "shifts, has no clock-in or timesheets, does not calculate holiday entitlement or track "
        "absence, does not run payroll, does not do performance reviews and does not give "
        "employment-law advice. It does not automatically choose which team member gets a job; the "
        "owner or manager puts people on jobs. Team members are free on every plan and are not "
        "counted as extra seats. If a question is about something in that list, the honest answer "
        "is that BLAM8 doesn't do it and you'd keep using whatever you use today alongside it. "
        "What it does do for a team: give each person their own sign-in with only the parts of the "
        "app the owner chooses, show each person their own jobs, let people who never sign in still "
        "be put on jobs and sent a job sheet, let you leave a note for a colleague attached to a "
        "particular job or customer, and record who did what in the activity log."),

    "Stays that change — extend, shorten or move": (
        "A stay can be extended, shortened or moved after it's booked. The price "
        "follows the nights: an extra night raises its own draft invoice, and "
        "shortening back cancels that invoice. The owner can do it from the job "
        "page on any plan, and on Pro + Concierge by asking the Concierge, who "
        "sets up a card to confirm."),

    "Getting paid — quotes, invoices and payment settings": (
        "Quotes and invoices are included on every plan. Card payments go through "
        "the business's own Stripe account (Stripe Connect), so a customer can pay "
        "straight from the invoice, and unpaid invoices are chased at 7, 14 and 30 "
        "days. There are settings a business can choose: a deposit to secure a "
        "booking, a higher deposit on peak dates, billing the balance the day before "
        "collection (off by default), and no separate quote when the diary has "
        "already set the price. Describe these as settings the business can switch "
        "on, not as something every account does."),

    "Phone calls and SMS": (
        "BLAM8 doesn't answer phone calls. It answers email and runs the diary. The "
        "Concierge can be spoken to, but her voice is for the owner and their team, "
        "not for callers. SMS and missed-call text-back are built and being tested; "
        "they switch on when testing is done. There is no date to give, so never "
        "promise one, and never describe SMS as live or rolling out."),

    "Common questions": (
        "Yes, BLAM8 works with both Outlook and Gmail. No, it won't send anything without "
        "you unless you turn on auto-send for an inbox — it is draft-first by default. "
        "Yes, you can edit any draft before it sends. Yes, you can run multiple branches "
        "or inboxes from one account. No, your data is never used to train the AI. You "
        "can usually be live on a real inbox within days, with no IT project."),

    "Where to point people — key pages on blam8.com": (
        "When someone asks how BLAM8 works, wants the short version in writing, or asks "
        "for 'the two-minute version', share these public pages. "
        "https://blam8.com/how-it-works walks through onboarding end to end: sign in, a "
        "short interview instead of a settings maze, pick your industry, connect the "
        "mailbox (Outlook or Gmail), teach it your business, then approve drafts from "
        "the queue — plus what BLAM8 needs access to and how heated emails get escalated "
        "to a human. https://blam8.com/security covers data handling and security. "
        "https://blam8.com/help is the help centre, with one-page answers to common "
        "questions. https://blam8.com/capabilities lists what the platform can do, and "
        "current pricing is public on https://blam8.com. Share these links freely — "
        "they are public pages and the fastest way to give someone a straight answer "
        "they can read in their own time."),
}
