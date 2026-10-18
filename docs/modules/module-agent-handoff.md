# Multi-agent hand-off — base agent + industry specialists (SCOPE — LOCKED)

> **Status (2026-09-27, confirmed by the clean-up pass):** BUILT: Phase 1 (783568c), Phase 2a (fbdbfdf), the reverse direction via the outbound_reminder jobs, the field-service specialist. Live: field-service e2e June 2026; logistics side not recorded; runs since 2026-09-05 test the core only.

**Status:** LOCKED 2026-06-29 — hand-off model confirmed with Kris: the email
agent stays the single voice; it ASKS an industry specialist agent for answers
(which may place a provisional hold) and COMPOSES the reply itself. Origin:
2026-06-29 conversation after confirming the concierge is already a separate
agent — the draft agent is still one layered prompt (email + scheduling + TMS),
and each industry module should instead be its own agent with its own
instructions, context and tools.

## Goal
Split the single draft agent into a **generic base agent** (email + scheduling)
plus **one specialist agent per industry module** (Logistics → TMS first), with a
clean hand-off between them. A tenant on the pilot/Core has *only* the base agent;
when an industry module unlocks, its agent appears alongside. No industry's
instructions, context, or tools ever live in another agent's prompt.

Today this is one agent (`src/app/agent.py:build_system_prompt`) whose prompt grows
conditional `<scheduling>` / fleet / dual sections by active module. At runtime a
pilot tenant already gets none of the TMS text — so this is a **structural** change
(separation, scalability, hand-off), not a fix for an active leak.

## Why this shape
- **Separation by construction.** Each agent owns its system instructions, context
  injection and tool set in its own file — no conditional bleed, no risk of one
  module's rules colouring another. Adding a vertical = adding an agent, not
  fattening one prompt.
- **One voice, one reply.** A customer email must still produce a single reply in
  the tenant's voice. The base agent owns the customer relationship and the reply;
  specialists return **structured answers**, never prose. (Chosen over "specialist
  drafts its own paragraph" — that splits the voice.)
- **Reuse the proven capacity machinery.** The riskiest part — placing real holds
  against capacity — is unchanged; it just moves to live behind the specialist.

## What the platform already gives us (confirmed in code)
- **Tool-loop drafting** (`src/app/agent.py:draft_reply`): a draft call already runs a
  manual tool loop with an injected `tool_executor`. The hand-off is just one more
  tool on the base agent — no new call shape.
- **Module gating by data** (`src/app/pipeline.py:_scheduling_setup`, `db.active_modules`):
  tools/executors are already assembled per tenant from `subscriptions` rows. The
  specialist agent is gated the same way.
- **The capacity tools + executor** (`src/app/scheduling.py`: FLEET/ROUTING tools,
  `make_executor`, the atomic `hold_fleet_booking` RPC) — these become the **TMS
  agent's** tools, lifted out of the base agent's prompt.
- **The provisional-hold lifecycle** (the "temp booked until confirmed/denied/
  ignored" Kris described — already built): a hold counts against capacity
  immediately, is **confirmed** when the human approves & sends the draft,
  **released** when they skip it, and **auto-released** when stale (~7 days). Holds
  link to the `email_log` row in the pipeline. Reused unchanged.
- **Structured output**: the agent already emits validated JSON — the hand-off
  result is a structured object, the same mechanism.

## Decisions

### D1 — The split: base = email + scheduling; specialists = industry modules
The **base agent** is agnostic: email drafting + (when the scheduling module is
on) the office capacity diary. It is tailored only by the tenant's voice/knowledge.
**Industry agents** are separate, one per vertical — **TMS (Logistics) is the
first**. The base agent's prompt carries NO transport instructions and NO fleet
tools; all of that lives in the TMS agent.

### D2 — Hand-off model: ask → answer → compose (base owns the voice)
The base (email) agent ASKS the specialist for facts/decisions and COMPOSES the
reply itself. The specialist returns a **structured result, not prose**. The base
agent never sees vans/capacity internals it doesn't need; the specialist never
writes in the tenant's voice.

### D3 — The hand-off is a tool on the base agent's draft call
When (and only when) the tenant's industry module is active, the base agent's
draft call gains ONE hand-off tool — `ask_transport` (generic name per vertical,
e.g. `ask_<module>`). Its executor invokes the specialist agent (a nested agent
call) and returns the structured result into the base agent's tool loop. This
reuses `draft_reply`'s existing executor pattern; the base agent's loop stays the
`{reply, deferred, missing, scheduling_requested}` schema.

### D4 — The TMS agent: its own prompt + tools + context
The TMS agent is a separate module (`src/app/agents/tms.py` or similar) with: its own
system prompt (the transport instructions currently in `agent.py`'s fleet/dual
blocks, extracted verbatim to preserve behaviour), the existing fleet/capacity/
routing tools + `make_executor`, and its own context (vehicles, branches,
pallet-types, the transport diary). It does the transport-specific extraction
(quantities, pallet types, branch, postcodes) — the base agent does not.

### D5 — Holds stay exactly as they are, behind the specialist
The TMS agent places the provisional hold via the existing atomic RPC. The
pipeline links it to the draft's `email_log` row, and it resolves on the human's
action on that draft: **approve & send → confirmed; skip → released; ignored →
auto-released (~7 days)**. One hold per email. The base agent never touches
capacity. (This IS the "temp booked until confirmed/denied/ignored" — unchanged.)

### D6 — The hand-off contract
- **In** (base → specialist): the customer's ask (the relevant inbound text), the
  thread/contact id (so a follow-up in the same thread doesn't double-hold), the
  tenant id, and the receiving mailbox's default branch (enterprise).
- **Out** (specialist → base): a structured result, e.g.
  `{ status: "held" | "offered" | "no_fit" | "needs_info" | "error",
     date, window/branch, spaces, weight_kg, booking_ref, assumed (e.g. pallet
     type), alternatives: [dates], ask: "which branch?" }`.
  The base agent turns that into the reply (e.g. confirm the hold + ref, offer the
  nearest dates, or ask the one clarifying question).

### D7 — Graceful failure (honesty preserved)
A specialist error returns `{status:"error"}`; the base agent then says a colleague
will confirm and places no booking claim — mirroring today's tool-failure path.
The base agent must never invent capacity or a booking the specialist didn't
return.

### D8 — Dual case simplifies
Office bookings (people/appointments) stay with the **base** agent's scheduling;
only transport hands off to the **TMS** agent. This retires the tangled
single-prompt "dual diary" mode (`agent.py` DUAL_* blocks): instead of one prompt
routing via a `diary` arg, the base handles office and delegates transport. One
hold per diary still holds (base for office, specialist for transport).

### D9 — Gated by data, like everything else
The `ask_transport` tool is added to the base agent ONLY when the tenant's TMS
module is active (`db.active_modules`). Pilot/Core tenants: no hand-off tool, no
specialist — the base agent is pure email + scheduling, structurally.

### D10 — Direction: base → specialist first; bidirectional later
v1 is email-driven: inbound email → base agent → (maybe) ask the specialist. The
"vice versa" direction — a specialist initiating an outbound message the base
agent composes (e.g. a delivery-ETA notification) — is the same contract in
reverse and is **Phase 2**, built when an outbound trigger needs it.

## Flow (the worked example)
```
inbound "van space Friday, ~6 pallets?"
  └─ base (email) agent, tool loop
       └─ ask_transport("van space Friday, ~6 pallets", thread, branch)
            └─ TMS agent (own prompt + fleet tools)
                 ├─ check_availability  (real vehicle-day capacity)
                 ├─ hold_booking        (provisional hold, counts against capacity)
                 └─ returns {status:"held", date:Fri, spaces:6, booking_ref:"BK-1042"}
       └─ base agent composes: "Yes — pencilled in for Friday, ref BK-1042…"
  └─ draft queued; hold linked to it → confirmed on approve / released on skip
```

## Phased rollout
1. **Phase 1 — extract the TMS agent + base→TMS hand-off.** Lift the fleet/routing
   prompt + tools + executor out of `agent.py` into a TMS agent module; add the
   `ask_transport` tool (gated on the tms module); base agent's prompt loses the
   fleet sections. **Acceptance: a logistics tenant's drafts + holds are
   behaviourally identical to today** (same replies, same booking refs, same
   capacity enforcement) — verified against the existing fleet tests + a live E2E.
2. **Phase 2 — retire the dual-diary prompt** (base = office, hand-off = transport)
   and add the reverse direction (specialist-initiated outbound) if/when needed.
3. **Phase 3 (future) — more industry agents** as verticals ship (one agent +
   hand-off tool per vertical, driven by `INDUSTRY_MODULE`). Concierge ⇄ agents
   stays deferred (not needed now).

## Out of scope
- Concierge ⇄ agent hand-off (deferred — Kris: "not necessary right now").
- Changing hold/capacity semantics (reused as-is).
- New verticals beyond TMS in this spec (Logistics is the first specialist; the
  pattern generalises per `app/plans.INDUSTRY_MODULE`).

## Risks / trade-offs (named, accepted)
- **Latency + tokens:** a transport email becomes base call(s) + a nested TMS agent
  call. Accepted for the separation; non-transport email is unaffected (no hand-off
  tool fires).
- **Routing reliability:** the base agent must recognise a transport matter to hand
  off — handled by a clear tool description; the tool only exists when TMS is on, so
  the surface is small.
- **Behaviour parity is the bar for Phase 1** — the extraction must not change what
  customers receive; the existing fleet/dual tests are the guardrail.
