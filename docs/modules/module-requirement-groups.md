# Requirement groups — the booking is about a dependant of the customer

*Design written 2026-09-02 from the Revelation Pets gap analysis (LittleDogs
kennels/daycare, session 09-02). STATUS: Phases 1–3 BUILT 2026-09-04 (see *Built* at the end). The gap
this closes: BLAM8 models customers, threads and jobs, but has no "dependant
with its own history". A dog daycare's unit of record is the dog — breed,
neutering status, feeding instructions, vaccine expiry — and today that data
smears across `bookings.details` (re-collected every stay, no history) and
`crm_contacts.custom_data` (flat per contact: two dogs in one household
collide). The same shape is childminders (the child), garages (the vehicle),
livery yards (the horse), equipment repair (the machine), lettings cleaning
(the property). One pattern, config surface only, zero verticals.*

## Why

Vertical pet-care SaaS (Revelation Pets, ~£600/yr) wins the LittleDogs-shaped
business on exactly four capabilities: a per-dog profile, vaccine expiry as a
booking gate, remembered care instructions, and multi-dog households. Every one
reduces to a single universal mechanism: **a group of booking requirements that
is repeatable per customer and remembered between bookings** ("Copied
Requirements", Kris 09-02). BLAM8 already owns each half of the loop —
`scheduling_config.intake_fields` asks and hard-gates; `crm_field_definitions`
defines, extracts with confidence chips, and (as of 6bf22b2, unmerged) probes —
they just operate on the wrong nouns. This module gives their machinery the
right noun and changes neither pipeline's character.

Positioning line: **"Tell BLAM8 what a booking is about — it asks, remembers,
and never lets a lapsed certificate slip through."**

What stays out on purpose: multi-night occupancy inventory (N bedrooms,
check-in/out). That is a different scheduling engine, not a config surface —
see *Deliberately not doing*.

## Design principles

1. **The label is the operator's word; the mechanics are the system's.**
   A group is called "Dog", "Child", "Vehicle", "Horse" — free text, exactly the
   `job_parties` label doctrine. No industry enum, no sector template in code.
   What the system does is fixed and small: ask, extract, remember, prefill,
   gate, chase.
2. **Zero-config for the 90% case.** A tenant with no groups behaves exactly as
   today: `intake_fields` collects booking-scoped details into
   `bookings.details` and nothing else exists. Groups are additive; no existing
   flow, test or prompt changes until a group is defined.
3. **Fields carry durability, not industry meaning.** Each field in a group is
   `once` (breed — collect on first meeting, never re-ask), `confirm`
   (feeding instructions — prefill and re-confirm each booking: "still two
   scoops morning and night?" reads as care, not admin), or `expires` (a date
   the field invalidates itself on — vaccine expiry, MOT due, insurance
   renewal). The vaccine gate and the certificate chase are not features; they
   are what `expires` + `required` means.
4. **Instances are read, not typed** (the party-extraction doctrine). The
   pipeline fills instance fields from what customers write — the
   `crm_fields` extraction/confidence pattern reused: ≥0.75 autofill, mid-band
   renders a confirm chip, below dropped. Manual entry exists; the inbox is the
   primary author. This is the moat: the vertical SaaS makes the owner type
   Rex's vaccine date into a form; BLAM8 reads the vaccination card email.
5. **The gate evaluates per instance, not per contact.** "Board Rex and Bella"
   links the booking to two instances; Rex's lapsed vaccine blocks Rex, with
   Bella unaffected. The hold refusal names the instance and the field, in the
   operator's own vocabulary ("Still needed for Rex: Vaccination expiry").
6. **The agent carries the generalisation; the schema carries none.** Nothing
   in code ever knows what a dog is. The concierge, the onboarding interview
   and the drafter are taught ONE transferable concept — "a booking may be
   about a dependant of the customer" — and they instantiate it per tenant in
   the tenant's words. This is what makes "most businesses can config" true:
   the operator describes their world in prose; the agent translates prose into
   a staged group definition; a confirm card makes it real. See *Teaching the
   agent the pattern*.

## Data model

```sql
requirement_groups (           -- the definition ("what a Dog is", per tenant)
  id uuid pk, tenant_id uuid,
  label text,                  -- operator's word, singular: 'Dog'
  label_plural text,           -- 'Dogs' (prompt + UI copy); derived from label
                               --   when not stated, in the editor and in
                               --   assistant proposals alike
  cardinality text default 'many',   -- 'one' | 'many' per contact
  fields jsonb not null,       -- ordered [{key, label, type(text|number|date|
                               --   bool|dropdown), options, required,
                               --   hint,           -- free English for the model
                               --   durability('once'|'confirm'|'expires'),
                               --   gate bool}]     -- expires+gate: date must
                               --                      cover the booking
  display_order int, created_at, updated_at
)

requirement_instances (        -- a filled copy ("Rex")
  id uuid pk, tenant_id uuid,
  group_id uuid,
  contact_id uuid,             -- crm_contacts: the household/owner
  name text,                   -- 'Rex' — how everyone refers to it
  data jsonb not null default '{}',       -- {key: value}
  field_meta jsonb not null default '{}', -- {key: {confirmed_at, source
                                          --   ('owner'|'extracted'|'confirmed'),
                                          --   email_log_id}}
  created_at, updated_at,
  unique (tenant_id, group_id, contact_id, name)
)

booking_subjects (             -- which instance(s) a booking is about
  booking_id uuid, instance_id uuid, tenant_id uuid,
  primary key (booking_id, instance_id)
)
```

No change to `bookings`, `intake_fields`, or any document table. Booking-scoped
details (preferred date, access notes) stay in `intake_fields` →
`bookings.details`; group fields live on the instance and are *referenced* by
the booking. Field validation, extraction and confidence gating reuse
`src/app/crm_fields.py` as code (validate_definition-style limits: ≤10 fields per
group, ≤3 groups per tenant to start), not as tables.

## The collection loop (the "copied" in Copied Requirements)

| Moment | What happens |
|---|---|
| First contact, group defined | Drafter's `<booking_intake>`-equivalent block lists the group's required fields; extraction fills the instance as answers arrive (confidence-gated, chips for mid-band) |
| Booking hold | `missing_required` extended: booking-scoped fields checked on `bookings.details` (as today) AND, per linked instance, required group fields checked on `instance.data`. Any gap → hold refuses, inbox shows "Still needed for Rex: …" |
| Later booking, same household | Instances prefill. `once` fields never re-asked; `confirm` fields prefilled + one-line re-confirmation woven into the reply (probe idiom — one question, never a form); `expires` fields checked against the booking's dates |
| Expiry approaching | `expires` fields feed the existing chase engine as milestones ("Rex's vaccination expires 12th Oct — before his stay on the 15th"), stand-down on reply, exactly like payment chases |
| Multi-instance | Cardinality 'many': extraction may propose a NEW instance ("we've got a new puppy, Biscuit") as a confirm chip; accepting creates the row |

## Teaching the agent the pattern (the point of the module)

The claim to operationalise: *with the agent's help, most businesses can
config.* Four surfaces, one concept:

1. **One prompt concept, everywhere.** The drafter, hold executor and concierge
   learn a single block — "this tenant's bookings may be about
   `{label_plural}`; each has these fields; collect, confirm, and gate as
   marked" — rendered from config the way `<booking_intake>` is today. No
   industry text in any prompt. A kennels tenant and a childminder differ only
   in the rendered JSON.
2. **`add_requirement_group` concierge action** (confirm-first, staged like
   every business action). The operator pastes prose — "we look after dogs;
   we need breed, age, neutering status, feeding instructions, medications,
   vet details, behaviour notes; vaccines must be in date or no booking" — and
   the concierge stages a complete group definition (label, fields, durability,
   gates) as a card. Tap to apply. This is config-not-coding executed *by the
   product*: the settings surface becomes something the agent can drive, which
   the 09-02 ground-truth sweep showed it cannot today (its write surface is
   never_do, intake fields via fix-flow, KB entries, CRM stages — nothing
   structural).
3. **The onboarding interview asks the one diagnostic question.** "When someone
   books you, is the booking about something or someone that belongs to them —
   a pet, a child, a vehicle, a property?" A yes routes the tenant's own answer
   through the same prose→group translation and proposes it in the wizard.
   The existing `PROFILE_INTAKE` sector packs (kennels already ships pet +
   vaccine fields) demote from code-seeded intake lists to *suggestion data*
   for this step — packs become examples the agent adapts, never templates the
   product hardcodes.
4. **Proactive detection, later.** The settings-recommend idiom: when
   extraction keeps meeting the same dependant-shaped references in a tenant's
   threads (named non-contacts with recurring attributes), the concierge
   surfaces "Your bookings seem to be about dogs — want BLAM8 to remember each
   one?" with the staged group attached. Ship only after 1–3 prove the
   translation quality.

## Phases

**Phase 1 — schema + manual config + the loop's spine.**
Migrations; `src/app/requirement_groups.py` (pure: validate, render_for_prompt,
missing_required-per-instance, prefill resolution); groups editor beside
`/app/diary/intake` (same fold idiom); booking→instance linking on the diary
job page (parties-fold idiom); gate wired into the hold executor; CRM contact
page grows instance cards (Rex · Bella) under the household. Zero-config
principle holds throughout.

**Phase 2 — extraction + durability semantics (the differentiator).**
Instance fill from inbound email via the crm_fields confidence pattern (new llm
operation → register in `usage.SOURCE_BY_OPERATION`, standing trap); `confirm`
re-asks and new-instance proposals through the probe machinery (wired 6bf22b2 —
merge that branch first); `expires` fields feeding chase milestones with
booking-date awareness.

**Phase 3 — the agent as configurator + portal.**
`add_requirement_group` concierge action; onboarding interview question;
portal: a household's reference code shows its instances, lets the customer
update `confirm` fields and upload the vaccination card before arrival
(extraction reads it; nothing auto-applies without the confidence gate).
Proactive detection last, evidence permitting.

## Deliberately not doing

- **No occupancy inventory.** Bedrooms/date-range capacity is a new scheduling
  engine serving hire- and stay-shaped businesses; it must be a deliberate
  build decision, never smuggled in as "config". The gate here blocks a
  booking; it does not allocate a resource.
- **No industry vocabulary in code.** No `pet`, `child`, `vehicle` anywhere —
  labels are tenant text. Sector packs are suggestion data, not schema.
- **No document-management system.** A vaccination card is an email attachment;
  we store the extracted date + `email_log_id` provenance in `field_meta`, not
  a file vault.
- **No per-instance pricing.** "Two-dog trial night" is a pricing question for
  quotes/invoicing, not for this schema (same discipline as job-parties'
  no-splits rule).

## Open decisions

1. **Operator-facing name.** "Copied Requirements" (Kris's coinage) vs
   something like "What bookings are about". The settings fold copy decides it;
   schema names stay `requirement_groups` regardless.
2. **Instance identity edge cases.** Rename ("Rex" → "Rex jr"?), death/sale of
   the dependant (archive flag vs delete — history argues archive), transfer
   between households (out of scope until asked?).
3. **Limits.** 3 groups × 10 fields × how many instances per contact? Propose
   20, revisit on real data.
4. **Sequencing.** This is core-roadmap work, not the current programme.
   Per customers-first: hold for a second dependant-shaped prospect, or pull
   forward if the trades vertical wants `expires`-gating (gas certs) sooner —
   the primitive is shared.

## Build order (Phase 1)

1. Migrations (`requirement_groups`, `requirement_instances`,
   `booking_subjects`) + db helpers.
2. `src/app/requirement_groups.py` pure module + tests (validate, render, gate,
   prefill).
3. Groups editor UI + instance cards on CRM contact page.
4. Booking↔instance linking on diary job page + hold-executor gate.
5. Prompt block rendering into both draft paths (office/diary + email-only).
6. Inbox "Still needed for {name}" legibility.

## Built — 2026-09-04 (Phase 1 + the booking-side half of Phase 2)

Kris's word for it in the product: **Dependants** (Diary ▸ Dependants).

- Schema: `requirement_groups`, `requirement_instances`, `booking_subjects`
  (scripts/db/migrations/apply_requirement_groups_migration.py, applied to prod).
- `src/app/requirement_groups.py` (pure): definitions (≤3 groups × ≤10 fields,
  durability once/confirm/expires, gate), loose label matching, placeholder
  rejection, per-instance gate with expiry-vs-booking-date, durability merge,
  the `subjects` tool argument, prompt blocks, the editor form parser.
- The hold executor (`scheduling.make_executor(groups=…)`): every hold/offer
  needs `subjects` when a group exists; each subject is prefilled from the
  customer's record, gated per instance ("Still needed for Tilly: Current
  shoeing"), written back as an instance and linked to the booking; the
  names land in the booking's details under the tenant's plural ("Horses:
  Murphy, Tilly") so every existing surface shows what the visit is about.
- The drafter: the definition block in the scheduling prompt, `subjects` on
  hold_booking/offer_slots, the customer's instances in <customer_context>
  with their gaps. The portal: same executor, same argument on
  create_booking, the instances in <customer_record>.
- UI: Diary ▸ Dependants editor; the contact card's fold per kind (edit, add,
  archive); the job page's About block (link/unlink).

### Phases 2 + 3 — 2026-09-04 (2672180)
- `src/app/dependants_extract.py`: one small call per inbound email (operation
  `dependant_extract`, only for tenants with a kind + an email with a contact):
  sure reads on a known dependant write by durability (a `once` field on record
  is never overwritten by a read); middling reads, `once` contradictions and
  NEW dependants become chips (`crm_field_suggestions` entity `dependant`,
  `__new__:<group_id>` for a new one); accepting writes / creates. The contact
  card shows the chips in the kind's fold.
- `confirm` re-asks ride the prompt (the field description says "confirm it
  still stands in one short line"); the portal and drafter carry the record.
- `src/app/dependants_sweep.py`: daily 08:05 — a gated expiry lapsing before an
  upcoming booking (or within 14 days) raises one feed row + push
  (`dependant_expiry`, Bookings family, "Lapsing" chip). A new date read from
  the reply is the stand-down.
- Concierge `define_dependants` (every tier): prose → a staged kind card →
  Confirm saves (updates an existing kind by label).
- Onboarding: the interview asks the one diagnostic question and records the
  answer in the brief; the settings assistant has a `dependants` recommend
  page whose card fills the editor (concierge.js prefill).
- Portal `update_dependant`: the identified customer updates or adds their own
  (source `customer`; a `once` field stands as the business set it); the
  prompt says to pass subjects and to keep the record current. Documents still
  go by email.
- Not built, on purpose: proactive detection ("your bookings seem to be about
  dogs"), per-instance pricing, occupancy inventory, a document vault.
