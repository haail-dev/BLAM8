"""Environment-backed settings.

Secrets live in Render → Environment, never in code (spec §10). Locally they
come from a .env file (see .env.example).
"""
import logging
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Enable after 20261018_knowledge_source_review.sql is installed.
    knowledge_source_review_enabled: bool = False

    # Install db/migrations/20261020_device_sync.sql before enabling.
    device_sync_enabled: bool = False

    # Offline telephone pilot only. No real telephone transport is enabled here.
    telephone_pilot_enabled: bool = False
    telephone_pilot_provider: Literal["simulated"] = "simulated"
    telephone_pilot_webhook_secret: str = Field(default="", repr=False)

    # Separate opt-in for a future explicitly authorised carrier trial.
    # Empty deployment facts fail readiness; credentials reuse existing integrations.
    telephone_live_enabled: bool = False
    telephone_webhook_base_url: str = ""
    telephone_realtime_model: str = ""
    telephone_openai_project_id: str = ""
    telephone_openai_webhook_secret: str = Field(default="", repr=False)

    # Microsoft Graph
    azure_client_id: str = ""
    azure_client_secret: str = ""
    azure_redirect_uri: str = ""
    azure_tenant_id: str = "common"

    # Google (Gmail) — second mailbox provider
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = ""           # e.g. https://blam8.haail.dev/oauth/google/callback
    gmail_poll_seconds: int = 120           # Gmail auto-draft poll cadence; 0 disables the poller
    # Gmail push (users.watch → Pub/Sub → POST /webhook/gmail). Both empty = the
    # feature is off and Gmail rides the poller alone. The topic is the full
    # Pub/Sub name (projects/<project>/topics/<name>) created in the SAME Google
    # Cloud project as the OAuth app; the token is a shared secret carried in the
    # push subscription's endpoint URL (?token=…), verified constant-time — the
    # Gmail mirror of graph_webhook_secret. The poller stays on as the safety net.
    gmail_pubsub_topic: str = ""
    gmail_pubsub_token: str = ""
    calendar_sync_seconds: int = 300        # Calendar Connect busy-sync cadence; 0 disables
    subscription_renew_seconds: int = 21600  # Graph subscription renewal cadence (6h); 0 disables
    # In-process daily maintenance (day-before reminders + retention purge) — runs
    # inside the web service so no separate Render cron services are billed/rebuilt.
    # The jobs are idempotent (dedupe/redact), so a double-run is harmless. False to
    # disable (e.g. if you run them as an external cron instead).
    # Unset = ON only on Render (security review 2026-09, M29: a laptop running
    # the app used to start every customer-facing job — Gmail drafting, reminders,
    # chases — against production, racing Render). Covers the Gmail poller,
    # calendar sync, Graph renewer and the Concierge task resume too; see
    # `background_jobs_on`. RUN_DAILY_JOBS=true forces them on anywhere.
    run_daily_jobs: bool | None = None

    # Google sign-in slots. While the Google OAuth app is in 'testing' mode it
    # caps Gmail-mailbox grants (the restricted gmail.* scopes) at 100 distinct
    # users and warns "Google hasn't verified this app" — sign-in only uses basic
    # scopes and is uncapped, so the connect surfaces show honest availability
    # before a tenant clicks Gmail (src/app/slots.py). Flip GOOGLE_OAUTH_VERIFIED=true
    # the moment verification clears to hide the indicator (no cap once verified)
    # — no redeploy.
    google_oauth_verified: bool = False
    # Gmail-connect invite gate (src/app/slots.py) — protects the scarce unverified-app
    # grants at the step that actually burns them: /oauth/connect?provider=google.
    # GOOGLE_INVITE_GATE=true arms it (single-use codes are minted in /admin/invites);
    # GOOGLE_INVITE_CODES optionally adds reusable comma-separated "channel" codes
    # (also arms the gate). Lifts automatically once GOOGLE_OAUTH_VERIFIED=true.
    # Sign-in is never gated (either provider); neither is connecting Microsoft.
    google_invite_gate: bool = False
    google_invite_codes: str = ""
    # System notifications (e.g. "someone requested Google access"): sent FROM the
    # company tenant's connected mailbox named here (empty = no emails; requests
    # still queue in /admin/invites), TO the notify address below.
    platform_notify_mailbox: str = ""            # e.g. admin.blam8@gmail.com once connected
    access_request_notify_to: str = "hello@blam8.com"

    # Web Push (PWA notifications, src/app/push.py). Base64url-raw pair from
    # scripts/ops/generate_vapid_keys.py — the SAME pair everywhere (subscriptions
    # bind to the public key; rotating orphans every subscribed device). Empty
    # keys = push quietly off: the client banner never shows, sends no-op.
    # Deliberately NOT in assert_runtime_secrets(): push degrades, never blocks boot.
    vapid_public_key: str = ""
    vapid_private_key: str = ""
    vapid_subject: str = "mailto:hello@blam8.com"

    # Supabase
    supabase_url: str = ""
    supabase_service_key: str = ""
    supabase_anon_key: str = ""

    # Anthropic
    anthropic_api_key: str = ""
    # Draft + concierge model. Moved Haiku 4.5 -> Sonnet 4.6 on 2026-07-12: the
    # 2026-07-12 red-team showed Haiku confirming false customer premises ("free
    # forever", inventing a Companies House entity) even with the correct facts in
    # context, which the anti-invention prompt rules alone couldn't fully hold at
    # Haiku's level. Sonnet 4.6 is the same model the booking path already uses.
    # Same structured-output shape (output_config json_schema), no prefill/sampling
    # params, so no migration changes needed. Override via env (ANTHROPIC_MODEL)
    # without a redeploy — a prod env override, if set, WINS over this default.
    anthropic_model: str = "claude-sonnet-4-6"
    # Booking-capable drafts (a scheduling / TMS / FSM module is active, so the agent
    # must reliably DRIVE the diary tools) use a stronger model: Haiku 4.5 under-calls
    # hold_booking and defers the booking; Sonnet follows the "hold before you reply"
    # mandate far more reliably. Bookings are the revenue path, so the cost is worth it;
    # tool-less replies stay on the cheaper model. Override via env without a redeploy.
    anthropic_booking_model: str = "claude-sonnet-4-6"
    # Server-owned candidate policy. No tenant-facing controls; both gates default off.
    model_candidate_enabled: bool = False
    model_candidate_model: str = "claude-haiku-5-5"
    model_candidate_operations: str = ""
    # Reviewed definition fingerprints only; never dates, sensitive or financial fields.
    model_candidate_crm_fields: str = ""

    # Concierge task list, split mode (docs/plan-planner-worker-reviewer.md). A planner
    # writes checkable success criteria before the worker runs; a reviewer checks the
    # tool trail against them. Planner and reviewer are blank = follow anthropic_model.
    # The worker is Haiku 5.5 (this is the only new model in phase 1). Env-overridable.
    anthropic_planner_model: str = ""
    anthropic_worker_model: str = "claude-haiku-5-5"
    anthropic_reviewer_model: str = ""
    # "single" = one answer_business call per task, exactly as before (the default).
    # "split" = the planner -> worker -> reviewer loop. Off until measured on the rig.
    concierge_task_loop: str = "single"

    # LLM observability — src/app/llm.py logs the FULL transcript of every Anthropic
    # call (exact system prompt, messages incl. tool loops, raw response) to
    # llm_calls. 'full' = payloads + envelope, 'meta' = envelope only (model/
    # tokens/duration), 'off' = nothing. Transcripts contain customer email
    # bodies, so the daily sweep purges rows older than the retention window.
    llm_log_mode: str = "full"
    llm_log_retention_days: int = 30

    # Prompt caching (docs/modules/module-concierge-cost-controls.md Part A) — src/app/llm.py
    # stamps one ephemeral cache breakpoint on every request's final message, so
    # tool-loop rounds and rapid chat turns re-bill their shared prefix at ~0.1x
    # instead of full price. Kill-switch (LLM_PROMPT_CACHE=false) in case a
    # model/API change ever misbehaves with the marker.
    llm_prompt_cache: bool = True

    # OpenAI — AUDIO ONLY (the one job Anthropic's API doesn't do): Whisper
    # transcription for the founder Voice CRM (/admin/voice-crm) and the live
    # voice conversation's STT/TTS (docs/voice-live-seed.md — /concierge/voice/*).
    # Every LLM text call stays on Anthropic via src/app/llm.py — this key must
    # never grow non-audio uses. Only needed when speech_provider is openai.
    openai_api_key: str = Field(default="", repr=False)

    # Local means a shared self-hosted worker, not the customer's device.
    # Select explicitly; an outage never silently sends audio to a paid provider.
    # Explicit gate: availability is not an access or health probe.
    gptlive_enabled: bool = False
    # GPT-Live: what new speech may stop a business turn that is still working. "cue_words"
    # (default) stops it only on a correction or stop cue ("no, not ...", "wait", "never mind",
    # "stop") or when the model itself raises a new request; "any_words" is the previous
    # behaviour (every heard fragment stops it). Flip it back without a deploy if a room's
    # speech needs it. Not a tenant or trade assumption. This is the rollback switch, so a typo
    # in it must not stop the app starting: the value is trimmed and lower-cased, and anything
    # else logs a warning and uses cue_words (see _interrupt_rule).
    gptlive_interrupt_rule: str = "cue_words"

    @field_validator("gptlive_interrupt_rule", mode="before")
    @classmethod
    def _interrupt_rule(cls, value):
        rule = str(value or "cue_words").strip().lower()
        if rule in {"cue_words", "any_words"}:
            return rule
        logging.getLogger(__name__).warning(
            "GPTLIVE_INTERRUPT_RULE=%r is not cue_words or any_words; using cue_words", value)
        return "cue_words"

    # GPT-Live idle close: a live conversation that has been idle this many seconds is ended
    # (the browser warns ten seconds before) so it stops billing live-voice minutes. Idle means
    # nobody speaking, nothing working and nothing waiting to be spoken; a muted microphone
    # counts as idle; while a card or draft is waiting for the owner's Confirm the time allowed
    # is doubled. 0 = never close for idleness. Env: GPTLIVE_IDLE_CLOSE_SECONDS. A value that is
    # not a whole number must not stop the app starting: it is trimmed, and anything else logs a
    # warning and uses 60 (see _idle_close_seconds). The server clamps it to 0-1800 where it is used.
    gptlive_idle_close_seconds: int = 60

    @field_validator("gptlive_idle_close_seconds", mode="before")
    @classmethod
    def _idle_close_seconds(cls, value):
        try:
            return int(str(value).strip())
        except ValueError:
            logging.getLogger(__name__).warning(
                "GPTLIVE_IDLE_CLOSE_SECONDS=%r is not a whole number of seconds; using 60", value)
            return 60

    # GPT-Live settle wait, in milliseconds: when the model raises a request, the server waits until the owner
    # has been silent this long before it takes the words heard as the request, so a pause inside one request
    # ("... can you just bin it? [pause] Or change it to two hundred quid") is not taken as its end (rig N20,
    # 15 Oct). Longer absorbs longer pauses and adds that much latency to every delegated request: the default
    # is about 0.4 s slower than the old fixed 600. The whole wait for a request's words grows with it, to the
    # settle plus 2.4 s when that is more than 3 s, so words that trail their delegation keep their headroom at
    # any setting (see gptlive.context_wait_seconds). Env: GPTLIVE_SETTLE_MS. A value that is not a whole
    # number must not stop the app starting: anything else logs a warning and uses 1000; a whole number
    # outside 300-2500 is clamped into that range, with a warning (see _settle_ms).
    gptlive_settle_ms: int = 1000

    @field_validator("gptlive_settle_ms", mode="before")
    @classmethod
    def _settle_ms(cls, value):
        try:
            ms = int(str(value))
        except ValueError:
            logging.getLogger(__name__).warning(
                "GPTLIVE_SETTLE_MS=%r is not a whole number of milliseconds; using 1000", value)
            return 1000
        clamped = max(300, min(2500, ms))
        if clamped != ms:
            logging.getLogger(__name__).warning(
                "GPTLIVE_SETTLE_MS=%r is outside 300-2500 milliseconds; using %d", value, clamped)
        return clamped

    speech_provider: Literal["openai", "local"] = "openai"
    speech_service_url: str = ""
    speech_service_ws_url: str = ""
    speech_service_key: str = ""

    # Live voice conversation (docs/voice-live-seed.md). ON wherever the
    # selected speech provider is configured (Kris 2026-09-23: the mic must start a
    # conversation, not wait on an env flip); VOICE_LIVE_ENABLED=false is the
    # off switch. The mic itself is always in the composer (as OurTab's);
    # without voice, tapping it says why.
    voice_live_enabled: bool = True
    # Enable only after the additive Cycle 3 task RPC migration is installed.
    concierge_delegation_enabled: bool = False
    # Task-list timeouts (minutes). A waiting task is nudged, then shown as
    # expired; a working task with no result is shown as stalled. Display-only:
    # the stored task is never changed by the clock.
    concierge_task_nudge_minutes: int = 30
    concierge_task_expire_minutes: int = 24 * 60
    concierge_task_stall_minutes: int = 5

    # Embeddings — Voyage AI (knowledge base retrieval). Optional: when unset the
    # knowledge base is simply off and drafting proceeds without retrieved facts.
    voyage_api_key: str = ""
    voyage_model: str = "voyage-4"          # 4-series; 1024-dim default — matches knowledge_chunks.embedding
    voyage_output_dimension: int = 1024     # pinned to the vector(1024) schema (voyage-4 supports 256/512/1024/2048)
    knowledge_match_count: int = 6          # chunks retrieved per incoming email
    # Relevance floor (Kris 2026-08-31: "every search provides six matches...
    # most aren't even relevant"): vector search always returns the K nearest
    # neighbours however distant, so a floor makes "matches" mean something.
    # Calibrated on live data — booking-only emails topped out ≤0.45 (noise),
    # genuinely relevant hits ran 0.46-0.78 (Xero doc 0.78, pricing 0.51-0.58).
    knowledge_min_similarity: float = 0.45

    # Route planning — map display (route-planning D3). One Google Cloud key
    # (Julius's env name) used by the Maps JavaScript API in the browser; when
    # unset the route page renders without a map and says why.
    google_places_api_key: str = ""

    # Twilio — SMS channel + missed-call text-back (docs/modules/module-sms-channel.md).
    # Platform-level credentials (per-tenant numbers live in channel_connections);
    # both blank = the channel is off and /webhook/sms rejects everything.
    twilio_account_sid: str = ""
    twilio_auth_token: str = Field(default="", repr=False)
    # Per-segment outbound cost recorded to usage_events (pass-through metering,
    # spec §8). Re-quote against Twilio UK rates before the pricing page ships.
    twilio_sms_segment_usd: float = 0.045

    # App
    app_base_url: str = "http://localhost:8000"
    graph_webhook_secret: str = ""

    # Build-request loop (docs/build-request-loop.md). Empty token = the admin
    # "Approve & build" button is disabled; empty secret = the event hook 503s.
    github_build_token: str = Field(default="", repr=False)   # issues + labels write
    github_build_repo: str = "haail-dev/BLAM8"
    build_request_secret: str = Field(default="", repr=False)  # HMAC for workflow events

    # Security
    token_encryption_key: str = ""          # Fernet key (urlsafe-base64, 32 bytes) for tokens at rest
    email_retention_days: int = 90          # purge window for email_log body_in / draft_reply
    # Support tickets (chatbot): resolved issue tickets lose their contact +
    # device details after this window (daily sweep — UK GDPR minimisation).
    # 12 months default, pending Julius's confirmation.
    ticket_retention_days: int = 365
    security_headers_enabled: bool = True    # master switch for the headers/CSP middleware
    csp_enforce: bool = False               # False => Content-Security-Policy-Report-Only
    rate_limit_enabled: bool = True
    # FAQ "run a scenario past BLAM8" box (POST /concierge/scenario). Kill switch,
    # the per-visitor daily allowance (per IP), and a site-wide daily ceiling so a
    # botnet of fresh IPs still cannot run the bill up. Env-overridable.
    scenario_box_enabled: bool = True
    scenario_per_ip_daily: int = 2
    scenario_global_daily: int = 150

    # Usage & cost tracking (Phase 1)
    usd_gbp_rate: float = 0.79          # approximate USD->GBP, display only (cost is stored in USD)
    usage_enforcement: str = "soft"     # "soft" = warn only | "hard" = block at limit (not wired in Phase 1)

    # Stripe billing — hosted Checkout + Customer Portal + signed webhook.
    # All blank == billing is off (require_subscription is wired but currently
    # NOT applied to dashboard routes; the gate flip is the follow-on upgrade).
    stripe_secret_key: str = ""
    stripe_publishable_key: str = ""    # reserved for future Stripe Elements use; currently unused
    stripe_webhook_secret: str = ""     # whsec_… — used by stripe.Webhook.construct_event()
    stripe_email_price_id: str = ""         # BLAM8 Starter — £39/mo (the full loop; tier renamed from "Email" 2026-07-24)
    stripe_starter_price_id: str = ""       # BLAM8 Pro — £99/mo (tier renamed from "Core" 2026-07-24)
    stripe_concierge_price_id: str = ""     # BLAM8 Pro + Concierge — £249/mo (2026-09-25)
    stripe_professional_price_id: str = ""  # Core + Field OS · Transport — £298/mo (PARKED 2026-07-24; kept so a legacy subscription still resolves)
    stripe_os_price_id: str = ""            # Core + your OS — £248/mo (PARKED 2026-07-24; kept so a legacy subscription still resolves)
    stripe_scale_price_id: str = ""         # BLAM8 Scale — £799/mo (RETIRED 2026-07-11; kept so a legacy subscription still resolves)
    stripe_enterprise_price_id: str = ""    # Enterprise — custom MSA pricing
    stripe_extra_mailbox_price_id: str = ""  # Extra-mailbox add-on — £39/mailbox/mo (recurring)
    stripe_email_topup_price_id: str = ""    # Email top-up — £39/300 emails, ONE-OFF (not a subscription)
    stripe_extra_depot_price_id: str = ""    # Additional-branch add-on — £149/branch/mo (recurring; includes its mailbox + 1,000 emails)
    trial_days: int = 14                # passed to Stripe Checkout subscription_data.trial_period_days

    # Stripe Connect — tenants' OWN payment collection (invoicing Pay Now links).
    # Distinct from the platform billing keys above: the platform secret key makes
    # the API calls, but Checkout Sessions are created ON the tenant's connected
    # account (stripe_account=acct_…) so the money lands in THEIR Stripe, never ours.
    # Blank client id == Connect is off (invoices fall back to bank-transfer details).
    stripe_connect_client_id: str = ""       # ca_… — Dashboard → Settings → Connect
    # Partner commission as subscription credit (src/app/partner_credit.py). Agreement v2.3
    # now covers it (clauses 4.9–4.12), so the remaining gate is the VAT treatment —
    # 4.12 allocates responsibility but does not decide the treatment. With this off,
    # partners can still SEE and choose the mode; nothing is ever credited.
    partner_credit_enabled: bool = False
    stripe_connect_webhook_secret: str = ""  # whsec_… for the /webhooks/stripe CONNECT endpoint

    # Access gate (trial lock). When true, require_subscription enforces on the
    # dashboard AND the drafting pipeline stops once a tenant's free trial is
    # over (14 days OR trial_email_cap emails — src/app/trial.py). Default false keeps
    # the trial open-ended (the launch state); flip REQUIRE_SUBSCRIPTION_GATE=true
    # to turn enforcement on without a redeploy. Stripe CHARGING stays separate.
    require_subscription_gate: bool = False
    # Identity stale-while-revalidate (src/app/auth.py). A signed-in GET page view whose 60 s identity cache
    # has just expired is served on the identity validated up to this many seconds ago while GoTrue
    # /user is asked again in the background (a definitive 401/403 drops the session at once; writes and
    # first-seen tokens always validate live, and so does the admin/partner side). 0 turns it off.
    # Env: IDENTITY_SWR_SECONDS.
    identity_swr_seconds: int = 120
    # Process memory read-out (src/app/memstat.py): one log line with the resident memory and the size of
    # each in-process store every N seconds (minimum 15), and the same numbers at /admin/memstat.
    # 0 = no log line. Env: MEMSTAT_SECONDS.
    memstat_seconds: int = 0
    # Free-trial email allowance — deliberately far below the paid tiers
    # so the trial is a taste, not a free ride. Env-driven so it can be retuned
    # from the Render dashboard without a redeploy (applies on the restart that an
    # env change triggers).
    trial_email_cap: int = 100
    # Free-trial concierge allowance (full-product trial, 2026-07-31): total
    # owner-assistant questions across the whole trial. Env-driven
    # (TRIAL_CONCIERGE_CAP) like the email pot; enforced behind the same
    # require_subscription_gate flag.
    trial_concierge_cap: int = 30
    # Daily fair-use allowance for concierge business chat, per plan tier
    # (docs/modules/module-concierge-cost-controls.md Part B). ALWAYS-ON (not behind the
    # gate flag — this protects margins, not the trial funnel); generous enough
    # that a normal owner never sees it. 0 = that tier unlimited; enterprise is
    # always unlimited; unknown/trial/comped plans get the Pro allowance. The
    # day boundary is Europe/London (src/app/fairuse.py).
    concierge_daily_cap_starter: int = 10   # plan key "email"  (Starter £39)
    concierge_daily_cap_pro: int = 30       # plan key "starter" (Pro £99)
    concierge_daily_cap_core: int = 60      # plan key "professional" (£248)

    # Per-tenant daily ceilings for the ONBOARDING cost surfaces (2026-09-02
    # cost review): these routes sit outside the business-chat allowance above
    # and previously had no cost guard at all. Enforced by counting today's
    # usage_events rows for the operation (src/app/fairuse.py onboarding_allowed —
    # no new table, London-day boundary, fail-open), so a cap bounds actual
    # recorded spend. Sized far above legitimate use; 0 = that surface uncapped.
    onboard_website_daily_cap: int = 10       # kb_website distills (URL pastes) ≈ $0.10 each
    voice_learning_daily_call_cap: int = 24   # voice_learning batch calls ≈ 3 full 8-batch runs
    concierge_onboard_daily_cap: int = 150    # co-pilot turns ≈ $0.02 each

    def is_configured(self) -> bool:
        """True once the essentials for a live deploy are present (advisory — drives
        landing/health copy). The hard gate is assert_runtime_secrets() at startup."""
        return all([
            self.supabase_url,
            self.supabase_service_key,
            self.anthropic_api_key,
            self.graph_webhook_secret,
            self.token_encryption_key,
        ])

    def assert_runtime_secrets(self) -> None:
        """Fail the deploy loudly if a security-critical secret is missing, rather
        than silently running with an empty webhook secret or unencryptable tokens."""
        missing = [
            name for name, value in {
                "GRAPH_WEBHOOK_SECRET": self.graph_webhook_secret,
                "TOKEN_ENCRYPTION_KEY": self.token_encryption_key,
            }.items()
            if not value
        ]
        if missing:
            raise RuntimeError(f"Missing required secret(s): {', '.join(missing)}")


def background_jobs_on() -> bool:
    """True when this process should run the in-process loops that act on
    customers' data. Explicit RUN_DAILY_JOBS wins; otherwise only on Render
    (it sets RENDER and RENDER_GIT_COMMIT on every service)."""
    import os
    if settings.run_daily_jobs is not None:
        return bool(settings.run_daily_jobs)
    return bool(os.environ.get("RENDER") or os.environ.get("RENDER_GIT_COMMIT"))


settings = Settings()
