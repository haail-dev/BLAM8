# Phase 3 — Knowledge-base self-improvement loop (SPEC)

> **Status (2026-09-27, confirmed by the clean-up pass):** BUILT. Feature 1 grew into the knowledge hold (765b379) — seen live on the rig. Feature 2 (flag a wrong fact → fix its note) routes exist in src/app/dashboard.py, not proven live.

**Status:** spec locked 2026-06-09, **not yet built.** Decisions captured below.

## Goal
Turn the human approval step into a flywheel. Every reviewed draft either reveals a
knowledge **gap** (the agent deferred a fact to a colleague) or an **error** (the agent
cited a wrong fact) — and both feed back into the knowledge base so the agent improves
over time. No per-tenant code; pure KB-curation UX. Fits the "tenants self-serve via
data" philosophy ([[product-philosophy-no-custom-builds]]).

Two features. Build **Feature 1 first**, then Feature 2; each ships + deploys independently.

## Locked decisions
- **F1 gap detection:** structured output on the draft call (one call; captures the *missing topic*).
- **F2 correction panel:** light inline JS (the "little chat box" feel; current CSP allows inline script).
- **F2 capture:** paste-the-quote first (highlight-text-to-flag is a later polish).
- **F2 KB edit granularity:** chunk-level (edit the specific matched `knowledge_chunks` row, re-embed).
- **Re-draft after correction:** included (regenerate the current draft from the fixed KB).

---

## Feature 1 — "Colleague" deferral flag (fills gaps)

When the agent defers a specific fact to a colleague, that's a knowledge gap. Capture it
+ the missing topic so the tenant knows exactly what to add.

### 1.1 Drafting → structured output (`src/app/agent.py`)
Switch `draft_reply` from plain text to a constrained JSON response:
```json
{ "type": "object",
  "properties": {
    "reply":    { "type": "string"  },
    "deferred": { "type": "boolean" },
    "missing":  { "type": "string"  }
  },
  "required": ["reply", "deferred", "missing"],
  "additionalProperties": false }
```
- `client.messages.create(..., output_config={"format": {"type": "json_schema", "schema": <above>}})`.
- Append to the system prompt (after the platform rules): *"If you deferred any specific fact
  (price, date, availability, policy) to a colleague because it was not in the knowledge
  provided, set `deferred` true and `missing` to a short description of what was missing
  (e.g. 'delivery time for perishable goods'); otherwise `deferred` false, `missing` empty."*
- Parse the JSON text block; `DraftResult` gains `deferred: bool`, `missing: str`.
- Notes: Haiku 4.5 supports structured outputs; schema compiles once (24h cache); incompatible
  with citations/prefill (we use neither). The "write only the reply body" rule now applies to `reply`.

### 1.2 Data
```sql
alter table email_log add column if not exists knowledge_gap text;  -- deferred topic; null = none
```
`db.insert_email_log(..., knowledge_gap=None)`; add `knowledge_gap` to `list_email_log` + `get_email_log_entry` selects.

### 1.3 Pipeline (`src/app/pipeline.py`)
After drafting, `knowledge_gap = result.missing if result.deferred else None` → pass to `insert_email_log`.
(Escalation-keyword prefix logic unchanged — separate mechanism.)

### 1.4 Surfaces
- `templates/app/drafts.html`: amber **⚠ Knowledge gap** badge on rows where `e.knowledge_gap`.
- `templates/app/draft_review.html`: when set, a banner — *"The agent didn't have this info and
  deferred to a colleague — missing: {{ e.knowledge_gap }}"* + an **"Add to Knowledge"** button
  linking to `/app/knowledge?prefill=<topic>` (paste-note form pre-filled with the topic).
- *(optional later)* `/app/gaps` — every `email_log` row with a non-null `knowledge_gap`, each
  linking to its draft + an add-to-KB shortcut.

### 1.5 Code touch (F1)
`agent.py` · `pipeline.py` · `db.py` · `db/schema.sql` · `dashboard.py` (drafts/review contexts; optional `/app/gaps`; `prefill` in knowledge GET) · `templates/app/drafts.html`, `draft_review.html`.

---

## Feature 2 — Flag-incorrect → trace-to-source → fix (corrects errors)

### 2.1 UX (inline JS panel on the review page)
Third action next to **Approve & send** / **Skip**: **"Something's wrong"**, toggling a hidden panel:
1. textarea — *"What was incorrect? Paste the wrong text from the draft."*
2. **Find source** → `fetch POST /app/drafts/{id}/flag {text}` → JSON matches.
3. Render each match `{source, content, similarity%}` with:
   - an editable textarea (chunk content) + **Update knowledge** → `POST /app/knowledge/chunk/{chunk_id} {content}` (re-embed + save) → toast.
   - **This one's actually correct** → dismiss.
4. **None of these — add it** → textarea + label → existing `POST /app/knowledge/text` (grounds a hallucination as new knowledge).
5. After any KB change: **Re-draft with the correction** → `POST /app/drafts/{id}/redraft` → re-runs the pipeline draft for this email against the updated KB → JS replaces the draft textarea.
- Small inline `<script>`; endpoints return JSON; CSRF via the existing double-submit token (read the hidden field, send it in the request).

### 2.2 Endpoints (`src/app/dashboard.py`)
- `POST /app/drafts/{id}/flag` — in `{text}`; out `{matches:[{chunk_id, source, content, similarity}]}`. Voyage-embed → `knowledge.search_chunks`. Best-effort; `[]` if KB off.
- `POST /app/knowledge/chunk/{chunk_id}` — in `{content}`; re-embed + update that chunk (tenant-scoped — closes IDOR).
- `POST /app/drafts/{id}/redraft` — re-draft this `email_log` entry vs current KB; `update_email_log_draft`; re-evaluate `knowledge_gap`; out `{draft}`.
- add-new reuses existing `POST /app/knowledge/text`.
All gated by `require_onboarded` + CSRF; tenant-scoped.

### 2.3 `src/app/knowledge.py` / `src/app/db.py`
- `knowledge.search_chunks(tenant_id, text, k=5) -> [{chunk_id, source, content, similarity}]` (embed_query + `match_knowledge_chunks`, which already returns id/content/source/similarity).
- `knowledge.update_chunk(tenant_id, chunk_id, content, client=None)` — re-embed (single doc) → `db.update_knowledge_chunk`.
- `db.get_knowledge_chunk(chunk_id, tenant_id, client)`; `db.update_knowledge_chunk(chunk_id, content, embedding, tenant_id, client)` (update where id+tenant match).

### 2.4 Edge cases
- **No match** → add-new branch (hallucination grounding).
- **KB correct but draft misquoted** → "actually correct" → tenant hand-edits the draft textarea, sends.
- **Paraphrase** (draft reworded the fact) → semantic match handles it; tenant confirms.
- **Multi-chunk source** → edits the matched chunk only.
- **Voyage off** → flag/search disabled with an honest message; manual draft edit still works.

### 2.5 Code touch (F2)
`knowledge.py` · `db.py` · `dashboard.py` (3 routes) · `templates/app/draft_review.html` (panel + JS) · reuse `knowledge/text`.

---

## Migrations (run in Supabase at build time)
- F1: `alter table email_log add column if not exists knowledge_gap text;`
- F2: none (reuses `knowledge_chunks`).

## Out of scope (later)
Auto-applying corrections without review; gap/error analytics; highlight-to-flag selection;
per-chunk version history/audit; bulk gap triage.
