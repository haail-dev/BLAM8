# Smart booking: offered-slot holds ("the offer is the hold")

> **Status (2026-09-27, confirmed by the clean-up pass):** BUILT, all 3 phases (40cfa35, e19572f, b461a35); live e2e passed 2026-07-31; listed in verified-behaviours. The Phase 3 nudge is not yet seen firing live.

Status: SPEC — agreed with Julius 2026-07-31. Not built.

## The gap

When the agent merely *offers* times ("we're free at 10, 11, 12 or 1"), nothing is
persisted. Consequences today:

- Two concurrent enquiries are offered the same slots; the second customer to
  reply is told "actually that one's gone" (the atomic RPC saves the diary, not
  the conversation).
- The diary shows nothing for offered slots — the business can't see that
  options are dangling in a customer's inbox.
- There is no "customer picked 11:00 → release 10, 12, 13" mechanic, because
  the siblings were never held.

The hard-hold system (`bookings.status='proposed'`, atomic
`hold_booking_if_capacity`, email linkage, approve/skip resolution, 7-day
expiry, diary rendering) already exists end-to-end. This module adds the
missing *soft* state in front of it. Extend, don't rebuild.

## Decisions (Julius, 2026-07-31)

| # | Decision | Choice |
|---|----------|--------|
| D1 | Soft-hold TTL | **Tenant-configurable, default 24h** (`scheduling_config.offer_ttl_hours`). Always capped at `slot_start − min_notice_hours`. The deadline is **stated in the customer's email** — it's a promise, not vibes. |
| D2 | Offer overlap | **Exclusive.** A slot offered to customer A is never offered to customer B until it releases. Offered slots consume capacity in availability maths. No overbook factor (leave room in the design, no schema commitment). |
| D3 | Pre-expiry nudge | **Phase 3, tenant-configurable toggle.** "Still want Tuesday 10:00? It's released at 5pm today." Reuses the quote-chase pattern; obeys the auto-send ladder. |
| D4 | Vague asks | **Clarify first, honestly** (revised 07-31): no holds, no slot list — reply asks for at least a day and whether morning, midday or afternoon suits, in one message. Once a day (+ optional band) comes back, offer up to **3 slots within that range**, soft-held, TTL stated. Exception: if the asked-about range has ≤3 free slots total, skip the clarify round-trip and offer them directly (scarcity honesty). |

## The three ask types

The agent classifies what the customer's email actually asked:

1. **Named time, free** → hard-hold that one slot (today's behaviour,
   unchanged). Reply reads as a booking; confirmed on approve/auto-send;
   pushed to calendar. No TTL — the customer asked, we booked.
2. **Named time, full** → offer exactly **2 alternatives**: nearest free slot
   *before* and nearest free slot *after* the requested time, closest first.
   **Same-day-adjacent beats strictly-nearest** — if the nearest-before is
   three days earlier but the same day has a later slot, prefer the same day.
   Soft-hold both; TTL line in the reply.
3. **Vague ask** ("sometime next week") → **counts first** (upgraded
   2026-08-02, was clarify-first): no holds. The agent calls
   check_availability across the whole period; a multi-day range's result
   carries **`day_summary`** — `[{date, day, free}]`, one entry per day with
   availability, `free` = how many bookable windows/start times that day
   still has (derived in the executor from the same slots list; no schema
   change, so the tool list stays byte-stable for the prompt cache). The
   reply gives the per-day picture ("Tuesday has 4 slots free, Wednesday 5,
   Thursday 4"), quoting `free` counts exactly (say-vs-do: never a count the
   tool didn't return), and asks for a day at minimum, and whether morning,
   midday or afternoon is best — all in one message. **Counts are
   informational and reserve nothing** — offer_slots stays reserved for
   concrete times. When the customer
   replies with a day (+ optional time-of-day band), search that range and
   offer up to **3 slots**, soft-held, TTL stated. If they give a day but no
   band, spread the 3 offers across morning/midday/afternoon (which answers
   the band question implicitly). If the band is full, fall back to
   nearest-2 outside it. Exception: if the whole asked-about range has ≤3
   free slots, skip clarifying and offer them directly.
   - **Bands** are thirds of the tenant's open hours (a 7:00–16:00 trade gets
     morning 7–10, midday 10–13, afternoon 13–16), so "morning" adapts to
     each business rather than assuming 9–5.
   - Slot selection prefers **diary packing**: slots adjacent to existing
     bookings over ones that fragment a day into unusable gaps (respect
     `buffer_minutes` / `travel_mode`).

Scarcity honesty: if literally one slot fits the ask, the agent may say "this
is the only slot left this week" — computed, never vibed.

## Data model: `offered` as a third status on `bookings`

Chosen over a separate `slot_offers` table because every piece of machinery we
need already keys off `bookings`: `email_log_id`/`thread_id` linkage
(`link_bookings_to_log`), resolution (`resolve_holds_for_log`), the atomic
capacity RPC, the diary view-model, and the expiry sweep.

- `bookings.status` gains `'offered'`; new column `expires_at timestamptz`
  (null for proposed/confirmed).
- `ACTIVE_STATUSES` becomes `("offered", "proposed", "confirmed")` — this one
  change makes offers consume capacity everywhere (slotgrid, fleet, engineers,
  stays, portal, concierge) because all availability flows through
  `list_active_bookings`.
- `hold_booking_if_capacity` (+ fleet/engineer variants) accept
  `p_status='offered'` and an optional `p_expires_at`. Same advisory lock —
  offers are race-safe against each other and against manual diary adds.
- One inbound email → up to 3 offered rows, all linked via `email_log_id`.
- **Audit every `ACTIVE_STATUSES` / `list_bookings` / `diary_days` consumer**
  and the diary template's binary proposed-vs-confirmed branch. Calendar push
  stays `confirmed`-only. `bookings_for_contact` correctly injects outstanding
  offers into the next draft for free.

## Lifecycle

| Event | Behaviour |
|---|---|
| Draft offers slots | Agent tool `offer_slots` places N offered rows atomically (all-or-nothing per slot; a slot that fails capacity is swapped for the next candidate). Linked to the draft's `email_log_id`. |
| Draft skipped / re-drafted | Release this draft's offered rows first (existing `resolve_holds_for_log` path, extended to offered). |
| Customer picks one | Winning row flips `offered → proposed` (it already holds the capacity — no re-check needed), siblings flip to `cancelled` **in the same transaction**. Then the normal proposed→confirmed flow on approve/send. |
| Customer declines all | All released. |
| TTL expires | Sweep (extend `purge_email_log.py` job) releases expired offered rows. Runs on `expires_at`, not the 7-day proposed rule. |
| Late reply after expiry | Agent re-checks availability and gracefully re-offers; never honours a dead hold. |
| Duplicate webhook | Existing same-run release logic covers offered rows too. |

## Customer-facing copy

- Every offer message states the deadline: "we'll hold these for you until
  **Thursday 5pm** — just reply with which suits."
- Deadline formatting uses the tenant's timezone and reads naturally
  (day + time, not ISO).
- EU AI Act disclosure wrapper unaffected (all auto-sends already wrapped).

## Diary UI (Phase 2)

- Offered rows render **grey** with an "Offered" pill + expiry countdown
  ("releases in 6h"), visually distinct from amber Proposed and emerald
  Confirmed.
- **Clickable → the exact conversation/draft** holding the slot. Use the
  stored `email_log_id`/`thread_id` deep-link — and fix the existing
  `view conversation` link at the same time (today it links by contact
  address, not the specific message, despite the columns existing).
- Confirmed rows stay clickable too: customer, conversation, summary.
- Occupancy bars / window pills show offered as a distinct segment so a day
  full of dangling offers doesn't read as booked solid.
- Manual release: an operator can release an offered row from the diary
  (same pattern as Confirm/Decline on proposed).

## Config surface

- `scheduling_config.offer_ttl_hours int default 24` — exposed in Diary
  settings with the recommended-default treatment.
- Offer count for vague asks: constant 3 (not a setting yet).
- Phase 3: `offer_expiry_nudge bool` tenant toggle.

## Phases

1. **Phase 1 — the offered state.** Migration (`status='offered'`,
   `expires_at`, RPC changes), `ACTIVE_STATUSES`, `offer_slots` tool +
   agent-prompt rules for the three ask types (nearest-2, vague-3,
   same-day-adjacent, diary packing), TTL copy in drafts, acceptance
   flip + sibling release, expiry sweep, re-offer on late reply. Tests.
2. **Phase 2 — the visible diary.** Grey Offered pills + countdown, distinct
   occupancy segment, exact-conversation deep-links (incl. fixing the
   contact-based link), manual release, confirmed-row summary popover.
3. **Phase 3 — the nudge.** Pre-expiry chase (configurable toggle), quote-chase
   pattern, auto-send ladder respected.

## Prod notes

- Migration must be applied to prod (psycopg2 direct, per standing rule)
  **before** deploy — `ACTIVE_STATUSES` reading a status the DB check
  constraint rejects would break hold placement.
- Live e2e after Phase 1: vague enquiry → 3 grey holds → pick one → siblings
  release → confirm → calendar push.
