# Job parties — many contacts, one job, the right document to the right person

> **Status (2026-09-27, confirmed by the clean-up pass):** Phases 1–3 BUILT (2026-09-02: 05e5dd5, b3806fa, 258a4b0). Landlord + tenant on site proven live 2026-09-26 (verified-behaviours). The party-aware portal is not yet proven live. (The 'DESIGN ONLY' line below is history.)

*Design written 2026-09-01 from the competitive research (complaints-first sweep;
report: "The Complaints Ledger" artifact, session 09-01). STATUS: DESIGN ONLY —
nothing built. The named leak this closes, verbatim from ElectriciansForums
(baldelectrician, thread 202705): "most of these job apps fail in not letting you
have different contact fields — a tenanted property could have 2 tenants, a
landlord, an agent and a managing factor; some apps only allow 1-2 contacts per
job" — and the wrong document goes to the wrong party. The PO carry-through
(customer_reference, shipped b8b0f26) solved the FM paperwork chain; this
completes the people half.*

## Why

Every surface today assumes ONE person per job: `bookings.customer_email`,
`invoices.customer_email`, `quotes.customer_email` — the thread sender becomes
the bill-to becomes the quote approver. For owner-occupier work that's right.
For tenanted property, FM contracts, and anything brokered (agent books, landlord
pays, tenant opens the door) it forces the operator back into manual forwarding —
the exact "back and forth" paperwork BLAM8 exists to kill. No app in the
researched set does this well; the ones that try hard-code a role vocabulary
that doesn't match reality.

Positioning line the research supports: **"The agent books it, the landlord pays
for it, the tenant lets you in — and each of them only ever sees their part."**

## Design principles (each traces to a researched complaint or a BLAM8 doctrine)

1. **Labels are not routing.** What the operator *calls* someone ("landlord",
   "managing factor", "site foreman") is a free-text label. What the system *does*
   with them is a small fixed set of duties: **pays**, **approves**, **on site**,
   **kept informed**. Apps fail by welding the two together — a fixed dropdown
   never fits ("2 tenants AND an agent"), and a free label the machine can't route
   on is decoration. Split them and both problems dissolve.
2. **Zero-config for the 90% case.** A job with no party rows behaves exactly as
   today: the booking's contact holds every duty. Parties are additive; nothing
   about existing jobs, tests or flows changes until a second person exists.
   (Build note, 2026-09-02: `bookings` carries only `contact` — a bare email,
   no name column. The earlier draft of this doc said `customer_email`; wrong.)
3. **Parties are read, not typed.** The platform is proactive: when a thread says
   "I'm the letting agent — the invoice should go to the landlord, John Smith
   (john@…)", the extractor proposes the party + duty as an amber confirm card
   (the crm_fields suggestion machinery, reused). Manual entry exists; the
   pipeline is the primary author. Nobody in the sector ships this.
4. **Money routing is never silent — and extracted money routing is treated as
   hostile until confirmed.** "Send the invoice to this other address instead"
   is the classic invoice-redirection scam (BEC). An extracted suggestion may
   NEVER take the `pays` or `approves` duty by itself: it renders as a
   distinctly-warned confirm card ("⚠ this changes who gets billed — confirm
   you know this person"), logs an agent_event, and the old payer stays cc'd on
   the first re-routed invoice. Operator-entered parties skip the warning but
   still log.
5. **The chain never breaks.** Documents keep working the way they do now:
   quote/invoice rows carry a RESOLVED recipient (customer_email/name filled at
   creation from the duty holder), so rendering, chasing, exports and the
   accountant handoff need no joins and no changes. `job_parties` is the source;
   the document is the receipt of what was resolved.
6. **The CRM is the address book.** Each party links to a `crm_contacts` row
   (created/matched by email, the invoice-generate pattern) so history accrues
   per person: the landlord's contact page shows every invoice across every
   property, the agent's shows every booking they brokered.

## Data model

```sql
job_parties (
  id uuid pk, tenant_id uuid,
  booking_id uuid,                 -- the job. Quotes/invoices reach parties
                                   -- through their existing booking_id links.
  contact_id uuid,                 -- crm_contacts; matched on email at insert
  name text, email text, phone text,  -- captured as stated; email nullable —
                                   -- an on-site tenant may only have a phone
  label text,                      -- operator's word: 'landlord', 'tenant'…
  pays bool default false,         -- invoices, payment chases, statements
  approves bool default false,     -- quotes, acceptance chases
  on_site bool default false,      -- confirmations, day-before reminders, ETA
  informed bool default false,     -- cc on the others' documents, no asks
  source text default 'owner',     -- 'owner' | 'extracted' (unconfirmed)
                                   --   | 'confirmed' (extracted, then approved)
  created_at, updated_at,
  unique (tenant_id, booking_id, email)
)
```

No change to `bookings`, `quotes`, `invoices` — their `customer_*` columns stay,
now meaning "the resolved recipient". One new pure module, `src/app/job_parties.py`:

```
resolve(booking, parties) -> {pays, approves, on_site, informed: []}
```

Fallback order per duty: a confirmed/owner party holding the duty → the
booking's own contact. `extracted` (unconfirmed) rows never resolve (D4).

## Routing table (which document reaches which duty)

| Surface | Goes to | cc |
|---|---|---|
| Quote send + quote chases (check_quote_chases) | `approves` | `informed` |
| Invoice send + payment chases (invoice_chase) | `pays` | `informed` |
| Booking confirmation, day-before reminder, ETA | `on_site` | `informed` |
| Reply drafts | the thread sender, always — a reply is a reply |
| Portal links / reference codes | each party gets their own (Phase 3) |

The reply drafter gains party *context*, not party routing: the compose prompt
learns "you are talking to the letting agent; the landlord pays; the tenant is
on site" so drafts stop conflating them ("your invoice" to someone who never
pays). Chase engines need no logic changes — they already follow the document's
customer_email, which resolution filled correctly at creation.

## Phases

**Phase 1 — manual parties + routing (the useful core).**
Schema + `src/app/job_parties.py` + a "Parties" fold on the diary job detail
(the crm_contact folded-row idiom: label bold, name · email quiet, duty dots) +
resolution at quote/invoice creation + cc for `informed` on sends. Quote/invoice
detail pages show "To: John Smith (landlord — pays)" so the routing is legible.
CRM contact page grows a "Party to" line under deals. Export: parties ride
`bookings.json` in the exit bundle automatically.

**Phase 2 — proactive extraction (the differentiator). BUILT 2026-09-02.**
crm_extract-style pass over inbound email on threads with a booking: propose
party + label + duty as confirm cards (thread ghost + job page), with the D4
hostile-until-confirmed treatment for `pays`/`approves`. Accepting creates the
crm_contact, the job_party row, and — where the duty changed a live document's
recipient — offers the re-send explicitly. New llm operation → register in
usage.SOURCE_BY_OPERATION (standing trap).

*Build notes (2026-09-02):* `src/app/party_extract.py` (operation `party_extract`,
Haiku, booking-threads only — everyone else pays nothing); the suggestion IS
the `job_parties` row (`source='extracted'`, new `evidence`/`email_log_id`
columns) — no separate suggestions table; confirm cards live on the diary job
page (rose + explicit warning for pays/approves, amber otherwise, evidence
quoted verbatim), the thread gets a `party_suggested` agent event. A claim
with no unambiguous job (several live bookings, no stated ref) is DROPPED —
which job a payer belongs to is exactly the wrong thing to guess. Dismiss
deletes the row, so a later email repeating the claim re-proposes it
(deliberate: persistent claims deserve re-surfacing). The re-send offer was
trimmed to D4's cc guard: the FIRST invoice routed to a confirmed re-routed
payer ccs the displaced payer exactly once (`redirect_ccd` column,
`cc_for_document(redirect_guard=True)` on invoice sends only).

**Phase 3 — the EXISTING portal becomes party-aware. BUILT 2026-09-02
(hold lifted by Julius).**
Not a new surface: /c/{slug} and reference codes already exist — but today a
job effectively has one portal identity. This phase mints each party contact
its own reference code (crm_contacts.reference_code, per-party) and scopes the
portal view by duty: `on_site` sees and reschedules the visit, `pays` sees the
invoice and Pay Now, `approves` sees the quote.

*Build notes (2026-09-02):* smaller than feared, because the portal was
already email-keyed — once Phase 1 routes documents to duty holders, `pays`
and `approves` see their documents by the existing email match, and codes
mint for them via the existing outbound footers. What Phase 3 added:
`db.job_party_memberships` (trusted rows only — an extracted claim grants no
portal capability, D4 again); the on-site party sees the visit in chat AND on
the ?ref= landing (labelled with the operator's own role word) and may
reschedule it — on_site is the ONLY duty that grants the diary, a payer
cannot move an appointment at someone else's property; the day-before
reminder now carries the recipient's own ref-code footer (their portal door,
minted on first need; still dropped when a cc rides along); and duty guards
on the money tools — a verified party without `pays`/`approves` gets an
explicit model note never to discuss the job's prices, so an on-site tenant
cannot chat their way to the landlord's invoice. No migration needed.

## Deliberately not doing

- **No per-party pricing/splits.** One invoice, one payer. Split billing is a
  different (rarer) ask; do not let it into this schema.
- **No role vocabulary.** No enum of landlord/tenant/agent anywhere in code —
  labels are text, duties are the four booleans. Verticals name their own world
  (TMS: "consignee", events: "venue manager") for free.
- **No routing on replies.** Conversations stay with whoever is talking.

## Decisions (Julius, 2026-09-01)

1. **`informed` cc is opt-in, always.** Nothing is ever cc'd unless the
   operator (or a confirmed suggestion) explicitly ticks *Kept informed* on
   that party. No implicit copies of money documents, ever.
2. **Parties are edited on the diary job page only** (Phase 1). Quote and
   invoice pages *display* the resolved routing ("To: John Smith — landlord,
   pays") but do not edit parties — one place owns the truth.
3. **Phase 3 waits for real demand.** The portal already exists; making it
   party-aware is held until a tenanted-property/lettings tenant asks.

## Build order (Phase 1)

1. Migration (`job_parties`) + `db.list_job_parties/upsert_job_party/delete_job_party`.
2. `src/app/job_parties.py` (pure resolve + duty labels) + tests.
3. Diary job page Parties fold + POST routes.
4. Resolution at the three quote/invoice creation sites + `informed` cc in
   `quote_send`/`invoice_send`/`invoice_chase`.
5. Detail-page "To:" legibility + CRM contact "Party to" line.
6. Compose-prompt party context for threads with a multi-party booking.
