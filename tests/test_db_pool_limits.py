from app import db


def test_supabase_sockets_are_kept_for_two_minutes_not_httpx_five_seconds():
    pool = db._http1_options().httpx_client._transport._pool
    assert pool._keepalive_expiry == 120.0
    assert pool._max_keepalive_connections == 32
