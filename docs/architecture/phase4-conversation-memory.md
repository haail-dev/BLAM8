# Phase 4 — Per-thread conversation memory (SPEC)

> **Status (2026-09-27, confirmed by the clean-up pass):** BUILT and merged 2026-06-09 (9e7393b; agent.strip_quoted / thread_messages, pipeline history=). Proven live 2026-06-10 on both providers. Guard: tests/test_conversation_memory.py.

**Status:** spec locked 2026-06-09, **not yet built.** Implement in a fresh context from
this doc. Origin: design discussion about how thread context reaches the model.

## Goal
Give the drafting agent **true, tight, multi-turn memory** of each conversation, assembled
from **our own stored data** — replacing today's stateless single-turn call that leans on
whatever the customer's email client happened to quote. Wins:
- The agent reliably remembers the back-and-forth **including its own previously-sent replies**, even when a client strips the quoted chain.
- Context is **de-duped and cheap** — one clean copy of each message, not a growing re-quoted tail.
- Robust, consistent behaviour per contact (the "individualized chatbot" model).

## Current state (as-is — confirmed in code, do not re-derive)
- Drafting is **one stateless model call** in `app/agent.draft_reply`:
  `messages=[{"role":"user","content":"Draft a reply to this incoming email.\n\nFrom: …\nSubject: …\n\n<body>"}]`,
  `system=build_system_prompt(cfg, knowledge)`, `output_config={"format":{"type":"json_schema","schema":DRAFT_SCHEMA}}` (F1 structured output → `{reply, deferred, missing}`).
- **No prior turns are sent.** The only "history" the model sees is the quoted chain inside `<body>` (the `On <date> X wrote:` tail the sender's client included). If a client strips quotes, the agent is blind to the exchange — and it never sees its *own* prior replies except via the customer quoting them back.
- `app/pipeline.draft_for_email(tenant_id, email, source_message_id=None)` builds `email={from_address, subject, body}` from `mailbox.get_message` (plain-text body via `Prefer: outlook.body-content-type="text"`, **includes quotes**), runs the ignore check, retrieves knowledge, escalation-checks, calls `draft_reply`, then `db.insert_email_log(...)`.
- `pipeline.redraft_email_log(tenant_id, log_id, client=None)` does the same single-turn draft from the stored row's `body_in`.
- A "thread" exists **only in the dashboard**: `db.get_conversation(tenant_id, from_address, client=None)` returns that sender's `email_log` rows `oldest→newest`, selecting `id, from_address, subject, body_in, draft_reply, status, knowledge_gap, created_at`. It feeds the conversation VIEW, **not the model**.
- `email_log` row = `from_address, subject, body_in (full, incl. quotes), draft_reply, status ('pending'|'sent'|'skipped'), source_message_id, knowledge_gap, created_at`. Nothing strips/de-dups quotes anywhere. No prompt caching is used.

## Locked decisions
1. **Build context from our `email_log` rows**, grouped per `(tenant_id, from_address)` (same grouping as the conversation view) — NOT from the email's quoted chain.
2. **Strip quotes for the prompt only; store originals unchanged.** Heuristic `strip_quoted()` runs at prompt-build time; `body_in` stays full for audit/fidelity. **No schema change, no migration.**
3. **Turn mapping** for each prior row (oldest→newest): `user` = `strip_quoted(body_in)`; `assistant` = `draft_reply` **only if `status == 'sent'`** (so the model never believes it replied when it didn't), with any leading `[⚠ ESCALATION …]` marker stripped. Final `user` turn = the current inbound (stripped).
4. **Multi-turn `messages` array** passed to the **existing structured-output draft call** (schema unchanged — only the model's *new* output is constrained). `build_system_prompt` reframed to explain the thread.
5. **History scope:** all prior rows for the sender. `draft_for_email` (current inbound not yet stored) → all existing rows. `redraft_email_log` → rows where `id != log_id` (exclude the row being regenerated). Cap to the most recent `THREAD_CONTEXT_MAX_ROWS` rows.
6. **Knowledge retrieval unchanged** — query is still the current email (subject + body).
7. **Conservative stripping** — if stripping empties a body, fall back to the full body. Never drop genuinely-new content.

## 4.1 — Quote stripping (`src/app/agent.py`)
`strip_quoted(text: str) -> str`: cut the body at the **first** reply-delimiter and keep the
text before it. Heuristic + conservative. Cut at the earliest multiline match of:
- `On <date>, <who> wrote:`  (Apple/Gmail) — the line ending `wrote:` plus the nearest
  line above it starting `On`, up to three lines with no blank line between: Gmail's
  plain-text part wraps the attribution at ~78 columns, so a business name and its
  address push `wrote:` onto the next line (live 2026-09-20, the kennels film). When a
  `>`-quoted run follows, the customer's own words typed *under* that run (a bottom-posted
  or interleaved reply) are kept; a flattened HTML quote (no `>`) is cut whole.
- `(?im)^\s*-{2,}\s*original message\s*-{2,}`  (Outlook)
- `(?m)^\s*_{5,}\s*$`  (Outlook underscore rule)
- `(?im)^\s*From:\s.+$` **when** the next ~3 lines contain `Sent:`/`Date:`/`To:`/`Subject:`  (Outlook header block)
- a trailing run of `^\s*>` quoted lines

The `[Attachments: …]` marker the mail readers append (after the quote, so it used to
defeat the trailing-quote cut) is split off first and re-appended after: a reply that is
only a file comes back as just the marker, never the whole chain.

Trim trailing whitespace. **If the result is empty or < ~2 non-space chars, return the original**
(the marker alone when the message carried a file).
*(Optional, conservative)* drop a trailing `Sent from my <device>` line. Do **not** attempt
general signature stripping (too risky).

`strip_escalation(text: str) -> str`: remove a leading `[⚠ ESCALATION …]` line/block
(format produced in `pipeline`), else return text unchanged.

## 4.2 — Thread → messages (`src/app/agent.py`)
`thread_messages(history_rows: list[dict], current_body: str) -> list[dict]`:
```
out = []
for row in history_rows[-THREAD_CONTEXT_MAX_ROWS:]:        # oldest→newest
    u = strip_quoted(row.get("body_in") or "")
    if u: out.append({"role": "user", "content": u})
    if row.get("status") == "sent" and row.get("draft_reply"):
        a = strip_escalation(row["draft_reply"]).strip()
        if a: out.append({"role": "assistant", "content": a})
cur = strip_quoted(current_body) or current_body
out.append({"role": "user", "content": cur})
while out and out[0]["role"] == "assistant":   # API: first turn must be user
    out.pop(0)
return out
```
Consecutive `user` turns (a prior inbound with no sent reply) are fine — the API merges them.

## 4.3 — Draft call (`src/app/agent.py`)
`draft_reply(cfg, email, knowledge=None, history=None) -> DraftResult`:
- `msgs = thread_messages(history or [], email.get("body", ""))`
- `client().messages.create(model=settings.anthropic_model, max_tokens=1024, system=build_system_prompt(cfg, knowledge=knowledge), messages=msgs, output_config={"format":{"type":"json_schema","schema":DRAFT_SCHEMA}})`
- parse with the existing `_parse_draft`; return `DraftResult(...)`.
- Remove the old single `"Draft a reply to this incoming email.…"` wrapper string — framing moves to the system prompt. New-sender (empty history) ⇒ `msgs == [{"role":"user","content": <current body>}]` (same shape as today, minus the wrapper text).

## 4.4 — System prompt (`app/agent.build_system_prompt`)
Append to the **CORE** block (after the numbered platform rules, before `<business_voice>`):
> "The messages that follow are an ongoing email conversation with one customer: `user` turns are the customer; `assistant` turns are replies your business has already sent. Draft the next reply to the customer's most recent message."

Keep platform rule 4 (write only the reply body in `reply`) and rule 5 (the F1 deferral
report). Voice / rules / examples / `<knowledge>` sections unchanged.

## 4.5 — Pipeline (`src/app/pipeline.py`)
- `draft_for_email`: after the ignore check, `history = db.get_conversation(tenant_id, email.get("from_address"))` (service role — webhook/manual path). Pass `history=history` to `draft_reply`. If `from_address` is falsy → `history = []`.
- `redraft_email_log`: `history = [r for r in db.get_conversation(tenant_id, entry["from_address"], client=client) if r["id"] != log_id]`. Pass `history=history`.
- `get_conversation` already returns the needed fields oldest→newest — **no new query, no new columns.**

## 4.6 — Caps & cost
- `THREAD_CONTEXT_MAX_ROWS = 20` (≈ up to 40 turns), keep newest. Haiku 4.5 has a 200K window, so this is a **cost guardrail, not a window limit**.
- Optional secondary char budget (~60K chars) — not required for the pilot.

## No schema change · no migration.

## Edge cases
- New sender → empty history → single user turn. ✓
- Customer replied with only a quote (no new text) → strip empties → fall back to full body. ✓
- Prior drafts skipped/pending → no assistant turn (only `sent`). ✓
- Human edited before sending → `send_draft` persists the edited `draft_reply` *before* sending, so the assistant turn is the actual sent text. ✓
- Escalation-marked sent reply → `strip_escalation` removes the prefix from the assistant turn. ✓
- Array would start with an assistant turn → leading assistant turns dropped (first must be user). ✓
- Structured output + multi-turn → only the model's new output is schema-constrained; prior assistant turns are plain text context. ✓

## Out of scope (later)
- Prompt caching of a stable prefix (the `<knowledge>` block varies per email, so a byte-stable cache prefix is awkward — revisit if cost matters).
- Showing stripped/clean text in the conversation VIEW (display nicety; `body_in` stays full).
- Cross-address identity (same person across multiple emails).
- Long-thread summarisation / compaction (only needed far beyond pilot volume).

## Code touch
`src/app/agent.py` — `strip_quoted`, `strip_escalation`, `thread_messages`, `THREAD_CONTEXT_MAX_ROWS`, `draft_reply` (history param + multi-turn), `build_system_prompt` (framing line).
`src/app/pipeline.py` — `draft_for_email` + `redraft_email_log` fetch & pass history.
Reuse `db.get_conversation`. **No templates, no DB, no migration.**

## Verification
- **Unit:** `strip_quoted` on Gmail/Apple (`On … wrote:`), Outlook (`-----Original Message-----`, `From:/Sent:` block), `>`-quoted, and quote-only bodies; `thread_messages` for alternation, sent-only assistant turns, redraft-excludes-current, leading-assistant drop, empty-strip fallback.
- **Live:** a multi-message thread where the customer's latest email does **not** re-quote earlier context → the new draft still reflects it; redraft preserves context; brand-new sender → still drafts (single turn). Confirm token usage per draft is lower than the equivalent quoted-chain call.
