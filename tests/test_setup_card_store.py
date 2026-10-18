from types import SimpleNamespace
import pytest
from app import setup_card_store as store, setup_cards as cards


class Client:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response, error, []

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            if name == "execute":
                if self.error:
                    raise self.error
                return SimpleNamespace(data=self.response)
            return self
        return call


def test_history_always_scopes_tenant_page_and_exclusive_cursor():
    client = Client(response=[])
    assert store.history("owner", "diary_settings", before=90, client=client) == []
    assert ("eq", ("tenant_id", "owner"), {}) in client.calls
    assert ("eq", ("page", "diary_settings"), {}) in client.calls
    assert ("lt", ("revision", 90), {}) in client.calls
    assert ("limit", (50,), {}) in client.calls


def test_save_uses_one_rpc_and_requires_its_confirmed_receipt():
    card = {"revision": 2}
    client = Client(response=card)
    assert store.save("owner", "diary_settings", 1, card, expected_settings={"booking_revision": 3},
                      settings_patch={"service_types": []}, client=client) == card
    assert [call[0] for call in client.calls] == ["rpc", "execute"]
    args = client.calls[0][1][1]
    assert args["p_tenant"] == "owner" and args["p_revision"] == 1
    assert args["p_expected_settings"] == {"booking_revision": 3}
    with pytest.raises(RuntimeError, match="could not be confirmed"):
        store.save("owner", "diary_settings", 1, card, client=Client(response=None))


@pytest.mark.parametrize("message", ["BLAM8_SETUP_STALE", "BLAM8_SETTINGS_STALE"])
def test_stale_rpc_has_no_retry_or_fallback_write(message):
    client = Client(error=RuntimeError(message))
    with pytest.raises(cards.Stale):
        store.save("owner", "diary_settings", 1, {}, client=client)
    assert len(client.calls) == 2
