"""Today overview route and rendered-template regression checks."""
from pathlib import Path

import pytest

from app.templating import templates
from tests.test_deck_namesakes import _crm, _promise, client  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def rendered(client, monkeypatch):
    names = ['Tom', 'Pat', 'Priya', 'Sam', 'Alex', 'Morgan', 'Chris', 'Kim', 'Lee', 'Jo']
    contacts = [dict(id=f'c{i}', name=name, email=f'person{i}@example.invalid', crm_companies=None)
                for i, name in enumerate(names)]
    _crm(monkeypatch, contacts, [_promise(f'p{i}', f'c{i}', 'Confirm the appointment details')
                                for i in range(len(names))])
    response = client.get('/app/')
    assert response.status_code == 200
    return response


def test_overview_is_separate_from_cards_and_preserves_navigation(rendered):
    cards, overview = rendered.text.split('aria-label="Business overview"')
    assert 'data-tour="emails"' not in cards
    assert 'aria-controls="today-overview"' in overview
    assert 'data-tour="emails"' in overview and 'data-tour="crm"' in overview
    assert 'data-concierge-row' in overview
    assert 'href="/app/crm"' in overview.split('aria-label="Primary"')[1]
