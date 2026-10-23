# Native sign-in for the iOS apps

The BLAM8 and BLAM8 Concierge iOS apps are `WKWebView` shells around this site.
Google refuses OAuth inside embedded web views (RFC 8252), so SSO started in the
apps goes through the system sign-in sheet (`ASWebAuthenticationSession`)
instead. The web and PWA sign-in is unchanged.

## Flow

1. **Tap.** The app intercepts the site's own `/auth/sso/{google,azure}` buttons. It makes a
   random verifier `V` (32 bytes, base64url) and opens the sheet at:

   ```
   GET /auth/native/start?provider=…&scheme=…&next=…&challenge=S256(V)
   ```
2. **Start.** `/auth/native/start` validates its parameters against allow-lists, then
   redirects to GoTrue exactly like `/auth/sso/{provider}`. It uses the same
   `redirect_to={APP_BASE_URL}/auth/callback`, so the Supabase allow-list doesn't change. It
   also sets `bl_native` (scheme, challenge, landing; 10 min, `path=/auth/`) beside `bl_pkce`.
3. **Callback.** `/auth/callback` sees `bl_native` and exchanges the code as usual. It then
   parks the GoTrue session under a single-use code `K` (`native_auth.issue`) and redirects to
   `{scheme}://auth/complete?code=K`, or `?error=…`. It sets no session cookies and does no
   tenant work.
4. **Complete.** The sheet returns that URL to the app. The app's web view then sends:

   ```
   POST /auth/native/complete   (form: code, verifier)
   ```

   The server redeems `K`: single use, 120 s lifetime, `S256(verifier)` must match the
   challenge. It then runs `_complete_sign_in`, the same completion as the web callback
   (partner landing, tenant and invite, attribution, session cookies), and lands on the app's
   `next`.

Tenant work runs in step 4 rather than step 3 because the invite, referral and attribution
cookies were set in the app's web view, not in the sheet.

## Allow-lists

`src/app/native_auth.py` holds both.

| What | Allowed values | Source |
|---|---|---|
| Callback schemes | `blam8`, `blam8-concierge` | the apps' `Info.plist` |
| Landings (`next`) | `/app`, `/concierge/live` | |

A `bl_native` cookie that fails either list is ignored, and the callback takes the web path.

## Security

- **Interception.** A code intercepted on its way back to the app can't be redeemed without
  the verifier, which never leaves the app (PKCE-style binding, RFC 7636).
- **Codes.** Single use, 120 s, stored only as SHA-256 hashes. A wrong verifier still
  consumes the code.
- **POST for completion.** The code and verifier stay out of URLs and access logs.
- **No CSRF token on completion.** The shell posts it directly. The verifier binding gives
  the same protection: only the app that started a sign-in can finish it.
- **No open redirect.** Scheme and landing are exact-match allow-lists.

## Operating notes

- **One process.** Pending codes live in process memory, like the public-ask sessions in
  `concierge_routes`. The service runs one uvicorn process (`render.yaml`). A restart inside
  a code's two-minute life means the user taps sign-in again. Running more than one process
  or instance would need a shared store (for example a Postgres table) first.
- **Detecting server support.** Before using the sheet, the app requests
  `/auth/native/start?scheme=…` with no other parameters. A server with this change answers
  `303` back to the scheme; an older one returns `404`, and the app signs in inside the web
  view as before. Shipping the server and the app in either order is safe.
- **Mailbox connect isn't covered.** Gmail mailbox connect (`src/app/oauth.py`) still loads
  Google OAuth in the web view, so it fails in the apps. Connect mailboxes on the web until
  that has its own native hand-off.

## Tests

`tests/test_native_sso.py` covers:
- the start allow-lists and the GoTrue redirect;
- the native callback (code hand-back, no session, error paths, a tampered marker);
- completion parity with the web callback (tenant, invite, partner, switched-off);
- single use, expiry and wrong-verifier burning;
- the code store.

The existing callback tests pin the web path.
