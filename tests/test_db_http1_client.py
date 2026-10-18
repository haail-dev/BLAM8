"""The Supabase clients run over HTTP/1.1 (live 2026-09-04: the Today fan-out
shared postgrest's HTTP/2 session across threads and 500'd two loads in
three with ConnectionTerminated error_code:9)."""
import httpx

from app import db


def _is_http2(client: httpx.Client) -> bool:
    return bool(getattr(client._transport._pool, "_http2", False))


def test_the_shared_options_carry_an_http1_client():
    opts = db._http1_options()
    assert isinstance(opts.httpx_client, httpx.Client)
    assert not _is_http2(opts.httpx_client)


def test_a_plain_httpx_client_would_be_http2_when_asked(monkeypatch):
    # the control: the pin above only means something if http2=True reads True
    c = httpx.Client(http2=True)
    assert _is_http2(c)
    c.close()
