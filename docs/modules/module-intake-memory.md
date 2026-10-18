# Remembered booking requirements — a row's answer kept on the customer's record

*Built 2026-09-21 from Kris's question: "is the Booking requirements info saved on
the Contacts CRM for future bookings?" It was not. Every answer the gather flow
collected lived on its own booking, so a repeat customer was asked their feeding
instructions, their licence number, where to park, on every visit — while the
inscription Kris sent by email sat on BK-1005 alone (2026-09-04) and the phone
number rode four bookings before the built-in extraction lifted it (2026-09-03).*

## The decision

**Per field, default off, with the concierge recommending the setting.** Not a
global default. Two reasons, both from live runs:

1. The wrong-reuse failure is the worse one. On 2026-09-04 a landlord's CP12
   detail rode into a service at a different property. A booking held with the
   wrong fact is silent and costly; a re-asked question costs one line of email.
2. The label is tenant text, so the system cannot tell which kind of fact a row
   is. "Address" is a customer fact for a homeowner and a job fact for a
   landlord. "Inscription" is always per job. Only the owner knows, per row.

The default stays painless because the concierge already recommends the
checklist per business type; it now recommends the durability with each row.

## The mechanism

One vocabulary, borrowed from a dependant's field (`src/app/requirement_groups.py`):

| `remember` | Meaning | On the record as |
|---|---|---|
| `ask` (default, never stored) | about this booking; asked every time | nothing |
| `once` | about the customer; asked on the first booking, read from the record after | text field, durability once |
| `confirm` | about the customer; read from the record and confirmed in one line each visit | text field, durability once |
| `expires` | a date on the customer's record that lapses | date field, durability expires, recall 30 |

A document row and the reserved stay field are never remembered.

**One store, not a third.** Switching a row on promotes it to a customer field
(`crm_field_definitions`, entity contact) on save — `intake_memory.sync_definitions`
adopts an existing definition by slug or creates one, and writes its key onto the
row as `crm_key`. The drafter's `<customer_context>`, the portal profile and the
contact page all read the value where they already read customer fields; the
email extraction pass reads it from emails too (the hint is the extraction hint).
A row that cannot be promoted (the ten-field cap, a ladder collision) drops back
to ask-every-time and the page says so — the owner is never left believing a
fact is kept when it is not.

**The gate reads before it refuses.** `scheduling._intake_gated` (email and
portal alike) calls `intake_memory.prefill_from_record` before `missing_required`:
a remembered row the record can answer is filled in — only where this call
carries no real value, because what the customer says now wins over what they
said last time. A lapsed `expires` date is left unfilled and named in the
refusal ("on the customer's record, Insurance expiry lapsed 1st Mar 26 — ask for
the new date"). A successful hold lists what it used under
`prefilled_from_record`, and the message says never to ask for it; a `confirm`
row is checked in one line ("still two scoops morning and night?").

**What the customer gives is written back.** `values_to_remember` takes the
real, in-scope values this call carried on remembered rows (dates ISO-only,
typed fields coerced like extraction) and `remember` merges them onto the
contact with overwrite — the owner said the row is about the customer, so the
customer's word is the new truth. A first-time customer has no record when the
gate runs (CRM ingest is after the draft), so the values park on the executor
state and `pipeline` flushes them after ingest (`flush_pending`), logging one
Agent Log line for everything the email kept. The manual add does the same
through `remember_by_email`.

**The drafter is told.** `intake.render_for_prompt` flags each remembered row
(REMEMBERED / ON FILE / a DATE kept on the record) and adds one rule about
`prefilled_from_record`, so the reply never asks for a fact on file the way the
silent sender-address fill once did (2026-09-03).

**The concierge recommends it.** The `field` row in `settings_recommend` gained a
fifth part — `remember: once, confirm or expires` — with the guidance on which
facts are which; `clean()` moves the part to the fifth slot wherever the model
put it, and the card script fills the row's select from it. The Customer fields
page's own entry now points a booking-needed fact back to a booking row with a
remember part.

## Files

- `src/app/intake.py` — `remember`/`crm_key` on a row, `clean_remember`, `remembered`,
  `REMEMBER_FACE`, prompt flags, `parse_form` columns.
- `src/app/intake_memory.py` — promotion, prefill, notes, write-back, the flush.
- `src/app/scheduling.py` — `_record_ctx`/`_record_defs`, the gate's read and write.
- `src/app/pipeline.py` — the post-ingest flush.
- `src/app/dashboard.py` — the save (sync + notice), the page context, the manual add.
- `src/app/templates/app/diary_intake.html` — the per-row select, faces, the notice.
- `src/app/settings_recommend.py`, `src/app/static/concierge.js`, `src/app/concierge.py`,
  `src/app/settings_guide.py` — the recommendation and its card.
- `tests/test_intake_memory.py`.

## Not done, on purpose

- No hard gate on a customer-supplied lapsed date (a dependant's `gate` flag does
  that per instance); an intake `expires` row is remembered and re-asked once it
  lapses. Add a gate toggle if a run shows a booking slipping past one.
- The manual add form does not prefill from the record (the owner has the
  contact page); it writes back only.
