"""The shared TLS trust store (src/app/httpx_ssl.py).

Building an ssl.SSLContext means parsing the whole CA bundle — ~340ms, which
was essentially the entire cost of constructing a Supabase client, paid by
whoever loaded the first page on a new JWT. The context is shared instead.

These tests exist because the shim sits on a SECURITY path: the danger is not
that it is slow, it is that it hands back a context someone else has since
mutated, or that it quietly stops verifying anything.

Run: python -m pytest tests/test_httpx_ssl_cache.py -q
"""
import ssl

import httpx
import httpx._config
import httpx._transports.default

from app import httpx_ssl


def test_the_default_context_is_built_once_and_shared():
    a = httpx_ssl._cached_create_ssl_context()
    b = httpx_ssl._cached_create_ssl_context()
    assert a is b
    assert isinstance(a, ssl.SSLContext)


def test_the_shared_context_still_verifies_certificates():
    """A trust store that trusts everything would be worse than a slow one."""
    ctx = httpx_ssl._cached_create_ssl_context()
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True
    assert ctx.get_ca_certs(), "the shared context has no CA certificates loaded"


def test_verify_false_is_never_served_from_the_cache():
    """An unverified client must not be able to hand its context to everyone."""
    insecure = httpx_ssl._cached_create_ssl_context(verify=False)
    assert insecure is not httpx_ssl._cached_create_ssl_context()
    assert insecure.verify_mode == ssl.CERT_NONE


def test_a_client_certificate_never_touches_the_shared_context():
    """create_ssl_context MUTATES the context it returns when cert= is given
    (load_cert_chain). Sharing that would leak one client's key material into
    every other client in the process — so cert= always builds its own."""
    calls = []
    original = httpx_ssl._original

    def _spy(verify=True, cert=None, trust_env=True):
        calls.append((verify, cert, trust_env))
        return original(verify=verify, cert=cert, trust_env=trust_env)

    httpx_ssl._original = _spy
    try:
        shared = httpx_ssl._cached_create_ssl_context()
        calls.clear()
        try:
            httpx_ssl._cached_create_ssl_context(cert="/nonexistent/client.pem")
        except Exception:
            pass                      # loading a fake cert fails; the DELEGATION is the point
        assert calls, "cert= was served from the cache instead of building its own context"
        assert shared is httpx_ssl._cached_create_ssl_context()   # shared one unharmed
    finally:
        httpx_ssl._original = original


def test_a_caller_supplied_context_is_returned_untouched():
    mine = ssl.create_default_context()
    assert httpx_ssl._cached_create_ssl_context(verify=mine) is mine


def test_the_shim_is_bound_everywhere_httpx_reads_it():
    """httpx's transports do `from .._config import create_ssl_context`, so the
    name is bound in their module at import time — patching _config alone
    silently does nothing at all."""
    assert httpx._config.create_ssl_context is httpx_ssl._cached_create_ssl_context
    assert (httpx._transports.default.create_ssl_context
            is httpx_ssl._cached_create_ssl_context)


def test_install_is_idempotent():
    httpx_ssl.install()
    httpx_ssl.install()
    assert httpx._config.create_ssl_context is httpx_ssl._cached_create_ssl_context


def test_a_real_client_gets_the_shared_context():
    """The end of the chain: an ordinary httpx.Client now reuses the store."""
    c = httpx.Client()
    try:
        transport = c._transport
        assert transport._pool._ssl_context is httpx_ssl._cached_create_ssl_context()
    finally:
        c.close()
