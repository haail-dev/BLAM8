# Morning debrief ("the concierge briefs you, before the day does")

Status: Phases 1 + 2 BUILT 2026-08-02 (D1/D5/D6 as decided; migration applied
to prod first). Phase 3 not built. **Phase 4 — the live walk — BUILT 2026-10-14
on branch `claude/debrief-live-concierge`: offline-tested only, NOT yet proven on
a device or live tenant, not merged or deployed** (see "Phase 4" below).

## The gap

The 2026-08-01 codebase inventory (§19, `docs/BLAM8_current_inventory.md`) found
"morning debrief" on the believed-feature list with **zero matching code**. Two
existing surfaces get mistaken for it, and neither is it:

- **The Today dashboard** (`GET /app/`, `src/app/dashboard.py:320`) — today's
  strip, gone-quiet card, running-hot card, pending queue. Pull only: the
  owner has to think to open the app.
- **The concierge notification bubble** (`GET /concierge/notifications`,
  `src/app/concierge_routes.py:963`; rows written by `src/app/push.py:_record_notification`)
  — the "while you were away" feed. Event-driven: one row per event, written
  at event time, badge glows on rejoin.

And the config half: **no per-tenant time setting exists for anything.** All
13 daily jobs are hardcoded UTC constants in `src/app/main.py:237-316`
(`scheduler.run_daily`), global across tenants.

## Positioning: why this is not the bubble again

The bubble answers *"what happened while I was away?"* — backward-looking,
event-granular, triggered by rejoining the app.

The debrief answers *"what does today need from me?"* — forward-looking,
**aggregated**, triggered by the clock. It reaches the owner **before** they
open the app (push to the phone at 07:30), which the bubble by definition
cannot do — the bubble requires the visit the debrief is trying to earn.

**The non-duplication contract (hard rule for the composer):**

1. The debrief never re-announces individual events. "Quote Q-1007 accepted"
   fired as its own push/feed row at event time and stays there. The debrief
   only carries **state aggregates** ("2 quotes awaiting a decision, one
   expires today") and **forward-looking facts** ("6 bookings today, first at
   08:30") — things no event row ever contains.
2. One debrief = one `concierge_notifications` row + one push, tagged
   `debrief-{date}` so a redelivery replaces rather than stacks.
3. **A quiet day sends nothing.** If every section is empty, no push, no feed
   row. Silence is the feature that keeps the loud days credible.

## Decisions

| # | Decision | Choice |
|---|----------|--------|
| D1 | Default on or off? | **DECIDED (Julius 2026-08-02): set during onboarding.** New tenants choose in the flow — the AI interview asks for it in its preferences phase and writes the config; the manual path gets a pre-checked card (on, 07:30) so skipping never strands the setting. Columns still default `true` / `'07:30'`, which is what existing tenants (who never re-enter onboarding) silently get — with the Settings card as their off-switch. The brief self-silences on empty days, so a default-on tenant with nothing happening hears nothing. |
| D2 | Timezone | **Europe/London fixed**, matching `scheduling.DIARY_TZ` (`src/app/scheduling.py:50`). UK product; a `tenant_tz` column is the future generalisation seam, not built now. Setting is HH:MM local, 15-minute grid, default **07:30**. |
| D3 | LLM or not | **Zero-LLM, deterministic assembly** (Phase 1+2). Same reasoning as the events run-sheet job (`src/app/main.py:314`: "a wrong carriage time is a real liability") — a briefing that misstates the diary burns trust at 7am. Also: zero marginal cost, no interaction with `TRIAL_CONCIERGE_CAP`. An optional voice pass is Phase 3, behind its own flag. |
| D4 | Delivery surfaces | Push (`push.notify`, kind `morning_debrief`) + the concierge feed row it records automatically. Deep-link `url=/app/` — the Today dashboard *is* the detail view; the debrief is the knock on the door, not a second dashboard. No new page in Phase 1. |
| D5 | Concierge chat injection | **DECIDED (Julius 2026-08-02): yes, Phase 2.** Insert the debrief as an assistant-role `concierge_messages` row (with `links`), so opening the concierge in the morning reads as the concierge having greeted you — and "anything need chasing?" follow-ups have the brief in-context. Cost: one transcript row/day against the 40-row history window. |
| D6 | Closed-day behaviour | **DECIDED (Julius 2026-08-02): follow the business's real schedule.** The tenant already declares open days — `working_days` (default `"mon,tue,wed,thu,fri"`, `src/app/scheduling.py:65`) plus `blackouts` date list, both in the scheduling config. Default behaviour: **no brief on a closed day**. Override toggle `debrief_on_closed_days` (default `false`) for owners who want it anyway — a closed day can still hold overdue invoices and waiting drafts. Tenants without a configured scheduling module have no schedule to consult: every day counts as open (self-silencing still applies). |

## Content — sections, in render order

Every section is best-effort (read failure skips the section, never kills the
run — the dashboard-card contract, `src/app/dashboard.py:326`), renders only when
non-empty, and is module-gated exactly as its source surface is.

1. **Waiting on you** — pending drafts count + oldest age
   (`db.list_email_log(status="pending")`, the dashboard queue read at
   `src/app/dashboard.py:44`); pending portal reschedule requests; quote
   questions awaiting an answer.
2. **Today's diary** — bookings today: total / confirmed / proposed + first
   start time (`db.list_bookings` filtered to today, the board read at
   `src/app/dashboard.py:307`). OS flavour rides the same gates the dashboard
   uses: events today (`events_os.board_view`), assets going out
   (handover job's read), run-sheet-ready note for events tenants.
3. **Money** — overdue invoices: count + £ total (reuse
   `db.list_unpaid_due_invoices`, the 08:45 job's read); quotes open: count +
   any expiring today (`db.list_quotes` status filter). Aggregates only —
   the per-invoice milestone pushes stay the 08:45 job's business.
4. **Relationships** — gone-quiet count + top name, running-hot count
   (`crm.with_recency` honouring `tenant_config.crm_quiet_days`, as
   `src/app/dashboard.py:333`). CRM-gated.

**Assembly:** a pure function `compose(data: dict) -> Debrief | None` in
`src/app/debrief.py` (unit-testable with no I/O). Push title
`"Morning brief — Tue 4 Aug"`; body = the two or three highest-salience lines
(priority: waiting-on-you > money > diary > relationships), respecting the
500-char feed cap (`db.add_concierge_notification`). Returns `None` when all
sections are empty → rule 3.

## Config + migration

`tenant_config` is a **wide table** (`db/schema.sql:77`) — new settings are
new columns, written through the existing `db.upsert_tenant_config` dict path
(the `crm_quiet_days` precedent, `src/app/dashboard.py:3168`):

```sql
alter table tenant_config add column if not exists debrief_enabled        boolean not null default true;   -- D1
alter table tenant_config add column if not exists debrief_time           text    not null default '07:30';
alter table tenant_config add column if not exists debrief_on_closed_days boolean not null default false;  -- D6
alter table tenant_config add column if not exists debrief_last_sent_on   date;   -- dedupe stamp
```

Migration script: `scripts/db/migrations/apply_debrief_migration.py`, applied to prod
**before** merge (standing rule). `debrief_time` validated server-side to
HH:MM on a 15-minute grid.

Settings UI: a card in the Settings notifications area (`src/app/templates/app/settings.html`)
— enable toggle + time `<select>` (15-min steps) + "Also on closed days"
checkbox (shown only when a scheduling config exists — without one the phrase
is meaningless), POSTed through the existing settings handler in
`src/app/dashboard.py`. Keep the `bg-white rounded-xl` card idiom (dark-mode
layer remaps it).

Onboarding (D1): the AI interview (`src/app/onboarding.py`, STEPS
`["interview", "mailbox", "voice", "knowledge", "data"]`) asks in its
preferences phase — "Want a morning brief? What time suits?" — and writes
`debrief_enabled` / `debrief_time` through the same config-filling path it
already uses (a pre-checked card on the interview form — the form POST stays
the only write path). The manual industry-picker fallback is a per-card
click-through, not a settings form — it deliberately carries no card and
relies on the column defaults (on, 07:30), which equal an untouched card.
Either path skipped → the defaults stand; nothing is ever unset.

## Scheduling mechanics — the first per-tenant-timed job

Not another `run_daily` constant. Register in `src/app/main.py`:

```python
app.state.morning_debrief = asyncio.create_task(
    scheduler.run_every(900, run_morning_debriefs, "morning_debrief"))
```

Each 15-min pass (the `send_appointment_reminders` pattern):

1. `now_local = datetime.now(scheduling.DIARY_TZ)`.
2. Cross-tenant read: tenants where `debrief_enabled` and
   `debrief_last_sent_on is distinct from current_date` and
   `debrief_time <= now_local HH:MM` (one filtered select — a new
   `db.list_debrief_due_tenants(today, hhmm)` helper).
3. Per tenant, the **closed-day gate (D6)**: unless `debrief_on_closed_days`,
   skip-and-stamp when today's weekday is not in the scheduling config's
   `working_days` or today is in `blackouts` (the same merged read the diary
   uses, `src/app/scheduling.py:114-131`). No scheduling config → treat as open.
   Stamping on skip keeps the due-read cheap — a closed-day tenant isn't
   re-examined every 15 minutes all day.
4. Per tenant, **stamp `debrief_last_sent_on` first, then compose and send**
   — the `mark_booking_reminded` / overdue-milestone ordering
   (`src/app/jobs/check_overdue_invoices.py:42`): a crash after the stamp costs
   one morning's brief; the reverse costs a double-push. Per-tenant
   try/except so one bad tenant never stops the sweep.
5. Late-start catch-up is inherent: a redeploy over 07:30 sends on the next
   pass (07:30 setting, 07:41 delivery — fine). A tenant enabling at 3pm gets
   today's brief within 15 min iff `debrief_time` has passed and content is
   non-empty; the stamp prevents any repeat.

Single-instance assumption: same as every existing loop (explicit in
`src/app/scheduler.py:8` and `render.yaml:90`).

## Delivery

- `push.notify(tenant_id, title=…, body=…, url="/app/", tag=f"debrief-{today}", kind="morning_debrief")` —
  one call does push + feed row + live-tab SSE glow (`src/app/push.py:72`).
  Kind registers in the tenant feed normally (not in `_FEED_EXCLUDED_KINDS`).
- `db.log_agent_event(tenant_id, "morning_debrief", summary)` — the Agent Log
  is the tenant-visible record that the brief ran.
- `trace.emit` rides free inside `push.notify`.

## Files

| File | Change |
|---|---|
| `src/app/debrief.py` | NEW — pure composer: gather (via injected reads) + `compose()` |
| `src/app/jobs/morning_debrief.py` | NEW — the 15-min sweep: due-read, stamp, send |
| `src/app/db.py` | `list_debrief_due_tenants`, `stamp_debrief_sent` |
| `src/app/main.py` | one `run_every(900, …)` registration |
| `scripts/db/migrations/apply_debrief_migration.py` | NEW — the three columns |
| `db/schema.sql` | mirror the columns |
| `src/app/templates/app/settings.html` + `src/app/dashboard.py` | settings card + POST (incl. closed-days toggle) |
| `src/app/onboarding.py` + interview config writer | D1 — the onboarding ask (interview phase + manual-path card) |
| `tests/` | composer unit tests (each section, empty→None, char caps); due-read/stamp dedupe tests; closed-day gate tests (working_days, blackouts, no-config fallback); settings + onboarding round-trip |

## Phases

- **Phase 1 — the brief exists.** ✅ BUILT. Columns + migration, composer,
  sweep job with the closed-day gate (D6), push/feed delivery, settings card,
  onboarding ask (D1). Zero-LLM.
- **Phase 2 — the concierge owns it.** ✅ BUILT. Chat-transcript injection
  (D5): the brief lands as an assistant turn with section deep-links
  (`compose()["chat"]`/`["links"]`, still zero-LLM); when it's the newest
  turn, the concierge page shows an "Anything need chasing?" follow-up chip
  (detected via `debrief.CHAT_PREFIX`).
- **Phase 3 — optional voice + intelligence.** Single short LLM pass to
  render the same facts in the tenant's register (flagged, metered via
  `llm.create`); trend lines ("quietest week this month"). Also the natural
  seam for the gone-quiet tie-in: "3 leads went quiet — want me to start the
  re-engage sequence?" once trigger-based enrolment exists (inventory §14
  PARTIAL — sequences have trigger types but no engine).

## Phase 4 — the live walk (2026-10-14)

Kris, 2026-10-14: "add the morning debrief into the live Concierge — a little
button with a symbol, maybe top right, that when clicked triggers the live
Concierge to start dealing with your debrief, in the correct grouping priority
order ... so the debrief becomes more of a receptionist."

The brief tells you the day in one go and stops. A receptionist briefing the
person in charge does it in ORDER — what matters most first, one matter at a
time, waiting for the answer before moving on — and can then DEAL with each
thing. The walk is that, on the live voice screen, in both modes (Standard and
GPT-Live).

**Where it lives.** `src/app/debrief_walk.py` (the agenda, the walk state, the note,
the tool), hooks in `concierge_routes.voice_chat` and `gptlive.control`, the
button in `concierge_panel.js` / `_concierge_panel.html`. Tests:
`tests/test_debrief_walk*.py`, `tests/test_debrief_button.py`,
`tests/replays/debrief_button_replay.mjs`.

**The order is the owner's.** "The correct grouping priority order" is the order
set on Today (the cog on Today / Settings › Today's order): `deck.ordered_topics`,
with a group ranking by its loudest fact exactly as a card does (`deck.rank_of`) —
so an appointment about to start lifts the diary, but never past a topic the owner
put first. Two fixed ends: **something that has stopped** (a dead mailbox) always
leads, **good news** always closes.

**What is in it** (generic — every word is meant to read the same to any kind of
business): unhappy customers · questions · replies waiting on the owner's yes (a
thread held WITHOUT a draft — an escalation match, an upset customer, one of the
owner's rules, a missing fact, a failed draft — is said as held, with its reason,
never as "draft ready") · today and tomorrow in the diary (confirmed and proposed
bookings; an offer the customer has not picked is not an appointment) · money
(overdue and due today, saying when a reminder is already drafted; "says they've
paid" is the owner's to check against the bank, not good news) · quotes awaiting a
decision · promises both ways · customers gone quiet · overnight events · good news
from the last 24 h. A thread counts as waiting by Today's rule: the sender's LATEST
row decides. Ignored senders are left out, as on Today. It reads what Today reads, fresh, zero-LLM (D3 holds: a wrong fact at 7am
burns trust), in parallel (`parallel_reads`), each read best-effort — **a read
that fails is named to the Concierge as unread, so she says she could not check
it and never "nothing there"**.

**The owner's brief setting still rules.** Topics switched off under "What the
brief tells you" (`debrief_topics`) are not walked either: one setting, one
answer. Overnight events the brief does not know are told only if they are in
`debrief_walk._NEEDS_OWNER` — a short list chosen by reading what each kind is
(a dispute, a vulnerable-household flag, an alert word, an out-of-hours ask, an
after-the-job deadline, a lapsing customer certificate, "says they've paid"),
each gated by the brief topic that governs it. Card tone is NOT used: amber also
marks "she has told the customer already", a recall prompt and a payment in
flight, none of which is the owner's to do. Events older than three days are
Today's, not the walk's (nothing marks an event seen yet, so age is the gauge).

**How it runs.**

1. The button (top right of the live screen; owner and managers only —
   `people.is_admin`, never while viewing as someone else) sends the owner's
   request, "Run me through my debrief.", with `debrief="start"`. Standard:
   `voice-live.js startDebrief` → `runTurn`. GPT-Live: the data channel takes no
   client commands, so the button asks the server (`gptlive` control operation
   `debrief`), which runs the same backend turn a delegation would and hands the
   verified reply to the native voice as commentary; it never silences the voice.
2. `voice_chat` builds the agenda, begins a **walk** keyed by tenant + person +
   conversation (in process, like `voice_turn_state`; a restart loses it and the
   button simply starts again), and gives the Concierge a note (the current
   group's items and how to run the walk — data, not instructions; customer
   words are stripped of anything that could pose as the note's framing) and one
   extra tool, `debrief_next`.
3. She speaks one group at a time, from the items given (no lookups just to
   brief a group); the owner's answers go through her ordinary tools and cards,
   so confirm-first, the owner's doors and one-card-at-a-time all still apply.
   When the owner is finished she calls `debrief_next` (or the button, now
   showing "2/5", is tapped for "Next."), and the next group's items come back.
4. After the last group she closes in one sentence — what is done, what was left
   for later, anything she could not check.

**Things the dry run against the real model taught** (fake data, 14 Oct): without
"make NO tool call" the opening looked something up first (an extra model round
before she spoke); she promised "say yes and it sends" for a draft that was not on
the screen; offered to cancel an unconfirmed booking instead of pointing at the
diary; and guessed a customer's pronoun. The note now says each of those.

**Not done / known limits.**

- Not proven live: no device, no real GPT-Live session, no tenant run. The
  verified-behaviours row waits for that.
- The agenda duplicates the sources `app_home()` assembles inline in
  `dashboard.py` (the deck is not reusable from there); it reuses `deck.py`'s pure
  helpers so the ranking cannot drift, but a new deck fact will not appear in the
  walk by itself.
- Records to confirm/label (a deck topic) are left out — not voice material.
- "£" is chosen in one place (`debrief_walk._money`); currency is a per-business
  gap shared with the brief.
- In GPT-Live a spoken yes cannot confirm a card (its trust model); the note says
  so and the owner taps Confirm.
- The live walk reads the whole business, so it is the owner's and managers' only;
  a staff version would need the lenses of `people` applied to every source.

## Explicitly out of scope

- Per-tenant timezone (D2) — UK product; add `tenant_tz` when a non-UK tenant exists.
- Email delivery of the brief — the owner's inbox is the surface BLAM8 is
  *clearing*; adding to it is off-brand. Push + in-app only.
- Retro-fitting per-tenant times onto the other 13 daily jobs — this job is
  the pattern-setter; migrate others only with a reason.
