"""One shared TLS trust store for every outbound httpx client in the process.

httpx builds a fresh `ssl.SSLContext` for each client it creates, and building
one means reading and parsing the whole certifi CA bundle: **~340ms**, measured,
which is essentially the entire cost of constructing a Supabase client (profile
2026-09-01 — `load_verify_locations` was 336ms of a 341ms `create_client`). The
dashboard builds one per signed-in JWT, so a user whose cached client had
expired paid that before their page could read its first row. That is what was
left of "slow the first time" (Kris 2026-09-01) after the round-trip work.

An SSLContext is designed to be shared — passing a pre-built one is httpx's own
documented way to avoid this cost, and `create_ssl_context` returns such a
context untouched. So we memoise exactly the default case and let every other
call fall through to the real thing:

  * `verify=True, cert=None`  → the shared context (what every client here uses)
  * a caller-supplied context, `verify=False`, or any `cert=`  → untouched,
    because those either mean something different or MUTATE the context
    (`load_cert_chain`), and a mutated shared context would leak that client's
    key material into every other client in the process.

The cache key carries the environment httpx itself reads (SSL_CERT_FILE /
SSL_CERT_DIR under trust_env), so a process configured to trust a different
store still gets that store, not a stale one.

Importing this module installs the shim. It is idempotent.
"""
import os

import httpx._config as _httpx_config
import httpx._transports.default as _httpx_default

# httpx's transports do `from .._config import create_ssl_context`, so the name
# is bound in their module at import time — patching _config alone changes
# nothing. Every module that holds a reference has to be rebound.
_BINDINGS = (_httpx_config, _httpx_default)

_original = _httpx_config.create_ssl_context
_contexts: dict = {}


def _cached_create_ssl_context(verify=True, cert=None, trust_env=True):
    # Anything but the plain default is either a context the caller already
    # built, or one this function is about to mutate — never shared.
    if verify is not True or cert is not None:
        return _original(verify=verify, cert=cert, trust_env=trust_env)
    key = (trust_env,
           os.environ.get("SSL_CERT_FILE") if trust_env else None,
           os.environ.get("SSL_CERT_DIR") if trust_env else None)
    ctx = _contexts.get(key)
    if ctx is None:
        # A duplicate build under a race is harmless — both are equivalent, and
        # the last one wins for everyone after it.
        ctx = _contexts[key] = _original(verify=True, cert=None, trust_env=trust_env)
    return ctx


def install() -> None:
    """Point httpx at the shared trust store. Safe to call more than once."""
    for mod in _BINDINGS:
        if getattr(mod, "create_ssl_context", None) is not _cached_create_ssl_context:
            mod.create_ssl_context = _cached_create_ssl_context


def warm() -> None:
    """Build the shared context now, so the first request of the process does
    not pay for it. Best-effort: a failure here just means the first client
    builds its own, exactly as before."""
    try:
        _cached_create_ssl_context()
    except Exception:      # pragma: no cover - a broken trust store fails later, loudly
        pass


install()
