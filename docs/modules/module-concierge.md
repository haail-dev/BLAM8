# Concierge — the onboarding co-pilot (SCOPE — DRAFT)

**About you capture (offline contract, 18 October 2026):** supplied goals and
business facts belong in their separate Step 1 fields, with material qualifiers
retained. The widget distinguishes unsubmitted edits from recorded held
suggestions. See [capture and review](../onboarding-interview-capture.md) for the
persistence boundary, pause behaviour, tests and live-retest limitation.

**Mobile panel height:** drag the blue Assistant header up or down below 768px.
The sheet starts at 60% height and remembers the chosen proportion for the
current tenant and browser tab. It clamps to 240px minimum (or the available
height on a smaller viewport), leaving space below the page header at maximum.
The grip is keyboard-accessible: Up/Down adjusts by 32px, Home/End use the limits.
Clear/Close remain separate buttons; the page scroll spacer follows the panel.
Offline browser interaction verification is maintained in the external release rig.

> **Status (2026-09-27, confirmed by the clean-up pass):** BUILT (see Build progress). Steps 1–5, fix mode, recommended settings and the settings hand-off proven live (07-26 / 08-02). Voice shipped in the owner's Concierge (09-23), not in the onboarding widget. Step 6 (gap log to /admin) not built.

**Status:** LOCKED 2026-06-29 — all three open decisions answered (voice deferred
to v1.1; conversion = model-offer + always-present button; platform KB = reserved
namespace, no new RAG code). Ready to build. Origin: 2026-06-29 conversation —
"a chatbot in the corner that actually
*does* stuff": answers pre-login questions, then visually drives a prospect
through the real onboarding instead of leaving them to scroll a form alone.

## Goal
The landing, sign-in and onboarding launchers (`/`, `/login` and `/onboarding/*`) use `static/assistant-glass-logo.js`: a
transparent glass B with mist, slow cog-driven water swells and rotating cog
cut-outs. Pink mist sheds from actual particles along the full slanted left
edge, drifting left with the longest reach at the centre and a shorter taper
at either end. Three left-moving speed lines remain. A separate random stream
preserves the interior motion; 12 glow and 12 dot paths batch the escaping mist
with one blurred layer. The outside-body clip keeps both cog openings fully
transparent. The upper cog is slightly right of the lower cog. The existing
64-pixel button still opens the same chat. Its white disc is removed; the glass
supplies a soft shadow. Animation loads when idle and pauses when the launcher
or document is hidden. Reduced motion, data saver and no JavaScript use
`static/img/blam8-assistant-glass.png`. The chat header and onboarding page header
also use that coloured glass still, without a monochrome filter. Other public launchers and the
live Concierge/Pond effects are unchanged. Route rendering is covered by
`tests/test_assistant_glass_logo.py`; live browser verification belongs in the
external QA workspace.

The Assistant's footer notices use the reusable `bc-notice` / `bc-notice-text`
presentation: a plain grey text line
with a slow pink-and-blue shine across the letters. There is no surrounding box.
Text remains readable throughout; there is no progress estimate. This includes
waiting, validation, pending-note and form-review notices. Their wording retains
the actual state; only an active operation has the `bc-busy` marker. Completing
the operation removes that marker and restores the current notice or Continue
action. Reduced motion and forced colours use static readable text.

A single corner widget that does two jobs across the public→tenant boundary:

1. **Pre-login concierge** — anyone on the landing/marketing pages can ask BLAM8
   questions ("what does it cost?", "do you do kennels?", "is my data safe?")
   and get grounded answers. It **stores nothing, writes nothing** — pure Q&A —
   until the visitor signals they're ready.
2. **Onboarding co-pilot** — once signed up, the same widget *drives the existing
   onboarding wizard*: the user types (or later, speaks) their answers and watches
   the real form fields fill and the real steps advance. At file-upload points it
   surfaces the real upload control. At the end it hands the user the filled
   onboarding to review, edit and confirm — nothing auto-commits.

The thesis: onboarding stops being a form you complete and becomes a conversation
that completes the form **for** you, on rails you can see.

## Why this shape (and the duplication trap it avoids)
The obvious-but-wrong build is a parallel chat that collects answers into its own
store and writes the tenant itself — two onboarding paths, two sources of truth,
double the maintenance. **We don't do that.** The concierge owns no onboarding
data. It calls a small set of **tools that are thin wrappers over the onboarding
POST routes that already exist** (`src/app/onboarding.py`: `/onboarding/industry`,
`/voice`, `/knowledge/*`, `/data`). The wizard remains canonical; the concierge is
an operator of it. One source of truth, one set of validation, one place a field
is defined — the chat is just a second steering wheel.

This is also the fifth outing of the platform pattern, repurposed: a feature =
tools for the shared agent + a surface, reusing the engine — here the "surface" is
public and the "tools" act on the onboarding wizard rather than a tenant mailbox.

## What the platform already gives us (confirmed in code)
- **The agent engine** (`src/app/agent.py`): structured output + tool loop on Haiku
  4.5, plus the fixed CORE honesty prompt (never invent facts, defer what it
  doesn't know). The concierge is a new *system prompt + toolset* on this engine,
  not a new engine.
- **Knowledge RAG** (`src/app/knowledge.py`, Voyage + pgvector): chunk → embed →
  retrieve into a `<knowledge>` block. The concierge grounds its answers the same
  way — see D3 for *whose* knowledge.
- **Knowledge-gap self-reporting** (Phase 3 loop): the agent already flags facts
  it couldn't answer. We reuse this verbatim for the concierge's "flag gaps to
  the operator" job (D9) — your self-improvement signal, already a shipped pattern.
- **The onboarding wizard** (`src/app/onboarding.py`): every step is already a
  structured POST writing structured config (`tenants.industry`, `tenant_config`
  voice fields, KB ingest, `data_sources`). These are the concierge's tool targets.
- **Draft-first trust ladder**: the whole product holds that a human approves
  before anything commits. The review-first finish (D8) inherits it directly.

## Decisions

### D1 — Co-pilot over the existing wizard, never a replacement
The concierge's authenticated-mode tools map 1:1 onto the existing onboarding
POSTs. It cannot write a field the wizard doesn't already own. The frontend
reflects each tool result into the real form (fill the input, advance the step)
so the user *sees* the wizard being driven. Single source of truth, zero
duplicate write path.

### D2 — Two modes, split by auth state
- **Pre-auth (transcript-stateless):** answers questions from the platform knowledge
  base (D3). No account or tenant is created; the transcript stays in browser
  session storage. Public LLM calls retain usage/reliability metadata under the
  platform sentinel, but not the prompt or response content. The prompt itself is
  sent to Anthropic to generate the answer.
  Ends only in a *conversion offer* (D5).
- **Authenticated (stateful):** drives onboarding via D1 tools against the
  signed-in tenant. Only reachable after signup creates a tenant + session.

### D3 — A **platform** knowledge base grounds the concierge (resolves the "whose knowledge?" question)
The concierge answers questions about *BLAM8 itself* (pricing, modules, security,
verticals) — that is **not** any tenant's knowledge. It needs a **platform-scoped
KB**: the same ingest/retrieval machinery, owned by a reserved platform identity
(e.g. a sentinel `tenant_id`, or a dedicated `platform_knowledge` namespace).
This is exactly where the real plan prices + company/contact details belong — and
it doubles as the profile your own BLAM8 sales inbox would answer from. **This is
the "agent knowledge" thread from earlier; it lives here, not on a customer.**
- **LOCKED:** reserved-namespace reuse of `src/app/knowledge.py` (a sentinel platform
  `tenant_id`) — no new RAG code, no new table. Seeded from the deck-derived facts
  + the live pricing table.

### D4 — Pre-auth → auth carry-over
The anonymous transcript is held in the **ephemeral browser session** and replayed
to the model on the next turn. The public assistant also returns a suggested
industry, which the widget currently stores in session storage; there is no
consumer that carries that value into onboarding today. Prompt and response text
are excluded from the public call's server-side LLM log; usage and reliability
metadata remain under the platform sentinel.

### D5 — Conversion is model-offered, user-confirmed (LOCKED)
When the conversation reads as "ready" (questions answered, intent expressed), the
concierge calls an `offer_signup` tool; the UI then reveals **buttons** ("Start my
setup" / "Keep asking") and, on accept, launches the existing signup screen
*behind* the panel (panel stays docked, signup loads in the page). A **persistent
"Get started" button is always available** too — the model never gate-keeps
signup. (LOCKED: offer + always-present button.)

### D6 — Voice (type *or* speak) — DEFERRED to v1.1 (LOCKED)
v1 is **type-only**. Speech-to-text is a real scope add (browser
`SpeechRecognition` is quick but flaky cross-browser; Whisper is robust but a new
paid call + audio plumbing), so it ships as a v1.1 toggle once the core flow is
proven. Type-first does not constrain the v1.1 add — the chat input is the same
surface either way.

### D7 — Visible fill + step navigation; uploads handed to the real control
As each authenticated answer lands, the frontend writes the value into the real
field and advances/scrolls the wizard so the user watches it happen (your "drag
them through it"). At the **knowledge** step the concierge does not ingest blindly
— it surfaces the existing upload affordance ("upload from device / drag-drop
here") and lets the real `/onboarding/knowledge/upload` handle the file.

### D8 — Review-first finish (inherits the draft-first ladder)
At the end the concierge presents the **fully filled onboarding** for the user to
read, edit, or accept — "here's what I captured, anything to change?" — then they
click continue. The concierge never silently completes onboarding.

### D9 — Gap logging → operator console (the self-improvement signal)
Every question the concierge couldn't answer from the platform KB, and every
free-text vertical a visitor names ("do you do dog grooming?"), is logged as an
aggregate gap to `/admin` (reusing the Phase-3 gap pattern; aggregates only, no
visitor PII). That's the backlog of what to add to the platform KB and which
verticals are being asked for.

### D10 — Public-surface hardening (non-negotiable for the pre-auth endpoint)
The pre-auth `/concierge/ask` endpoint is unauthenticated and calls a paid model —
i.e. a token-burn/abuse surface. It currently has an in-memory per-IP burst
limit (12/minute) and daily limit (150/day), a 512 KiB request-body cap before
JSON parsing, a 1,000-character question cap, at most 16 history turns with at
most 4,000 characters each, and bounded model output (800 tokens / 1,400 reply
characters). Visitors also see the public limits and receive 20 successfully
answered questions per browser session; warnings appear at five and two
questions remaining. An HttpOnly session cookie identifies the allowance, and
the count survives page navigation and clearing the transcript. Anonymous
session state expires after 48 hours without activity. The session allowance
and per-IP counters are held in worker memory, reset on process restart, and
are not shared across app instances; they do not provide a durable site-wide
daily spend ceiling. The per-IP limits are shared fair-use backstops, so people
on the same network share them. The model follows configured `anthropic_model`;
it is not pinned to Haiku. The endpoint returns a graceful fallback on model
errors and uses the same security headers/CSP as the rest of the app. Anonymous
support tickets have separate per-IP limits plus a per-email hourly cap and
duplicate folding; the visitor must review the draft and explicitly press Raise
ticket. Authenticated mode writes go through the existing CSRF-protected
onboarding POSTs unchanged.

## Out of scope (v1, named deliberately)
- Voice/STT (D6) — v1.1.
- A general "do anything in the app" agent — the concierge only answers questions
  and drives onboarding. Post-onboarding dashboard actions are a later concierge.
- Multi-language — English v1.
- Anonymous server-side persistence — pre-auth is ephemeral by design.

## Build progress
- **Steps 1–4 — DONE** (commit `3714d8e`, live-verified on blam8.haail.dev): platform
  facts + sentinel KB, hardened `/concierge/ask`, the widget on all three bases, the
  offer→"Start my setup" CTA + industry carry-over.
- **Step 5 — DONE** (this change): authenticated `/concierge/onboard` + the onboarding
  co-pilot. On the industry and voice steps the concierge fills the visible fields from
  the conversation (mirrored into the real form, highlighted) and nudges the real
  Continue button; on mailbox/knowledge/data it guides verbally. Writes nothing — the
  page's own CSRF-protected POST stays the only write path. `_sanitize_fill` constrains
  output to the wizard's own fields. Pending: live E2E as a signed-in tenant.
- **Agnostic-instructions refactor — DONE** (post-step-5): the concierge's system
  instructions are now product-agnostic (behaviour only, no product name/prices). The
  BLAM8 facts are injected as DATA from `src/app/platform_facts.py`, with pricing
  single-sourced from the plan catalog `src/app/plans.py` (which `/billing/plans` also
  renders from) — so the assistant can never quote a price that differs from billing.
  A drift-guard test fails if they diverge.
- **Step 6** (review-first finish polish + gap logging to `/admin`) and the platform-KB
  seed run remain.
- **Fix mode — DONE** (2026-07-02): thumbs-down on a draft opens the assistant scoped to
  THAT draft (`blam8ConciergeFix` → `/concierge/fix` with the draft + current never-rules +
  intake labels injected as data). It diagnoses in chat, then proposes ONE action
  (`{kind: never|knowledge|requirement|redraft, content}`) rendered as an Apply card —
  nothing changes until pressed. `/concierge/fix/apply` (tenant + CSRF header + rate limit)
  executes it — never rule via the shared `_append_never_rule`, knowledge via `kb.ingest`
  under an "Assistant fix · <id>" source, requirement appended (deduped) to intake_fields —
  then ALWAYS re-drafts via `pipeline.redraft_email_log` and returns the fresh draft, which
  the widget swaps into the page's textarea. Replaces the inline "Something's wrong" panel
  and the legacy `/app/drafts?fix=1` detour. Metered as `concierge_fix`.
- **Feature requests & issue reports through the chat — DONE** (2026-07-02): help mode
  can now DRAFT a feature request or an issue report from the conversation
  (`submit: {kind: feature|bug, summary}` in the `/concierge/help` structured output).
  The widget renders the draft as a card with a Submit button — nothing is stored until
  the customer approves — then POSTs `/concierge/submit` (require_tenant + rate limit +
  CSRF double-submit via `X-CSRF-Token` header), which files into the SAME
  `feature_requests` pipeline as the `/app/requests` form, tagged `kind` +
  `submitted_by='assistant chat'` + category `Feature idea`/`Issue report`. The tenant
  page and `/admin/requests` show the kind; `feature_requests.kind` added via
  `scripts/db/migrations/apply_request_kind_migration.py`. A `requests` nav destination and a
  platform-KB source ("Feature requests and reporting a problem") let the assistant
  point people at the page — reseed the KB after deploy to pick the source up.
- **Recommended settings ("Help me set this up" → "Use recommended settings") — DONE**
  (2026-07-27, Julius): help mode can hand back READY-MADE settings for one page
  (`recommend: {page, values:[{name,value}]}` in the `/concierge/help` structured
  output). `src/app/settings_recommend.py` is the contract: the per-page field catalogue
  injected into the help prompt (model may only use real form-field names; repeatable
  rows — service/resource/field/stage — use one pipe-format entry each) and the
  server-side whitelist `clean()`. The widget renders a card listing the values with a
  "Use recommended settings" button. Two apply shapes:
  * `diary_settings` + `booking_details` — stash-and-navigate: values go to
    sessionStorage, the destination page's REAL form is pre-filled on arrival
    (`applySettingsPrefill` in concierge.js — checkbox groups, selects, template-cloned
    rows), a review banner appears, and NOTHING is saved until the form's own Save.
  * `crm_settings` — the page is per-row forms with no end-confirm, so the click IS the
    confirm: `POST /concierge/crm-apply` (tenant + rate limit + CSRF header) adds only
    MISSING stages (case-insensitive) + sets the gone-quiet threshold, then links to the
    page to review/rename/remove.
  The prompt orders the model to ask questions first when it doesn't know enough, and
  the customer profile now carries the current booking checklist + CRM stages/threshold
  so recommendations start from what exists. "Help me set this up" buttons live on Diary
  settings, Booking details and CRM settings. Tests: tests/test_settings_recommend.py.

- **Post-onboarding guided-settings hand-off — DONE** (2026-08-02, Julius; reworked same
  day to ZERO USER INITIATIVE after the watched e2e): a freshly onboarded tenant lands on
  `/app` and the widget CONTINUES onboarding unprompted — it auto-selects the App-help
  tab, auto-opens the panel, and announces the NEXT step as a message with a nav link
  (appendNavLink) to that settings page: diary → CRM → invoicing, one step at a time,
  announced once per step per session (the greet stamp is the step key, so completing a
  step announces the next on the following load). A persistent chip strip in the footer
  slot (`#bc-cta`) keeps the walk-through visible with per-step ✓s + an explicit ×; every
  affordance NAVIGATES to the page, where the existing flows take over ("Help me set this
  up" → "Use recommended settings" → pre-fill for review; invoice details are facts only
  the owner knows). `src/app/setup_handoff.py` (pure) decides WHEN (fresh
  `tenants.onboarded_at` ≤ 14 days, not dismissed, something left) and WHAT (module-gated
  steps with live done flags: scheduling_config row exists / CRM made-their-own / invoice
  business name or bank account filled). `GET /concierge/setup-status` serves it
  (degrade-not-500 → `show:false`); dismiss = `POST /concierge/setup-dismiss` →
  `tenant_config.settings_handoff_dismissed_at` (cross-device; migration
  `scripts/db/migrations/apply_settings_handoff_migration.py`, applied to prod first). E2E-found traps
  now encoded: `tenant_config.crm_quiet_days` is NOT NULL DEFAULT 14 on every row, so
  only a NON-default value proves configuration; and the lazily-seeded default CRM stages
  made the help assistant congratulate "already set up" — `_tenant_context` now flags
  `default_seed` and the help prompt treats untouched defaults as still-to-do. A 30-min
  sessionStorage snooze keeps the steady-state cost at ~one status read per tab per half
  hour. Tests: tests/test_setup_handoff.py.

## Build steps (proposed)
1. **Platform KB** (D3): namespace decision, seed with deck facts + live pricing +
   company/contact; verify retrieval. (This is the "feed real prices" task, landed
   in its correct home.)
2. **`/concierge/ask`** stateless endpoint: Haiku + platform-KB grounding + CORE
   honesty, with D10 hardening. Pre-auth Q&A working end to end.
3. **Corner widget**: dockable panel on landing/signup/onboarding pages; transcript
   in ephemeral session; "Get started" always present.
4. **Conversion** (D5): `offer_signup` tool + button reveal + signup launch behind
   the panel; D4 carry-over into the new tenant.
5. **Onboarding tools** (D1/D7): thin wrappers over the onboarding POSTs; frontend
   reflects each into the real field + advances the step; upload handoff.
6. **Review-first finish** (D8) + **gap logging** (D9) to `/admin`.
7. Tests (rate-limit + cap behaviour, tool→endpoint mapping, carry-over) + a live
   E2E: anonymous question → convert → talk through onboarding → review → live.
