# Google OAuth Verification — readiness checklist & working doc

> Working doc. We tick items off across sessions until complete. Self-contained on
> purpose — a fresh session (no chat context) should be able to continue from this
> file alone. Status keys: `[x]` done · `[ ]` to do. Owner: **J** = Julius (must do
> it / provide content), **C** = Claude (can produce/drive).

## Goal & why
BLAM8 sign-in is SSO-only (Google + Microsoft) via Supabase Auth. The Google OAuth
app is **In production but UNVERIFIED**. Verification removes three things:
1. the **100 new-user cap** on the Gmail/Calendar scopes,
2. the **"Google hasn't verified this app"** warning at sign-in,
3. the raw **`…supabase.co`** shown on the consent screen (→ replaced with "BLAM8" + logo).

Until verified it's fine for the pilot (<100 users, click-through warning). Needed
before public scale. BLAM8 uses **restricted scopes (Gmail)** so it hits Google's
heaviest track, which includes a paid third-party security assessment (CASA).

## Current state / facts (so a cold session has everything)
- **Google Cloud project:** `blam8-498911`
- **OAuth client:** "BLAM8 web" · client id `83704696332-hbb5b7stpa7g6ne28v2cihhhj9jgttco.apps.googleusercontent.com`
- **Scopes requested at runtime** (`src/app/gmail.py` SCOPES):
  - `https://www.googleapis.com/auth/gmail.readonly` — **RESTRICTED**
  - `https://www.googleapis.com/auth/gmail.send` — **RESTRICTED**
  - `https://www.googleapis.com/auth/calendar.events` — **SENSITIVE**
- **Supabase project ref:** `anyqmoypqfeihezovlkj` (`https://anyqmoypqfeihezovlkj.supabase.co`)
- **OAuth callback in use:** `https://anyqmoypqfeihezovlkj.supabase.co/auth/v1/callback` (registered on the Google client + the Azure app `ea2fe93e-69f8-46db-a121-6f335557b929`)
- **App domain:** `blam8.com` (canonical since the custom-domain move; `blam8.haail.dev` still resolves as a legacy alias — do NOT use it in verification) · privacy page live at `https://blam8.com/privacy` — needs Limited-Use upgrade.
- **Already done:** app **In production**; app **name** "BLAM8" + **support email** `kris.deane93@gmail.com`.

### Console links
- Branding: https://console.cloud.google.com/auth/branding?project=blam8-498911
- Audience (publishing status): https://console.cloud.google.com/auth/audience?project=blam8-498911
- Data access (scopes): https://console.cloud.google.com/auth/scopes?project=blam8-498911
- Verification centre: https://console.cloud.google.com/auth/verification?project=blam8-498911
- Credentials (OAuth client): https://console.cloud.google.com/apis/credentials?project=blam8-498911

## The track (in order)
Brand verification → Sensitive-scope verification (Calendar) → Restricted-scope
verification (Gmail) → **CASA security assessment** (the paid gate).

---

## Stage 1 — Branding (free; mostly content)
- [ ] **App logo** — square, ≤1 MB, 120×120 px. *(J: provide a BLAM8 logo → C/J upload in Branding.)*
- [ ] **Homepage URL** = `https://blam8.com` *(C can set in Branding.)*
- [ ] **Privacy policy URL** = `https://blam8.com/privacy` — **must include Google "Limited Use" disclosure** for Gmail data. *(C: draft compliant policy → J: host at /privacy.)*
- [ ] **Terms of Service URL** (optional but recommended).
- [ ] **Authorized domain** = `blam8.com` *(C can set.)*
- [ ] **Verify domain ownership** of `blam8.com` in Google Search Console. *(J: must be done by an account that owns the domain.)*

## Stage 2 — Data Access / scopes (free, ~15 min)
- [ ] **Register the scopes** on the Data Access page: `gmail.readonly`, `gmail.send`, `calendar.events`. ⚠️ Currently **EMPTY** — the app requests them at runtime but hasn't declared them; verification requires them declared. *(C can drive.)*
- [ ] **Per-scope justification** text (why each is needed). *(C: draft.)*

## Stage 3 — Submit for verification (Google review; days→weeks)
- [ ] **Demo video** (unlisted YouTube): show the consent screen with the client id, the grant, and **how each scope is used in-app**. Required for sensitive + restricted. *(C: script → J: record.)*
- [ ] **Submit** for verification and respond to the reviewer's follow-ups. *(J.)*

## Stage 4 — Restricted (Gmail) → CASA security assessment (PAID — the gate)
- [ ] **Engage a Google-approved CASA assessor**; pass (typically **Tier 2**). ~**£/$500–4,500 per year, re-done annually**. *(J: budget + engage.)*
- [ ] **Limited-Use compliance** demonstrated: no using Gmail data for ads; no human reading except security/legal/with explicit consent; secure storage/encryption; limited transfer. *(C: help document; J: ensure practices.)*

## Cross-cutting — Supabase custom domain (likely required here)
- [ ] **Supabase Custom Domain** add-on (~$10/mo) → e.g. `auth.blam8.com`. Brands the consent URL **and** moves the OAuth redirect onto a domain you own (Google verification prefers owned redirect domains; `…supabase.co` is not yours). *(J: enable add-on + DNS; C: drive Supabase config.)*
- [ ] After the custom domain: **update the 3 redirect URIs** — Supabase URL config, the Google OAuth client, and the Azure app — to the new `auth.blam8.com/auth/v1/callback`.

---

## Decisions / notes
- **Pilot stance:** staying production-unverified (<100 users, click-through warning) is acceptable now. **CASA is the gate to time for public launch.**
- **Why so heavy:** the **Gmail** restricted scopes trigger CASA. Calendar alone is only "sensitive" (no CASA). Microsoft/Outlook has none of this — a reason the strategy could lead MS-first and defer CASA (see memory `multi-vertical-expansion-strategy`).
- Don't pay for the Calendar/Gmail **APIs** — they're free; the only spend here is CASA + the optional custom domain.

## What C can produce immediately (no console needed)
1. **Limited-Use-compliant privacy policy** (→ `/privacy`) — highest leverage, unblocks Stage 1.
2. **Per-scope justifications** (Stage 2/3).
3. **Demo-video script** (Stage 3).
Then drive the console for branding fields + scope registration + domain verification.

## References
- Restricted-scope verification: https://developers.google.com/identity/protocols/oauth2/production-readiness/restricted-scope-verification
- Sensitive-scope verification: https://developers.google.com/identity/protocols/oauth2/production-readiness/sensitive-scope-verification
- CASA / Security Assessment: https://support.google.com/cloud/answer/13465431
- Supabase Custom Domains: https://supabase.com/docs/guides/platform/custom-domains
- Related memory: `sso-signin-passwordless`, `multi-vertical-expansion-strategy`, `supabase-signup-email-deliverability`
