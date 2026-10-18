# Concierge cost controls: prompt caching + daily fair-use caps

Status: SPEC agreed 2026-08-02 (follows the cost probe in that day's session).
Built same day.

## The problem, quantified (30 days of prod llm_calls, probed 2026-08-02)

- A concierge business-chat exchange averages **~20k input / ~670 output tokens
  across 4.7 tool-loop rounds** on Sonnet 4.6 → **~7¢/exchange**, p95 ~10¢,
  worst observed single exchange ~70¢ (200k input on a long history).
- Total business-chat spend across ALL tenants in 30 days: **~$1.83** — tiny
  today, but a tenant chatting 20×/day would cost **~$42/mo ≈ £33**, wiping
  the £39 Starter margin.
- Input outweighs output ~30:1 because **every tool-loop round re-bills the
  full system + history + tool catalogue at full price** — `cache_control`
  appears NOWHERE in `app/` (the accounting fields in `usage.py` exist; no
  request ever sets a breakpoint).

Decision (Julius 2026-08-02): **no paywall on chat** — the concierge stays the
primary money surface and the trial's wow moment. Two instruments instead:

## Part A — prompt caching (the big lever, every LLM surface)

One transformation in `src/app/llm.py:create()` — the single choke point every
Anthropic call already flows through — stamps **one ephemeral cache breakpoint
on the last cacheable content block of the final message** of every request.

Why this placement (see the prompt-caching prefix-match model):

- A breakpoint at position N caches everything before it — `tools` → `system`
  → `messages` — so ONE tail breakpoint covers the whole prompt; no separate
  system/tools markers needed (max 4 allowed; we use 1).
- In a tool loop, round N+1's prompt = round N's prompt + assistant turn +
  tool results. Round N's tail entry is a prefix of round N+1 → the repeat
  bills at **0.1×** (read) and only the delta writes at 1.25×. The 5-minute
  TTL comfortably covers a tool loop, and each round adds ~2 content blocks —
  far inside the 20-block lookback window.
- Multi-turn chat gets the same benefit across turns arriving within the TTL.

Mechanics:

- String content on the final message → wrapped to
  `[{"type": "text", "text": …, "cache_control": {"type": "ephemeral"}}]`.
  List content → the last block gets the marker iff it's a plain dict of a
  cacheable type (`text`/`image`/`tool_use`/`tool_result`/`document`).
  Anything else (SDK objects, empty content) → request goes through
  unchanged. **Never mutates caller structures** — copies the affected
  message only (tool loops reuse their messages list across rounds).
- Kill-switch: `settings.llm_prompt_cache` (env `LLM_PROMPT_CACHE`, default
  true) returns kwargs untouched.
- Below-minimum prompts (Sonnet 4.6: 1024 tokens; Haiku 4.5: 4096) silently
  don't cache — marker ignored, no premium, no error.
- Metering already correct: `usage.record_anthropic` reads
  `cache_read_input_tokens`/`cache_creation_input_tokens` off the response and
  `usage.cost_usd` prices them at 0.10×/1.25× (`src/app/usage.py:33`). ⚠ Known
  reporting quirk: `llm_calls.input_tokens` logs the API's `input_tokens`,
  which EXCLUDES cached reads — post-caching, the /admin cost queries in
  docs/operations/llm-cost-tracking.md under-state total context (billed truth lives in
  usage_events, which has the cache columns).

Expected effect: rounds 2..N of every exchange re-read the prefix at 0.1× —
cutting the dominant input line by well over half on multi-round exchanges.
Applies to drafting, portal chat, planner, specialists — not just concierge.

## Part B — per-plan daily fair-use cap (business chat only)

The backstop for the pathological tail. **Always-on** (not behind
`require_subscription_gate` — this protects margins whether or not the trial
gate is up), generous enough that a normal user never sees it.

| Plan key | Display | Daily cap (env-tunable, 0 = unlimited) |
|---|---|---|
| `email` | Starter £39 | `CONCIERGE_DAILY_CAP_STARTER` = **10** |
| `starter` | Pro £99 | `CONCIERGE_DAILY_CAP_PRO` = **30** |
| `professional` | Core + OS £248 | `CONCIERGE_DAILY_CAP_CORE` = **60** |
| `enterprise` | Enterprise | unlimited |
| unknown / trial / comped | — | Pro-level (trial seeds Pro modules) |

Worst case post-caching: Starter ≈ 10 × 30 × ~2-3¢ ≈ **£5-7/mo** against £39.

Mechanics:

- Scope: `POST /concierge/business` only. Help/fix/onboard are rare setup
  surfaces; pre-login is already Haiku-pinned + per-IP rate-limited; the
  existing TRIAL total pot (30, gate-flagged) stays unchanged in front.
- Metering mirrors the trial RPC exactly: columns
  `tenants.concierge_daily_used` + `concierge_daily_date`, RPC
  `increment_concierge_daily(p_tenant_id, p_today)` — atomically resets the
  counter when the stored date ≠ `p_today` and returns the new total.
  Count-then-check: the route increments first, refuses when the return
  exceeds the cap (refused retries keep incrementing past the cap — harmless,
  still refused, resets at midnight).
- The day boundary is **Europe/London** (the app's diary timezone) — "back
  tomorrow" means UK tomorrow.
- Fail-open: an RPC failure logs and allows (a metering hiccup must never
  break an answer — the trial-cap contract).
- Refusal is the trial-cap NDJSON shape: one polite `done` message stating
  today's allowance and the UK-midnight reset; Starter additionally gets the
  Pro-raises-it upgrade line.
- Logic lives in `src/app/fairuse.py` (cap resolution + consume); the route
  gains ~6 lines after the trial block.

## Files

| File | Change |
|---|---|
| `src/app/llm.py` | `_cached_kwargs()` + call in `create()` |
| `src/app/config.py` | `llm_prompt_cache` + 3 cap settings |
| `src/app/fairuse.py` | NEW — `daily_cap()`, `consume()` |
| `src/app/db.py` | `increment_concierge_daily()` |
| `src/app/concierge_routes.py` | daily-cap block after the trial block |
| `db/schema.sql` + `scripts/db/migrations/apply_concierge_fairuse_migration.py` | 2 columns + RPC (applied to prod BEFORE merge) |
| `tests/test_llm_cache.py`, `tests/test_concierge_fairuse.py` | NEW |

## Explicitly out of scope

- Paywalling chat access (decided against — allowance differentiates, not access).
- Caps on help/fix/onboarding/portal surfaces (revisit only if data says so).
- Haiku routing for business chat (caching is the cheaper-first lever; model
  tiering would trade answer quality).
- Operator alert on per-tenant daily spend (the /admin margin view already
  shows it; alerting is a later nicety).
