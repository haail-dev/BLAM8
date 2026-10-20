"""Supabase data access for BLAutoM8 (service role — bypasses RLS).

The background agent and OAuth callback run server-side with the service key,
so they are not subject to the dashboard RLS policies (spec §5).
"""
import calendar
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import httpx
from postgrest._sync import request_builder as _pg_request_builder
from supabase import create_client, Client, ClientOptions

# Imported for its side effect, and imported HERE because every entry point in
# the app (web, jobs, scripts) comes through db: it points httpx at one shared
# TLS trust store, so building a Supabase client costs ~5ms instead of ~340ms.
from app import httpx_ssl  # noqa: F401

from app.config import settings
from app.startup_timing import instrument, span
from app import crypto
from app import navcache
from app import record_versions
from app.dates import pretty_date
from app import operating_systems

log = logging.getLogger(__name__)

_client: Client | None = None

# postgrest's send_with_retry only retries 503/520 *responses*. A transport
# error on a stale keep-alive connection — Supabase's edge GOAWAYs the HTTP/2
# connection after this instance idles — escapes it and 500s the request.
# httpx evicts the dead connection from the pool when it raises, so a single
# resend dials fresh. Patching the module global covers every sync execute()
# (service singleton and per-request user clients alike).
_pg_send = _pg_request_builder.send_with_retry

# Connection-setup / request-write / GOAWAY failures: the request never
# completed at the server, so any method is safe to resend.
_STALE_CONNECTION_ERRORS = (
    httpx.ConnectError,
    httpx.WriteError,
    httpx.RemoteProtocolError,
)
# A read that never answers (a silently dead pooled socket, or one stalled request at the edge)
# surfaces as ReadTimeout after the client's 20 s. Like ReadError it may have been processed, so
# only GET/HEAD are resent. The resend runs on a fresh socket (httpcore closes the one that raised)
# and waits far less: a normal read answers in 45-110 ms (14 Oct), and the retry must not hold a
# caller for another 20 s. Live 2026-10-14: four voice sessions were ended by single stalled reads.
_RETRY_READ_TIMEOUT = 8.0
_retry_read = threading.local()
_retry_counts: dict[str, int] = {}   # "<ErrorClass>:recovered" / ":failed"; read-only diagnostics


def _count_retry(exc, outcome):
    key = f"{type(exc).__name__}:{outcome}"
    _retry_counts[key] = min(1_000_000, _retry_counts.get(key, 0) + 1)


def _send_retrying_stale_connections(req):
    try:
        return _pg_send(req)
    except (httpx.ReadError, httpx.ReadTimeout, *_STALE_CONNECTION_ERRORS) as exc:
        # A ReadError or ReadTimeout on a write may mean the server processed it and only the
        # response was lost — resending could duplicate the write, so re-raise.
        if isinstance(exc, (httpx.ReadError, httpx.ReadTimeout)) and req.http_method not in ("GET", "HEAD"):
            raise
        log.warning(
            "postgrest connection died (%s: %s); retrying on a fresh connection",
            type(exc).__name__, exc,
        )
        _retry_read.seconds = _RETRY_READ_TIMEOUT if isinstance(exc, httpx.ReadTimeout) else None
        try:
            response = _pg_send(req)
        except Exception:
            _count_retry(exc, "failed")
            raise
        finally:
            _retry_read.seconds = None
        _count_retry(exc, "recovered")
        return response


_pg_request_builder.send_with_retry = _send_retrying_stale_connections

# Reserved sentinel tenant that OWNS the concierge's platform knowledge base
# (src/app/platform_kb_content.py). Not a real customer — excluded from operator
# aggregates (list_all_tenants) so it never skews the cross-tenant margin view.
PLATFORM_TENANT_ID = "00000000-0000-0000-0000-00000000b1a8"


def _http1_options() -> ClientOptions:
    """One httpx client per Supabase client, HTTP/1.1 only. postgrest's own
    session is HTTP/2, and a sync HTTP/2 connection shared across threads
    (the page fan-outs) corrupts its HPACK state under load — the live
    `ConnectionTerminated error_code:9` / EAGAIN 500s of 2026-09-04. An
    HTTP/1.1 pool gives every thread its own connection."""
    # keepalive_expiry: httpx drops an idle pooled socket after 5 s by default, so every page after a
    # short pause (reading, a tap 10-60 s later) opened a fresh DNS+TCP+TLS connection per concurrent
    # read: ~8-12 handshakes of CPU on a 0.5 vCPU box (a warm page costs ~60 ms CPU, the first after
    # a pause ~310 ms). Measured 14 Oct: Supabase's edge kept an idle socket usable for at least 180 s
    # (probe: reuse after 5/15/30/45/60/75/90/120/180 s all succeeded), and the retry layer above
    # re-dials on a stale-connection error, so holding sockets for 2 minutes is safe.
    return ClientOptions(httpx_client=httpx.Client(http2=False, follow_redirects=True,
                                                   timeout=httpx.Timeout(20.0),
                                                   limits=httpx.Limits(max_keepalive_connections=32,
                                                                       keepalive_expiry=120.0),
                                                   event_hooks={"request": [_stamp_request],
                                                                "response": [_count_response]}))


_DATA_WRITE_TABLES = ("/rest/v1/email_log", "/rest/v1/bookings")
_TENANT_IN_URL = re.compile(r"tenant_id=eq\.([0-9a-fA-F-]{36})")


def _note_data_write(request) -> None:
    """Any write the client sends to email_log, bookings or an RPC invalidates the page-data caches
    (navcache.bump_data). Done on the way out AND on the way back, so a reader that started between
    the two cannot store the old rows. Over-invalidating is harmless; missing a writer is not, which is
    why this sits on the client instead of in the 60 writer functions."""
    if request.method in ("GET", "HEAD"):
        return
    path = request.url.path
    if path.endswith(_DATA_WRITE_TABLES) or "/rest/v1/rpc/" in path:
        m = _TENANT_IN_URL.search(request.url.query.decode("ascii", "ignore"))
        navcache.bump_data(m.group(1).lower() if m else None)


def _stamp_request(request):
    request.extensions["blam8_started"] = time.monotonic()
    seconds = getattr(_retry_read, "seconds", None)
    if seconds:
        # httpx carries the timeout to httpcore in this extension; the hook runs in the thread
        # that is retrying, so only the resend is shortened.
        request.extensions["timeout"] = {**(request.extensions.get("timeout") or {}), "read": seconds}
    _note_data_write(request)


def _count_response(response):
    _note_data_write(response.request)
    # Opted-in startup timing only: a no-op unless this request asked for it.
    from app import startup_timing
    started = response.request.extensions.get("blam8_started")
    if started is not None:
        startup_timing.db_call(response.request, started)


def db() -> Client:
    """Lazily-created Supabase client (so import never fails on missing env)."""
    global _client
    if _client is None:
        _client = create_client(settings.supabase_url, settings.supabase_service_key,
                                options=_http1_options())
    return _client


def db_user(access_token: str) -> Client:
    """A per-request Supabase client bound to the END USER's JWT (anon key).

    PostgREST then runs under the user's auth.uid(), so RLS (auth_tenant_ids())
    enforces tenant isolation as a second wall behind the in-app tenant_id
    filters. Never cache this and never mutate the shared service singleton —
    the token is request-scoped (caching would bleed one user's token to another).
    """
    client = create_client(settings.supabase_url, settings.supabase_anon_key,
                           options=_http1_options())
    client.postgrest.auth(access_token)
    return client


# ── tenants ────────────────────────────────────────────────────────────────
def set_tenant_mailbox(tenant_id: str, mailbox: str | None) -> None:
    db().table("tenants").update({"mailbox": mailbox}).eq("id", tenant_id).execute()


def set_tenant_provider(tenant_id: str, provider: str, client: Client | None = None) -> None:
    """Record which email provider the tenant connected ('microsoft' | 'google')."""
    (client or db()).table("tenants").update({"provider": provider}).eq("id", tenant_id).execute()


def get_tenant_mailbox(tenant_id: str, client: Client | None = None) -> str | None:
    res = (client or db()).table("tenants").select("mailbox").eq("id", tenant_id).limit(1).execute()
    return res.data[0].get("mailbox") if res.data else None


def get_tenant(tenant_id: str, client: Client | None = None) -> dict | None:
    res = (
        (client or db()).table("tenants")
        .select("id,name,provider,mailbox,industry,onboarded_at,created_at,plan,"
                "billing_started_at,contract_monthly_gbp,reporting_tier,"
                "stripe_customer_id,stripe_subscription_id,subscription_status,"
                "subscription_price_id,trial_ends_at,current_period_end,billing_provider,"
                "trial_start_at,trial_emails_used,trial_status,trial_grace_until,"
                "comp_access,comp_until,fair_use_exempt,portal_slug,concierge_settings")
        .eq("id", tenant_id).limit(1).execute()
    )
    return res.data[0] if res.data else None


def set_concierge_settings(tenant_id: str, values: dict, client: Client | None = None) -> None:
    """Replace the business's Concierge settings object (see src/app/concierge_settings.py)."""
    (client or db()).table("tenants").update({"concierge_settings": values}).eq("id", tenant_id).execute()


def get_tenant_by_portal_slug(slug: str) -> dict | None:
    """The tenant behind a public portal URL (/c/{slug}) — service role: the
    route is unauthenticated and the slug IS the lookup key. Only the fields the
    portal page needs; tenant_id is derived HERE, never from the request body."""
    res = (
        db().table("tenants")
        .select("id,name,industry,onboarded_at,portal_slug")
        .eq("portal_slug", (slug or "").strip().lower()).limit(1).execute()
    )
    return res.data[0] if res.data else None


def set_portal_slug(tenant_id: str, slug: str, client: Client | None = None) -> bool:
    """Claim a portal slug for a tenant. Returns False when another tenant
    already holds it (unique index) — the caller picks a variant and retries."""
    try:
        (client or db()).table("tenants").update({"portal_slug": slug}).eq(
            "id", tenant_id).execute()
        return True
    except Exception:
        return False


def tenants_for_reference_code(code: str) -> list[dict]:
    """Every tenant that minted this customer reference code — the /for-customers
    entry page's "type your code, we find the business" path. Codes are unique
    per tenant, not globally, so two businesses can (rarely) hold the same code;
    the caller treats anything but exactly one live-portal match as not found.
    Service role, unauthenticated route: returns only what a portal link needs —
    never contact data."""
    code = (code or "").strip().upper()
    if not code:
        return []
    res = (db().table("crm_contacts").select("tenant_id")
           .eq("reference_code", code).limit(3).execute())
    ids = sorted({r["tenant_id"] for r in (res.data or [])})
    if not ids:
        return []
    t = (db().table("tenants").select("id,name,portal_slug")
         .in_("id", ids).execute())
    return t.data or []


def search_portal_tenants(q: str, limit: int = 8) -> list[dict]:
    """Public business-name search for the /for-customers entry page. Only
    tenants that already hold a portal slug (i.e. have a live portal URL) can
    match — everyone else stays invisible. Wildcards in the query are escaped
    so a visitor can't type '%' and enumerate the tenant list."""
    q = (q or "").strip()
    if len(q) < 2:
        return []
    pat = "%" + (q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")) + "%"
    res = (db().table("tenants").select("id,name,portal_slug")
           .not_.is_("portal_slug", "null")
           .ilike("name", pat).limit(limit).execute())
    return res.data or []


def set_tenant_industry(tenant_id: str, industry: str, client: Client | None = None) -> None:
    # The single industry write-path (interview + classic picker both land here),
    # so it's also where we auto-derive the OS "shape" from the sector. Only set
    # it when the industry is on the map — never clobber a known OS back to null
    # for the free-text long tail.
    fields: dict = {"industry": industry}
    os_id = operating_systems.os_for_industry(industry)
    if os_id:
        fields["operating_system"] = os_id
    (client or db()).table("tenants").update(fields).eq("id", tenant_id).execute()


def set_tenant_name(tenant_id: str, name: str, client: Client | None = None) -> None:
    """The customer-facing trading name (portal header, replies). Sign-up seeds a
    placeholder derived from the login email, so onboarding offers this rename."""
    (client or db()).table("tenants").update({"name": name}).eq("id", tenant_id).execute()


def ensure_tenant(tenant_id: str, name: str) -> None:
    """Idempotently ensure a tenants row exists. Used for the reserved sentinel
    tenant that OWNS the concierge's platform knowledge base — knowledge_chunks
    has a FK to tenants(id), so the row must exist before its chunks ingest.
    Upserts on the primary key, so re-running is a no-op."""
    db().table("tenants").upsert({"id": tenant_id, "name": name}).execute()


def is_enterprise(tenant, client: Client | None = None) -> bool:
    """THE gate for multi-mailbox behaviour (enterprise spec D2): plan == 'enterprise'.
    Accepts a tenant dict (no extra query) or a tenant id."""
    t = tenant if isinstance(tenant, dict) else (get_tenant(tenant, client=client) or {})
    return (t or {}).get("plan") == "enterprise"


def set_tenant_plan(tenant_id: str, plan: str) -> None:
    """Operator console only (the Account type flip) — behind require_admin."""
    db().table("tenants").update({"plan": plan}).eq("id", tenant_id).execute()
    navcache.invalidate_tenant(tenant_id)   # nav chrome caches plan/enterprise


def sync_tenant_mailbox_mirror(tenant_id: str) -> None:
    """Keep tenants.mailbox/provider mirroring the DEFAULT (oldest) connection —
    the legacy single-mailbox reads. Called after every connect/disconnect. With
    no connections left, mailbox clears; provider keeps its last value (the
    column is NOT NULL and nothing reads it without a connection)."""
    res = (
        db().table("email_connections").select("address,provider")
        .eq("tenant_id", tenant_id).order("created_at").limit(1).execute()
    )
    row = res.data[0] if res.data else {}
    update: dict = {"mailbox": row.get("address")}
    if row.get("provider"):
        update["provider"] = row["provider"]
    db().table("tenants").update(update).eq("id", tenant_id).execute()


def is_onboarded(tenant_id: str, client: Client | None = None) -> bool:
    res = (client or db()).table("tenants").select("onboarded_at").eq("id", tenant_id).limit(1).execute()
    return bool(res.data and res.data[0].get("onboarded_at"))


def set_onboarded(tenant_id: str, client: Client | None = None) -> None:
    (client or db()).table("tenants").update(
        {"onboarded_at": datetime.now(timezone.utc).isoformat()}
    ).eq("id", tenant_id).execute()


# ── email_connections (enterprise D1: THE mailbox entity) ──────────────────
def upsert_email_connection(
    tenant_id: str,
    access_token: str,
    refresh_token: str | None,
    token_expires: datetime,
    *,
    address: str | None = None,
    provider: str | None = None,
    mode: str = "replace",
) -> str:
    """Store a mailbox connection; returns its id.

    Reconnecting an address the tenant already has UPDATES that row in place —
    the connection id is the mailbox's identity (mailbox_config, scoped
    knowledge, email_log stamps and the Gmail watermark all hang off it), so a
    token refresh must never mint a new one. calendar_connected_at is cleared
    here because the new grant may have dropped calendar scope; the OAuth
    callback re-lights it when the scope is present.

    mode='replace' (SaaS — one mailbox per tenant): any OTHER connection is
    deleted, cascading its mailbox_config + scoped knowledge.
    mode='add' (enterprise "Add mailbox"): siblings are left alone.
    """
    payload = {
        "access_token": crypto.encrypt(access_token),
        "refresh_token": crypto.encrypt(refresh_token),
        "token_expires": token_expires.isoformat(),
        "address": address,
        "provider": provider,
        "calendar_connected_at": None,
    }
    existing_id = None
    if address:
        # A mailbox's identity is (address, provider) — the live pilot proved
        # address alone is unsafe: a Microsoft account whose PRIMARY alias is a
        # gmail address reports that gmail address to Graph, so the same string
        # can name two different provider mailboxes. Same address on a different
        # provider is therefore a SIBLING, not a reconnect. A legacy row with no
        # provider yet matches any (it predates per-connection providers).
        res = db().table("email_connections").select("id,address,provider").eq("tenant_id", tenant_id).execute()
        existing_id = next(
            (r["id"] for r in (res.data or [])
             if (r.get("address") or "").lower() == address.lower()
             and (r.get("provider") or provider) == provider),
            None,
        )
    if mode != "add":
        q = db().table("email_connections").delete().eq("tenant_id", tenant_id)
        if existing_id:
            q = q.neq("id", existing_id)
        q.execute()
    if existing_id:
        db().table("email_connections").update(payload).eq("id", existing_id).execute()
        return existing_id
    res = db().table("email_connections").insert({"tenant_id": tenant_id, **payload}).execute()
    return res.data[0]["id"]


def _decrypt_conn(row: dict) -> dict:
    """Decrypt the token columns of an email_connections row in place."""
    row["access_token"] = crypto.decrypt(row.get("access_token"))
    row["refresh_token"] = crypto.decrypt(row.get("refresh_token"))
    return row


def get_email_connection(tenant_id: str, client: Client | None = None) -> dict | None:
    """The tenant's DEFAULT mailbox connection (oldest) — every legacy
    single-mailbox call path resolves here, so the ordering must be
    deterministic now that an enterprise tenant can hold several rows."""
    res = (
        (client or db()).table("email_connections").select("*")
        .eq("tenant_id", tenant_id).order("created_at").limit(1).execute()
    )
    return _decrypt_conn(res.data[0]) if res.data else None


def claim_mailbox_import(tenant_id: str, connection_id: str) -> dict | None:
    """Claim the durable per-mailbox lease before scheduling any background work."""
    return db().rpc("claim_mailbox_import", {
        "p_tenant": tenant_id, "p_connection": connection_id}).execute().data or None


def choose_mailbox_import(tenant_id, connection_id, actor_id, decision, expected_run):
    return db().rpc("choose_mailbox_import", {
        "p_tenant": tenant_id, "p_connection": connection_id, "p_actor": actor_id,
        "p_decision": decision, "p_expected_run": expected_run}).execute().data is True


def mailbox_import_access(tenant_id, connection_id, client=None):
    rows = ((client or db()).table("email_connections")
            .select("id,address,created_at,backlog_import,backlog_import_choice")
            .eq("tenant_id", tenant_id).eq("id", connection_id).limit(1).execute().data)
    if not rows:
        raise LookupError("Mailbox not found")
    return rows[0]


def finish_mailbox_import(tenant_id: str, connection_id: str, run: dict) -> None:
    """An old worker cannot overwrite a replacement run's status."""
    db().table("email_connections").update({"backlog_import": run}).eq(
        "tenant_id", tenant_id).eq("id", connection_id).eq(
        "backlog_import->>run_id", run["run_id"]).eq(
        "backlog_import->>state", "running").execute()


def get_mailbox_import(tenant_id: str, connection_id: str, client=None) -> dict:
    rows = ((client or db()).table("email_connections").select("backlog_import")
            .eq("tenant_id", tenant_id).eq("id", connection_id).limit(1).execute().data)
    if not rows:
        raise LookupError("Mailbox not found")
    return rows[0].get("backlog_import") or {"state": "not_started"}


def get_connection_by_id(connection_id: str, tenant_id: str | None = None) -> dict | None:
    """One mailbox connection by id (tokens decrypted). Pass tenant_id from any
    tenant-facing path so another tenant's id can never resolve (IDOR wall)."""
    q = db().table("email_connections").select("*").eq("id", connection_id)
    if tenant_id:
        q = q.eq("tenant_id", tenant_id)
    res = q.limit(1).execute()
    return _decrypt_conn(res.data[0]) if res.data else None


_MAILBOX_META_COLS = (
    "id,tenant_id,address,provider,label,department,category,"
    "subscription_id,sub_expires,calendar_connected_at,created_at,grandfathered"
)


def list_mailboxes(tenant_id: str, client: Client | None = None) -> list[dict]:
    """Every connected mailbox, oldest (the default) first — NON-SECRET columns
    only, for the dashboard. The token columns are never selected here."""
    res = (
        (client or db()).table("email_connections").select(_MAILBOX_META_COLS)
        .eq("tenant_id", tenant_id).order("created_at").execute()
    )
    return res.data or []


def update_mailbox_meta(
    connection_id: str, tenant_id: str,
    label: str | None, department: str | None, category: str | None,
    client: Client | None = None,
) -> None:
    """The mailbox's grouping labels (D6 — free text in v1), tenant-scoped."""
    (client or db()).table("email_connections").update(
        {"label": label, "department": department, "category": category}
    ).eq("id", connection_id).eq("tenant_id", tenant_id).execute()


def delete_connection_by_id(connection_id: str, tenant_id: str) -> None:
    """Disconnect ONE mailbox (enterprise) — siblings untouched. Cascades its
    mailbox_config and mailbox-scoped knowledge; email_log rows keep their
    mailbox_id stamp (no FK) for audit."""
    db().table("email_connections").delete() \
        .eq("id", connection_id).eq("tenant_id", tenant_id).execute()


def own_mailbox_addresses(tenant_id: str) -> set:
    """Every address this tenant's mailboxes answer from (lowercased), plus the
    legacy tenants.mailbox mirror — the SIBLING SET for the self-mail guard:
    inbound from any of these drafts but never auto-sends (orders@ and info@
    must not loop each other). Includes each mailbox's send_as_address: mail we
    send AS that identity can route straight back (2026-07-26 e2e — hello@ via
    the Cloudflare route) and is just as much 'our own mail' as the mailbox
    address itself."""
    res = (db().table("email_connections").select("address,send_as_address")
           .eq("tenant_id", tenant_id).execute())
    out = set()
    for r in (res.data or []):
        for a in (r.get("address"), r.get("send_as_address")):
            if a and a.strip():
                out.add(a.strip().lower())
    mirror = get_tenant_mailbox(tenant_id)
    if mirror:
        out.add(mirror.lower())
    return out


def mailbox_self_addresses(tenant_id: str, mailbox_id: str | None = None,
                           client: Client | None = None) -> set:
    """The identities THIS mailbox sends as (lowercased): its own address plus its
    send_as_address when set. The hard self-loop set — inbound From any of these
    is our own outgoing mail re-arriving and is never drafted to (the sibling set
    above still drafts; only auto-send is held there). Selects only address
    columns, never the encrypted tokens."""
    c = client or db()
    row = None
    if mailbox_id:
        res = (c.table("email_connections").select("address,send_as_address")
               .eq("id", mailbox_id).eq("tenant_id", tenant_id).limit(1).execute())
        row = (res.data or [None])[0]
    if row is None:
        res = (c.table("email_connections").select("address,send_as_address")
               .eq("tenant_id", tenant_id).order("created_at").limit(1).execute())
        row = (res.data or [None])[0]
    out = set()
    for a in ((row or {}).get("address"), (row or {}).get("send_as_address")):
        if a and a.strip():
            out.add(a.strip().lower())
    mirror = get_tenant_mailbox(tenant_id, client=client)
    if not out and mirror:
        out.add(mirror.lower())
    return out


def mailbox_address(tenant_id: str, mailbox_id: str | None = None, client: Client | None = None) -> str | None:
    """The single address THIS mailbox answers from (lowercased) — the given
    connection, or the tenant's default. Used to detect our own outgoing reply
    re-arriving in the same mailbox (the self-loop guard in the pipeline). Selects
    only the address column, so it never touches the encrypted tokens."""
    c = client or db()
    if mailbox_id:
        res = (c.table("email_connections").select("address")
               .eq("id", mailbox_id).eq("tenant_id", tenant_id).limit(1).execute())
        if res.data and res.data[0].get("address"):
            return res.data[0]["address"].lower()
    addr = get_tenant_mailbox(tenant_id, client=client)
    return addr.lower() if addr else None


def update_connection_tokens(
    connection_id: str, access_token: str, refresh_token: str, token_expires: datetime
) -> None:
    db().table("email_connections").update(
        {
            "access_token": crypto.encrypt(access_token),
            "refresh_token": crypto.encrypt(refresh_token),
            "token_expires": token_expires.isoformat(),
        }
    ).eq("id", connection_id).execute()


def delete_email_connection(tenant_id: str, client: Client | None = None) -> None:
    """Disconnect the mailbox: drop the stored connection (tokens + subscription)."""
    (client or db()).table("email_connections").delete().eq("tenant_id", tenant_id).execute()


def get_connection_status(tenant_id: str, client: Client | None = None) -> dict | None:
    """Non-secret connection status for the dashboard — no token columns read.
    The DEFAULT (oldest) mailbox, matching get_email_connection."""
    res = (
        (client or db()).table("email_connections")
        .select("subscription_id,sub_expires").eq("tenant_id", tenant_id)
        .order("created_at").limit(1).execute()
    )
    return res.data[0] if res.data else None


def list_all_connections() -> list[dict]:
    """Every connection (tokens decrypted) — for the self-healing renewal cron."""
    res = db().table("email_connections").select("*").execute()
    return [_decrypt_conn(r) for r in (res.data or [])]


def list_google_connections() -> list[dict]:
    """Every Gmail-provider connection (tokens decrypted) — the poll set for
    Gmail auto-draft. Provider lives on the CONNECTION (enterprise D5), which
    fixes the poll set for a mixed tenant: its Gmail branch polls while its
    Outlook branches stay on the webhook."""
    res = db().table("email_connections").select("*").eq("provider", "google").execute()
    return [_decrypt_conn(r) for r in (res.data or [])]


def set_gmail_history_id(connection_id: str, history_id: str) -> None:
    """Advance the Gmail polling watermark (users.history.list startHistoryId)."""
    db().table("email_connections").update({"gmail_history_id": history_id}) \
        .eq("id", connection_id).execute()


def get_google_connection_by_address(address: str) -> dict | None:
    """The Gmail connection for one mailbox address — how a Pub/Sub push
    notification (which carries only emailAddress + historyId) finds its
    connection. Case-insensitive in Python because the address column stores
    whatever the provider reported (upsert compares the same way)."""
    addr = (address or "").lower()
    if not addr:
        return None
    return next((c for c in list_google_connections()
                 if (c.get("address") or "").lower() == addr), None)


def set_gmail_watch_expiry(connection_id: str, expiry_iso: str) -> None:
    """Stamp when this mailbox's Gmail push watch lapses (users.watch, ~7 days) —
    the renewal loop re-arms any watch inside its 24h renew window."""
    db().table("email_connections").update({"gmail_watch_expiry": expiry_iso}) \
        .eq("id", connection_id).execute()


# ── subscriptions (the module switchboard) ──────────────────────────────────
def active_modules(tenant_id: str, client: Client | None = None) -> set:
    """The set of module ids this tenant has switched on — subscriptions rows with
    status='active'. The base 'email' module is treated as always-on by the hub
    (every tenant starts with it), so it needs no row; future modules (scheduling,
    voice…) activate by inserting one. Flipping a row is the whole rollout."""
    res = (
        (client or db()).table("subscriptions").select("module")
        .eq("tenant_id", tenant_id).eq("status", "active").execute()
    )
    return {r["module"] for r in (res.data or [])}


def module_active(tenant_id: str, module: str, client: Client | None = None) -> bool:
    """Is one module switched on for this tenant? The pipeline asks this before
    injecting that module's tools — data-gated, never code paths."""
    return module in active_modules(tenant_id, client=client)


# ── OS preview (founding-partner gate's look-inside mode) ────────────────────
# A subscriptions row can carry status='preview': the tenant SEES the module's
# dashboard surfaces (seeded with the same working defaults as activation) but
# the AGENT stays off — pipeline tool injection, proactive jobs and calendar
# sync all gate on module_active()/active_modules(), which only count 'active'.
# UI routes and nav gate on the *_visible/visible_* variants below, which
# COMPOSE on those primitives (preview is an additive overlay, never a rewrite
# of the switchboard's source of truth).

def preview_modules(tenant_id: str, client: Client | None = None) -> set:
    """Modules this tenant is previewing (look-but-the-agent's-off)."""
    res = (
        (client or db()).table("subscriptions").select("module")
        .eq("tenant_id", tenant_id).eq("status", "preview").execute()
    )
    return {r["module"] for r in (res.data or [])}


def visible_modules(tenant_id: str, client: Client | None = None) -> set:
    """Modules whose UI surfaces should render: live ones plus previews."""
    return active_modules(tenant_id, client=client) | preview_modules(tenant_id, client=client)


def module_ui_visible(tenant_id: str, module: str, client: Client | None = None) -> bool:
    """UI-route gate: active OR preview. Never used by the pipeline/jobs —
    they stay on module_active()."""
    return (module_active(tenant_id, module, client=client)
            or module in preview_modules(tenant_id, client=client))


def set_module_preview(tenant_id: str, module: str) -> None:
    """Flip a module row to 'preview'. Never downgrades an ACTIVE row — starting
    a preview of something you already own is a no-op."""
    existing = (
        db().table("subscriptions").select("id,status")
        .eq("tenant_id", tenant_id).eq("module", module).limit(1).execute()
    )
    if existing.data:
        if existing.data[0]["status"] == "active":
            return
        db().table("subscriptions").update({"status": "preview"}).eq("id", existing.data[0]["id"]).execute()
    else:
        db().table("subscriptions").insert(
            {"tenant_id": tenant_id, "module": module, "status": "preview"}
        ).execute()
    navcache.invalidate_tenant(tenant_id)   # nav chrome caches the module sets


def seed_module_defaults(tenant_id: str, module: str) -> None:
    """The activation side-effects shared by real activation and preview: seed a
    working operation, not empty tables. Idempotent — every seeder guards."""
    if module == "tms":
        seed_fleet_defaults(tenant_id)
        seed_tms_defaults(tenant_id)
    elif module == "fsm":                    # field-service (docs/modules/module-field-service.md)
        seed_fsm_defaults(tenant_id)
    elif module == "assets":                 # Assets OS (docs/os/assets.md §9)
        seed_assets_defaults(tenant_id)


def apply_plan_modules(tenant_id: str, plan_name: str | None) -> set[str]:
    """Plan-bundled entitlement: reconcile a tenant's switchboard modules to match
    their plan AND their industry (app/plans.modules_for). Activates the base
    modules for the tier plus the tenant's OWN industry module (Logistics → TMS;
    never another vertical's) — seeding TMS defaults exactly like the operator
    path so the tenant lands on a working operation, not empty tables — and pauses
    any managed module the plan excludes (e.g. on downgrade, or a non-logistics
    tenant who must never get TMS). A 'preview' row survives plan churn on OTHER
    tiers (buying Core must not kill an OS preview); a wanted module upgrades
    preview → active. Email (base) is never touched. Called from
    billing.sync_subscription when a subscription goes active/trialing. Returns the
    set of modules now active."""
    from app import plans
    industry = (get_tenant(tenant_id) or {}).get("industry")
    wanted = plans.modules_for(plan_name, industry)
    previews = preview_modules(tenant_id)
    for module in plans.MANAGED_MODULES:
        on = module in wanted
        if not on and module in previews:
            continue                          # previews outlive non-OS plan changes
        set_module_active(tenant_id, module, on)
        if on:
            seed_module_defaults(tenant_id, module)   # mirror the operator activation side-effects
    return wanted


def set_module_active(tenant_id: str, module: str, active: bool) -> None:
    """Flip one module's subscriptions row (insert on first activation, update
    after). Flipping this row IS the rollout. SERVICE ROLE — called by the operator
    switchboard (admin console) and by billing.apply_plan_modules on a plan change."""
    status = "active" if active else "paused"
    existing = (
        db().table("subscriptions").select("id")
        .eq("tenant_id", tenant_id).eq("module", module).limit(1).execute()
    )
    if existing.data:
        db().table("subscriptions").update({"status": status}).eq("id", existing.data[0]["id"]).execute()
    else:
        db().table("subscriptions").insert(
            {"tenant_id": tenant_id, "module": module, "status": status}
        ).execute()
    navcache.invalidate_tenant(tenant_id)   # nav chrome caches the module sets


# ── scheduling module (capacity diary — src/app/scheduling.py owns the semantics) ─
def get_scheduling_config(tenant_id: str, client: Client | None = None) -> dict | None:
    res = (client or db()).table("scheduling_config").select("*").eq("tenant_id", tenant_id).limit(1).execute()
    return res.data[0] if res.data else None


def apply_booking_settings(tenant_id: str, cfg: dict, revision: int, client=None):
    """One atomic revision-checked settings write under the tenant's existing RLS."""
    return (client or db()).rpc("apply_booking_settings", {
        "p_tenant": tenant_id, "p_revision": revision, "p_patch": cfg,
    }).execute()


def upsert_scheduling_config(tenant_id: str, cfg: dict, client: Client | None = None) -> None:
    if "intake_fields" in cfg and "booking_mode" not in cfg:
        from app import intake
        cfg = {**cfg, "booking_mode": "stay" if intake.is_stay(cfg["intake_fields"]) else "appointment"}
    (client or db()).table("scheduling_config").upsert(
        {"tenant_id": tenant_id, **cfg, "updated_at": datetime.now(timezone.utc).isoformat()}
    ).execute()


# ── Phase 2B — day-before reminders (src/app/jobs/send_reminders.py) ─────────────
def tenants_with_modules(modules: tuple, client: Client | None = None) -> list[str]:
    """Tenant ids with ANY of `modules` switched on (status='active'). The reminder job's
    work-list: only diary-bearing tenants can have bookings to remind about."""
    res = ((client or db()).table("subscriptions").select("tenant_id")
           .in_("module", list(modules)).eq("status", "active").execute())
    return list({r["tenant_id"] for r in (res.data or [])})


def reachable_contact(contact) -> bool:
    """Can the reminder/recall jobs message this booking contact at all? An email
    address, or an E.164 phone (an SMS-booked contact — docs/modules/module-sms-channel.md
    §6; the shape check lives here so db never imports app.sms)."""
    c = str(contact or "")
    return "@" in c or bool(re.fullmatch(r"\+[1-9]\d{6,14}", c))


def setup_has_booking_history(tenant_id: str, client: Client | None = None) -> bool:
    rows = ((client or db()).table("bookings").select("id").eq("tenant_id", tenant_id).limit(1).execute().data)
    if not isinstance(rows, list):
        raise RuntimeError("Booking history could not be verified")
    return bool(rows)


def count_bookings_since(tenant_id: str, since_iso: str, source: str | None = None,
                         client: Client | None = None) -> int:
    """How many bookings were CREATED since `since_iso` (optionally from one
    source — 'portal' for the customer portal's own bookings). One count
    query; the usage tile's "portal · N bookings"."""
    q = ((client or db()).table("bookings").select("id", count="exact")
         .eq("tenant_id", tenant_id).gte("created_at", since_iso))
    if source:
        q = q.eq("source", source)
    res = q.limit(1).execute()
    return int(getattr(res, "count", None) or 0)


def list_bookings_for_reminder(tenant_id: str, date_iso: str, client: Client | None = None) -> list[dict]:
    """CONFIRMED bookings on `date_iso` that still need a day-before reminder: a
    reachable customer contact (email or SMS phone) and not yet reminded. Proposed
    holds (unapproved) and already-reminded rows are excluded, so a booking is
    reminded at most once."""
    res = ((client or db()).table("bookings").select("*")
           .eq("tenant_id", tenant_id).eq("booking_date", date_iso)
           .eq("status", "confirmed").is_("reminder_queued_at", "null").execute())
    return [b for b in (res.data or []) if reachable_contact(b.get("contact"))]


def mark_booking_reminded(booking_id: str, tenant_id: str, client: Client | None = None) -> None:
    """Stamp a booking reminded (dedupe), tenant-scoped."""
    (client or db()).table("bookings").update(
        {"reminder_queued_at": datetime.now(timezone.utc).isoformat()}
    ).eq("id", booking_id).eq("tenant_id", tenant_id).execute()


def save_scheduling_intake(tenant_id: str, fields: list, client: Client | None = None) -> None:
    """Persist the scheduling-intake schema and stamp it configured (which unblocks the
    office diary — the "block the diary until set up" onboarding). `fields` is the cleaned
    list of {label, required, hint} from app/intake."""
    upsert_scheduling_config(tenant_id, {
        "intake_fields": fields,
        "intake_configured_at": datetime.now(timezone.utc).isoformat(),
    }, client=client)


def mark_dependants_configured(tenant_id: str, client: Client | None = None) -> None:
    """The Dependants page was answered (run #8): a kind, a ladder-only save, or
    nothing of the sort — the setup hand-off counts the step done either way."""
    upsert_scheduling_config(tenant_id, {
        "dependants_configured_at": datetime.now(timezone.utc).isoformat(),
    }, client=client)


def _drop_expired_offers(rows: list[dict]) -> list[dict]:
    """An offered slot past its deadline no longer exists for ANY consumer —
    capacity math, the diary, or the next draft's context — even before the
    release sweep has cancelled the row (docs/modules/module-smart-booking-offers.md).
    Proposed/confirmed rows pass through untouched."""
    now = datetime.now(timezone.utc)
    out = []
    for r in rows:
        if r.get("status") == "offered" and r.get("expires_at"):
            try:
                if datetime.fromisoformat(str(r["expires_at"]).replace("Z", "+00:00")) <= now:
                    continue
            except ValueError:
                pass
        out.append(r)
    return out


def list_bookings(tenant_id: str, date_from_iso: str, client: Client | None = None) -> list[dict]:
    """Full diary rows from a date forward (offered + proposed + confirmed — the
    Diary's working set; declined/cancelled rows stay in the table but aren't
    shown, and an offered row past its deadline drops out immediately)."""
    # Cached like the conversation list (up to 60 s, dropped by ANY write to bookings or email_log via
    # the db client hooks, a fill that raced a write is not stored). The expiry filter runs AFTER the
    # cache on a private copy: an offer past its deadline must still vanish at once, not at the TTL.
    import copy

    def read():
        res = (
            (client or db()).table("bookings").select("*")
            .eq("tenant_id", tenant_id)
            .in_("status", ["offered", "proposed", "confirmed"])
            .gte("booking_date", date_from_iso)
            .order("booking_date").order("created_at")
            .execute()
        )
        return res.data or []
    rows = navcache.get_or_fill_data(tenant_id, f"t:{tenant_id}:data:bookings:{date_from_iso}", 60.0, read)
    return _drop_expired_offers(copy.deepcopy(rows))


def list_booking_contacts(tenant_id: str, since_iso: str,
                          client: Client | None = None) -> list[dict]:
    """(contact, resource_type) for every booking that still counts since a
    date — the minimum needed to decide whose customers these are (People
    slice 3). Cancelled rows are dropped: a booking somebody cancelled is not
    a working relationship. Two columns, because this runs on a page load."""
    res = (
        (client or db()).table("bookings").select("contact,resource_type")
        .eq("tenant_id", tenant_id).neq("status", "cancelled")
        .gte("booking_date", since_iso)
        .execute()
    )
    return res.data or []


def list_live_bookings(tenant_id: str, client: Client | None = None) -> list[dict]:
    """Every offered / proposed / confirmed row, past dates included — the
    diary's clear-all reads this so a reset never leaves a stale event behind
    (list_bookings starts at today; an old confirmed row would survive it)."""
    res = (
        (client or db()).table("bookings")
        .select("id,booking_ref,status,booking_date,calendar_event_id,contact,email_log_id")
        .eq("tenant_id", tenant_id)
        .in_("status", ["offered", "proposed", "confirmed"])
        .order("booking_date")
        .execute()
    )
    return res.data or []


def bookings_for_contact(tenant_id: str, contact: str | None, client: Client | None = None) -> list[dict]:
    """Active (offered/proposed/confirmed) bookings this contact already has — injected
    into the draft so a follow-up reply references the existing booking (its ref, date,
    status) instead of offering to book again; offered rows tell the agent which options
    are already out with this customer. Empty when the contact has none."""
    if not contact:
        return []
    res = (
        (client or db()).table("bookings")
        # `id` matters: the portal's list_my_bookings builds its booking_id
        # from it — without it every portal reschedule died on not_found
        # (live bug found 2026-07-31, present since portal phase 1). The grid
        # columns feed the portal's cross-session duplicate guard (a stay's
        # real span — 2026-09-03).
        # `details` too (2026-09-04): the portal record carries the facts a
        # customer gave on an earlier booking, so they are never re-asked.
        .select("id,booking_ref,booking_date,window,status,summary,expires_at,"
                "start_block,length_blocks,details,contact")
        .eq("tenant_id", tenant_id).eq("contact", contact)
        .in_("status", ["offered", "proposed", "confirmed"])
        .order("booking_date")
        .execute()
    )
    return _drop_expired_offers(res.data or [])


def list_active_bookings(
    tenant_id: str, date_from_iso: str, date_to_iso: str, client: Client | None = None
) -> list[dict]:
    """Capacity-consuming bookings (offered + proposed + confirmed) in a date range — the
    input to scheduling.availability(), .fleet_availability() and .service_availability().
    Offered slots consume capacity (exclusive offers) until their deadline passes. The fleet
    columns ride along (windows-mode math ignores them); the grid columns (start_block/
    length_blocks) carry the real span so concurrent-occupancy capacity (stays + per-service
    durations) counts a booking on every block it covers, not just its date."""
    res = (
        (client or db()).table("bookings")
        # `id` rides along: the wallet sweep (src/app/wallet.py run_sweep) keys its
        # attendance / spend look-ups on it (run #8: without it the sweep died
        # silently and credits.spend = auto never spent anything).
        # `resource_type` + `quantity` (2026-09-23): the pool maths reads both —
        # _pool_spans filters a pool by resource_type and stay_span_q weights by
        # quantity. Without them every typed pool counted NOTHING (an
        # appointment's "at once" never saw the one already booked) and a stays
        # diary counted its appointments against the spaces.
        .select("id,booking_date,window,status,vehicle_id,spaces,weight_kg,"
                "start_block,length_blocks,engineer_id,expires_at,resource_type,quantity")
        .eq("tenant_id", tenant_id)
        .in_("status", ["offered", "proposed", "confirmed"])
        .gte("booking_date", date_from_iso).lte("booking_date", date_to_iso)
        .execute()
    )
    return _drop_expired_offers(res.data or [])


def count_service_bookings_by_day(tenant_id: str, service_name: str, date_from_iso: str,
                                  date_to_iso: str, exclude_id: str | None = None,
                                  client: Client | None = None) -> dict[str, int]:
    """{YYYY-MM-DD: n} active bookings of one service per day — a service's "max a
    day" (docs/plan-diary-multiview.md). A service booking carries its name in
    details.Service (scheduling._service_hold sets it). Read only when the rule
    is set; `exclude_id` leaves out a booking being moved."""
    res = (
        (client or db()).table("bookings")
        .select("id,booking_date,status,expires_at")
        .eq("tenant_id", tenant_id)
        .in_("status", ["offered", "proposed", "confirmed"])
        .eq("details->>Service", service_name)
        .gte("booking_date", date_from_iso).lte("booking_date", date_to_iso)
        .execute()
    )
    out: dict[str, int] = {}
    for b in _drop_expired_offers(res.data or []):
        if exclude_id and b.get("id") == exclude_id:
            continue
        d = str(b.get("booking_date"))[:10]
        out[d] = out.get(d, 0) + 1
    return out


def hold_booking_if_capacity(
    tenant_id: str,
    booking_date_iso: str,
    window: str,
    summary: str,
    details: dict | None = None,
    contact: str | None = None,
    source: str = "email",
    thread_id: str | None = None,
    status: str = "proposed",
    expires_at: str | None = None,
) -> dict | None:
    """Atomic capacity-check + insert via the SQL function (service-role RPC, like
    match_knowledge_chunks). Returns the new bookings row, or None when the
    window is full — the ONLY place capacity is decided, so racing holds can't
    overbook. The agent path keeps status='proposed' ('offered' + expires_at for
    soft offer-holds); the Diary's manual add may pass 'confirmed'."""
    res = db().rpc(
        "hold_booking_if_capacity",
        {
            "p_tenant_id": tenant_id,
            "p_booking_date": booking_date_iso,
            "p_window": window,
            "p_summary": summary,
            "p_details": details,
            "p_contact": contact,
            "p_source": source,
            "p_thread_id": thread_id,
            "p_status": status,
            "p_expires_at": expires_at,
        },
    ).execute()
    return res.data[0] if res.data else None


def insert_stay_booking(tenant_id: str, booking_date_iso: str, start_block: int,
                        length_blocks: int, summary: str, details: dict | None = None,
                        contact: str | None = None, source: str = "email",
                        thread_id: str | None = None, status: str = "proposed",
                        window: str = "day", quantity: int | None = None,
                        resource_type: str | None = None, deposit_amount=None,
                        recurrence_id: str | None = None, expires_at: str | None = None,
                        client: Client | None = None) -> dict:
    """Insert a grid-span hold (booking_date + start_block + length_blocks — docs/
    unified-slot-grid.md): a multi-day STAY (length runs past a day) OR a per-service
    appointment (a within-day span of the service's length). `window` defaults to 'day' for
    legacy stay readers; a service hold passes its start time ('14:00') so the row reads like
    a slots-mode booking. The grid columns carry the real span either way. Capacity is
    concurrent occupancy, checked in the app layer (slotgrid.stay_offer / max_concurrent)
    BEFORE this insert for useful feedback. The bookings_capacity_guard database
    trigger serializes office writers and rechecks capacity at the actual write.

    The inbound-depth columns (quantity = units consumed / party size; resource_type = which
    capacity pool; deposit_amount; recurrence_id = a standing series) are written ONLY when
    set, so a row that uses none of them is shaped exactly as before. Returns the new row."""
    c = client or db()
    ref = c.rpc("next_booking_ref", {"p_tenant_id": tenant_id}).execute().data
    row = {
        "tenant_id": tenant_id, "contact": contact, "summary": summary, "details": details,
        "booking_date": booking_date_iso, "window": window, "status": status,
        "source": source, "thread_id": thread_id, "booking_ref": ref,
        "start_block": int(start_block), "length_blocks": int(length_blocks),
    }
    for key, val in (("quantity", quantity), ("resource_type", resource_type),
                     ("deposit_amount", deposit_amount), ("recurrence_id", recurrence_id),
                     ("expires_at", expires_at)):
        if val is not None:
            row[key] = val
    res = c.table("bookings").insert(row).execute()
    return res.data[0]


# ── fleet module (fleet resources as the unit of capacity — docs/modules/module-fleet-capacity.md) ─
# branches / `vehicles` / `pallet_types` CRUD for the Fleet settings page (RLS via
# the ctx client), plus the fleet hold RPC wrapper and the activation seeding.

DEFAULT_PALLET_TYPES = (("full", 1.0), ("euro", 1.0), ("half", 0.5), ("quarter", 0.25))


def seed_fleet_defaults(tenant_id: str) -> None:
    """Idempotent fleet-activation seeding (operator console, D2 + D4): the four
    standard load-unit types, and a 'Main' branch when the tenant has none — a
    single-branch tenant sees 'Main' and never thinks about branches."""
    db().table("pallet_types").upsert(
        [{"tenant_id": tenant_id, "name": n, "space_factor": f}
         for n, f in DEFAULT_PALLET_TYPES],
        on_conflict="tenant_id,name", ignore_duplicates=True,
    ).execute()
    have = db().table("branches").select("id").eq("tenant_id", tenant_id).limit(1).execute()
    if not have.data:
        db().table("branches").insert({"tenant_id": tenant_id, "name": "Main"}).execute()


def list_branches(tenant_id: str, client: Client | None = None) -> list[dict]:
    res = (
        (client or db()).table("branches").select("*")
        .eq("tenant_id", tenant_id).order("created_at").execute()
    )
    return res.data or []


def add_branch(tenant_id: str, name: str, postcode: str | None = None,
               client: Client | None = None) -> None:
    (client or db()).table("branches").insert(
        {"tenant_id": tenant_id, "name": name, "postcode": postcode}).execute()


def update_branch(branch_id: str, tenant_id: str, fields: dict,
                  client: Client | None = None) -> None:
    """Edit one branch (name/postcode/address), tenant-scoped. The postcode is
    the branch's route origin on the fleet route map (route-planning D3)."""
    (client or db()).table("branches").update(fields) \
        .eq("id", branch_id).eq("tenant_id", tenant_id).execute()


def delete_branch(branch_id: str, tenant_id: str, client: Client | None = None) -> None:
    """Remove a branch — its fleet resources stay (branch_id nulls to 'unassigned') and
    any mailbox default pointing at it clears, both via ON DELETE SET NULL."""
    (client or db()).table("branches").delete() \
        .eq("id", branch_id).eq("tenant_id", tenant_id).execute()


def list_vehicles(tenant_id: str, client: Client | None = None,
                  active_only: bool = False) -> list[dict]:
    q = (client or db()).table("vehicles").select("*").eq("tenant_id", tenant_id)
    if active_only:
        q = q.eq("active", True)
    return (q.order("created_at").execute()).data or []


def add_vehicle(tenant_id: str, name: str, capacity_spaces: float,
                max_payload_kg: float | None = None, branch_id: str | None = None,
                registration: str | None = None,
                client: Client | None = None) -> None:
    (client or db()).table("vehicles").insert({
        "tenant_id": tenant_id, "name": name, "capacity_spaces": capacity_spaces,
        "max_payload_kg": max_payload_kg, "branch_id": branch_id,
        "registration": registration,
    }).execute()


def get_vehicle(vehicle_id: str, tenant_id: str, client: Client | None = None) -> dict | None:
    res = (
        (client or db()).table("vehicles").select("*")
        .eq("id", vehicle_id).eq("tenant_id", tenant_id).limit(1).execute()
    )
    return res.data[0] if res.data else None


def update_vehicle(vehicle_id: str, tenant_id: str, fields: dict,
                   client: Client | None = None) -> None:
    """Edit one fleet resource (name/capacity/payload/branch/active), tenant-scoped.
    Deactivating only blocks NEW holds — existing bookings keep their fleet resource."""
    (client or db()).table("vehicles").update(fields) \
        .eq("id", vehicle_id).eq("tenant_id", tenant_id).execute()


def delete_vehicle(vehicle_id: str, tenant_id: str, client: Client | None = None) -> None:
    """Remove a fleet resource. Its bookings keep their date with vehicle_id nulled
    (ON DELETE SET NULL) — the Diary shows them 'unassigned' for a human."""
    (client or db()).table("vehicles").delete() \
        .eq("id", vehicle_id).eq("tenant_id", tenant_id).execute()


def list_pallet_types(tenant_id: str, client: Client | None = None) -> list[dict]:
    res = (
        (client or db()).table("pallet_types").select("*")
        .eq("tenant_id", tenant_id)
        .order("space_factor", desc=True).order("name").execute()
    )
    return res.data or []


def list_vehicle_day_bookings(tenant_id: str, vehicle_id: str, date_iso: str,
                              client: Client | None = None) -> list[dict]:
    """One van's capacity-consuming bookings for one date — the manifest's rows
    (route-planning slice 1), oldest first to match the board's chip order."""
    res = (
        (client or db()).table("bookings").select("*")
        .eq("tenant_id", tenant_id).eq("vehicle_id", vehicle_id)
        .eq("booking_date", date_iso)
        .in_("status", ["proposed", "confirmed"])
        .order("created_at")
        .execute()
    )
    return res.data or []


def set_booking_routing(booking_id: str, tenant_id: str,
                        collection_postcode: str | None = None,
                        delivery_postcode: str | None = None,
                        reference: str | None = None,
                        client: Client | None = None) -> None:
    """Stamp the routing capture (route-planning D2) onto a fresh hold — a
    separate update so hold_fleet_booking, the ONE atomic capacity decision,
    stays untouched: these fields carry location, never capacity."""
    fields = {k: v for k, v in {
        "collection_postcode": collection_postcode,
        "delivery_postcode": delivery_postcode,
        "reference": reference,
    }.items() if v}
    if fields:
        (client or db()).table("bookings").update(fields) \
            .eq("id", booking_id).eq("tenant_id", tenant_id).execute()


# ── transport planner (docs/modules/module-transport-planner.md) ────────────────────
# postcodes cache + plans CRUD + the atomic apply RPC. Plans and postcodes are
# only ever touched server-side (the planner persona and the board routes), so
# everything here runs on the service role; plans reads from the dashboard pass
# the ctx client so RLS stays the second wall.

def list_date_bookings(tenant_id: str, date_iso: str, client: Client | None = None) -> list[dict]:
    """One date's capacity-consuming bookings, full rows — the planner's working
    set (get_day_state) and the plan-diff view's before/after input."""
    res = (
        (client or db()).table("bookings").select("*")
        .eq("tenant_id", tenant_id).eq("booking_date", date_iso)
        .in_("status", ["proposed", "confirmed"])
        .order("created_at")
        .execute()
    )
    return res.data or []


def get_postcodes(codes: list[str]) -> list[dict]:
    """Cached postcode geometry (lat/lng null = a cached postcodes.io miss)."""
    if not codes:
        return []
    res = db().table("postcodes").select("*").in_("postcode", list(codes)).execute()
    return res.data or []


def put_postcodes(rows: list[dict]) -> None:
    """Cache postcode lookups forever — hits and misses alike (D4)."""
    if rows:
        db().table("postcodes").upsert(rows, on_conflict="postcode").execute()


def insert_plan(tenant_id: str, plan_date_iso: str, summary: str, moves: list[dict]) -> dict:
    """One proposed plan (status='proposed' by default). The moves stored are
    the ENGINE's, validated upstream — never raw model output."""
    res = db().table("plans").insert({
        "tenant_id": tenant_id, "plan_date": plan_date_iso,
        "summary": summary, "moves": moves,
    }).execute()
    return res.data[0]


def list_plans(tenant_id: str, date_from_iso: str | None = None,
               status: str | None = None, client: Client | None = None) -> list[dict]:
    q = (client or db()).table("plans").select("*").eq("tenant_id", tenant_id)
    if date_from_iso:
        q = q.gte("plan_date", date_from_iso)
    if status:
        q = q.eq("status", status)
    return (q.order("created_at", desc=True).execute()).data or []


def get_plan(plan_id: str, tenant_id: str, client: Client | None = None) -> dict | None:
    res = (
        (client or db()).table("plans").select("*")
        .eq("id", plan_id).eq("tenant_id", tenant_id).limit(1).execute()
    )
    return res.data[0] if res.data else None


def discard_plan(plan_id: str, tenant_id: str, client: Client | None = None) -> None:
    """Resolve a proposed plan without applying it. Only proposed rows move —
    an applied plan is history, never rewritten."""
    (client or db()).table("plans").update(
        {"status": "discarded", "resolved_at": datetime.now(timezone.utc).isoformat()}
    ).eq("id", plan_id).eq("tenant_id", tenant_id).eq("status", "proposed").execute()


def discard_plans_for_date(tenant_id: str, plan_date_iso: str, client: Client | None = None) -> None:
    """Resolve every still-proposed plan for one date — the Replan button
    supersedes (one active plan per day); applied/discarded history untouched."""
    (client or db()).table("plans").update(
        {"status": "discarded", "resolved_at": datetime.now(timezone.utc).isoformat()}
    ).eq("tenant_id", tenant_id).eq("plan_date", plan_date_iso).eq("status", "proposed").execute()


def apply_plan(plan_id: str, tenant_id: str) -> tuple[dict | None, str | None]:
    """Apply one proposed plan through the atomic SQL function (D6): every move
    in ONE transaction, the whole day re-validated, any failure rolls the lot
    back. Returns (applied plan row, None) or (None, the loud human-readable
    reason) — the RAISE message names the blocking booking/resource."""
    try:
        res = db().rpc("apply_plan", {"p_plan_id": plan_id, "p_tenant_id": tenant_id}).execute()
    except Exception as e:
        # PostgREST wraps our RAISE EXCEPTION text — surface it, drop the wrapper.
        msg = getattr(e, "message", None) or str(e)
        return None, msg
    return (res.data[0] if res.data else None), None


def get_mailbox_default_branch(mailbox_id: str, client: Client | None = None) -> str | None:
    """The receiving mailbox's default branch (D4 leg 2: orders@leeds books
    Leeds vans). Lean single-column read — the drafting path calls this."""
    res = (
        (client or db()).table("mailbox_config").select("default_branch_id")
        .eq("mailbox_id", mailbox_id).limit(1).execute()
    )
    return res.data[0].get("default_branch_id") if res.data else None


def hold_fleet_booking(
    tenant_id: str,
    booking_date_iso: str,
    summary: str,
    spaces: float,
    weight_kg: float | None = None,
    branch_id: str | None = None,
    details: dict | None = None,
    contact: str | None = None,
    source: str = "email",
    thread_id: str | None = None,
    status: str = "proposed",
) -> dict | None:
    """Atomic best-fit fleet resource assignment + insert via the SQL function — the
    fleet sibling of hold_booking_if_capacity and the ONLY place a fleet resource is
    chosen, so racing holds can never overload a van. Returns the new bookings
    row (vehicle_id set), or None when no single active fleet resource fits (D1: one
    fleet resource per order; the agent offers alternatives, never a split)."""
    res = db().rpc(
        "hold_fleet_booking",
        {
            "p_tenant_id": tenant_id,
            "p_booking_date": booking_date_iso,
            "p_summary": summary,
            "p_spaces": spaces,
            "p_weight_kg": weight_kg,
            "p_branch_id": branch_id,
            "p_details": details,
            "p_contact": contact,
            "p_source": source,
            "p_thread_id": thread_id,
            "p_status": status,
        },
    ).execute()
    return res.data[0] if res.data else None


# ── Assets OS (docs/os/assets.md §5) — units as the durable supply object ──────
# asset_units is the `vehicles` of Assets OS: the unit CRUD below follows the fleet-resource-
# CRUD twin, and hold_unit_booking is a THIN wrapper over the SHIPPED stay engine
# (insert_stay_booking + scheduling.service_availability) — a NEW scoping key
# (resource_type = the unit id, capacity 1 for a named unit), never new capacity
# math. The deposit / condition_report / damage_case tables exist in the schema but
# their engines are Phases B/C — those db fns are deliberately deferred (§5, §13).

# A couple of example units on activation so the diary works out of the box — the
# seed_fleet_defaults analogue (§9). Named units (their own resource_type = pool key
# NULL means "book against my own id"); a lettings tenant sees two cottages, edits to taste.
DEFAULT_ASSET_UNITS = (
    {"name": "Unit A", "category": "unit"},
    {"name": "Unit B", "category": "unit"},
)


def seed_assets_defaults(tenant_id: str) -> None:
    """Idempotent assets-activation seeding (§9): a couple of example named units when
    the tenant has none, so the registry + board aren't empty on first open. Mirrors
    seed_fleet_defaults (load-unit types + a Main branch)."""
    have = db().table("asset_units").select("id").eq("tenant_id", tenant_id).limit(1).execute()
    if not have.data:
        db().table("asset_units").insert(
            [{"tenant_id": tenant_id, **u} for u in DEFAULT_ASSET_UNITS]
        ).execute()


def asset_list_units(tenant_id: str, *, active=None, client: Client | None = None) -> list[dict]:
    """The unit registry (the list_vehicles twin). active=True → only bookable units."""
    q = (client or db()).table("asset_units").select("*").eq("tenant_id", tenant_id)
    if active is not None:
        q = q.eq("active", active)
    return (q.order("created_at").execute()).data or []


def asset_get_unit(unit_id: str, tenant_id: str, client: Client | None = None) -> dict | None:
    res = (
        (client or db()).table("asset_units").select("*")
        .eq("id", unit_id).eq("tenant_id", tenant_id).limit(1).execute()
    )
    return res.data[0] if res.data else None


def asset_create_unit(tenant_id: str, unit: dict, client: Client | None = None) -> dict:
    """Add a unit (the add_vehicle twin). `unit` carries name + the optional rate card /
    resource_type / deposit / condition / service fields; unknown keys are the caller's
    responsibility (the route cleans the form)."""
    row = {"tenant_id": tenant_id, **unit}
    res = (client or db()).table("asset_units").insert(row).execute()
    return res.data[0]


def asset_update_unit(unit_id: str, tenant_id: str, fields: dict,
                      client: Client | None = None) -> dict:
    """Edit one unit (the update_vehicle twin), tenant-scoped. Deactivating (active=False)
    only blocks NEW holds — existing bookings keep their unit_id."""
    res = (
        (client or db()).table("asset_units").update({**fields,
            "updated_at": datetime.now(timezone.utc).isoformat()})
        .eq("id", unit_id).eq("tenant_id", tenant_id).execute()
    )
    return res.data[0] if res.data else {}


def asset_units_for_pool(tenant_id: str, resource_type: str | None,
                         client: Client | None = None) -> list[dict]:
    """Active units in one fungible pool (resource_type) — the best-fit assignment set for a
    pool hire (§1, §12a), the asset_units analogue of a fleet resource pool. resource_type None
    returns the named units that book against their own id."""
    q = ((client or db()).table("asset_units").select("*")
         .eq("tenant_id", tenant_id).eq("active", True))
    q = q.eq("resource_type", resource_type) if resource_type else q.is_("resource_type", "null")
    return (q.order("created_at").execute()).data or []


def asset_list_unit_bookings(tenant_id: str, unit_id: str, date_from_iso: str,
                             date_to_iso: str, client: Client | None = None) -> list[dict]:
    """One unit's capacity-consuming (proposed/confirmed) hires in a date window — the input
    to the unit-scoped availability check. The grid columns carry the stay span so
    concurrent-occupancy counts the hire on every day it covers."""
    res = (
        (client or db()).table("bookings")
        .select("booking_date,window,status,start_block,length_blocks,quantity,"
                "unit_id,hire_state,resource_type")
        .eq("tenant_id", tenant_id).eq("unit_id", unit_id)
        .in_("status", ["proposed", "confirmed"])
        .gte("booking_date", date_from_iso).lte("booking_date", date_to_iso)
        .execute()
    )
    return res.data or []


def hold_unit_booking(tenant_id: str, unit_id: str | None, start_block: int, length_blocks: int,
                      summary: str, *, deposit_amount=None, contact: str | None = None,
                      details: dict | None = None, thread_id: str | None = None,
                      source: str = "email", status: str = "proposed",
                      booking_date_iso: str | None = None,
                      client: Client | None = None) -> dict | None:
    """Reserve a hire against a specific unit — a THIN wrapper over the SHIPPED stay engine,
    NO new capacity math (§1, §12a).

    NAMED unit (unit_id set — individually named assets): availability of unit U = no active hire
    overlaps this span on U. We reuse the SHIPPED concurrent-occupancy primitive
    (slotgrid.stay_fits — the same math stay_offer/insert_stay_booking's capacity check runs
    on) scoped to U's OWN hires with capacity=1: if the requested span doesn't fit on U,
    return None (the caller surfaces 'not available'); otherwise insert_stay_booking with
    unit_id + resource_type=unit_id + hire_state='held'. resource_type=unit_id is the new
    SCOPING KEY — the shipped math counts only THIS unit's hires — so two enquiries for the
    same unit-span cannot both hold. (The atomic hardening — an hold_unit_if_capacity RPC, the
    sibling of hold_stay_if_capacity — is the same "too cautious, never wrong" pre-check the
    whole diary runs on today; deferred to the RPC pass, like insert_stay_booking's.)

    POOL unit (unit_id None — 12 identical drills): the pool is capacity N; the hire consumes
    1, unit_id is assigned best-fit at handover exactly like hold_fleet_booking picks a
    fleet resource. Callers scope by the pool's resource_type on the booking; this wrapper places the
    hold with hire_state='held' and no unit_id (assigned later).
    """
    from datetime import date as _date, timedelta as _td
    from app import slotgrid

    length = int(length_blocks)
    # Same convention as the shipped stay path: start_block is a WITHIN-DAY block and
    # booking_date is the arrival day; the absolute span start = abs_block(day, start_block).
    if not booking_date_iso:
        raise ValueError("hold_unit_booking needs booking_date_iso (the arrival day)")
    day = _date.fromisoformat(booking_date_iso)
    lo = slotgrid.abs_block(day, int(start_block))

    if unit_id is not None:
        # Named-unit availability: the SHIPPED concurrent-occupancy check, scoped to U's own
        # hires (capacity 1). A pre-flight — the atomic RPC is the later hardening.
        window_end = (day + _td(days=length // slotgrid.BLOCKS_PER_DAY + 1)).isoformat()
        booked = asset_list_unit_bookings(
            tenant_id, unit_id, booking_date_iso, window_end, client=client)
        spans = [s for s in (slotgrid.stay_span_q(b) for b in booked) if s]
        if not slotgrid.stay_fits(spans, lo, length, capacity=1):
            return None

    row = insert_stay_booking(
        tenant_id, booking_date_iso, int(start_block), length, summary,
        details=details, contact=contact, source=source, thread_id=thread_id, status=status,
        deposit_amount=deposit_amount,
        resource_type=(unit_id if unit_id is not None else None), client=client)
    fields = {"hire_state": "held"}
    if unit_id is not None:
        fields["unit_id"] = unit_id
    (client or db()).table("bookings").update(fields).eq("id", row["id"]).execute()
    return {**row, **fields}


# ── maintenance / service cadence (Assets OS Phase D — docs/os/assets.md §6/§11) ─
# The maintenance TWIN of the FSM recurring-service cadence
# (complete_job_and_schedule_service / _add_months): a completed service advances
# next_service_due by service_interval_days (by DAYS, not months — kit is serviced on
# an interval, not an annual servicing cadence). asset_units_due_service is the
# list_jobs_due_service sibling — the units the maintenance lane + the owner nudge
# surface: a service falling inside the lead window, OR condition dropped below floor.

def asset_units_due_service(tenant_id: str, within_days: int = 14,
                            client: Client | None = None) -> list[dict]:
    """Active units NEEDING ATTENTION (§6/§11) — the maintenance-lane / owner-nudge set:
      * next_service_due on/before today+within_days (a service falling due soon or overdue), OR
      * condition_rating < condition_floor (run down below its floor → wants a look).
    Newest-due first. The asset_units sibling of list_jobs_due_service (which keys on a
    completed job's service_due_date); here the durable UNIT carries its own cadence."""
    cutoff = (date.today() + timedelta(days=int(within_days))).isoformat()
    rows = (((client or db()).table("asset_units").select("*")
             .eq("tenant_id", tenant_id).eq("active", True).execute()).data) or []
    out = []
    for u in rows:
        due = u.get("next_service_due")
        due_soon = bool(due) and str(due)[:10] <= cutoff
        rating = u.get("condition_rating")
        floor = u.get("condition_floor")
        below_floor = (rating is not None and floor is not None
                       and float(rating) < float(floor))
        if due_soon or below_floor:
            out.append({**u, "_due_soon": due_soon, "_below_floor": below_floor})
    # Soonest service first (None dues last), then name — a stable lane order.
    out.sort(key=lambda u: (str(u.get("next_service_due") or "9999-12-31")[:10],
                            u.get("name") or ""))
    return out


def complete_unit_service(unit_id: str, tenant_id: str, *, serviced_on: str | None = None,
                          lift_condition: bool = True,
                          client: Client | None = None) -> dict:
    """Mark a unit serviced and advance its cadence — the FSM
    complete_job_and_schedule_service twin, by DAYS (service_interval_days) not months.
    Advances next_service_due = serviced_on + interval (when an interval is set; else the
    date is cleared, a one-off service), stamps a service note into custom_data, and — by
    default — lifts condition_rating back toward 5.0 (a fresh service restores condition).
    Tenant-scoped (IDOR-safe). Returns the updated unit."""
    c = client or db()
    unit = asset_get_unit(unit_id, tenant_id, client=c)
    if not unit:
        return {}
    on = date.fromisoformat(serviced_on[:10]) if serviced_on else date.today()
    interval = unit.get("service_interval_days")
    fields: dict = {"updated_at": datetime.now(timezone.utc).isoformat()}
    if interval:
        fields["next_service_due"] = (on + timedelta(days=int(interval))).isoformat()
    else:
        fields["next_service_due"] = None    # a one-off service clears the due date
    if lift_condition:
        # A service restores the unit toward pristine (never above 5.0); only ever lifts.
        current = unit.get("condition_rating")
        target = 5.0
        if current is None or float(current) < target:
            fields["condition_rating"] = target
    # A short service note in custom_data (the seam column) so the trajectory carries it —
    # merged, never clobbering _shot_list or other keys.
    cd = dict(unit.get("custom_data") or {})
    history = list(cd.get("_service_history") or [])
    history.append(on.isoformat())
    cd["_service_history"] = history[-12:]      # keep the last dozen services
    cd["_last_serviced_on"] = on.isoformat()
    fields["custom_data"] = cd
    res = (c.table("asset_units").update(fields)
           .eq("id", unit_id).eq("tenant_id", tenant_id).execute())
    return res.data[0] if res.data else {**unit, **fields}


def stamp_unit_service_nudged(unit_id: str, tenant_id: str,
                              client: Client | None = None) -> None:
    """Dedupe the owner service-due nudge (custom_data._service_nudged_at) so the daily
    maintenance sweep nudges a unit at most once per service cycle — the
    mark_service_reminded analogue, without a migration (rides custom_data). Cleared when
    the unit is next serviced (complete_unit_service rewrites custom_data)."""
    c = client or db()
    unit = asset_get_unit(unit_id, tenant_id, client=c)
    cd = dict((unit or {}).get("custom_data") or {})
    cd["_service_nudged_at"] = datetime.now(timezone.utc).isoformat()
    (c.table("asset_units").update(
        {"custom_data": cd, "updated_at": datetime.now(timezone.utc).isoformat()})
     .eq("id", unit_id).eq("tenant_id", tenant_id).execute())


def assign_pool_unit(booking_id: str, tenant_id: str,
                     client: Client | None = None) -> dict | None:
    """Assign a specific unit to a FUNGIBLE-POOL hire at HANDOVER (§1/§12a) — the
    best_fit / hold_fleet_booking pattern, picking an asset_unit instead of a fleet resource.
    A pool hire holds with unit_id NULL (12 identical drills); at collection we pin ONE
    free unit so the condition capture + damage diff key on that specific asset.

    Free = an active unit in the hire's pool (bookings.resource_type) with NO OTHER active
    hire currently assigned to it. Deterministic pick (by id) — the fleet best_fit choice
    made concrete. Returns the assigned unit, or None when the booking is already assigned
    or the pool has no free unit. Tenant-scoped (IDOR-safe)."""
    c = client or db()
    booking = get_booking(booking_id, tenant_id, client=c)
    if not booking:
        return None
    if booking.get("unit_id"):
        # Already pinned (named hire, or assigned earlier) — idempotent no-op.
        return asset_get_unit(booking["unit_id"], tenant_id, client=c)
    pool = booking.get("resource_type")
    candidates = asset_units_for_pool(tenant_id, pool, client=c)
    if not candidates:
        return None
    # Units already tied up by another active hire are unavailable.
    taken = {b.get("unit_id") for b in (
        (c.table("bookings").select("unit_id")
         .eq("tenant_id", tenant_id).in_("status", ["proposed", "confirmed"])
         .not_.is_("unit_id", "null").execute()).data or [])}
    free = sorted((u for u in candidates if u["id"] not in taken),
                  key=lambda u: str(u["id"]))
    if not free:
        return None
    chosen = free[0]
    (c.table("bookings").update(
        {"unit_id": chosen["id"], "updated_at": datetime.now(timezone.utc).isoformat()})
     .eq("id", booking_id).eq("tenant_id", tenant_id).execute())
    return chosen


# ── unit deposits (over the reused Stripe rail — docs/os/assets.md §4/§5) ──────
# One row per hire's deposit; the lifecycle (pending→held/paid→refunded/deducted/
# released/failed) is driven by src/app/assets_deposits.py (the Stripe wrappers) + the
# Stripe webhook. This layer is the tenant-scoped, IDOR-safe persistence only.
_DEPOSIT_STATUSES = {"pending", "held", "paid", "refunded", "deducted", "released", "failed"}


def upsert_deposit(tenant_id: str, booking_id: str, unit_id: str | None, amount,
                   *, currency: str = "GBP", method: str = "stripe_hold",
                   client: Client | None = None) -> dict:
    """Create-or-update the ONE deposit row for a booking (uq_unit_deposits_booking).
    Idempotent: re-minting a link for the same booking overwrites amount/method/unit
    and resets status to 'pending' (a fresh intent supersedes an unresolved one).
    Tenant-stamped; the unique index keeps it one-per-booking."""
    c = client or db()
    existing = get_deposit_for_booking(tenant_id, booking_id, client=c)
    payload = {
        "tenant_id": tenant_id, "booking_id": booking_id, "unit_id": unit_id,
        "amount": amount, "currency": currency, "method": method,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if existing:
        res = (c.table("unit_deposits").update({**payload, "status": "pending"})
               .eq("id", existing["id"]).eq("tenant_id", tenant_id).execute())
        return (res.data or [{**existing, **payload, "status": "pending"}])[0]
    res = c.table("unit_deposits").insert({**payload, "status": "pending"}).execute()
    return res.data[0]


def set_deposit_status(deposit_id: str, tenant_id: str, status: str, *,
                       external_ref: str | None = None, deducted_amount=None,
                       damage_case_id: str | None = None,
                       client: Client | None = None) -> dict:
    """Flip a deposit's lifecycle status (the webhook + the refund/deduct wrappers write
    here). Tenant-scoped (IDOR-safe). Only the passed optionals are written, so a webhook
    that only knows the PaymentIntent id doesn't clobber a deduction figure set elsewhere."""
    if status not in _DEPOSIT_STATUSES:
        raise ValueError(f"unknown deposit status {status!r}")
    fields: dict = {"status": status, "updated_at": datetime.now(timezone.utc).isoformat()}
    if external_ref is not None:
        fields["external_ref"] = external_ref
    if deducted_amount is not None:
        fields["deducted_amount"] = deducted_amount
    if damage_case_id is not None:
        fields["damage_case_id"] = damage_case_id
    res = ((client or db()).table("unit_deposits").update(fields)
           .eq("id", deposit_id).eq("tenant_id", tenant_id).execute())
    return res.data[0] if res.data else {}


def set_deposit_status_by_ref(tenant_id: str, external_ref: str, status: str, *,
                              deducted_amount=None, client: Client | None = None) -> dict:
    """Flip a deposit keyed by its Stripe external_ref (PaymentIntent id) — the webhook
    path, which knows the intent id from metadata but not our row id. Tenant-scoped."""
    if status not in _DEPOSIT_STATUSES:
        raise ValueError(f"unknown deposit status {status!r}")
    fields: dict = {"status": status, "updated_at": datetime.now(timezone.utc).isoformat()}
    if deducted_amount is not None:
        fields["deducted_amount"] = deducted_amount
    res = ((client or db()).table("unit_deposits").update(fields)
           .eq("tenant_id", tenant_id).eq("external_ref", external_ref).execute())
    return res.data[0] if res.data else {}


def get_deposit_for_booking(tenant_id: str, booking_id: str,
                            client: Client | None = None) -> dict | None:
    """The one deposit row for a booking (or None) — the board chip + the route reads."""
    res = ((client or db()).table("unit_deposits").select("*")
           .eq("tenant_id", tenant_id).eq("booking_id", booking_id).limit(1).execute())
    return res.data[0] if res.data else None


def get_deposit(deposit_id: str, tenant_id: str, client: Client | None = None) -> dict | None:
    res = ((client or db()).table("unit_deposits").select("*")
           .eq("id", deposit_id).eq("tenant_id", tenant_id).limit(1).execute())
    return res.data[0] if res.data else None


def deposits_for_bookings(tenant_id: str, booking_ids: list, *,
                          client: Client | None = None) -> dict:
    """Deposit rows keyed by booking_id for a set of bookings — the board's chip fetch
    (one query for the whole lane rather than N). Empty ids → {}."""
    ids = [b for b in booking_ids if b]
    if not ids:
        return {}
    res = ((client or db()).table("unit_deposits").select("*")
           .eq("tenant_id", tenant_id).in_("booking_id", ids).execute())
    return {r["booking_id"]: r for r in (res.data or [])}


def hires_arriving_on(tenant_id: str, date_iso: str,
                      client: Client | None = None) -> list[dict]:
    """Active hires whose arrival day is `date_iso` — the handover-reminder set (units going
    out that day). A hire is a booking with hire_state set (or a unit_id); proposed/confirmed
    only (cancelled holds don't collect). The sweep filters paid vs unpaid via the deposit."""
    res = ((client or db()).table("bookings").select("*")
           .eq("tenant_id", tenant_id).eq("booking_date", date_iso)
           .in_("status", ["proposed", "confirmed"])
           .not_.is_("hire_state", "null").execute())
    return res.data or []


def release_hire(booking_id: str, tenant_id: str, client: Client | None = None) -> None:
    """Cancel a stale unpaid hire and clear its hire membership — the deposit lifecycle sweep's
    release (§6). Sets status='cancelled' + hire_state=NULL so the unit frees up and the row
    drops off the board. Tenant-scoped (IDOR-safe)."""
    (client or db()).table("bookings").update(
        {"status": "cancelled", "hire_state": None,
         "updated_at": datetime.now(timezone.utc).isoformat()}
    ).eq("id", booking_id).eq("tenant_id", tenant_id).execute()


def stamp_handover_reminded(booking_id: str, tenant_id: str,
                            client: Client | None = None) -> None:
    """Stamp details.handover_reminded_at so the daily handover sweep chases/reminds a hire at
    most once (dedupe without a new column — no migration in Phase B). Merges into the existing
    details jsonb so it never clobbers the hire's captured fields."""
    c = client or db()
    row = get_booking(booking_id, tenant_id, client=c)
    details = dict((row or {}).get("details") or {})
    details["handover_reminded_at"] = datetime.now(timezone.utc).isoformat()
    c.table("bookings").update(
        {"details": details, "updated_at": datetime.now(timezone.utc).isoformat()}
    ).eq("id", booking_id).eq("tenant_id", tenant_id).execute()


def deposits_stale_unpaid(tenant_id: str, cutoff_iso: str,
                          client: Client | None = None) -> list[dict]:
    """UNPAID deposits (status pending|failed) created before the cutoff — the lifecycle
    sweep's release set. Only rows on a still-active hold get released (the sweep checks the
    booking); the query returns the deposit + its booking_id for that follow-up."""
    res = ((client or db()).table("unit_deposits").select("*")
           .eq("tenant_id", tenant_id).in_("status", ["pending", "failed"])
           .lt("created_at", cutoff_iso).execute())
    return res.data or []


# ── condition reports + damage cases (Assets OS Phase C — docs/os/assets.md §5) ─
# The photo-diff spine: a condition_report is ONE snapshot at handover ('out') or
# return ('in'), its photo PATHS in photo_urls (Supabase Storage). On an 'in' report
# the diff pipeline (src/app/assets_condition.py) pairs it with latest_out_report and,
# past the confidence/severity gate, opens a damage_case — the evidence bundle a
# deposit deduction stands on. Every deduction is human-approved; no baseline → no
# claim (§14). Tenant-scoped, IDOR-safe.

def insert_condition_report(tenant_id: str, unit_id: str | None, booking_id: str | None,
                            phase: str, *, rating=None, photo_urls: list | None = None,
                            notes: str | None = None, captured_by: str | None = None,
                            storage_error: bool = False,
                            client: Client | None = None) -> dict:
    """Persist one condition snapshot. phase is 'out' (handover baseline) or 'in'
    (return). photo_urls is the ordered list of Storage PATHS (pairing is by ordinal).
    storage_error flags a capture where the photos couldn't be stored — the report is
    still recorded (never lose the caretaker's submission) but carries no usable photos."""
    if phase not in ("out", "in"):
        raise ValueError(f"unknown condition phase {phase!r}")
    row = {
        "tenant_id": tenant_id, "unit_id": unit_id, "booking_id": booking_id,
        "phase": phase, "photo_urls": photo_urls or [],
    }
    if rating is not None:
        row["rating"] = rating
    if notes is not None:
        row["notes"] = notes
    if captured_by is not None:
        row["captured_by"] = captured_by
    if storage_error:
        # Phase A's schema has no dedicated flag column — stamp it into notes so the
        # report row itself records that its photos couldn't be stored (the capture
        # route also surfaces it live to the caretaker). Never lose the submission.
        prefix = "[photo storage unavailable] "
        row["notes"] = prefix + (row.get("notes") or "")
    res = (client or db()).table("condition_reports").insert(row).execute()
    return res.data[0]


def list_condition_reports(tenant_id: str, *, unit_id: str | None = None,
                           booking_id: str | None = None,
                           client: Client | None = None) -> list[dict]:
    """Condition snapshots for a unit or a booking (the trajectory / evidence read),
    oldest first. Tenant-scoped."""
    q = ((client or db()).table("condition_reports").select("*")
         .eq("tenant_id", tenant_id))
    if unit_id is not None:
        q = q.eq("unit_id", unit_id)
    if booking_id is not None:
        q = q.eq("booking_id", booking_id)
    return (q.order("captured_at").execute()).data or []


def latest_out_report(tenant_id: str, booking_id: str,
                      client: Client | None = None) -> dict | None:
    """The 'out' (handover) baseline for a booking — the BEFORE the diff pipeline
    compares the return photos against. None when no handover was captured (no
    baseline → no claim, §14). Most-recent out report wins (a re-capture supersedes)."""
    res = ((client or db()).table("condition_reports").select("*")
           .eq("tenant_id", tenant_id).eq("booking_id", booking_id).eq("phase", "out")
           .order("captured_at", desc=True).limit(1).execute())
    return res.data[0] if res.data else None


def get_condition_report(report_id: str, tenant_id: str,
                         client: Client | None = None) -> dict | None:
    res = ((client or db()).table("condition_reports").select("*")
           .eq("id", report_id).eq("tenant_id", tenant_id).limit(1).execute())
    return res.data[0] if res.data else None


def insert_damage_case(tenant_id: str, unit_id: str | None, booking_id: str | None, *,
                       out_report_id: str | None, in_report_id: str | None,
                       diff_findings: list, diff_confidence, severity: str,
                       description: str | None = None, proposed_deduction=None,
                       status: str = "open", client: Client | None = None) -> dict:
    """Open a damage case — the output of the photo-diff (or a manual flag) and the
    evidence bundle a deduction stands on. diff_findings carries each finding with its
    out/in Storage path; severity is the max across findings. status='open' (needs the
    owner's decision) or a review flag lives in the diff_findings/description."""
    row = {
        "tenant_id": tenant_id, "unit_id": unit_id, "booking_id": booking_id,
        "out_report_id": out_report_id, "in_report_id": in_report_id,
        "diff_findings": diff_findings or [], "diff_confidence": diff_confidence,
        "severity": severity, "description": description,
        "proposed_deduction": proposed_deduction, "status": status,
    }
    res = (client or db()).table("damage_cases").insert(row).execute()
    return res.data[0]


def list_damage_cases(tenant_id: str, *, status: str | None = None,
                      client: Client | None = None) -> list[dict]:
    """The damage-case queue (open / review / resolved), newest first. Tenant-scoped."""
    q = ((client or db()).table("damage_cases").select("*").eq("tenant_id", tenant_id))
    if status is not None:
        q = q.eq("status", status)
    return (q.order("created_at", desc=True).execute()).data or []


def get_damage_case(case_id: str, tenant_id: str,
                    client: Client | None = None) -> dict | None:
    res = ((client or db()).table("damage_cases").select("*")
           .eq("id", case_id).eq("tenant_id", tenant_id).limit(1).execute())
    return res.data[0] if res.data else None


def resolve_damage_case(case_id: str, tenant_id: str, *, resolution: str,
                        resolved_amount=None, client: Client | None = None) -> dict:
    """Close a case with the owner's decision (deducted | refunded | waived). Human-
    approved always (§14) — the route calls the deposit action, this records the
    outcome. Tenant-scoped."""
    fields = {
        "status": "resolved", "resolution": resolution,
        "resolved_amount": resolved_amount,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    res = ((client or db()).table("damage_cases").update(fields)
           .eq("id", case_id).eq("tenant_id", tenant_id).execute())
    return res.data[0] if res.data else {}


def get_damage_case_for_booking(tenant_id: str, booking_id: str,
                                client: Client | None = None) -> dict | None:
    """The most recent damage case for a booking (the hire-detail read). None when the
    return was clean / not yet captured."""
    res = ((client or db()).table("damage_cases").select("*")
           .eq("tenant_id", tenant_id).eq("booking_id", booking_id)
           .order("created_at", desc=True).limit(1).execute())
    return res.data[0] if res.data else None


# ── capture magic-link token (§7/§11) — mirrors the track_token convention ─────
def set_booking_capture_token(booking_id: str, tenant_id: str, token: str,
                              client: Client | None = None) -> None:
    """Mint/store the tokenised capture link for a hire (the /capture/{token} magic-link,
    the track_token twin). Tenant-scoped."""
    (client or db()).table("bookings").update({"capture_token": token}) \
        .eq("id", booking_id).eq("tenant_id", tenant_id).execute()


def get_booking_by_capture_token(token: str, client: Client | None = None) -> dict | None:
    """PUBLIC capture-page lookup — service role, NO tenant scope (the token IS the
    capability, IDOR-safe: token → the one booking). Returns the booking or None."""
    res = ((client or db()).table("bookings").select("*")
           .eq("capture_token", token).limit(1).execute())
    return res.data[0] if res.data else None


# ── field service module (team members + job types — docs/modules/module-field-service.md) ─
# `engineers` / `job_types` CRUD for the Field Service settings page (RLS via the ctx
# client), plus the team member hold RPC wrapper and the activation seeding. The
# team member sibling of the fleet CRUD above; src/app/scheduling.py owns the capacity math.

# A sensible starter MENU on activation — durations only; the tenant sets skills and
# prices to taste. An unknown job in an email makes the specialist ask which service.
DEFAULT_JOB_TYPES = (
    {"name": "Service", "default_minutes": 60, "service_months": 12},  # recurs annually
    {"name": "Repair", "default_minutes": 90},
    {"name": "Installation", "default_minutes": 240},
    {"name": "Emergency callout", "default_minutes": 60, "emergency": True},
)


def seed_fsm_defaults(tenant_id: str) -> None:
    """Idempotent field-service-activation seeding: a starter menu of job types, and a
    'Main' branch when the tenant has none (a single-area tenant never thinks about
    areas). Team members are NOT seeded — the tenant adds their own people, like fleet resources."""
    # Uniform keys across every row: Supabase batch-upsert unions columns across the
    # batch, so a row omitting `emergency` would be sent emergency=null (the one row that
    # sets it pulls the not-null column into the batch). Fill the default explicitly.
    db().table("job_types").upsert(
        [{"tenant_id": tenant_id, "name": jt["name"],
          "default_minutes": jt["default_minutes"], "emergency": jt.get("emergency", False),
          "service_months": jt.get("service_months")}
         for jt in DEFAULT_JOB_TYPES],
        on_conflict="tenant_id,name", ignore_duplicates=True,
    ).execute()
    have = db().table("branches").select("id").eq("tenant_id", tenant_id).limit(1).execute()
    if not have.data:
        db().table("branches").insert({"tenant_id": tenant_id, "name": "Main"}).execute()
    # A default rate card so quoting works out of the box (D3); editable in the UI.
    have_rc = db().table("fsm_rate_cards").select("id").eq("tenant_id", tenant_id).limit(1).execute()
    if not have_rc.data:
        db().table("fsm_rate_cards").insert({
            "tenant_id": tenant_id, "name": "Standard",
            "callout_charge": 60, "hourly_rate": 55, "min_charge": 75, "vat_pct": 20,
        }).execute()


def list_engineers(tenant_id: str, client: Client | None = None,
                   active_only: bool = False) -> list[dict]:
    q = (client or db()).table("engineers").select("*").eq("tenant_id", tenant_id)
    if active_only:
        q = q.eq("active", True)
    return (q.order("created_at").execute()).data or []


def get_engineer(engineer_id: str, tenant_id: str, client: Client | None = None) -> dict | None:
    res = (
        (client or db()).table("engineers").select("*")
        .eq("id", engineer_id).eq("tenant_id", tenant_id).limit(1).execute()
    )
    return res.data[0] if res.data else None


def add_engineer(tenant_id: str, name: str, skills: list | None = None,
                 daily_minutes: int = 480, branch_id: str | None = None,
                 client: Client | None = None) -> None:
    (client or db()).table("engineers").insert({
        "tenant_id": tenant_id, "name": name, "skills": skills or [],
        "daily_minutes": daily_minutes, "branch_id": branch_id,
    }).execute()


def update_engineer(engineer_id: str, tenant_id: str, fields: dict,
                    client: Client | None = None) -> None:
    """Edit one team member (name/skills/daily_minutes/branch/active), tenant-scoped.
    Deactivating only blocks NEW holds — existing jobs keep their team member."""
    (client or db()).table("engineers").update(fields) \
        .eq("id", engineer_id).eq("tenant_id", tenant_id).execute()


def delete_engineer(engineer_id: str, tenant_id: str, client: Client | None = None) -> None:
    """Remove a team member. Their jobs keep their date with engineer_id nulled
    (ON DELETE SET NULL) — the board shows them 'unassigned' for a human."""
    (client or db()).table("engineers").delete() \
        .eq("id", engineer_id).eq("tenant_id", tenant_id).execute()


def list_job_types(tenant_id: str, client: Client | None = None) -> list[dict]:
    res = (
        (client or db()).table("job_types").select("*")
        .eq("tenant_id", tenant_id).order("name").execute()
    )
    return res.data or []


def add_job_type(tenant_id: str, name: str, default_minutes: int = 60,
                 required_skill: str | None = None, default_price: float | None = None,
                 emergency: bool = False, service_months: int | None = None,
                 client: Client | None = None) -> None:
    (client or db()).table("job_types").insert({
        "tenant_id": tenant_id, "name": name, "default_minutes": default_minutes,
        "required_skill": required_skill, "default_price": default_price,
        "emergency": emergency, "service_months": service_months,
    }).execute()


def update_job_type(job_type_id: str, tenant_id: str, fields: dict,
                    client: Client | None = None) -> None:
    (client or db()).table("job_types").update(fields) \
        .eq("id", job_type_id).eq("tenant_id", tenant_id).execute()


def delete_job_type(job_type_id: str, tenant_id: str, client: Client | None = None) -> None:
    (client or db()).table("job_types").delete() \
        .eq("id", job_type_id).eq("tenant_id", tenant_id).execute()


def hold_engineer_booking(
    tenant_id: str,
    booking_date_iso: str,
    summary: str,
    duration_min: int,
    required_skill: str | None = None,
    job_type: str | None = None,
    priority: str = "standard",
    branch_id: str | None = None,
    details: dict | None = None,
    contact: str | None = None,
    source: str = "email",
    thread_id: str | None = None,
    status: str = "proposed",
) -> dict | None:
    """Atomic best-fit team member assignment + insert via the SQL function — the
    field-service sibling of hold_fleet_booking and the ONLY place a team member is
    chosen, so racing holds can never overbook a day. Returns the new bookings row
    (engineer_id set), or None when no single active, suitably-skilled team member can fit
    the job that day (one team member per job; the agent offers alternatives)."""
    res = db().rpc(
        "hold_engineer_booking",
        {
            "p_tenant_id": tenant_id,
            "p_booking_date": booking_date_iso,
            "p_summary": summary,
            "p_duration_min": duration_min,
            "p_required_skill": required_skill,
            "p_job_type": job_type,
            "p_priority": priority,
            "p_branch_id": branch_id,
            "p_details": details,
            "p_contact": contact,
            "p_source": source,
            "p_thread_id": thread_id,
            "p_status": status,
        },
    ).execute()
    return res.data[0] if res.data else None


def link_bookings_to_log(booking_ids: list, email_log_id: str, tenant_id: str) -> None:
    """Stamp the draft row onto the holds placed while drafting it — the email⇄booking
    interconnect (Diary deep-links to the conversation; approve/skip resolves the hold).
    Service-role: runs in the pipeline right after insert_email_log."""
    if not booking_ids:
        return
    db().table("bookings").update(
        {"email_log_id": email_log_id, "updated_at": datetime.now(timezone.utc).isoformat()}
    ).eq("tenant_id", tenant_id).in_("id", list(booking_ids)).execute()


def set_booking_status(booking_id: str, status: str, tenant_id: str, client: Client | None = None) -> None:
    """One booking's status, tenant-scoped — the Diary's manual confirm/decline, and
    releasing a duplicate webhook delivery's hold."""
    (client or db()).table("bookings").update(
        {"status": status, "updated_at": datetime.now(timezone.utc).isoformat()}
    ).eq("id", booking_id).eq("tenant_id", tenant_id).execute()
    if status == "confirmed":
        # The CRM promise that said we'd confirm this booking is now fulfilled —
        # close it (same thread only, conservative). Best-effort: a promise-close
        # failure never breaks the confirm itself.
        try:
            row = get_booking(booking_id, tenant_id, client=client)
            if row:
                crm_close_promises_for_confirmed_booking(
                    tenant_id, thread_id=row.get("thread_id"),
                    email_log_id=row.get("email_log_id"),
                    booking_ref=row.get("booking_ref"), client=client)
        except Exception:
            log.warning("promise auto-close failed for booking %s", booking_id,
                        exc_info=True)


# ── Calendar Connect (v1.1) ─────────────────────────────────────────────────
def set_calendar_connected(connection_id: str) -> None:
    """Mark that this connection's token grant includes calendar scope (set at
    OAuth callback when Microsoft returns Calendars.* in the granted scopes)."""
    db().table("email_connections").update(
        {"calendar_connected_at": datetime.now(timezone.utc).isoformat()}
    ).eq("id", connection_id).execute()


def list_calendar_connections() -> list[dict]:
    """Connections with calendar scope (tokens decrypted) — the busy-sync poll set."""
    res = db().table("email_connections").select("*").not_.is_("calendar_connected_at", "null").execute()
    return [_decrypt_conn(r) for r in (res.data or [])]


def calendar_connected(tenant_id: str, client: Client | None = None) -> bool:
    res = (
        (client or db()).table("email_connections").select("calendar_connected_at")
        .eq("tenant_id", tenant_id).not_.is_("calendar_connected_at", "null")
        .limit(1).execute()
    )
    return bool(res.data)


def get_calendar_connection(tenant_id: str) -> dict | None:
    """The connection whose calendar the tenant-level diary pushes to / syncs
    from: the OLDEST calendar-connected mailbox (the diary stays tenant-level
    in enterprise v1, so one calendar represents the tenant). Tokens decrypted."""
    res = (
        db().table("email_connections").select("*")
        .eq("tenant_id", tenant_id).not_.is_("calendar_connected_at", "null")
        .order("created_at").limit(1).execute()
    )
    return _decrypt_conn(res.data[0]) if res.data else None


def list_calendar_linked_bookings(tenant_id: str) -> list[dict]:
    """Every booking carrying a calendar_event_id (any status) — the reconcile
    input: imported busy-blocks AND pushed bookings (so sync never re-imports
    BLAM8's own events)."""
    res = (
        db().table("bookings")
        .select("id,booking_date,window,status,source,calendar_event_id,details")
        .eq("tenant_id", tenant_id).not_.is_("calendar_event_id", "null").execute()
    )
    return res.data or []


def crm_set_contact_phone_if_empty(contact_id: str, tenant_id: str, phone: str,
                                   client: Client | None = None) -> None:
    """Fill the contact's first-class phone ONLY when it is empty — the
    extraction's built-in _phone target (crm_fields.BUILTIN_DEFINITIONS) must
    never overwrite a number a human already recorded. Atomic: the emptiness
    check is the update's own filter."""
    (client or db()).table("crm_contacts").update({"phone": phone}) \
        .eq("id", contact_id).eq("tenant_id", tenant_id) \
        .is_("phone", "null").execute()


def set_booking_calendar_missing(booking_id: str, tenant_id: str, missing: bool,
                                 client: Client | None = None) -> None:
    """Stamp (or clear) details.calendar_event_missing_at — the diary chip's
    signal that the Google/Outlook event behind a PUSHED booking was deleted
    by hand in the calendar (Julius 2026-09-03). The booking itself is never
    touched: BLAM8 stays the source of truth for its own bookings, the chip
    just surfaces the mismatch."""
    b = get_booking(booking_id, tenant_id, client=client)
    if not b:
        return
    details = dict(b.get("details") or {})
    if missing:
        details["calendar_event_missing_at"] = datetime.now(timezone.utc).isoformat()
    elif "calendar_event_missing_at" in details:
        details.pop("calendar_event_missing_at")
    else:
        return
    (client or db()).table("bookings").update({"details": details}) \
        .eq("id", booking_id).eq("tenant_id", tenant_id).execute()


def insert_calendar_block(tenant_id: str, event_id: str, summary: str,
                          date_iso: str, window: str) -> None:
    """A busy-block imported from the tenant's calendar. Deliberately NOT via the
    capacity RPC — their real calendar is reality, it lands even when the diary
    says full (the agent then simply has nothing left to offer)."""
    db().table("bookings").insert({
        "tenant_id": tenant_id, "summary": summary, "booking_date": date_iso,
        "window": window, "status": "confirmed", "source": "calendar",
        "calendar_event_id": event_id,
    }).execute()


def set_booking_calendar_event(booking_id: str, event_id: str | None, tenant_id: str) -> None:
    db().table("bookings").update({"calendar_event_id": event_id}) \
        .eq("id", booking_id).eq("tenant_id", tenant_id).execute()


def set_booking_blocks(booking_id: str, start_block: int, length_blocks: int,
                       tenant_id: str, client: Client | None = None) -> None:
    """Stamp a booking's unified slot-grid coordinates (docs/architecture/unified-slot-grid.md):
    which 15-min block it starts on and how many it spans. Dual-written alongside the
    legacy `window` on a new hold, and back-filled for history."""
    (client or db()).table("bookings").update(
        {"start_block": start_block, "length_blocks": length_blocks}
    ).eq("id", booking_id).eq("tenant_id", tenant_id).execute()


def move_booking_in_place(booking_id: str, tenant_id: str, new_date_iso: str,
                          start_block: int, length_blocks: int, window: str,
                          client: Client | None = None) -> dict | None:
    """Move one services-diary booking to a new date and grid position IN PLACE
    — reference, details, subjects, quote and calendar linkage all survive
    (the email-side move tool). Availability is the caller's check, as it is
    for a services hold. Returns the updated row, None when nothing matched."""
    res = (client or db()).table("bookings").update(
        {"booking_date": new_date_iso, "start_block": start_block,
         "length_blocks": length_blocks, "window": window}
    ).eq("id", booking_id).eq("tenant_id", tenant_id).execute()
    return res.data[0] if res.data else None


def list_bookings_to_push(tenant_id: str, email_log_id: str | None = None,
                          booking_id: str | None = None) -> list[dict]:
    """Confirmed bookings that should exist in the tenant's calendar but don't
    yet (no event id, and not themselves imported from the calendar)."""
    q = (
        db().table("bookings").select("*")
        .eq("tenant_id", tenant_id).eq("status", "confirmed")
        .neq("source", "calendar").is_("calendar_event_id", "null")
    )
    if email_log_id:
        q = q.eq("email_log_id", email_log_id)
    if booking_id:
        q = q.eq("id", booking_id)
    return q.execute().data or []


def get_booking(booking_id: str, tenant_id: str, client: Client | None = None) -> dict | None:
    res = (
        (client or db()).table("bookings").select("*")
        .eq("id", booking_id).eq("tenant_id", tenant_id).limit(1).execute()
    )
    return res.data[0] if res.data else None


def resolve_holds_for_log(email_log_id: str, status: str, tenant_id: str, client: Client | None = None) -> int:
    """Resolve the hold(s) linked to one draft (D4 — one approval drives both):
    'confirmed' on approve-&-send / auto-send success, 'cancelled' on skip / re-draft.
    Deliberately asymmetric about OFFERED rows (docs/modules/module-smart-booking-offers.md):
    sending a draft that offered slots must NOT confirm them (the customer hasn't
    picked — they stay offered until acceptance or expiry), but skipping/redrafting
    releases them along with any proposed hold (the offer email never went out).
    A booking already confirmed or declined in the Diary is never touched by the
    email side. Returns how many holds moved, so callers know whether the
    booking_pending restamp below actually happened (agent trace)."""
    movable = ["proposed"] if status == "confirmed" else ["proposed", "offered"]
    res = (client or db()).table("bookings").update(
        {"status": status, "updated_at": datetime.now(timezone.utc).isoformat()}
    ).eq("tenant_id", tenant_id).eq("email_log_id", email_log_id).in_("status", movable).execute()
    if status == "cancelled":
        _offers_closed(tenant_id, [r for r in (res.data or []) if r.get("expires_at")],
                       "released", client=client)
        # A quote the auto-draft wrote from these holds must not outlive them
        # (round three, 1 Oct 2026 - F26). Best-effort: never costs the release.
        if res.data:
            try:
                from app import quote_autodraft
                quote_autodraft.withdraw_for_released_holds(
                    tenant_id, email_log_id, list(res.data), client=client)
            except Exception:
                log.warning("withdrawing the auto-drafted quote failed (log=%s)",
                            email_log_id, exc_info=True)
    if status == "confirmed" and (res.data or []):
        # Smart inbox label transition: a hold this draft placed just became a real
        # booking — the conversation is Booking Pending until the date passes (the
        # read side downgrades it to quote_sent after that; see dashboard).
        (client or db()).table("email_log").update(
            {"category": "booking_pending", "category_source": "auto",
             "category_reason": "Booking confirmed in the diary"}
        ).eq("id", email_log_id).eq("tenant_id", tenant_id).execute()
        # The flip itself belongs in the Agent Log (live e2e 2026-08-31: the
        # proposed→confirmed moment was invisible — only 'Held an appointment'
        # and 'Sent the reply' appeared, with the confirmation between them
        # deducible from neither). log_agent_event is best-effort by design.
        for b in res.data:
            win = str(b.get("window") or "")
            log_agent_event(
                tenant_id, "booking_confirmed",
                f"Confirmed {b.get('booking_ref') or 'the booking'} for "
                f"{pretty_date(b.get('booking_date'))}"
                + (f" — {win}" if win and win != "day" else "")
                + " (the reply promising it was sent)",
                contact=b.get("contact"), email_log_id=email_log_id,
                booking_id=b.get("id"), booking_ref=b.get("booking_ref"),
                event_date=str(b.get("booking_date") or "") or None,
                detail={"window": b.get("window"), "trigger": "reply_sent"},
                client=client)

        # The "Out of hours?" ask on Today is answered by the booking itself:
        # the customer is in the diary, so the question is stale (first-sign-up
        # film 2026-09-30: Today still asked about an out-of-hours visit for a
        # customer confirmed for tomorrow morning). Best-effort, per contact.
        for b in res.data:
            settle_out_of_hours_ask(tenant_id, b.get("contact"), client=client)

        # The CRM promise that said we'd confirm this booking is now fulfilled —
        # close it (same thread only, conservative). Best-effort: a promise-close
        # failure never breaks the approve/auto-send path.
        try:
            log_row = ((client or db()).table("email_log").select("thread_id")
                       .eq("id", email_log_id).eq("tenant_id", tenant_id)
                       .limit(1).execute())
            thread_id = (log_row.data[0] or {}).get("thread_id") if log_row.data else None
            crm_close_promises_for_confirmed_booking(
                tenant_id, thread_id=thread_id, email_log_id=email_log_id,
                booking_ref=(res.data[0] or {}).get("booking_ref"), client=client)
        except Exception:
            log.warning("promise auto-close failed for email_log %s", email_log_id,
                        exc_info=True)
    return len(res.data or [])


def settle_out_of_hours_ask(tenant_id: str, contact: str | None,
                            client: Client | None = None) -> int:
    """Settle Today's "Out of hours?" card for one customer (src/app/urgency.py
    ask_owner_ooh files it at /app/conversations/view?contact=…) once a booking
    of theirs is confirmed — the ask is answered by the diary. Best-effort."""
    if not contact:
        return 0
    try:
        from urllib.parse import quote
        return resolve_concierge_notifications(
            tenant_id, kind="out_of_hours_ask",
            url="/app/conversations/view?contact=" + quote(contact), client=client)
    except Exception:
        log.warning("settling the out-of-hours ask failed (tenant=%s)", tenant_id, exc_info=True)
        return 0


# ── Offered-slot holds (docs/modules/module-smart-booking-offers.md) ────────────────
def offered_bookings_for_contact(tenant_id: str, contact: str | None,
                                 client: Client | None = None) -> list[dict]:
    """The offer set dangling in this contact's inbox: live offered rows, full
    columns (the accept flow needs id/date/window/ref). Expired offers drop out
    here too — a dead offer must never be silently honoured."""
    if not contact:
        return []
    res = (
        (client or db()).table("bookings").select("*")
        .eq("tenant_id", tenant_id).eq("contact", contact)
        .eq("status", "offered")
        .order("booking_date")
        .execute()
    )
    return _drop_expired_offers(res.data or [])


def accept_offered_booking(booking_id: str, tenant_id: str,
                           client: Client | None = None,
                           details: dict | None = None, *, atomic: bool = False, exclude_ids: list | None = None) -> dict | None:
    """The customer picked this offered slot: flip it offered → proposed (it already
    holds the capacity, so no re-check — the normal approve/send flow confirms it
    from here) and clear the deadline. The status guard makes this safe against the
    expiry sweep racing us: a row the sweep already released returns None and the
    caller falls back to a fresh capacity-checked hold. `details`, when given, is
    the merged intake bag (offer-time + acceptance-time) and replaces the row's.
    Portal `atomic` choice confirms and releases siblings in one transaction,
    checking the database clock and serialising concurrent sibling choices.
    """
    patch = {"status": "proposed", "expires_at": None,
             "updated_at": datetime.now(timezone.utc).isoformat()}
    if details:
        patch["details"] = details
    from app.booking_errors import CapacityConflict, capacity_conflict
    try:
        if atomic:
            res = (client or db()).rpc("choose_offered_booking", {
                "p_booking_id": booking_id, "p_tenant_id": tenant_id,
                "p_details": details, "p_exclude_ids": exclude_ids or []}).execute()
        else:
            res = (client or db()).table("bookings").update(patch).eq(
                "id", booking_id).eq("tenant_id", tenant_id).eq("status", "offered").execute()
    except Exception as exc:
        if capacity_conflict(exc):
            raise CapacityConflict() from None
        raise
    if res.data:
        # The customer chose: the "do they take the offer?" follow-up is answered.
        _offers_closed(tenant_id, res.data, "accepted", client=client)
    return res.data[0] if res.data else None


def _offers_closed(tenant_id: str, rows: list[dict], outcome: str, *, client=None,
                   event: bool = True) -> None:
    """After offered rows close: log each (unless the caller logs its own),
    close their follow-ups. Best-effort."""
    if not rows:
        return
    try:
        from app import offer_promises
        if event:
            offer_promises.note_closed(tenant_id, rows, outcome, client=client)
        else:
            offer_promises.close_for_bookings(tenant_id, rows, outcome, client=client)
    except Exception:
        log.warning("offer close bookkeeping failed (tenant=%s)", tenant_id, exc_info=True)


def release_offered_for_contact(tenant_id: str, contact: str | None,
                                exclude_ids: list | None = None,
                                client: Client | None = None) -> int:
    """Release every offered slot still dangling for this contact (minus the one
    they just accepted): the customer made their choice, the siblings go back to
    free. Also the path for 'none of those work' — a fresh hold or a new offer
    set supersedes the old one."""
    if not contact:
        return 0
    q = (client or db()).table("bookings").update(
        {"status": "cancelled", "updated_at": datetime.now(timezone.utc).isoformat()}
    ).eq("tenant_id", tenant_id).eq("contact", contact).eq("status", "offered")
    for bid in exclude_ids or []:
        q = q.neq("id", bid)
    res = q.execute()
    # The offers that just went: their "does the customer take it?" follow-ups
    # close with them (src/app/offer_promises.py); the event is the one below.
    _offers_closed(tenant_id, res.data or [], "released", client=client, event=False)
    released = res.data or []
    if released:
        # The release is visible in the Agent Log, not only the diary (Kris
        # 2026-08-31 went looking for exactly this after a mind-change e2e —
        # "did the agent release the correct slots?"). log_agent_event is
        # already best-effort, so this can never cost the caller.
        refs = ", ".join(r.get("booking_ref") or "?" for r in released)
        log_agent_event(
            tenant_id, "offers_released",
            f"Released {len(released)} previously offered "
            f"slot{'' if len(released) == 1 else 's'} ({refs}) — superseded "
            "by the customer's new choice",
            contact=contact, client=client)
    return len(released)


def expire_offered_bookings(client: Client | None = None) -> int:
    """The release sweep: cancel offered rows past their deadline (all tenants —
    runs under the service key from the retention cron). Capacity math already
    ignores them the moment expires_at passes; this makes the diary agree."""
    now = datetime.now(timezone.utc)
    res = (client or db()).table("bookings").update(
        {"status": "cancelled", "updated_at": now.isoformat()}
    ).eq("status", "offered").lt("expires_at", now.isoformat()).execute()
    rows = res.data or []
    by_tenant: dict[str, list[dict]] = {}
    for r in rows:
        by_tenant.setdefault(r.get("tenant_id"), []).append(r)
    for tid, group in by_tenant.items():
        if tid:
            _offers_closed(tid, group, "expired", client=client)
    return len(rows)


def list_offers_to_nudge(tenant_id: str, window_end_iso: str, min_age_iso: str,
                         client: Client | None = None) -> list[dict]:
    """Offered rows entering the pre-expiry nudge window (Phase 3): still live,
    releasing before `window_end_iso`, never nudged, and old enough
    (`created_at <= min_age_iso`) that the nudge isn't landing minutes after
    the offer email itself — a short-TTL offer simply never gets one. Only
    reachable contacts: a nudge with nowhere to go isn't a nudge."""
    now_iso = datetime.now(timezone.utc).isoformat()
    res = (
        (client or db()).table("bookings").select("*")
        .eq("tenant_id", tenant_id).eq("status", "offered")
        .is_("offer_nudged_at", "null")
        .gt("expires_at", now_iso).lte("expires_at", window_end_iso)
        .lte("created_at", min_age_iso)
        .order("expires_at")
        .execute()
    )
    return [b for b in (res.data or []) if reachable_contact(b.get("contact"))]


def mark_offers_nudged(booking_ids: list, tenant_id: str, client: Client | None = None) -> None:
    """Stamp a whole offer set nudged in one write (dedupe — the sweep runs every
    30 minutes and must not re-chase inside the window)."""
    if not booking_ids:
        return
    (client or db()).table("bookings").update(
        {"offer_nudged_at": datetime.now(timezone.utc).isoformat()}
    ).eq("tenant_id", tenant_id).in_("id", list(booking_ids)).execute()


# ── tenant_config ──────────────────────────────────────────────────────────
def get_tenant_config(tenant_id: str, client: Client | None = None) -> dict | None:
    res = (client or db()).table("tenant_config").select("*").eq("tenant_id", tenant_id).limit(1).execute()
    return res.data[0] if res.data else None


def upsert_tenant_config(tenant_id: str, cfg: dict, client: Client | None = None) -> None:
    (client or db()).table("tenant_config").upsert({"tenant_id": tenant_id, **cfg}).execute()


# ── Morning debrief (docs/modules/module-morning-debrief.md) ─────────────────────────
def list_debrief_due_tenants(today_iso: str, hhmm: str,
                             client: Client | None = None) -> list[dict]:
    """The 15-min sweep's work-list: enabled tenants whose local debrief time
    has passed and who haven't been briefed (or closed-day-skipped) today.
    debrief_time is zero-padded 'HH:MM' text, so lexicographic lte IS the time
    comparison. The not-sent-today filter runs client-side: PostgREST's neq
    never matches NULL rows, and never-briefed tenants are exactly the ones
    that must not be dropped."""
    res = (
        (client or db()).table("tenant_config")
        .select("tenant_id,debrief_time,debrief_on_closed_days,debrief_last_sent_on")
        .eq("debrief_enabled", True)
        .lte("debrief_time", hhmm)
        .execute()
    )
    return [r for r in (res.data or [])
            if str(r.get("debrief_last_sent_on") or "")[:10] != today_iso]


def stamp_debrief_sent(tenant_id: str, today_iso: str,
                       client: Client | None = None) -> None:
    """Stamped BEFORE composing/sending (the mark_booking_reminded ordering):
    a crash after the stamp costs one morning's brief; the reverse risks a
    double-push on the next sweep pass."""
    (client or db()).table("tenant_config") \
        .update({"debrief_last_sent_on": today_iso}) \
        .eq("tenant_id", tenant_id).execute()


# ── mailbox_config (enterprise D3: per-mailbox brain = tenant ⊕ overrides) ──
# The persona fields a mailbox may override. Everything else in a mailbox_config
# row (keys, timestamps) must never leak into the agent's config dict.
MAILBOX_OVERRIDE_FIELDS = (
    "tone", "agent_name", "sign_off", "do_list", "never_do",
    "escalation", "custom_instructions", "examples", "auto_send",
)


def get_mailbox_config(mailbox_id: str, client: Client | None = None) -> dict | None:
    res = (client or db()).table("mailbox_config").select("*").eq("mailbox_id", mailbox_id).limit(1).execute()
    return res.data[0] if res.data else None


def upsert_mailbox_config(mailbox_id: str, tenant_id: str, cfg: dict, client: Client | None = None) -> None:
    (client or db()).table("mailbox_config").upsert(
        {"mailbox_id": mailbox_id, "tenant_id": tenant_id, **cfg,
         "updated_at": datetime.now(timezone.utc).isoformat()}
    ).execute()


def effective_config(tenant_id: str, mailbox_id: str | None = None, client: Client | None = None) -> dict:
    """The config the agent actually drafts with: tenant defaults, overlaid with
    this mailbox's non-null overrides. No mailbox (the SaaS path) or no
    mailbox_config row = pure tenant config, byte-identical to today. `purpose`
    is folded into the business brief as the mailbox's role framing, so
    agent.build_system_prompt needs no new field."""
    cfg = dict(get_tenant_config(tenant_id, client=client) or {})
    if not mailbox_id:
        return cfg
    mc = get_mailbox_config(mailbox_id, client=client) or {}
    for k in MAILBOX_OVERRIDE_FIELDS:
        if mc.get(k) is not None:
            cfg[k] = mc[k]
    purpose = (mc.get("purpose") or "").strip()
    if purpose:
        brief = (cfg.get("business_brief") or "").strip()
        line = f"This mailbox: {purpose}"
        cfg["business_brief"] = f"{brief}\n\n{line}" if brief else line
    return cfg


# ── knowledge_chunks (knowledge base / fact-check sources) ──────────────────
# Enterprise D4 — layered knowledge: a chunk's mailbox_id scopes it to one
# mailbox; null = company-wide (everything pre-enterprise). A "source" is
# therefore keyed by (tenant, source name, scope): re-importing price-list.pdf
# into orders@ replaces orders@'s copy and never touches the company-wide one.
def _scope(q, mailbox_id: str | None):
    return q.eq("mailbox_id", mailbox_id) if mailbox_id else q.is_("mailbox_id", "null")


def replace_knowledge_source(
    tenant_id: str, source: str, rows: list[dict],
    client: Client | None = None, mailbox_id: str | None = None,
) -> None:
    """Replace every chunk for one source (within one scope), so re-importing a
    file/URL is idempotent."""
    result = (client or db()).rpc('replace_knowledge_source_atomic', {
        'p_tenant': tenant_id, 'p_source': source, 'p_mailbox': mailbox_id,
        'p_rows': rows,
    }).execute().data
    if type(result) is not int or result != len(rows):
        raise RuntimeError('Knowledge replacement could not be confirmed.')


def delete_knowledge_source(
    tenant_id: str, source: str, client: Client | None = None, mailbox_id: str | None = None
) -> None:
    _scope(
        (client or db()).table("knowledge_chunks").delete()
        .eq("tenant_id", tenant_id).eq("source", source),
        mailbox_id,
    ).execute()


def knowledge_source_chunks(tenant_id: str, source: str,
                            client: Client | None = None,
                            mailbox_id: str | None = None) -> list[str]:
    """One source's chunk texts, in the order they were written — what the
    Knowledge page reads back so a note can be EDITED rather than removed and
    retyped. The scope is part of the source's identity (D4)."""
    res = _scope(
        (client or db()).table("knowledge_chunks").select("content,created_at")
        .eq("tenant_id", tenant_id).eq("source", source).order("created_at").order("source_chunk_index"),
        mailbox_id,
    ).execute()
    return [r.get("content") or "" for r in (res.data or [])]


def list_knowledge_sources(tenant_id: str, client: Client | None = None) -> list[dict]:
    """One entry per (source, scope) with its chunk count (aggregated app-side —
    PostgREST has no GROUP BY). mailbox_id null = company-wide."""
    res = (
        (client or db()).table("knowledge_chunks")
        .select("source,created_at,mailbox_id")
        .eq("tenant_id", tenant_id)
        .order("created_at", desc=True)
        .execute()
    )
    agg: dict[tuple, dict] = {}
    for row in res.data or []:
        key = (row["source"], row.get("mailbox_id"))
        s = agg.setdefault(
            key,
            {"source": row["source"], "mailbox_id": row.get("mailbox_id"),
             "chunks": 0, "created_at": row["created_at"]},
        )
        s["chunks"] += 1
    return list(agg.values())


def export_knowledge_chunks(tenant_id: str, client: Client | None = None) -> list[dict]:
    """Every knowledge chunk's text, for the exit bundle (src/app/exit_export.py).
    Embeddings stay home — they're an index, not the tenant's knowledge."""
    res = ((client or db()).table("knowledge_chunks")
           .select("source,content,created_at")
           .eq("tenant_id", tenant_id)
           .order("created_at", desc=False).execute())
    return res.data or []


def match_knowledge_chunks(
    tenant_id: str, query_embedding: str, k: int = 6, mailbox_id: str | None = None
) -> list[dict]:
    """Closest chunks for one tenant via the pgvector SQL function. Service-role
    RPC — scoping is the p_tenant_id argument, not RLS. With a mailbox_id the
    match UNIONS company-wide chunks with that mailbox's own (D4); without one
    it matches company-wide only — for a standard tenant, that's everything."""
    res = db().rpc(
        "match_knowledge_chunks",
        {"p_tenant_id": tenant_id, "p_query_embedding": query_embedding,
         "p_match_count": k, "p_mailbox_id": mailbox_id},
    ).execute()
    return res.data or []


def get_knowledge_chunk(chunk_id: str, tenant_id: str, client: Client | None = None) -> dict | None:
    """One chunk scoped to its tenant — None for a missing row OR another tenant's
    (closes the IDOR on the chunk-correction flow)."""
    res = (
        (client or db()).table("knowledge_chunks")
        .select("id,content,source").eq("id", chunk_id).eq("tenant_id", tenant_id).limit(1).execute()
    )
    return res.data[0] if res.data else None


def update_knowledge_chunk(
    chunk_id: str, content: str, embedding: str, tenant_id: str, client: Client | None = None
) -> None:
    """Replace one chunk's content + re-embedded vector (tenant-scoped)."""
    (client or db()).table("knowledge_chunks").update({"content": content, "embedding": embedding}) \
        .eq("id", chunk_id).eq("tenant_id", tenant_id).execute()


# ── tenant_data_sources (onboarding: system-of-record capture) ──────────────
def list_data_sources(tenant_id: str, client: Client | None = None) -> list[dict]:
    res = (
        (client or db()).table("tenant_data_sources")
        .select("kind,vendor,status,created_at").eq("tenant_id", tenant_id)
        .order("created_at").execute()
    )
    return res.data or []


def set_data_sources(tenant_id: str, items: list[dict], client: Client | None = None) -> None:
    """Replace the tenant's captured data sources with `items` (each {kind, vendor, status})."""
    c = client or db()
    c.table("tenant_data_sources").delete().eq("tenant_id", tenant_id).execute()
    if items:
        c.table("tenant_data_sources").insert(
            [{"tenant_id": tenant_id, **it} for it in items]
        ).execute()


# ── email_log (audit trail + approval queue) ───────────────────────────────
_UNCHANGED = object()  # sentinel: "leave this column as-is" (vs. an explicit None)


def insert_email_log(
    tenant_id: str,
    from_address: str | None,
    subject: str,
    body_in: str,
    draft_reply: str,
    status: str = "pending",
    knowledge_gap: str | None = None,
    source_message_id: str | None = None,
    thread_id: str | None = None,
    mailbox_id: str | None = None,
    scheduling_gap: bool = False,
    kind: str = "inbound",
    thread_state: str | None = None,
    request_summary: str | None = None,
    action_summary: str | None = None,
    searched_for: str | None = None,
    booking_details: list | None = None,
    from_name: str | None = None,
    contact_name: str | None = None,
    contact_company: str | None = None,
    channel: str = "email",
    category: str | None = None,
    category_reason: str | None = None,
    draft_sources: list | None = None,
    draft_reasoning: str | None = None,
) -> tuple[str, bool]:
    """Insert a draft row; returns (id, created). `created` is False when a concurrent
    duplicate (same tenant + source_message_id) already existed and we returned its id
    instead — callers use this to avoid acting twice on one inbound (notably auto-send).
    `mailbox_id` stamps which mailbox received the inbound (enterprise D5); null =
    legacy rows / the SaaS single mailbox. `channel` says how the message travels
    ('email' | 'sms' — docs/modules/module-sms-channel.md D1); the send step dispatches on it."""
    try:
        res = (
            db().table("email_log")
            .insert(
                {
                    "tenant_id": tenant_id,
                    "from_address": from_address,
                    "subject": subject,
                    "body_in": body_in,
                    "draft_reply": draft_reply,
                    "status": status,
                    "knowledge_gap": knowledge_gap,
                    "scheduling_gap": scheduling_gap,
                    "source_message_id": source_message_id,
                    "thread_id": thread_id,
                    "mailbox_id": mailbox_id,
                    "kind": kind,
                    "thread_state": thread_state,
                    "request_summary": request_summary,
                    "action_summary": action_summary,
                    "searched_for": searched_for or None,
                    "booking_details": booking_details or None,
                    "from_name": from_name or None,
                    "contact_name": contact_name or None,
                    "contact_company": contact_company or None,
                    "channel": channel,
                    "category": category or None,
                    "category_reason": category_reason or None,
                    # None = retrieval never ran (KB off) — distinct from [] =
                    # ran and matched nothing (an honest "inferred" signal).
                    "draft_sources": draft_sources,
                    # The model's captured working (behind the draft box's ⓘ).
                    "draft_reasoning": draft_reasoning or None,
                }
            )
            .execute()
        )
        return res.data[0]["id"], True
    except Exception:
        # Race backstop: a concurrent webhook delivery already drafted this inbound
        # message (the (tenant_id, source_message_id) unique index rejected ours).
        # Return the existing draft rather than duplicating; re-raise anything else.
        existing = get_email_log_by_message(tenant_id, source_message_id)
        if existing:
            return existing["id"], False
        raise


# ── draft_claims (2026-09-22): one draft per inbound message, ACROSS processes ──
# `pipeline._drafts_in_flight` only guards one process. A duplicate webhook
# delivery lands in another and both draft. The primary key on
# (tenant_id, message_id) is the lock.
DRAFT_CLAIM_GRACE_S = 600


def draft_fingerprint(from_address: str | None, subject: str | None, body: str | None) -> str:
    """A key for one inbound email that does NOT depend on the provider's id.

    The push path and the poll path both claim on `source_message_id`, and on
    2026-09-22 two runs drafted the same email anyway — so the id they carried
    was not the same id. Whatever the reason, keying the lock on it makes the
    lock only as reliable as the thing it is guarding against. The sender, the
    subject and the opening of the body are identical however the message
    arrives.

    Concurrency only: the claim is released when the draft ends, so a customer
    who really does send the same text twice still gets a second draft. What
    cannot happen is two drafts of it at once."""
    import hashlib
    raw = "".join([
        (from_address or "").strip().lower(),
        " ".join((subject or "").split()).lower(),
        " ".join((body or "")[:500].split()).lower(),
    ])
    return "fp:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def claim_inbound_draft(tenant_id: str, message_id: str, client: Client | None = None) -> bool:
    """True if this caller may draft `message_id`. False if someone else holds
    the claim and has not been at it longer than the grace period.

    FAILS OPEN. If the claim table cannot be reached we draft anyway: a rare
    duplicate draft is a cost, while refusing to draft on a database blip is a
    customer email that never gets answered.
    """
    try:
        c = client or db()
    except Exception:
        # The client factory itself is unavailable (and in tests it is blocked
        # outright). Nothing to claim with — draft, as the docstring promises.
        log.warning("no client for the draft claim on %s — drafting anyway", message_id)
        return True
    try:
        c.table("draft_claims").insert(
            {"tenant_id": tenant_id, "message_id": message_id}).execute()
        return True
    except Exception:
        pass                                       # taken — or the table is unreachable
    try:
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(seconds=DRAFT_CLAIM_GRACE_S)).isoformat()
        # Server-side filter, so two processes racing to take over a dead claim
        # cannot both win: whoever updates first moves claimed_at past the
        # cutoff and the other's update matches no row.
        res = (c.table("draft_claims")
               .update({"claimed_at": datetime.now(timezone.utc).isoformat()})
               .eq("tenant_id", tenant_id).eq("message_id", message_id)
               .lt("claimed_at", cutoff).execute())
        if res.data:
            log.warning("took over a stale draft claim for %s", message_id)
            return True
        return False
    except Exception:
        log.warning("draft claim check failed for %s — drafting anyway", message_id,
                    exc_info=True)
        return True


def release_inbound_draft(tenant_id: str, message_id: str, client: Client | None = None) -> None:
    """The draft finished (or died). Dropping the claim lets a genuine retry of
    the same message through without waiting out the grace period."""
    try:
        ((client or db()).table("draft_claims").delete()
         .eq("tenant_id", tenant_id).eq("message_id", message_id).execute())
    except Exception:
        # Never raises: this runs in a `finally`, and a claim left behind is
        # taken over after the grace period anyway.
        log.warning("failed to release draft claim %s", message_id)


def get_email_log_by_message(tenant_id: str, source_message_id: str | None, client: Client | None = None) -> dict | None:
    """The draft (if any) already created for an inbound provider message — the
    idempotency key for auto-drafting. None when not yet drafted."""
    if not source_message_id:
        return None
    res = (
        (client or db()).table("email_log").select("id,status")
        .eq("tenant_id", tenant_id).eq("source_message_id", source_message_id).limit(1).execute()
    )
    return res.data[0] if res.data else None


def email_log_index(tenant_id: str, client: Client | None = None) -> list[dict]:
    """Per-draft handling state (oldest→newest) for correlating the inbox to how far each
    conversation has been dealt with — drafted / sent / skipped — and whether it flagged a
    knowledge gap. Callers let the latest matching row win per inbound message."""
    res = (
        (client or db()).table("email_log")
        .select("source_message_id,from_address,subject,status,knowledge_gap,scheduling_gap,created_at")
        .eq("tenant_id", tenant_id).order("created_at").execute()
    )
    return res.data or []


def _booking_label(date_iso: str | None, window: str | None) -> str:
    """Compact 'Thu 2 Jul' / 'Thu 2 Jul, 9am' label for the inbox booking chip."""
    try:
        d = datetime.fromisoformat(date_iso)
        out = f"{d.strftime('%a')} {d.day} {d.strftime('%b')}"
    except (ValueError, TypeError):
        return date_iso or ""
    w = window or ""
    if w and w != "day":
        if ":" in w:
            try:
                h, m = w.split(":")[0:2]; h = int(h)
                out += f", {h % 12 or 12}{'' if m == '00' else ':' + m}{'am' if h < 12 else 'pm'}"
            except ValueError:
                out += f", {w}"
        elif w in ("am", "pm"):
            out += f", {w.upper()}"
    return out


# How many row ids ride in one preview lookup. The ids travel in the request
# URL, so this bounds the request line (150 uuids ≈ 5.7 KB, inside the usual
# 8 KB) — one round trip for any realistic book of contacts, and a tenant
# past that pages instead of building a URL the gateway would reject.
_PREVIEW_CHUNK = 150
# Two slots: the previews read runs alongside the bookings read on the calling
# thread. Deliberately tiny — this is one query's worth of overlap, not a
# general-purpose pool.
_CONVO_FAN = ThreadPoolExecutor(max_workers=2, thread_name_prefix="convo")

# An outbound-only round — an invoice, a quote, a reminder the business sent
# with no customer message behind it — is logged as one email_log row with a
# "[invoice]"-style placeholder where the customer's words would be. The
# thread names such a round for what it is (dashboard._inbound_display); the
# inbox row's preview has to do the same rather than print the placeholder
# (stays film, 2026-09-20: "Invoice INV-1001 — [invoice] · 3 messages").
PLACEHOLDER_BODY = re.compile(r"^\s*\[[a-z_ -]+\]", re.IGNORECASE)   # "[invoice]", "[day-before reminder] …"
OUTBOUND_KIND_WORDS = {
    "invoice": "the invoice you sent", "quote": "the quote you sent",
    "invoice_reminder": "the payment reminder you sent",
    "invoice_overdue": "the overdue-payment chase you sent",
    "quote_reminder": "the quote reminder you sent",
    "quote_expired": "the quote-expiry note you sent",
    "quote_keeps_looking": "the follow-up you sent",
    "reminder": "the booking reminder you sent", "offer_nudge": "the offered-slots nudge you sent",
    "recall": "the recall reminder you sent", "special": "the message you sent",
    "on_my_way": "the on-my-way message you sent",
    # An email the owner asked the voice Concierge to write (src/app/concierge_draft.py).
    "owner_message": "the email you asked for",
    # An owner-confirmed request for an up-to-date record (a certificate, an Inspection).
    "renewal_request": "the request for an up-to-date record you sent",
}


def conversation_preview(body, kind) -> str:
    """The inbox row's summary for one email_log row: the customer's first 140
    characters — or, when the row is an outbound-only round, what the business
    sent ('the invoice you sent'), never the bracketed placeholder."""
    text = body or ""
    if PLACEHOLDER_BODY.match(text) and str(kind or "") in OUTBOUND_KIND_WORDS:
        return OUTBOUND_KIND_WORDS[str(kind)]
    return text[:140]


def _conversation_previews(ids: list, client) -> dict:
    """{row id: its preview (conversation_preview)} for the rows that won the
    fold. Only these bodies are read: selecting body_in in the list query
    itself shipped every email this tenant has ever received on every Inbox and
    Today render (2.5 MB across 323 rows on the pilot tenant), and that payload
    grew with message volume forever. Best-effort — no body, no preview, never
    a broken inbox."""
    bodies: dict = {}
    if not ids:
        return bodies
    try:
        for i in range(0, len(ids), _PREVIEW_CHUNK):
            res = ((client or db()).table("email_log").select("id,body_in,kind")
                   .in_("id", ids[i:i + _PREVIEW_CHUNK]).execute())
            for r in res.data or []:
                bodies[r["id"]] = conversation_preview(r.get("body_in"), r.get("kind"))
    except Exception:
        log.warning("conversation previews unavailable", exc_info=True)
    return bodies


_EMAIL_LOG_PAGE = 1000   # PostgREST's max-rows: one request never returns more than this


def _all_rows(build, page: int = _EMAIL_LOG_PAGE) -> list[dict]:
    """Every row of a query that may exceed PostgREST's per-request cap. `build(count)` returns a
    fresh, ordered query builder (count="exact" asks for the total on the first page). The first page
    says how many rows there are; the rest are fetched in parallel. Before this, a tenant past 1,000
    emails got only its OLDEST 1,000 in the Inbox and Today: the newest emails (and their state)
    silently never appeared (found 14 Oct on an account with 1,093)."""
    from app import parallel_reads
    first = build("exact").range(0, page - 1).execute()
    rows = list(first.data or [])
    total = getattr(first, "count", None) or len(rows)
    if len(rows) >= page and total > page:
        parts = parallel_reads.run_all(
            [lambda s=s: build(None).range(s, s + page - 1).execute().data or []
             for s in range(page, total, page)])
        for part in parts:
            rows += part
    return rows


def list_conversations(
    tenant_id: str, client: Client | None = None,
    mailbox_id: str | None = None, default_mailbox_id: str | None = None,
) -> list[dict]:
    """The conversation list (see _list_conversations), cached per tenant for up to 60 s and
    dropped by ANY write to email_log or bookings (navcache.bump_data via the db client hooks), so
    the list the owner acts on is never older than their last action. Rebuilding it reads the
    tenant's whole email log (1.5 s for 1,000 emails, growing with every message) on every Today and
    Inbox view. Returns a private copy: callers mutate the rows."""
    import copy
    key = f"t:{tenant_id}:data:convos:{mailbox_id or ''}:{default_mailbox_id or ''}"
    out = navcache.get_or_fill_data(
        tenant_id, key, 60.0,
        lambda: _list_conversations(tenant_id, client, mailbox_id, default_mailbox_id))
    return copy.deepcopy(out)


def _list_conversations(
    tenant_id: str, client: Client | None = None,
    mailbox_id: str | None = None, default_mailbox_id: str | None = None,
) -> list[dict]:
    """One entry per contact (sender address) — the conversation list. Aggregated app-side
    (PostgREST has no GROUP BY); the most recent handled email wins for the subject/state/
    gap shown, newest conversations first.

    `mailbox_id` filters to one mailbox (enterprise). Legacy rows carry a null
    mailbox_id and render under the DEFAULT mailbox — pass `default_mailbox_id`
    so the filter knows which one that is. Filtering happens app-side, like the
    aggregation (an OR-with-null filter in PostgREST buys nothing at this scale)."""
    # body_in is deliberately NOT selected here. The fold below overwrites
    # `preview` on every row, so only the LATEST row per contact ever survives —
    # but selecting the column shipped every body this tenant has ever received
    # on every Inbox and Today render (2.5 MB for 323 rows on the pilot tenant,
    # ~180ms of pure payload, growing with message volume forever). The bodies
    # for exactly the rows that win are fetched below instead, so the payload
    # now scales with CONTACTS, not with message count.
    log_rows = _all_rows(lambda count: (
        (client or db()).table("email_log")
        .select("id,from_address,subject,status,knowledge_gap,scheduling_gap,created_at,"
                "mailbox_id,thread_state,request_summary,action_summary,thread_id,"
                "from_name,contact_name,contact_company,booking_details,"
                "category,category_reason,category_source", count=count)
        .eq("tenant_id", tenant_id).order("created_at").order("id")))   # oldest→newest, stable pages
    convos: dict[str, dict] = {}
    for r in log_rows:
        if mailbox_id:
            row_box = r.get("mailbox_id") or default_mailbox_id
            if row_box != mailbox_id:
                continue
        key = r.get("from_address") or "(unknown sender)"
        c = convos.setdefault(key, {"contact": key, "count": 0})
        c["count"] += 1
        c["subject"] = r.get("subject")            # rows are oldest→newest, so last seen = latest
        c["preview"] = ""                          # filled from the winning row's body below
        c["_preview_row"] = r.get("id")            # ...which is this one, until a newer row wins
        c["state"] = r.get("status")
        c["gap"] = r.get("knowledge_gap")
        c["sched_gap"] = r.get("scheduling_gap")
        c["last_at"] = r.get("created_at")
        c["mailbox_id"] = r.get("mailbox_id")      # latest row's mailbox, for the scope chip
        c["thread_state"] = r.get("thread_state")  # inbox-at-a-glance: latest row wins
        c["request_summary"] = r.get("request_summary")
        c["action_summary"] = r.get("action_summary")
        # Smart inbox label: latest row wins, exactly like thread_state (the label
        # IS the conversation's current state, so an older row must never win).
        c["category"] = r.get("category")
        c["category_reason"] = r.get("category_reason")
        c["category_source"] = r.get("category_source")
        # Contact identity: last NON-EMPTY wins (a later email that didn't restate
        # the name must never blank what the thread already established).
        # booking_details rides the same rule — the agent re-reports the cumulative
        # bag each turn, so the newest non-empty list is the complete one.
        for f in ("from_name", "contact_name", "contact_company", "booking_details"):
            if r.get(f):
                c[f] = r[f]
    # Booking-aware inbox: attach each contact's soonest UPCOMING confirmed booking, so the
    # row shows a ✓ Booked chip regardless of the thread's latest state. Proposed holds
    # ride the same query for the smart label only (has_upcoming_booking keeps a
    # conversation Booking Pending — and lets one whose date passed complete to
    # quote_sent); the chip itself stays confirmed-only.
    # The bookings read and the previews read below both depend only on the fold
    # and on nothing from each other, so they go out together rather than one
    # after the other.
    wanted = {c["contact"]: c.pop("_preview_row", None) for c in convos.values()}
    f_previews = _CONVO_FAN.submit(_conversation_previews,
                                   [i for i in wanted.values() if i], client)
    if convos:
        today = datetime.now(timezone.utc).date().isoformat()
        bks = (
            (client or db()).table("bookings")
            .select("contact,booking_ref,booking_date,window,status")
            .eq("tenant_id", tenant_id).in_("status", ["proposed", "confirmed"])
            .gte("booking_date", today).order("booking_date").execute()
        )
        for b in (bks.data or []):
            c = convos.get(b.get("contact"))
            if not c:
                continue
            c["has_upcoming_booking"] = True
            if b.get("status") == "confirmed" and not c.get("booking"):
                # rows are date-ordered → soonest confirmed wins
                c["booking"] = {"ref": b.get("booking_ref"),
                                "label": _booking_label(b.get("booking_date"), b.get("window"))}
    bodies = f_previews.result()
    for key, row_id in wanted.items():
        if row_id in bodies:
            convos[key]["preview"] = bodies[row_id]
    # The person, not the address: the name the customer STATED beats the From
    # header's display name; None keeps the template on its address fallback.
    for c in convos.values():
        c["display_name"] = c.get("contact_name") or c.get("from_name") or None
    return sorted(convos.values(), key=lambda c: c.get("last_at") or "", reverse=True)


def count_confirmed_booking_sends(tenant_id: str, client: Client | None = None) -> int:
    """How many booking-request replies the human has manually confirmed & sent —
    sent rows carrying a gathered booking_details bag. Drives the occasional
    'this can be automated' upsell (every 5th confirm), statelessly."""
    res = ((client or db()).table("email_log").select("id", count="exact")
           .eq("tenant_id", tenant_id).eq("status", "sent")
           .not_.is_("booking_details", "null").limit(1).execute())
    return res.count or 0


def get_conversation(tenant_id: str, contact: str | None, client: Client | None = None,
                     thread_id: str | None = None) -> list[dict]:
    """Every handled email for one conversation, oldest→newest (each row carries the
    inbound message + our reply/draft). Tenant-scoped. Grouped by provider thread_id when
    one is given (Phase 5 — a single real email thread), else by contact (sender address)."""
    q = (
        (client or db()).table("email_log")
        .select("id,from_address,subject,body_in,draft_reply,status,knowledge_gap,scheduling_gap,created_at,"
                "thread_state,request_summary,action_summary,searched_for,booking_details,"
                "category,category_reason,category_source,source_message_id,"
                # kind: the decision ghost's proactive-send why; draft_sources:
                # the grounding pills + the ghost's search-line fallback — both
                # were silently absent from the thread until selected here.
                # sent_via/edited: the sent bubble's provenance label ("AI ·
                # Sent" / "AI · Approved by you" / "You · Sent").
                # draft_reasoning: the ⓘ working panel on the draft box.
                # sent_at: the time/date on the sent bubble.
                "kind,draft_sources,sent_via,edited,draft_reasoning,sent_at,"
                # mailbox_id: the own-mailbox lens (people.may_read_mailbox)
                # reads it — without it every row looked like the owner's.
                "mailbox_id")
        .eq("tenant_id", tenant_id)
    )
    q = q.eq("thread_id", thread_id) if thread_id else q.eq("from_address", contact)
    return (q.order("created_at").execute()).data or []


# ── portal_messages (2026-08-31): the customer-portal chat transcript ───────
# Turns land only for IDENTIFIED customers, keyed by the same contact email
# the inbox threads on — the thread interleaves them, the drafter replays them.
def insert_portal_messages(tenant_id: str, contact: str, turns: list[dict],
                           client: Client | None = None) -> None:
    """`turns` = [{"role": "customer"|"assistant", "content": str}, ...] —
    one chat exchange, stored in order."""
    rows = [{"tenant_id": tenant_id, "contact": contact,
             "role": t["role"], "content": t["content"]}
            for t in turns if (t.get("content") or "").strip()]
    if rows:
        (client or db()).table("portal_messages").insert(rows).execute()


def list_portal_messages(tenant_id: str, contact: str, limit: int = 40,
                         client: Client | None = None) -> list[dict]:
    """The newest `limit` portal chat turns for one contact, oldest→newest."""
    res = ((client or db()).table("portal_messages")
           .select("role,content,created_at")
           .eq("tenant_id", tenant_id).eq("contact", contact)
           .order("created_at", desc=True).limit(limit).execute())
    return list(reversed(res.data or []))


def booking_by_ref(tenant_id: str, ref: str, client: Client | None = None) -> dict | None:
    """One booking by its human reference (BK-1037) — the thread's portal-request
    strip resolves the ref a customer mentioned into the live row."""
    res = ((client or db()).table("bookings").select("*")
           .eq("tenant_id", tenant_id).eq("booking_ref", ref)
           .limit(1).execute())
    return (res.data or [None])[0]


def skip_pending_for_contact(tenant_id: str, address: str, client: Client | None = None) -> None:
    """Mark a contact's pending drafts skipped — used when ignoring them so nothing lingers
    in the approval queue / draft count."""
    (client or db()).table("email_log").update({"status": "skipped"}) \
        .eq("tenant_id", tenant_id).eq("from_address", address).eq("status", "pending").execute()


# ── ignored_contacts (senders the agent never drafts to + are hidden) ───────
def ignore_contact(tenant_id: str, address: str, reason: str | None = None, client: Client | None = None) -> None:
    (client or db()).table("ignored_contacts").upsert(
        {"tenant_id": tenant_id, "address": address, "reason": reason},
        on_conflict="tenant_id,address",
    ).execute()


def unignore_contact(tenant_id: str, address: str, client: Client | None = None) -> None:
    (client or db()).table("ignored_contacts").delete() \
        .eq("tenant_id", tenant_id).eq("address", address).execute()


def list_ignored(tenant_id: str, client: Client | None = None) -> list[dict]:
    res = (
        (client or db()).table("ignored_contacts").select("address,reason,created_at")
        .eq("tenant_id", tenant_id).order("created_at", desc=True).execute()
    )
    return res.data or []


def is_ignored(tenant_id: str, address: str | None, client: Client | None = None) -> bool:
    """Has the tenant ignored this sender? Called by the pipeline (service role) to skip
    auto-drafting, and to filter the inbox/threads list."""
    if not address:
        return False
    res = (
        (client or db()).table("ignored_contacts").select("address")
        .eq("tenant_id", tenant_id).eq("address", address).limit(1).execute()
    )
    return bool(res.data)


def ignored_addresses(tenant_id: str, client: Client | None = None) -> set:
    res = (client or db()).table("ignored_contacts").select("address").eq("tenant_id", tenant_id).execute()
    return {r["address"] for r in (res.data or []) if r.get("address")}


# ── contact_draft_stats (draft suppression — src/app/suppression.py owns the logic) ──
def get_contact_draft_stats(tenant_id: str, address: str, client: Client | None = None) -> dict | None:
    res = (
        (client or db()).table("contact_draft_stats").select("*")
        .eq("tenant_id", tenant_id).eq("address", address).limit(1).execute()
    )
    return res.data[0] if res.data else None


def upsert_contact_draft_stats(tenant_id: str, address: str, fields: dict,
                               client: Client | None = None) -> None:
    """Write one contact's draft-streak record (unique on tenant_id+address, so a
    re-write just updates). `updated_at` is stamped here so callers never forget."""
    row = {"tenant_id": tenant_id, "address": address,
           "updated_at": datetime.now(timezone.utc).isoformat(), **fields}
    (client or db()).table("contact_draft_stats").upsert(
        row, on_conflict="tenant_id,address").execute()


def list_draft_suppressed(tenant_id: str, client: Client | None = None) -> list[dict]:
    """Every contact whose drafting is paused (threshold hit) or permanently off —
    the Ignored page's second section and the Concierge's 'you've been ignoring…' nudge."""
    res = (
        (client or db()).table("contact_draft_stats").select("*")
        .eq("tenant_id", tenant_id).neq("status", "active")
        .order("suppressed_at", desc=True).execute()
    )
    return res.data or []


def recent_inbound_body_lengths(tenant_id: str, address: str, limit: int = 20,
                                client: Client | None = None) -> list[int]:
    """Character lengths of this sender's recent inbound bodies — the baseline the
    re-engagement heuristic compares an unusual email against."""
    res = (
        (client or db()).table("email_log").select("body_in")
        .eq("tenant_id", tenant_id).eq("from_address", address).eq("kind", "inbound")
        .order("created_at", desc=True).limit(limit).execute()
    )
    return [len(r["body_in"]) for r in (res.data or []) if r.get("body_in")]


def list_email_log(tenant_id: str, status: str | None = None, client: Client | None = None) -> list[dict]:
    q = (
        (client or db()).table("email_log")
        .select("id,from_address,subject,status,draft_reply,knowledge_gap,scheduling_gap,created_at")
        .eq("tenant_id", tenant_id)
        .order("created_at", desc=True)
    )
    if status:
        q = q.eq("status", status)
    return q.execute().data


# ── agent_events (the Agent Log — app/agent_log.py transparency page) ────────
def log_agent_event(tenant_id: str, kind: str, summary: str, *, direction: str = "inbound",
                    contact: str | None = None, subject: str | None = None,
                    email_log_id: str | None = None, booking_id: str | None = None,
                    booking_ref: str | None = None, mailbox_id: str | None = None,
                    event_date: str | None = None, detail: dict | None = None,
                    client: Client | None = None) -> None:
    """Append one curated activity-log row. BEST-EFFORT: a logging failure must never
    break drafting, so any error is swallowed (and warned). Written by the pipeline
    under the service role. `event_date` is the diary date the action concerns (ISO
    'YYYY-MM-DD'), so the page can answer 'what put something on THIS calendar day'."""
    try:
        # Who did it (People slice 4). Ambient, from the request — see
        # src/app/actor.py for why it is not an argument on all 149 call sites.
        # Absent for everything BLAM8 does on its own, which is most of this
        # feed, and absent is the honest answer rather than a guess.
        from app import actor as _actor
        who = _actor.stamp(tenant_id)
        (client or db()).table("agent_events").insert({
            **who,
            "tenant_id": tenant_id,
            "kind": kind,
            "direction": direction,
            "summary": summary,
            "contact": contact,
            "subject": subject,
            "email_log_id": email_log_id,
            "booking_id": booking_id,
            "booking_ref": booking_ref,
            "mailbox_id": mailbox_id,
            "event_date": event_date,
            "detail": detail or {},
        }).execute()
    except Exception:
        log.warning("agent_events insert failed (tenant=%s kind=%s)", tenant_id, kind,
                    exc_info=True)


def list_agent_events(tenant_id: str, *, on_date: str | None = None,
                      kind: str | list | tuple | None = None,
                      q: str | None = None, who: str | None = None,
                      limit: int = 100, offset: int = 0,
                      client: Client | None = None) -> list[dict]:
    """The Agent Log feed, newest-first. Optional filters: `on_date` (ISO date — the
    CALENDAR date the action concerns, so a date search traces a calendar entry back to
    its cause), `kind`, `q` (substring over contact/subject/summary), and `who` — a
    person's user id, or the literal 'blam8' for everything the agent did on its own.
    Tenant-scoped; dashboard reads also pass their JWT client so RLS double-guards
    isolation."""
    query = (
        (client or db()).table("agent_events")
        .select("id,kind,direction,contact,subject,summary,event_date,booking_ref,"
                "email_log_id,booking_id,detail,created_at,mailbox_id,"
                "actor_user_id,actor_name")
        .eq("tenant_id", tenant_id)
        .order("created_at", desc=True)
    )
    if who == "blam8":
        query = query.is_("actor_user_id", "null")
    elif who:
        query = query.eq("actor_user_id", who)
    if kind:
        # A list/tuple is a kind GROUP (e.g. the Agent Log's 'Customer portal'
        # pill covering every portal-written kind); a string stays an exact match.
        query = (query.in_("kind", list(kind))
                 if isinstance(kind, (list, tuple)) else query.eq("kind", kind))
    if on_date:
        query = query.eq("event_date", on_date)
    if q:
        # Strip PostgREST or-filter delimiters so a stray comma/paren can't break the query.
        like = "%" + "".join(c for c in q if c not in ",()") + "%"
        query = query.or_(f"contact.ilike.{like},subject.ilike.{like},summary.ilike.{like}")
    return query.range(offset, offset + limit - 1).execute().data or []


def get_email_log_entry(log_id: str, tenant_id: str, client: Client | None = None) -> dict | None:
    """Fetch one email_log row, scoped to the owning tenant. Returns None for a
    missing row OR a row belonging to another tenant (closes the IDOR)."""
    res = (
        (client or db()).table("email_log")
        .select("*")
        .eq("id", log_id)
        .eq("tenant_id", tenant_id)
        .limit(1)
        .execute()
    )
    return res.data[0] if res.data else None


def get_email_log_entries(tenant_id: str, log_ids: list[str],
                          client: Client | None = None) -> list[dict]:
    """The rows behind a batch of ids, tenant-scoped — the exchange each open
    promise on Today was read out of (crm.promise_card). One query for the
    whole deck, and only the columns the pop-up shows: never the draft or the
    agent's working, and the inbound body only so their own words can be
    quoted. Missing ids simply come back absent."""
    ids = [str(i) for i in dict.fromkeys(log_ids or ()) if i]
    if not ids:
        return []
    res = (
        (client or db()).table("email_log")
        .select("id,subject,from_address,from_name,kind,channel,status,created_at,"
                "request_summary,action_summary,body_in")
        .eq("tenant_id", tenant_id)
        .in_("id", ids[:100])
        .execute()
    )
    return res.data or []


def set_email_log_status(log_id: str, status: str, tenant_id: str, client: Client | None = None) -> None:
    (client or db()).table("email_log").update({"status": status}) \
        .eq("id", log_id).eq("tenant_id", tenant_id).execute()


def retire_superseded_drafts(tenant_id: str, thread_id: str, keep_id: str,
                             client: Client | None = None) -> list[dict]:
    """Skip this thread's OLDER inbound drafts still waiting — a newer draft
    replaces them (Kris 2026-09-27). Filed under FYI with the why, the way a
    hand skip is. Best-effort: returns the retired rows, [] on any failure."""
    try:
        keep = ((client or db()).table("email_log").select("created_at")
                .eq("id", keep_id).eq("tenant_id", tenant_id).limit(1).execute().data or [])
        if not keep:
            return []
        res = ((client or db()).table("email_log")
               .update({"status": "skipped", "category": "fyi", "category_source": "auto",
                        "category_reason": "Replaced by a newer draft in this conversation"})
               .eq("tenant_id", tenant_id).eq("thread_id", thread_id)
               .eq("kind", "inbound").eq("status", "pending")
               .neq("id", keep_id).lt("created_at", keep[0]["created_at"]).execute())
        return res.data or []
    except Exception:
        log.warning("retiring superseded drafts failed (tenant=%s thread=%s)",
                    tenant_id, thread_id, exc_info=True)
        return []


def set_email_log_category(log_id: str, tenant_id: str, category: str,
                           reason: str | None = None, source: str = "auto",
                           client: Client | None = None) -> None:
    """Restamp one row's smart inbox label (auto-categorisation) — used by the
    pipeline events that aren't a draft write (skip → fyi) and by the manual
    override (source='manual', which sticks until the next pipeline event)."""
    (client or db()).table("email_log").update(
        {"category": category, "category_reason": reason, "category_source": source}
    ).eq("id", log_id).eq("tenant_id", tenant_id).execute()


def set_conversation_category(tenant_id: str, contact: str, category: str,
                              client: Client | None = None) -> bool:
    """Manual override from the conversation header: stamp the label on the
    conversation's LATEST row (the row the inbox aggregation lets win). Returns
    False when the contact has no rows (nothing to stamp — e.g. a brand-new
    undrafted sender)."""
    rows = ((client or db()).table("email_log").select("id")
            .eq("tenant_id", tenant_id).eq("from_address", contact)
            .order("created_at", desc=True).limit(1).execute().data or [])
    if not rows:
        return False
    set_email_log_category(rows[0]["id"], tenant_id, category,
                           reason="Set manually", source="manual", client=client)
    return True


def _settle_draft_ready_card(log_id: str, tenant_id: str, client: Client | None = None) -> None:
    """The feed keys a draft's card by the thread it deep-links to (push.py:
    /app/conversations/view?contact=…), so the row's sender address is the key."""
    try:
        from urllib.parse import quote
        res = ((client or db()).table("email_log").select("from_address")
               .eq("id", log_id).eq("tenant_id", tenant_id).limit(1).execute())
        addr = (res.data or [{}])[0].get("from_address")
        if addr:
            resolve_concierge_notifications(
                tenant_id, kind="draft_ready",
                url="/app/conversations/view?contact=" + quote(addr), client=client)
    except Exception:
        log.warning("settling the draft-ready card failed (log=%s)", log_id, exc_info=True)


def mark_email_log_sent(
    log_id: str, tenant_id: str, via: str, edited: bool | None = None, client: Client | None = None
) -> None:
    """Move a row to 'sent' AND stamp how it got there — the raw material for the
    operator's draft-quality rates. via: 'human' (dashboard approve) | 'auto'
    (full-auto pipeline). edited: did the human change the agent's text before
    approving (auto sends pass False by definition). The send also settles the
    row's "Draft ready" card on Today (resolve_concierge_notifications)."""
    _settle_draft_ready_card(log_id, tenant_id, client=client)
    (client or db()).table("email_log").update(
        {"status": "sent", "sent_via": via, "edited": edited,
         # When it went — the time on the thread's sent bubble (Kris 2026-09-23).
         "sent_at": datetime.now(timezone.utc).isoformat(),
         # Smart inbox label transition: approved/auto-sent → Quote Sent (a linked
         # hold confirmed right after moves it on to booking_pending — see
         # resolve_holds_for_log). A send is a pipeline event, so it restamps a
         # manual label too (the system state genuinely changed).
         "category": "quote_sent", "category_source": "auto",
         "category_reason": "Reply sent — awaiting the customer"}
    ).eq("id", log_id).eq("tenant_id", tenant_id).execute()


def set_email_log_review_seconds(log_id: str, tenant_id: str, seconds: int,
                                 client: Client | None = None) -> None:
    """Stamp how long the approved draft was actually on screen (visible dwell,
    client-measured) before the human hit send — the HRT raw material. A
    separate best-effort update so a send NEVER fails on it (e.g. the
    review_seconds migration not applied yet)."""
    try:
        (client or db()).table("email_log").update({"review_seconds": seconds}) \
            .eq("id", log_id).eq("tenant_id", tenant_id).execute()
    except Exception:
        log.warning("review_seconds stamp failed for email_log %s", log_id, exc_info=True)


def set_email_log_draft_original(log_id: str, tenant_id: str, original: str,
                                 client: Client | None = None) -> bool:
    """Keep the agent's own text of a draft the owner then changed (the send
    overwrites draft_reply with the edit) — the edit-review card compares the
    two. Best-effort like the HRT stamp: a send never fails on it (e.g. the
    draft_original migration not applied yet). True if it was kept."""
    try:
        (client or db()).table("email_log").update({"draft_original": original}) \
            .eq("id", log_id).eq("tenant_id", tenant_id).execute()
        return True
    except Exception:
        log.warning("draft_original stamp failed for email_log %s", log_id, exc_info=True)
        return False


def set_email_log_spoken_seconds(log_id: str, tenant_id: str, seconds: int,
                                 client: Client | None = None) -> None:
    """Stamp how long the owner spoke asking the voice Concierge for this
    draft — human time src/app/time_saved.py takes off the email's saving.
    Best-effort like the HRT stamp: a draft never fails on it (e.g. the
    spoken_seconds migration not applied yet)."""
    try:
        (client or db()).table("email_log").update({"spoken_seconds": seconds}) \
            .eq("id", log_id).eq("tenant_id", tenant_id).execute()
    except Exception:
        log.warning("spoken_seconds stamp failed for email_log %s", log_id, exc_info=True)


def email_log_month_stats(tenant_id: str, client: Client | None = None) -> dict:
    """Per-month draft history for the Inbox's collapsed month faces:
    {'YYYY-MM': {'sent': n, 'drafted': n, 'hrt': avg_seconds | None}}.
    drafted counts every row the agent wrote (sent, still pending, or skipped);
    HRT averages review_seconds over human sends that carry one — tracking
    starts 2026-08-31, so older months have none and show no HRT. Best-effort:
    any failure returns {} and the month faces just skip their history line."""
    try:
        rows: list[dict] = []
        while True:   # page past the 1000-row PostgREST cap
            page = ((client or db()).table("email_log")
                    .select("created_at,status,sent_via,review_seconds")
                    .eq("tenant_id", tenant_id).order("created_at", desc=True)
                    .range(len(rows), len(rows) + 999).execute().data or [])
            rows.extend(page)
            if len(page) < 1000:
                break
    except Exception:
        log.warning("email_log month stats unavailable", exc_info=True)
        return {}
    out: dict[str, dict] = {}
    for r in rows:
        key = (r.get("created_at") or "")[:7]
        if not key:
            continue
        m = out.setdefault(key, {"sent": 0, "drafted": 0, "_rs": []})
        if r.get("status") == "sent":
            m["sent"] += 1
        if r.get("status") in ("sent", "pending", "skipped"):
            m["drafted"] += 1
        if (r.get("status") == "sent" and r.get("sent_via") == "human"
                and isinstance(r.get("review_seconds"), (int, float))):
            m["_rs"].append(r["review_seconds"])
    for m in out.values():
        rs = m.pop("_rs")
        m["hrt"] = (sum(rs) / len(rs)) if rs else None
    return out


def _page_all(build) -> list[dict]:
    """Every row of a query, paging past the 1000-row PostgREST cap."""
    rows: list[dict] = []
    while True:
        page = build().range(len(rows), len(rows) + 999).execute().data or []
        rows.extend(page)
        if len(page) < 1000:
            return rows


def time_saved_email_rows(tenant_id: str | None = None, client: Client | None = None) -> list[dict]:
    """Sent email_log rows, just the columns src/app/time_saved.py prices
    (cross-tenant when tenant_id is None — service role)."""
    def build():
        q = ((client or db()).table("email_log")
             .select("tenant_id,kind,status,sent_via,review_seconds,spoken_seconds")
             .eq("status", "sent"))
        if tenant_id:
            q = q.eq("tenant_id", tenant_id)
        return q.order("created_at", desc=True)
    return _page_all(build)


def time_saved_action_rows(tenant_id: str | None = None, client: Client | None = None) -> list[dict]:
    """Executed concierge_proposals rows for src/app/time_saved.py (payload +
    result travel so a bulk card and a series cancel count what really ran)."""
    def build():
        q = ((client or db()).table("concierge_proposals")
             .select("id,tenant_id,kind,status,payload,result,created_at,executed_at")
             .eq("status", "executed"))
        if tenant_id:
            q = q.eq("tenant_id", tenant_id)
        return q.order("created_at", desc=True)
    from app import proposal_outcomes
    return [row for row in _page_all(build) if proposal_outcomes.state(row) == "completed"]


def review_time_stats(tenant_id: str | None = None, client: Client | None = None) -> dict:
    """HRT raw numbers for the operator console: {tenant_id: {'n': tracked
    sends, 'total': seconds}} over every send carrying a review stamp —
    all-time, because the pitch number ("your team averages 42s per email")
    wants the whole record, not a billing window. Cross-tenant when tenant_id
    is None (service role). Only stamped rows travel, so the result stays small.
    Best-effort: {} on any failure and the console just omits its HRT numbers."""
    try:
        rows: list[dict] = []
        while True:   # page past the 1000-row PostgREST cap
            q = ((client or db()).table("email_log")
                 .select("tenant_id,review_seconds")
                 .not_.is_("review_seconds", "null"))
            if tenant_id:
                q = q.eq("tenant_id", tenant_id)
            page = (q.order("created_at", desc=True)
                    .range(len(rows), len(rows) + 999).execute().data or [])
            rows.extend(page)
            if len(page) < 1000:
                break
    except Exception:
        log.warning("review time stats unavailable", exc_info=True)
        return {}
    out: dict[str, dict] = {}
    for r in rows:
        m = out.setdefault(r["tenant_id"], {"n": 0, "total": 0})
        m["n"] += 1
        m["total"] += r.get("review_seconds") or 0
    return out


def email_log_quality(tenant_id: str, since_iso: str, client: Client | None = None) -> dict:
    """Draft-outcome counters for one tenant since `since_iso` — the operator
    console's agent-quality card (aggregates only, never content).

    Approval rate = sent / (sent + skipped): how often a draft was good enough
    to go out at all. Edit rate = edited / human-decided sends: how often the
    human changed the text before approving — the 'how correct is the agent'
    number. Legacy rows sent before the stamps existed (sent_via null) count as
    sent but are excluded from the via/edit splits rather than guessed."""
    rows: list[dict] = []
    while True:   # page past the 1000-row PostgREST cap — a busy month can exceed a page
        page = ((client or db()).table("email_log").select("status,sent_via,edited")
                .eq("tenant_id", tenant_id).gte("created_at", since_iso)
                .order("created_at", desc=True)
                .range(len(rows), len(rows) + 999).execute().data or [])
        rows.extend(page)
        if len(page) < 1000:
            break
    sent = [r for r in rows if r.get("status") == "sent"]
    skipped = sum(1 for r in rows if r.get("status") == "skipped")
    pending = sum(1 for r in rows if r.get("status") == "pending")
    auto = sum(1 for r in sent if r.get("sent_via") == "auto")
    human = [r for r in sent if r.get("sent_via") == "human"]
    edit_known = [r for r in human if r.get("edited") is not None]
    edited = sum(1 for r in edit_known if r["edited"])
    decided = len(sent) + skipped
    return {
        "handled": len(rows), "sent": len(sent), "skipped": skipped, "pending": pending,
        "auto_sent": auto, "human_sent": len(human), "edited": edited,
        "verbatim": len(edit_known) - edited,
        "approval_pct": (len(sent) / decided * 100) if decided else None,
        "edit_pct": (edited / len(edit_known) * 100) if edit_known else None,
    }


def update_email_log_draft(
    log_id: str, draft_reply: str, tenant_id: str, client: Client | None = None, knowledge_gap=_UNCHANGED,
    thread_state=_UNCHANGED, request_summary=_UNCHANGED, action_summary=_UNCHANGED,
    searched_for=_UNCHANGED,
    booking_details=_UNCHANGED, contact_name=_UNCHANGED, contact_company=_UNCHANGED,
    category=_UNCHANGED, category_reason=_UNCHANGED, draft_sources=_UNCHANGED,
    draft_reasoning=_UNCHANGED,
) -> None:
    """Persist an edited/re-generated draft. Pass `knowledge_gap` (a topic or None) to
    re-evaluate the gap too; omit it to leave the existing value untouched. Same omit-to-
    keep contract for the inbox state/outcome stamps (a redraft refreshes them)."""
    update = {"draft_reply": draft_reply}
    if category is not _UNCHANGED:
        update["category"] = category
        update["category_source"] = "auto"   # a redraft is a pipeline event — it restamps
    if category_reason is not _UNCHANGED:
        update["category_reason"] = category_reason or None
    if knowledge_gap is not _UNCHANGED:
        update["knowledge_gap"] = knowledge_gap
    if thread_state is not _UNCHANGED:
        update["thread_state"] = thread_state
    if request_summary is not _UNCHANGED:
        update["request_summary"] = request_summary
    if action_summary is not _UNCHANGED:
        update["action_summary"] = action_summary
    if searched_for is not _UNCHANGED:
        update["searched_for"] = searched_for or None
    if booking_details is not _UNCHANGED:
        update["booking_details"] = booking_details or None
    if contact_name is not _UNCHANGED:
        update["contact_name"] = contact_name or None
    if contact_company is not _UNCHANGED:
        update["contact_company"] = contact_company or None
    if draft_sources is not _UNCHANGED:
        update["draft_sources"] = draft_sources
    if draft_reasoning is not _UNCHANGED:
        update["draft_reasoning"] = draft_reasoning or None
    (client or db()).table("email_log").update(update) \
        .eq("id", log_id).eq("tenant_id", tenant_id).execute()


def get_knowledge_chunks_by_ids(tenant_id: str, chunk_ids: list[str],
                                client: Client | None = None) -> list[dict]:
    """The live content of specific KB chunks, tenant-scoped — the click-through
    behind a draft's "Grounded on" pills. Chunks edited or deleted since the
    draft simply aren't returned (the UI says "updated since drafting")."""
    if not chunk_ids:
        return []
    res = (
        (client or db()).table("knowledge_chunks")
        .select("id,source,content")
        .eq("tenant_id", tenant_id)
        .in_("id", chunk_ids[:20])
        .execute()
    )
    return res.data or []


# ── usage_events (token & cost tracking) ───────────────────────────────────
_USAGE_COLS = (
    "id,created_at,provider,model,operation,module,input_tokens,output_tokens,"
    "cache_read_tokens,cache_write_tokens,embed_tokens,cost_usd,email_log_id,raw_usage,cost_version"
)


def insert_usage_event(row: dict) -> None:
    """Service-role insert (capture runs server-side in the pipeline / knowledge base)."""
    db().table("usage_events").insert(row).execute()


def count_usage_events(tenant_id: str, operation: str, since_iso: str,
                       client: Client | None = None) -> int:
    """How many `operation` events this tenant has recorded since `since_iso` —
    the onboarding daily-cap meter (src/app/fairuse.py). PostgREST count, no rows."""
    res = ((client or db()).table("usage_events").select("id", count="exact")
           .eq("tenant_id", tenant_id).eq("operation", operation)
           .gte("created_at", since_iso).limit(1).execute())
    return int(res.count or 0)


_USAGE_PAGE = 1000   # PostgREST caps a single select at 1000 rows — page past it


def list_usage_events(
    tenant_id: str, since: str | None = None, limit: int | None = None, client: Client | None = None
) -> list[dict]:
    """With `limit`: the newest N events (one query). Without: EVERY event since
    `since`, paged past the 1000-row PostgREST cap — a busy month must never be
    silently truncated (emails/cost/margin all aggregate from this)."""
    def q():
        base = ((client or db()).table("usage_events").select(_USAGE_COLS)
                .eq("tenant_id", tenant_id).order("created_at", desc=True))
        return base.gte("created_at", since) if since else base

    if limit:
        return q().limit(limit).execute().data or []
    out: list[dict] = []
    while True:
        page = q().range(len(out), len(out) + _USAGE_PAGE - 1).execute().data or []
        out.extend(page)
        if len(page) < _USAGE_PAGE:
            return out


# ── llm_calls (full-transcript LLM observability — src/app/llm.py) ─────────────
def insert_llm_call(row: dict) -> None:
    """Service-role insert (written best-effort from src/app/llm.py — its caller
    already swallows failures; see the loud-test conftest stub)."""
    db().table("llm_calls").insert(row).execute()


# ── agent_trace (execution-narrative observability — src/app/trace.py) ─────────
def insert_agent_trace(row: dict) -> None:
    """Service-role insert (written best-effort from src/app/trace.py — its caller
    already swallows failures; see the loud-test conftest stub)."""
    db().table("agent_trace").insert(row).execute()


def list_agent_trace(tenant_id: str | None = None, *, trace_type: str | None = None,
                     outcome: str | None = None, request_id: str | None = None,
                     email_log_id: str | None = None, limit: int = 100,
                     before: str | None = None) -> list[dict]:
    """Newest-first page of the trace, for the operator console. The three
    directive query shapes: by tenant ("everything the AI did for tenant X"),
    by trace_type ("all concierge executions"), by outcome ("all failures" —
    pass outcome='ok' inverted via neq is not needed; filter a specific one).
    Service-role only — traces carry operator-grade internals."""
    q = db().table("agent_trace").select("*").order("created_at", desc=True).limit(limit)
    if tenant_id:
        q = q.eq("tenant_id", tenant_id)
    if trace_type:
        q = q.eq("trace_type", trace_type)
    if outcome:
        q = q.eq("outcome", outcome)
    if request_id:
        q = q.eq("request_id", request_id)
    if email_log_id:
        q = q.eq("email_log_id", email_log_id)
    if before:
        q = q.lt("created_at", before)
    return q.execute().data or []


def list_agent_trace_failures(tenant_id: str | None = None, *, limit: int = 100) -> list[dict]:
    """Every non-ok trace row, newest-first — the "show me all failed
    executions / expired proposals" view in one call."""
    q = (db().table("agent_trace").select("*").neq("outcome", "ok")
         .order("created_at", desc=True).limit(limit))
    if tenant_id:
        q = q.eq("tenant_id", tenant_id)
    return q.execute().data or []


def list_llm_calls(tenant_id: str | None = None, *, operation: str | None = None,
                   limit: int = 50, before: str | None = None) -> list[dict]:
    """Newest-first page for the operator console. Service-role only — the rows
    carry full prompts (platform internals + customer email bodies)."""
    q = db().table("llm_calls").select("*").order("created_at", desc=True).limit(limit)
    if tenant_id:
        q = q.eq("tenant_id", tenant_id)
    if operation:
        q = q.eq("operation", operation)
    if before:
        q = q.lt("created_at", before)
    return q.execute().data or []


def count_llm_calls_since(operation: str, since_iso: str) -> int:
    """How many llm_calls rows an operation has written since `since_iso` — a
    DURABLE count (survives a restart, unlike the in-memory rate buckets) for a
    site-wide daily ceiling. Raises on a storage error; the caller decides."""
    res = (db().table("llm_calls").select("id", count="exact")
           .eq("operation", operation).gte("created_at", since_iso).limit(1).execute())
    return int(res.count or 0)


def get_llm_call(call_id: str) -> dict | None:
    res = db().table("llm_calls").select("*").eq("id", call_id).limit(1).execute()
    return res.data[0] if res.data else None


# ── tenant_members (user → company mapping for auth) ───────────────────────
class MembershipSwitchedOff(Exception):
    """The user belongs to a business, but every membership is switched off."""


def get_tenant_for_user(user_id: str) -> str | None:
    # Deterministic pick (oldest membership) — without an order, a user with two
    # memberships could land on a DIFFERENT tenant per request. ACTIVE only
    # (security review 2026-09, M1): a switched-off person resolves to no
    # business, so no page, API or export answers them.
    res = (
        db().table("tenant_members").select("tenant_id").eq("user_id", user_id)
        .eq("active", True).order("created_at").limit(1).execute()
    )
    return res.data[0]["tenant_id"] if res.data else None


def has_any_membership(user_id: str) -> bool:
    """Any membership at all, switched off included. A failed read answers False
    — the old behaviour (a new business), never a locked-out sign-in."""
    try:
        res = (db().table("tenant_members").select("tenant_id").eq("user_id", user_id)
               .limit(1).execute())
        return bool(res.data)
    except Exception:
        return False


def ensure_tenant_for_user(user_id: str, company_name: str | None,
                           email: str | None = None) -> str:
    """Return the user's tenant id, creating the company + owner membership on
    first login. `email` is display-only — it rides the operator sign-up push."""
    existing = get_tenant_for_user(user_id)
    if existing:
        return existing
    if has_any_membership(user_id):
        # Switched off everywhere (M1): never hand them a fresh empty business.
        raise MembershipSwitchedOff(user_id)
    t = db().table("tenants").insert({"name": company_name or "My company"}).execute()
    tenant_id = t.data[0]["id"]
    db().table("tenant_members").insert(
        {"tenant_id": tenant_id, "user_id": user_id, "role": "owner"}
    ).execute()
    # The owner is on their own team (Kris 2026-09-25, services rehearsal: a new
    # business's team list was empty, so no job could be put on the owner).
    # Named by their address until they give a name on People, which mirrors.
    try:
        sync_team_person_for_member(tenant_id, user_id, {
            "name": (email or "").strip().lower() or "Owner", "role": "owner",
            "email": (email or "").strip().lower() or None, "active": True})
    except Exception:
        log.warning("owner team row failed (tenant=%s)", tenant_id, exc_info=True)
    # Full-product trial (Julius, 2026-07-31): a brand-new tenant gets the Pro
    # module set (scheduling + CRM) from day one — the trial's job is to show
    # the aha-moments, not an email-only slice, and the LLM cost is already
    # bounded by TRIAL_EMAIL_CAP (+ the concierge trial cap). 'starter' is the
    # plan KEY for the Pro tier (legacy naming); it is not an industry tier, so
    # no vertical OS ever leaks into a trial. When a real subscription lands,
    # billing → apply_plan_modules reconciles these rows to the purchased tier
    # (a Starter £39 purchase correctly strips the CRM back off). Best-effort:
    # a seeding failure must never block sign-in.
    # Since 2026-09-25 the trial is Pro + Concierge ('concierge'): the
    # Concierge is its own tier now, and the trial shows everything.
    try:
        apply_plan_modules(tenant_id, "concierge")
    except Exception:
        log.exception("trial module seeding failed for tenant %s", tenant_id)
    # Morning debrief default (docs/modules/module-morning-debrief.md D1): on at 07:30.
    # Seeded HERE because the sweep selects debrief_enabled = True — a NULL row
    # gets no brief, and since 2026-08-02 no onboarding form asks (it's a
    # Settings concern). Best-effort like the module seeding above.
    # Go-live hold from BIRTH (Julius, 2026-09-02): a new tenant's drafting
    # starts held — mail files and threads but nothing is drafted until the
    # owner has been through what's arriving, ignored who they like, and
    # switched drafting on in Settings themselves. Stamped here rather than at
    # wizard finish alone so mail arriving MID-wizard (or after an abandoned
    # wizard) is held too; the finish-step stamp stays as a belt-and-braces.
    try:
        upsert_tenant_config(tenant_id, {"debrief_enabled": True, "debrief_time": "07:30",
                                         "draft_hold": True})
    except Exception:
        log.exception("debrief default seeding failed for tenant %s", tenant_id)
    # Operator heads-up (Kris, 2026-08-03): ping the platform devices the moment
    # a new business registers. Lazy import (push imports db) and best-effort
    # like the seeding above — a notification must never block a sign-up.
    try:
        from app import push
        push.notify_signup(tenant_id, company_name, email)
    except Exception:
        log.warning("sign-up operator push failed for tenant %s", tenant_id, exc_info=True)
    return tenant_id


def list_tenant_member_user_ids(tenant_id: str) -> list[str]:
    """All auth user_ids that belong to a tenant — captured before account
    closure so we can clean up their logins after the cascade delete."""
    res = db().table("tenant_members").select("user_id").eq("tenant_id", tenant_id).execute()
    return [r["user_id"] for r in (res.data or [])]


# ── People (docs/plan-people.md) — the business's own people, and the invites
# that bind a new sign-in to it. Service role: membership IS the RLS key, so a
# user-scoped client cannot be trusted to write the rows that grant itself
# access. Every caller is already behind require_onboarded + the admin check.
def list_members(tenant_id: str) -> list[dict]:
    """Everyone on this business, oldest first — the owner leads."""
    res = (db().table("tenant_members").select("*")
           .eq("tenant_id", tenant_id).order("created_at").execute())
    return res.data or []


def get_member(tenant_id: str, user_id: str) -> dict | None:
    """One person's membership row, or None when they are not on this business.
    None is also what every pre-People tenant's single user gets — the app
    reads that as the account's own owner (app/people.can_open)."""
    if not user_id:
        return None
    res = (db().table("tenant_members").select("*")
           .eq("tenant_id", tenant_id).eq("user_id", user_id).limit(1).execute())
    return res.data[0] if res.data else None


def update_member(tenant_id: str, user_id: str, fields: dict) -> None:
    (db().table("tenant_members").update(fields)
     .eq("tenant_id", tenant_id).eq("user_id", user_id).execute())


def touch_member_seen(tenant_id: str, user_id: str, when_iso: str) -> None:
    """The People page's quiet "last seen" column. Best-effort, never blocking."""
    try:
        update_member(tenant_id, user_id, {"last_seen_at": when_iso})
    except Exception:
        log.debug("member last_seen write failed", exc_info=True)


def create_member_invite(row: dict) -> dict | None:
    res = db().table("member_invites").insert(row).execute()
    return res.data[0] if res.data else None


def list_member_invites(tenant_id: str) -> list[dict]:
    """Invitations still worth showing — accepted ones have become people."""
    res = (db().table("member_invites").select("*")
           .eq("tenant_id", tenant_id).is_("accepted_at", None)
           .is_("revoked_at", None).order("created_at", desc=True).execute())
    return res.data or []


def get_member_invite(token: str) -> dict | None:
    if not token:
        return None
    res = db().table("member_invites").select("*").eq("token", token).limit(1).execute()
    return res.data[0] if res.data else None


def revoke_member_invite(tenant_id: str, invite_id: str, when_iso: str) -> None:
    (db().table("member_invites").update({"revoked_at": when_iso})
     .eq("tenant_id", tenant_id).eq("id", invite_id).execute())


def accept_member_invite(invite: dict, user_id: str, email: str | None, when_iso: str) -> None:
    """Bind a signed-in user to the business the invitation names, at the role
    and scopes it carries, and stamp the invitation used. Upsert, so following
    the same link twice is harmless and re-inviting someone who left simply
    restores them."""
    db().table("tenant_members").upsert({
        "tenant_id": invite["tenant_id"], "user_id": user_id,
        "role": invite.get("role") or "staff",
        "scopes": invite.get("scopes") or {},
        "display_name": invite.get("display_name") or None,
        "email": (email or invite.get("email") or "").strip().lower() or None,
        "active": True,
    }, on_conflict="tenant_id,user_id").execute()
    (db().table("member_invites")
     .update({"accepted_at": when_iso, "accepted_by": user_id})
     .eq("id", invite["id"]).execute())
    # The team list (docs/plan-people-team-and-jobs.md step 1): an invitation
    # made from "Give them a login" joins the login to the person already on
    # the team, so their jobs and history stay theirs; any other invitation
    # puts a new person on the team.
    try:
        if invite.get("team_person_id"):
            update_team_person(invite["tenant_id"], invite["team_person_id"],
                               {"user_id": user_id, "active": True})
        else:
            sync_team_person_for_member(invite["tenant_id"], user_id, {
                "name": invite.get("display_name") or (email or invite.get("email") or "").strip().lower() or "Someone",
                "role": invite.get("role") or "staff",
                "email": (email or invite.get("email") or "").strip().lower() or None,
                "does": list((invite.get("scopes") or {}).get("does") or []),
                "active": True,
            })
    except Exception:
        log.warning("team row for new member failed (tenant=%s)", invite.get("tenant_id"), exc_info=True)


# ── team_people (docs/plan-people-team-and-jobs.md step 1) — everyone who
# works here, whether or not they sign in. A row with no user_id grants no
# access: there is nothing to sign in with. Logged-in people keep their doors
# on tenant_members; their team row mirrors name/role/kinds so every person
# has one id that jobs can point at. Service role, like the member rows.
def list_team_people(tenant_id: str) -> list[dict]:
    res = (db().table("team_people").select("*")
           .eq("tenant_id", tenant_id).order("created_at").execute())
    return res.data or []


def get_team_person(tenant_id: str, person_id: str) -> dict | None:
    if not person_id:
        return None
    res = (db().table("team_people").select("*")
           .eq("tenant_id", tenant_id).eq("id", person_id).limit(1).execute())
    return res.data[0] if res.data else None


def create_team_person(row: dict) -> dict | None:
    res = db().table("team_people").insert(row).execute()
    return res.data[0] if res.data else None


def update_team_person(tenant_id: str, person_id: str, fields: dict) -> None:
    (db().table("team_people").update({**fields, "updated_at": datetime.now(timezone.utc).isoformat()})
     .eq("tenant_id", tenant_id).eq("id", person_id).execute())


def list_booking_people(tenant_id: str, booking_ids: list[str]) -> list[dict]:
    """Who's on these bookings (docs/plan-people-team-and-jobs.md step 2)."""
    ids = [b for b in booking_ids if b]
    if not ids:
        return []
    res = (db().table("booking_people").select("*")
           .eq("tenant_id", tenant_id).in_("booking_id", ids[:500]).execute())
    return res.data or []


def list_bookings_for_person(tenant_id: str, person_id: str) -> list[str]:
    res = (db().table("booking_people").select("booking_id")
           .eq("tenant_id", tenant_id).eq("person_id", person_id).execute())
    return [r["booking_id"] for r in (res.data or [])]


def add_booking_person(row: dict) -> None:
    db().table("booking_people").upsert(row, on_conflict="booking_id,person_id").execute()


def remove_booking_people(tenant_id: str, booking_id: str, person_ids: list[str] | None = None) -> None:
    q = (db().table("booking_people").delete()
         .eq("tenant_id", tenant_id).eq("booking_id", booking_id))
    if person_ids is not None:
        if not person_ids:
            return
        q = q.in_("person_id", person_ids)
    q.execute()


# ── Job sheets (src/app/job_sheets.py) ──────────────────────────────────────────
def list_job_sheets(tenant_id: str, booking_ids: list[str]) -> list[dict]:
    ids = [b for b in booking_ids if b]
    if not ids:
        return []
    res = (db().table("job_sheets").select("*")
           .eq("tenant_id", tenant_id).in_("booking_id", ids[:500]).execute())
    return res.data or []


def get_job_sheet_by_token(token: str) -> dict | None:
    """The public /j/{token} lookup — the token is the capability, so this is
    deliberately not tenant-scoped (the /pay and /q doctrine)."""
    if not token:
        return None
    res = db().table("job_sheets").select("*").eq("token", token).limit(1).execute()
    return res.data[0] if res.data else None


def insert_job_sheet(row: dict) -> dict | None:
    res = db().table("job_sheets").upsert(row, on_conflict="booking_id,person_id").execute()
    return (res.data or [None])[0]


def update_job_sheet(sheet_id: str, fields: dict) -> None:
    db().table("job_sheets").update(fields).eq("id", sheet_id).execute()


def claim_job_sheet(sheet_id: str, column: str, at: str) -> bool:
    """Stamp on_my_way_at / done_at ONLY if it is still empty — one tap wins,
    every repeat (a double tap, a phone re-sending the form) is told no. The
    claim happens before anything is sent (Kris 2026-09-26: six taps on
    On my way sent Sarah six emails)."""
    if column not in ("on_my_way_at", "done_at"):
        raise ValueError(column)
    res = (db().table("job_sheets").update({column: at})
           .eq("id", sheet_id).is_(column, "null").execute())
    return bool(res.data)


def open_job_sheet_booking_ids() -> list[tuple[str, str]]:
    """(tenant_id, booking_id) for every sheet still open — the sweep's list."""
    res = db().table("job_sheets").select("tenant_id,booking_id").is_("closed_at", "null").execute()
    return sorted({(r["tenant_id"], r["booking_id"]) for r in res.data or []})


def recent_booking_people(since_iso: str) -> list[tuple[str, str]]:
    """(tenant_id, booking_id) for people put on jobs since `since_iso` — the
    sweep's net for assignments made anywhere (a card, a form, a future path)."""
    res = (db().table("booking_people").select("tenant_id,booking_id")
           .gte("created_at", since_iso).execute())
    return sorted({(r["tenant_id"], r["booking_id"]) for r in res.data or []})


def sync_team_person_for_member(tenant_id: str, user_id: str, fields: dict) -> None:
    """Keep a logged-in person's team row in step with their member row —
    update it, or add it when they have none yet."""
    res = (db().table("team_people").update({**fields, "updated_at": datetime.now(timezone.utc).isoformat()})
           .eq("tenant_id", tenant_id).eq("user_id", user_id).execute())
    if not res.data:
        db().table("team_people").insert({**fields, "tenant_id": tenant_id,
                                          "user_id": user_id}).execute()


# ── staff_notes (docs/plan-people.md slice 3) — telling a person something,
# fastened to the work it is about. Service role: these rows are read and
# written on behalf of a signed-in member whose access has already been
# checked by the People guard, and the reader is often a staff member whose
# RLS reach is deliberately narrower than the note's subject.
def create_staff_note(row: dict) -> dict | None:
    res = db().table("staff_notes").insert(row).execute()
    return res.data[0] if res.data else None


def list_staff_notes_for(tenant_id: str, user_id: str, *, limit: int = 50) -> list[dict]:
    """What this person has been asked, newest first."""
    if not user_id:
        return []
    res = (db().table("staff_notes").select("*")
           .eq("tenant_id", tenant_id).eq("to_user", user_id)
           .is_("cancelled_at", None)
           .order("created_at", desc=True).limit(limit).execute())
    return res.data or []


def list_staff_notes_about(tenant_id: str, kind: str, about_id: str) -> list[dict]:
    """Everything said about one booking, customer or thread — the record that
    outlives the conversation."""
    if not kind or not about_id:
        return []
    res = (db().table("staff_notes").select("*")
           .eq("tenant_id", tenant_id).eq("about_kind", kind).eq("about_id", about_id)
           .order("created_at").execute())
    return res.data or []


def list_staff_notes_sent(tenant_id: str, user_id: str, *, limit: int = 50) -> list[dict]:
    """What this person has asked of others — so an owner can see what is out."""
    if not user_id:
        return []
    res = (db().table("staff_notes").select("*")
           .eq("tenant_id", tenant_id).eq("from_user", user_id)
           .is_("cancelled_at", None)
           .order("created_at", desc=True).limit(limit).execute())
    return res.data or []


def get_staff_note(tenant_id: str, note_id: str) -> dict | None:
    res = (db().table("staff_notes").select("*")
           .eq("tenant_id", tenant_id).eq("id", note_id).limit(1).execute())
    return res.data[0] if res.data else None


def update_staff_note(tenant_id: str, note_id: str, fields: dict) -> None:
    (db().table("staff_notes").update(fields)
     .eq("tenant_id", tenant_id).eq("id", note_id).execute())


def count_unseen_staff_notes(tenant_id: str, user_id: str) -> int:
    """The dot on their Today. 0 on any failure — a count never breaks a page."""
    if not user_id:
        return 0
    try:
        res = (db().table("staff_notes").select("id", count="exact")
               .eq("tenant_id", tenant_id).eq("to_user", user_id)
               .is_("seen_at", None).is_("cancelled_at", None).limit(1).execute())
        return int(res.count or 0)
    except Exception:
        return 0


def user_has_other_tenants(user_id: str) -> bool:
    """True if the user still belongs to any tenant. Call AFTER deleting one so a
    user shared across companies keeps their login."""
    res = db().table("tenant_members").select("tenant_id").eq("user_id", user_id).limit(1).execute()
    return bool(res.data)


def delete_auth_user(user_id: str) -> None:
    """Hard-delete a Supabase Auth user via the service-role admin API."""
    db().auth.admin.delete_user(user_id)


# ── tenant_visits (operator journey card — src/app/journey.py) ───────────────────
# Service role throughout: written from the auth dependency, read only by the
# operator console. Tenant-facing code never touches these rows.
def latest_visit(tenant_id: str) -> dict | None:
    res = (db().table("tenant_visits").select("*")
           .eq("tenant_id", tenant_id).order("last_seen_at", desc=True)
           .limit(1).execute())
    return res.data[0] if res.data else None


def insert_visit(tenant_id: str, user_id: str | None) -> dict:
    res = (db().table("tenant_visits")
           .insert({"tenant_id": tenant_id, "user_id": user_id}).execute())
    return res.data[0]


def bump_visit(visit_id: str, last_seen_iso: str, request_inc: int) -> None:
    """Extend a visit. The requests counter is read-modify-write from the
    in-process cache's pending count — visits are single-browser by nature, so
    a lost increment under racing workers only softens an approximate number."""
    row = (db().table("tenant_visits").select("requests")
           .eq("id", visit_id).limit(1).execute())
    current = row.data[0]["requests"] if row.data else 0
    (db().table("tenant_visits")
     .update({"last_seen_at": last_seen_iso, "requests": current + request_inc})
     .eq("id", visit_id).execute())


def list_recent_visits(tenant_id: str, limit: int = 400) -> list[dict]:
    """Newest first — enough history for the journey card's totals without an
    unbounded read (400 visits ≈ months of heavy use)."""
    res = (db().table("tenant_visits").select("started_at,last_seen_at,requests")
           .eq("tenant_id", tenant_id).order("last_seen_at", desc=True)
           .limit(limit).execute())
    return res.data or []


def tenant_owner_user_id(tenant_id: str) -> str | None:
    res = (db().table("tenant_members").select("user_id")
           .eq("tenant_id", tenant_id).eq("role", "owner").limit(1).execute())
    return res.data[0]["user_id"] if res.data else None


def get_auth_user(user_id: str) -> dict | None:
    """Auth identity for the operator console (service-role admin API): email,
    SSO provider, and Supabase's own sign-in stamps. Plain dict — the supabase-py
    User object's datetimes are normalised to ISO strings."""
    try:
        user = db().auth.admin.get_user_by_id(user_id).user
    except Exception:
        return None
    if not user:
        return None
    iso = lambda v: v.isoformat() if hasattr(v, "isoformat") else v
    return {
        "email": user.email,
        "provider": (user.app_metadata or {}).get("provider"),
        "created_at": iso(user.created_at),
        "last_sign_in_at": iso(user.last_sign_in_at),
    }


def count_tenant_rows(table: str, tenant_id: str) -> int:
    """Cheap head-count of one tenant's rows in a tenant-scoped table — the
    journey card's activity line (contacts, bookings, knowledge chunks)."""
    res = (db().table(table).select("id", count="exact", head=True)
           .eq("tenant_id", tenant_id).execute())
    return res.count or 0


def delete_tenant(tenant_id: str) -> None:
    """Hard-delete a tenant and ALL its data. Every tenant-scoped table FKs to
    tenants(id) ON DELETE CASCADE (db/schema.sql), so this single delete removes
    email_connections (tokens), email_log, knowledge_chunks, bookings, fleet,
    invoices, config, usage_events, members, etc. in one transaction.
    portal_messages had no foreign key (security review M15), so it goes first
    by hand; stored files are removed by the caller (storage.remove_tenant_files)."""
    db().table("portal_messages").delete().eq("tenant_id", tenant_id).execute()
    db().table("tenants").delete().eq("id", tenant_id).execute()


def count_gmail_connections() -> int:
    """Connected Gmail mailboxes across all tenants — the Google unverified-app
    cap counter (src/app/slots.py). It's the mailbox CONNECT (gmail.readonly +
    gmail.send, restricted scopes) that consumes one of Google's 100 pre-CASA
    grants; SSO sign-in only asks for email+profile and is uncapped, so the
    meter reads email_connections, not auth.identities. A disconnect frees the
    row (Google's own count is stickier) — honest-enough for an indicator.
    Returns 0 on any failure so no page ever breaks over a slot count."""
    try:
        res = (db().table("email_connections").select("id", count="exact")
               .eq("provider", "google").limit(1).execute())
        return int(res.count or 0)
    except Exception:
        return 0


def count_google_member_calendars() -> int:
    """People who have connected their OWN Google calendar (People slice 5).

    These draw on the SAME 100 grants. Google's cap is on users who authorise
    an unapproved SENSITIVE **or** restricted scope over the project's whole
    lifetime — and calendar.events is sensitive — so a staff member consenting
    on their own Google account is a new user against the cap even though no
    mailbox was connected. Counting rows slightly OVER-counts (an owner whose
    Gmail mailbox is already connected and who then connects that same account's
    calendar is one Google user, not two); over-counting is the safe direction
    for a cap you cannot reset. Returns 0 on any failure."""
    try:
        res = (db().table("member_calendars").select("user_id", count="exact")
               .eq("provider", "google").limit(1).execute())
        return int(res.count or 0)
    except Exception:
        return 0


# ── trial (free-trial email counter — src/app/trial.py owns the state machine) ──
def increment_trial_concierge(tenant_id: str) -> int | None:
    """Atomically count one trial concierge question and return the new total
    (docs: full-product trial, 2026-07-31 — the concierge sibling of
    increment_trial_email; the cap comparison happens in src/app/trial.py so the
    counter needs no status machinery). Service-role RPC."""
    res = db().rpc("increment_trial_concierge", {"p_tenant_id": tenant_id}).execute()
    data = res.data
    if isinstance(data, list):
        data = data[0] if data else None
    return data


def refund_concierge_turn(tenant_id: str, client: Client | None = None) -> None:
    """Give one concierge turn back on both pots (trial total + today's fair
    use) after a PROVIDER failure — the owner asked a fine question and got
    nothing (2026-09-06). Read-modify-write, floored at zero, best-effort."""
    c = client or db()
    res = (c.table("tenants").select("trial_concierge_used,concierge_daily_used")
           .eq("id", tenant_id).limit(1).execute())
    row = (res.data or [None])[0]
    if not row:
        return
    c.table("tenants").update({
        "trial_concierge_used": max(0, int(row.get("trial_concierge_used") or 0) - 1),
        "concierge_daily_used": max(0, int(row.get("concierge_daily_used") or 0) - 1),
    }).eq("id", tenant_id).execute()


def increment_concierge_daily(tenant_id: str, today_iso: str) -> int | None:
    """Atomically count one business-chat question against TODAY's fair-use
    allowance and return the new total (docs/modules/module-concierge-cost-controls.md
    Part B). The RPC resets the counter when the stored date isn't p_today, so
    day rollover needs no sweep; the cap comparison happens in src/app/fairuse.py.
    Service-role RPC, the increment_trial_concierge sibling."""
    res = db().rpc("increment_concierge_daily",
                   {"p_tenant_id": tenant_id, "p_today": today_iso}).execute()
    data = res.data
    if isinstance(data, list):
        data = data[0] if data else None
    return data


def concierge_live_rollout() -> bool:
    rows = db().table("concierge_live_rollout").select("enabled").eq("id", True).limit(1).execute().data or []
    return bool(rows and rows[0].get("enabled"))


def concierge_live_tier_limits() -> dict[str, int]:
    rows = db().table("concierge_live_tier_limits").select("tier,minutes").execute().data or []
    return {str(r["tier"]): int(r["minutes"]) for r in rows if r.get("tier")}


def set_concierge_live_rollout(enabled: bool, user_id: str | None) -> None:
    db().table("concierge_live_rollout").upsert({
        "id": True, "enabled": bool(enabled), "updated_at": datetime.now(timezone.utc).isoformat(),
        "updated_by": user_id,
    }, on_conflict="id").execute()


def set_concierge_live_tier_limit(tier: str, minutes: int, user_id: str | None) -> None:
    db().table("concierge_live_tier_limits").upsert({
        "tier": tier, "minutes": int(minutes), "updated_at": datetime.now(timezone.utc).isoformat(),
        "updated_by": user_id,
    }, on_conflict="tier").execute()


def reserve_concierge_live_session(session_id: str, tenant_id: str, user_id: str,
                                  london_day: str, limit_seconds: int) -> dict:
    result = db().rpc("concierge_live_reserve", {
        "p_id": session_id, "p_tenant_id": tenant_id, "p_user_id": user_id,
        "p_day": london_day, "p_limit_seconds": int(limit_seconds), "p_lease_seconds": 90,
    }).execute().data
    return result[0] if isinstance(result, list) and result else result or {"ok": False}


def heartbeat_concierge_live_session(session_id: str, tenant_id: str, user_id: str,
                                    limit_seconds: int = 0) -> dict:
    result = db().rpc("concierge_live_heartbeat", {
        "p_id": session_id, "p_tenant_id": tenant_id, "p_user_id": user_id,
        "p_limit_seconds": int(limit_seconds), "p_lease_seconds": 90,
    }).execute().data
    return result[0] if isinstance(result, list) and result else result or {"ok": False}


def close_concierge_live_session(session_id: str, tenant_id: str, user_id: str) -> dict:
    result = db().rpc("concierge_live_close", {
        "p_id": session_id, "p_tenant_id": tenant_id, "p_user_id": user_id,
    }).execute().data
    return result[0] if isinstance(result, list) and result else result or {"ok": False}


def concierge_live_session_open(session_id: str, tenant_id: str, user_id: str) -> bool:
    rows = (db().table("concierge_live_sessions").select("id").eq("id", session_id)
            .eq("tenant_id", tenant_id).eq("user_id", user_id).is_("closed_at", "null")
            .limit(1).execute().data or [])
    return bool(rows)


def concierge_live_status(tenant_id: str, user_id: str, london_day: str, limit_seconds: int) -> dict:
    result = db().rpc("concierge_live_status", {
        "p_tenant_id": tenant_id, "p_user_id": user_id, "p_day": london_day,
        "p_limit_seconds": int(limit_seconds),
    }).execute().data
    return result[0] if isinstance(result, list) and result else result or {}


def increment_trial_email(tenant_id: str, cap: int = 1000) -> int | None:
    """Atomically count one drafted trial email and return the new total. A
    single UPDATE (col = col + 1) so concurrent webhook deliveries can never lose
    an increment; flips trial_status to 'expired_cap' once the cap is reached.
    Service-role RPC. Returns None if the tenant row is missing."""
    res = db().rpc("increment_trial_email", {"p_tenant_id": tenant_id, "p_cap": cap}).execute()
    data = res.data
    if isinstance(data, list):
        data = data[0] if data else None
    return int(data) if data is not None else None


# ── Email top-up credit (Kris 2026-09-01) ──────────────────────────────────
# Prepaid emails, spent only once the monthly plan allowance is gone. Both
# writers are Postgres functions (db/schema.sql) rather than read-modify-write
# here: granting has to be idempotent against a replayed Stripe webhook, and
# spending has to be atomic against two drafts racing for the last credit.

def grant_email_credit(tenant_id: str, emails: int, payment_intent: str) -> int:
    """Credit a paid top-up and return the balance after. Idempotent on the
    Stripe payment intent — a webhook Stripe delivers twice credits once."""
    res = db().rpc("grant_email_credit", {
        "p_tenant_id": tenant_id, "p_emails": int(emails), "p_payment": payment_intent,
    }).execute()
    data = res.data
    if isinstance(data, list):
        data = data[0] if data else None
    return int(data) if data is not None else 0


def spend_email_credit(tenant_id: str, email_log_id: str | None = None) -> int:
    """Spend one email of credit. Returns the balance after, or -1 when there
    was nothing left to spend — the caller uses -1 to mean 'genuinely out'."""
    res = db().rpc("spend_email_credit", {
        "p_tenant_id": tenant_id, "p_email_log_id": email_log_id,
    }).execute()
    data = res.data
    if isinstance(data, list):
        data = data[0] if data else None
    return int(data) if data is not None else -1


def email_credit_ledger(tenant_id: str, limit: int = 50, client: Client | None = None) -> list[dict]:
    """The audit trail behind the balance, newest first."""
    res = ((client or db()).table("email_credit_ledger")
           .select("*").eq("tenant_id", tenant_id)
           .order("created_at", desc=True).limit(limit).execute())
    return res.data or []


# ── Connection health (src/app/health.py) ──────────────────────────────────────
def get_connection_health(connection_id: str,
                          client: Client | None = None) -> dict | None:
    """The three health columns for one connection — no token columns read.
    Its own narrow select because the health path runs inside background sweeps
    that have no business decrypting a mailbox's tokens to ask whether it is
    poorly."""
    res = (
        (client or db()).table("email_connections")
        .select("id,tenant_id,address,provider,sync_failing_since,sync_error,"
                "health_alerted_at")
        .eq("id", connection_id).limit(1).execute()
    )
    return res.data[0] if res.data else None


def set_connection_health(connection_id: str, *, failing_since: str | None,
                          error: str | None, alerted_at: str | None,
                          client: Client | None = None) -> None:
    """Write the whole health triple at once. All three move together — a
    partial write is what would leave a recovered mailbox still stamped as
    alerted, and therefore silent the next time it really does die."""
    (client or db()).table("email_connections").update({
        "sync_failing_since": failing_since,
        "sync_error": (error or "")[:300] or None,
        "health_alerted_at": alerted_at,
    }).eq("id", connection_id).execute()


def list_unhealthy_connections(tenant_id: str,
                               client: Client | None = None) -> list[dict]:
    """This tenant's connections that are currently failing — for the strip on
    Today and the mailbox face in Settings. Reads the streak, not the alert:
    the page should say something is wrong from the first failed sweep, even
    while the notification is still inside its grace period. A page is not an
    interruption, so it does not need one."""
    return (
        (client or db()).table("email_connections")
        .select("id,address,provider,sync_failing_since,sync_error")
        .eq("tenant_id", tenant_id)
        .not_.is_("sync_failing_since", "null")
        .order("sync_failing_since")
        .execute().data or []
    )


# ── Graph subscriptions (auto-draft webhook) ───────────────────────────────
def set_subscription(connection_id: str, subscription_id: str, sub_expires_iso: str) -> None:
    db().table("email_connections").update(
        {"subscription_id": subscription_id, "sub_expires": sub_expires_iso}
    ).eq("id", connection_id).execute()


def get_connection_by_subscription(subscription_id: str) -> dict | None:
    res = (
        db().table("email_connections")
        .select("*")
        .eq("subscription_id", subscription_id)
        .limit(1)
        .execute()
    )
    return _decrypt_conn(res.data[0]) if res.data else None


# ── OAuth state (CSRF + tenant binding across the consent round-trip) ───────
def put_oauth_state(state: str, tenant_id: str, mode: str = "replace",
                    user_id: str | None = None) -> None:
    """`mode` carries the connect intent through the consent round-trip:
    'replace' (SaaS — swap the mailbox), 'add' (enterprise — add a sibling) or
    'mycal' (People slice 5 — one person's own calendar, `user_id` says whose).
    Bound server-side here so the callback never trusts either from the URL."""
    db().table("oauth_states").insert(
        {"state": state, "tenant_id": tenant_id, "mode": mode, "user_id": user_id}
    ).execute()


def pop_oauth_state(state: str) -> tuple[str | None, str, str | None]:
    """Return (tenant_id, mode, user_id) for a valid, unexpired state and
    consume it (single-use). (None, 'replace', None) if missing or expired."""
    res = (
        db().table("oauth_states")
        .select("tenant_id,mode,user_id")
        .eq("state", state)
        .gt("expires_at", datetime.now(timezone.utc).isoformat())
        .limit(1)
        .execute()
    )
    db().table("oauth_states").delete().eq("state", state).execute()  # consume regardless
    if not res.data:
        return None, "replace", None
    row = res.data[0]
    return row["tenant_id"], row.get("mode") or "replace", row.get("user_id")


# ── a person's own calendar (People slice 5) ───────────────────────────────
def _decrypt_cal(row: dict) -> dict:
    row["access_token"] = crypto.decrypt(row.get("access_token"))
    row["refresh_token"] = crypto.decrypt(row.get("refresh_token"))
    return row


def save_member_calendar(tenant_id: str, user_id: str, provider: str,
                         access_token: str, refresh_token: str | None,
                         token_expires: datetime, calendar_email: str | None = None) -> None:
    """Store one person's calendar grant, tokens encrypted at rest exactly as
    the mailbox's are. Re-connecting overwrites — and clears the last error, so
    a person who fixed a dead grant stops being told it is broken."""
    db().table("member_calendars").upsert({
        "tenant_id": tenant_id, "user_id": user_id, "provider": provider,
        "access_token": crypto.encrypt(access_token),
        "refresh_token": crypto.encrypt(refresh_token),
        "token_expires": token_expires.isoformat(),
        "calendar_email": calendar_email,
        "connected_at": datetime.now(timezone.utc).isoformat(),
        "last_error": None,
    }, on_conflict="tenant_id,user_id").execute()


def get_member_calendar(tenant_id: str, user_id: str) -> dict | None:
    if not user_id:
        return None
    res = (db().table("member_calendars").select("*")
           .eq("tenant_id", tenant_id).eq("user_id", user_id).limit(1).execute())
    return _decrypt_cal(res.data[0]) if res.data else None


def list_member_calendars(tenant_id: str) -> list[dict]:
    res = (db().table("member_calendars").select("*")
           .eq("tenant_id", tenant_id).execute())
    return [_decrypt_cal(r) for r in (res.data or [])]


def update_member_calendar_tokens(tenant_id: str, user_id: str, access_token: str,
                                  refresh_token: str | None, token_expires: datetime) -> None:
    db().table("member_calendars").update({
        "access_token": crypto.encrypt(access_token),
        "refresh_token": crypto.encrypt(refresh_token),
        "token_expires": token_expires.isoformat(),
    }).eq("tenant_id", tenant_id).eq("user_id", user_id).execute()


def set_member_calendar_error(tenant_id: str, user_id: str, message: str | None) -> None:
    """Why their calendar stopped taking events, in words their own page can
    show. None clears it."""
    db().table("member_calendars").update({"last_error": message}) \
        .eq("tenant_id", tenant_id).eq("user_id", user_id).execute()


def delete_member_calendar(tenant_id: str, user_id: str) -> None:
    db().table("member_calendars").delete() \
        .eq("tenant_id", tenant_id).eq("user_id", user_id).execute()


def set_member_booking_event(tenant_id: str, user_id: str, booking_id: str,
                             event_id: str) -> None:
    db().table("member_booking_events").upsert({
        "tenant_id": tenant_id, "user_id": user_id, "booking_id": booking_id,
        "event_id": event_id,
    }, on_conflict="tenant_id,user_id,booking_id").execute()


def list_member_booking_events(tenant_id: str, booking_id: str | None = None,
                               user_id: str | None = None) -> list[dict]:
    """What has already been written into people's own calendars — by booking
    (to take an event back out) or by person (to clear up after a disconnect)."""
    q = db().table("member_booking_events").select("*").eq("tenant_id", tenant_id)
    if booking_id:
        q = q.eq("booking_id", booking_id)
    if user_id:
        q = q.eq("user_id", user_id)
    return q.execute().data or []


def delete_member_booking_event(tenant_id: str, user_id: str, booking_id: str) -> None:
    db().table("member_booking_events").delete().eq("tenant_id", tenant_id) \
        .eq("user_id", user_id).eq("booking_id", booking_id).execute()


def list_confirmed_bookings(tenant_id: str, email_log_id: str | None = None,
                            booking_id: str | None = None,
                            from_date: str | None = None) -> list[dict]:
    """Confirmed, not-imported bookings in one scope — whether or not the
    BUSINESS calendar already holds them. list_bookings_to_push filters on an
    empty calendar_event_id, which is right for the one tenant calendar and
    wrong for everybody else's: a person who connects theirs on Tuesday must
    still receive Wednesday's work."""
    q = (db().table("bookings").select("*")
         .eq("tenant_id", tenant_id).eq("status", "confirmed").neq("source", "calendar"))
    if email_log_id:
        q = q.eq("email_log_id", email_log_id)
    if booking_id:
        q = q.eq("id", booking_id)
    if from_date:
        q = q.gte("booking_date", from_date)
    return q.execute().data or []


# ── Graph subscription renewal (cron) ───────────────────────────────────────
def list_connections_expiring(before_iso: str) -> list[dict]:
    """Connections whose Graph subscription expires before `before_iso` (tokens decrypted)."""
    res = db().table("email_connections").select("*").lt("sub_expires", before_iso).execute()
    return [_decrypt_conn(r) for r in (res.data or [])]


# ── platform admins + operator console (cross-tenant; SERVICE ROLE ONLY) ────
# These bypass RLS. Callers MUST be behind app.admin_auth.require_admin — never
# expose them to a tenant request.
def is_platform_admin(user_id: str) -> dict | None:
    """Is this Supabase user a platform admin? Returns {user_id,email,role} or None."""
    res = db().table("platform_admins").select("user_id,email,role").eq("user_id", user_id).limit(1).execute()
    return res.data[0] if res.data else None


def list_all_tenants() -> list[dict]:
    res = db().table("tenants").select(
        "id,name,plan,contract_monthly_gbp,reporting_tier,billing_started_at,created_at,"
        "industry,onboarded_at"      # journey.stalled_step (operator overview flag)
    ).neq("id", PLATFORM_TENANT_ID).order("created_at").execute()   # hide the sentinel KB tenant
    return res.data or []


def usage_events_since_all(since_iso: str) -> list[dict]:
    """Every tenant's usage events since `since_iso` (cross-tenant aggregation
    source), paged past the 1000-row PostgREST cap so platform totals never
    silently truncate."""
    out: list[dict] = []
    while True:
        page = (db().table("usage_events").select(
                    "tenant_id,created_at,operation,email_log_id,cost_usd,"
                    "input_tokens,output_tokens,embed_tokens,raw_usage,cost_version")
                .gte("created_at", since_iso).order("created_at", desc=True)
                .range(len(out), len(out) + _USAGE_PAGE - 1).execute().data or [])
        out.extend(page)
        if len(page) < _USAGE_PAGE:
            return out


def set_tenant_contract(tenant_id: str, value) -> None:
    db().table("tenants").update({"contract_monthly_gbp": value}).eq("id", tenant_id).execute()


def set_tenant_reporting_tier(tenant_id: str, tier: str) -> None:
    db().table("tenants").update({"reporting_tier": tier}).eq("id", tenant_id).execute()


def set_tenant_comp(tenant_id: str, comp_access: str | None, comp_until: str | None) -> None:
    """Set/clear an operator comp grant (free access). comp_access is
    None|'indefinite'|'until_date'; comp_until is the ISO expiry for 'until_date'.
    Webhook-safe — no Stripe path writes these columns."""
    db().table("tenants").update(
        {"comp_access": comp_access, "comp_until": comp_until}
    ).eq("id", tenant_id).execute()
    navcache.invalidate("t:" + tenant_id + ":subgate")


def set_tenant_fair_use_exempt(tenant_id: str, exempt: bool) -> None:
    """Lift (or restore) the Concierge's daily fair-use allowance for one
    account — the operator's own rigs (src/app/fairuse.py). Operator-only, set
    from the admin console; nothing a tenant can reach writes it."""
    db().table("tenants").update({"fair_use_exempt": bool(exempt)}).eq("id", tenant_id).execute()


def list_email_log_for_period(tenant_id: str, since_iso: str, client: Client | None = None) -> list[dict]:
    """Handled-emails log for the enterprise CSV export and the by-mailbox usage
    slice (the tenant's own data; RLS via client)."""
    return (
        (client or db()).table("email_log")
        .select("created_at,from_address,subject,status,mailbox_id")
        .eq("tenant_id", tenant_id).gte("created_at", since_iso)
        .order("created_at", desc=True).execute().data or []
    )


# ── Stripe billing (subscription lifecycle — service role) ──────────────────
# All `stripe_*` helpers are prefixed to avoid collision with the existing
# `set_subscription` (Graph webhook subscription id, schema.sql:50 / db.py
# above). The webhook writes via these; require_subscription reads via
# get_tenant().subscription_status.

def stripe_get_customer_id(tenant_id: str) -> str | None:
    res = db().table("tenants").select("stripe_customer_id").eq("id", tenant_id).limit(1).execute()
    return res.data[0].get("stripe_customer_id") if res.data else None


def stripe_set_customer(tenant_id: str, stripe_customer_id: str,
                       billing_provider: str = "self_serve") -> None:
    """Link a Stripe Customer id to a tenant (first Checkout). billing_provider
    distinguishes self-serve Stripe customers from enterprise MSA invoice
    billing — the admin form can set it to 'enterprise' explicitly."""
    db().table("tenants").update(
        {"stripe_customer_id": stripe_customer_id, "billing_provider": billing_provider}
    ).eq("id", tenant_id).execute()


def stripe_set_subscription(tenant_id: str, *, subscription_id: str | None,
                           status: str, price_id: str | None,
                           trial_ends_at: str | None,
                           current_period_end: str | None) -> None:
    """Mirror the full subscription state onto the tenant row. Called from
    billing.sync_subscription() — the ONE write path the webhook uses."""
    db().table("tenants").update({
        "stripe_subscription_id": subscription_id,
        "subscription_status": status,
        "subscription_price_id": price_id,
        "trial_ends_at": trial_ends_at,
        "current_period_end": current_period_end,
    }).eq("id", tenant_id).execute()
    navcache.invalidate("t:" + tenant_id + ":subgate")   # the access gate caches this row briefly


def stripe_set_subscription_status(tenant_id: str, status: str) -> None:
    """Status-only update (for customer.subscription.deleted and the
    invoice.payment_failed branch). Used when we don't need to overwrite
    price/period fields."""
    db().table("tenants").update({"subscription_status": status}).eq("id", tenant_id).execute()
    navcache.invalidate("t:" + tenant_id + ":subgate")


def stripe_get_tenant_id_by_customer(stripe_customer_id: str) -> str | None:
    res = (
        db().table("tenants").select("id")
        .eq("stripe_customer_id", stripe_customer_id).limit(1).execute()
    )
    return res.data[0]["id"] if res.data else None


def stripe_get_tenant_id_by_subscription(stripe_subscription_id: str) -> str | None:
    res = (
        db().table("tenants").select("id")
        .eq("stripe_subscription_id", stripe_subscription_id).limit(1).execute()
    )
    return res.data[0]["id"] if res.data else None


def stripe_record_event(event_id: str, event_type: str) -> bool:
    """Insert an event into stripe_events (event-id dedup). Return True if this
    is a NEW event, False if a duplicate (already processed — caller should
    short-circuit). Stripe retries delivery on a non-2xx response for up to 3
    days, so duplicates are routine and must be silent no-ops. Any OTHER
    insert failure is logged and re-raised: the caller then answers 5xx and
    Stripe retries, rather than a 200 that would lose a first-time event."""
    try:
        db().table("stripe_events").insert(
            {"id": event_id, "type": event_type}
        ).execute()
        return True
    except Exception as exc:
        # A primary-key conflict (Postgres 23505, surfaced by PostgREST as
        # APIError.code) is the one expected failure: the event was already
        # recorded, so the caller short-circuits with a 200.
        if getattr(exc, "code", None) == "23505":
            return False
        # Anything else — database unreachable, RLS, a malformed response —
        # is NOT a duplicate. Log it with the event identity and re-raise so
        # the webhook answers 5xx and Stripe retries.
        log.exception("stripe_events insert failed (not a duplicate): event=%s type=%s",
                      event_id, event_type)
        raise


# ── TMS baseline (drivers · pricing · POD/tracking · invoicing · compliance) ──
# Thin, tenant-scoped data access for the logistics add-on's post-dispatch
# surfaces. The pure view-models (src/app/pricing.py, src/app/pod.py, src/app/invoicing.py,
# src/app/tracking.py, src/app/compliance.py) do the shaping. shipment_events is the
# spine shared by POD, live tracking and the customer portal.

# Drivers ---------------------------------------------------------------------
def list_drivers(tenant_id: str, client: Client | None = None,
                 active_only: bool = False) -> list[dict]:
    q = (client or db()).table("drivers").select("*").eq("tenant_id", tenant_id)
    if active_only:
        q = q.eq("active", True)
    return (q.order("name").execute()).data or []


def add_driver(tenant_id: str, name: str, phone: str | None = None,
               licence_no: str | None = None, licence_expiry: str | None = None,
               cpc_expiry: str | None = None, client: Client | None = None) -> dict | None:
    res = (client or db()).table("drivers").insert({
        "tenant_id": tenant_id, "name": name, "phone": phone,
        "licence_no": licence_no, "licence_expiry": licence_expiry,
        "cpc_expiry": cpc_expiry,
    }).execute()
    return res.data[0] if res.data else None


def get_driver(driver_id: str, tenant_id: str, client: Client | None = None) -> dict | None:
    res = ((client or db()).table("drivers").select("*")
           .eq("id", driver_id).eq("tenant_id", tenant_id).limit(1).execute())
    return res.data[0] if res.data else None


# Rate cards (pricing) --------------------------------------------------------
def get_rate_card(tenant_id: str, client: Client | None = None) -> dict | None:
    """The tenant's active rate card (one default per tenant in v1)."""
    res = ((client or db()).table("rate_cards").select("*")
           .eq("tenant_id", tenant_id).eq("active", True)
           .order("created_at").limit(1).execute())
    return res.data[0] if res.data else None


def upsert_rate_card(tenant_id: str, fields: dict, client: Client | None = None) -> None:
    """Edit the tenant's rate card (creates it if none exists yet)."""
    existing = get_rate_card(tenant_id, client=client)
    if existing:
        (client or db()).table("rate_cards").update(fields) \
            .eq("id", existing["id"]).eq("tenant_id", tenant_id).execute()
    else:
        (client or db()).table("rate_cards").insert(
            {"tenant_id": tenant_id, **fields}).execute()


def seed_tms_defaults(tenant_id: str) -> None:
    """Idempotent TMS-activation seeding: a default rate card so the pricing page
    isn't empty. Load-unit types + a Main branch come from seed_fleet_defaults."""
    have = db().table("rate_cards").select("id").eq("tenant_id", tenant_id).limit(1).execute()
    if not have.data:
        db().table("rate_cards").insert({
            "tenant_id": tenant_id, "name": "Standard",
            "base_charge": 25, "per_mile": 1.20, "per_pallet": 8,
            "per_drop": 5, "min_charge": 45, "fuel_pct": 6,
        }).execute()


# ── field service pricing (docs/modules/module-field-service.md D3) ──────────────────
def get_fsm_rate_card(tenant_id: str, client: Client | None = None) -> dict | None:
    """The tenant's active field-service rate card (one default per tenant in v1)."""
    res = ((client or db()).table("fsm_rate_cards").select("*")
           .eq("tenant_id", tenant_id).eq("active", True)
           .order("created_at").limit(1).execute())
    return res.data[0] if res.data else None


def upsert_fsm_rate_card(tenant_id: str, fields: dict, client: Client | None = None) -> None:
    """Edit the tenant's field-service rate card (creates it if none exists yet)."""
    existing = get_fsm_rate_card(tenant_id, client=client)
    if existing:
        (client or db()).table("fsm_rate_cards").update(fields) \
            .eq("id", existing["id"]).eq("tenant_id", tenant_id).execute()
    else:
        (client or db()).table("fsm_rate_cards").insert(
            {"tenant_id": tenant_id, **fields}).execute()


# Booking quote + public tracking token ---------------------------------------
def update_booking_details(booking_id: str, tenant_id: str, extra: dict,
                           summary: str | None = None, client: Client | None = None) -> None:
    """Merge keys into a booking's details jsonb (a make-up's 'Make-up for',
    the portal's add-to-booking dependant / service) and optionally set the
    summary. ONE definition: until 2026-09-06 this file held two — this
    merge form shadowed an earlier replace-with-summary form, so every
    caller passing summary= (the portal's add-to-booking) raised TypeError."""
    c = client or db()
    row = get_booking(booking_id, tenant_id, client=c)
    if not row:
        return
    details = dict(row.get("details") or {})
    details.update(extra or {})
    patch: dict = {"details": details, "updated_at": datetime.now(timezone.utc).isoformat()}
    if summary:
        patch["summary"] = str(summary)[:200]
    c.table("bookings").update(patch).eq("id", booking_id).eq("tenant_id", tenant_id).execute()


def rename_booking(booking_id: str, tenant_id: str, summary: str,
                   client: Client | None = None) -> None:
    """A booking's summary by hand (src/app/record_edits.py). Its own helper:
    update_booking_details is defined twice in this file and the later,
    merge-into-details signature shadows the one that took a summary (live
    2026-09-06: a 500 on the job page's first rename)."""
    (client or db()).table("bookings").update(
        {"summary": str(summary or "")[:200], "updated_at": datetime.now(timezone.utc).isoformat()}
    ).eq("id", booking_id).eq("tenant_id", tenant_id).execute()


def tenants_billing_balances() -> list[str]:
    """Tenants whose Invoice settings bill the balance (src/app/jobs/raise_balances.py)."""
    res = (db().table("invoice_settings").select("tenant_id")
           .filter("extras->>balance_mode", "in", "(draft,send)").execute())
    return [r["tenant_id"] for r in (res.data or [])]


def bookings_awaiting_balance(tenant_id: str, last_day_iso: str, *,
                              lookback_days: int = 120) -> list[dict]:
    """Confirmed, priced bookings with no balance raised, starting on or before
    `last_day_iso` (a stay began earlier) — the caller keeps those ENDING then."""
    first = (date.fromisoformat(last_day_iso) - timedelta(days=lookback_days)).isoformat()
    res = (db().table("bookings").select("*").eq("tenant_id", tenant_id)
           .eq("status", "confirmed").gt("quoted_amount", 0).is_("balance_invoiced_at", "null")
           .gte("booking_date", first).lte("booking_date", last_day_iso).execute())
    return res.data or []


def mark_booking_balance(booking_id: str, tenant_id: str, invoice_id: str | None) -> None:
    """The balance for this booking is dealt with — raised (`invoice_id`) or
    nothing to bill (None). Either way the sweep never looks at it again."""
    (db().table("bookings").update({
        "balance_invoice_id": invoice_id,
        "balance_invoiced_at": datetime.now(timezone.utc).isoformat()})
     .eq("id", booking_id).eq("tenant_id", tenant_id).execute())


def set_booking_deposit(booking_id: str, tenant_id: str, amount: float | None,
                        client: Client | None = None) -> None:
    """The deposit asked for on a booking (src/app/deposits.py) — the diary and the
    job page read it back beside the price."""
    (client or db()).table("bookings").update({"deposit_amount": amount})         .eq("id", booking_id).eq("tenant_id", tenant_id).execute()


def set_booking_quote(booking_id: str, tenant_id: str, amount: float,
                      client: Client | None = None) -> None:
    (client or db()).table("bookings").update({"quoted_amount": amount}) \
        .eq("id", booking_id).eq("tenant_id", tenant_id).execute()


def set_booking_track_token(booking_id: str, tenant_id: str, token: str,
                            client: Client | None = None) -> None:
    (client or db()).table("bookings").update({"track_token": token}) \
        .eq("id", booking_id).eq("tenant_id", tenant_id).execute()


def get_booking_by_track_token(token: str, client: Client | None = None) -> dict | None:
    """PUBLIC portal lookup — service role, NO tenant scope (the token IS the
    capability). Returns the one booking or None."""
    res = ((client or db()).table("bookings").select("*")
           .eq("track_token", token).limit(1).execute())
    return res.data[0] if res.data else None


def list_completed_bookings(tenant_id: str, date_from_iso: str, date_to_iso: str,
                            client: Client | None = None) -> list[dict]:
    """Delivered (status='completed') transport rows in a period — the invoice
    working set. Office rows (spaces null) are excluded, as everywhere."""
    res = ((client or db()).table("bookings").select("*")
           .eq("tenant_id", tenant_id).eq("status", "completed")
           .gte("booking_date", date_from_iso).lte("booking_date", date_to_iso)
           .order("booking_date").execute())
    return [b for b in (res.data or []) if b.get("spaces") is not None]


def list_completed_office_bookings(tenant_id: str, date_from_iso: str, date_to_iso: str,
                                   client: Client | None = None) -> list[dict]:
    """Completed (status='completed') OFFICE bookings (no spaces, no team member) in a period —
    the office invoicing working set (appointments + stays). The office sibling of
    list_completed_bookings (transport) / list_completed_fsm_jobs (trades)."""
    res = ((client or db()).table("bookings").select("*")
           .eq("tenant_id", tenant_id).eq("status", "completed")
           .gte("booking_date", date_from_iso).lte("booking_date", date_to_iso)
           .order("booking_date").execute())
    return [b for b in (res.data or [])
            if b.get("spaces") is None and not b.get("engineer_id")]


def list_completed_fsm_jobs(tenant_id: str, date_from_iso: str, date_to_iso: str,
                            client: Client | None = None) -> list[dict]:
    """Completed field-service jobs (engineer_id set) in a period — the trades
    invoice working set (the team member sibling of list_completed_bookings)."""
    res = ((client or db()).table("bookings").select("*")
           .eq("tenant_id", tenant_id).eq("status", "completed")
           .gte("booking_date", date_from_iso).lte("booking_date", date_to_iso)
           .order("booking_date").execute())
    return [b for b in (res.data or []) if b.get("engineer_id")]


# ── field service recurring service reminders (docs/modules/module-field-service.md D5) ──
def _add_months(d: date, months: int) -> date:
    """d + `months`, clamping the day to the target month's last day (so a 31st + 1
    month lands on the 30th/28th rather than overflowing)."""
    m = d.month - 1 + int(months)
    y = d.year + m // 12
    m = m % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def complete_job_and_schedule_service(booking_id: str, tenant_id: str,
                                      client: Client | None = None) -> str | None:
    """Mark a field-service job completed and, if its job type recurs
    (job_types.service_months), stamp service_due_date = booking_date + that interval so
    the recurring-service reminder can chase the rebook (D5). Returns the due date (iso)
    or None when the job type doesn't recur."""
    c = client or db()
    res = (c.table("bookings").select("booking_date,job_type")
           .eq("id", booking_id).eq("tenant_id", tenant_id).limit(1).execute())
    b = res.data[0] if res.data else None
    set_booking_status(booking_id, "completed", tenant_id, client=client)
    if not b or not b.get("job_type"):
        return None
    jt = (c.table("job_types").select("service_months")
          .eq("tenant_id", tenant_id).eq("name", b["job_type"]).limit(1).execute())
    months = jt.data[0].get("service_months") if jt.data else None
    if not months:
        return None
    due = _add_months(date.fromisoformat(str(b["booking_date"])[:10]), int(months))
    c.table("bookings").update({"service_due_date": due.isoformat()}) \
        .eq("id", booking_id).eq("tenant_id", tenant_id).execute()
    return due.isoformat()


def list_jobs_due_service(tenant_id: str, cutoff_iso: str,
                          client: Client | None = None) -> list[dict]:
    """Completed jobs whose recurring service is due on/before `cutoff_iso` and not yet
    chased (service_reminder_queued_at null), with a customer email — the service-reminder
    work set (the recurring sibling of list_bookings_for_reminder). lte excludes the
    null-service_due rows, so only recurring jobs surface."""
    res = ((client or db()).table("bookings").select("*")
           .eq("tenant_id", tenant_id).eq("status", "completed")
           .lte("service_due_date", cutoff_iso)
           .is_("service_reminder_queued_at", "null").execute())
    return [b for b in (res.data or [])
            if b.get("engineer_id") and reachable_contact(b.get("contact"))]


def mark_service_reminded(booking_id: str, tenant_id: str, client: Client | None = None) -> None:
    """Stamp a job's service reminder queued (dedupe — at most one per service cycle)."""
    (client or db()).table("bookings").update(
        {"service_reminder_queued_at": datetime.now(timezone.utc).isoformat()}
    ).eq("id", booking_id).eq("tenant_id", tenant_id).execute()


# ── Practice OS recall — the complete_job_and_schedule_service twin (docs/os/practice.md §5) ──
def complete_appointment_and_schedule_recall(
        booking_id: str, tenant_id: str, outcome: str = "completed",
        client: Client | None = None) -> str | None:
    """Set the appointment's status (completed | no_show), and — if its service has a
    recall_months cadence (scheduling_config.service_types[i].recall_months) — stamp
    recall_due_date = booking_date + recall_months and copy recall_interval_months.
    Returns the recall due date (iso) or None. The Practice twin of
    complete_job_and_schedule_service; reuses _add_months(). On outcome='no_show' the
    contact's no-show history feeds the scorer (P3 — not stamped here yet)."""
    from app import practice_config
    c = client or db()
    res = (c.table("bookings").select("booking_date,summary")
           .eq("id", booking_id).eq("tenant_id", tenant_id).limit(1).execute())
    b = res.data[0] if res.data else None
    status = "no_show" if outcome == "no_show" else "completed"
    set_booking_status(booking_id, status, tenant_id, client=client)
    if status != "completed" or not b or not b.get("booking_date"):
        return None
    cfg = get_scheduling_config(tenant_id, client=client) or {}
    months = practice_config.recall_months_for_service(cfg.get("service_types"), b.get("summary"))
    if not months:
        return None
    due = _add_months(date.fromisoformat(str(b["booking_date"])[:10]), int(months))
    c.table("bookings").update(
        {"recall_due_date": due.isoformat(), "recall_interval_months": int(months)}
    ).eq("id", booking_id).eq("tenant_id", tenant_id).execute()
    return due.isoformat()


def list_appointments_due_recall(tenant_id: str, cutoff_iso: str,
                                 client: Client | None = None) -> list[dict]:
    """Completed appointments whose recall_due_date <= cutoff and recall_reminder_queued_at
    is null, with a client email — the recall work set (the sibling of list_jobs_due_service).
    lte excludes the null-recall_due rows, so only appointments with a cadence surface."""
    res = ((client or db()).table("bookings").select("*")
           .eq("tenant_id", tenant_id).eq("status", "completed")
           .lte("recall_due_date", cutoff_iso)
           .is_("recall_reminder_queued_at", "null").execute())
    return [b for b in (res.data or []) if reachable_contact(b.get("contact"))]


def mark_recall_reminded(booking_id: str, tenant_id: str, client: Client | None = None) -> None:
    """Stamp an appointment's recall queued (dedupe — at most one recall per cycle). Twin
    of mark_service_reminded."""
    (client or db()).table("bookings").update(
        {"recall_reminder_queued_at": datetime.now(timezone.utc).isoformat()}
    ).eq("id", booking_id).eq("tenant_id", tenant_id).execute()


# ── the 48/24/2h reminder cycle (docs/os/practice.md §5, §6.2) ────────────────
def list_appointments_for_reminder_stage(
        tenant_id: str, stage: str, window_start_iso: str, window_end_iso: str,
        client: Client | None = None) -> list[dict]:
    """CONFIRMED appointments whose start falls in [window_start, window_end] and whose
    reminder_stages_sent jsonb LACKS `stage` (e.g. '24h') — the per-stage work set (§5).

    The multi-stage generalisation of list_bookings_for_reminder (day-before only): the
    caller passes one stage's window per lead time, and reminder_stages_sent gives a
    per-stage dedupe (so the same appointment can receive 48h, 24h and 2h touches, but each
    at most once). We compare against the booking's DATE window (Supabase can't range on a
    jsonb-stripped start), then keep only rows whose booking_date is in range AND whose
    stage stamp is absent — the stage check is a cheap client-side dict lookup. Only rows
    with a client email surface."""
    start_day = str(window_start_iso)[:10]
    end_day = str(window_end_iso)[:10]
    res = (
        (client or db()).table("bookings").select("*")
        .eq("tenant_id", tenant_id).eq("status", "confirmed")
        .gte("booking_date", start_day).lte("booking_date", end_day)
        .execute()
    )
    out: list[dict] = []
    for b in (res.data or []):
        if "@" not in str(b.get("contact") or ""):
            continue
        stages = b.get("reminder_stages_sent") or {}
        if isinstance(stages, dict) and stage in stages:
            continue                                # this stage already queued — dedupe
        out.append(b)
    return out


def mark_reminder_stage_sent(booking_id: str, stage: str, tenant_id: str,
                             client: Client | None = None) -> None:
    """Merge {stage: now()} into reminder_stages_sent (jsonb) — a per-stage dedupe stamp
    (§5). Read-modify-write the row's jsonb (no jsonb_set RPC needed): fetch the current
    map, add this stage, write it back. The presence of the key IS the dedupe."""
    c = client or db()
    res = (c.table("bookings").select("reminder_stages_sent")
           .eq("id", booking_id).eq("tenant_id", tenant_id).limit(1).execute())
    current = (res.data[0].get("reminder_stages_sent") if res.data else None) or {}
    if not isinstance(current, dict):
        current = {}
    current[stage] = datetime.now(timezone.utc).isoformat()
    c.table("bookings").update({"reminder_stages_sent": current}) \
        .eq("id", booking_id).eq("tenant_id", tenant_id).execute()


# ── Practice OS no-show scoring (docs/os/practice.md §5, §12.1) ───────────────
def contact_no_show_history(tenant_id: str, email: str,
                            client: Client | None = None) -> dict:
    """Prior-behaviour signal for the no-show scorer — a cheap aggregate over this
    contact's `bookings` + `email_log`, no new table (§5). Returns
    {total_appts, no_shows, last_no_show_at, avg_reply_latency_hours, reminders_ignored}.

    Behavioural metadata ONLY — counts and latencies, never note/clinical text (§12.1
    privacy). `total_appts` counts settled appointments (completed + no_show), so the
    no-show RATE (no_shows / total_appts) is well-defined. `reminders_ignored` is a coarse
    engagement signal: True when we've sent this contact reminder/recall touches but have
    no inbound reply on record (a silent customer scores higher risk)."""
    c = client or db()
    email = (email or "").strip()
    if "@" not in email:
        return {"total_appts": 0, "no_shows": 0, "last_no_show_at": None,
                "avg_reply_latency_hours": None, "reminders_ignored": False}

    # Settled appointments for this contact — completed + no_show only (an appointment
    # that hasn't happened yet carries no attendance signal).
    appts = (c.table("bookings").select("status,booking_date")
             .eq("tenant_id", tenant_id).eq("contact", email)
             .in_("status", ["completed", "no_show"]).execute()).data or []
    total = len(appts)
    no_show_rows = [a for a in appts if a.get("status") == "no_show"]
    no_shows = len(no_show_rows)
    last_no_show_at = max((str(a.get("booking_date")) for a in no_show_rows if a.get("booking_date")),
                          default=None)

    # Engagement from email_log: outbound reminder/recall touches vs inbound replies.
    # `from_address` holds the contact on BOTH directions (insert_email_log(...,to,...)),
    # so we split by `kind`: inbound = the customer wrote in; reminder/recall = we chased.
    logs = (c.table("email_log").select("kind,created_at")
            .eq("tenant_id", tenant_id).eq("from_address", email)
            .order("created_at").execute()).data or []
    inbound = [l for l in logs if l.get("kind") == "inbound"]
    touches = [l for l in logs if l.get("kind") in ("reminder", "recall")]

    # Average reply latency: for each of our touches, hours to the next inbound reply.
    latencies: list[float] = []
    inbound_times = [_parse_ts(l.get("created_at")) for l in inbound]
    inbound_times = [t for t in inbound_times if t is not None]
    for tch in touches:
        sent = _parse_ts(tch.get("created_at"))
        if sent is None:
            continue
        nxt = next((t for t in inbound_times if t >= sent), None)
        if nxt is not None:
            latencies.append((nxt - sent).total_seconds() / 3600.0)
    avg_latency = round(sum(latencies) / len(latencies), 1) if latencies else None

    reminders_ignored = bool(touches) and not inbound
    return {"total_appts": total, "no_shows": no_shows, "last_no_show_at": last_no_show_at,
            "avg_reply_latency_hours": avg_latency, "reminders_ignored": reminders_ignored}


def _parse_ts(value) -> datetime | None:
    """Parse a Supabase timestamptz string to an aware UTC datetime, tolerantly."""
    if not value:
        return None
    s = str(value).replace("Z", "+00:00")
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        try:
            d = datetime.fromisoformat(s[:19])
        except ValueError:
            return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def list_appointments_to_score(tenant_id: str, horizon_days: int = 2,
                               client: Client | None = None) -> list[dict]:
    """CONFIRMED appointments starting within `horizon_days` whose no-show score is stale —
    the scorer's work set (§5). Stale = never scored (no_show_scored_at null) OR scored
    before the contact's behaviour last changed (their newest reminder/reply is more recent
    than the score). We compute staleness client-side against email_log so re-scoring is
    cheap and re-runs are idempotent when nothing changed. Only rows with a client email."""
    c = client or db()
    today = datetime.now(timezone.utc).date()
    horizon = (today + timedelta(days=max(0, int(horizon_days)))).isoformat()
    rows = (c.table("bookings").select("*")
            .eq("tenant_id", tenant_id).eq("status", "confirmed")
            .gte("booking_date", today.isoformat()).lte("booking_date", horizon)
            .execute()).data or []
    out: list[dict] = []
    for b in rows:
        if "@" not in str(b.get("contact") or ""):
            continue
        scored_at = _parse_ts(b.get("no_show_scored_at"))
        if scored_at is None:
            out.append(b)
            continue
        # Re-score only if the contact's engagement moved since we last scored.
        logs = (c.table("email_log").select("created_at")
                .eq("tenant_id", tenant_id).eq("from_address", b["contact"])
                .order("created_at", desc=True).limit(1).execute()).data or []
        newest = _parse_ts(logs[0].get("created_at")) if logs else None
        if newest is not None and newest > scored_at:
            out.append(b)
    return out


def save_no_show_score(booking_id: str, tenant_id: str, score: int, band: str,
                       reason: str, client: Client | None = None) -> None:
    """Persist no_show_score/band/reason/no_show_scored_at on the booking (§5)."""
    (client or db()).table("bookings").update({
        "no_show_score": int(score),
        "no_show_band": band,
        "no_show_reason": (reason or "")[:280],
        "no_show_scored_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", booking_id).eq("tenant_id", tenant_id).execute()


def mark_extra_reminder_queued(booking_id: str, tenant_id: str,
                               client: Client | None = None) -> None:
    """Stamp extra_reminder_queued_at — dedupe the high-risk extra reminder so a re-run of
    the scorer (or a manual fire) never double-nudges the same appointment (§5, §12.1)."""
    (client or db()).table("bookings").update(
        {"extra_reminder_queued_at": datetime.now(timezone.utc).isoformat()}
    ).eq("id", booking_id).eq("tenant_id", tenant_id).execute()


def list_confirmed_appointments_between(tenant_id: str, date_from_iso: str, date_to_iso: str,
                                        client: Client | None = None) -> list[dict]:
    """CONFIRMED appointments in [date_from, date_to] — the risk-panel work set (§2). Ordered
    by date/window so the board can group by day; the route sorts by no_show_score desc."""
    res = (
        (client or db()).table("bookings").select("*")
        .eq("tenant_id", tenant_id).eq("status", "confirmed")
        .gte("booking_date", date_from_iso).lte("booking_date", date_to_iso)
        .order("booking_date").order("window").execute()
    )
    return [b for b in (res.data or []) if "@" in str(b.get("contact") or "")]


# ── Practice OS encrypted medical notes (docs/os/practice.md §4.3, §5, §14) ───
# Special-category data (UK GDPR Art. 9): the ciphertext lives in a bytea column and is
# only ever decrypted inside a tenant-user context (the /app/practice routes + the
# history-summary build) — never returned raw over an API, never logged, never sent to the
# LLM. crypto.encrypt returns a `str` ('v1:<fernet>'); the column is `bytea`. We bridge the
# two by hex-encoding the token's UTF-8 bytes and prefixing '\x' — PostgreSQL's own bytea
# input format — so PostgREST stores it losslessly, and on read PostgREST hands the same
# '\x<hex>' escape string back, which we decode to recover the exact token. This keeps the
# round-trip explicit and independent of any driver-specific str→bytea coercion.

def _bytea_encode(token: str) -> str:
    r"""A ciphertext string → a PostgreSQL bytea literal ('\x' + hex of its UTF-8 bytes).
    The token is ASCII ('v1:' + urlsafe base64), so this is a faithful, reversible mapping."""
    return "\\x" + token.encode("utf-8").hex()


def _bytea_decode(stored) -> str | None:
    r"""A bytea value read back from PostgREST → the original ciphertext string. PostgREST
    returns bytea as a '\x<hex>' escape string; tolerate a bare hex string or already-decoded
    bytes/memoryview too. Returns None when it can't be decoded (a caller surfaces that as a
    decrypt_error rather than crashing)."""
    if stored is None:
        return None
    if isinstance(stored, (bytes, bytearray, memoryview)):
        try:
            return bytes(stored).decode("utf-8")
        except (UnicodeDecodeError, ValueError):
            return None
    s = str(stored)
    if s.startswith("\\x"):
        s = s[2:]
    try:
        return bytes.fromhex(s).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


def add_medical_note(tenant_id: str, contact_id: str, text: str, kind: str = "note",
                     source: str = "manual", source_email_id: str | None = None,
                     created_by: str | None = None, client: Client | None = None) -> dict:
    """Encrypt `text` (app.crypto — the mailbox-token envelope) and insert one medical note.
    Returns the stored row WITHOUT the ciphertext (§5). Tenant-user context only."""
    token = crypto.encrypt(str(text or ""))
    row = (client or db()).table("practice_medical_notes").insert({
        "tenant_id": tenant_id, "contact_id": contact_id,
        "note_ciphertext": _bytea_encode(token or ""),
        "note_kind": (kind or "note"), "source": source,
        "source_email_id": source_email_id, "created_by": created_by,
    }).execute()
    out = (row.data[0] if row.data else {}) or {}
    out.pop("note_ciphertext", None)           # never hand ciphertext back
    return out


def list_medical_notes(tenant_id: str, contact_id: str,
                       client: Client | None = None) -> list[dict]:
    """This contact's notes, DECRYPTED and newest first (§5). Tenant-user context only.

    LOUD on failure (§14): the known local/prod key mismatch makes crypto.decrypt RAISE on
    a v1:-prefixed token. A silent drop of a medical note is worse than a token — so instead
    of crashing the page (or dropping the row), we return the note with decrypt_error=True and
    an empty `text`, letting the UI show 'note unavailable — encryption key issue'. The
    ciphertext itself is never returned."""
    res = ((client or db()).table("practice_medical_notes").select("*")
           .eq("tenant_id", tenant_id).eq("contact_id", contact_id)
           .order("created_at", desc=True).execute())
    out: list[dict] = []
    for r in (res.data or []):
        row = {k: v for k, v in r.items() if k != "note_ciphertext"}
        token = _bytea_decode(r.get("note_ciphertext"))
        try:
            if token is None:
                raise ValueError("ciphertext could not be decoded from storage")
            row["text"] = crypto.decrypt(token) or ""
            row["decrypt_error"] = False
        except Exception:
            log.warning("medical note decrypt failed for note %s (tenant %s) — key mismatch?",
                        r.get("id"), tenant_id)
            row["text"] = ""
            row["decrypt_error"] = True
        out.append(row)
    return out


def delete_medical_note(tenant_id: str, contact_id: str, note_id: str,
                        client: Client | None = None) -> None:
    """Delete one note — scoped to the owning tenant AND contact (IDOR-safe). Tenant-user only."""
    (client or db()).table("practice_medical_notes").delete() \
        .eq("id", note_id).eq("tenant_id", tenant_id).eq("contact_id", contact_id).execute()


def patient_history_summary(tenant_id: str, contact_id: str,
                            client: Client | None = None) -> str:
    """A SHORT, drafter-safe one-liner of who this customer is (§5, §12.2): CRM fields
    (insurer, recall cadence, usual provider, last visit) PLUS a REDACTED note line
    ('has recorded allergies/medical notes on file') — but NEVER the raw clinical text.

    This is the ONLY customer-history string allowed to ride into the drafter (build_crm_
    context, §12.2). It reads the contact's custom_data (the self-filling customer fields)
    and the presence/kinds of medical notes, never their decrypted content."""
    contact = crm_get_contact(contact_id, tenant_id, client=client)
    if not contact:
        return ""
    bits: list[str] = []
    custom = contact.get("custom_data") or {}
    # Match the customer fields by the labels the Practice CRM seeds use (industry_lingo),
    # tolerant to the stored key casing/spacing. Only non-clinical fields ride in here.
    defs = crm_list_field_definitions(tenant_id, client=client)
    label_by_key = {d["key"]: d.get("label") for d in defs if d.get("entity") == "contact"}
    _WANT = ("insurer", "recall cadence", "usual practitioner", "preferred contact")
    for key, value in custom.items():
        label = (label_by_key.get(key) or key)
        if value in (None, "") or not label:
            continue
        if any(w in str(label).strip().lower() for w in _WANT):
            bits.append(f"{label}: {value}")
    # Last visit — the most recent settled appointment date for this contact's email.
    email = (contact.get("email") or "").strip()
    if "@" in email:
        appts = ((client or db()).table("bookings").select("booking_date,status")
                 .eq("tenant_id", tenant_id).eq("contact", email)
                 .in_("status", ["completed", "no_show"]).execute()).data or []
        last = max((str(a.get("booking_date")) for a in appts if a.get("booking_date")),
                   default=None)
        if last:
            bits.append(f"last visit {last[:10]}")
    # The REDACTED clinical line — presence + kinds only, NEVER the note text (§14).
    notes = ((client or db()).table("practice_medical_notes").select("note_kind")
             .eq("tenant_id", tenant_id).eq("contact_id", contact_id).execute()).data or []
    if notes:
        kinds = sorted({(n.get("note_kind") or "note") for n in notes})
        label = "allergies/medical notes" if any(k != "note" for k in kinds) else "medical notes"
        bits.append(f"has recorded {label} on file — see the patient's notes")
    return "; ".join(bits)


# Shipment events — the POD / tracking / portal spine -------------------------
def add_shipment_event(tenant_id: str, booking_id: str, kind: str,
                       note: str | None = None, signed_by: str | None = None,
                       photo_url: str | None = None, driver_id: str | None = None,
                       lat: float | None = None, lng: float | None = None,
                       client: Client | None = None) -> dict | None:
    res = (client or db()).table("shipment_events").insert({
        "tenant_id": tenant_id, "booking_id": booking_id, "kind": kind,
        "note": note, "signed_by": signed_by, "photo_url": photo_url,
        "driver_id": driver_id, "lat": lat, "lng": lng,
    }).execute()
    return res.data[0] if res.data else None


def list_shipment_events(tenant_id: str, client: Client | None = None) -> list[dict]:
    """A tenant's shipment events, oldest first — the POD board and tracking map
    derive each booking's current state from these."""
    res = ((client or db()).table("shipment_events").select("*")
           .eq("tenant_id", tenant_id).order("created_at").execute())
    return res.data or []


def list_events_for_booking(booking_id: str, client: Client | None = None) -> list[dict]:
    """One shipment's timeline — the PUBLIC portal reads this (no tenant scope:
    the caller already resolved the booking via its capability token)."""
    res = ((client or db()).table("shipment_events").select("*")
           .eq("booking_id", booking_id).order("created_at").execute())
    return res.data or []


def mark_delivered(booking_id: str, tenant_id: str, delivered: bool,
                   signed_by: str | None = None, note: str | None = None,
                   photo_url: str | None = None, driver_id: str | None = None,
                   client: Client | None = None) -> None:
    """POD write path (shared by the POD board and the driver app): record the
    delivered/failed event and, on delivery, move the booking to 'completed' —
    the status the diary already excludes from active capacity. A failed attempt
    logs the event but leaves the booking active for a re-attempt."""
    add_shipment_event(tenant_id, booking_id, "delivered" if delivered else "failed",
                       note=note, signed_by=signed_by, photo_url=photo_url,
                       driver_id=driver_id, client=client)
    if delivered:
        set_booking_status(booking_id, "completed", tenant_id, client=client)


# Invoices --------------------------------------------------------------------
def next_invoice_ref(tenant_id: str, client: Client | None = None) -> str:
    res = (client or db()).rpc("next_invoice_ref", {"p_tenant_id": tenant_id}).execute()
    data = res.data
    if isinstance(data, list):
        data = data[0] if data else None
    return data or "INV-1001"


DEFAULT_INVOICE_DUE_DAYS = 14   # the fallback; live value = invoice_config.due_days


def create_invoice(tenant_id: str, customer_email: str | None, customer_name: str | None,
                   period_from: str | None, period_to: str | None, lines: list[dict],
                   client: Client | None = None, *, contact_id: str | None = None,
                   deal_id: str | None = None, booking_id: str | None = None,
                   due_date: str | None = None, notes: str | None = None,
                   customer_reference: str | None = None,
                   prices_include_vat: bool | None = None,
                   created_by: str = "owner") -> dict | None:
    """Create one invoice + its lines. `lines` = [{booking_id, description, amount,
    quantity?, unit_price?, kb_entry_id?}] — amount stays the line TOTAL. Totals are
    computed here (so the row is self-consistent regardless of caller) using the
    tenant's invoice settings (VAT can be toggled on/off or re-rated). issue_date is
    today; a blank due_date follows the tenant's payment terms (14 days when unset).
    period_from/to stay optional — a
    standalone (non-period) invoice passes None for both.

    The invoice remembers the price basis it was made under
    (invoices.prices_include_vat, written only when True). `prices_include_vat`
    None = the settings' basis in force now; an invoice made FROM a quote passes
    the quote's own (True/False) so it inherits it."""
    from app import invoice_config
    settings = invoice_config.settings_for(get_invoice_settings(tenant_id, client=client))
    include = (settings.get("prices_include_vat") is True if prices_include_vat is None
               else bool(prices_include_vat))
    subtotal, vat, total = invoice_config.split_vat(
        sum(float(l["amount"]) for l in lines), settings, include=include)
    ref = next_invoice_ref(tenant_id, client=client)
    if deal_id is None:
        # Same walk as create_quote: an invoice raised for a job belongs to that
        # job's deal. (No email_log_id on this path — the booking carries it.)
        deal_id = deal_id_for_job(tenant_id, booking_id=booking_id, client=client) \
            or open_deal_id_for_contact(tenant_id, contact_id, client=client)
    today = datetime.now(timezone.utc).date()
    inv_row = {
        "tenant_id": tenant_id, "invoice_ref": ref,
        "customer_email": customer_email, "customer_name": customer_name,
        "period_from": period_from, "period_to": period_to,
        "subtotal": subtotal, "vat": vat, "total": total,
        "contact_id": contact_id, "deal_id": deal_id, "booking_id": booking_id,
        "issue_date": today.isoformat(),
        # Blank due date → the tenant's payment terms decide (invoice_config.due_days).
        "due_date": due_date or invoice_config.default_due_date(settings, today),
        "notes": notes, "created_by": created_by,
        # Their PO / works-order number, captured once and never re-keyed —
        # rides from the quote, prints on the PDF, lands in the export.
        "customer_reference": (customer_reference or "").strip()[:100] or None,
    }
    if include:     # sent only when True: every other write is byte-identical
        inv_row["prices_include_vat"] = True
    inv = (client or db()).table("invoices").insert(inv_row).execute()
    row = inv.data[0] if inv.data else None
    if row and lines:
        (client or db()).table("invoice_lines").insert([
            {"tenant_id": tenant_id, "invoice_id": row["id"],
             "booking_id": l.get("booking_id"), "description": l["description"],
             "amount": l["amount"], "quantity": l.get("quantity") or 1,
             "unit_price": l.get("unit_price", l["amount"]),
             "kb_entry_id": l.get("kb_entry_id"),
             # Packages (src/app/wallet.py): a line that sells one names it.
             "package_key": l.get("package_key"), "instance_id": l.get("instance_id")} for l in lines
        ]).execute()
    return row


# Job parties (docs/modules/module-job-parties.md Phase 1) --------------------------
def list_job_parties(tenant_id: str, booking_id: str,
                     client: Client | None = None) -> list[dict]:
    res = ((client or db()).table("job_parties").select("*")
           .eq("tenant_id", tenant_id).eq("booking_id", booking_id)
           .order("created_at").execute())
    return res.data or []


def job_parties_for_bookings(tenant_id: str, booking_ids: list[str],
                             client: Client | None = None) -> dict[str, list[dict]]:
    """Parties for many jobs in ONE read (the invoice-generate resolution path)
    — {booking_id: [party, …]}, empty dict for the common no-parties tenant."""
    if not booking_ids:
        return {}
    res = ((client or db()).table("job_parties").select("*")
           .eq("tenant_id", tenant_id).in_("booking_id", booking_ids).execute())
    out: dict[str, list[dict]] = {}
    for row in res.data or []:
        out.setdefault(row["booking_id"], []).append(row)
    return out


def job_parties_for_contact(tenant_id: str, contact_id: str | None,
                            email: str | None,
                            client: Client | None = None) -> list[dict]:
    """Every job this person is a party on — matched on contact_id OR email
    (imports and hand-typed parties may carry only one). The CRM contact page's
    'Party to' lines."""
    email = (email or "").strip().lower()
    c = client or db()
    rows: dict[str, dict] = {}
    if contact_id:
        res = (c.table("job_parties").select("*")
               .eq("tenant_id", tenant_id).eq("contact_id", contact_id)
               .execute())
        for r in res.data or []:
            rows[r["id"]] = r
    if email:
        res = (c.table("job_parties").select("*")
               .eq("tenant_id", tenant_id).eq("email", email).execute())
        for r in res.data or []:
            rows[r["id"]] = r
    return list(rows.values())


def job_party_memberships(tenant_id: str, email: str | None,
                          client: Client | None = None) -> list[dict]:
    """TRUSTED party rows carrying this email across the tenant's jobs — the
    portal's party-awareness read (docs/modules/module-job-parties.md Phase 3).
    source='extracted' rows are excluded HERE, not at the callers: an
    unconfirmed claim must never grant portal visibility or reschedule
    rights (D4 applies to capabilities too)."""
    email = (email or "").strip().lower()
    if not email:
        return []
    res = ((client or db()).table("job_parties").select("*")
           .eq("tenant_id", tenant_id).eq("email", email)
           .in_("source", ["owner", "confirmed"]).execute())
    return res.data or []


def bookings_by_ids(tenant_id: str, booking_ids: list[str],
                    client: Client | None = None) -> list[dict]:
    """A handful of bookings by id, tenant-scoped — the CRM contact page turning
    party rows into legible job lines."""
    if not booking_ids:
        return []
    res = ((client or db()).table("bookings").select("*")
           .eq("tenant_id", tenant_id).in_("id", list(booking_ids)).execute())
    return res.data or []


def upsert_job_party(tenant_id: str, booking_id: str, party: dict,
                     client: Client | None = None) -> dict | None:
    """Insert or (matched on the unique tenant+booking+email) update one party.
    `party` = {name, email, phone, label, pays, approves, on_site, informed,
    contact_id?, source?}."""
    row = {"tenant_id": tenant_id, "booking_id": booking_id,
           "name": (party.get("name") or "").strip() or None,
           "email": (party.get("email") or "").strip().lower() or None,
           "phone": (party.get("phone") or "").strip() or None,
           "label": (party.get("label") or "").strip()[:60] or None,
           "contact_id": party.get("contact_id"),
           "source": party.get("source") or "owner",
           "evidence": party.get("evidence"),
           "email_log_id": party.get("email_log_id"),
           "updated_at": datetime.now(timezone.utc).isoformat()}
    for duty in ("pays", "approves", "on_site", "informed"):
        row[duty] = bool(party.get(duty))
    c = client or db()
    if row["email"]:
        existing = (c.table("job_parties").select("id")
                    .eq("tenant_id", tenant_id).eq("booking_id", booking_id)
                    .eq("email", row["email"]).limit(1).execute())
        if existing.data:
            res = (c.table("job_parties").update(row)
                   .eq("id", existing.data[0]["id"]).eq("tenant_id", tenant_id)
                   .execute())
            return res.data[0] if res.data else None
    res = c.table("job_parties").insert(row).execute()
    return res.data[0] if res.data else None


def delete_job_party(party_id: str, tenant_id: str,
                     client: Client | None = None) -> None:
    (client or db()).table("job_parties").delete() \
        .eq("id", party_id).eq("tenant_id", tenant_id).execute()


def confirm_job_party(party_id: str, tenant_id: str, *,
                      contact_id: str | None = None,
                      client: Client | None = None) -> dict | None:
    """A human's yes on an extracted party (Phase 2): source → 'confirmed', so
    resolve() starts honouring its duties, plus the CRM link. Only an
    'extracted' row can be confirmed — confirming an owner row is a no-op."""
    patch = {"source": "confirmed",
             "updated_at": datetime.now(timezone.utc).isoformat()}
    if contact_id:
        patch["contact_id"] = contact_id
    res = ((client or db()).table("job_parties").update(patch)
           .eq("id", party_id).eq("tenant_id", tenant_id)
           .eq("source", "extracted").execute())
    return res.data[0] if res.data else None


def mark_party_redirect_ccd(party_id: str, tenant_id: str,
                            client: Client | None = None) -> None:
    """The one-time receipt behind D-principle 4: the displaced payer has now
    been cc'd on the first invoice routed to this confirmed party."""
    (client or db()).table("job_parties").update({"redirect_ccd": True}) \
        .eq("id", party_id).eq("tenant_id", tenant_id).execute()


def import_invoice(tenant_id: str, inv: dict, *, contact_id: str | None = None,
                   client: Client | None = None) -> dict | None:
    """One invoice imported from another system (src/app/xero_import.py) —
    deliberately NOT create_invoice: the ref is theirs (the accountant matches
    on it, so it is preserved, never re-minted from tenant_counters), and the
    totals are imported as-is (subtotal = total, VAT 0 — their VAT split
    stayed in their old books; what must be exact is what's owed). Caller
    checks ref uniqueness first."""
    row = (client or db()).table("invoices").insert({
        "tenant_id": tenant_id, "invoice_ref": inv["invoice_ref"],
        "customer_email": inv.get("customer_email"),
        "customer_name": inv.get("customer_name"),
        "subtotal": inv["total"], "vat": 0, "total": inv["total"],
        "amount_paid": inv.get("amount_paid") or 0,
        "contact_id": contact_id,
        "issue_date": inv.get("issue_date"), "due_date": inv.get("due_date"),
        "status": inv.get("status") or "sent",
        "paid_at": (inv.get("issue_date") if inv.get("status") == "paid" else None),
        "notes": "Imported from Xero",
        "customer_reference": (inv.get("customer_reference") or "").strip()[:100] or None,
        "created_by": "import",
    }).execute()
    created = row.data[0] if row.data else None
    if created and inv.get("lines"):
        (client or db()).table("invoice_lines").insert([
            {"tenant_id": tenant_id, "invoice_id": created["id"],
             "description": l["description"], "amount": l["amount"],
             "quantity": l.get("quantity") or 1,
             "unit_price": l.get("unit_price", l["amount"])}
            for l in inv["lines"]]).execute()
    return created


def update_invoice_lines(invoice_id: str, tenant_id: str, lines: list[dict],
                         client: Client | None = None) -> None:
    """Replace a (draft) invoice's lines and recompute its totals from the tenant's VAT
    settings (Move 2 — editing before issuing). `lines` = [{description, amount}]."""
    c = client or db()
    c.table("invoice_lines").delete().eq("invoice_id", invoice_id).eq("tenant_id", tenant_id).execute()
    if lines:
        c.table("invoice_lines").insert([
            {"tenant_id": tenant_id, "invoice_id": invoice_id,
             "description": l["description"], "amount": l["amount"],
             # Preserved through an edit: this is the link the duplicate-invoice guard
             # reads (invoiced_booking_ids), so losing it here would let already-billed
             # work be invoiced a second time. quantity/unit_price are still dropped —
             # the editor doesn't expose them.
             "booking_id": l.get("booking_id")} for l in lines
        ]).execute()
    # The invoice's OWN stored basis decides the split (null = VAT added on top),
    # never the setting as it stands today.
    inv = get_invoice(invoice_id, tenant_id, client=client) or {}
    subtotal, vat, total = invoice_totals(
        tenant_id, lines, include=inv.get("prices_include_vat") is True, client=client)
    c.table("invoices").update(
        {"subtotal": subtotal, "vat": vat, "total": total}
    ).eq("id", invoice_id).eq("tenant_id", tenant_id).execute()


def invoice_totals(tenant_id: str, lines: list[dict], *, include: bool = False,
                   client: Client | None = None) -> tuple[float, float, float]:
    """(subtotal, vat, total) for these lines under the tenant's invoice settings and
    the document's own price basis (`include` = invoices.prices_include_vat is True).
    The one calculation update_invoice_lines stores, so a confirm card's preview can
    never differ from what the save writes (the quote twin is quote_totals)."""
    from app import invoice_config
    return invoice_config.split_vat(
        sum(float(l["amount"]) for l in lines),
        invoice_config.settings_for(get_invoice_settings(tenant_id, client=client)),
        include=include)


def update_draft_invoice_lines(invoice_id: str, tenant_id: str, lines: list[dict],
                               client: Client | None = None) -> bool:
    """update_invoice_lines, but only while the invoice is still a DRAFT in this
    business: a guarded write first (it matches no row for another tenant's invoice
    or one that has been sent, paid or cancelled), then the same line replacement and
    totals the main app's editor performs. True when it wrote. There is no
    transaction here, so the window between the guard and the line swap is the same
    few milliseconds the main app's own status check leaves."""
    held = ((client or db()).table("invoices").update({"status": "draft"})
            .eq("id", invoice_id).eq("tenant_id", tenant_id).eq("status", "draft").execute())
    if not held.data:
        return False
    update_invoice_lines(invoice_id, tenant_id, lines, client=client)
    return True


# Columns that are null on a draft nothing has ever happened to. Anything else means the
# invoice reached the customer, or money touched it, at some point — whatever its status
# says now (the status page can put a sent invoice back to draft).
UNTOUCHED_INVOICE_NULLS = ("sent_at", "paid_at", "payment_method", "stripe_payment_intent_id",
                           "payment_link", "viewed_at")


def invoice_is_untouched_draft(inv: dict) -> bool:
    """A draft that was never sent, opened, linked for payment or paid against."""
    return (inv.get("status") == "draft"
            and all(not inv.get(col) for col in UNTOUCHED_INVOICE_NULLS)
            and not float(inv.get("amount_paid") or 0))


def delete_draft_invoice(invoice_id: str, tenant_id: str, client: Client | None = None) -> bool:
    """Remove a draft invoice nothing has ever happened to — ONE tenant-scoped DELETE
    guarded on every fact invoice_is_untouched_draft reads, so a draft sent a moment
    ago matches nothing. Its lines go in the same statement (invoice_lines.invoice_id
    is ON DELETE CASCADE). True when a row went.

    Its number is never reissued: invoice refs come from next_invoice_ref, a
    per-tenant counter that only goes up (tenant_counters.invoice_seq), never from
    the highest ref still standing."""
    q = ((client or db()).table("invoices").delete()
         .eq("id", invoice_id).eq("tenant_id", tenant_id).eq("status", "draft")
         .eq("amount_paid", 0))
    for col in UNTOUCHED_INVOICE_NULLS:
        q = q.is_(col, "null")
    return bool(q.execute().data)


def invoice_linked_quote_ref(invoice_id: str, tenant_id: str,
                             client: Client | None = None) -> str | None:
    """The ref of a quote of this business that points at the invoice (the one it
    became, or its deposit invoice), else None. A draft invoice a quote relies on is
    not binned from the Concierge: the link would silently drop."""
    c = client or db()
    for col in ("invoice_id", "deposit_invoice_id"):
        rows = (c.table("quotes").select("quote_ref")
                .eq("tenant_id", tenant_id).eq(col, invoice_id).limit(1).execute().data or [])
        if rows:
            return rows[0].get("quote_ref") or "a quote"
    return None


def list_invoices(tenant_id: str, client: Client | None = None) -> list[dict]:
    res = ((client or db()).table("invoices").select("*")
           .eq("tenant_id", tenant_id).order("created_at", desc=True).execute())
    return res.data or []


def get_invoice(invoice_id: str, tenant_id: str, client: Client | None = None) -> dict | None:
    res = ((client or db()).table("invoices").select("*")
           .eq("id", invoice_id).eq("tenant_id", tenant_id).limit(1).execute())
    return res.data[0] if res.data else None


def list_invoice_lines(invoice_id: str, tenant_id: str, client: Client | None = None) -> list[dict]:
    res = ((client or db()).table("invoice_lines").select("*")
           .eq("invoice_id", invoice_id).eq("tenant_id", tenant_id).execute())
    return res.data or []


# An invoice in one of these states no longer bills its work, so voiding or
# cancelling one FREES its bookings to be invoiced again. Anything else holds them.
INVOICE_DEAD_STATUSES = ("cancelled", "void")


def invoiced_booking_ids(tenant_id: str, booking_ids: list[str],
                         client: Client | None = None) -> set[str]:
    """Which of these bookings already sit on a LIVE invoice — the duplicate-invoice
    guard behind the generate flow (see app/invoicing._unbilled).

    Two queries per chunk instead of one join, because a bare select on invoice_lines
    would also hold work billed by an invoice that was later cancelled or voided —
    and that work SHOULD be re-invoiceable. The lines give (booking_id, invoice_id);
    the invoices' statuses decide which of those lines still count.

    Chunked for the same reason as invoice_lines_by_invoice: an `in_` filter travels
    in the query string.
    """
    out: set[str] = set()
    c = client or db()
    ids = [b for b in (booking_ids or []) if b]
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        lines = ((c.table("invoice_lines").select("booking_id, invoice_id")
                  .eq("tenant_id", tenant_id).in_("booking_id", chunk).execute()).data or [])
        if not lines:
            continue
        inv_ids = sorted({l["invoice_id"] for l in lines if l.get("invoice_id")})
        live: set[str] = set()
        for j in range(0, len(inv_ids), 100):
            rows = ((c.table("invoices").select("id, status")
                     .eq("tenant_id", tenant_id).in_("id", inv_ids[j:j + 100]).execute()).data or [])
            live |= {r["id"] for r in rows
                     if (r.get("status") or "") not in INVOICE_DEAD_STATUSES}
        out |= {l["booking_id"] for l in lines
                if l.get("booking_id") and l.get("invoice_id") in live}
    return out


def invoice_lines_by_invoice(tenant_id: str, invoice_ids: list[str],
                             client: Client | None = None) -> dict[str, list[dict]]:
    """Lines for many invoices at once, grouped by invoice_id — the export would
    otherwise fire one query per invoice. Chunked because an `in_` filter travels
    in the query string, so a tenant with hundreds of invoices would build a URL
    long enough to be rejected."""
    out: dict[str, list[dict]] = {}
    c = client or db()
    for i in range(0, len(invoice_ids), 100):
        chunk = invoice_ids[i:i + 100]
        if not chunk:
            continue
        res = (c.table("invoice_lines").select("*")
               .eq("tenant_id", tenant_id).in_("invoice_id", chunk).execute())
        for row in res.data or []:
            out.setdefault(row["invoice_id"], []).append(row)
    return out


EXTRA_NIGHTS_NOTE = "this covers the extra nights only"


def extra_night_drafts(tenant_id: str, booking_id: str, client: Client | None = None) -> list[dict]:
    """The DRAFT invoices a stay change raised for extra nights on this
    booking (record_edits.apply_stay_change), newest first — what a later
    shortening cancels before it asks the owner to refund anything by hand."""
    rows = ((client or db()).table("invoices")
            .select("id,invoice_ref,status,notes,created_at,total")
            .eq("tenant_id", tenant_id).eq("booking_id", booking_id).eq("status", "draft")
            .order("created_at", desc=True).limit(20).execute().data or [])
    return [r for r in rows if EXTRA_NIGHTS_NOTE in (r.get("notes") or "")]


def retire_pending_owner_messages(tenant_id: str, contact: str, subject: str,
                                  client: Client | None = None) -> list[dict]:
    """Skip earlier owner-message drafts with this exact subject to this
    contact that are still waiting — a newer one says the same thing more
    truly (two stay changes in a row left "now the 27th" AND "back to the
    26th" both waiting, 2026-09-24). Returns the retired rows."""
    res = ((client or db()).table("email_log").update({"status": "skipped"})
           .eq("tenant_id", tenant_id).eq("kind", "owner_message").eq("status", "pending")
           .eq("from_address", contact).eq("subject", subject).execute())
    return res.data or []


def list_waiting_invoices(tenant_id: str, *, days: int = 14,
                          client: Client | None = None) -> list[dict]:
    """Extra-night DRAFT invoices a stay change raised that nobody has sent
    yet — waiting on the owner like a draft email (Kris 2026-09-24: "it says
    a draft invoice is ready but doesn't show that as a draft or pending").
    A tap opens the invoice."""
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    try:
        rows = ((client or db()).table("invoices")
                .select("id,invoice_ref,total,notes,created_at,customer_email")
                .eq("tenant_id", tenant_id).eq("status", "draft").gte("created_at", cutoff)
                .ilike("notes", f"%{EXTRA_NIGHTS_NOTE}%")
                .order("created_at", desc=True).limit(5).execute().data or [])
    except Exception:
        log.warning("waiting invoices read failed (tenant=%s)", tenant_id, exc_info=True)
        return []
    return [{"type": "link", "invoice_id": r["id"], "created_at": r.get("created_at"),
             "href": f"/app/invoices/{r['id']}",
             "summary": f"Draft invoice {r.get('invoice_ref') or ''} — £{float(r.get('total') or 0):,.2f} extra night".replace("  ", " ")}
            for r in rows]


def set_invoice_status(invoice_id: str, tenant_id: str, status: str,
                       client: Client | None = None) -> None:
    fields: dict = {"status": status}
    if status == "sent":
        fields["sent_at"] = datetime.now(timezone.utc).isoformat()
    if status == "paid":
        fields["paid_at"] = datetime.now(timezone.utc).isoformat()
    (client or db()).table("invoices").update(fields) \
        .eq("id", invoice_id).eq("tenant_id", tenant_id).execute()


# Statuses an invoice can still be paid from — everything except a terminal state.
INVOICE_PAYABLE_STATUSES = ("draft", "sent", "overdue", "partially_paid")


def mark_invoice_paid(invoice_id: str, tenant_id: str, *, payment_method: str,
                      stripe_payment_intent_id: str | None = None,
                      client: Client | None = None) -> dict | None:
    """Settle an invoice in full. Idempotent: a second call (a Stripe webhook retry,
    or a manual mark racing the webhook) matches zero rows because the status filter
    excludes 'paid' — so the invoice is never double-updated and the caller can tell
    (None return) that nothing changed this time."""
    inv = get_invoice(invoice_id, tenant_id, client=client)
    if not inv or inv.get("status") not in INVOICE_PAYABLE_STATUSES:
        return None
    res = ((client or db()).table("invoices").update({
        "status": "paid",
        "paid_at": datetime.now(timezone.utc).isoformat(),
        "payment_method": payment_method,
        "stripe_payment_intent_id": stripe_payment_intent_id,
        "amount_paid": inv.get("total") or 0,
    }).eq("id", invoice_id).eq("tenant_id", tenant_id)
        .in_("status", list(INVOICE_PAYABLE_STATUSES)).execute())
    return res.data[0] if res.data else None


def get_invoice_by_ref(tenant_id: str, invoice_ref: str,
                       client: Client | None = None) -> dict | None:
    res = ((client or db()).table("invoices").select("*")
           .eq("tenant_id", tenant_id).eq("invoice_ref", invoice_ref).limit(1).execute())
    return res.data[0] if res.data else None


def get_invoice_any_tenant(invoice_id: str) -> dict | None:
    """The public pay page's lookup — the invoice uuid IS the capability (same pattern
    as /track/{token}), so this is service-role and unscoped by design."""
    res = db().table("invoices").select("*").eq("id", invoice_id).limit(1).execute()
    return res.data[0] if res.data else None


def open_invoices_for_customer(tenant_id: str, email: str, *, exclude_id: str | None = None,
                               limit: int = 10) -> list[dict]:
    """The customer's OTHER unpaid invoices with this business, for the pay page's
    "pay all together" offer (docs/plan-payments-panel.md §0.5). Service-role like
    get_invoice_any_tenant: the invoice uuid the customer arrived with is the
    capability, and the match is the same tenant + the same customer email, so a
    customer only ever sees their own. Drafts were never issued and dead invoices
    can't be paid, so only issued, payable rows count. Oldest first, so the offer
    reads like a statement."""
    addr = (email or "").strip().lower()
    if not addr:
        return []
    # ilike for case-insensitivity; its two wildcards escaped so an address with
    # an underscore can't match a stranger's.
    pattern = addr.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    res = (db().table("invoices").select("*").eq("tenant_id", tenant_id)
           .ilike("customer_email", pattern)
           .in_("status", ["sent", "overdue", "partially_paid"])
           .order("issue_date").limit(limit + 1).execute())
    return [r for r in (res.data or []) if r.get("id") != exclude_id][:limit]


def mark_invoice_payment_pending(invoice_id: str, tenant_id: str, *, payment_method: str,
                                 stripe_payment_intent_id: str | None) -> dict | None:
    """A bank payment (Bacs Direct Debit / Pay by Bank) has been submitted but not
    cleared (docs/plan-payments-panel.md §0.7). Stamps HOW it is being paid and the
    PaymentIntent onto a still-payable invoice so the pay page can say "in progress"
    instead of offering Pay Now again. Status is untouched: payment_intent.succeeded
    settles it through mark_invoice_paid as usual, overwriting both fields."""
    res = (db().table("invoices").update({
        "payment_method": payment_method,
        "stripe_payment_intent_id": stripe_payment_intent_id})
        .eq("id", invoice_id).eq("tenant_id", tenant_id)
        .in_("status", list(INVOICE_PAYABLE_STATUSES)).execute())
    return res.data[0] if res.data else None


def record_invoice_refund(invoice_id: str, tenant_id: str, *, amount_refunded_total: float,
                          key: str, note: str, client: Client | None = None) -> dict | None:
    """Apply a refund to a settled invoice from Stripe's ABSOLUTE amount_refunded on
    the charge, never a decrement, so a webhook retry, or our own refund route racing
    the charge.refunded event it causes, converge on the same row (plan §0.4).

    `key` is the ledger line's identity ("[ch_…@<pence refunded so far>]"): it is
    appended to notes with the human line, and a second call carrying a key already
    in notes returns None, which is what lets the caller skip the follow-up (push,
    agent log, CRM activity) on a replay. A full refund flips the status to
    'refunded' (money.py and summarize_invoice_rows count only 'paid', so it leaves
    "collected" by itself); a partial one keeps 'paid' with amount_paid reduced."""
    inv = get_invoice(invoice_id, tenant_id, client=client)
    if not inv or inv.get("status") not in ("paid", "refunded"):
        return None
    notes = inv.get("notes") or ""
    if key in notes:
        return None
    total = float(inv.get("total") or 0)
    refunded = max(0.0, min(float(amount_refunded_total or 0), total))
    remaining = round(total - refunded, 2)
    fields = {"amount_paid": remaining,
              "status": "refunded" if remaining <= 0 else "paid",
              "notes": notes + ("\n" if notes else "") + f"{note} {key}"}
    res = ((client or db()).table("invoices").update(fields)
           .eq("id", invoice_id).eq("tenant_id", tenant_id).execute())
    return res.data[0] if res.data else None


def invoices_for_crm_contact(tenant_id: str, contact_id: str, email: str | None = None,
                             limit: int = 50, client: Client | None = None) -> list[dict]:
    """A contact's invoices for the OWNER's CRM contact page.

    Matches on contact_id AND, for rows whose contact_id is null, on customer_email —
    invoices the bulk generate flow created before it started linking contacts would
    otherwise be invisible, and one extra filter here is a better trade than a backfill
    migration over every tenant's history.

    Deliberately separate from invoices_for_contact, which is the customer PORTAL's
    reader and is email-only, scoped to a verified address. Keeping them apart means a
    change to the owner's view can never widen what a customer can see.
    """
    c = client or db()
    rows: dict[str, dict] = {}
    res = (c.table("invoices").select("*").eq("tenant_id", tenant_id)
           .eq("contact_id", contact_id).order("created_at", desc=True)
           .limit(limit).execute())
    for r in res.data or []:
        rows[r["id"]] = r
    addr = (email or "").strip().lower()
    if addr:
        legacy = (c.table("invoices").select("*").eq("tenant_id", tenant_id)
                  .is_("contact_id", "null").ilike("customer_email", addr)
                  .order("created_at", desc=True).limit(limit).execute())
        for r in legacy.data or []:
            rows.setdefault(r["id"], r)
    return sorted(rows.values(), key=lambda r: r.get("created_at") or "",
                  reverse=True)[:limit]


def invoices_for_contact(tenant_id: str, email: str,
                         client: Client | None = None) -> list[dict]:
    """A customer's invoices, filtered server-side by their verified email — the
    portal's reader. Never exposes another customer's rows to the model."""
    email = (email or "").strip().lower()
    if not email:
        return []
    res = ((client or db()).table("invoices").select("*")
           .eq("tenant_id", tenant_id).ilike("customer_email", email)
           .order("created_at", desc=True).limit(50).execute())
    return res.data or []


def set_invoice_viewed(invoice_id: str, tenant_id: str) -> bool:
    """Stamp first customer view. Returns True only on the FIRST view (the update
    matches only while viewed_at is null) so the opt-in push fires once."""
    res = (db().table("invoices")
           .update({"viewed_at": datetime.now(timezone.utc).isoformat()})
           .eq("id", invoice_id).eq("tenant_id", tenant_id)
           .is_("viewed_at", "null").execute())
    return bool(res.data)


def set_invoice_payment_link(invoice_id: str, tenant_id: str, url: str,
                             client: Client | None = None) -> None:
    (client or db()).table("invoices").update({"payment_link": url}) \
        .eq("id", invoice_id).eq("tenant_id", tenant_id).execute()


def set_invoice_reference(invoice_id: str, tenant_id: str, reference: str,
                          client: Client | None = None) -> None:
    """The customer's PO / works-order number — metadata, editable in any
    status (a PO that arrives after sending still belongs on the record)."""
    (client or db()).table("invoices").update(
        {"customer_reference": (reference or "").strip()[:100] or None}
    ).eq("id", invoice_id).eq("tenant_id", tenant_id).execute()


def set_quote_reference(quote_id: str, tenant_id: str, reference: str,
                        client: Client | None = None) -> None:
    (client or db()).table("quotes").update(
        {"customer_reference": (reference or "").strip()[:100] or None}
    ).eq("id", quote_id).eq("tenant_id", tenant_id).execute()


def list_pending_invoice_chases(tenant_id: str, client: Client | None = None) -> list[dict]:
    """Payment-chase drafts still waiting for the owner's yes (src/app/invoice_chase.py
    staged them; kind='invoice_reminder'). The Money page matches them to overdue
    rows by the ref carried in body_in ('[invoice chase] INV-1041')."""
    res = ((client or db()).table("email_log")
           .select("id,from_address,body_in,created_at")
           .eq("tenant_id", tenant_id).eq("kind", "invoice_reminder")
           .eq("status", "pending").execute())
    return res.data or []


def record_invoice_reminder(invoice_id: str, tenant_id: str,
                            client: Client | None = None) -> None:
    inv = get_invoice(invoice_id, tenant_id, client=client)
    if not inv:
        return
    (client or db()).table("invoices").update({
        "reminders_sent": int(inv.get("reminders_sent") or 0) + 1,
        "last_reminder_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", invoice_id).eq("tenant_id", tenant_id).execute()


# Quotes (docs/modules/module-quotes.md) ----------------------------------------------
# The invoice engine's sibling: same ref minting, same settings surface, same
# line contract (amount = the line TOTAL, net of VAT). Quotes add two ideas
# invoices don't have: optional lines the customer ticks on /q (totals always
# recompute server-side from the SELECTED lines), and an acceptance snapshot —
# the exact lines + totals as agreed, frozen at the moment of the typed-name
# signature, immutable afterwards (the scope-dispute shield).

DEFAULT_QUOTE_VALID_DAYS = 30

# A quote in one of these states can no longer be acted on by the customer.
QUOTE_TERMINAL_STATUSES = ("accepted", "declined", "expired", "invoiced", "cancelled")


def next_quote_ref(tenant_id: str, client: Client | None = None) -> str:
    res = (client or db()).rpc("next_quote_ref", {"p_tenant_id": tenant_id}).execute()
    data = res.data
    if isinstance(data, list):
        data = data[0] if data else None
    return data or "QUO-1001"


def _quote_totals(tenant_id: str, lines: list[dict],
                  client: Client | None = None, *,
                  include: bool | None = None) -> tuple[float, float, float]:
    """(subtotal, vat, total) over the SELECTED lines only — a deselected optional
    line exists on the quote but never counts. Same VAT source as invoices.
    `include` is the quote's own price basis (a stored quote recomputes on the
    basis it was made under; a quote with none passes False); None = the
    settings' basis now, for a quote being created."""
    from app import invoice_config
    settings = invoice_config.settings_for(get_invoice_settings(tenant_id, client=client))
    return invoice_config.split_vat(
        sum(float(l.get("amount") or 0) for l in lines if l.get("is_selected", True)),
        settings, include=include)


def deal_id_for_job(tenant_id: str, *, booking_id: str | None = None,
                    email_log_id: str | None = None,
                    client: Client | None = None) -> str | None:
    """The deal THIS JOB belongs to, walked from the job itself rather than
    guessed from the customer.

    quotes/invoices carry the booking they are for; a booking carries the inbound
    that created it (bookings.email_log_id); and the clustering decision is
    stamped on that email's activity rows (crm_set_activity_deal). So the chain
    quote → booking → email → deal is exact, where open_deal_id_for_contact can
    only ask "does this customer happen to have exactly one deal?" — which is
    false for every repeat customer with two live jobs (live 2026-09-04: a
    services customer with a repair AND a safety-check job got a quote with no
    deal at all, so the accepted-quote stage nudge never fired).

    None when the job did not come from an email (portal and manually-added
    bookings carry no email_log_id) or the email was never clustered onto a
    deal — the caller falls back as before. Deliberately does NOT require the
    deal to still be open: this is the job's identity, not its lifecycle, and
    the final invoice for a won job belongs on that won deal."""
    log_id = (email_log_id or "").strip() or None
    if not log_id and booking_id:
        log_id = (get_booking(booking_id, tenant_id, client=client) or {}).get("email_log_id")
    if not log_id:
        return None
    try:
        res = ((client or db()).table("crm_activities").select("deal_id")
               .eq("tenant_id", tenant_id).eq("source_id", log_id)
               .not_.is_("deal_id", "null").limit(1).execute())
    except Exception:
        return None
    return (res.data[0].get("deal_id") if res.data else None)


def open_deal_id_for_contact(tenant_id: str, contact_id: str | None,
                             client: Client | None = None) -> str | None:
    """The contact's single OPEN deal, or None when they have none or several.
    Quotes and invoices raised for a contact with one live deal belong to it —
    the form-created QUO-1001 carried no deal_id (live 2026-09-03), so the
    accepted-quote stage nudge never fired and the deal sat at 'New enquiry'.
    Ambiguity (two open deals) is left to the owner rather than guessed."""
    if not contact_id:
        return None
    try:
        open_deals = [d for d in crm_list_deals(tenant_id, contact_id=contact_id, client=client)
                      if (d.get("status") or "open") == "open"]
    except Exception:
        return None
    return open_deals[0]["id"] if len(open_deals) == 1 else None


def _quote_line_row(tenant_id: str, quote_id: str, l: dict, i: int) -> dict:
    """One quote_lines insert row. The deposit-rule flag is sent only when set,
    so a line that doesn't carry it writes exactly what it always did."""
    r = {"tenant_id": tenant_id, "quote_id": quote_id,
         "booking_id": l.get("booking_id"), "description": l["description"],
         "quantity": l.get("quantity") or 1,
         "unit_price": l.get("unit_price", l["amount"]),
         "amount": l["amount"],
         "is_optional": bool(l.get("is_optional")),
         "is_selected": bool(l.get("is_selected", True)),
         "line_order": int(l.get("line_order") or i),
         "kb_entry_id": l.get("kb_entry_id")}
    if l.get("counts_to_deposit_rule"):
        r["counts_to_deposit_rule"] = True
    return r


def _quote_line_rows(tenant_id: str, quote_id: str, lines: list[dict]) -> list[dict]:
    """The rows for ONE bulk insert. A bulk insert needs every row to carry the
    same keys: a quote mixing ticked and unticked lines sent the flag on some
    rows only and the whole insert was refused, leaving a quote with totals and
    no lines (1 Oct 2026, the first quote to use the tick). When any line is
    ticked, every row states the flag; when none is, no row does, so a quote
    without ticks writes exactly what it always did."""
    rows = [_quote_line_row(tenant_id, quote_id, l, i) for i, l in enumerate(lines)]
    if any("counts_to_deposit_rule" in r for r in rows):
        for r in rows:
            r.setdefault("counts_to_deposit_rule", False)
    return rows


def create_quote(tenant_id: str, customer_email: str | None, customer_name: str | None,
                 lines: list[dict], client: Client | None = None, *,
                 contact_id: str | None = None, deal_id: str | None = None,
                 booking_id: str | None = None, email_log_id: str | None = None,
                 title: str | None = None, intro: str | None = None,
                 notes: str | None = None, valid_until: str | None = None,
                 show_item_prices: bool = True, deposit_percent: float | None = None,
                 customer_reference: str | None = None,
                 created_by: str = "owner") -> dict | None:
    """Create one quote + its lines. `lines` = [{description, amount, quantity?,
    unit_price?, booking_id?, kb_entry_id?, is_optional?, is_selected?, line_order?}].
    Totals are computed here from the selected lines (VAT per the tenant's shared
    invoice settings). valid_until defaults from invoice_settings.default_quote_valid_days;
    deposit_percent defaults from invoice_settings.default_deposit_percent (pass a
    value to override, 0 to force no deposit)."""
    raw = get_invoice_settings(tenant_id, client=client) or {}
    from app import invoice_config
    include = invoice_config.settings_for(raw).get("prices_include_vat") is True
    subtotal, vat, total = _quote_totals(tenant_id, lines, client=client)   # the settings' basis
    ref = next_quote_ref(tenant_id, client=client)
    today = datetime.now(timezone.utc).date()
    try:
        valid_days = int(raw.get("default_quote_valid_days") or DEFAULT_QUOTE_VALID_DAYS)
    except (TypeError, ValueError):
        valid_days = DEFAULT_QUOTE_VALID_DAYS
    deposit_base = None      # null = the percentage is of the whole subtotal
    if deposit_percent is None:
        # The usual default, or a deposit rule on this document's total
        # (invoice_config.deposit_percent_for — the one place that decides).
        from app import quoting
        _st = invoice_config.settings_for(raw)
        ticked_net = quoting.ticked_net_sum(lines)
        # A "ticked" rule is tested on the GROSS sum of the ticked lines (the
        # same gross basis as `total`); its deposit is taken of the ticked sum
        # as written (their net sum when VAT is added, the same sum when prices
        # include VAT).
        ticked_gross = (None if ticked_net is None
                        else invoice_config.split_vat(ticked_net, _st, include=include)[2])
        deposit_percent, _why = invoice_config.deposit_percent_for(
            _st, total, ticked_amount=ticked_gross)
        if _why == "value":
            _rule = invoice_config.deposit_rule_for(_st, total, ticked_gross)
            if _rule and _rule.get("basis") == "ticked":
                deposit_base = ticked_net
    if deposit_percent is not None and not (0 < float(deposit_percent) <= 100):
        deposit_percent = None
    if deal_id is None:
        # The job's own deal beats "the customer's only deal" — and beats
        # nothing at all, which is what a customer with two live jobs used to get.
        deal_id = deal_id_for_job(tenant_id, booking_id=booking_id,
                                  email_log_id=email_log_id, client=client) \
            or open_deal_id_for_contact(tenant_id, contact_id, client=client)
    quote_row = {
        "tenant_id": tenant_id, "quote_ref": ref,
        "customer_email": customer_email, "customer_name": customer_name,
        "contact_id": contact_id, "deal_id": deal_id, "booking_id": booking_id,
        "email_log_id": email_log_id,
        "title": title, "intro": intro, "notes": notes,
        "issue_date": today.isoformat(),
        "valid_until": valid_until or (today + timedelta(days=valid_days)).isoformat(),
        "subtotal": subtotal, "vat": vat, "total": total,
        "show_item_prices": show_item_prices, "deposit_percent": deposit_percent,
        "customer_reference": (customer_reference or "").strip()[:100] or None,
        "created_by": created_by,
    }
    if deposit_base is not None:      # sent only when set: every other write is unchanged
        quote_row["deposit_base"] = deposit_base
    if include:                       # likewise: the basis is stored only when True
        quote_row["prices_include_vat"] = True
    res = (client or db()).table("quotes").insert(quote_row).execute()
    row = res.data[0] if res.data else None
    if row and lines:
        (client or db()).table("quote_lines").insert(
            _quote_line_rows(tenant_id, row["id"], lines)).execute()
    return row


def list_quotes(tenant_id: str, client: Client | None = None) -> list[dict]:
    res = ((client or db()).table("quotes").select("*")
           .eq("tenant_id", tenant_id).order("created_at", desc=True).execute())
    return res.data or []


def get_quote(quote_id: str, tenant_id: str, client: Client | None = None) -> dict | None:
    res = ((client or db()).table("quotes").select("*")
           .eq("id", quote_id).eq("tenant_id", tenant_id).limit(1).execute())
    return res.data[0] if res.data else None


def get_quote_by_ref(tenant_id: str, quote_ref: str,
                     client: Client | None = None) -> dict | None:
    res = ((client or db()).table("quotes").select("*")
           .eq("tenant_id", tenant_id).eq("quote_ref", quote_ref).limit(1).execute())
    return res.data[0] if res.data else None


def get_quote_any_tenant(quote_id: str) -> dict | None:
    """The public /q page's lookup — the quote uuid IS the capability (same pattern
    as /pay/{invoice_id}), so this is service-role and unscoped by design."""
    res = db().table("quotes").select("*").eq("id", quote_id).limit(1).execute()
    return res.data[0] if res.data else None


def list_quote_lines(quote_id: str, tenant_id: str,
                     client: Client | None = None) -> list[dict]:
    res = ((client or db()).table("quote_lines").select("*")
           .eq("quote_id", quote_id).eq("tenant_id", tenant_id)
           .order("line_order").order("created_at").execute())
    return res.data or []


def update_quote_lines(quote_id: str, tenant_id: str, lines: list[dict],
                       client: Client | None = None) -> None:
    """Replace a quote's lines and recompute its totals. Preserves booking_id,
    kb_entry_id, is_optional and is_selected through the edit (the invoice editor
    once dropped booking_id and silently freed billed work — same lesson). A
    post-send edit bumps `version`, so /q shows 'revised'; accepted and other
    terminal quotes must not reach here (callers gate on status)."""
    c = client or db()
    quote = get_quote(quote_id, tenant_id, client=client)
    if quote is None:
        return
    c.table("quote_lines").delete().eq("quote_id", quote_id) \
        .eq("tenant_id", tenant_id).execute()
    if lines:
        c.table("quote_lines").insert(
            _quote_line_rows(tenant_id, quote_id, lines)).execute()
    subtotal, vat, total = _quote_totals(
        tenant_id, lines, client=client, include=quote.get("prices_include_vat") is True)
    fields = {"subtotal": subtotal, "vat": vat, "total": total,
              "updated_at": datetime.now(timezone.utc).isoformat()}
    if quote.get("deposit_base") is not None:
        # A deposit taken of the ticked lines follows them (0 = no deposit due);
        # the percentage is left alone. A quote with no base is untouched.
        from app import quoting
        fields["deposit_base"] = quoting.ticked_net_sum(lines) or 0.0
    if quote.get("status") == "sent":
        fields["version"] = int(quote.get("version") or 1) + 1
    c.table("quotes").update(fields) \
        .eq("id", quote_id).eq("tenant_id", tenant_id).execute()


def set_quote_status(quote_id: str, tenant_id: str, status: str,
                     client: Client | None = None) -> None:
    fields: dict = {"status": status,
                    "updated_at": datetime.now(timezone.utc).isoformat()}
    if status == "sent":
        fields["sent_at"] = datetime.now(timezone.utc).isoformat()
    (client or db()).table("quotes").update(fields) \
        .eq("id", quote_id).eq("tenant_id", tenant_id).execute()


def set_quote_viewed(quote_id: str, tenant_id: str) -> bool:
    """Stamp customer views on /q. Returns True only on the FIRST view (the update
    matches only while viewed_at is null — mirror set_invoice_viewed) so the
    'quote viewed' push fires once; view_count always increments (the 'they keep
    looking' escalation signal reads it)."""
    quote = get_quote_any_tenant(quote_id)
    if quote:
        db().table("quotes").update(
            {"view_count": int(quote.get("view_count") or 0) + 1}) \
            .eq("id", quote_id).eq("tenant_id", tenant_id).execute()
    res = (db().table("quotes")
           .update({"viewed_at": datetime.now(timezone.utc).isoformat()})
           .eq("id", quote_id).eq("tenant_id", tenant_id)
           .is_("viewed_at", "null").execute())
    return bool(res.data)


def set_quote_line_selected(quote_id: str, tenant_id: str, line_id: str,
                            selected: bool) -> bool:
    """The /q optional-line tick. Only optional lines can be toggled, and only
    while the quote is still 'sent' — after acceptance the snapshot is the truth
    and the lines are display-only. Recomputes the quote's totals server-side
    (the customer's browser is never the calculator)."""
    quote = get_quote(quote_id, tenant_id)
    if not quote or quote.get("status") != "sent":
        return False
    res = (db().table("quote_lines").update({"is_selected": bool(selected)})
           .eq("id", line_id).eq("quote_id", quote_id).eq("tenant_id", tenant_id)
           .eq("is_optional", True).execute())
    if not res.data:
        return False
    lines = list_quote_lines(quote_id, tenant_id)
    subtotal, vat, total = _quote_totals(
        tenant_id, lines, include=quote.get("prices_include_vat") is True)
    fields = {"subtotal": subtotal, "vat": vat, "total": total}
    if quote.get("deposit_base") is not None:
        # A deposit taken of the ticked lines follows the customer's own choice
        # too: an optional ticked line they untick stops counting, so the page
        # and the acceptance both show the amount actually due.
        from app import quoting
        fields["deposit_base"] = quoting.ticked_net_sum(lines) or 0.0
    db().table("quotes").update(fields) \
        .eq("id", quote_id).eq("tenant_id", tenant_id).execute()
    return True


def accept_quote(quote_id: str, tenant_id: str, accepted_name: str) -> dict | None:
    """The typed-name acceptance. Freezes acceptance_snapshot (selected lines +
    totals + version — the exact document agreed to) and flips sent → accepted in
    ONE status-guarded update, so a double-submit or a race with a concurrent
    edit/cancel matches zero rows and returns None (mirror mark_invoice_paid)."""
    quote = get_quote(quote_id, tenant_id)
    if not quote or quote.get("status") != "sent":
        return None
    lines = list_quote_lines(quote_id, tenant_id)
    selected = [l for l in lines if l.get("is_selected", True)]
    snapshot = {
        "version": quote.get("version") or 1,
        "subtotal": quote.get("subtotal"), "vat": quote.get("vat"),
        "total": quote.get("total"),
        "lines": [{"description": l.get("description"),
                   "quantity": l.get("quantity"), "unit_price": l.get("unit_price"),
                   "amount": l.get("amount"), "booking_id": l.get("booking_id"),
                   "kb_entry_id": l.get("kb_entry_id"),
                   "is_optional": l.get("is_optional"),
                   **({"counts_to_deposit_rule": True} if l.get("counts_to_deposit_rule") else {})}
                  for l in selected],
    }
    if quote.get("deposit_base") is not None:
        snapshot["deposit_base"] = quote["deposit_base"]
    if quote.get("prices_include_vat") is True:     # carried only when True
        snapshot["prices_include_vat"] = True
    res = (db().table("quotes").update({
        "status": "accepted",
        "accepted_at": datetime.now(timezone.utc).isoformat(),
        "accepted_name": accepted_name.strip()[:200],
        "acceptance_snapshot": snapshot,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", quote_id).eq("tenant_id", tenant_id)
        .eq("status", "sent").execute())
    return res.data[0] if res.data else None


def decline_quote(quote_id: str, tenant_id: str,
                  reason: str | None = None) -> dict | None:
    """Customer decline — status-guarded like accept_quote, so decline can never
    overwrite an acceptance that beat it."""
    res = (db().table("quotes").update({
        "status": "declined",
        "declined_at": datetime.now(timezone.utc).isoformat(),
        "decline_reason": (reason or "").strip()[:500] or None,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", quote_id).eq("tenant_id", tenant_id)
        .eq("status", "sent").execute())
    return res.data[0] if res.data else None


def link_quote_invoice(quote_id: str, tenant_id: str, invoice_id: str, *,
                       deposit: bool = False, client: Client | None = None) -> None:
    """Record what the quote became. The full conversion flips status → invoiced;
    a deposit invoice just links (the quote stays accepted — the balance is
    still to be invoiced)."""
    fields: dict = {("deposit_invoice_id" if deposit else "invoice_id"): invoice_id,
                    "updated_at": datetime.now(timezone.utc).isoformat()}
    if not deposit:
        fields["status"] = "invoiced"
    (client or db()).table("quotes").update(fields) \
        .eq("id", quote_id).eq("tenant_id", tenant_id).execute()


def record_quote_reminder(quote_id: str, tenant_id: str,
                          client: Client | None = None) -> None:
    quote = get_quote(quote_id, tenant_id, client=client)
    if not quote:
        return
    (client or db()).table("quotes").update({
        "reminders_sent": int(quote.get("reminders_sent") or 0) + 1,
        "last_reminder_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", quote_id).eq("tenant_id", tenant_id).execute()


def quotes_for_crm_contact(tenant_id: str, contact_id: str, email: str | None = None,
                           limit: int = 50, client: Client | None = None) -> list[dict]:
    """A contact's quotes for the OWNER's CRM contact page — contact_id plus the
    customer_email fallback for unlinked rows, exactly like invoices_for_crm_contact
    (and kept separate from quotes_for_contact, the portal's reader, for the same
    never-widen-what-a-customer-sees reason)."""
    c = client or db()
    rows: dict[str, dict] = {}
    res = (c.table("quotes").select("*").eq("tenant_id", tenant_id)
           .eq("contact_id", contact_id).order("created_at", desc=True)
           .limit(limit).execute())
    for r in res.data or []:
        rows[r["id"]] = r
    addr = (email or "").strip().lower()
    if addr:
        legacy = (c.table("quotes").select("*").eq("tenant_id", tenant_id)
                  .is_("contact_id", "null").ilike("customer_email", addr)
                  .order("created_at", desc=True).limit(limit).execute())
        for r in legacy.data or []:
            rows.setdefault(r["id"], r)
    return sorted(rows.values(), key=lambda r: r.get("created_at") or "",
                  reverse=True)[:limit]


def quotes_for_contact(tenant_id: str, email: str,
                       client: Client | None = None) -> list[dict]:
    """A customer's quotes, filtered server-side by their verified email — the
    portal's reader. Drafts never shown (they're the owner's workspace)."""
    email = (email or "").strip().lower()
    if not email:
        return []
    res = ((client or db()).table("quotes").select("*")
           .eq("tenant_id", tenant_id).ilike("customer_email", email)
           .neq("status", "draft")
           .order("created_at", desc=True).limit(50).execute())
    return res.data or []


def list_open_sent_quotes() -> list[dict]:
    """Cross-tenant: every quote still out for an answer — the daily chase job's
    working set (service role; the job fans out per tenant itself, mirror of
    list_unpaid_due_invoices)."""
    res = db().table("quotes").select("*").eq("status", "sent").execute()
    return res.data or []


def has_inbound_from_since(tenant_id: str, email: str, since_iso: str) -> bool:
    """Did this contact write back after `since_iso`? The chase job's stop
    condition: an inbound reply means the conversation is live — a robot nudge
    on top of it would read as not listening. kind='inbound' filters out our
    own outbound rows (from_address holds the contact on BOTH directions)."""
    email = (email or "").strip()
    if not (email and since_iso):
        return False
    res = (db().table("email_log").select("id")
           .eq("tenant_id", tenant_id).ilike("from_address", email)
           .eq("kind", "inbound").gte("created_at", since_iso)
           .limit(1).execute())
    return bool(res.data)


def bookings_for_log(email_log_id: str, tenant_id: str,
                     client: Client | None = None) -> list[dict]:
    """The bookings/holds one draft placed (bookings.email_log_id — the D4
    link resolve_holds_for_log already relies on). The auto-draft hook reads
    these for their quoted_amount."""
    res = ((client or db()).table("bookings").select("*")
           .eq("tenant_id", tenant_id).eq("email_log_id", email_log_id)
           .order("created_at").execute())
    return res.data or []


def quote_for_log(email_log_id: str, tenant_id: str,
                  client: Client | None = None) -> dict | None:
    """The latest live quote linked to one conversation draft — the auto-draft
    dedupe AND the send-time link lookup. Cancelled quotes don't count."""
    res = ((client or db()).table("quotes").select("*")
           .eq("tenant_id", tenant_id).eq("email_log_id", email_log_id)
           .neq("status", "cancelled")
           .order("created_at", desc=True).limit(1).execute())
    return res.data[0] if res.data else None


def open_quote_for_customer(tenant_id: str, customer_email: str,
                            client: Client | None = None) -> dict | None:
    """A quote already in play with this customer — sent (awaiting an answer) or
    accepted (agreed, not yet invoiced). The auto-draft guard: a follow-up chat
    about a job that already has a quote must not mint a second, stray one
    (live 2026-09-29: the start-date confirmation drafted a £85 QUO-1024 beside
    the accepted £2,850 QUO-1023)."""
    if not customer_email:
        return None
    res = ((client or db()).table("quotes").select("*")
           .eq("tenant_id", tenant_id).ilike("customer_email", customer_email.strip())
           .in_("status", ["sent", "accepted"])
           .order("created_at", desc=True).limit(1).execute())
    return res.data[0] if res.data else None


def quote_summary(tenant_id: str, since_iso: str | None = None) -> dict:
    """Lightweight quote aggregate for the admin console — one read, bucketed in
    Python (the invoice_summary shape). Their pipeline, not platform revenue."""
    q = db().table("quotes").select("status,total,created_at") \
        .eq("tenant_id", tenant_id)
    if since_iso:
        q = q.gte("created_at", since_iso)
    rows = q.execute().data or []
    live = [r for r in rows if (r.get("status") or "") not in ("draft", "cancelled")]
    accepted = [r for r in live if r.get("status") in ("accepted", "invoiced")]
    answered = len(accepted) + sum(1 for r in live
                                   if r.get("status") in ("declined", "expired"))
    return {
        "sent": len(live),
        "accepted": len(accepted),
        "outstanding": sum(1 for r in live if r.get("status") == "sent"),
        "expired": sum(1 for r in live if r.get("status") == "expired"),
        "quoted": round(sum(float(r.get("total") or 0) for r in live), 2),
        "won": round(sum(float(r.get("total") or 0) for r in accepted), 2),
        "acceptance_rate": (round(100 * len(accepted) / answered)
                            if answered else None),
    }


def withdraw_draft_quote(quote_id: str, tenant_id: str,
                         client: Client | None = None) -> bool:
    """draft -> cancelled (the status every other withdrawn quote uses), guarded
    on status so a quote the owner sent a moment ago is never pulled back.
    True when this call flipped it."""
    res = ((client or db()).table("quotes").update({
        "status": "cancelled",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", quote_id).eq("tenant_id", tenant_id)
        .eq("status", "draft").execute())
    return bool(res.data)


def quote_totals(tenant_id: str, lines: list[dict], *, include: bool | None = None,
                 client: Client | None = None) -> tuple[float, float, float]:
    """(subtotal, vat, total) over the SELECTED lines — the calculation
    update_quote_lines stores, public so a confirm card's preview is the same number
    the save writes. `include` = the quote's own price basis (quotes.prices_include_vat
    is True)."""
    return _quote_totals(tenant_id, lines, client=client, include=include)


def update_draft_quote_lines(quote_id: str, tenant_id: str, lines: list[dict],
                             client: Client | None = None) -> bool:
    """update_quote_lines, but only while the quote is still a DRAFT in this business:
    a guarded write first (no row for another tenant's quote, or one that has been
    sent, answered or cancelled), then the same line replacement and totals the main
    app's editor performs. True when it wrote. See update_draft_invoice_lines for the
    note on the missing transaction."""
    held = ((client or db()).table("quotes")
            .update({"updated_at": datetime.now(timezone.utc).isoformat()})
            .eq("id", quote_id).eq("tenant_id", tenant_id).eq("status", "draft").execute())
    if not held.data:
        return False
    update_quote_lines(quote_id, tenant_id, lines, client=client)
    return True


# Columns that are null on a draft quote nothing has ever happened to; the counters
# (views, chases) must be zero too. A sent quote put back to draft would still show here.
UNTOUCHED_QUOTE_NULLS = ("sent_at", "viewed_at", "accepted_at", "declined_at",
                         "invoice_id", "deposit_invoice_id")


def quote_is_untouched_draft(quote: dict) -> bool:
    """A draft that was never sent, opened, answered, chased or turned into an invoice."""
    return (quote.get("status") == "draft"
            and all(not quote.get(col) for col in UNTOUCHED_QUOTE_NULLS)
            and not int(quote.get("view_count") or 0)
            and not int(quote.get("reminders_sent") or 0))


def delete_draft_quote(quote_id: str, tenant_id: str, client: Client | None = None) -> bool:
    """Remove a draft quote nothing has ever happened to — ONE tenant-scoped DELETE
    guarded on every fact quote_is_untouched_draft reads. Its lines go in the same
    statement (quote_lines.quote_id is ON DELETE CASCADE). True when a row went. Its
    number is never reissued: quote refs come from next_quote_ref, a per-tenant
    counter that only goes up (tenant_counters.quote_seq)."""
    q = ((client or db()).table("quotes").delete()
         .eq("id", quote_id).eq("tenant_id", tenant_id).eq("status", "draft")
         .eq("view_count", 0).eq("reminders_sent", 0))
    for col in UNTOUCHED_QUOTE_NULLS:
        q = q.is_(col, "null")
    return bool(q.execute().data)


def quote_lines_by_quote(tenant_id: str, quote_ids: list[str],
                         client: Client | None = None) -> dict[str, list[dict]]:
    """Lines for many quotes at once, grouped by quote_id and in the quote's own line
    order (the invoice twin is invoice_lines_by_invoice). Chunked: an `in_` filter
    travels in the query string."""
    out: dict[str, list[dict]] = {}
    c = client or db()
    for i in range(0, len(quote_ids), 100):
        chunk = quote_ids[i:i + 100]
        if not chunk:
            continue
        res = (c.table("quote_lines").select("*")
               .eq("tenant_id", tenant_id).in_("quote_id", chunk)
               .order("line_order").order("created_at").execute())
        for row in res.data or []:
            out.setdefault(row["quote_id"], []).append(row)
    return out


def expire_quote(quote_id: str, tenant_id: str) -> bool:
    """sent → expired, guarded on status so a race with an acceptance loses
    (the customer's yes beats the calendar). True when this call flipped it."""
    res = (db().table("quotes").update({
        "status": "expired",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", quote_id).eq("tenant_id", tenant_id)
        .eq("status", "sent").execute())
    return bool(res.data)


def set_invoice_overdue_milestone(invoice_id: str, tenant_id: str, days: int,
                                  client: Client | None = None) -> None:
    """The daily overdue job's stamp: flip status to 'overdue' and record the milestone
    so each of 7/14/30 pushes exactly once. Chase-now stamps it too — a manual
    chase satisfies the milestone it lands in, or the morning run re-chases."""
    (client or db()).table("invoices").update(
        {"status": "overdue", "overdue_notified_days": days}) \
        .eq("id", invoice_id).eq("tenant_id", tenant_id) \
        .in_("status", ["sent", "overdue"]).execute()


def list_unpaid_due_invoices(today_iso: str) -> list[dict]:
    """Cross-tenant: every issued-but-unsettled invoice past its due date — the daily
    overdue job's working set (service role; the job fans out per tenant itself)."""
    res = (db().table("invoices").select("*")
           .in_("status", ["sent", "overdue", "partially_paid"])
           .lt("due_date", today_iso).execute())
    return res.data or []


def invoice_summary(tenant_id: str, since_iso: str | None = None) -> dict:
    """Lightweight revenue aggregate for the admin console — one read, bucketed in
    Python (mirrors usage.summarize's shape: no reporting engine)."""
    q = db().table("invoices").select(
        "status,total,amount_paid,sent_at,paid_at,due_date,created_at") \
        .eq("tenant_id", tenant_id)
    if since_iso:
        q = q.gte("created_at", since_iso)
    return summarize_invoice_rows(q.execute().data or [])


def summarize_invoice_rows(rows: list[dict]) -> dict:
    """Pure bucketing for invoice_summary (split out so the maths is testable)."""
    out = {"sent": 0, "paid": 0, "overdue": 0, "invoiced": 0.0, "collected": 0.0,
           "outstanding": 0.0, "avg_days_to_pay": None}
    pay_days = []
    today = datetime.now(timezone.utc).date().isoformat()
    for r in rows:
        total = float(r.get("total") or 0)
        status = r.get("status")
        if status == "paid":
            out["paid"] += 1
            out["invoiced"] += total
            out["collected"] += float(r.get("amount_paid") or total)
            if r.get("sent_at") and r.get("paid_at"):
                try:
                    d = (datetime.fromisoformat(r["paid_at"])
                         - datetime.fromisoformat(r["sent_at"])).days
                    pay_days.append(max(d, 0))
                except ValueError:
                    pass
        elif status in ("sent", "overdue", "partially_paid"):
            out["sent"] += 1
            out["invoiced"] += total
            paid_part = float(r.get("amount_paid") or 0)
            out["collected"] += paid_part
            out["outstanding"] += total - paid_part
            if status == "overdue" or (r.get("due_date") or "9999") < today:
                out["overdue"] += 1
    if pay_days:
        out["avg_days_to_pay"] = round(sum(pay_days) / len(pay_days), 1)
    for k in ("invoiced", "collected", "outstanding"):
        out[k] = round(out[k], 2)
    return out


def get_stripe_account_id(tenant_id: str, client: Client | None = None) -> str | None:
    res = ((client or db()).table("tenants").select("stripe_account_id")
           .eq("id", tenant_id).limit(1).execute())
    return (res.data[0].get("stripe_account_id") if res.data else None) or None


def set_stripe_account_id(tenant_id: str, account_id: str | None) -> None:
    """Connect/disconnect the tenant's own Stripe account (service role — the OAuth
    callback has no user client)."""
    db().table("tenants").update({"stripe_account_id": account_id}) \
        .eq("id", tenant_id).execute()


# ── invoicing module — modular invoice settings (docs/modules/module-invoicing.md) ───
def get_invoice_settings(tenant_id: str, client: Client | None = None) -> dict | None:
    res = ((client or db()).table("invoice_settings").select("*")
           .eq("tenant_id", tenant_id).limit(1).execute())
    return res.data[0] if res.data else None


class InvoiceSettingsStale(RuntimeError):
    pass


def save_invoice_settings_checked(tenant_id, expected, fields, quote_mode, client=None):
    """One snapshot-checked transaction for the invoice page and quote preference."""
    try:
        (client or db()).rpc("save_invoice_settings_checked", {
            "p_tenant": tenant_id, "p_expected": expected, "p_fields": fields,
            "p_quote_mode": quote_mode,
        }).execute()
    except Exception as exc:
        if "BLAM8_SETTINGS_STALE" in str(exc):
            raise InvoiceSettingsStale("Invoice settings changed; reload.") from None
        raise


def upsert_invoice_settings(tenant_id: str, fields: dict, client: Client | None = None) -> None:
    (client or db()).table("invoice_settings").upsert(
        {"tenant_id": tenant_id, **fields,
         "updated_at": datetime.now(timezone.utc).isoformat()}
    ).execute()


# Driver hours (compliance) ---------------------------------------------------
def list_driver_hours(tenant_id: str, date_from_iso: str | None = None,
                      client: Client | None = None) -> list[dict]:
    q = (client or db()).table("driver_hours").select("*").eq("tenant_id", tenant_id)
    if date_from_iso:
        q = q.gte("work_date", date_from_iso)
    return (q.order("work_date").execute()).data or []


def add_driver_hours(tenant_id: str, driver_id: str, work_date: str, hours: float,
                     client: Client | None = None) -> None:
    (client or db()).table("driver_hours").insert({
        "tenant_id": tenant_id, "driver_id": driver_id,
        "work_date": work_date, "hours": hours,
    }).execute()


# Feature requests / support tickets ------------------------------------------
def next_ticket_reference() -> str | None:
    """Mint the next BLAM8-XXXX reference (global sequence, service-role rpc).
    Best-effort: None when the migration isn't applied yet — the row still files,
    just without a quotable reference."""
    try:
        res = db().rpc("next_ticket_reference", {}).execute()
        return res.data or None
    except Exception:
        log.warning("next_ticket_reference rpc failed — filing without a reference")
        return None


def add_feature_request(tenant_id: str | None, body: str, category: str | None = None,
                        submitted_by: str | None = None, industry: str | None = None,
                        kind: str = "feature", user_email: str | None = None,
                        account_type: str | None = None, error_message: str | None = None,
                        severity: str = "normal", status: str | None = None,
                        page_url: str | None = None, user_agent: str | None = None,
                        client: Client | None = None) -> str | None:
    """File a request/report/ticket; returns its BLAM8-XXXX reference (or None).
    tenant_id None = an anonymous website ticket — those rows are service-role
    only (RLS: a NULL tenant never matches auth_tenant_ids())."""
    reference = next_ticket_reference()
    row = {
        "tenant_id": tenant_id, "body": body, "category": category,
        "submitted_by": submitted_by, "industry": industry, "kind": kind,
        "reference": reference, "user_email": user_email,
        "account_type": account_type, "error_message": error_message,
        "severity": severity, "page_url": page_url, "user_agent": user_agent,
    }
    if status:
        row["status"] = status
    # Legacy-schema tolerance: before the support-tickets migration the new
    # columns don't exist and PostgREST rejects them — retry with the core shape
    # so a submission is never lost to a half-deployed migration.
    try:
        (client or db()).table("feature_requests").insert(row).execute()
    except Exception:
        (client or db()).table("feature_requests").insert({
            "tenant_id": tenant_id, "body": body, "category": category,
            "submitted_by": submitted_by, "industry": industry, "kind": kind,
        }).execute()
        reference = None
    return reference


def get_ticket_by_reference(reference: str, email: str) -> dict | None:
    """One ticket by reference + follow-up email — the status-check lookup.
    Service role (anonymous tickets have no tenant); the email must match what
    the ticket was filed with, so a reference alone can't read someone else's
    ticket. None = no such ticket OR wrong email (indistinguishable on purpose)."""
    ref = (reference or "").strip().upper()
    addr = (email or "").strip().lower()
    if not ref or not addr:
        return None
    res = (db().table("feature_requests")
           .select("reference,status,status_note,category,kind,severity,created_at,user_email,body")
           .eq("reference", ref).limit(1).execute())
    row = res.data[0] if res.data else None
    if not row or (row.get("user_email") or "").strip().lower() != addr:
        return None
    return row


def update_feature_request_status(request_id: str, status: str,
                                  status_note: str | None = None) -> None:
    """Operator console: set a request/ticket's status (+ optional note shown
    back to the user on a status check). Service role."""
    patch: dict = {"status": status}
    if status_note is not None:
        patch["status_note"] = status_note.strip() or None
    db().table("feature_requests").update(patch).eq("id", request_id).execute()


def get_feature_request(request_id: str) -> dict | None:
    """One request row by id. Service role (operator console / build hook)."""
    res = (db().table("feature_requests").select("*")
           .eq("id", request_id).limit(1).execute())
    return res.data[0] if res.data else None


def get_feature_request_by_issue(issue_number: int) -> dict | None:
    """The request a GitHub build issue belongs to. Service role."""
    res = (db().table("feature_requests").select("*")
           .eq("github_issue", int(issue_number)).limit(1).execute())
    return res.data[0] if res.data else None


def update_feature_request_build(request_id: str, patch: dict, event: dict | None = None) -> None:
    """Build-loop write: patch columns and append one storyline event. The event
    list is read-modify-write — one operator and serialised workflow runs, so a
    lost update isn't a realistic race. Service role."""
    patch = dict(patch)
    if event is not None:
        row = get_feature_request(request_id) or {}
        patch["build_events"] = (row.get("build_events") or []) + [event]
    db().table("feature_requests").update(patch).eq("id", request_id).execute()


def count_recent_tickets_by_email(email: str, hours: int = 1) -> int:
    """Anti-spam: tickets this follow-up address filed in the window. Service role."""
    addr = (email or "").strip().lower()
    if not addr:
        return 0
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    res = (db().table("feature_requests").select("id", count="exact")
           .ilike("user_email", addr).gte("created_at", cutoff).limit(1).execute())
    return res.count or 0


def find_recent_ticket_duplicate(email: str, body: str, minutes: int = 10) -> str | None:
    """Idempotency: an identical ticket from the same address inside the window
    returns its existing reference instead of filing again (double-click guard)."""
    addr = (email or "").strip().lower()
    if not addr or not body:
        return None
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    res = (db().table("feature_requests").select("reference,body")
           .ilike("user_email", addr).gte("created_at", cutoff)
           .order("created_at", desc=True).limit(5).execute())
    for row in res.data or []:
        if (row.get("body") or "").strip() == body.strip() and row.get("reference"):
            return row["reference"]
    return None


def redact_expired_tickets(days: int) -> int:
    """GDPR retention (daily sweep): scrub the personal/triage fields from
    resolved issue tickets older than the window. The body stays (it is the
    de-identified issue record the roadmap/bug history is built from); contact
    and device details go. created_at is the proxy for age — we don't stamp a
    resolved_at. Returns rows redacted."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    res = (db().table("feature_requests")
           .update({"user_email": None, "error_message": None, "page_url": None,
                    "user_agent": None,
                    "redacted_at": datetime.now(timezone.utc).isoformat()})
           .eq("kind", "bug").in_("status", ["resolved", "closed", "done"])
           .lt("created_at", cutoff).is_("redacted_at", "null").execute())
    return len(res.data or [])


def list_feature_requests(tenant_id: str, client: Client | None = None) -> list[dict]:
    res = ((client or db()).table("feature_requests").select("*")
           .eq("tenant_id", tenant_id).order("created_at", desc=True).execute())
    return res.data or []


def list_all_feature_requests() -> list[dict]:
    """Operator console — every tenant's requests (service role, no tenant scope)."""
    res = (db().table("feature_requests").select("*")
           .order("created_at", desc=True).execute())
    return res.data or []


def count_unseen_feature_requests() -> int:
    """Operator nav badge — requests the operator hasn't opened /admin/requests
    since (seen_at null). Service role."""
    res = (db().table("feature_requests").select("id", count="exact")
           .is_("seen_at", "null").limit(1).execute())
    return res.count or 0


def mark_feature_requests_seen() -> None:
    """Stamp every unseen request seen — called when the operator opens the
    Requests page, so the badge clears."""
    from datetime import datetime, timezone
    (db().table("feature_requests")
     .update({"seen_at": datetime.now(timezone.utc).isoformat()})
     .is_("seen_at", "null").execute())


# ── Google sign-in invites (operator console; src/app/slots.py gate) ─────────────
def list_google_invites() -> list[dict]:
    """All invites, newest first. Service role (operator console only)."""
    res = (db().table("google_invites")
           .select("id,code,email,note,created_at,used_at,revoked_at")
           .order("created_at", desc=True).execute())
    return res.data or []


def create_google_invite(code: str, email: str | None, note: str | None) -> dict:
    """Store a new single-use invite (codes kept uppercase). Service role."""
    res = (db().table("google_invites")
           .insert({"code": code.strip().upper(), "email": (email or "").strip() or None,
                    "note": (note or "").strip() or None}).execute())
    return res.data[0]


def revoke_google_invite(invite_id: str) -> None:
    """Kill an unused invite. Idempotent; already-used invites keep their used_at."""
    from datetime import datetime, timezone
    (db().table("google_invites")
     .update({"revoked_at": datetime.now(timezone.utc).isoformat()})
     .eq("id", invite_id).is_("revoked_at", "null").execute())


def use_google_invite(code: str) -> bool:
    """Atomically redeem a single-use invite: stamps used_at iff the code exists,
    is unused and unrevoked. The filtered UPDATE makes double-redeems race-safe —
    only one caller gets rows back. Returns whether the code was accepted."""
    from datetime import datetime, timezone
    res = (db().table("google_invites")
           .update({"used_at": datetime.now(timezone.utc).isoformat()})
           .eq("code", code.strip().upper())
           .is_("used_at", "null").is_("revoked_at", "null").execute())
    return bool(res.data)


def google_invite_email_available(email: str) -> bool:
    """Whether an unused, unrevoked Gmail invite is registered to this email."""
    normalized = (email or "").strip().casefold()
    if not normalized:
        return False
    rows = (db().table("google_invites").select("id,email")
            .is_("used_at", "null").is_("revoked_at", "null").execute().data or [])
    return any((row.get("email") or "").strip().casefold() == normalized for row in rows)


def use_google_invite_for_email(email: str) -> bool:
    """Atomically consume one active Gmail invite registered to this email.

    The email is the lookup key, so the operator's existing invite can be used
    without exposing or re-minting its code. Consumption happens only after the
    Google callback confirms the selected account matches this address.
    """
    normalized = (email or "").strip().casefold()
    if not normalized:
        return False
    rows = (db().table("google_invites").select("id,email,created_at")
            .is_("used_at", "null").is_("revoked_at", "null")
            .order("created_at").execute().data or [])
    invite = next((row for row in rows
                   if (row.get("email") or "").strip().casefold() == normalized), None)
    if not invite:
        return False
    from datetime import datetime, timezone
    result = (db().table("google_invites")
              .update({"used_at": datetime.now(timezone.utc).isoformat()})
              .eq("id", invite["id"]).is_("used_at", "null")
              .is_("revoked_at", "null").execute())
    return bool(result.data)


# ── Google access requests (the gate's "request access" vetting queue) ───────
def create_google_access_request(email: str, message: str | None) -> dict:
    res = (db().table("google_access_requests")
           .insert({"email": email.strip(), "message": (message or "").strip() or None})
           .execute())
    return res.data[0]


def list_google_access_requests() -> list[dict]:
    """All requests, newest first — the operator's vetting queue."""
    res = (db().table("google_access_requests")
           .select("id,email,message,created_at,invited_at,dismissed_at")
           .order("created_at", desc=True).execute())
    return res.data or []


def get_google_access_request(request_id: str) -> dict | None:
    res = (db().table("google_access_requests").select("*")
           .eq("id", request_id).limit(1).execute())
    return res.data[0] if res.data else None


def mark_access_request_invited(request_id: str) -> None:
    from datetime import datetime, timezone
    (db().table("google_access_requests")
     .update({"invited_at": datetime.now(timezone.utc).isoformat()})
     .eq("id", request_id).execute())


def dismiss_access_request(request_id: str) -> None:
    from datetime import datetime, timezone
    (db().table("google_access_requests")
     .update({"dismissed_at": datetime.now(timezone.utc).isoformat()})
     .eq("id", request_id).is_("dismissed_at", "null").execute())


def count_pending_access_requests() -> int:
    """Nav-badge count — requests not yet invited or dismissed."""
    res = (db().table("google_access_requests").select("id", count="exact")
           .is_("invited_at", "null").is_("dismissed_at", "null").limit(1).execute())
    return res.count or 0


# ── OS founding-partner access (the OS-tier "request access" vetting queue) ──
# "Core + your OS" is request-only while the founding cohort is hand-picked.
# The tenant requests from /billing/plans; approval flips tenants.os_access_approved,
# which billing_routes.start_checkout enforces for the OS-tier price ids.

def create_os_access_request(tenant_id: str, email: str | None, message: str | None) -> dict:
    res = (db().table("os_access_requests")
           .insert({"tenant_id": tenant_id,
                    "email": (email or "").strip() or None,
                    "message": (message or "").strip() or None})
           .execute())
    return res.data[0]


def tenant_pending_os_request(tenant_id: str) -> dict | None:
    """The tenant's open request, if any — one pending request per tenant."""
    res = (db().table("os_access_requests").select("*")
           .eq("tenant_id", tenant_id)
           .is_("approved_at", "null").is_("dismissed_at", "null")
           .limit(1).execute())
    return res.data[0] if res.data else None


def list_os_access_requests() -> list[dict]:
    """All requests, newest first — the operator's founding-partner queue."""
    res = (db().table("os_access_requests")
           .select("id,tenant_id,email,message,created_at,approved_at,dismissed_at")
           .order("created_at", desc=True).execute())
    return res.data or []


def get_os_access_request(request_id: str) -> dict | None:
    res = (db().table("os_access_requests").select("*")
           .eq("id", request_id).limit(1).execute())
    return res.data[0] if res.data else None


def set_os_access_approved(tenant_id: str, approved: bool) -> None:
    """The actual lock: billing_routes.start_checkout consults this tenant flag
    before minting an OS-tier Checkout session. SERVICE ROLE — flipped by the
    operator console only."""
    db().table("tenants").update({"os_access_approved": approved}).eq("id", tenant_id).execute()


def approve_os_access_request(request_id: str) -> dict | None:
    """Vetted → stamp the request and unlock the tenant's OS-tier checkout."""
    from datetime import datetime, timezone
    req = get_os_access_request(request_id)
    if not req or req.get("dismissed_at"):
        return None
    (db().table("os_access_requests")
     .update({"approved_at": datetime.now(timezone.utc).isoformat()})
     .eq("id", request_id).execute())
    set_os_access_approved(req["tenant_id"], True)
    return req


def dismiss_os_access_request(request_id: str) -> None:
    from datetime import datetime, timezone
    (db().table("os_access_requests")
     .update({"dismissed_at": datetime.now(timezone.utc).isoformat()})
     .eq("id", request_id).is_("dismissed_at", "null").execute())


def count_pending_os_requests() -> int:
    """Nav-badge count — OS access requests awaiting a decision."""
    res = (db().table("os_access_requests").select("id", count="exact")
           .is_("approved_at", "null").is_("dismissed_at", "null").limit(1).execute())
    return res.count or 0


def get_connection_by_address(address: str) -> dict | None:
    """A connected mailbox by its address — the system-notification sender
    (PLATFORM_NOTIFY_MAILBOX) resolves through this."""
    res = (db().table("email_connections").select("*")
           .ilike("address", address.strip()).limit(1).execute())
    return _decrypt_conn(res.data[0]) if res.data else None


# ── CRM (docs/architecture/CRM_design.md — src/app/crm.py owns the ingest logic) ──────────────
# Writes run on the SERVICE ROLE (they're pipeline/backfill side effects, no user
# in the loop); reads take the per-request `client` like every dashboard query.

def crm_email_log_rows(tenant_id: str, limit: int = 5000) -> list[dict]:
    """The email_log slice the CRM backfill derives from — oldest first, so a
    contact's last_activity_at settles on their newest email."""
    res = (
        db().table("email_log")
        .select("id,from_address,from_name,contact_name,contact_company,subject,"
                "status,request_summary,action_summary,created_at")
        .eq("tenant_id", tenant_id).order("created_at", desc=False).limit(limit).execute()
    )
    return res.data or []

def crm_get_or_create_company(tenant_id: str, domain: str, name: str | None = None) -> dict:
    """The tenant's company row for an email domain — created on first sight.
    Select-then-insert with a re-select on the unique-index race (the same shape
    as insert_email_log's dedupe): two concurrent ingests of the same new domain
    settle on one row."""
    domain = domain.strip().lower()
    res = (db().table("crm_companies").select("*")
           .eq("tenant_id", tenant_id).eq("domain", domain).limit(1).execute())
    if res.data:
        return res.data[0]
    try:
        ins = (db().table("crm_companies")
               .insert({"tenant_id": tenant_id, "domain": domain, "name": name})
               .execute())
        return ins.data[0]
    except Exception:
        res = (db().table("crm_companies").select("*")
               .eq("tenant_id", tenant_id).eq("domain", domain).limit(1).execute())
        if res.data:
            return res.data[0]
        raise


def crm_create_company_by_name(tenant_id: str, name: str) -> dict:
    """Phase 3.5: create a company from a stated name (no domain). Used for
    freemail senders where the company was LLM-extracted from the email body."""
    ins = (db().table("crm_companies")
           .insert({"tenant_id": tenant_id, "name": name, "domain": None})
           .execute())
    return ins.data[0]


def crm_get_or_create_contact(tenant_id: str, email: str, name: str | None = None,
                              company_id: str | None = None) -> dict:
    """The tenant's contact row for an email address — created on first sight
    (unique on tenant_id+email, same race-safe shape as the company upsert)."""
    email = email.strip().lower()
    res = (db().table("crm_contacts").select("*")
           .eq("tenant_id", tenant_id).eq("email", email).limit(1).execute())
    if res.data:
        return res.data[0]
    try:
        ins = (db().table("crm_contacts")
               .insert({"tenant_id": tenant_id, "email": email, "name": name,
                        "company_id": company_id})
               .execute())
        return ins.data[0]
    except Exception:
        res = (db().table("crm_contacts").select("*")
               .eq("tenant_id", tenant_id).eq("email", email).limit(1).execute())
        if res.data:
            return res.data[0]
        raise


def crm_contact_by_reference(tenant_id: str, code: str,
                             client: Client | None = None) -> dict | None:
    """The CRM contact behind a customer reference code (portal lookup).
    Tenant-scoped: a code from another business can never resolve here —
    that's the whole cross-business isolation story for the portal."""
    code = (code or "").strip().upper()
    if not code:
        return None
    res = (
        (client or db()).table("crm_contacts")
        .select("*")
        .eq("tenant_id", tenant_id).eq("reference_code", code)
        .limit(1).execute()
    )
    return res.data[0] if res.data else None


def set_contact_reference(contact_id: str, tenant_id: str, code: str) -> bool:
    """Stamp a freshly minted reference code on a contact. Returns False on a
    collision (unique per tenant) — the caller mints another and retries.
    Service role: minting happens on unauthenticated portal requests and inside
    the send pipeline alike."""
    try:
        (db().table("crm_contacts").update({"reference_code": code})
         .eq("id", contact_id).eq("tenant_id", tenant_id).execute())
        return True
    except Exception:
        return False


def crm_contact_by_phone(tenant_id: str, phone: str,
                         client: Client | None = None) -> dict | None:
    """The CRM contact for a phone number — the SMS channel's context lookup
    (docs/modules/module-sms-channel.md §1: the phone IS the identity for SMS-only
    customers). Same shape as crm_contact_by_email."""
    res = (
        (client or db()).table("crm_contacts")
        .select("*, crm_companies(id,name,domain,custom_data)")
        .eq("tenant_id", tenant_id).eq("phone", (phone or "").strip())
        .limit(1).execute()
    )
    return res.data[0] if res.data else None


def crm_get_or_create_contact_by_phone(tenant_id: str, phone: str,
                                       name: str | None = None) -> dict:
    """The tenant's contact row for a phone number — created on first sight
    (unique on tenant_id+phone, same race-safe shape as the email upsert). An
    SMS-only contact starts with no email; extraction can fill it later, at
    which point the row simply carries both identities."""
    phone = phone.strip()
    res = (db().table("crm_contacts").select("*")
           .eq("tenant_id", tenant_id).eq("phone", phone).limit(1).execute())
    if res.data:
        return res.data[0]
    try:
        ins = (db().table("crm_contacts")
               .insert({"tenant_id": tenant_id, "phone": phone, "name": name})
               .execute())
        return ins.data[0]
    except Exception:
        res = (db().table("crm_contacts").select("*")
               .eq("tenant_id", tenant_id).eq("phone", phone).limit(1).execute())
        if res.data:
            return res.data[0]
        raise


def crm_update_company(company_id: str, tenant_id: str, fields: dict) -> None:
    fields = {**fields, "updated_at": datetime.now(timezone.utc).isoformat()}
    (db().table("crm_companies").update(fields)
     .eq("id", company_id).eq("tenant_id", tenant_id).execute())


def crm_update_contact(contact_id: str, tenant_id: str, fields: dict) -> None:
    fields = {**fields, "updated_at": datetime.now(timezone.utc).isoformat()}
    (db().table("crm_contacts").update(fields)
     .eq("id", contact_id).eq("tenant_id", tenant_id).execute())
    record_versions.bump(tenant_id, contact_id)   # the portal's cached record re-reads


def crm_insert_activity(tenant_id: str, *, contact_id: str | None, company_id: str | None,
                        activity_type: str, occurred_at: str, source_id: str | None = None,
                        direction: str | None = None, subject: str | None = None,
                        summary: str | None = None, deal_id: str | None = None) -> bool:
    """Append one activity to the feed. Returns False when the row already exists
    (crm_activities_source_uniq) — a re-run backfill or duplicate webhook delivery
    lands here, never a doubled feed."""
    try:
        (db().table("crm_activities").insert({
            "tenant_id": tenant_id, "contact_id": contact_id, "company_id": company_id,
            "deal_id": deal_id, "activity_type": activity_type, "source_id": source_id,
            "direction": direction, "subject": subject, "summary": summary,
            "occurred_at": occurred_at,
        }).execute())
        return True
    except Exception:
        if source_id is None:
            raise
        res = (db().table("crm_activities").select("id")
               .eq("tenant_id", tenant_id).eq("activity_type", activity_type)
               .eq("source_id", source_id).limit(1).execute())
        if res.data:
            return False
        raise


def crm_stamp_activity_sentiment(tenant_id: str, email_log_id: str, mood: str) -> None:
    """Stamp the mood onto the email's inbound activity row — the contact page's
    timeline shows the emotional arc, not just the rolling state. The activity is
    inserted by crm.ingest_email_log_row before intel runs, so the row exists."""
    (db().table("crm_activities").update({"sentiment": mood})
     .eq("tenant_id", tenant_id).eq("source_id", email_log_id)
     .eq("direction", "inbound").execute())


def crm_contact_temperatures(tenant_id: str, client: Client | None = None) -> dict:
    """address/phone → sentiment state, for chipping the Inbox list. One bounded
    query: only warm/hot contacts come back (calm is the default and gets no
    chip), so the map stays tiny for any sane book."""
    res = (
        (client or db()).table("crm_contacts")
        .select("email,phone,sentiment,sentiment_trend,sentiment_reason,sentiment_thread_id")
        .eq("tenant_id", tenant_id)
        .in_("sentiment", ["concerned", "frustrated", "angry"])
        .limit(500).execute()
    )
    temps: dict[str, dict] = {}
    for c in res.data or []:
        state = {"mood": c["sentiment"], "trend": c.get("sentiment_trend"),
                 "reason": c.get("sentiment_reason"),
                 # The conversation the read came from — the Inbox only tints
                 # that one. None (a read older than the column) tints nothing.
                 "thread_id": c.get("sentiment_thread_id")}
        if c.get("email"):
            temps[c["email"]] = state
        if c.get("phone"):    # SMS threads key on the number
            temps[c["phone"]] = state
    return temps


def crm_list_contacts(tenant_id: str, q: str | None = None, limit: int = 100,
                      offset: int = 0, client: Client | None = None) -> list[dict]:
    """Contacts newest-activity first, each with its company embedded. `q`
    filters on name OR email (the list page's search box)."""
    query = (
        (client or db()).table("crm_contacts")
        .select("*, crm_companies(id,name,domain)")
        .eq("tenant_id", tenant_id)
        .order("last_activity_at", desc=True, nullsfirst=False)
        .range(offset, offset + limit - 1)
    )
    if q:
        query = query.or_(f"email.ilike.%{q}%,name.ilike.%{q}%")
    return query.execute().data or []


def crm_contacts_by_known_name(tenant_id: str, q: str, limit: int = 10,
                               client: Client | None = None) -> list[dict]:
    """Resolve historical sender names to existing contacts, within one tenant.

    No body search, inferred surname or change to the contact's chosen name.
    """
    q = "".join(ch for ch in (q or "") if ch not in "%,()*\\").strip()
    if len(q) < 2:
        return []
    c = client or db()
    matches = (c.table("email_log").select("from_address")
               .eq("tenant_id", tenant_id)
               .or_(f"from_name.ilike.%{q}%,contact_name.ilike.%{q}%")
               .order("created_at", desc=True).limit(100).execute().data or [])
    addresses = sorted({str(r.get("from_address") or "").strip().lower() for r in matches} - {""})
    if not addresses:
        return []
    return (c.table("crm_contacts").select("*, crm_companies(id,name,domain)")
            .eq("tenant_id", tenant_id).in_("email", addresses)
            .order("last_activity_at", desc=True, nullsfirst=False)
            .limit(max(1, min(limit, 100))).execute().data or [])


def crm_contacts_by_dependant_name(tenant_id: str, q: str, limit: int = 10,
                                   client: Client | None = None) -> list[dict]:
    """Owners whose live dependant's name matches `q` (a subject or asset),
    each row a contact with `matched_dependant` {kind, name}. The concierge's
    search by name found only people, so "when does Mabel's certificate run out?"
    came back "no contact called Mabel" (live 2026-09-23)."""
    q = "".join(ch for ch in (q or "") if ch not in "%,()*\\").strip()
    if len(q) < 2:
        return []
    c = client or db()
    inst = (c.table("requirement_instances")
            .select("contact_id,name,requirement_groups(label)")
            .eq("tenant_id", tenant_id).is_("archived_at", "null")
            .ilike("name", f"%{q}%").limit(limit).execute().data or [])
    by_contact: dict = {}
    for r in inst:
        if r.get("contact_id") and r["contact_id"] not in by_contact:
            by_contact[r["contact_id"]] = {
                "kind": (r.get("requirement_groups") or {}).get("label"),
                "name": r.get("name")}
    if not by_contact:
        return []
    rows = (c.table("crm_contacts").select("*, crm_companies(id,name,domain)")
            .eq("tenant_id", tenant_id).in_("id", list(by_contact))
            .execute().data or [])
    for r in rows:
        r["matched_dependant"] = by_contact.get(r.get("id"))
    return rows


def crm_get_contact(contact_id: str, tenant_id: str, client: Client | None = None) -> dict | None:
    """One contact with its company, scoped to the owning tenant (IDOR-safe)."""
    res = (
        (client or db()).table("crm_contacts")
        .select("*, crm_companies(id,name,domain)")
        .eq("id", contact_id).eq("tenant_id", tenant_id).limit(1).execute()
    )
    return res.data[0] if res.data else None


def crm_activities_for_contact(contact_id: str, tenant_id: str, limit: int = 20,
                               client: Client | None = None) -> list[dict]:
    res = (
        (client or db()).table("crm_activities").select("*")
        .eq("tenant_id", tenant_id).eq("contact_id", contact_id)
        .order("occurred_at", desc=True).limit(limit).execute()
    )
    return res.data or []


def crm_list_companies(tenant_id: str, limit: int = 100, offset: int = 0,
                       client: Client | None = None) -> list[dict]:
    """Companies newest-activity first, each with its contact count embedded."""
    res = (
        (client or db()).table("crm_companies")
        .select("*, crm_contacts(count)")
        .eq("tenant_id", tenant_id)
        .order("last_activity_at", desc=True, nullsfirst=False)
        .range(offset, offset + limit - 1).execute()
    )
    return res.data or []


def crm_get_company(company_id: str, tenant_id: str, client: Client | None = None) -> dict | None:
    res = (
        (client or db()).table("crm_companies").select("*")
        .eq("id", company_id).eq("tenant_id", tenant_id).limit(1).execute()
    )
    return res.data[0] if res.data else None


def crm_contacts_for_company(company_id: str, tenant_id: str,
                             client: Client | None = None) -> list[dict]:
    res = (
        (client or db()).table("crm_contacts").select("*")
        .eq("tenant_id", tenant_id).eq("company_id", company_id)
        .order("last_activity_at", desc=True, nullsfirst=False).execute()
    )
    return res.data or []


def crm_list_deals(tenant_id: str, contact_id: str | None = None,
                   company_id: str | None = None, client: Client | None = None) -> list[dict]:
    """Deals (open first, newest first) with stage + company + primary contact
    embedded — one query serves the list page, the pipeline grouping and the
    contact/company detail panels."""
    q = (
        (client or db()).table("crm_deals")
        .select("*, crm_stages(id,name,stage_type), crm_companies(id,name,domain), "
                "crm_contacts(id,name,email)")
        .eq("tenant_id", tenant_id)
        .order("status", desc=False).order("opened_at", desc=True)
    )
    if contact_id:
        q = q.eq("primary_contact_id", contact_id)
    if company_id:
        q = q.eq("company_id", company_id)
    return q.execute().data or []


def crm_get_deal(deal_id: str, tenant_id: str, client: Client | None = None) -> dict | None:
    res = (
        (client or db()).table("crm_deals")
        .select("*, crm_stages(id,name,stage_type), crm_companies(id,name,domain), "
                "crm_contacts(id,name,email)")
        .eq("id", deal_id).eq("tenant_id", tenant_id).limit(1).execute()
    )
    return res.data[0] if res.data else None


def crm_activities_for_deal(deal_id: str, tenant_id: str, limit: int = 50,
                            client: Client | None = None) -> list[dict]:
    res = (
        (client or db()).table("crm_activities").select("*")
        .eq("tenant_id", tenant_id).eq("deal_id", deal_id)
        .order("occurred_at", desc=True).limit(limit).execute()
    )
    return res.data or []


def crm_list_stages(tenant_id: str, client: Client | None = None) -> list[dict]:
    res = (
        (client or db()).table("crm_stages").select("*")
        .eq("tenant_id", tenant_id).order("display_order").order("created_at").execute()
    )
    return res.data or []


def crm_insert_stage(tenant_id: str, name: str, display_order: int = 0,
                     stage_type: str = "active", client: Client | None = None) -> dict:
    res = (
        (client or db()).table("crm_stages")
        .insert({"tenant_id": tenant_id, "name": name,
                 "display_order": display_order, "stage_type": stage_type}).execute()
    )
    return res.data[0]


def crm_update_stage(stage_id: str, tenant_id: str, fields: dict,
                     client: Client | None = None) -> None:
    ((client or db()).table("crm_stages").update(fields)
     .eq("id", stage_id).eq("tenant_id", tenant_id).execute())


def crm_delete_stage(stage_id: str, tenant_id: str,
                     client: Client | None = None) -> None:
    """Deals on the stage keep their status; stage_id goes null (FK set null),
    so they surface in the pipeline's Unstaged bucket rather than vanishing."""
    ((client or db()).table("crm_stages").delete()
     .eq("id", stage_id).eq("tenant_id", tenant_id).execute())


# ── CRM custom fields (phase 2 — src/app/crm_fields.py owns validation + gates) ──

_CRM_ENTITY_TABLES = {"company": "crm_companies", "contact": "crm_contacts",
                      "deal": "crm_deals"}


def crm_list_field_definitions(tenant_id: str, entity: str | None = None,
                               client: Client | None = None) -> list[dict]:
    q = (
        (client or db()).table("crm_field_definitions").select("*")
        .eq("tenant_id", tenant_id).order("display_order").order("created_at")
    )
    if entity:
        q = q.eq("entity", entity)
    return q.execute().data or []


def crm_insert_field_definition(tenant_id: str, definition: dict,
                                client: Client | None = None) -> dict:
    res = (
        (client or db()).table("crm_field_definitions")
        .insert({"tenant_id": tenant_id, **definition}).execute()
    )
    return res.data[0]


def crm_update_field_definition(def_id: str, tenant_id: str, fields: dict,
                                client: Client | None = None) -> None:
    ((client or db()).table("crm_field_definitions").update(fields)
     .eq("id", def_id).eq("tenant_id", tenant_id).execute())


def crm_delete_field_definition(def_id: str, tenant_id: str,
                                client: Client | None = None) -> None:
    ((client or db()).table("crm_field_definitions").delete()
     .eq("id", def_id).eq("tenant_id", tenant_id).execute())


def crm_get_record(entity: str, record_id: str, tenant_id: str,
                   client: Client | None = None) -> dict | None:
    """One CRM entity row (company/contact/deal) by kind — the custom-data
    merge and the suggestion accept path resolve records through this."""
    res = (
        (client or db()).table(_CRM_ENTITY_TABLES[entity]).select("*")
        .eq("id", record_id).eq("tenant_id", tenant_id).limit(1).execute()
    )
    return res.data[0] if res.data else None


def crm_merge_custom_data(entity: str, record_id: str, tenant_id: str,
                          values: dict, client: Client | None = None,
                          overwrite: bool = True) -> dict:
    """Fold extracted/confirmed values into the record's custom_data JSONB
    (read-merge-write; last write wins per key — fine for a human's confirmed
    edit, which is what overwrite=True is for).

    With `overwrite=False`, a key whose stored value is present and DIFFERENT is
    left alone and returned as {key: stored_value}; everything else writes as
    normal (an empty slot, or the same value again). Live 2026-09-04: a
    property owner's SECOND property replaced the customer's own appliance make on the
    record, because extraction merges whatever it read. The value was true — it
    was just true about a different thing, so no confidence gate could catch it.
    Returns the conflicts so the caller can offer the change instead of making
    it."""
    row = crm_get_record(entity, record_id, tenant_id, client=client)
    if row is None:
        return {}
    current = row.get("custom_data") or {}
    conflicts: dict = {}
    if not overwrite:
        writable = {}
        for k, v in (values or {}).items():
            stored = current.get(k)
            if stored is None or not str(stored).strip() \
                    or str(stored).strip() == str(v).strip():
                writable[k] = v
            else:
                conflicts[k] = stored
        values = writable
    if not values:
        return conflicts
    merged = {**current, **values}
    ((client or db()).table(_CRM_ENTITY_TABLES[entity])
     .update({"custom_data": merged,
              "updated_at": datetime.now(timezone.utc).isoformat()})
     .eq("id", record_id).eq("tenant_id", tenant_id).execute())
    # A level moved on the CRM mid-session never reached the portal's cached
    # record (run #8, 2026-09-05): the version tells that cache to rebuild.
    record_versions.bump(tenant_id, record_id)
    return conflicts


def crm_insert_field_suggestion(tenant_id: str, *, entity: str, record_id: str,
                                field_key: str, value, confidence,
                                email_log_id: str | None) -> bool:
    """Queue a middling-confidence extraction for one-click confirm. Returns
    False when this (record, field, email) suggestion already exists
    (crm_field_suggestions_source_uniq) — a re-processed email never stacks."""
    try:
        (db().table("crm_field_suggestions").insert({
            "tenant_id": tenant_id, "entity": entity, "record_id": record_id,
            "field_key": field_key, "value": None if value is None else str(value),
            "confidence": confidence, "email_log_id": email_log_id,
        }).execute())
        return True
    except Exception:
        if email_log_id is None:
            raise
        res = (db().table("crm_field_suggestions").select("id")
               .eq("tenant_id", tenant_id).eq("entity", entity)
               .eq("record_id", record_id).eq("field_key", field_key)
               .eq("email_log_id", email_log_id).limit(1).execute())
        if res.data:
            return False
        raise


def email_log_ids_for_thread(tenant_id: str, thread_id: str | None,
                             client: Client | None = None) -> list[str]:
    """Every email_log row on one thread — what a booking's thread said, for
    the job page's "On the day" notes. Empty on no thread or a read error."""
    if not thread_id:
        return []
    try:
        return [r["id"] for r in ((client or db()).table("email_log").select("id")
                                  .eq("tenant_id", tenant_id).eq("thread_id", thread_id)
                                  .limit(200).execute().data or [])]
    except Exception:
        log.warning("thread rows read failed (tenant=%s)", tenant_id, exc_info=True)
        return []


def pending_chips_for_email(tenant_id: str, email_log_id: str,
                            client: Client | None = None) -> list[dict]:
    """The dependant chips one email's read queued that nobody has settled yet —
    what a document-held reply is still waiting on (pipeline, 2026-09-22)."""
    try:
        return ((client or db()).table("crm_field_suggestions").select("*")
                .eq("tenant_id", tenant_id).eq("status", "pending")
                .eq("entity", "dependant").eq("email_log_id", email_log_id)
                .execute().data or [])
    except Exception:
        log.warning("pending chips read failed (tenant=%s)", tenant_id, exc_info=True)
        return []


def claim_email_log_summary(log_id: str, tenant_id: str, expect: str, to: str,
                            client: Client | None = None) -> bool:
    """Move a row's action_summary from `expect` to `to` only if it still reads
    `expect` — so two confirms landing together release a held reply ONCE.
    True when this caller made the move."""
    res = ((client or db()).table("email_log").update({"action_summary": to})
           .eq("id", log_id).eq("tenant_id", tenant_id).eq("action_summary", expect).execute())
    return bool(res.data)


def crm_pending_suggestions(tenant_id: str, record_ids: list[str] | None = None,
                            client: Client | None = None) -> list[dict]:
    q = (
        (client or db()).table("crm_field_suggestions").select("*")
        .eq("tenant_id", tenant_id).eq("status", "pending")
        .order("created_at", desc=True)
    )
    if record_ids is not None:
        if not record_ids:
            return []
        q = q.in_("record_id", record_ids)
    return q.execute().data or []


def crm_get_suggestion(suggestion_id: str, tenant_id: str,
                       client: Client | None = None) -> dict | None:
    res = (
        (client or db()).table("crm_field_suggestions").select("*")
        .eq("id", suggestion_id).eq("tenant_id", tenant_id).limit(1).execute()
    )
    return res.data[0] if res.data else None


def crm_resolve_suggestion(suggestion_id: str, tenant_id: str, status: str,
                           client: Client | None = None) -> None:
    ((client or db()).table("crm_field_suggestions")
     .update({"status": status,
              "resolved_at": datetime.now(timezone.utc).isoformat()})
     .eq("id", suggestion_id).eq("tenant_id", tenant_id).execute())


def crm_recent_inbound(tenant_id: str, limit: int = 5,
                       client: Client | None = None) -> list[dict]:
    """The tenant's last N inbound emails WITH bodies — the fields wizard's
    live-preview feed ('here's what BLAM8 would fill in')."""
    res = (
        (client or db()).table("email_log")
        .select("id,from_address,from_name,subject,body_in,created_at")
        .eq("tenant_id", tenant_id).order("created_at", desc=True)
        .limit(limit).execute()
    )
    return res.data or []


# ── CRM intelligence (phase 3 — src/app/crm_intel.py owns the reasoning) ─────────

def crm_create_deal(tenant_id: str, fields: dict) -> dict:
    res = db().table("crm_deals").insert({"tenant_id": tenant_id, **fields}).execute()
    return res.data[0]


def crm_update_deal(deal_id: str, tenant_id: str, fields: dict,
                    client: Client | None = None) -> None:
    fields = {**fields, "updated_at": datetime.now(timezone.utc).isoformat()}
    ((client or db()).table("crm_deals").update(fields)
     .eq("id", deal_id).eq("tenant_id", tenant_id).execute())


def crm_set_activity_deal(tenant_id: str, source_id: str, deal_id: str) -> None:
    """Stamp the email's activity rows with the deal clustering decided on —
    the deal's feed then shows the thread without a second write path."""
    (db().table("crm_activities").update({"deal_id": deal_id})
     .eq("tenant_id", tenant_id).eq("source_id", source_id).execute())


def crm_insert_promise(tenant_id: str, *, contact_id: str | None, company_id: str | None,
                       deal_id: str | None, direction: str, description: str,
                       due_hint: str | None, email_log_id: str | None) -> bool:
    """Append one promise. False when this (email, direction, description)
    already exists (crm_promises_source_uniq) — re-processing never stacks."""
    try:
        (db().table("crm_promises").insert({
            "tenant_id": tenant_id, "contact_id": contact_id, "company_id": company_id,
            "deal_id": deal_id, "direction": direction, "description": description,
            "due_hint": due_hint, "email_log_id": email_log_id,
        }).execute())
        return True
    except Exception:
        if email_log_id is None:
            raise
        res = (db().table("crm_promises").select("id")
               .eq("tenant_id", tenant_id).eq("email_log_id", email_log_id)
               .eq("direction", direction).eq("description", description)
               .limit(1).execute())
        if res.data:
            return False
        raise


def crm_open_promises(tenant_id: str, contact_id: str | None = None,
                      client: Client | None = None) -> list[dict]:
    q = (
        (client or db()).table("crm_promises").select("*")
        .eq("tenant_id", tenant_id).eq("status", "open")
        .order("created_at", desc=True)
    )
    if contact_id:
        q = q.eq("contact_id", contact_id)
    return q.execute().data or []


def crm_get_promise(promise_id: str, tenant_id: str,
                    client: Client | None = None) -> dict | None:
    res = ((client or db()).table("crm_promises").select("*")
           .eq("id", promise_id).eq("tenant_id", tenant_id).limit(1).execute())
    return res.data[0] if res.data else None


CLEARED_STATUSES = ("resolved", "done", "dropped")


def crm_recent_cleared_promises(tenant_id: str, contact_id: str | None = None, *,
                                days: int = 30, limit: int = 8,
                                client: Client | None = None) -> list[dict]:
    """Promises cleared lately — resolved by an email or an action, marked done,
    or dropped — newest first. The green ticks under the open rows (Kris
    2026-09-20: a cleared promise must be SHOWN cleared, not vanish)."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    q = ((client or db()).table("crm_promises").select("*")
         .eq("tenant_id", tenant_id).in_("status", list(CLEARED_STATUSES))
         .gte("resolved_at", since)
         .order("resolved_at", desc=True).limit(limit))
    if contact_id:
        q = q.eq("contact_id", contact_id)
    return q.execute().data or []


def crm_resolve_promise(promise_id: str, tenant_id: str, status: str,
                        client: Client | None = None) -> None:
    """Done / Drop by hand. Stamped 'manual' so the card can say 'you marked
    it done' rather than pretend an email did it."""
    ((client or db()).table("crm_promises")
     .update({"status": status,
              "resolved_at": datetime.now(timezone.utc).isoformat(),
              "resolution_type": "manual",
              "resolved_by_kind": "manual", "resolved_by_id": None,
              "resolved_by_label": None})
     .eq("id", promise_id).eq("tenant_id", tenant_id).execute())


def crm_confirm_promise_resolved(promise_id: str, tenant_id: str,
                                 client: Client | None = None) -> None:
    """The owner agrees with a 'suggested' resolution: it becomes resolved and
    KEEPS the evidence + cleared-by link the suggestion carried."""
    ((client or db()).table("crm_promises")
     .update({"status": "resolved",
              "resolved_at": datetime.now(timezone.utc).isoformat(),
              "resolution_type": "confirmed"})
     .eq("id", promise_id).eq("tenant_id", tenant_id).execute())
    try:
        crm_complete_tasks_for_promise(promise_id, tenant_id)
    except Exception:
        pass


def crm_keep_promise_open(promise_id: str, tenant_id: str,
                          client: Client | None = None) -> None:
    """'Still open': the owner rejects a suggested resolution — the row goes
    back to a plain open promise with no suggestion on it."""
    ((client or db()).table("crm_promises")
     .update({"resolution_type": None, "resolution_evidence": None,
              "resolved_by_kind": None, "resolved_by_id": None,
              "resolved_by_label": None})
     .eq("id", promise_id).eq("tenant_id", tenant_id).eq("status", "open").execute())


def crm_reinforce_promise(promise_id: str, tenant_id: str,
                          description: str | None = None) -> None:
    """Phase 3.5 §18.3: a restated promise bumps mention_count + last_seen_at.
    If the LLM returned a more specific description, update it."""
    now = datetime.now(timezone.utc).isoformat()
    # Fetch current to conditionally update description
    res = (db().table("crm_promises").select("mention_count, description")
           .eq("id", promise_id).eq("tenant_id", tenant_id).limit(1).execute())
    if not res.data:
        return
    current = res.data[0]
    updates = {
        "last_seen_at": now,
        "mention_count": (current.get("mention_count") or 1) + 1,
    }
    # Update description only if new one is more specific (longer or adds a date)
    if description and len(description) > len(current.get("description") or ""):
        updates["description"] = description
    (db().table("crm_promises").update(updates)
     .eq("id", promise_id).eq("tenant_id", tenant_id).execute())


def crm_resolve_promise_auto(promise_id: str, tenant_id: str,
                             evidence: str | None = None, *,
                             by_kind: str | None = None, by_id: str | None = None,
                             by_label: str | None = None) -> None:
    """Phase 3.5 §18.4: auto-resolve a promise when fulfilment detected.
    Phase 4a: also auto-completes any tasks linked to this promise.
    by_kind/by_id/by_label (2026-09-20) name the thing that cleared it —
    the email, booking, invoice or quote — so the tick links to it."""
    now = datetime.now(timezone.utc).isoformat()
    (db().table("crm_promises")
     .update({"status": "resolved", "resolved_at": now,
              "resolution_type": "auto", "resolution_evidence": evidence or None,
              "resolved_by_kind": by_kind, "resolved_by_id": str(by_id) if by_id else None,
              "resolved_by_label": by_label})
     .eq("id", promise_id).eq("tenant_id", tenant_id).execute())
    # Auto-complete linked tasks
    try:
        crm_complete_tasks_for_promise(promise_id, tenant_id)
    except Exception:
        pass  # task completion is best-effort


def crm_suggest_promise_resolved(promise_id: str, tenant_id: str,
                                 evidence: str | None = None, *,
                                 by_kind: str | None = None, by_id: str | None = None,
                                 by_label: str | None = None) -> None:
    """Phase 3.5 §18.4: medium-confidence resolution → suggested, not auto.
    Carries the same cleared-by link so a Confirm keeps it."""
    (db().table("crm_promises")
     .update({"resolution_type": "suggested", "resolution_evidence": evidence or None,
              "resolved_by_kind": by_kind, "resolved_by_id": str(by_id) if by_id else None,
              "resolved_by_label": by_label})
     .eq("id", promise_id).eq("tenant_id", tenant_id).execute())


# ── Documents (src/app/documents.py) — rows in crm_artifacts ─────────────────────
_DOC_COLS = ("id,tenant_id,contact_id,contact_email,email_log_id,booking_id,instance_id,"
             "artifact_type,filename,content_type,url_or_path,size_bytes,label,created_at,requirement_keys")


def insert_document(tenant_id: str, *, filename: str, content_type: str, size_bytes: int,
                    storage_path: str, source: str, contact_email: str | None = None,
                    contact_id: str | None = None, email_log_id: str | None = None,
                    booking_id: str | None = None, instance_id: str | None = None,
                    label: str | None = None, client: Client | None = None) -> dict | None:
    row = {"tenant_id": tenant_id, "filename": filename, "content_type": content_type,
           "size_bytes": int(size_bytes or 0), "url_or_path": storage_path,
           # 'sent' = a file the business attached to a reply (2026-09-26) —
           # a second row on the same stored object, stamped with the reply's
           # round, and kept OUT of the documents lists below.
           "artifact_type": source if source in ("upload", "sent") else "attachment",
           "contact_email": contact_email, "contact_id": contact_id, "email_log_id": email_log_id,
           "booking_id": booking_id, "instance_id": instance_id, "label": label}
    try:
        res = (client or db()).table("crm_artifacts").insert(row).execute()
        return res.data[0] if res.data else None
    except Exception:
        log.warning("document insert failed (tenant=%s, %s)", tenant_id, filename, exc_info=True)
        return None


def list_documents(tenant_id: str, *, contact_email: str | None = None, contact_id: str | None = None,
                   booking_id: str | None = None, sent: bool = False,
                   client: Client | None = None, strict: bool = False) -> list[dict]:
    """The files on a record — by the customer's address (the way inbound mail
    files them), by contact id, or those pinned to one booking. `sent=True`
    lists instead the files the business attached to its replies."""
    try:
        q = (client or db()).table("crm_artifacts").select(_DOC_COLS).eq("tenant_id", tenant_id) \
            .in_("artifact_type", ["sent"] if sent else ["attachment", "upload"]) \
            .not_.is_("url_or_path", "null")
        if booking_id:
            q = q.eq("booking_id", booking_id)
        elif contact_email and contact_id:
            q = q.or_(f"contact_email.eq.{(contact_email or '').strip().lower()},contact_id.eq.{contact_id}")
        elif contact_email:
            q = q.eq("contact_email", (contact_email or "").strip().lower())
        elif contact_id:
            q = q.eq("contact_id", contact_id)
        else:
            return []
        return q.order("created_at", desc=True).limit(100).execute().data or []
    except Exception:
        log.warning("document list failed (tenant=%s)", tenant_id, exc_info=True)
        if strict:
            from app.booking_errors import VerificationUnavailable
            raise VerificationUnavailable("the required documents") from None
        return []


def get_document(doc_id: str, tenant_id: str, client: Client | None = None) -> dict | None:
    try:
        res = ((client or db()).table("crm_artifacts").select(_DOC_COLS)
               .eq("id", doc_id).eq("tenant_id", tenant_id).limit(1).execute())
        return res.data[0] if res.data else None
    except Exception:
        return None


def update_document(doc_id: str, tenant_id: str, patch: dict, client: Client | None = None) -> None:
    allowed = {k: v for k, v in patch.items() if k in ("label", "instance_id", "booking_id", "contact_id", "requirement_keys")}
    if not allowed:
        return
    (client or db()).table("crm_artifacts").update(allowed).eq("id", doc_id).eq("tenant_id", tenant_id).execute()


def delete_document(doc_id: str, tenant_id: str, client: Client | None = None) -> None:
    (client or db()).table("crm_artifacts").delete().eq("id", doc_id).eq("tenant_id", tenant_id).execute()

# "We'll confirm the booking" promises: the confirm-stem plus a booking noun.
# Deliberately narrow — "confirm your address" or "send the quote" never match.
# Tightened 2026-09-27 (live data): "confirm" as a loose stem also caught
# "...sync issue for the user's CONFIRMED bookings" and "once the details are
# CONFIRMED" - work that has nothing to do with confirming a booking. Now the
# verb or noun "confirm/confirmation" must lead, within a few words, to the
# booking noun ("confirm the appointment", "a formal confirmation of the
# Wednesday 10:00 booking"); "confirmed" never counts.
_PROMISE_CONFIRMS_BOOKING_RE = re.compile(
    r"\bconfirm(?:ation)?\b[^.;]{0,60}?\b(booking|appointment|reservation|slot)s?\b",
    re.IGNORECASE)


def is_booking_confirmation_promise(description: str | None) -> bool:
    """Does this promise's text read as 'we will confirm the booking'?"""
    return bool(_PROMISE_CONFIRMS_BOOKING_RE.search(description or ""))


def crm_close_promises_for_confirmed_booking(
        tenant_id: str, *, thread_id: str | None = None,
        email_log_id: str | None = None, booking_ref: str | None = None,
        client: Client | None = None) -> int:
    """A booking just moved to confirmed — the promise that said we'd confirm it
    is fulfilled, so it must not sit open forever (2026-07-26 e2e finding).

    Conservative on purpose: only OPEN we_owe promises sourced from the SAME
    email thread (their email_log_id is the confirming log or a sibling log on
    the booking's thread_id) whose text matches is_booking_confirmation_promise.
    A promise with no source email, from another conversation, or about anything
    other than confirming a booking is never touched. Resolution goes through
    crm_resolve_promise_auto so linked follow-through tasks complete too.
    Returns how many promises closed."""
    if not (thread_id or email_log_id):
        return 0   # manual booking with no email trail — nothing to attribute
    c = client or db()
    log_ids = {email_log_id} if email_log_id else set()
    if thread_id:
        res = (c.table("email_log").select("id")
               .eq("tenant_id", tenant_id).eq("thread_id", thread_id).execute())
        log_ids.update(r["id"] for r in (res.data or []))
    if not log_ids:
        return 0
    res = (c.table("crm_promises").select("id, description")
           .eq("tenant_id", tenant_id).eq("status", "open")
           .eq("direction", "we_owe")
           .in_("email_log_id", sorted(log_ids)).execute())
    evidence = f"Booking {booking_ref} confirmed" if booking_ref else "Booking confirmed"
    closed = 0
    for row in res.data or []:
        if is_booking_confirmation_promise(row.get("description")):
            crm_resolve_promise_auto(row["id"], tenant_id, evidence=evidence)
            closed += 1
    return closed


def crm_contact_by_email(tenant_id: str, email: str,
                         client: Client | None = None) -> dict | None:
    """The CRM contact for an address — the drafter's context lookup."""
    res = (
        (client or db()).table("crm_contacts")
        .select("*, crm_companies(id,name,domain,custom_data)")
        .eq("tenant_id", tenant_id).eq("email", (email or "").strip().lower())
        .limit(1).execute()
    )
    return res.data[0] if res.data else None


def crm_probe_log_for_contact(tenant_id: str, contact_id: str) -> list[dict]:
    """All probe attempts for a contact (Phase 3.5 §18.2)."""
    res = (db().table("crm_probe_log").select("*")
           .eq("tenant_id", tenant_id).eq("contact_id", contact_id)
           .execute())
    return res.data or []


def crm_insert_probe_log(tenant_id: str, *, contact_id: str,
                         field_definition_id: str, draft_id: str | None = None) -> None:
    """Record one probe attempt (Phase 3.5 §18.2)."""
    attempts = (db().table("crm_probe_log").select("id", count="exact")
                .eq("tenant_id", tenant_id).eq("contact_id", contact_id)
                .eq("field_definition_id", field_definition_id).execute())
    attempt_number = (attempts.count or 0) + 1
    db().table("crm_probe_log").insert({
        "tenant_id": tenant_id, "contact_id": contact_id,
        "field_definition_id": field_definition_id,
        "attempt_number": attempt_number, "draft_id": draft_id,
    }).execute()


def crm_export(tenant_id: str, client: Client | None = None) -> dict:
    """Everything the tenant's CRM holds, in one dict — the "yours to export"
    brand promise (design §14). Service-role never used here: the caller's
    JWT client keeps RLS as the second wall on a bulk read."""
    out = {}
    for table in ("crm_companies", "crm_contacts", "crm_stages", "crm_deals",
                  "crm_activities", "crm_artifacts", "crm_field_definitions",
                  "crm_promises"):
        res = ((client or db()).table(table).select("*")
               .eq("tenant_id", tenant_id).execute())
        out[table.removeprefix("crm_")] = res.data or []
    return out


# ── Phase 4a: Reports (§19.1) ───────────────────────────────────────────────

def crm_report_pipeline(tenant_id: str, client: Client | None = None) -> list[dict]:
    """Pipeline summary: deal count + total value per stage."""
    stages = ((client or db()).table("crm_stages").select("id, name, display_order, stage_type")
              .eq("tenant_id", tenant_id).order("display_order").execute()).data or []
    deals = ((client or db()).table("crm_deals").select("stage_id, value_estimate, opened_at, status")
             .eq("tenant_id", tenant_id).eq("status", "open").execute()).data or []
    now = datetime.now(timezone.utc)
    by_stage = {}
    for d in deals:
        sid = d.get("stage_id")
        if sid not in by_stage:
            by_stage[sid] = {"count": 0, "total_value": 0, "days_list": []}
        by_stage[sid]["count"] += 1
        by_stage[sid]["total_value"] += float(d.get("value_estimate") or 0)
        if d.get("opened_at"):
            try:
                opened = datetime.fromisoformat(str(d["opened_at"]).replace("Z", "+00:00"))
                if opened.tzinfo is None:
                    opened = opened.replace(tzinfo=timezone.utc)
                by_stage[sid]["days_list"].append((now - opened).days)
            except (ValueError, TypeError):
                pass
    out = []
    for s in stages:
        bucket = by_stage.get(s["id"], {"count": 0, "total_value": 0, "days_list": []})
        avg_days = (sum(bucket["days_list"]) / len(bucket["days_list"])
                    if bucket["days_list"] else 0)
        out.append({"stage_id": s["id"], "stage_name": s["name"],
                    "stage_type": s.get("stage_type", "active"),
                    "deal_count": bucket["count"],
                    "total_value": bucket["total_value"],
                    "avg_days_in_stage": round(avg_days, 1)})
    return out


def crm_report_activity(tenant_id: str, period: int = 30,
                        client: Client | None = None) -> list[dict]:
    """Activity heatmap: emails per day over trailing period."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=period)).isoformat()
    rows = ((client or db()).table("crm_activities")
            .select("occurred_at, direction")
            .eq("tenant_id", tenant_id).gte("occurred_at", cutoff)
            .order("occurred_at").execute()).data or []
    by_day: dict = {}
    for r in rows:
        day = str(r.get("occurred_at") or "")[:10]
        if not day:
            continue
        if day not in by_day:
            by_day[day] = {"day": day, "inbound": 0, "outbound": 0}
        direction = r.get("direction") or "inbound"
        by_day[day][direction] = by_day[day].get(direction, 0) + 1
    return sorted(by_day.values(), key=lambda x: x["day"])


def crm_report_won_lost(tenant_id: str, period: int = 90,
                        client: Client | None = None) -> dict:
    """Won/Lost analysis over a period."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=period)).isoformat()
    rows = ((client or db()).table("crm_deals")
            .select("status, value_estimate, opened_at, closed_at")
            .eq("tenant_id", tenant_id).in_("status", ["won", "lost"])
            .gte("closed_at", cutoff).execute()).data or []
    won = [r for r in rows if r.get("status") == "won"]
    lost = [r for r in rows if r.get("status") == "lost"]

    def avg_value(deals):
        vals = [float(d.get("value_estimate") or 0) for d in deals if d.get("value_estimate")]
        return round(sum(vals) / len(vals), 2) if vals else 0

    def avg_days_to_close(deals):
        days = []
        for d in deals:
            if d.get("opened_at") and d.get("closed_at"):
                try:
                    opened = datetime.fromisoformat(str(d["opened_at"]).replace("Z", "+00:00"))
                    closed = datetime.fromisoformat(str(d["closed_at"]).replace("Z", "+00:00"))
                    days.append((closed - opened).days)
                except (ValueError, TypeError):
                    pass
        return round(sum(days) / len(days), 1) if days else 0

    total = len(won) + len(lost)
    return {
        "period_days": period,
        "won_count": len(won), "lost_count": len(lost),
        "win_rate": round(len(won) / total, 2) if total else 0,
        "avg_won_value": avg_value(won),
        "avg_lost_value": avg_value(lost),
        "avg_days_to_close": avg_days_to_close(won + lost),
    }


# ── Phase 4a: Tasks (§19.2) ─────────────────────────────────────────────────

def crm_create_task(tenant_id: str, *, title: str, description: str | None = None,
                    due_date=None, priority: str = "normal",
                    contact_id: str | None = None, company_id: str | None = None,
                    deal_id: str | None = None, promise_id: str | None = None,
                    source: str = "manual") -> dict:
    """Create a CRM task (manual or auto-generated)."""
    row = {"tenant_id": tenant_id, "title": title, "description": description,
           "due_date": str(due_date) if due_date else None,
           "priority": priority, "source": source,
           "contact_id": contact_id, "company_id": company_id,
           "deal_id": deal_id, "promise_id": promise_id}
    res = db().table("crm_tasks").insert(row).execute()
    return res.data[0]


def crm_list_tasks(tenant_id: str, *, status: str = "open",
                   contact_id: str | None = None,
                   deal_id: str | None = None,
                   client: Client | None = None) -> list[dict]:
    """List tasks with optional filters."""
    q = (client or db()).table("crm_tasks").select("*").eq("tenant_id", tenant_id)
    if status != "all":
        q = q.eq("status", status)
    if contact_id:
        q = q.eq("contact_id", contact_id)
    if deal_id:
        q = q.eq("deal_id", deal_id)
    q = q.order("due_date", nullsfirst=False).order("created_at", desc=True)
    return q.execute().data or []


def crm_tasks_due_today(tenant_id: str, client: Client | None = None) -> list[dict]:
    """Tasks that are overdue or due today (for the Today card)."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    q = ((client or db()).table("crm_tasks").select("*")
         .eq("tenant_id", tenant_id).eq("status", "open")
         .lte("due_date", today)
         .order("due_date").order("priority"))
    return q.execute().data or []


def crm_update_task(task_id: str, tenant_id: str, fields: dict,
                    client: Client | None = None) -> None:
    """Update a task's fields."""
    fields["updated_at"] = datetime.now(timezone.utc).isoformat()
    ((client or db()).table("crm_tasks").update(fields)
     .eq("id", task_id).eq("tenant_id", tenant_id).execute())


def crm_complete_tasks_for_promise(promise_id: str, tenant_id: str) -> None:
    """Auto-complete tasks linked to a resolved promise (Phase 4a §19.2)."""
    now = datetime.now(timezone.utc).isoformat()
    (db().table("crm_tasks")
     .update({"status": "done", "completed_at": now, "updated_at": now})
     .eq("tenant_id", tenant_id).eq("promise_id", promise_id)
     .eq("status", "open").execute())


# ── Phase 4b: Sequences (§19.3) ─────────────────────────────────────────────
# Request-path reads/writes are tenant-scoped (client = caller's JWT client, RLS
# the second wall). The engine's cross-tenant sweep uses the service-role db().

def crm_list_sequences(tenant_id: str, *, include_archived: bool = False,
                       client: Client | None = None) -> list[dict]:
    q = ((client or db()).table("crm_sequences").select("*")
         .eq("tenant_id", tenant_id))
    if not include_archived:
        q = q.neq("status", "archived")
    return q.order("created_at", desc=True).execute().data or []


def crm_get_sequence(sequence_id: str, tenant_id: str,
                     client: Client | None = None) -> dict | None:
    res = ((client or db()).table("crm_sequences").select("*")
           .eq("id", sequence_id).eq("tenant_id", tenant_id).limit(1).execute())
    return res.data[0] if res.data else None


def crm_create_sequence(tenant_id: str, *, name: str, description: str | None = None,
                        trigger_type: str = "manual", trigger_config: dict | None = None,
                        client: Client | None = None) -> dict:
    row = {"tenant_id": tenant_id, "name": name, "description": description,
           "trigger_type": trigger_type, "trigger_config": trigger_config or {}}
    return (client or db()).table("crm_sequences").insert(row).execute().data[0]


def crm_update_sequence(sequence_id: str, tenant_id: str, fields: dict,
                        client: Client | None = None) -> None:
    fields = {**fields, "updated_at": datetime.now(timezone.utc).isoformat()}
    ((client or db()).table("crm_sequences").update(fields)
     .eq("id", sequence_id).eq("tenant_id", tenant_id).execute())


def crm_count_active_sequences(tenant_id: str, client: Client | None = None) -> int:
    """How many non-archived sequences the tenant has (guardrail: max 5 active)."""
    res = ((client or db()).table("crm_sequences").select("id", count="exact")
           .eq("tenant_id", tenant_id).eq("status", "active").execute())
    return res.count or 0


def crm_list_steps(sequence_id: str, tenant_id: str,
                   client: Client | None = None) -> list[dict]:
    return ((client or db()).table("crm_sequence_steps").select("*")
            .eq("tenant_id", tenant_id).eq("sequence_id", sequence_id)
            .order("step_order").execute().data or [])


def crm_add_step(tenant_id: str, sequence_id: str, *, step_order: int,
                 delay_days: int = 3, action_type: str = "send_email",
                 template_subject: str | None = None, template_body: str | None = None,
                 exit_on_reply: bool = True, side_effects: list | None = None,
                 client: Client | None = None) -> dict:
    row = {"tenant_id": tenant_id, "sequence_id": sequence_id,
           "step_order": step_order, "delay_days": delay_days,
           "action_type": action_type, "template_subject": template_subject,
           "template_body": template_body, "exit_on_reply": exit_on_reply,
           "side_effects": side_effects or []}
    return (client or db()).table("crm_sequence_steps").insert(row).execute().data[0]


def crm_update_step(step_id: str, tenant_id: str, fields: dict,
                    client: Client | None = None) -> None:
    ((client or db()).table("crm_sequence_steps").update(fields)
     .eq("id", step_id).eq("tenant_id", tenant_id).execute())


def crm_delete_step(step_id: str, tenant_id: str, client: Client | None = None) -> None:
    ((client or db()).table("crm_sequence_steps").delete()
     .eq("id", step_id).eq("tenant_id", tenant_id).execute())


def crm_enroll(tenant_id: str, sequence_id: str, contact_id: str,
               deal_id: str | None = None, client: Client | None = None) -> dict | None:
    """Enroll a contact in a sequence. Returns the enrollment row, or None when
    blocked (do_not_sequence, a duplicate active enrollment, or no step 1).
    next_action_at = now + step 1's delay_days."""
    c = client or db()
    contact = (c.table("crm_contacts").select("do_not_sequence")
               .eq("id", contact_id).eq("tenant_id", tenant_id).limit(1).execute().data)
    if contact and contact[0].get("do_not_sequence"):
        return None
    existing = (c.table("crm_sequence_enrollments").select("id")
                .eq("tenant_id", tenant_id).eq("sequence_id", sequence_id)
                .eq("contact_id", contact_id).eq("status", "active").limit(1).execute().data)
    if existing:
        return None
    steps = crm_list_steps(sequence_id, tenant_id, client=client)
    if not steps:
        return None
    first_delay = int(steps[0].get("delay_days") or 0)
    next_at = (datetime.now(timezone.utc) + timedelta(days=first_delay)).isoformat()
    row = {"tenant_id": tenant_id, "sequence_id": sequence_id, "contact_id": contact_id,
           "deal_id": deal_id, "current_step": 0, "status": "active",
           "next_action_at": next_at}
    return c.table("crm_sequence_enrollments").insert(row).execute().data[0]


def crm_list_enrollments(tenant_id: str, *, sequence_id: str | None = None,
                         contact_id: str | None = None, status: str | None = None,
                         client: Client | None = None) -> list[dict]:
    q = ((client or db()).table("crm_sequence_enrollments").select("*")
         .eq("tenant_id", tenant_id))
    if sequence_id:
        q = q.eq("sequence_id", sequence_id)
    if contact_id:
        q = q.eq("contact_id", contact_id)
    if status:
        q = q.eq("status", status)
    return q.order("enrolled_at", desc=True).execute().data or []


def crm_get_enrollment(enrollment_id: str, tenant_id: str,
                       client: Client | None = None) -> dict | None:
    res = ((client or db()).table("crm_sequence_enrollments").select("*")
           .eq("id", enrollment_id).eq("tenant_id", tenant_id).limit(1).execute())
    return res.data[0] if res.data else None


def crm_due_enrollments(now_iso: str, limit: int = 200) -> list[dict]:
    """Active enrollments whose next_action_at has passed, ACROSS ALL TENANTS
    (service role bypasses RLS — the engine's cross-tenant sweep)."""
    return (db().table("crm_sequence_enrollments").select("*")
            .eq("status", "active").lte("next_action_at", now_iso)
            .order("next_action_at").limit(limit).execute().data or [])


def crm_advance_enrollment(enrollment_id: str, tenant_id: str, *, current_step: int,
                           next_action_at: str) -> None:
    """Move to the next step and re-arm the timer (service role — engine path)."""
    (db().table("crm_sequence_enrollments")
     .update({"current_step": current_step, "next_action_at": next_action_at})
     .eq("id", enrollment_id).eq("tenant_id", tenant_id).execute())


def crm_reschedule_enrollment(enrollment_id: str, tenant_id: str,
                              next_action_at: str) -> None:
    """Push next_action_at out without advancing the step (rate-limit collisions)."""
    (db().table("crm_sequence_enrollments")
     .update({"next_action_at": next_action_at})
     .eq("id", enrollment_id).eq("tenant_id", tenant_id).execute())


def crm_exit_enrollment(enrollment_id: str, tenant_id: str, reason: str) -> None:
    (db().table("crm_sequence_enrollments")
     .update({"status": "exited", "exit_reason": reason,
              "exited_at": datetime.now(timezone.utc).isoformat()})
     .eq("id", enrollment_id).eq("tenant_id", tenant_id).execute())


def crm_complete_enrollment(enrollment_id: str, tenant_id: str) -> None:
    (db().table("crm_sequence_enrollments")
     .update({"status": "completed", "exit_reason": "completed",
              "exited_at": datetime.now(timezone.utc).isoformat()})
     .eq("id", enrollment_id).eq("tenant_id", tenant_id).execute())


def crm_set_enrollment_status(enrollment_id: str, tenant_id: str, status: str,
                              client: Client | None = None) -> None:
    """Request-path pause/resume/exit (tenant-scoped)."""
    fields: dict = {"status": status}
    if status in ("exited", "completed"):
        fields["exited_at"] = datetime.now(timezone.utc).isoformat()
        fields.setdefault("exit_reason", "manual" if status == "exited" else "completed")
    ((client or db()).table("crm_sequence_enrollments").update(fields)
     .eq("id", enrollment_id).eq("tenant_id", tenant_id).execute())


def crm_contact_replied_since(tenant_id: str, contact_email: str, since_iso: str) -> bool:
    """True if an inbound email from this address landed since `since_iso`
    (exit-on-reply check; service role — engine path)."""
    if not contact_email:
        return False
    res = (db().table("email_log").select("id")
           .eq("tenant_id", tenant_id).eq("kind", "inbound")
           .eq("from_address", contact_email).gte("created_at", since_iso)
           .limit(1).execute())
    return bool(res.data)


def crm_sequenced_today(tenant_id: str, contact_id: str) -> bool:
    """True if this contact already got a sequence email today (rate limit:
    max 1 sequence email/contact/day). Joins enrollments (contact) → email_log
    via the sequence 'kind' + day window; service role — engine path."""
    start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0,
                                               microsecond=0).isoformat()
    contact = (db().table("crm_contacts").select("email")
               .eq("id", contact_id).eq("tenant_id", tenant_id).limit(1).execute().data)
    if not contact or not contact[0].get("email"):
        return False
    res = (db().table("email_log").select("id")
           .eq("tenant_id", tenant_id).eq("kind", "sequence")
           .eq("from_address", contact[0]["email"]).gte("created_at", start)
           .limit(1).execute())
    return bool(res.data)


# ── Events OS (docs/os/events.md §5) — the event primitive + its children ─────
# An `event` IS a crm_deals row (1:1) enriched: crm_events carries the date/timings,
# crm_event_guests the guest list (NOT contacts — §14), crm_event_stakeholders the
# suppliers + staff the chase engine (Phase B) will drive. Same crm_* conventions:
# tenant_id first-class, client=None → service-role db() for the capture hook /
# schedulers, a TenantCtx.client for RLS-scoped route reads; IDOR-safe (every read
# and write is tenant-scoped). Phase A stores + lists + rolls up; the chase and
# run-of-show engines are Phases B/C.

def crm_create_event(tenant_id: str, *, deal_id: str, event_date=None, event_type=None,
                     guest_count=None, client: Client | None = None) -> dict:
    """Create the 1:1 crm_events row for a deal. Called from the intel capture hook
    (app/events_os.on_deal_is_event) when an enquiry reads as a dated occasion. The
    deal_id UNIQUE index (uq_crm_events_deal) makes this once-per-deal by construction."""
    row = {"tenant_id": tenant_id, "deal_id": deal_id}
    if event_date is not None:
        row["event_date"] = event_date
    if event_type is not None:
        row["event_type"] = event_type
    if guest_count is not None:
        row["guest_count"] = guest_count
    cl = client or db()
    try:
        return cl.table("crm_events").insert(row).execute().data[0]
    except Exception:
        # Race: two near-simultaneous inbounds (or a replay) for the same deal both
        # saw crm_event_for_deal → None and both INSERT — the second violates
        # uq_crm_events_deal. Re-select the winner (get-or-create), so this email's
        # guests/timings land on the existing event rather than being dropped —
        # mirroring crm_get_or_create_contact's except→re-select.
        existing = crm_event_for_deal(deal_id, tenant_id, client=cl)
        if existing:
            return existing
        raise


def crm_get_event(event_id: str, tenant_id: str, client: Client | None = None) -> dict | None:
    res = (
        (client or db()).table("crm_events").select("*")
        .eq("id", event_id).eq("tenant_id", tenant_id).limit(1).execute()
    )
    return res.data[0] if res.data else None


def crm_event_for_deal(deal_id: str, tenant_id: str, client: Client | None = None) -> dict | None:
    """The event backing a deal, or None — the 1:1 lookup the capture hook checks
    before creating (so re-processing an event thread updates, never duplicates)."""
    res = (
        (client or db()).table("crm_events").select("*")
        .eq("deal_id", deal_id).eq("tenant_id", tenant_id).limit(1).execute()
    )
    return res.data[0] if res.data else None


def crm_update_event(event_id: str, tenant_id: str, updates: dict,
                     client: Client | None = None) -> dict:
    """Merge fields onto an event (event_date / timings / headcount / licence /
    contingency / status / run_of_show), tenant-scoped + updated_at stamped."""
    fields = {**updates, "updated_at": datetime.now(timezone.utc).isoformat()}
    res = (
        (client or db()).table("crm_events").update(fields)
        .eq("id", event_id).eq("tenant_id", tenant_id).execute()
    )
    return res.data[0] if res.data else {}


def crm_events_between(tenant_id: str, start_date: str, end_date: str,
                       client: Client | None = None) -> list[dict]:
    """The daily-surface spine query — events with event_date in [start,end], date
    order, each with its backing deal (title/value/stage) embedded. NULL-dated events
    (not yet pinned) are excluded: the board is date-driven. The child rollups
    (dietary matrix, stakeholder lights) are joined per-event by the route/module."""
    res = (
        (client or db()).table("crm_events")
        .select("*, crm_deals(id,title,value_estimate,status,"
                "crm_stages(id,name,stage_type),crm_companies(id,name),"
                "crm_contacts(id,name,email))")
        .eq("tenant_id", tenant_id)
        .gte("event_date", start_date).lte("event_date", end_date)
        .order("event_date").execute()
    )
    return res.data or []


def crm_list_events(tenant_id: str, *, status: str | None = None, limit: int = 200,
                    client: Client | None = None) -> list[dict]:
    q = (
        (client or db()).table("crm_events")
        .select("*, crm_deals(id,title,value_estimate,status)")
        .eq("tenant_id", tenant_id)
    )
    if status:
        q = q.eq("status", status)
    return q.order("event_date", desc=True).limit(limit).execute().data or []


# ── Guests + dietary rollup (NOT crm_contacts — §14) ──────────────────────────
def _anon_dietary_signature(dietary, dietary_tags) -> str:
    """A stable key for an anonymous (name=NULL) dietary count so reprocessing the
    same "6 vegan guests" line UPDATES the one row rather than stacking a phantom
    second (the uq_crm_event_guests_name index exempts NULL names). Keyed on the
    sorted tag-set + free-text dietary, case-folded."""
    tags = sorted(str(t).strip().lower() for t in (dietary_tags or []) if str(t).strip())
    return "|".join(tags) + "::" + (str(dietary or "").strip().lower())


def crm_upsert_event_guest(tenant_id: str, *, event_id: str, name: str | None = None,
                           party_size: int = 1, rsvp: str = "unknown",
                           dietary: str | None = None, dietary_tags: list | None = None,
                           table_name: str | None = None, source_email: str | None = None,
                           client: Client | None = None) -> dict:
    """Idempotent on (event_id, lower(name)) so re-extraction from a later email
    reinforces one guest, not duplicates — same discipline as promise dedup. A NULL
    name (an anonymous dietary count) always inserts (NULLs are distinct). Guests are
    NEVER crm_contacts (§14): an event's 120 guests must not pollute the CRM book."""
    payload = {"tenant_id": tenant_id, "event_id": event_id, "party_size": party_size,
               "rsvp": rsvp}
    if name is not None:
        payload["name"] = name
    if dietary is not None:
        payload["dietary"] = dietary
    if dietary_tags is not None:
        payload["dietary_tags"] = dietary_tags
    if table_name is not None:
        payload["table_name"] = table_name
    if source_email is not None:
        payload["source_email"] = source_email
    cl = client or db()

    def _find_named():
        """The existing named guest, matched EXACTLY (case-insensitive) — never
        .ilike: an LLM-supplied name carries `%`/`_`/`(` (e.g. "Mr & Mrs Smith
        (50% deposit)") that .ilike would read as wildcards → wrong-row match or a
        missed dup. Fetch the event's named guests and compare lower()==lower() in
        Python (the unique key is functional-lower, not a stored column)."""
        rows = (cl.table("crm_event_guests").select("id,name")
                .eq("tenant_id", tenant_id).eq("event_id", event_id).execute()).data or []
        needle = name.strip().lower()
        return next((r for r in rows
                     if str(r.get("name") or "").strip().lower() == needle), None)

    def _find_anon():
        """The existing anonymous (name=NULL) row for this dietary signature — the
        idempotency key for an anonymous count ("6 vegan guests") so a reprocess of
        the same email UPDATES it rather than stacking a second phantom row (the
        unique index exempts NULL names, so we dedup on the tag signature here)."""
        rows = (cl.table("crm_event_guests").select("id,name,dietary,dietary_tags")
                .eq("tenant_id", tenant_id).eq("event_id", event_id).execute()).data or []
        sig = _anon_dietary_signature(dietary, dietary_tags)
        return next((r for r in rows
                     if not (r.get("name") or "").strip()
                     and _anon_dietary_signature(r.get("dietary"), r.get("dietary_tags")) == sig),
                    None)

    def _update(gid):
        upd = {k: v for k, v in payload.items() if k not in ("tenant_id", "event_id")}
        upd["updated_at"] = datetime.now(timezone.utc).isoformat()
        res = (cl.table("crm_event_guests").update(upd)
               .eq("id", gid).eq("tenant_id", tenant_id).execute())
        return res.data[0] if res.data else {"id": gid, **payload}

    # Dedup within the event: update the existing row rather than stack.
    existing = _find_named() if name else _find_anon()
    if existing:
        return _update(existing["id"])
    try:
        return cl.table("crm_event_guests").insert(payload).execute().data[0]
    except Exception:
        # Race: a concurrent inbound inserted the same guest between our select and
        # insert (the uq_crm_event_guests_name index fired) — re-select + update,
        # mirroring crm_get_or_create_contact's except→re-select.
        existing = _find_named() if name else _find_anon()
        if existing:
            return _update(existing["id"])
        raise


def crm_list_event_guests(tenant_id: str, event_id: str,
                          client: Client | None = None) -> list[dict]:
    res = (
        (client or db()).table("crm_event_guests").select("*")
        .eq("tenant_id", tenant_id).eq("event_id", event_id)
        .order("created_at").execute()
    )
    return res.data or []


def crm_event_dietary_matrix(tenant_id: str, event_id: str,
                             client: Client | None = None) -> dict:
    """{"vegan": 6, "gluten_free": 3, "nut_allergy": 2, ...} aggregated from every
    guest's dietary_tags, weighted by party_size — the supplier order + the board card
    render from this. Pure aggregation over the guest list; no new table."""
    matrix: dict[str, int] = {}
    for g in crm_list_event_guests(tenant_id, event_id, client=client):
        # A stored 0 (a declined/placeholder party) weights 0 — `or 1` would rewrite
        # it to a phantom cover, inflating the supplier order.
        ps = g.get("party_size")
        weight = ps if ps is not None else 1
        for tag in (g.get("dietary_tags") or []):
            key = str(tag).strip()
            if key:
                matrix[key] = matrix.get(key, 0) + weight
    return matrix


# ── Stakeholders (the chase substrate — Phase A stores, Phase B chases) ───────
def crm_upsert_event_stakeholder(tenant_id: str, *, event_id: str, kind: str, role: str,
                                 name: str | None = None, email: str | None = None,
                                 contact_id: str | None = None,
                                 outstanding: str | None = None,
                                 client: Client | None = None) -> dict:
    """Add (or reinforce) a supplier/staff member on an event. Idempotent on
    (event_id, kind, lower(role)) so re-extraction of the same party from a later
    email updates rather than duplicates — the chase engine (Phase B) then drives it."""
    cl = client or db()
    payload = {"kind": kind, "role": role}
    if name is not None:
        payload["name"] = name
    if email is not None:
        payload["email"] = email
    if contact_id is not None:
        payload["contact_id"] = contact_id
    if outstanding is not None:
        payload["outstanding"] = outstanding

    def _find():
        """The existing (kind, lower(role)) stakeholder, matched EXACTLY (case-
        insensitive) — never .ilike: an LLM-supplied role carries `%`/`_`/`(` (e.g.
        "Bar (100% cover)") that .ilike would read as wildcards → wrong-row match or
        a missed dup. Compare lower()==lower() in Python (functional-lower key)."""
        rows = (cl.table("crm_event_stakeholders").select("id,kind,role")
                .eq("tenant_id", tenant_id).eq("event_id", event_id)
                .eq("kind", kind).execute()).data or []
        needle = role.strip().lower()
        return next((r for r in rows
                     if str(r.get("role") or "").strip().lower() == needle), None)

    def _update(sid):
        upd = {**payload, "updated_at": datetime.now(timezone.utc).isoformat()}
        res = (cl.table("crm_event_stakeholders").update(upd)
               .eq("id", sid).eq("tenant_id", tenant_id).execute())
        return res.data[0] if res.data else {"id": sid, **upd}

    existing = _find()
    if existing:
        return _update(existing["id"])
    try:
        return (cl.table("crm_event_stakeholders")
                .insert({"tenant_id": tenant_id, "event_id": event_id, **payload})
                .execute().data[0])
    except Exception:
        # Race: a concurrent inbound inserted the same stakeholder (the new
        # uq_crm_event_stakeholders_role index fired) — re-select + update,
        # mirroring crm_get_or_create_contact's except→re-select.
        existing = _find()
        if existing:
            return _update(existing["id"])
        raise


def crm_list_event_stakeholders(tenant_id: str, event_id: str,
                                client: Client | None = None) -> list[dict]:
    res = (
        (client or db()).table("crm_event_stakeholders").select("*")
        .eq("tenant_id", tenant_id).eq("event_id", event_id)
        .order("created_at").execute()
    )
    return res.data or []


def crm_list_event_stakeholders_by_id(stakeholder_id: str, tenant_id: str,
                                      client: Client | None = None) -> list[dict]:
    """One stakeholder by id, tenant-scoped (IDOR-safe: a foreign id resolves to []).
    Returns a list so the callsite can `if not rows` — mirrors the crm_* read shape."""
    res = ((client or db()).table("crm_event_stakeholders").select("*")
           .eq("id", stakeholder_id).eq("tenant_id", tenant_id).limit(1).execute())
    return res.data or []


def crm_update_event_stakeholder(stakeholder_id: str, tenant_id: str, updates: dict,
                                 client: Client | None = None) -> dict:
    """confirmation / outstanding / last_chased_at / chase_count / promise link — the
    fields the chase engine (Phase B) mutates. Tenant-scoped + updated_at stamped."""
    fields = {**updates, "updated_at": datetime.now(timezone.utc).isoformat()}
    res = (
        (client or db()).table("crm_event_stakeholders").update(fields)
        .eq("id", stakeholder_id).eq("tenant_id", tenant_id).execute()
    )
    return res.data[0] if res.data else {}


# ── Phase B: the chase substrate wired to promises (docs/os/events.md §6.2, §12) ─
def crm_link_stakeholder_promise(tenant_id: str, *, stakeholder_id: str, deal_id: str,
                                 description: str, client: Client | None = None) -> str | None:
    """Ensure a `they_owe` crm_promise exists for a stakeholder's OUTSTANDING item and
    link it via crm_event_stakeholders.outstanding_promise_id — the killer-feature
    substrate (§12.1). Get-or-create so a restated item stays ONE promise, never stacks
    (the promise engine's dedup discipline, §18.3): we match an OPEN they_owe promise on
    this deal with the EXACT (case-insensitive) description before inserting a new one.

    Returns the linked promise id, or None on a bad call. Best-effort at the callsite —
    the linkage must never break event capture. Reuses the shipped crm_promises table
    (does NOT fork the promise engine); a null email_log_id marks a stakeholder-sourced
    promise (extraction-sourced ones carry the source email + the source-uniq index)."""
    description = (description or "").strip()
    if not stakeholder_id or not deal_id or not description:
        return None
    cl = client or db()
    # If this stakeholder is already linked to an OPEN promise, reuse it — the item was
    # linked on a prior email; re-linking must not mint a second promise for one chase.
    srow = (cl.table("crm_event_stakeholders").select("outstanding_promise_id")
            .eq("id", stakeholder_id).eq("tenant_id", tenant_id).limit(1).execute()).data
    existing_id = (srow[0].get("outstanding_promise_id") if srow else None)
    if existing_id:
        open_rows = (cl.table("crm_promises").select("id,status")
                     .eq("id", existing_id).eq("tenant_id", tenant_id).limit(1).execute()).data
        if open_rows and open_rows[0].get("status") == "open":
            return existing_id   # still open → keep the one promise
    # Dedup against the deal's open they_owe promises by EXACT (case-insensitive) match —
    # never .ilike: an LLM item carries %/_/( (e.g. "licence (100% cover)") .ilike would
    # read as wildcards → a wrong-row match or a missed dedup. Compare lower()==lower().
    needle = description.lower()
    deal_open = (cl.table("crm_promises").select("id,description")
                 .eq("tenant_id", tenant_id).eq("deal_id", deal_id)
                 .eq("direction", "they_owe").eq("status", "open").execute()).data or []
    match = next((p for p in deal_open
                  if str(p.get("description") or "").strip().lower() == needle), None)
    promise_id = match["id"] if match else None
    if promise_id is None:
        ins = (cl.table("crm_promises").insert({
            "tenant_id": tenant_id, "deal_id": deal_id, "direction": "they_owe",
            "description": description[:300], "email_log_id": None, "status": "open",
        }).execute().data)
        if not ins:
            return None
        promise_id = ins[0]["id"]
    (cl.table("crm_event_stakeholders")
     .update({"outstanding_promise_id": promise_id,
              "updated_at": datetime.now(timezone.utc).isoformat()})
     .eq("id", stakeholder_id).eq("tenant_id", tenant_id).execute())
    return promise_id


def crm_stakeholders_to_chase(tenant_id: str, *, now, max_chases: int = 3,
                              step_days: tuple = (4, 8), client: Client | None = None) -> list[dict]:
    """The chase engine's work queue (§6.2, §12.3): PENDING stakeholders on UPCOMING
    events whose linked they_owe promise is still OPEN, under the chase cap, not muted,
    and DUE by the escalation cadence. Service-role sweep (client=None → db()).

    Selection contract:
      · confirmation='pending' only — 'confirmed'/'declined'/'needs_info' are excluded,
        and 'muted' is the per-stakeholder don't-chase state (§14) so it's excluded too.
      · the event's date is today-or-future (a past event is not chased).
      · chase_count < max_chases — an exhausted item stops chasing + surfaces to a human.
      · the linked promise (outstanding_promise_id) is still OPEN — a resolved promise
        already flipped the stakeholder to confirmed, but this is the belt-and-braces
        guard so a race can't chase a just-fulfilled item.
      · DUE: last_chased_at is null (never chased) OR older than the step interval for
        the NEXT chase level (step_days[chase_count]) — the gone-quiet cadence.
    `now` is a tz-aware datetime (the caller passes datetime.now(DIARY_TZ)); the "today"
    and due windows are computed from it, never a naive local now()."""
    cl = client or db()
    today = now.date().isoformat()
    # Join events so we can filter on event_date + reference the deal for the chase facts.
    rows = (cl.table("crm_event_stakeholders")
            .select("*, crm_events(id,deal_id,event_date,event_type,guest_count,"
                    "crm_deals(id,title,crm_companies(name)))")
            .eq("tenant_id", tenant_id).eq("confirmation", "pending").execute()).data or []
    due: list[dict] = []
    for s in rows:
        ev = s.get("crm_events") or {}
        ed = ev.get("event_date")
        if not ed or ed < today:            # only upcoming events; NULL-dated skipped
            continue
        cc = s.get("chase_count")
        cc = cc if cc is not None else 0    # never falsy-coerce a real 0
        if cc >= max_chases:                # exhausted → needs a human, stop chasing
            continue
        if not s.get("outstanding_promise_id"):
            continue                        # nothing tracked to chase
        # Confirm the linked promise is still open (a resolved one already flipped us).
        prow = (cl.table("crm_promises").select("status")
                .eq("id", s["outstanding_promise_id"]).eq("tenant_id", tenant_id)
                .limit(1).execute()).data
        if not prow or prow[0].get("status") != "open":
            continue
        # DUE by the cadence: index the step by the NEXT chase (chase_count), clamped.
        last = s.get("last_chased_at")
        if last:
            step = step_days[min(cc, len(step_days) - 1)]
            cutoff = now - timedelta(days=step)
            # last_chased_at is stored UTC ISO; compare as tz-aware.
            try:
                last_dt = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                last_dt = None
            if last_dt is not None and last_dt > cutoff:
                continue                    # chased too recently → not yet due
        due.append(s)
    return due


def crm_stamp_stakeholder_chased(stakeholder_id: str, tenant_id: str, *, now,
                                 client: Client | None = None) -> dict:
    """Record one chase: last_chased_at=now, chase_count++. Read-then-write on the
    count (the Supabase client has no atomic increment); the daily job is single-writer
    (in-process, single-instance) so a lost update needs two concurrent chases of ONE
    item on ONE day — which the due-window guard already precludes."""
    cl = client or db()
    cur = (cl.table("crm_event_stakeholders").select("chase_count")
           .eq("id", stakeholder_id).eq("tenant_id", tenant_id).limit(1).execute()).data
    cc = (cur[0].get("chase_count") if cur else 0)
    cc = cc if cc is not None else 0
    res = (cl.table("crm_event_stakeholders")
           .update({"last_chased_at": now.astimezone(timezone.utc).isoformat(),
                    "chase_count": cc + 1,
                    "updated_at": datetime.now(timezone.utc).isoformat()})
           .eq("id", stakeholder_id).eq("tenant_id", tenant_id).execute())
    return res.data[0] if res.data else {}


def crm_stakeholders_for_promise(promise_id: str, tenant_id: str,
                                 client: Client | None = None) -> list[dict]:
    """Every stakeholder whose outstanding item is this promise — the auto-resolve hook
    (§12.4) reads this to flip them to 'confirmed' when the promise resolves. Tenant-
    scoped. Empty for a non-events tenant (no stakeholder rows) → the hook no-ops."""
    if not promise_id:
        return []
    res = ((client or db()).table("crm_event_stakeholders").select("*")
           .eq("tenant_id", tenant_id).eq("outstanding_promise_id", promise_id).execute())
    return res.data or []


# ── Storefront OS (docs/os/storefront.md §5 — "the shop off your inbox") ──────
# The shop_* layer follows the crm_* conventions exactly: tenant_id first-class
# on every fn; client=None → service-role db() for schedulers/OCR, a
# TenantCtx.client for RLS-scoped route reads; get-or-create is race-safe
# (insert in try/except, re-fetch on conflict) like crm_get_or_create_contact.
# The atomic on-hand mutations (reserve / stock-in) go through the Postgres RPCs
# shipped in the migration — availability is decided in the DB, never Python
# (src/app/scheduling.py: "two racing emails cannot overbook"). Phases 2–4 (drafter
# tools, OCR engine, best-before) are BUILT (P2 22298cf, P3 dfd76f8, P4 926d0ec):
# shop_past_buyers_of, shop_products_expiring and the OCR-job fns below are real.

# ── catalogue ────────────────────────────────────────────────────────────────
def shop_list_products(tenant_id: str, *, q: str | None = None, low_stock: bool = False,
                       expiring: bool = False, limit: int = 200, offset: int = 0,
                       client: Client | None = None) -> list[dict]:
    """Active products, name-ordered. `q` filters on name/sku (the catalogue search);
    `low_stock` keeps only products at/below their reorder_threshold; `expiring` keeps
    only dated stock still on hand (the Phase-4 panel reads this)."""
    query = ((client or db()).table("shop_products").select("*")
             .eq("tenant_id", tenant_id))
    if q:
        query = query.or_(f"name.ilike.%{q}%,sku.ilike.%{q}%")
    query = query.order("name").limit(limit).offset(offset)
    rows = query.execute().data or []
    if low_stock:
        rows = [r for r in rows if _shop_is_low(r)]
    if expiring:
        rows = [r for r in rows if r.get("best_before_date") and float(r.get("qty_on_hand") or 0) > 0]
    return rows


def _shop_is_low(p: dict) -> bool:
    """A product is low when it has a reorder_threshold and on-hand is at/below it."""
    thr = p.get("reorder_threshold")
    return thr is not None and float(p.get("qty_on_hand") or 0) <= float(thr)


def shop_get_product(product_id: str, tenant_id: str,
                     client: Client | None = None) -> dict | None:
    res = ((client or db()).table("shop_products").select("*")
           .eq("id", product_id).eq("tenant_id", tenant_id).limit(1).execute())
    return res.data[0] if res.data else None


def shop_get_or_create_product(tenant_id: str, name: str, *, unit: str = "each",
                               best_before_date: str | None = None,
                               supplier: str | None = None,
                               client: Client | None = None) -> dict:
    """The tenant's product row for a name — created on first sight, deduped on
    lower(name) (the unique index). Race-safe: insert in try/except, re-fetch on
    conflict, exactly like crm_get_or_create_contact. A second delivery of
    'Sourdough' tops up the existing row rather than forking it (§12.1)."""
    c = client or db()
    key = (name or "").strip()
    res = (c.table("shop_products").select("*")
           .eq("tenant_id", tenant_id).ilike("name", key).limit(1).execute())
    for row in (res.data or []):
        if str(row.get("name") or "").strip().lower() == key.lower():
            return row
    try:
        ins = (c.table("shop_products").insert({
            "tenant_id": tenant_id, "name": key, "unit": unit,
            "best_before_date": best_before_date, "supplier": supplier,
        }).execute())
        return ins.data[0]
    except Exception:
        res = (c.table("shop_products").select("*")
               .eq("tenant_id", tenant_id).ilike("name", key).limit(1).execute())
        for row in (res.data or []):
            if str(row.get("name") or "").strip().lower() == key.lower():
                return row
        raise


def shop_update_product(product_id: str, tenant_id: str, fields: dict,
                        client: Client | None = None) -> None:
    """Edit price / threshold / best-before / supplier etc. Stamps updated_at.
    qty_on_hand/qty_reserved are NOT edited here — those move only through the
    atomic RPCs (shop_apply_stock_in / shop_hold_stock_if_available)."""
    fields = {**fields, "updated_at": datetime.now(timezone.utc).isoformat()}
    ((client or db()).table("shop_products").update(fields)
     .eq("id", product_id).eq("tenant_id", tenant_id).execute())


def shop_merge_product_custom_data(product_id: str, tenant_id: str, values: dict,
                                   client: Client | None = None) -> None:
    """Fold extracted/confirmed extras into custom_data JSONB (read-merge-write;
    last write wins per key) — the crm_merge_custom_data shape for products."""
    row = shop_get_product(product_id, tenant_id, client=client)
    if row is None:
        return
    merged = {**(row.get("custom_data") or {}), **values}
    ((client or db()).table("shop_products")
     .update({"custom_data": merged, "updated_at": datetime.now(timezone.utc).isoformat()})
     .eq("id", product_id).eq("tenant_id", tenant_id).execute())


def shop_products_low_stock(tenant_id: str, client: Client | None = None) -> list[dict]:
    """Products at/below their reorder_threshold — the daily surface's reorder panel."""
    return shop_list_products(tenant_id, low_stock=True, client=client)


# ── stock ledger + atomic stock-in ───────────────────────────────────────────
def shop_record_movement(tenant_id: str, product_id: str, delta, reason: str, *,
                         source: str | None = None, source_ref: str | None = None,
                         best_before_date: str | None = None, note: str | None = None,
                         client: Client | None = None) -> dict:
    """Append one stock-ledger row (the append-only audit). This ONLY writes the
    ledger — the qty_on_hand mutation is the atomic RPC's job (shop_apply_stock_in
    for +, shop_hold_stock_if_available for reserve). Used for adjust/wastage/
    release notes that don't ride an RPC."""
    res = ((client or db()).table("shop_stock_movements").insert({
        "tenant_id": tenant_id, "product_id": product_id, "delta": delta,
        "reason": reason, "source": source, "source_ref": source_ref,
        "best_before_date": best_before_date, "note": note,
    }).execute())
    return res.data[0]


def shop_apply_stock_in(tenant_id: str, lines: list[dict], *, source: str = "ocr",
                        source_ref: str | None = None) -> dict:
    """Bulk stock-in: for each {name, qty, unit, best_before, unit_price} get-or-create
    the product (dedupe on lower(name)), then call the atomic increment RPC
    (shop_apply_stock_in_one) which bumps qty_on_hand under a row-lock AND writes the
    stock_movements row in one transaction. Returns {created, updated, movements}.
    Service-role — used now for MANUAL stock-in, later by OCR apply (§12.1)."""
    created = updated = movements = 0
    for line in lines or []:
        name = str(line.get("name") or "").strip()
        qty = line.get("qty")
        if not name or qty in (None, "") or float(qty) == 0:
            continue
        existing = (db().table("shop_products").select("id")
                    .eq("tenant_id", tenant_id).ilike("name", name).limit(1).execute()).data
        prod = shop_get_or_create_product(
            tenant_id, name, unit=str(line.get("unit") or "each"),
            best_before_date=line.get("best_before"), supplier=line.get("supplier"))
        was_new = not any(str(e.get("name") or "").strip().lower() == name.lower()
                          for e in (existing or []))
        if was_new:
            created += 1
        else:
            updated += 1
        # A unit_price on the delivery updates the product's sell reference (only when given).
        if line.get("unit_price") not in (None, ""):
            shop_update_product(prod["id"], tenant_id, {"unit_price": line["unit_price"]})
        db().rpc("shop_apply_stock_in_one", {
            "p_tenant_id": tenant_id, "p_product_id": prod["id"],
            "p_delta": float(qty), "p_best_before": line.get("best_before"),
            "p_source": source, "p_source_ref": source_ref,
            "p_note": line.get("note"),
        }).execute()
        movements += 1
    return {"created": created, "updated": updated, "movements": movements}


# ── transactions ─────────────────────────────────────────────────────────────
def shop_list_transactions(tenant_id: str, *, status: str | None = None,
                           ready_by: str | None = None, contact_id: str | None = None,
                           limit: int = 100, offset: int = 0,
                           client: Client | None = None) -> list[dict]:
    """Transactions newest first, with their line items embedded (one query serves
    the order list + the status filter)."""
    q = ((client or db()).table("shop_transactions")
         .select("*, shop_transaction_items(*)")
         .eq("tenant_id", tenant_id))
    if status:
        q = q.eq("status", status)
    if ready_by:
        q = q.lte("ready_date", ready_by)
    if contact_id:
        q = q.eq("contact_id", contact_id)
    return q.order("created_at", desc=True).limit(limit).offset(offset).execute().data or []


def shop_today_collections(tenant_id: str, today_iso: str,
                           client: Client | None = None) -> list[dict]:
    """Daily surface panel 1 — reservations collectable today or overdue: status
    'reserved'/'ready' with ready_date <= today (nulls kept — a reservation with no
    date still needs working), oldest first. Items embedded for the counter view."""
    rows = ((client or db()).table("shop_transactions")
            .select("*, shop_transaction_items(*)")
            .eq("tenant_id", tenant_id)
            .in_("status", ["reserved", "ready"])
            .order("ready_date").order("created_at").execute()).data or []
    return [r for r in rows
            if not r.get("ready_date") or str(r["ready_date"])[:10] <= today_iso]


def shop_get_transaction(tx_id: str, tenant_id: str,
                         client: Client | None = None) -> dict | None:
    """One transaction with its line items embedded."""
    res = ((client or db()).table("shop_transactions")
           .select("*, shop_transaction_items(*)")
           .eq("id", tx_id).eq("tenant_id", tenant_id).limit(1).execute())
    return res.data[0] if res.data else None


def shop_create_transaction(tenant_id: str, *, contact_id: str | None = None,
                            company_id: str | None = None, channel: str = "email",
                            items: list[dict] | None = None, status: str = "enquiry",
                            ready_date: str | None = None, thread_id: str | None = None,
                            payment_method: str | None = None,
                            client: Client | None = None) -> dict:
    """Create a transaction + its line items (a walk-in counter entry, or an enquiry).
    Does NOT reserve stock — a reservation goes through shop_hold_stock_if_available so
    the atomic guarantee holds. total_amount is denormalised Σ line_total for the surface."""
    c = client or db()
    total = 0.0
    clean_items = []
    for it in (items or []):
        name = str(it.get("product_name") or it.get("name") or "").strip()
        if not name:
            continue
        qty = float(it.get("qty") or 1)
        price = it.get("unit_price")
        line = round(float(price) * qty, 2) if price not in (None, "") else None
        if line:
            total += line
        clean_items.append({"product_id": it.get("product_id"), "product_name": name,
                            "qty": qty, "unit_price": price, "line_total": line})
    tx = (c.table("shop_transactions").insert({
        "tenant_id": tenant_id, "contact_id": contact_id, "company_id": company_id,
        "status": status, "channel": channel, "ready_date": ready_date,
        "thread_id": thread_id, "payment_method": payment_method,
        "total_amount": round(total, 2) if clean_items else None,
    }).execute()).data[0]
    for it in clean_items:
        c.table("shop_transaction_items").insert(
            {"tenant_id": tenant_id, "transaction_id": tx["id"], **it}).execute()
    return tx


def shop_set_transaction_status(tx_id: str, tenant_id: str, status: str, *,
                                client: Client | None = None) -> dict:
    """Move a transaction through its lifecycle (enquiry → reserved → ready →
    completed | declined | cancelled, §3). Stamps completed_at when it reaches
    'completed'. Returns the updated row."""
    fields: dict = {"status": status, "updated_at": datetime.now(timezone.utc).isoformat()}
    if status == "completed":
        fields["completed_at"] = datetime.now(timezone.utc).isoformat()
    res = ((client or db()).table("shop_transactions").update(fields)
           .eq("id", tx_id).eq("tenant_id", tenant_id).execute())
    return res.data[0] if res.data else {}


# ── atomic reservation (the hold_booking_if_capacity analogue) ───────────────
def shop_hold_stock_if_available(tenant_id: str, product_id: str, qty, *,
                                 transaction_id: str | None = None,
                                 contact_id: str | None = None,
                                 company_id: str | None = None,
                                 ready_date: str | None = None,
                                 channel: str = "email",
                                 thread_id: str | None = None,
                                 source: str = "agent") -> dict | None:
    """Atomically reserve `qty` iff (qty_on_hand - qty_reserved) >= qty via the SQL
    RPC (service-role, like hold_booking_if_capacity): bumps qty_reserved, writes a
    'reserve' stock_movement, creates/updates the transaction+item. Returns the
    transaction row, or None ('out_of_stock') when there isn't enough available —
    the ONLY place availability is decided, so two racing emails can never
    over-reserve."""
    res = db().rpc("shop_hold_stock_if_available", {
        "p_tenant_id": tenant_id, "p_product_id": product_id, "p_qty": float(qty),
        "p_transaction_id": transaction_id, "p_contact_id": contact_id,
        "p_company_id": company_id, "p_ready_date": ready_date,
        "p_channel": channel, "p_thread_id": thread_id, "p_source": source,
    }).execute()
    return res.data[0] if res.data else None


# ── Phase 2: drafter tools + proactive jobs (docs/os/storefront.md §6) ────────
def shop_get_product_by_name(tenant_id: str, name: str,
                             client: Client | None = None) -> dict | None:
    """The catalogue row for an exact (case-insensitive) product name, or None —
    the drafter's check_stock/reserve_stock lookup (dedupes on lower(name) like the
    counter). Best-effort: never raises, so a missing product just reads as 'not found'."""
    key = (name or "").strip()
    if not key:
        return None
    res = ((client or db()).table("shop_products").select("*")
           .eq("tenant_id", tenant_id).ilike("name", key).limit(1).execute())
    for row in (res.data or []):
        if str(row.get("name") or "").strip().lower() == key.lower():
            return row
    return None


def shop_storefront_context(tenant_id: str, contact_id: str | None,
                            mentioned_names: list[str] | None = None,
                            client: Client | None = None) -> str:
    """A short <storefront> context string for the drafting prompt (the seam crm_context
    uses): on-hand for the products this email mentions, the customer's past purchases,
    and any OPEN reservation they already hold. Pure facts, cheap, best-effort — '' when
    there is nothing to say, so a shop with no matching data adds nothing to the prompt.

    Populated in src/app/pipeline.py only for storefront tenants; the drafter never invents
    stock, so this is the honest picture it answers from."""
    c = client or db()
    lines: list[str] = []
    seen: set = set()
    for name in (mentioned_names or []):
        key = (name or "").strip()
        if not key or key.lower() in seen:
            continue
        seen.add(key.lower())
        prod = shop_get_product_by_name(tenant_id, key, client=c)
        if not prod:
            continue
        avail = max(0.0, float(prod.get("qty_on_hand") or 0) - float(prod.get("qty_reserved") or 0))
        price = (f" at £{float(prod['unit_price']):g}/{prod.get('unit') or 'each'}"
                 if prod.get("unit_price") is not None else "")
        lines.append(f"In stock now: {prod['name']} — {avail:g} available{price}")
    if contact_id:
        try:
            past = (c.table("shop_transaction_items")
                    .select("product_name, shop_transactions!inner(contact_id, status)")
                    .eq("tenant_id", tenant_id)
                    .eq("shop_transactions.contact_id", contact_id)
                    .eq("shop_transactions.status", "completed").limit(50).execute()).data or []
            bought = sorted({str(r.get("product_name") or "").strip()
                             for r in past if r.get("product_name")})
            if bought:
                lines.append("Has bought before: " + ", ".join(bought[:8]))
        except Exception:
            pass
        try:
            openres = (c.table("shop_transactions")
                       .select("*, shop_transaction_items(product_name, qty)")
                       .eq("tenant_id", tenant_id).eq("contact_id", contact_id)
                       .in_("status", ["reserved", "ready"])
                       .order("created_at", desc=True).limit(3).execute()).data or []
            for tx in openres:
                items = ", ".join(
                    f"{float(it.get('qty') or 1):g}× {it.get('product_name')}"
                    for it in (tx.get("shop_transaction_items") or []))
                rd = str(tx.get("ready_date"))[:10] if tx.get("ready_date") else ""
                lines.append(f"Open reservation ({tx.get('status')}): {items or 'an order'}"
                             + (f" — ready {rd}" if rd else ""))
        except Exception:
            pass
    return "\n".join(lines)


def shop_reservations_ready_today(tenant_id: str, today_iso: str,
                                  client: Client | None = None) -> list[dict]:
    """Reservations whose ready_date is TODAY (or overdue) and still status='reserved' —
    the ready-to-collect nudge feed (§6 ready_reminders). Items embedded for the facts.
    Service-role by default (the scheduler has no tenant JWT)."""
    rows = ((client or db()).table("shop_transactions")
            .select("*, shop_transaction_items(*), crm_contacts(id,email,name)")
            .eq("tenant_id", tenant_id).eq("status", "reserved")
            .not_.is_("ready_date", "null")
            .lte("ready_date", today_iso)
            .order("ready_date").order("created_at").execute()).data or []
    return rows


def shop_reservations_stale(tenant_id: str, before_iso: str,
                            client: Client | None = None) -> list[dict]:
    """Reservations still 'reserved' whose created_at is OLDER than `before_iso` (the
    reservation_hold_days cutoff) — the release-stale-reservations sweep feed (§14).
    Items embedded so the release can decrement each held line. Service-role."""
    rows = ((client or db()).table("shop_transactions")
            .select("*, shop_transaction_items(*), crm_contacts(id,email,name)")
            .eq("tenant_id", tenant_id).eq("status", "reserved")
            .lt("created_at", before_iso)
            .order("created_at").execute()).data or []
    return rows


def shop_release_reservation(tx_id: str, tenant_id: str,
                             client: Client | None = None) -> dict | None:
    """Release a reservation's held stock back to the shelf: for each line, decrement the
    product's qty_reserved by the held qty and write a 'release' stock_movement, then set
    the transaction status='cancelled'. The reverse of the reserve RPC — used by the
    cancel route and the stale-reservation sweep. Best-effort per line (a missing product
    just skips its decrement); returns the updated transaction, or None if not found.

    Not the atomic RPC: a release only ever ADDS availability back, so — unlike a reserve —
    it can never over-commit capacity if it races. The reserve path stays the atomic one."""
    c = client or db()
    tx = shop_get_transaction(tx_id, tenant_id, client=c)
    if not tx:
        return None
    for it in (tx.get("shop_transaction_items") or []):
        pid = it.get("product_id")
        qty = float(it.get("qty") or 0)
        if not pid or qty <= 0:
            continue
        prod = shop_get_product(pid, tenant_id, client=c)
        if not prod:
            continue
        new_reserved = max(0.0, float(prod.get("qty_reserved") or 0) - qty)
        (c.table("shop_products").update(
            {"qty_reserved": new_reserved,
             "updated_at": datetime.now(timezone.utc).isoformat()})
         .eq("id", pid).eq("tenant_id", tenant_id).execute())
        shop_record_movement(tenant_id, pid, qty, "release",
                             source="agent", source_ref=tx_id, client=c)
    return shop_set_transaction_status(tx_id, tenant_id, "cancelled", client=c)


# ── best-before drafter feed (Phase 4 — §12.2) ───────────────────────────────
def shop_products_expiring(tenant_id: str, within_days: int | None = None,
                           client: Client | None = None) -> list[dict]:
    """Products the best-before drafter should nudge: qty_on_hand > 0 with a
    best_before_date falling within their OWN sell_before_lead_days window of today
    (or a `within_days` override applied to every product). Soonest-expiry first.
    Service-role by default (the daily job has no tenant JWT). Nulls (undated /
    non-perishable stock) never fire — the nudge only exists for decaying stock."""
    today = datetime.now(timezone.utc).date()
    rows = ((client or db()).table("shop_products").select("*")
            .eq("tenant_id", tenant_id)
            .not_.is_("best_before_date", "null")
            .gt("qty_on_hand", 0)
            .order("best_before_date").execute()).data or []
    out = []
    for p in rows:
        bb = str(p.get("best_before_date") or "")[:10]
        if not bb:
            continue
        try:
            bb_date = date.fromisoformat(bb)
        except ValueError:
            continue
        lead = within_days if within_days is not None \
            else int(p.get("sell_before_lead_days") or 0)
        # inside the window when today is within `lead` days of (or already past) the date
        if today >= (bb_date - timedelta(days=lead)):
            out.append(p)
    return out


def shop_past_buyers_of(tenant_id: str, product_id: str, *, limit: int = 200,
                        client: Client | None = None) -> list[dict]:
    """The best-before drafter's personalised audience (§12.2): the DISTINCT CRM
    contacts who have a line item for this product on a COMPLETED transaction, have an
    email, and are NOT opted out (crm_contacts.do_not_sequence). Deterministic SQL —
    the targeting can NEVER hallucinate a non-buyer. Service-role by default.

    Walk transaction_items → their transaction (status='completed') → the contact;
    dedupe by contact id, keep first-seen order (soonest surfaced first)."""
    c = client or db()
    items = (c.table("shop_transaction_items")
             .select("transaction_id, shop_transactions(id,status,contact_id,"
                     "crm_contacts(id,email,name,do_not_sequence))")
             .eq("tenant_id", tenant_id).eq("product_id", product_id)
             .limit(max(limit * 4, 200)).execute()).data or []
    seen, buyers = set(), []
    for it in items:
        tx = it.get("shop_transactions") or {}
        if tx.get("status") != "completed":
            continue
        contact = tx.get("crm_contacts") or {}
        cid = contact.get("id")
        if not cid or cid in seen:
            continue
        if contact.get("do_not_sequence"):
            continue
        if not (contact.get("email") or "").strip():
            continue
        seen.add(cid)
        buyers.append(contact)
        if len(buyers) >= limit:
            break
    return buyers


def shop_had_special_today(tenant_id: str, contact_id: str, today_iso: str,
                           client: Client | None = None) -> bool:
    """True if this contact already got a kind='special' email today — the per-contact/
    day cap for the best-before drafter (§12.2, the sequences guardrail). Mirrors
    crm_sequenced_today: resolve the contact's email, then look for a 'special' row in
    email_log since midnight. Service-role — the daily job path."""
    c = client or db()
    start = f"{today_iso}T00:00:00+00:00"
    contact = (c.table("crm_contacts").select("email")
               .eq("id", contact_id).eq("tenant_id", tenant_id).limit(1).execute().data)
    if not contact or not (contact[0].get("email") or "").strip():
        return False
    res = (c.table("email_log").select("id")
           .eq("tenant_id", tenant_id).eq("kind", "special")
           .eq("from_address", contact[0]["email"]).gte("created_at", start)
           .limit(1).execute())
    return bool(res.data)
# ── OCR jobs (Phase 3 — the photo → stock pipeline, §5/§12.1) ─────────────────
def shop_create_ocr_job(tenant_id: str, kind: str, image_path: str | None, *,
                        email_log_id: str | None = None,
                        client: Client | None = None) -> dict:
    """One shop_ocr_jobs row per photographed delivery (status='pending'). image_path
    is the stored image reference; email_log_id is set only when the photo arrived as
    an email attachment (§12.1 entry point a). Service-role by default (the OCR route
    runs the extraction under the service client)."""
    res = ((client or db()).table("shop_ocr_jobs").insert({
        "tenant_id": tenant_id, "kind": kind or "delivery_note",
        "image_path": image_path, "email_log_id": email_log_id,
        "status": "pending",
    }).execute())
    return res.data[0]


def shop_get_ocr_job(job_id: str, tenant_id: str,
                     client: Client | None = None) -> dict | None:
    """One OCR job by id, tenant-scoped (IDOR-safe: filters on tenant_id)."""
    res = ((client or db()).table("shop_ocr_jobs").select("*")
           .eq("id", job_id).eq("tenant_id", tenant_id).limit(1).execute())
    return res.data[0] if res.data else None


def shop_set_ocr_extracted(job_id: str, tenant_id: str, extracted,
                           client: Client | None = None) -> None:
    """Store the vision extraction result (the banded lines) + flip status='extracted'
    so the job is ready for the owner's review on /app/shop/stock-in."""
    ((client or db()).table("shop_ocr_jobs")
     .update({"extracted": extracted, "status": "extracted"})
     .eq("id", job_id).eq("tenant_id", tenant_id).execute())


def shop_mark_ocr_applied(job_id: str, tenant_id: str,
                          client: Client | None = None) -> None:
    """Flip a reviewed job to status='applied' + stamp applied_at, once its checked
    lines have been committed to stock via shop_apply_stock_in. Idempotent: a job
    already 'applied' is a no-op (the apply route dedupes on the status)."""
    ((client or db()).table("shop_ocr_jobs")
     .update({"status": "applied",
              "applied_at": datetime.now(timezone.utc).isoformat()})
     .eq("id", job_id).eq("tenant_id", tenant_id).execute())
# ── SMS channel (docs/modules/module-sms-channel.md) ──────────────────────────────────
# One channel_connections row per tenant per channel_type; the webhook routes
# inbound by the platform-unique provisioned number. Opt-outs are keyed by
# number (not contact) so a pre-contact STOP is still honoured.
def sms_connection(tenant_id: str, client: Client | None = None) -> dict | None:
    """The tenant's active SMS connection (their provisioned number), or None."""
    res = ((client or db()).table("channel_connections").select("*")
           .eq("tenant_id", tenant_id).eq("channel_type", "sms")
           .eq("is_active", True).limit(1).execute())
    return res.data[0] if res.data else None


def channel_connection_by_number(phone_number: str,
                                 client: Client | None = None) -> dict | None:
    """The connection a provisioned number belongs to — the webhook's tenant
    routing (service role: the caller is Twilio, not a logged-in user)."""
    if not phone_number:
        return None
    res = ((client or db()).table("channel_connections").select("*")
           .eq("phone_number", phone_number).eq("is_active", True)
           .limit(1).execute())
    return res.data[0] if res.data else None


def upsert_channel_connection(tenant_id: str, channel_type: str, *,
                              phone_number: str | None = None,
                              provider_sids: dict | None = None,
                              config: dict | None = None) -> dict:
    """Create or refresh the tenant's connection for one channel (settings page
    / number provisioning). One row per (tenant, channel_type) by convention."""
    existing = (db().table("channel_connections").select("id")
                .eq("tenant_id", tenant_id).eq("channel_type", channel_type)
                .limit(1).execute())
    fields = {k: v for k, v in {
        "phone_number": phone_number, "provider_sids": provider_sids,
        "config": config, "is_active": True,
    }.items() if v is not None}
    if existing.data:
        res = (db().table("channel_connections").update(fields)
               .eq("id", existing.data[0]["id"]).eq("tenant_id", tenant_id).execute())
        return res.data[0]
    res = (db().table("channel_connections")
           .insert({"tenant_id": tenant_id, "channel_type": channel_type, **fields})
           .execute())
    return res.data[0]


def update_channel_config(connection_id: str, tenant_id: str, config: dict) -> None:
    (db().table("channel_connections").update({"config": config})
     .eq("id", connection_id).eq("tenant_id", tenant_id).execute())


def sms_opted_out(tenant_id: str, phone: str, client: Client | None = None) -> bool:
    """Has this number texted STOP? Checked before EVERY send (compliance gate)."""
    res = ((client or db()).table("sms_opt_outs").select("phone")
           .eq("tenant_id", tenant_id).eq("phone", phone).limit(1).execute())
    return bool(res.data)


def sms_opt_out(tenant_id: str, phone: str) -> None:
    """Record a STOP. Idempotent — a second STOP just keeps the row."""
    try:
        db().table("sms_opt_outs").insert(
            {"tenant_id": tenant_id, "phone": phone}).execute()
    except Exception:
        if not sms_opted_out(tenant_id, phone):
            raise  # a real failure, not the primary-key race


def sms_opt_in(tenant_id: str, phone: str) -> None:
    """Clear a STOP (the customer texted START)."""
    (db().table("sms_opt_outs").delete()
     .eq("tenant_id", tenant_id).eq("phone", phone).execute())


def sms_opt_out_list(tenant_id: str, client: Client | None = None) -> list[dict]:
    res = ((client or db()).table("sms_opt_outs").select("*")
           .eq("tenant_id", tenant_id).order("opted_out_at", desc=True).execute())
    return res.data or []


def recent_missed_call_text(tenant_id: str, phone: str, since_iso: str,
                            client: Client | None = None) -> bool:
    """Did we already text this caller back since `since_iso`? The 24h dedupe
    guard — repeated ring-outs must not spam (docs/modules/module-sms-channel.md §5)."""
    res = ((client or db()).table("email_log").select("id")
           .eq("tenant_id", tenant_id).eq("kind", "missed_call")
           .eq("from_address", phone).gte("created_at", since_iso)
           .limit(1).execute())
    return bool(res.data)


def sms_thread_since(tenant_id: str, phone: str, since_iso: str,
                     client: Client | None = None) -> bool:
    """Any SMS-channel row with this number since `since_iso`? The 'already
    mid-conversation today' guard — a caller we're actively texting doesn't
    need a missed-call nudge on top (docs/modules/module-sms-channel.md §5)."""
    res = ((client or db()).table("email_log").select("id")
           .eq("tenant_id", tenant_id).eq("channel", "sms")
           .eq("from_address", phone).gte("created_at", since_iso)
           .limit(1).execute())
    return bool(res.data)


def upcoming_booking_for_contacts(tenant_id: str, contacts: list[str], today_iso: str,
                                  client: Client | None = None) -> dict | None:
    """The next CONFIRMED booking for any of `contacts` (a phone and/or the CRM
    contact's email) from `today_iso` — the confirm-by-reply target
    (docs/modules/module-sms-channel.md §6). Soonest first; None when nothing's coming up."""
    contacts = [c for c in contacts if c]
    if not contacts:
        return None
    res = ((client or db()).table("bookings").select("*")
           .eq("tenant_id", tenant_id).eq("status", "confirmed")
           .gte("booking_date", today_iso).in_("contact", contacts)
           .order("booking_date").limit(1).execute())
    return res.data[0] if res.data else None


def stamp_client_confirmed(booking_id: str, tenant_id: str,
                           client: Client | None = None) -> None:
    """The client said they're coming (reply C / a confirm click). Feeds the
    no-show scorer's confirmed_click feature — a strong not-a-no-show signal."""
    (client or db()).table("bookings").update(
        {"client_confirmed_at": datetime.now(timezone.utc).isoformat()}
    ).eq("id", booking_id).eq("tenant_id", tenant_id).execute()


# ── Wholesale OS (docs/os/wholesale.md §5) ────────────────────────────────────
# An order IS a crm_deal (is_order=true) + wholesale_order_lines. Service-role
# writes; client= honoured on reads (RLS does the isolation). The CRM deal
# helpers (crm_create_deal / crm_update_deal) are reused, never re-implemented.

WHOLESALE_STATUSES = ("quote", "order", "allocation", "fulfilment", "invoice",
                      "payment", "complete", "held", "skipped", "on_hold_short")


def wholesale_insert_line(tenant_id: str, deal_id: str, *, description: str,
                          quantity: float, unit: str | None = None, sku: str | None = None,
                          unit_price: float | None = None, unit_cost: float | None = None,
                          line_order: int = 0) -> dict:
    res = db().table("wholesale_order_lines").insert({
        "tenant_id": tenant_id, "deal_id": deal_id, "description": description,
        "quantity": quantity, "unit": unit, "sku": sku,
        "unit_price": unit_price, "unit_cost": unit_cost, "line_order": line_order,
    }).execute()
    return res.data[0]


def wholesale_list_lines(tenant_id: str, deal_id: str, client: Client | None = None) -> list[dict]:
    res = ((client or db()).table("wholesale_order_lines").select("*")
           .eq("tenant_id", tenant_id).eq("deal_id", deal_id)
           .order("line_order").order("created_at").execute())
    return res.data or []


def wholesale_lines_for_deals(tenant_id: str, deal_ids: list[str],
                              client: Client | None = None) -> dict:
    """{deal_id: [lines]} in one query — the order book / week plan feed."""
    if not deal_ids:
        return {}
    res = ((client or db()).table("wholesale_order_lines").select("*")
           .eq("tenant_id", tenant_id).in_("deal_id", deal_ids)
           .order("line_order").execute())
    by_deal: dict = {}
    for row in res.data or []:
        by_deal.setdefault(row["deal_id"], []).append(row)
    return by_deal


def wholesale_set_allocation(tenant_id: str, line_id: str, allocated_qty: float) -> None:
    (db().table("wholesale_order_lines").update({"allocated_qty": allocated_qty})
     .eq("id", line_id).eq("tenant_id", tenant_id).execute())


def wholesale_delete_line(tenant_id: str, line_id: str) -> None:
    (db().table("wholesale_order_lines").delete()
     .eq("id", line_id).eq("tenant_id", tenant_id).execute())


def _wholesale_maybe_num(v) -> float | None:
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def wholesale_create_order(tenant_id: str, *, company_id: str, contact_id: str | None,
                           title: str, required_by: str | None, lines: list[dict],
                           standing_order_id: str | None = None,
                           status: str = "quote") -> dict:
    """Create an order = crm_create_deal(is_order=True) + N line inserts.
    value_estimate = sum(qty * unit_price) over the lines that actually land
    (blank descriptions are dropped BEFORE the sum). Returns the deal row with
    'lines' attached."""
    lines = [ln for ln in lines if (ln.get("description") or "").strip()]
    value = 0.0
    for ln in lines:
        try:
            value += float(ln.get("quantity") or 0) * float(ln.get("unit_price") or 0)
        except (TypeError, ValueError):
            continue
    deal = crm_create_deal(tenant_id, {
        "company_id": company_id, "contact_id": contact_id, "title": title,
        "is_order": True, "wholesale_status": status, "required_by": required_by,
        "standing_order_id": standing_order_id, "value_estimate": round(value, 2),
    })
    made = []
    for i, ln in enumerate(lines):
        made.append(wholesale_insert_line(
            tenant_id, deal["id"], description=ln["description"].strip(),
            quantity=float(ln.get("quantity") or 0), unit=ln.get("unit"),
            sku=ln.get("sku"), unit_price=_wholesale_maybe_num(ln.get("unit_price")),
            unit_cost=_wholesale_maybe_num(ln.get("unit_cost")), line_order=i))
    deal["lines"] = made
    return deal


def wholesale_list_orders(tenant_id: str, *, status: str | None = None,
                          due_before: str | None = None, due_after: str | None = None,
                          company_id: str | None = None,
                          client: Client | None = None) -> list[dict]:
    q = ((client or db()).table("crm_deals").select("*")
         .eq("tenant_id", tenant_id).eq("is_order", True))
    if status:
        q = q.eq("wholesale_status", status)
    if company_id:
        q = q.eq("company_id", company_id)
    if due_before:
        q = q.lte("required_by", due_before)
    if due_after:
        q = q.gte("required_by", due_after)
    res = q.order("required_by", desc=False).order("created_at", desc=True).limit(500).execute()
    return res.data or []


def wholesale_get_order(tenant_id: str, deal_id: str, client: Client | None = None) -> dict | None:
    res = ((client or db()).table("crm_deals").select("*")
           .eq("tenant_id", tenant_id).eq("id", deal_id).eq("is_order", True)
           .limit(1).execute())
    return res.data[0] if res.data else None


def wholesale_set_status(tenant_id: str, deal_id: str, status: str) -> None:
    """Advance wholesale_status (delegates to crm_update_deal — updated_at stamped)."""
    crm_update_deal(deal_id, tenant_id, {"wholesale_status": status})


# ── Standing orders ───────────────────────────────────────────────────────────

def wholesale_create_standing_order(tenant_id: str, fields: dict) -> dict:
    res = db().table("wholesale_standing_orders").insert(
        {"tenant_id": tenant_id, **fields}).execute()
    return res.data[0]


def wholesale_update_standing_order(tenant_id: str, so_id: str, fields: dict) -> None:
    fields = {**fields, "updated_at": datetime.now(timezone.utc).isoformat()}
    (db().table("wholesale_standing_orders").update(fields)
     .eq("id", so_id).eq("tenant_id", tenant_id).execute())


def wholesale_list_standing_orders(tenant_id: str, *, status: str | None = None,
                                   client: Client | None = None) -> list[dict]:
    q = ((client or db()).table("wholesale_standing_orders").select("*")
         .eq("tenant_id", tenant_id))
    if status:
        q = q.eq("status", status)
    return q.order("created_at", desc=True).execute().data or []


def wholesale_get_standing_order(tenant_id: str, so_id: str,
                                 client: Client | None = None) -> dict | None:
    res = ((client or db()).table("wholesale_standing_orders").select("*")
           .eq("tenant_id", tenant_id).eq("id", so_id).limit(1).execute())
    return res.data[0] if res.data else None


def wholesale_due_standing_orders(on_or_before: str, limit: int = 500) -> list[dict]:
    """Service-role sweep read: active standing orders due to generate, ACROSS all
    tenants (the engine scopes per row.tenant_id) — mirrors crm_due_enrollments."""
    res = (db().table("wholesale_standing_orders").select("*")
           .eq("status", "active").lte("next_run_on", on_or_before)
           .order("next_run_on").limit(limit).execute())
    return res.data or []


def wholesale_advance_standing_order(tenant_id: str, so_id: str, *, last_run_on: str,
                                     next_run_on: str | None) -> None:
    wholesale_update_standing_order(tenant_id, so_id, {
        "last_run_on": last_run_on, "next_run_on": next_run_on})


# ── Credit maths (read-side; the §6c gate calls this) ─────────────────────────

def wholesale_credit_exposure(tenant_id: str, company_id: str,
                              client: Client | None = None) -> dict:
    """{'open_orders', 'unpaid_invoices', 'exposure', 'limit', 'headroom', 'over'}.
    open_orders = SUM(value_estimate) over order deals in flight; unpaid_invoices =
    SUM(total) over this buyer's 'sent' invoices (matched by the buyer's contact
    emails — invoices carry customer_email, not company_id). Exposure is only as
    good as the invoices BLAM8 sees (docs/os/wholesale.md §14 caveat)."""
    c = client or db()
    open_res = (c.table("crm_deals").select("value_estimate")
                .eq("tenant_id", tenant_id).eq("company_id", company_id)
                .eq("is_order", True)
                .in_("wholesale_status", ["order", "allocation", "fulfilment", "on_hold_short"])
                .execute())
    open_orders = sum(float(r.get("value_estimate") or 0) for r in (open_res.data or []))

    unpaid = 0.0
    emails_res = (c.table("crm_contacts").select("email")
                  .eq("tenant_id", tenant_id).eq("company_id", company_id).execute())
    emails = [r["email"] for r in (emails_res.data or []) if r.get("email")]
    if emails:
        inv_res = (c.table("invoices").select("total")
                   .eq("tenant_id", tenant_id).eq("status", "sent")
                   .in_("customer_email", emails).execute())
        unpaid = sum(float(r.get("total") or 0) for r in (inv_res.data or []))

    limit_val = None
    comp_res = (c.table("crm_companies").select("custom_data")
                .eq("tenant_id", tenant_id).eq("id", company_id).limit(1).execute())
    if comp_res.data:
        raw = (comp_res.data[0].get("custom_data") or {}).get("credit_limit")
        if raw not in (None, ""):
            try:
                limit_val = float(str(raw).replace("£", "").replace(",", "").strip())
            except (TypeError, ValueError):
                limit_val = None

    exposure = round(open_orders + unpaid, 2)
    return {
        "open_orders": round(open_orders, 2), "unpaid_invoices": round(unpaid, 2),
        "exposure": exposure, "limit": limit_val,
        "headroom": (round(limit_val - exposure, 2) if limit_val is not None else None),
        "over": (limit_val is not None and exposure > limit_val),
    }


def wholesale_order_companies(tenant_id: str, client: Client | None = None) -> list[dict]:
    """Buyer companies for the credit ledger — every company; the ledger view
    computes exposure per company and surfaces the ones with limits/orders."""
    res = ((client or db()).table("crm_companies").select("id, name, custom_data")
           .eq("tenant_id", tenant_id).order("name").limit(500).execute())
    return res.data or []


# ── partner referral programme (SERVICE ROLE ONLY — see src/app/partners.py) ────
# The partners table has RLS with no policy (service-role only). Reads/writes
# here run behind either the partner's own verified session email
# (src/app/partner_routes.py), the operator console (require_admin), or the
# server-side attribution paths (SSO callback / onboarding) — never raw from a
# tenant request.
def get_partner_by_code(ref_code: str) -> dict | None:
    """The partner owning a referral code (any status — callers decide whether
    a disabled partner still attributes). Codes are stored uppercase."""
    code = (ref_code or "").strip().upper()
    if not code:
        return None
    res = db().table("partners").select("*").eq("ref_code", code).limit(1).execute()
    return res.data[0] if res.data else None


def get_partner_by_email(email: str) -> dict | None:
    """The partner row matching a sign-in email (lowercased) — how /partner
    recognises a signed-in user as a partner."""
    addr = (email or "").strip().lower()
    if not addr:
        return None
    res = db().table("partners").select("*").eq("email", addr).limit(1).execute()
    return res.data[0] if res.data else None


def create_partner(name: str, email: str, ref_code: str,
                   commission_rate: float = 0.20, notes: str | None = None,
                   source: str = "operator", agreed_at: str | None = None,
                   terms_version: str | None = None,
                   signed_name: str | None = None) -> dict:
    """`source`/`agreed_at`/`terms_version`/`signed_name` record HOW the partner
    joined: operator-created (paper agreement) or self-serve (the Partner
    Referral Agreement signed electronically by typing their name)."""
    res = db().table("partners").insert({
        "name": name.strip(), "email": email.strip().lower(),
        "ref_code": ref_code.strip().upper(),
        "commission_rate": commission_rate, "notes": notes,
        "source": source, "agreed_at": agreed_at, "terms_version": terms_version,
        "signed_name": signed_name,
    }).execute()
    return res.data[0]


def list_partners() -> list[dict]:
    res = db().table("partners").select("*").order("created_at").execute()
    return res.data or []


def set_partner_status(partner_id: str, status: str) -> None:
    db().table("partners").update({"status": status}).eq("id", partner_id).execute()


def get_tenant_referral(tenant_id: str) -> dict | None:
    """The tenant's referral attribution — a DEDICATED read (not part of
    get_tenant's column list) so a not-yet-applied migration can only break the
    referral surfaces, never every dashboard page."""
    res = (db().table("tenants")
           .select("referred_by_partner_id,referral_code_used,referred_at")
           .eq("id", tenant_id).limit(1).execute())
    return res.data[0] if res.data else None


def attach_referral(tenant_id: str, partner_id: str, ref_code: str,
                    referred_at_iso: str) -> bool:
    """Attribute a tenant to a partner — ONLY if the tenant has no attribution
    yet (first code wins, forever; the lifetime-commission promise depends on
    this never being rewritable). True if this call set it."""
    res = (db().table("tenants")
           .update({"referred_by_partner_id": partner_id,
                    "referral_code_used": ref_code.strip().upper(),
                    "referred_at": referred_at_iso})
           .eq("id", tenant_id).is_("referred_by_partner_id", "null").execute())
    return bool(res.data)


def list_partner_referrals(partner_id: str) -> list[dict]:
    """Every tenant attributed to a partner, with the fields the earnings
    estimate needs (plan/status/industry/contract). Cross-tenant by design —
    only ever surfaced to the owning partner or the operator console."""
    res = (db().table("tenants")
           .select("id,name,industry,plan,subscription_status,billing_provider,"
                   "contract_monthly_gbp,created_at,referred_at")
           .eq("referred_by_partner_id", partner_id)
           .order("referred_at", desc=True).execute())
    return res.data or []


def get_partner(partner_id: str) -> dict | None:
    res = db().table("partners").select("*").eq("id", partner_id).limit(1).execute()
    return res.data[0] if res.data else None


# — leads (manual introductions; attribution protection when no link is used) —
def create_partner_lead(partner_id: str, company_name: str, contact_email: str,
                        expires_at_iso: str, status: str = "registered",
                        reason: str | None = None) -> dict:
    """`status`/`reason` let an introduction that is ALREADY known ineligible
    (the business has an account) be recorded as such — visible on the portal,
    never silently refused."""
    res = db().table("partner_leads").insert({
        "partner_id": partner_id, "company_name": company_name.strip(),
        "contact_email": contact_email.strip().lower(), "expires_at": expires_at_iso,
        "status": status, "reason": reason,
    }).execute()
    return res.data[0]


def list_partner_leads(partner_id: str) -> list[dict]:
    res = (db().table("partner_leads").select("*")
           .eq("partner_id", partner_id).order("created_at", desc=True).execute())
    return res.data or []


def list_leads_by_email(contact_email: str, status: str | None = "registered") -> list[dict]:
    """Leads registered against a signup email — the SSO-callback match. Oldest
    first, so the earliest introduction wins when no code was used."""
    q = (db().table("partner_leads").select("*")
         .eq("contact_email", (contact_email or "").strip().lower())
         .order("created_at"))
    if status:
        q = q.eq("status", status)
    return q.execute().data or []


def update_partner_lead(lead_id: str, fields: dict) -> None:
    db().table("partner_leads").update(fields).eq("id", lead_id).execute()


def list_all_partner_leads() -> list[dict]:
    """Every lead across partners — the operator's dispute-review view."""
    res = db().table("partner_leads").select("*").order("created_at", desc=True).execute()
    return res.data or []


# — commissions (the earned-money ledger; written from invoice.paid only) —
def create_partner_commission(partner_id: str, tenant_id: str, invoice_id: str,
                              invoice_total: float, amount: float,
                              earned_at_iso: str) -> bool:
    """One EARNED commission row per collected Stripe invoice. unique(invoice_id)
    makes webhook retries safe — a duplicate insert returns False, never raises."""
    try:
        db().table("partner_commissions").insert({
            "partner_id": partner_id, "tenant_id": tenant_id, "invoice_id": invoice_id,
            "invoice_total": invoice_total, "amount": amount, "earned_at": earned_at_iso,
        }).execute()
        return True
    except Exception:
        return False   # unique violation — the invoice was already credited


def tenant_has_commission(tenant_id: str) -> bool:
    """Has this referral ever earned (i.e. converted)? Drives the 90-day
    attribution-window check — conversion in-window locks lifetime earning."""
    res = (db().table("partner_commissions").select("id")
           .eq("tenant_id", tenant_id).limit(1).execute())
    return bool(res.data)


def list_partner_commissions(partner_id: str) -> list[dict]:
    res = (db().table("partner_commissions").select("*")
           .eq("partner_id", partner_id).order("earned_at", desc=True).execute())
    return res.data or []


# — payouts (monthly statements; manual runs from /admin/partners) —
def create_partner_payout(partner_id: str, total: float, period_start: str | None,
                          period_end: str | None, reference: str | None,
                          method: str = "bank") -> dict:
    """One settled statement. `method` is 'bank' (money moved) or 'credit' (it came
    off the partner's own subscription) — one ledger either way, so scheduled -> paid
    still happens in exactly one place."""
    res = db().table("partner_payouts").insert({
        "partner_id": partner_id, "total": total, "period_start": period_start,
        "period_end": period_end, "reference": reference,
        "method": "credit" if method == "credit" else "bank",
    }).execute()
    return res.data[0]


# — settlement preference + withdrawal requests (src/app/partner_settlement.py) —
def set_partner_settlement(partner_id: str, mode: str,
                           credit_tenant_id: str | None = None) -> None:
    """Switch a partner between bank payouts and subscription credit. The tenant is
    stored alongside the mode so the credit always lands on the account they chose,
    even if they later belong to more than one."""
    fields: dict = {"settlement_mode": "credit" if mode == "credit" else "cash"}
    if mode == "credit":
        fields["credit_tenant_id"] = credit_tenant_id
    db().table("partners").update(fields).eq("id", partner_id).execute()


def create_partner_withdrawal(partner_id: str, requested_gbp: float) -> dict | None:
    """Open a withdrawal request. Returns None when one is already open — the unique
    partial index does the enforcing, so a double-submit can't create two."""
    try:
        res = db().table("partner_withdrawals").insert({
            "partner_id": partner_id, "requested_gbp": requested_gbp,
        }).execute()
        return res.data[0] if res.data else None
    except Exception:
        return None      # unique violation: a request is already open


def list_partner_withdrawals(partner_id: str, limit: int = 20) -> list[dict]:
    res = (db().table("partner_withdrawals").select("*").eq("partner_id", partner_id)
           .order("requested_at", desc=True).limit(limit).execute())
    return res.data or []


def open_partner_withdrawal(partner_id: str) -> dict | None:
    res = (db().table("partner_withdrawals").select("*").eq("partner_id", partner_id)
           .eq("status", "requested").limit(1).execute())
    return res.data[0] if res.data else None


def list_open_withdrawals() -> list[dict]:
    """The operator's queue, oldest first — someone is waiting on each of these."""
    res = (db().table("partner_withdrawals").select("*").eq("status", "requested")
           .order("requested_at").execute())
    return res.data or []


def get_partner_withdrawal(withdrawal_id: str) -> dict | None:
    res = (db().table("partner_withdrawals").select("*")
           .eq("id", withdrawal_id).limit(1).execute())
    return res.data[0] if res.data else None


def settle_partner_withdrawal(withdrawal_id: str, payout_id: str,
                              settled_gbp: float) -> dict | None:
    """Mark a request paid. Status-guarded so a retry (or two operators clicking at
    once) can't settle it twice — a second call matches zero rows and returns None."""
    res = (db().table("partner_withdrawals").update({
        "status": "paid", "payout_id": payout_id, "settled_gbp": settled_gbp,
        "paid_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", withdrawal_id).eq("status", "requested").execute())
    return res.data[0] if res.data else None


def cancel_partner_withdrawal(withdrawal_id: str, note: str | None = None) -> dict | None:
    res = (db().table("partner_withdrawals").update({
        "status": "cancelled", "note": note,
    }).eq("id", withdrawal_id).eq("status", "requested").execute())
    return res.data[0] if res.data else None


def list_partner_payouts(partner_id: str) -> list[dict]:
    res = (db().table("partner_payouts").select("*")
           .eq("partner_id", partner_id).order("paid_at", desc=True).execute())
    return res.data or []


def auth_user_exists(email: str) -> bool:
    """Does a Supabase auth account already exist for this email? Security-
    definer RPC over auth.users, executable by the SERVICE ROLE ONLY (an
    email-existence oracle must never be reachable with the anon key). False
    on any failure — lead registration then degrades to the lazy check at that
    email's next sign-in instead of breaking."""
    try:
        res = db().rpc("auth_user_exists",
                       {"p_email": (email or "").strip().lower()}).execute()
        data = res.data
        if isinstance(data, list):
            data = data[0] if data else False
        return bool(data)
    except Exception:
        return False


def get_commission_by_invoice(invoice_id: str) -> dict | None:
    """One ledger row per Stripe invoice (or per '<invoice>:clawback' key) —
    the refund-clawback lookup."""
    res = (db().table("partner_commissions").select("*")
           .eq("invoice_id", invoice_id).limit(1).execute())
    return res.data[0] if res.data else None


def update_partner_commission(commission_id: str, fields: dict) -> None:
    db().table("partner_commissions").update(fields).eq("id", commission_id).execute()


def mark_commission_ids_paid(commission_ids: list[str], payout_id: str) -> None:
    """Attach EXACTLY the swept rows to a payout — the month-end run pays only
    cleared commissions (app/partners.payable_commissions), so this must not
    touch anything still inside the net-30 hold."""
    if not commission_ids:
        return
    (db().table("partner_commissions")
     .update({"status": "paid", "payout_id": payout_id})
     .in_("id", commission_ids).eq("status", "scheduled").execute())


# ── Concierge business Q&A (read-only tool layer + chat history) ─────────────
def search_email_log(tenant_id: str, *, q: str | None = None, contact: str | None = None,
                     date_from: str | None = None, date_to: str | None = None,
                     status: str | None = None, category: str | None = None, limit: int = 50,
                     client: Client | None = None) -> list[dict]:
    """Search this tenant's email_log for the concierge's business Q&A, newest-first.
    `q` = substring over subject/body/sender; `contact` = substring over the sender's
    address/name (so 'Sarah' finds sarah.jones@…); dates bound created_at (ISO);
    `category` = the smart inbox label (needs_reply | draft_ready | quote_sent |
    booking_pending | fyi | automated). Trimmed columns only — bodies are snipped
    by the tool layer, never shipped whole."""
    query = (
        (client or db()).table("email_log")
        .select("id,from_address,from_name,contact_name,contact_company,subject,"
                "body_in,draft_reply,status,thread_state,request_summary,"
                "booking_details,channel,created_at,category,mailbox_id")
        .eq("tenant_id", tenant_id)
        .order("created_at", desc=True)
    )
    if q:
        # Strip PostgREST or-filter delimiters so a stray comma/paren can't break the query.
        like = "%" + "".join(c for c in q if c not in ",()") + "%"
        query = query.or_(f"subject.ilike.{like},body_in.ilike.{like},"
                          f"from_address.ilike.{like},from_name.ilike.{like}")
    if contact:
        like = "%" + "".join(c for c in contact if c not in ",()") + "%"
        query = query.or_(f"from_address.ilike.{like},from_name.ilike.{like},"
                          f"contact_name.ilike.{like}")
    if status:
        query = query.eq("status", status)
    if category:
        query = query.eq("category", category)
    if date_from:
        query = query.gte("created_at", date_from)
    if date_to:
        query = query.lte("created_at", date_to)
    return query.limit(max(1, min(int(limit or 50), 50))).execute().data or []


def search_bookings(tenant_id: str, *, date_from: str | None = None, date_to: str | None = None,
                    contact: str | None = None, status: str | None = None, limit: int = 50,
                    client: Client | None = None) -> list[dict]:
    """Bookings for the concierge's diary Q&A: optional date range (booking_date),
    contact substring, and status filter (default = proposed+confirmed, the live
    diary). Trimmed columns, soonest-first."""
    query = (
        (client or db()).table("bookings")
        .select("id,contact,summary,booking_date,window,status,booking_ref,"
                "quoted_amount,job_type,duration_minutes,created_at,"
                "start_block,length_blocks")
        .eq("tenant_id", tenant_id)
        .order("booking_date", desc=False)
    )
    if status:
        query = query.eq("status", status)
    else:
        query = query.in_("status", ["proposed", "confirmed"])
    if contact:
        like = "%" + "".join(c for c in contact if c not in ",()") + "%"
        query = query.ilike("contact", like)
    if date_from:
        query = query.gte("booking_date", date_from)
    if date_to:
        query = query.lte("booking_date", date_to)
    return query.limit(max(1, min(int(limit or 50), 50))).execute().data or []


def stays_running_into(tenant_id: str, day_iso: str, *, lookback_days: int = 60,
                       contact: str | None = None, client: Client | None = None) -> list[dict]:
    """Live bookings that STARTED in the `lookback_days` before `day_iso` and
    carry a grid span — the candidates for a stay still running on that day
    (the caller does the overlap maths with slotgrid.stay_span). The Concierge's
    "which subjects are in on the 13th" (live 2026-09-23): search_bookings keys on
    booking_date, so a stay that began on the 9th was invisible on the 13th.
    Newest first, so the ones closest to the day survive the limit."""
    from datetime import date as _date, timedelta as _td
    day = _date.fromisoformat(str(day_iso)[:10])
    query = (
        (client or db()).table("bookings")
        .select("id,contact,summary,booking_date,window,status,booking_ref,"
                "quoted_amount,job_type,duration_minutes,created_at,"
                "start_block,length_blocks")
        .eq("tenant_id", tenant_id)
        .in_("status", ["proposed", "confirmed"])
        .gte("booking_date", (day - _td(days=max(1, int(lookback_days)))).isoformat())
        .lt("booking_date", day.isoformat())
        .not_.is_("length_blocks", "null")
        .order("booking_date", desc=True)
    )
    if contact:
        like = "%" + "".join(c for c in contact if c not in ",()") + "%"
        query = query.ilike("contact", like)
    return query.limit(200).execute().data or []


def _search_refs(table: str, column: str, select: str, tenant_id: str, prefix: str,
                 limit: int, client: Client | None) -> list[dict]:
    """Rows whose reference STARTS with `prefix` (app/search: "BK-10" finds
    BK-1003 and BK-1041), newest first. A bare prefix ("INV-") lists the latest."""
    prefix = "".join(c for c in str(prefix or "").upper() if c not in ",()%*")
    if not prefix:
        return []
    res = ((client or db()).table(table).select(select)
           .eq("tenant_id", tenant_id).ilike(column, prefix + "%")
           .order("created_at", desc=True)
           .limit(max(1, min(int(limit or 6), 20))).execute())
    return res.data or []


def search_booking_refs(tenant_id: str, prefix: str, limit: int = 6,
                        client: Client | None = None) -> list[dict]:
    return _search_refs("bookings", "booking_ref",
                        "id,booking_ref,summary,contact,booking_date,window,status,resource_type",
                        tenant_id, prefix, limit, client)


def search_invoice_refs(tenant_id: str, prefix: str, limit: int = 6,
                        client: Client | None = None) -> list[dict]:
    return _search_refs("invoices", "invoice_ref",
                        "id,invoice_ref,customer_name,customer_email,status,total",
                        tenant_id, prefix, limit, client)


def search_quote_refs(tenant_id: str, prefix: str, limit: int = 6,
                      client: Client | None = None) -> list[dict]:
    return _search_refs("quotes", "quote_ref",
                        "id,quote_ref,title,customer_name,customer_email,status,total",
                        tenant_id, prefix, limit, client)


def get_booking_by_ref(tenant_id: str, ref: str, client: Client | None = None) -> dict | None:
    """One booking by its human reference (BK-1004), exact and case-insensitive.
    The concierge stagers used to scan search_bookings' soonest-50 for a ref —
    on a diary with two months of past rows that never reached next week."""
    ref = " ".join(str(ref or "").split()).upper()
    if not ref:
        return None
    try:
        res = ((client or db()).table("bookings").select("*").eq("tenant_id", tenant_id)
               .ilike("booking_ref", ref).limit(1).execute())
        return res.data[0] if res.data else None
    except Exception:
        log.warning("get_booking_by_ref failed (tenant=%s)", tenant_id, exc_info=True)
        return None


def booking_contacts_between(tenant_id: str, date_from: str, date_to: str,
                             client: Client | None = None) -> list[dict]:
    """(contact, booking_date) for every live booking in a date range — the
    concierge's dormancy read (2026-09-06). Uncapped by design (search_bookings
    stops at 50): a 'who hasn't rebooked' answer over a year of rows."""
    try:
        res = ((client or db()).table("bookings").select("contact,booking_date")
               .eq("tenant_id", tenant_id).in_("status", ["proposed", "confirmed"])
               .gte("booking_date", date_from).lte("booking_date", date_to)
               .order("booking_date", desc=False).limit(5000).execute())
        return res.data or []
    except Exception:
        log.warning("booking_contacts_between failed (tenant=%s)", tenant_id, exc_info=True)
        return []


def add_concierge_message(tenant_id: str, role: str, content: str, *,
                          user_id: str | None = None, links: list | None = None,
                          trail: list | None = None, origin: str = "unknown",
                          display_turn_id: str = "", native_events: dict | None = None,
                          client: Client | None = None) -> None:
    """Append one business-Q&A chat turn. BEST-EFFORT: persistence is a convenience
    (the page reloads with history), so a storage failure must never break an answer.
    `trail` is the assistant turn's ghost trail — [{name, label, detail}] of the
    lookups that produced the answer — so history replays with its pills."""
    try:
        (client or db()).table("concierge_messages").insert({
            "tenant_id": tenant_id,
            "user_id": user_id,
            "role": role,
            "content": content,
            "links": links or [],
            "trail": list(trail or []) + ([{"name":"message_origin", "origin":origin, "turn_id":display_turn_id[:40], "native_events":native_events or {}}] if origin in {"typed", "voice", "live"} else []),
        }).execute()
    except Exception:
        log.warning("concierge_messages insert failed (tenant=%s)", tenant_id, exc_info=True)
    else:
        from app import events
        events.publish(tenant_id, {'type':'concierge_message','user_id':user_id})


def save_native_transcript(rows: list[dict], *, client=None) -> None:
    """Scoped deterministic segment IDs; lost-response retries cannot append duplicates."""
    if rows:
        (client or db()).table("concierge_messages").upsert(rows, on_conflict="id").execute()
        from app import events
        for tenant in {r['tenant_id'] for r in rows}:
            events.publish(tenant, {'type':'concierge_message'})


def latest_concierge_assistant_since(tenant_id: str, since: str,
                                     client: Client | None = None) -> list[dict]:
    """The Concierge's own turns since `since`, newest first — Today looks for
    the morning brief among them (the brief strip, 2026-09-25)."""
    return (
        (client or db()).table("concierge_messages")
        .select("content,created_at")
        .eq("tenant_id", tenant_id).eq("role", "assistant")
        .gte("created_at", since)
        .order("created_at", desc=True).limit(20)
        .execute().data or []
    )


@instrument("history")
def list_concierge_messages(tenant_id: str, limit: int = 40, *,
                            user_id: str | None = None, include_shared: bool = True,
                            include_native: bool = True,
                            client: Client | None = None) -> list[dict]:
    """A person's recent Concierge transcript, oldest-first (render order).

    Per person (security review 2026-09, H6: the transcript was one log per
    business, so staff could read, wipe or add to the owner's). With a
    user_id: their own turns, plus the business-wide ones (no user_id — the
    morning brief, payment and quote notes) only when include_shared, which
    callers give to owner/manager. No user_id (tests, pre-People callers):
    the whole business, as before."""
    prefix = "[Voice transcript segment — provisional; playback unverified]%"
    observed = "[Live observed speech — provisional; playback unverified]%"
    def scoped():
        q = ((client or db()).table("concierge_messages")
             .select("id,role,content,links,trail,created_at,user_id")
             .eq("tenant_id", tenant_id))
        if user_id:
            q = (q.or_(f"user_id.eq.{user_id},user_id.is.null") if include_shared
                 else q.eq("user_id", user_id))
        return q
    # Supplementary transport observations never consume the rich-message page.
    # The three reads are independent, so they run side by side (one round trip
    # of waiting, not three).
    def rich():
        with span("history_rich"):
            return scoped().not_.like("content", prefix).not_.like("content", observed).order("created_at", desc=True).limit(limit).execute().data or []
    def native():
        with span("history_native"):
            return scoped().like("content", prefix).order("created_at", desc=True).limit(200).execute().data or []
    def observed_rows():
        with span("history_observed"):
            return scoped().like("content", observed).order("created_at", desc=True).limit(4096).execute().data or []
    from app import parallel_reads
    rows = []
    for part in parallel_reads.run_all([rich, native, observed_rows] if include_native else [rich]):
        rows += part
    unique = {row["id"]: dict(row) for row in rows}
    def chronology(row):
        try:
            stamp = datetime.fromisoformat(str(row.get("display_created_at") or row.get("created_at") or "").replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError):
            stamp = 0
        metadata = next((entry for entry in row.get("trail") or [] if isinstance(entry, dict) and entry.get("name") == "message_origin"), {})
        return stamp, row.get("display_ordinal", row.get("ordinal", metadata.get("ordinal", 0))), str(row.get("id") or "")
    result = sorted(unique.values(), key=chronology)
    for row in result:
        metadata = next((entry for entry in row.get("trail") or [] if isinstance(entry, dict) and entry.get("name") == "message_origin"), {})
        row["origin"] = metadata.get("origin") if metadata.get("origin") in {"typed", "voice", "live"} else "unknown"
        row["turn_id"] = metadata.get("turn_id", "")[:40]
        row["native_events"] = metadata.get("native_events") or {}
        row["native_session"] = metadata.get("session", "")
        row["native_event_id"] = metadata.get("event_id", "")
        row["ordinal"] = metadata.get("ordinal", 0)
        if metadata.get("provisional") and (row.get("content") or "").startswith(observed[:-1]):
            row["content"] = row["content"][len(observed)-1:].lstrip("\n")
            row["provisional"] = True
        row["trail"] = [entry for entry in row.get("trail") or []
                        if isinstance(entry, dict) and entry.get("name") not in ("message_origin", "intake_receipt")]
        if row["origin"] == "unknown" and (row.get("content") or "").startswith(prefix[:-1]):
            row["origin"] = "live"
            row["provisional"] = True
    # Dedup must retain when the exact observed question happened, rather than
    # relocating it to the later backend save time. This is display metadata only.
    inputs = {(row.get("native_session"), row.get("native_event_id")): row for row in result
              if row.get("provisional") and row.get("role") == "user" and row.get("native_session")}
    for row in result:
        links = row["native_events"]
        if row.get("role") != "user" or not links.get("event_ids"):
            continue
        anchors = [inputs.get((links.get("session"), event_id)) for event_id in links["event_ids"]]
        if anchors and all(anchor is not None for anchor in anchors):
            first = min(anchors, key=chronology)
            row["display_created_at"] = first["created_at"]
            row["display_ordinal"] = first["ordinal"]
    result.sort(key=chronology)
    represented = {(row["native_events"].get("session"), event_id) for row in result if row.get("role") == "user"
                   for event_id in row["native_events"].get("event_ids", [])}
    visible = [row for row in result if not (row.get("provisional") and row.get("role") == "user"
            and (row.get("native_session"), row.get("native_event_id")) in represented)]
    grouped = []
    for row in visible:
        observation = bool(row.get("provisional") and row.get("native_session"))
        previous = grouped[-1] if grouped else {}
        if (observation and previous.get("native_session") == row["native_session"] and previous.get("role") == row["role"]
                and len(previous.get("observation_ids", [])) < 256 and len(previous.get("content", "")) + len(row["content"]) <= 12000):
            previous["content"] += row["content"]
            previous["observation_ids"].append(row["id"])
        else:
            if observation:
                row["observation_ids"] = [row["id"]]
            grouped.append(row)
    return grouped



def list_ledger_intakes(tenant_id: str, user_id: str, *, days: int,
                        client: Client | None = None) -> list[dict]:
    """This person's replies that carry an intake receipt (several things asked in one
    message; src/app/concierge_intake.py), newest first, at most 20 — the work ledger's
    read. Nothing at all without a user id (never the whole business's chat)."""
    if not user_id:
        return []
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    return ((client or db()).table("concierge_messages").select("id,created_at,user_id,trail")
            .eq("tenant_id", tenant_id).eq("user_id", user_id).eq("role", "assistant")
            # jsonb containment: a JSON STRING, because the client joins a list as a Postgres array
            .gte("created_at", cutoff).contains("trail", '[{"name":"intake_receipt"}]')
            .order("created_at", desc=True).limit(20).execute().data or [])


def clear_concierge_messages(tenant_id: str, *, user_id: str | None = None,
                             include_shared: bool = True,
                             client: Client | None = None) -> None:
    """Start fresh: a person's own turns (and the business-wide ones for an
    owner/manager). No user_id: the whole business, as before."""
    q = (client or db()).table("concierge_messages").delete().eq("tenant_id", tenant_id)
    if user_id:
        q = (q.or_(f"user_id.eq.{user_id},user_id.is.null") if include_shared
             else q.eq("user_id", user_id))
    q.execute()


# ── Concierge notifications (the bubble's badge/glow + digest feed) ──────────
def add_concierge_notification(tenant_id: str, *, kind: str, title: str,
                               body: str = "", url: str = "",
                               contact: str | None = None,
                               contact_name: str | None = None,
                               client: Client | None = None) -> None:
    """One proactive signal for the tenant's feed — written wherever a push
    fires (src/app/push.py), push on or off. BEST-EFFORT by the same contract as
    push itself: a feed failure must never cost the event that triggered it.

    `contact` is who it is ABOUT (2026-09-04), lower-cased so it keys the same
    way the deck, the inbox and the CRM key people. Absent is a real answer —
    a notification about the business itself has no card to land on."""
    row = {
        "tenant_id": tenant_id,
        "kind": kind,
        "title": title[:200],
        "body": (body or "")[:500],
        "url": url or "",
    }
    who = {"contact": (contact or "").strip().lower()[:320] or None,
           "contact_name": (contact_name or "").strip()[:120] or None}
    who = {k: v for k, v in who.items() if v}
    try:
        try:
            (client or db()).table("concierge_notifications").insert(row | who).execute()
        except Exception:
            if not who:
                raise
            # The contact columns land with their migration (2026-09-04); until
            # then a feed row must still be written — without the "who".
            (client or db()).table("concierge_notifications").insert(row).execute()
    except Exception:
        log.warning("concierge_notifications insert failed (tenant=%s kind=%s)",
                    tenant_id, kind, exc_info=True)


def list_concierge_notifications(tenant_id: str, limit: int = 20,
                                 since: str | None = None,
                                 client: Client | None = None) -> list[dict]:
    """Newest-first feed for the digest card. `since` (ISO timestamp) is what
    the morning brief uses to pick up everything that landed while you were
    not being interrupted — held by a family set to "in the brief", or by
    quiet hours."""
    q = (
        (client or db()).table("concierge_notifications")
        .select("id,kind,title,body,url,created_at,seen_at")
        .eq("tenant_id", tenant_id)
    )
    if since:
        q = q.gte("created_at", since)
    return (
        q.order("created_at", desc=True)
        .limit(max(1, min(int(limit or 20), 200)))
        .execute().data or []
    )


def list_unbriefed_notifications(tenant_id: str, *, not_before: str,
                                 limit: int = 200, include_briefed: bool = False,
                                 client: Client | None = None) -> list[dict]:
    """Everything the morning brief has never mentioned — the held queue.

    Not a time window: a rolling `since` dropped anything held over a closed
    weekend, because the next brief's 24h lookback had already passed it. A row
    stays unbriefed until a brief actually mentions it, however many closed days
    that takes. `not_before` is only a floor against resurrection — a brief
    switched off for a month must not come back and read out August.
    `include_briefed` replays rows a brief already said (/rig/brief only)."""
    q = ((client or db()).table("concierge_notifications")
         # contact_name: the spoken brief names who it happened to — without it
         # every clause read "Someone" (live, rig 2026-09-25).
         .select("id,kind,title,body,url,created_at,seen_at,contact,contact_name")
         .eq("tenant_id", tenant_id))
    if not include_briefed:
        q = q.is_("briefed_at", "null")
    return (q.gte("created_at", not_before)
            .order("created_at", desc=True)
            .limit(max(1, min(int(limit or 200), 500)))
            .execute().data or [])


def list_deck_notifications(tenant_id: str, *, since: str, limit: int = 200,
                            client: Client | None = None) -> list[dict]:
    """What happened lately, for Today's deck — live rows only.

    Dismissed rows are gone from here for good; that is what the swipe means.
    Everything else the deck shows is derived state that recomputes itself, so
    this read is the only place Today can say "and here is what actually
    happened", which is why it carries created_at and seen_at through."""
    return (
        (client or db()).table("concierge_notifications")
        .select("id,kind,title,body,url,created_at,seen_at,contact,contact_name")
        .eq("tenant_id", tenant_id)
        .is_("dismissed_at", "null")
        .gte("created_at", since)
        .order("created_at", desc=True)
        .limit(max(1, min(int(limit or 200), 500)))
        .execute().data or []
    )


def resolve_concierge_notifications(tenant_id: str, *, kind: str, url: str,
                                    client: Client | None = None) -> int:
    """Settle every live feed row of one kind that points at one place — the
    system's own swipe. A draft's send resolves its "Draft ready" cards (live
    2026-09-04: Today kept "tap to review and approve" for two drafts already
    approved and sent). Returns how many rows it settled; best-effort."""
    if not kind or not url:
        return 0
    try:
        res = (
            (client or db()).table("concierge_notifications")
            .update({"dismissed_at": datetime.now(timezone.utc).isoformat()})
            .eq("tenant_id", tenant_id).eq("kind", kind).eq("url", url)
            .is_("dismissed_at", "null")
            .execute()
        )
        return len(res.data or [])
    except Exception:
        log.warning("resolve_concierge_notifications failed (tenant=%s kind=%s)",
                    tenant_id, kind, exc_info=True)
        return 0


def dismiss_concierge_notification(tenant_id: str, notification_id: str,
                                   client: Client | None = None) -> bool:
    """Swipe one away. Tenant-scoped in the filter as well as by RLS — the id
    comes off a page, so it is never trusted to belong to the caller."""
    res = (
        (client or db()).table("concierge_notifications")
        .update({"dismissed_at": datetime.now(timezone.utc).isoformat()})
        .eq("tenant_id", tenant_id).eq("id", notification_id)
        .is_("dismissed_at", "null")
        .execute()
    )
    return bool(res.data)


def mark_notifications_briefed(tenant_id: str, *, client: Client | None = None) -> None:
    """Close the held queue: every unbriefed row for this tenant is stamped,
    including the ones too old for the floor to have shown. Called only when a
    brief was actually SENT — a skipped or empty morning leaves the queue
    standing, which is the whole point.

    Stamped AFTER the send, not before: a crash here costs one repeated mention
    tomorrow, where the other order would cost the mention entirely."""
    (client or db()).table("concierge_notifications") \
        .update({"briefed_at": datetime.now(timezone.utc).isoformat()}) \
        .eq("tenant_id", tenant_id).is_("briefed_at", "null").execute()


def count_unseen_concierge_notifications(tenant_id: str,
                                         client: Client | None = None) -> int:
    res = (
        (client or db()).table("concierge_notifications")
        .select("id", count="exact")
        .eq("tenant_id", tenant_id)
        .is_("seen_at", "null")
        .limit(1)
        .execute()
    )
    return int(res.count or 0)


def mark_concierge_notifications_seen(tenant_id: str, ids: list[str] | None = None,
                                      client: Client | None = None) -> None:
    """Opening the panel acknowledges everything unseen — the glow's off switch.
    `ids` limits it to the rows a narrower member was shown."""
    q = (client or db()).table("concierge_notifications") \
        .update({"seen_at": datetime.now(timezone.utc).isoformat()}) \
        .eq("tenant_id", tenant_id).is_("seen_at", "null")
    if ids is not None:
        q = q.in_("id", list(ids))
    q.execute()


def get_concierge_notification(tenant_id: str, notification_id: str,
                               client: Client | None = None) -> dict | None:
    res = ((client or db()).table("concierge_notifications")
           .select("id,kind,title,seen_at").eq("tenant_id", tenant_id)
           .eq("id", notification_id).limit(1).execute())
    return res.data[0] if res.data else None


# ── Concierge business actions (Phase 2): proposals + audit ──────────────────
PENDING_CARD_WINDOW_S = 300   # mirrors concierge_actions.PROPOSAL_TTL_S (db cannot import it)


def _live_pending_rows(c, tenant_id: str, user_id: str | None = None, max_age_s: int = PENDING_CARD_WINDOW_S) -> list[dict]:
    """The owner's own waiting cards that can still be confirmed: pending, not a background
    task's, made inside the confirm window. Newest first. Any read failure answers [] (the
    callers then carry on exactly as before)."""
    try:
        res = (c.table("concierge_proposals").select("id,kind,summary,payload,created_at,user_id")
               .eq("tenant_id", tenant_id).eq("status", "pending")
               .is_("payload->_delegation", "null").order("created_at", desc=True).limit(5).execute())
    except Exception:
        return []
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    live = []
    for r in res.data or []:
        if user_id and r.get("user_id") not in (None, user_id):
            continue
        try:
            made = datetime.fromisoformat(str(r["created_at"]).replace("Z", "+00:00"))
        except Exception:
            continue
        if (now - made).total_seconds() <= max_age_s:
            live.append(r)
    return live


def get_live_pending_proposal(tenant_id: str, user_id: str | None = None,
                              client: Client | None = None) -> dict | None:
    """The one card the owner could confirm right now, or None."""
    rows = _live_pending_rows(client or db(), tenant_id, user_id)
    return rows[0] if rows else None


def create_concierge_proposal(tenant_id: str, kind: str, summary: str, payload: dict, *,
                              user_id: str | None = None, action: dict | None = None,
                              client: Client | None = None, park: bool = False,
                              task_binding: dict | None = None) -> str:
    """Stage one action proposal (status=pending) and return its id. Any other
    pending proposal for the tenant is SUPERSEDED first — one live confirmation
    card at a time keeps 'Confirm' unambiguous. `action` (the model's request
    as it was made) rides in the payload as _action so a card nobody answered
    can be rebuilt later against the day's data (list_waiting_proposals)."""
    c = client or db()
    if action:
        payload = {**payload, "_action": action}
    if task_binding:
        from app import concierge_delegation as delegation
        if not task_binding.get("scope") or delegation.access_scope(tenant_id, user_id)[0] != task_binding["scope"]:
            raise PermissionError("Task access changed during preparation")
        receipt = delegation.transition("stage", tenant_id, user_id,
            task_id=task_binding["task_id"], revision=task_binding["revision"],
            attempt_id=task_binding["attempt_id"], data={"kind": kind, "summary": summary, "payload": payload})
        if not receipt.get("ok"):
            raise delegation.Superseded("Task revision no longer current")
        return receipt["task"]["proposal_id"]
    if not park:
        # The same request again (a repeated ask, or a "yes" the model took for a new one): the card
        # already waiting is the card. A second identical one would sit beside it, the first marked
        # replaced, and two Confirm buttons for one thing.
        for r in _live_pending_rows(c, tenant_id, user_id):
            if r.get("kind") == kind and r.get("summary") == summary and r.get("payload") == payload:
                return r["id"]
    row = {"tenant_id": tenant_id, "user_id": user_id, "kind": kind,
           "summary": summary, "payload": payload}
    if park:
        # A background task's card (docs/plan-concierge-task-list.md): straight
        # into the waiting list, never replacing the card on the owner's screen.
        row["status"] = "superseded"
    else:
        c.table("concierge_proposals").update({"status": "superseded"}) \
            .eq("tenant_id", tenant_id).eq("status", "pending").is_("payload->_delegation", "null").execute()
    res = c.table("concierge_proposals").insert(row).execute()
    return res.data[0]["id"]


class ClaimRefused(Exception):
    """The caller's own rule (claim_concierge_proposal's refuse_if) said this PENDING
    proposal must not be claimed by this request. Nothing was written: it is still
    pending, so the right confirm can still claim it. .args[0] is the row it refused."""


def claim_concierge_proposal(proposal_id: str, tenant_id: str, *, max_age_s: int = 300,
                             client: Client | None = None, execution_input: dict | None = None,
                             confirmed_by: str | None = None, confirm_via: str | None = None,
                             refuse_if=None) -> dict | None:
    """Atomically claim a pending, unexpired proposal for execution
    (pending -> executing; single-use by construction — a second claim matches
    zero rows). Returns the claimed row, or None (used / expired / not yours).
    `refuse_if(row)` is asked about the row this function already reads, before any
    write, and only while it is pending: a True raises ClaimRefused and leaves the row
    as it was (a proposal in any other state falls through to the ordinary claim, which
    matches no row, so its real outcome is reported rather than a refusal)."""
    from datetime import datetime, timedelta, timezone
    import uuid
    now = datetime.now(timezone.utc).isoformat()
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=max_age_s)).isoformat()
    receipt = {"execution": {"version": 2, "id": str(uuid.uuid4()),
                              "operation_id": proposal_id, "state": "executing",
                              "started_at": now, "confirmed_by": confirmed_by,
                              "confirm_via": confirm_via,   # 'tap' | 'words' | 'unknown'; absent on older rows
                              "input": execution_input or {}}}
    from app import concierge_delegation as delegation
    proposal = get_concierge_proposal(proposal_id, tenant_id, client=client)
    if refuse_if and proposal and proposal.get("status") == "pending" and refuse_if(proposal):
        raise ClaimRefused(proposal)
    if proposal and delegation.binding(proposal):
        # Route has already applied current member/mailbox authorization.
        if not delegation.enabled():
            return None
        result = delegation.transition("approval_claim", tenant_id, proposal.get("user_id"),
            data={"proposal_id": proposal_id, "cutoff": cutoff, "receipt": receipt})
        return result.get("proposal") if result.get("ok") else None
    res = (
        (client or db()).table("concierge_proposals")
        .update({"status": "executing", "result": receipt, "executed_at": None})
        .eq("id", proposal_id)
        .eq("tenant_id", tenant_id)
        .eq("status", "pending")
        .gte("created_at", cutoff)
        .execute()
    )
    return res.data[0] if res.data else None


def get_concierge_proposal(proposal_id: str, tenant_id: str,
                           client: Client | None = None) -> dict | None:
    res = (
        (client or db()).table("concierge_proposals")
        .select("*").eq("id", proposal_id).eq("tenant_id", tenant_id)
        .limit(1).execute()
    )
    return res.data[0] if res.data else None


def get_concierge_proposals(proposal_ids: list[str], tenant_id: str,
                            client: Client | None = None) -> dict[str, dict]:
    """Several proposals in one read, keyed by id (a task list's cards)."""
    ids = sorted({i for i in proposal_ids if i})
    if not ids:
        return {}
    rows = ((client or db()).table("concierge_proposals").select("*")
            .eq("tenant_id", tenant_id).in_("id", ids).execute().data or [])
    return {row["id"]: row for row in rows}


def finish_concierge_proposal(proposal_id: str, tenant_id: str, status: str,
                              result: dict | None = None,
                              client: Client | None = None, *, execution_id: str | None = None) -> bool:
    """CAS a receipt owned by the original claimant; no stale terminal writes.

    `executed` is the legacy storage spelling for a proven completed receipt.
    A stale classifier may set unknown; only the original execution can finish.
    """
    fields = {"status": status, "result": result or {}}
    from app import concierge_delegation as delegation
    proposal = get_concierge_proposal(proposal_id, tenant_id, client=client)
    if proposal and delegation.binding(proposal):
        if not execution_id:
            return False
        saved = delegation.transition("approval_finish", tenant_id, proposal.get("user_id"),
            data={"proposal_id": proposal_id, "status": status, "receipt": result or {}, "execution_id": execution_id})
        return bool(saved.get("ok"))
    if status == "executed":
        fields["executed_at"] = datetime.now(timezone.utc).isoformat()
    q = (client or db()).table("concierge_proposals").update(fields) \
        .eq("id", proposal_id).eq("tenant_id", tenant_id)
    if execution_id:
        q = q.in_("status", ["executing", "unknown"]).eq("result->execution->>id", execution_id)
    elif status == "expired":
        q = q.eq("status", "pending")
    else:
        return False  # no execution owner, no authority to stamp its outcome
    return bool(q.execute().data)


def list_recovering_proposals(tenant_id: str, user_id: str | None, *, client=None) -> list[dict]:
    """Raw internal rows for recovery; never offer these for fresh staging."""
    def recent(statuses):
        q = ((client or db()).table("concierge_proposals").select("*")
             .eq("tenant_id", tenant_id).in_("status", statuses)
             .order("created_at", desc=True).limit(40))
        if user_id:
            q = q.eq("user_id", user_id)
        return q.execute().data or []
    from app import proposal_outcomes
    # New successes cannot push unresolved operations out of the recovery list.
    # Separately inspect the latest 40 terminal receipts for unsupported legacy.
    rows = recent(["executing", "unknown"]) + recent(["executed", "failed"])
    return [row for row in rows
            if proposal_outcomes.state(row) in {"executing", "unknown"}]


# ── The Concierge's task list (docs/plan-concierge-task-list.md) ───────────
TASK_OPEN = ("queued", "working")


def create_concierge_tasks(tenant_id: str, user_id: str | None, batch_id: str,
                           tasks: list[dict], *, surface: str = "typed",
                           client: Client | None = None) -> list[dict]:
    """One row per task, in the order given. Returns the rows."""
    rows = [{"tenant_id": tenant_id, "user_id": user_id, "batch_id": batch_id,
             "position": i, "title": t["title"], "instruction": t["instruction"],
             "surface": surface} for i, t in enumerate(tasks)]
    return (client or db()).table("concierge_tasks").insert(rows).execute().data or []


def update_concierge_task(task_id: str, fields: dict, client: Client | None = None) -> None:
    # Legacy workers/card helpers cannot bypass revision authority with the
    # service client. New rows are changed exclusively by the transition RPC.
    from app import concierge_delegation
    query = (client or db()).table("concierge_tasks").update(fields).eq("id", task_id)
    if concierge_delegation.enabled():
        query = query.eq("revision", 0)
    else:
        # Retained revised rows also remain protected during feature rollback.
        existing = (client or db()).table("concierge_tasks").select("*").eq("id", task_id).limit(1).execute().data or []
        if existing and existing[0].get("revision", 0) > 0:
            return
    query.execute()


def list_concierge_tasks(tenant_id: str, batch_ids: list[str],
                         client: Client | None = None) -> list[dict]:
    """The tasks of these batches, in order (the panel's task lists)."""
    if not batch_ids:
        return []
    return ((client or db()).table("concierge_tasks")
            .select("*")
            .eq("tenant_id", tenant_id).in_("batch_id", list(batch_ids))
            .order("position").execute().data or [])


def get_concierge_task(tenant_id, user_id, task_id, client=None):
    q = (client or db()).table("concierge_tasks").select("*").eq("tenant_id", tenant_id).eq("id", task_id)
    q = q.eq("user_id", user_id) if user_id else q.is_("user_id", "null")
    rows = q.limit(1).execute().data or []
    return rows[0] if rows else None


TASK_UNSETTLED = ("queued", "working", "waiting", "needs_you")
TASK_SETTLED = ("done", "failed", "cancelled", "declined")


def list_session_concierge_tasks(tenant_id, user_id, voice_session, client=None, *,
                                 recent_hours=None):
    """`recent_hours` (the typed path, which has no session to bound it): the
    unsettled tasks plus anything started within that many hours, rather than
    every typed task ever."""
    q = (client or db()).table("concierge_tasks").select("*").eq("tenant_id", tenant_id)
    q = q.eq("voice_session", voice_session) if voice_session else q.is_("voice_session", "null")
    q = q.eq("user_id", user_id) if user_id else q.is_("user_id", "null")
    if recent_hours is not None:
        from datetime import datetime, timedelta, timezone
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=recent_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        q = q.or_(f"status.in.({','.join(TASK_UNSETTLED)}),created_at.gte.{cutoff}")
    return q.order("created_at", desc=True).limit(40).execute().data or []


def list_ledger_concierge_tasks(tenant_id: str, user_id: str, *, days: int,
                                client: Client | None = None) -> list[dict]:
    """This person's own tasks (legacy and delegated) started in the last `days`
    days, newest first, at most 80 — the work ledger's read. Always filtered to
    the asker, and nothing at all without one (never the whole account); the
    ledger filters again on the rows it gets back."""
    if not user_id:
        return []
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    return ((client or db()).table("concierge_tasks").select("*")
            .eq("tenant_id", tenant_id).eq("user_id", user_id).gte("created_at", cutoff)
            .order("created_at", desc=True).limit(80).execute().data or [])


def concierge_tasks_for_proposal(tenant_id: str, proposal_id: str,
                                 client: Client | None = None) -> list[dict]:
    """The task(s) whose card this is — what the owner did with the card
    settles the task (app/concierge_tasks.settle_card)."""
    if not proposal_id:
        return []
    return ((client or db()).table("concierge_tasks")
            .select("*")
            .eq("tenant_id", tenant_id).eq("proposal_id", proposal_id)
            .execute().data or [])


def list_unfinished_concierge_tasks(*, hours: int = 24) -> list[dict]:
    """Every task left queued or working in the last day — a deploy or restart
    stopped its worker; start-up picks these up again. Service role."""
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    return (db().table("concierge_tasks")
            .select("*")
            .in_("status", list(TASK_OPEN)).gte("created_at", cutoff)
            .order("created_at").execute().data or [])


WAITING_DAYS = 14


def list_waiting_proposals(tenant_id: str, user_id: str | None, *, days: int = WAITING_DAYS,
                           max_age_s: int | None = None,
                           client: Client | None = None) -> list[dict]:
    """Cards set up for this person that were never answered — pushed aside by
    a newer card, or left until they expired (Kris 2026-09-23: "a pending chip
    ... can be resurfaced at any time until confirmation"). A card that was
    confirmed, cancelled ("no" is an answer), dismissed or brought back is
    not waiting; nor is one whose exact change has since been confirmed. Only
    cards that carry their original request (_action) can come back. Newest
    first, one per change, at most 5."""
    from datetime import datetime, timedelta, timezone
    # max_age_s: the owner's parked lifetime (their task-expiry setting); days stays the outer limit.
    age = timedelta(days=days) if max_age_s is None else timedelta(seconds=min(max_age_s, days * 86400))
    cutoff = (datetime.now(timezone.utc) - age).isoformat()
    q = ((client or db()).table("concierge_proposals")
         .select("id,kind,summary,status,created_at,payload,user_id")
         .eq("tenant_id", tenant_id)
         .in_("status", ["pending", "superseded", "executed", "executing", "unknown"])
         .gte("created_at", cutoff)
         .order("created_at", desc=True).limit(80))
    if user_id:
        q = q.eq("user_id", user_id)
    rows = q.execute().data or []
    done_at: dict[str, str] = {}
    for r in rows:
        if r.get("status") in {"executed", "executing", "unknown"}:
            s = r.get("summary") or ""
            done_at[s] = max(done_at.get(s, ""), r.get("created_at") or "")
    out, seen = [], set()
    for r in rows:
        s = r.get("summary") or ""
        if r.get("status") in {"executed", "executing", "unknown"} or s in seen:
            continue
        if not (r.get("payload") or {}).get("_action"):
            continue
        from app import concierge_actions as _ca     # sends, deletes, edits, bulk: asked for again, not kept
        if r.get("kind") in _ca.REPREPARE_NEVER:
            continue
        if done_at.get(s, "") > (r.get("created_at") or ""):
            continue
        seen.add(s)
        out.append({"proposal_id": r["id"], "kind": r.get("kind"), "summary": s,
                    "created_at": r.get("created_at"), "status": r.get("status"),
                    "delegated": bool((r.get("payload") or {}).get("_delegation"))})
        if len(out) >= 5:
            break
    return out


def list_waiting_drafts(tenant_id: str, *, days: int = WAITING_DAYS,
                        client: Client | None = None) -> list[dict]:
    """Emails the owner asked the voice Concierge for that are still waiting
    for approval (Kris 2026-09-23: a draft set aside by the next question
    "didn't go into pending" — it had, but nothing showed it). Newest first,
    at most 5, shaped like the voice draft card (concierge_draft)."""
    from datetime import datetime, timedelta, timezone
    from urllib.parse import quote
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    try:
        rows = ((client or db()).table("email_log")
                .select("id,from_address,contact_name,subject,draft_reply,created_at")
                .eq("tenant_id", tenant_id).eq("kind", "owner_message").eq("status", "pending")
                .gte("created_at", cutoff).order("created_at", desc=True).limit(5)
                .execute().data or [])
    except Exception:
        log.warning("waiting drafts read failed (tenant=%s)", tenant_id, exc_info=True)
        return []
    out = []
    for r in rows:
        contact = r.get("from_address") or ""
        name = r.get("contact_name")
        if not name and contact:
            try:   # the chip read "b***@gmail.com" (Kris 2026-09-23): the CRM knows who
                name = (crm_contact_by_email(tenant_id, contact, client=client) or {}).get("name")
            except Exception:
                name = None
        name = name or contact
        out.append({"type": "draft", "log_id": r["id"], "contact": contact, "name": name,
                    "subject": r.get("subject") or "", "text": r.get("draft_reply") or "",
                    "status": "pending", "created_at": r.get("created_at"),
                    "href": "/app/conversations/view?contact=" + quote(contact),
                    "summary": f"Email to {name}" + (f" — {r.get('subject')}" if r.get("subject") else "")})
    return out


def list_ledger_proposals(tenant_id: str, user_id: str, *, days: int,
                          client: Client | None = None) -> list[dict]:
    """Every card set up for this person in the last `days` days, whatever became
    of it, newest first, at most 80 — the work ledger's read. Nothing at all
    without a user id (never the whole account)."""
    if not user_id:
        return []
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    return ((client or db()).table("concierge_proposals")
            .select("id,kind,summary,status,created_at,executed_at,result,payload,user_id")
            .eq("tenant_id", tenant_id).eq("user_id", user_id).gte("created_at", cutoff)
            .order("created_at", desc=True).limit(80).execute().data or [])


DONE_HOURS = 24


def list_done_proposals(tenant_id: str, user_id: str | None, *, hours: int = DONE_HOURS,
                        client: Client | None = None) -> list[dict]:
    """What the Concierge has DONE for this person lately (Kris 2026-09-24: "a
    counter chip for done actions, so the user can see a list of what's
    happened — not a whole thread of chat, just done actions"). Executed
    cards with their outcome, newest first, at most 20."""
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    q = ((client or db()).table("concierge_proposals")
         .select("id,kind,summary,executed_at,created_at,result,user_id")
         .eq("tenant_id", tenant_id).eq("status", "executed")
         .gte("created_at", cutoff)
         .order("created_at", desc=True).limit(20))
    if user_id:
        q = q.eq("user_id", user_id)
    from app import proposal_outcomes
    out = []
    for r in q.execute().data or []:
        if proposal_outcomes.state({**r, "status": "executed"}) != "completed":
            continue
        result = r.get("result") or {}
        link = result.get("link") if isinstance(result.get("link"), dict) else None
        out.append({"type": "card", "proposal_id": r["id"], "kind": r.get("kind"),
                    "summary": r.get("summary") or "", "outcome": result.get("summary") or "",
                    "done_at": r.get("executed_at") or r.get("created_at"),
                    "href": (link or {}).get("href") or ""})
    return out


def list_sent_drafts(tenant_id: str, *, hours: int = DONE_HOURS,
                     client: Client | None = None) -> list[dict]:
    """Emails the owner asked the voice Concierge for that have gone — the
    done list's other half. Newest first, at most 20."""
    from datetime import datetime, timedelta, timezone
    from urllib.parse import quote
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    try:
        rows = ((client or db()).table("email_log")
                .select("id,from_address,contact_name,subject,sent_at,created_at")
                .eq("tenant_id", tenant_id).eq("kind", "owner_message").eq("status", "sent")
                .gte("created_at", cutoff).order("created_at", desc=True).limit(20)
                .execute().data or [])
    except Exception:
        log.warning("sent drafts read failed (tenant=%s)", tenant_id, exc_info=True)
        return []
    out = []
    for r in rows:
        contact = r.get("from_address") or ""
        name = r.get("contact_name")
        if not name and contact:
            try:
                name = (crm_contact_by_email(tenant_id, contact, client=client) or {}).get("name")
            except Exception:
                name = None
        name = name or contact
        out.append({"type": "sent", "log_id": r["id"], "kind": "email",
                    "summary": f"Email to {name}" + (f" — {r.get('subject')}" if r.get("subject") else ""),
                    "outcome": "Sent", "done_at": r.get("sent_at") or r.get("created_at"),
                    "href": "/app/conversations/view?contact=" + quote(contact)})
    return out


def set_concierge_proposal_status(proposal_id: str, tenant_id: str, status: str, *,
                                  from_statuses: tuple = ("pending", "superseded"),
                                  client: Client | None = None) -> bool:
    """Move an unanswered card to `status` (dismissed / resurfaced). Never
    touches one that was executed or cancelled."""
    from app import concierge_delegation
    row = get_concierge_proposal(proposal_id, tenant_id, client=client)
    if row and concierge_delegation.binding(row):
        return False
    res = ((client or db()).table("concierge_proposals")
           .update({"status": status})
           .eq("id", proposal_id).eq("tenant_id", tenant_id)
           .in_("status", list(from_statuses)).execute())
    return bool(res.data)


def stamp_concierge_decision(proposal_id: str, tenant_id: str, via: str, *,
                             by: str | None = None, reprepared_from: str | None = None) -> bool:
    """Record who settled a card and how (sign-off record, db/migrations/20261016_
    concierge_sign_off.sql). Service role, once only: a card already stamped keeps its first
    stamp. Best effort by design: the outcome is already durable, so a failure here (for
    example the migration not applied yet) is logged without content and never raised."""
    try:
        from datetime import datetime, timezone
        fields = {"decided_at": datetime.now(timezone.utc).isoformat(), "decided_by": by or None,
                  "decided_via": via if via in ("tap", "words", "unknown", "system") else "unknown"}
        res = (db().table("concierge_proposals").update(fields)
               .eq("id", proposal_id).eq("tenant_id", tenant_id).is_("decided_at", "null").execute())
        return bool(res.data)
    except Exception:
        log.warning("sign-off stamp not written (proposal %s)", proposal_id)
        return False


def link_reprepared(new_id: str, old_id: str, tenant_id: str) -> bool:
    """Mark a card as prepared again from an older one (reprepared_from). Service role,
    best effort: the new card works without the link."""
    try:
        res = (db().table("concierge_proposals").update({"reprepared_from": old_id})
               .eq("id", new_id).eq("tenant_id", tenant_id).execute())
        return bool(res.data)
    except Exception:
        log.warning("reprepared_from link not written (proposal %s)", new_id)
        return False


def cancel_concierge_proposal(proposal_id: str, tenant_id: str,
                              client: Client | None = None) -> bool:
    from app import concierge_delegation
    row = get_concierge_proposal(proposal_id, tenant_id, client=client)
    if row and concierge_delegation.binding(row):
        return False
    res = (
        (client or db()).table("concierge_proposals")
        .update({"status": "cancelled"})
        .eq("id", proposal_id).eq("tenant_id", tenant_id).eq("status", "pending")
        .execute()
    )
    return bool(res.data)


def move_booking_if_capacity(tenant_id: str, booking_id: str, new_date_iso: str,
                             new_window: str) -> dict | None:
    """Atomically move one live booking to a new date/window IF the target has
    capacity — the concierge's move action. Same advisory-locked capacity gate
    as hold_booking_if_capacity, but the row is UPDATED in place so its ref,
    details, deposits and calendar linkage survive. None = full / not movable.
    SERVICE ROLE (RPC), like the hold — tenant scoping is the argument."""
    res = db().rpc("move_booking_if_capacity", {
        "p_tenant_id": tenant_id,
        "p_booking_id": booking_id,
        "p_new_date": new_date_iso,
        "p_new_window": new_window,
    }).execute()
    return res.data[0] if res.data else None


# ── Web Push subscriptions (PWA notifications — src/app/push.py) ────────────────
def upsert_push_subscription(tenant_id: str, user_id: str, endpoint: str,
                             p256dh: str, auth: str) -> None:
    """Store (or refresh) one device's push subscription. Endpoint is the
    natural key — the same browser re-subscribing (or a different user signing
    in on the same device) takes the row over, so a device only ever gets the
    notifications of whoever is CURRENTLY signed in on it. SERVICE ROLE on
    purpose: that cross-user takeover is exactly what per-user RLS would block."""
    db().table("push_subscriptions").upsert({
        "tenant_id": tenant_id, "user_id": user_id, "endpoint": endpoint,
        "keys_p256dh": p256dh, "keys_auth": auth,
    }, on_conflict="endpoint").execute()


def delete_push_subscription(user_id: str, endpoint: str) -> None:
    """Drop one device's subscription — the browser told us it unsubscribed.
    Scoped to the calling user so nobody can delete another user's device."""
    db().table("push_subscriptions").delete() \
        .eq("endpoint", endpoint).eq("user_id", user_id).execute()


def list_push_subscriptions(tenant_id: str) -> list[dict]:
    """Every subscribed device for a tenant — the send fan-out (src/app/push.py).
    Devices of switched-off members are left out (security review M30): an
    ex-employee's lock screen must not keep showing the business's money."""
    res = (db().table("push_subscriptions")
           .select("id,endpoint,keys_p256dh,keys_auth,user_id")
           .eq("tenant_id", tenant_id).execute())
    rows = res.data or []
    try:
        off = {r["user_id"] for r in (db().table("tenant_members").select("user_id")
                                      .eq("tenant_id", tenant_id).eq("active", False)
                                      .execute().data or [])}
    except Exception:
        off = set()
    return [r for r in rows if r.get("user_id") not in off]


def delete_push_subscriptions_for_user(tenant_id: str, user_id: str) -> None:
    """A switched-off member's devices stop receiving the business's pushes."""
    (db().table("push_subscriptions").delete().eq("tenant_id", tenant_id)
     .eq("user_id", user_id).execute())


def remove_push_subscription_by_id(sub_id: str) -> None:
    """Cleanup after the push service said 404/410 — the device is gone."""
    try:
        db().table("push_subscriptions").delete().eq("id", sub_id).execute()
    except Exception:
        log.warning("push subscription cleanup failed (id=%s)", sub_id, exc_info=True)


def touch_push_subscription(sub_id: str) -> None:
    """Best-effort last_used_at stamp on a successful delivery."""
    try:
        db().table("push_subscriptions") \
            .update({"last_used_at": datetime.now(timezone.utc).isoformat()}) \
            .eq("id", sub_id).execute()
    except Exception:
        log.warning("push subscription touch failed (id=%s)", sub_id, exc_info=True)


# ── Voice CRM (founder tool — /admin/voice-crm, service-role only) ───────────
# vcrm_* rows have NO tenant_id (voice-crm-build-spec.md): this is Kris's own
# cold-call pipeline, not a tenant feature. RLS is on with no policies, so the
# service-role client here is the only door.
_VCRM_PROSPECT_FIELDS = ("business_name", "description", "location", "address",
                         "phone", "website", "maps_url", "lat", "lng",
                         "contact_name", "status", "next_action",
                         "next_action_date", "notes",
                         "kind", "role", "channel", "source", "email",
                         "company_number", "email_source", "email_verified", "soft_match")
_VCRM_OPTIONAL_FIELDS = ("kind", "role", "channel", "source", "email")   # partner pipeline; only written when the form carries them


def _vcrm_prospect_payload(fields: dict) -> dict:
    return {k: fields[k] for k in _VCRM_PROSPECT_FIELDS if k in fields}


def vcrm_list_prospects(status: str | None = None, kind: str | None = None) -> list[dict]:
    """Prospects sorted call-list-first: nearest next_action_date, then the
    most recently touched. kind = 'customer' | 'partner' narrows the list."""
    q = db().table("vcrm_prospects").select("*")
    if status:
        q = q.eq("status", status)
    if kind:
        q = q.eq("kind", kind)
    res = q.order("next_action_date", desc=False, nullsfirst=False) \
           .order("updated_at", desc=True).execute()
    return res.data or []


def vcrm_get_prospect(prospect_id: str) -> dict | None:
    res = db().table("vcrm_prospects").select("*").eq("id", prospect_id).limit(1).execute()
    return res.data[0] if res.data else None


def vcrm_find_prospect(maps_url: str | None = None,
                       business_name: str | None = None) -> dict | None:
    """The debrief dedupe: same resolved link, else same name case-insensitively
    — a repeat call attaches to the existing prospect instead of duplicating."""
    if maps_url:
        res = db().table("vcrm_prospects").select("*").eq("maps_url", maps_url) \
                  .limit(1).execute()
        if res.data:
            return res.data[0]
    if business_name:
        res = db().table("vcrm_prospects").select("*") \
                  .ilike("business_name", business_name.strip()).limit(1).execute()
        if res.data:
            return res.data[0]
    return None


def vcrm_create_prospect(fields: dict) -> dict:
    payload = _vcrm_prospect_payload(fields)
    res = db().table("vcrm_prospects").insert(payload).execute()
    return res.data[0]


def vcrm_update_prospect(prospect_id: str, fields: dict) -> None:
    payload = _vcrm_prospect_payload(fields)
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    db().table("vcrm_prospects").update(payload).eq("id", prospect_id).execute()


def vcrm_create_call(prospect_id: str, outcome: str | None, summary: str | None,
                     transcript: str | None, extraction_json: dict | None) -> dict:
    res = db().table("vcrm_calls").insert({
        "prospect_id": prospect_id, "outcome": outcome, "summary": summary,
        "transcript": transcript, "extraction_json": extraction_json,
    }).execute()
    return res.data[0]


def vcrm_list_calls(prospect_id: str) -> list[dict]:
    res = db().table("vcrm_calls").select("*").eq("prospect_id", prospect_id) \
              .order("called_at", desc=True).execute()
    return res.data or []


def vcrm_delete_prospect(prospect_id: str) -> None:
    """Hard delete — vcrm_calls cascades via its FK. Founder tool, founder's
    call; reached only from the explicit two-tap delete in the quick menu."""
    db().table("vcrm_prospects").delete().eq("id", prospect_id).execute()


# ── Dependants (docs/modules/module-requirement-groups.md): groups, instances, subjects ──
# Every read is best-effort: a tenant with no groups — or a database without
# the tables yet — behaves exactly as before (the zero-config principle).
def list_requirement_groups(tenant_id: str, client: Client | None = None, *, strict: bool = False) -> list[dict]:
    try:
        res = ((client or db()).table("requirement_groups").select("*")
               .eq("tenant_id", tenant_id).order("display_order").execute())
        return res.data or []
    except Exception:
        if strict:
            raise
        log.warning("requirement_groups read failed (tenant=%s)", tenant_id, exc_info=True)
        return []


def save_requirement_groups(tenant_id: str, groups: list[dict],
                            client: Client | None = None) -> list[dict]:
    """Replace the tenant's definitions with `groups` (already cleaned): rows
    with an id update in place (their instances survive), new ones insert,
    the rest delete. Returns the rows as stored."""
    c = client or db()
    now = datetime.now(timezone.utc).isoformat()
    existing = {g["id"]: g for g in list_requirement_groups(tenant_id, client=c)}
    keep: list[str] = []
    out: list[dict] = []
    for i, g in enumerate(groups):
        row = {"label": g["label"], "label_plural": g.get("label_plural") or "",
               "cardinality": g.get("cardinality") or "many", "fields": g.get("fields") or [],
               "display_order": i, "updated_at": now}
        gid = g.get("id") if g.get("id") in existing else None
        if gid:
            res = (c.table("requirement_groups").update(row)
                   .eq("id", gid).eq("tenant_id", tenant_id).execute())
            keep.append(gid)
            out.append((res.data or [{**existing[gid], **row}])[0])
        else:
            res = c.table("requirement_groups").insert({**row, "tenant_id": tenant_id}).execute()
            if res.data:
                keep.append(res.data[0]["id"])
                out.append(res.data[0])
    for gid in existing:
        if gid not in keep:
            c.table("requirement_groups").delete().eq("id", gid).eq("tenant_id", tenant_id).execute()
    return out


def list_requirement_instances(tenant_id: str, contact_id: str | None,
                               client: Client | None = None) -> list[dict]:
    """A contact's dependants (live ones), with the group's label on each row."""
    if not contact_id:
        return []
    try:
        res = ((client or db()).table("requirement_instances")
               .select("*, requirement_groups(label,label_plural)")
               .eq("tenant_id", tenant_id).eq("contact_id", contact_id)
               .is_("archived_at", "null").order("created_at").execute())
        rows = res.data or []
        for r in rows:
            g = r.pop("requirement_groups", None) or {}
            r["group_label"] = g.get("label")
            r["group_label_plural"] = g.get("label_plural")
        return rows
    except Exception:
        log.warning("requirement_instances read failed (tenant=%s)", tenant_id, exc_info=True)
        return []


def get_requirement_instance(instance_id: str, tenant_id: str,
                             client: Client | None = None) -> dict | None:
    try:
        res = ((client or db()).table("requirement_instances").select("*")
               .eq("id", instance_id).eq("tenant_id", tenant_id).limit(1).execute())
        return res.data[0] if res.data else None
    except Exception:
        return None


def upsert_requirement_instance(tenant_id: str, group_id: str, contact_id: str, name: str,
                                data: dict, field_meta: dict | None = None,
                                client: Client | None = None) -> dict | None:
    """Write an instance by (group, contact, name) — case-insensitive on the
    name, so 'murphy' and 'Murphy' are one subject."""
    c = client or db()
    now = datetime.now(timezone.utc).isoformat()
    try:
        res = (c.table("requirement_instances").select("id,name,data,field_meta")
               .eq("tenant_id", tenant_id).eq("group_id", group_id).eq("contact_id", contact_id)
               .ilike("name", name).is_("archived_at", "null").limit(1).execute())
        if res.data:
            row = res.data[0]
            upd = (c.table("requirement_instances")
                   .update({"data": data, "field_meta": field_meta or row.get("field_meta") or {},
                            "updated_at": now})
                   .eq("id", row["id"]).eq("tenant_id", tenant_id).execute())
            return (upd.data or [{**row, "data": data}])[0]
        ins = c.table("requirement_instances").insert({
            "tenant_id": tenant_id, "group_id": group_id, "contact_id": contact_id,
            "name": name, "data": data, "field_meta": field_meta or {}}).execute()
        return ins.data[0] if ins.data else None
    except Exception:
        log.warning("requirement_instance upsert failed (tenant=%s)", tenant_id, exc_info=True)
        return None


def archive_requirement_instance(instance_id: str, tenant_id: str,
                                 client: Client | None = None) -> None:
    ((client or db()).table("requirement_instances")
     .update({"archived_at": datetime.now(timezone.utc).isoformat()})
     .eq("id", instance_id).eq("tenant_id", tenant_id).execute())


def link_booking_subjects(tenant_id: str, booking_id: str, instance_ids: list[str],
                          client: Client | None = None) -> None:
    if not instance_ids:
        return
    c = client or db()
    try:
        for iid in instance_ids:
            try:
                c.table("booking_subjects").insert(
                    {"tenant_id": tenant_id, "booking_id": booking_id, "instance_id": iid}).execute()
            except Exception:
                pass    # already linked
    except Exception:
        log.warning("booking_subjects link failed (booking=%s)", booking_id, exc_info=True)


def unlink_booking_subject(tenant_id: str, booking_id: str, instance_id: str,
                           client: Client | None = None) -> None:
    ((client or db()).table("booking_subjects").delete()
     .eq("tenant_id", tenant_id).eq("booking_id", booking_id).eq("instance_id", instance_id).execute())


def subjects_for_bookings(tenant_id: str, booking_ids: list[str],
                          client: Client | None = None) -> dict[str, list[dict]]:
    """{booking_id: [instance rows]} for many bookings in one read."""
    if not booking_ids:
        return {}
    try:
        res = ((client or db()).table("booking_subjects")
               .select("booking_id, requirement_instances(id,name,data,group_id,contact_id)")
               .eq("tenant_id", tenant_id).in_("booking_id", booking_ids).execute())
        out: dict[str, list[dict]] = {}
        for r in res.data or []:
            inst = r.get("requirement_instances")
            if inst:
                out.setdefault(r["booking_id"], []).append(inst)
        return out
    except Exception:
        log.warning("booking_subjects read failed (tenant=%s)", tenant_id, exc_info=True)
        return {}


def tenants_with_requirement_groups(client: Client | None = None) -> list[str]:
    try:
        res = (client or db()).table("requirement_groups").select("tenant_id").execute()
        return sorted({r["tenant_id"] for r in (res.data or [])})
    except Exception:
        return []


def list_gated_instances(tenant_id: str, client: Client | None = None) -> list[dict]:
    """Every live instance of the tenant (the sweep filters by field in code —
    a tenant has at most a few hundred)."""
    try:
        res = ((client or db()).table("requirement_instances").select("*")
               .eq("tenant_id", tenant_id).is_("archived_at", "null").limit(2000).execute())
        return res.data or []
    except Exception:
        return []


def upcoming_bookings_by_instance(tenant_id: str, instance_ids: list[str], *, since: str,
                                  client: Client | None = None) -> dict[str, list[dict]]:
    """{instance_id: [live bookings on/after `since`]} via booking_subjects."""
    if not instance_ids:
        return {}
    try:
        res = ((client or db()).table("booking_subjects")
               .select("instance_id, bookings(id,booking_ref,booking_date,status,start_block,length_blocks)")
               .eq("tenant_id", tenant_id).in_("instance_id", instance_ids).execute())
        out: dict[str, list[dict]] = {}
        for r in res.data or []:
            b = r.get("bookings") or {}
            if b.get("status") in ("confirmed", "proposed", "offered") and (b.get("booking_date") or "") >= since:
                out.setdefault(r["instance_id"], []).append(b)
        return out
    except Exception:
        return {}


def tenants_with_expiring_contact_fields(client: Client | None = None) -> list[str]:
    """Tenants with at least one contact field that expires (slice 4)."""
    try:
        res = ((client or db()).table("crm_field_definitions").select("tenant_id")
               .eq("entity", "contact").eq("durability", "expires").execute())
        return sorted({r["tenant_id"] for r in (res.data or [])})
    except Exception:
        return []


def crm_contacts_with_data(tenant_id: str, client: Client | None = None) -> list[dict]:
    """Every contact with its custom_data — the certificate sweep filters in
    code (a tenant has at most a few thousand)."""
    try:
        res = ((client or db()).table("crm_contacts").select("id,name,email,custom_data")
               .eq("tenant_id", tenant_id).limit(5000).execute())
        return res.data or []
    except Exception:
        return []


def upcoming_bookings_by_email(tenant_id: str, *, since: str,
                               client: Client | None = None) -> dict[str, list[dict]]:
    """{contact email (lower): [live bookings on/after `since`]} for one tenant."""
    try:
        res = ((client or db()).table("bookings").select("id,booking_ref,booking_date,contact")
               .eq("tenant_id", tenant_id).in_("status", ["proposed", "confirmed"])
               .gte("booking_date", since).execute())
    except Exception:
        return {}
    out: dict[str, list[dict]] = {}
    for b in res.data or []:
        if b.get("contact"):
            out.setdefault(str(b["contact"]).lower(), []).append(b)
    return out


def notification_exists_today(tenant_id: str, kind: str, *, tag: str,
                              client: Client | None = None) -> bool:
    """The sweep's own de-dupe: one feed row per (kind, tag) per day. The tag
    rides in `body`'s tail? No — in `url`'s fragment, so the row stays a plain
    feed row the deck already renders."""
    try:
        since = datetime.now(timezone.utc).date().isoformat()
        res = ((client or db()).table("concierge_notifications").select("id")
               .eq("tenant_id", tenant_id).eq("kind", kind).gte("created_at", since)
               .like("url", f"%#{tag}").limit(1).execute())
        return bool(res.data)
    except Exception:
        return False


def knowledge_source_texts(tenant_id: str, client: Client | None = None) -> list[dict]:
    """[{source, text}] — the FIRST chunk of every company-wide source, for the
    coverage read (src/app/knowledge_coverage.py). Best-effort: [] on failure."""
    try:
        res = ((client or db()).table("knowledge_chunks").select("source,content,created_at")
               .eq("tenant_id", tenant_id).is_("mailbox_id", "null")
               .order("created_at").limit(400).execute())
        seen: dict[str, str] = {}
        for r in res.data or []:
            seen.setdefault(r["source"], (r.get("content") or "")[:800])
        return [{"source": k, "text": v} for k, v in seen.items()]
    except Exception:
        return []


# ── The contacts logbook's reads (Kris 2026-09-21) ───────────────────────────
def crm_latest_activities(tenant_id: str, limit: int = 400,
                          client: Client | None = None) -> list[dict]:
    """The tenant's newest activity rows, newest first — the contacts logbook
    keeps the first per contact as that person's last log line. One bounded
    query for the whole page, never one per row."""
    res = (
        (client or db()).table("crm_activities")
        .select("contact_id,activity_type,direction,subject,summary,occurred_at")
        .eq("tenant_id", tenant_id).order("occurred_at", desc=True)
        .limit(limit).execute()
    )
    return res.data or []


def list_requirement_instances_all(tenant_id: str, limit: int = 1000,
                                   client: Client | None = None) -> list[dict]:
    """Every live dependant on the book with its group's label — the logbook's
    one read for the subjects or assets under each name."""
    res = ((client or db()).table("requirement_instances")
           .select("id,contact_id,group_id,name,data,requirement_groups(label,label_plural)")
           .eq("tenant_id", tenant_id).is_("archived_at", "null")
           .order("created_at").limit(limit).execute())
    rows = res.data or []
    for r in rows:
        g = r.pop("requirement_groups", None) or {}
        r["group_label"] = g.get("label")
        r["group_label_plural"] = g.get("label_plural")
    return rows


def list_unlabelled_documents(tenant_id: str, limit: int = 200,
                              client: Client | None = None) -> list[dict]:
    """Files kept from emails and uploads that nobody has labelled yet — the
    Today deck's "a document to label" (Kris 2026-09-21). One bounded read."""
    try:
        res = ((client or db()).table("crm_artifacts").select(_DOC_COLS)
               .eq("tenant_id", tenant_id).in_("artifact_type", ["attachment", "upload"])
               .not_.is_("url_or_path", "null").is_("label", "null")
               .order("created_at", desc=True).limit(limit).execute())
        return res.data or []
    except Exception:
        log.warning("unlabelled documents read failed (tenant=%s)", tenant_id, exc_info=True)
        return []
