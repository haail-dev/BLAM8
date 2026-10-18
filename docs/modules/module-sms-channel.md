# BLAM8 SMS Channel + Missed-Call Text-Back — Design & Build Spec

> **Status (2026-09-27, confirmed by the clean-up pass):** BUILT and merged (3455940), prod migration applied 2026-07-10 (src/app/sms.py, src/app/sms_webhook.py, src/app/channel_routes.py; tests/test_sms_channel.py). Locked — no Twilio credentials, shows 'Coming soon'. Live proof: none.

**Status:** 🔨 Not built. Spec only. First non-email channel; the pilot of the multi-channel architecture that WhatsApp (CRM Phase 5.2) later rides on.
**Author:** Julius Deane, w/ Claude synthesis
**Last updated:** 2026-07-05
**Companion docs:** `docs/architecture/CRM_design.md` §23 (the WhatsApp+SMS phase this implements the SMS half of — with one deliberate revision, see D1), `docs/os/practice.md` §6 (the reminder/recall engine that becomes SMS-routable), `docs/modules/module-scheduling.md` (decision-block format), `docs/architecture/enterprise-multi-mailbox.md` (the per-connection config pattern `channel_connections` mirrors).

> **Read this first.** This is *not* a new messaging product. BLAM8 already has everything a text channel needs except the transport: a provider-neutral inbound pipeline (`pipeline.draft_for_email` takes a plain dict — from, subject, body, thread_id), a proactive composer (`agent.compose_outbound`), a self-filling CRM (`crm_extract`), an approval inbox with live SSE updates (`events.publish`), webhook signature-verification patterns (Graph/Gmail/Stripe), encrypted credential storage (`src/app/crypto.py`), per-tenant usage metering (`src/app/usage.py`), and a forward-dated job skeleton (`src/app/jobs/send_reminders.py`). The SMS channel is a Twilio webhook that *normalises a text into the shape the pipeline already eats*, plus a send dispatch that routes approved drafts out via Twilio instead of the mailbox. Missed-call text-back is one more webhook on the same number. Everything genuinely new is called out in the **§12 reuse map**; if a section doesn't say "NEW", assume it's reuse.

---

## §0 Positioning

**Why SMS first (before WhatsApp).** Three compounding reasons:

1. **No gatekeeper.** WhatsApp needs Meta business verification (2–3 weeks), pre-approved message templates, and a 24-hour session window. SMS needs a Twilio account. We ship the week we start, and we kick off Meta verification *in parallel* so WhatsApp is un-gated when we want it (§14).
2. **Universal reach, zero adoption risk.** Every customer phone has SMS. No install-base question (Telegram's problem), no "is the customer on it" question. For *transactional* touches — appointment reminders, "your order's ready", "driver's 20 minutes away" — UK customers already expect SMS.
3. **Practice OS needs it at launch.** Dentists and salons send SMS reminders *today*; `docs/os/practice.md` §14 flags channel readiness as a launch-industry gate. A patient replying **C** to confirm is a free, strong not-a-no-show signal for the scorer. SMS resolves that gate without waiting on Meta.

**The missed-call wedge (trades).** A tradesperson on a roof misses a call; that's a lost job. With text-back: the call rings out → BLAM8 texts the caller back within seconds → the caller replies with the job details → the drafter answers in the tenant's voice → the CRM fills itself in. The demo sells itself, and it converts the phone channel's biggest pain into BLAM8's strongest surface (the drafting inbox) with **zero real-time voice AI**. It is a small extension of the SMS channel — one voice webhook — not a voice product.

**What this module is NOT.** Not call answering, not call transcription (CRM Phase 5.3, separate), not voicemail-to-text (v1.5 candidate, §13), not WhatsApp (rides in later on the same `channel_connections` spine), not marketing blasts (PECR, §11).

**Build state.** Not built. NEW code: the Twilio webhook pair (inbound SMS + missed call), `src/app/sms.py` (send + normalise), `channel_connections` table, a `channel` column on `email_log`, channel-aware send dispatch in the inbox, SMS style hints in the composer prompts, phone-keyed contact identity, opt-out handling, and channel routing in the reminder/recall jobs. Everything else is reuse.

---

## §1 The channel primitive — an SMS thread IS an `email_log` conversation

**Inbound normalisation.** A text message becomes the same provider-neutral dict the mailbox produces, and enters the *same* pipeline:

```python
email = {
    "from_address": "+447700900123",          # E.164 — the address IS the phone number
    "subject":      "",                        # SMS has no subject; prompt handles empty
    "body":         payload["Body"],
    "thread_id":    f"sms:{e164}",             # one rolling thread per (tenant, number)
}
log_id = await pipeline.draft_for_email(tenant_id, email,
             source_message_id=payload["MessageSid"],   # idempotency, reuses the
             auto_send_ok=True, mailbox_id=None)         # existing unique index
```

Everything downstream — conversation history (`db.get_conversation` keys on `from_address` + `thread_id`), CRM extraction, knowledge retrieval, the approval inbox, SSE — works unchanged because none of it is email-specific.

### D1 — one message log, not parallel tables (revises CRM_design §23)

CRM_design §23 proposed parallel `whatsapp_messages`/`sms_messages` tables "to not destabilise `email_log`". That was written before Phase 4/5 landed. Today, **five surfaces key off `email_log`**: the approval inbox + SSE, conversation threading (Phase 4/5), CRM extraction + activity linkage (`email_log_id`), usage metering (`usage_events.email_log_id`), and the draft-quality stamps (`sent_via`/`edited`). A parallel table forks all five.

**Decision:** add one additive column — `email_log.channel text NOT NULL DEFAULT 'email'` (`'email' | 'sms'`, later `'whatsapp'`) — and route on it at the send step only. Nothing existing changes behaviour (default `'email'`); the "don't destabilise" concern is met *better* by not forking the read paths. WhatsApp later adds an enum value, not a table. *Rejected alternative:* parallel tables + a UNION view for the activity feed — more code, three copies of the idempotency/threading/stamp columns, and the inbox would need a second query path.

**Contact identity (NEW — the one real gap).** `crm_contacts` is keyed by email; an SMS-only customer has none. v1: add `crm_contacts.phone` (E.164, unique per tenant where not null) and a `db.crm_ensure_contact_by_phone(tenant_id, e164)` twin of `crm_ensure_contact`. Extraction runs as normal and can fill the email field later ("what's your email for the invoice?" → extracted → stamped), at which point the contact simply has both identities. Cross-channel merge tooling (same human, email thread + SMS thread) is the existing dedupe/merge roadmap item (`docs/crm-competitive-analysis.md` #8) — explicitly **not** built here; the phone column just makes SMS contacts first-class rows.

---

## §2 Provider + number model

**Provider: Twilio.** Best docs, one account serves SMS + the voice webhook missed-call needs, and it's the §23-named SMS provider anyway. The Twilio dependency is isolated inside `src/app/sms.py` (one send function, one signature validator) so a swap is a one-file change. *Rejected:* Vonage (no reason to differ from §23), Messaging-Service-per-tenant (Twilio's bundled STOP handling is nice but per-tenant services complicate provisioning; we handle STOP ourselves in ~20 lines, §11).

**One UK number per tenant, platform-provisioned.** On channel activation we buy an SMS+voice-capable UK number via the Twilio API and store it on the tenant's `channel_connections` row. ⚠️ Note at build time: in the UK, **two-way SMS needs a mobile (07) number** — UK local/geographic numbers are voice-only. Budget ≈ £5–7/mo per number + ≈ 3–4p per outbound segment, ≈ 1p inbound (verify current Twilio UK rates at build; the §23 figures are stale).

### D2 — replyable long number, not an alphanumeric sender ID

Alphanumeric ("BLAM8"/tenant name) looks branded but **cannot receive replies** — which kills confirm-by-reply, kills the conversational drafter, and kills text-back. The whole point of this module is two-way. **Decision:** every tenant message goes out from their long number; the first outbound in any thread introduces itself ("Hi, it's Sarah at Border Dental…" — the composer does this naturally from `agent_name`/`sign_off` config). Alphanumeric sender as a later option for pure-broadcast tenants only.

**Credentials.** Platform-level Twilio account SID/auth token live in env (`settings.twilio_*`), like the Stripe/Graph secrets. Per-tenant rows hold only the phone number + provider SIDs — no per-tenant secrets, so nothing new to encrypt (`src/app/crypto.py` stays for mailbox tokens). If a tenant ever brings their own Twilio account, `channel_connections.config` jsonb holds an encrypted token using the existing `crypto.encrypt` pattern.

---

## §3 Inbound wiring — the SMS webhook

`POST /webhooks/twilio/sms` (NEW, in a new `src/app/sms_webhook.py`, mounted like `src/app/webhook.py`):

1. **Rate limit** — `Depends(rate_limit("twilio_webhook", 120, 60))`, same as Graph/Gmail.
2. **Verify** — `twilio.request_validator.RequestValidator(auth_token).validate(url, params, X-Twilio-Signature)`. Same posture as `stripe.Webhook.construct_event` / the Graph `clientState` compare: reject unverified with 403.
3. **Route to tenant** — look up `channel_connections` by the `To` number (E.164-normalised). Unknown number → 200 + log (Twilio retries on non-2xx; we never want retry storms).
4. **Idempotency** — `source_message_id = MessageSid`; the existing `email_log_tenant_source_msg_uniq` index makes replays no-ops.
5. **Opt-out gate (NEW, §11)** — `STOP/START/HELP` keywords are handled *before* the pipeline: stamp/clear `sms_opt_outs`, send the static compliance reply, return. No LLM involved.
6. **Contact + pipeline** — `crm_ensure_contact_by_phone`, then the `draft_for_email` call from §1. `auto_send_ok=True` like the mail webhooks; the tenant's existing `auto_send` config gates actual auto-sending, so SMS inherits the tenant's trust ladder with zero new config.
7. **Respond** — empty TwiML `<Response/>` (we reply asynchronously through the approval flow, not inline).

**Threading.** SMS has no native threads; the conversation is the number. `thread_id = "sms:{e164}"` gives Phase 4/5 history replay one rolling conversation per customer — which is exactly how humans experience SMS.

---

## §4 Outbound — send dispatch + channel style

**Send dispatch (NEW, small).** The inbox approve/send route currently calls `mailbox.send_reply(...)` then `db.mark_email_log_sent(log_id, via=...)`. It becomes a two-way dispatch on `email_log.channel`:

```python
if row["channel"] == "sms":
    await sms.send(tenant_id, to=row["from_address"], body=final_text)   # NEW src/app/sms.py
else:
    await mailbox.send_reply(...)                                         # unchanged
db.mark_email_log_sent(log_id, tenant_id, via=via, edited=edited)         # unchanged
events.publish(tenant_id, {"type": "status_changed", ...})                # unchanged
```

`sms.send` checks `sms_opt_outs` (hard gate — an approved draft to an opted-out number is *dropped with a visible skip status*, never sent), calls the Twilio Messages API, records the message SID, and meters the segment cost (§8). Delivery-status callbacks (delivered/failed → surface on the log row) are v1.5 — nice, not blocking.

**Approval surface: the existing live inbox, unchanged.** SMS drafts appear in `/app/conversations` as pending rows like any email draft — approve/edit/skip, SSE updates, `sent_via`/`edited` stamps all free. A small channel chip (✉/💬) on the row is the only UI change. This is deliberate: WhatsApp will need the same in-app approval home (WhatsApp has no drafts folder), so the inbox becoming "the place drafts live regardless of channel" is the strategic move, not a shortcut.

### D3 — channel style lives in the prompt, config stays jsonb

SMS drafts must be *short* (target ≤ 2 segments ≈ 300 chars, GSM-7), no greeting ceremony, no sign-off block, no links unless asked, sign with the agent/business name once. **Decision:** a NEW `CHANNEL_STYLE_HINTS = {"sms": "..."}` block injected into `build_system_prompt` / `build_outbound_prompt` when the message's channel is SMS, plus one additive `tenant_config.channel_config jsonb DEFAULT '{}'` for per-tenant overrides (`{"sms": {"max_segments": 2, "sign_as": "Sarah @ Border Dental"}}`). *Rejected:* dedicated `sms_tone`/`sms_max_chars` columns — WhatsApp/Telegram would each add more columns; one jsonb keyed by channel scales sideways. The base voice (`tone`, `do_list`, `never_do`, examples, KB) applies unchanged — same drafter, same tenant voice, different register.

---

## §5 Missed-call text-back

**Tenant setup (the clever bit — no porting, no new number for customers).** The tenant keeps their real number and dials one GSM code on their business mobile: `*61*<their BLAM8 number>#` (conditional divert: forward *only unanswered* calls; `*62*`/`*67*` cover unreachable/busy). Landline/VoIP tenants set the same divert-on-no-answer in their phone settings. Nothing about their public identity changes; BLAM8's number only ever sees the calls they missed. The settings page shows the exact dial string with their provisioned number filled in, plus a **[Test it →]** step (call your own number, decline it, watch the text arrive).

**Call flow.** `POST /webhooks/twilio/voice` (same file, same signature verification, routed by `To` number):

1. Respond with TwiML: `<Say>` a one-line message in the tenant's name — *"Sorry we missed you — we're sending you a text right now."* — then `<Hangup/>`. (Voicemail via `<Record>` is v1.5; it also produces the audio that Phase 5.3 transcription would later eat.)
2. On the call-status callback, fire the text-back through `agent.compose_outbound(cfg, purpose="missed-call text-back", facts=...)` → `insert_email_log(channel='sms', kind='missed_call', status per auto_send)`. Under `auto_send` this is fully hands-off (the right default here — a text-back is only valuable in the first minutes); draft-only tenants see it pending at the top of the inbox.
3. The caller's reply comes back through the §3 webhook and the conversation continues under the normal drafting flow. CRM fills itself in.

**Guards (all NEW, all small):** skip withheld/anonymous caller IDs; skip opted-out numbers; **dedupe one text-back per caller per 24h** (`kind='missed_call'` lookup on `email_log` — repeated calls shouldn't spam); skip callers with an already-open SMS thread from today (they're mid-conversation, not a new missed call).

### D4 — text-back only in v1; no ring-through, no voicemail

*Rejected for v1:* ring-through to a second phone (that's a phone system — out of scope), `<Record>` voicemail (v1.5, adds media storage + playback UI), and any AI voice interaction (explicitly the separate future voice module — this spec's whole thesis is capturing calls **without** real-time voice AI).

---

## §6 Reminders, recall + confirm-by-reply (the Practice OS hook)

**Channel routing in the jobs (NEW, thin).** `send_reminders.py` / `send_recall.py` / `send_service_reminders.py` keep their skeletons (`run() -> dict`, `tenants_with_modules`, per-stage dedupe via `reminder_stages_sent`). One addition at the compose/deliver step: resolve the contact's **preferred channel** — the "Preferred contact" CRM field the industry seeds already define (`industry_lingo.py`), normalised to `email | sms` — and if SMS: compose with the SMS style hints, insert with `channel='sms'`, deliver via `sms.send` under the same `auto_send` gate. Contacts with no phone or no preference fall back to email. No new job, no new dedupe mechanism.

**Confirm-by-reply.** SMS reminders end with *"Reply C to confirm or R to rearrange."* The §3 webhook gets a tiny pre-pipeline intercept (like STOP): a bare `C`/`YES` on a thread with a pending near-term booking flips the booking's confirmed state directly — no LLM, instant, and it feeds the no-show scorer exactly the signal `docs/os/practice.md` §12 wants. Anything else ("can we do 3pm instead?") flows to the drafter, which already has the scheduling tools to rearrange. A tokenised confirm *link* (`secrets.token_urlsafe`, `/confirm/{token}`, the `/track` pattern) is redundant for SMS v1 — reply-C is fewer taps than a link — but the practice.md §11 link can ride the same confirm plumbing later for email.

**PECR boundary (§11 details):** reminders/recall for an existing patient relationship are service messages — fine. The line to not cross in v1: no promotional content in SMS.

---

## §7 Schema — all additive

```sql
-- The channel spine (CRM_design §23's message_channels, final name):
CREATE TABLE IF NOT EXISTS channel_connections (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id     UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  channel_type  TEXT NOT NULL,                -- 'sms' (later 'whatsapp')
  phone_number  TEXT,                         -- E.164, the tenant's provisioned number
  provider      TEXT NOT NULL DEFAULT 'twilio',
  provider_sids JSONB DEFAULT '{}'::jsonb,    -- number SID etc.
  config        JSONB DEFAULT '{}'::jsonb,    -- text_back_enabled, forwarded_from, BYO creds (encrypted)
  is_active     BOOLEAN NOT NULL DEFAULT TRUE,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX channel_conn_number_uniq ON channel_connections (phone_number)
  WHERE phone_number IS NOT NULL;             -- webhook routes To → tenant

ALTER TABLE email_log      ADD COLUMN IF NOT EXISTS channel TEXT NOT NULL DEFAULT 'email';  -- D1
ALTER TABLE tenant_config  ADD COLUMN IF NOT EXISTS channel_config JSONB DEFAULT '{}'::jsonb; -- D3
ALTER TABLE crm_contacts   ADD COLUMN IF NOT EXISTS phone TEXT;                             -- E.164, §1
CREATE UNIQUE INDEX crm_contacts_tenant_phone_uniq ON crm_contacts (tenant_id, phone)
  WHERE phone IS NOT NULL;

CREATE TABLE IF NOT EXISTS sms_opt_outs (      -- §11; keyed by number not contact (pre-contact STOPs)
  tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  phone       TEXT NOT NULL,                   -- E.164
  opted_out_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, phone)
);
```

`email_log.kind` gains values `'sms'`-adjacent jobs already use (`reminder`, `recall`) plus NEW `'missed_call'`. No changes to bookings, CRM tables beyond `phone`, or any existing index.

---

## §8 Module activation, pricing + metering

**Module id `sms`** (working name in the hub: **"Text messaging"**, with missed-call text-back as its headline feature — one module, one price; text-back is not separately gated). Standard wiring: a `MODULES` entry in `src/app/dashboard.py` (moving it out of the "soon" group), a `subscriptions` row, gated via `db.active_modules`, route gate on the settings page. Plan mapping (`plans.py modules_for`) and the per-module line-item price are **Julius's call** per the pricing-menu discussion; the spec's working assumption for margin math: **£29–49/mo add-on including the number rental + a fair-use segment allowance (~300/mo), metered pass-through beyond** — at 3–4p/segment a reminder-heavy practice sending 1,000 texts/mo costs ~£35–40 in raw Twilio, so a flat low price without an allowance is underwater. Number provisioning happens on activation; deactivation releases the number after a grace period.

**Metering.** LLM costs flow through `usage.record_anthropic` unchanged (operations: `draft`, `reminder`, `recall`, NEW `missed_call`). NEW: per-segment provider cost recorded to `usage_events` with `provider='twilio', operation='sms_send', cost_usd=...` (computed from segment count — the shape §9 of usage.py already stores; `email_log_id` links it to the message). The usage page then shows SMS spend per tenant for free, which is exactly what allowance/pass-through billing needs.

---

## §9 Settings + onboarding surface

**Settings page `/app/channels` (NEW, gated on `sms`):** activate → number provisioned + shown; missed-call section with the `*61*` dial string + test step (§5); text-back on/off; opt-out list (view + manual add); channel style overrides (D3). This is a settings form, not an onboarding wizard step — channels are post-onboarding upsells.

**Onboarding touchpoints (tiny):** a `DATA_SOURCES` entry (`{"kind": "sms", "label": "Text messages", "hint": "Do customers text or call the business number?"}`) so the interview captures channel demand as an upgrade signal, mirroring how `scheduling_gap` works for the diary.

---

## §10 Compliance (UK)

- **STOP/START/HELP** — mandatory. Keyword intercept in the webhook (§3), `sms_opt_outs` table, hard gate in `sms.send`. Static replies, no LLM. HELP returns the tenant name + "reply STOP to opt out".
- **PECR** — service/transactional messages to existing customers (replies, reminders, recall, text-back to someone who just called *you*) are fine; **marketing SMS needs prior consent** — out of scope for v1, and `never_do` guidance in the SMS style hints tells the drafter not to upsell in texts.
- **Sender identity** — first message in a thread self-identifies with the business name (D2); UK has no US-style A2P 10DLC registration for long codes, but verify Twilio's UK regulatory bundle requirements when buying numbers.
- **Missed-call announcement** — the `<Say>` line means callers aren't silently harvested; no recording in v1 so no recording-notification obligations (that lands with Phase 5.3, whose "two-party consent" note should be corrected to the ICO notification standard when that spec is written).
- **Data retention** — SMS bodies live in `email_log`, so `purge_email_log.py` retention already applies.

---

## §11 Failure modes + edge cases

- **Twilio webhook retries** on non-2xx → always 200 after signature check; idempotency via `MessageSid` makes replays safe.
- **Segment blowout** — drafter told ≤ 2 segments; `sms.send` hard-truncates never — instead it *warns* in the inbox row if a human-edited draft exceeds 3 segments (cost surprise ≠ silent truncation).
- **Number spoofing / junk inbound** — signature verification kills forged webhooks; unknown `To` numbers logged + dropped.
- **Text-back loops** — two BLAM8 tenants calling each other, or a caller whose voicemail-divert points back: the 24h text-back dedupe + skip-withheld guards cover the realistic cases; a global "never text a number on our own platform's provisioned list" check closes the loop case cheaply.
- **The tenant's own mobile texting the BLAM8 number** — route by `From` match against `channel_connections.config.forwarded_from`; treat as internal, don't draft (mirrors the self-email guard in the mail pipeline, if present — verify at build).
- **Local dev** — Twilio can't reach localhost; use the Twilio CLI's webhook tunnel or replay captured payloads in tests (all webhook logic is testable in-process via TestClient like `tests/test_home_modules.py` does — no live Twilio in CI).

---

## §12 Reuse map

| Component | Status | What |
|---|---|---|
| Inbound draft pipeline | **REUSE** | `pipeline.draft_for_email` — takes the normalised dict verbatim |
| Drafter + voice + KB + CRM context | **REUSE** | `agent.draft_reply`, `build_system_prompt`, `kb.retrieve`, `crm_intel` |
| Proactive composer | **REUSE** | `agent.compose_outbound` (reminders, recall, text-back) |
| CRM extraction + activity | **REUSE** | `crm_extract.extract` + `apply_updates`, `email_log_id` linkage |
| Approval inbox + SSE | **REUSE** | `/app/conversations`, `events.publish`, `sent_via`/`edited` stamps |
| Idempotency + threading | **REUSE** | `source_message_id` unique index, `thread_id` conversation history |
| Job skeleton + dedupe | **REUSE** | `send_reminders/recall/service_reminders.py`, `reminder_stages_sent` |
| Webhook auth pattern | **REUSE** | rate_limit + constant-time verify (Graph/Gmail/Stripe precedent) |
| Usage metering | **REUSE** | `usage.record_anthropic` + `usage_events` (new provider row for Twilio) |
| Module gating + billing | **REUSE** | `MODULES`, `subscriptions`, `db.active_modules`, `plans.modules_for` |
| `src/app/sms.py` (send, normalise, segments) + `src/app/sms_webhook.py` (SMS + voice hooks) | **NEW** | ~the whole channel transport |
| `channel_connections`, `email_log.channel`, `crm_contacts.phone`, `sms_opt_outs`, `channel_config` | **NEW** | §7 schema |
| Send dispatch on channel | **NEW** | ~10 lines in the inbox send route |
| Channel style hints | **NEW** | prompt block + jsonb overrides (D3) |
| Phone-keyed contact identity | **NEW** | `crm_ensure_contact_by_phone` |
| Missed-call flow + guards | **NEW** | §5 |
| Confirm-by-reply intercept | **NEW** | §6 (feeds the existing no-show scorer) |
| Channel settings page | **NEW** | §9 |

---

## §13 Build phases

- **Phase S0 — Channel transport (3–4 days).** `channel_connections` + migration, number provisioning, `src/app/sms.py`, the SMS webhook (verify → normalise → pipeline), `email_log.channel`, send dispatch, inbox channel chip, opt-out handling. **Shippable value:** a tenant's customers can text the BLAM8 number and get drafted-in-voice replies through the normal approval inbox. *Demoable end-to-end.*
- **Phase S1 — Missed-call text-back (1–2 days).** Voice webhook + TwiML, compose the text-back, the four guards, settings section with the dial string + test step. **Shippable value:** the trades demo.
- **Phase S2 — Reminder/recall routing + confirm-by-reply (2–3 days).** `crm_contacts.phone` + preferred-channel resolution in the three jobs, SMS style hints, the C-to-confirm intercept feeding booking state. **Shippable value:** the Practice OS channel gate opens — SMS reminders + confirmations live.
- **Phase S3 — Money + polish (1–2 days).** Twilio cost metering into `usage_events`, allowance surfacing on the usage page, `MODULES`/plans wiring, docs + capabilities-page honesty update (SMS moves from "roadmap" to live; WhatsApp/Telegram chips stay "soon").

**Total: ~1.5–2 weeks.** S0 alone is demoable; S0+S1 is the sales demo; S2 is what Practice launches on.

**Success criteria.** (1) A trades tenant misses a real call and wins the job entirely over drafted SMS — contact, deal and promise autofilled to the email-baseline standard. (2) A practice tenant's reminder goes out by SMS to a patient whose preferred contact is SMS, the patient replies C, and the booking shows confirmed with no human touch. (3) Twilio spend per tenant is visible on the usage page within £0.10 of the Twilio console.

---

## §14 Open questions + deliberate deferrals

- **Pricing** — final per-module price + allowance size is Julius's call (§8 has the margin math). Decide before S3, not before S0.
- **Meta verification** — start the WhatsApp Business/Meta verification **now, in parallel** (costs a form; 2–3 weeks elapsed). When WhatsApp builds, it reuses `channel_connections` (`channel_type='whatsapp'`), the channel column, the style-hints block, the send dispatch, and the inbox — i.e. this module *is* most of CRM Phase 5.2's build.
- **Voicemail + transcription** — `<Record>` on the missed-call TwiML is the natural v1.5; it also feeds CRM Phase 5.3 (call transcription) audio for free. Deferred to keep S1 tiny.
- **Delivery-status callbacks** — v1.5; failed-delivery surfacing matters once reminder volume is real.
- **Alphanumeric sender / broadcast** — only if a pure-notification tenant asks; two-way is the product.
- **Cross-channel contact merge** — rides the existing CRM dedupe/merge roadmap item, not this module.
- **Rate verification at build** — Twilio UK mobile-number monthly cost, per-segment rates, and UK regulatory-bundle requirements all drift; re-quote before writing the pricing page.
