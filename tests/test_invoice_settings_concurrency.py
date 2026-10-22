import html
import json
import re
import pytest
from app import dashboard, db
from tests.test_invoicing_module import client


def snapshot():
    return {'invoice': {'extras': {'retained': 'yes'}, 'payment_terms': 'On receipt'}, 'quote_auto_draft': 'propose'}


def test_page_carries_raw_snapshot_and_save_keeps_unrelated_extras(client, monkeypatch):
    expected = snapshot()
    monkeypatch.setattr(db, 'get_invoice_settings', lambda *a, **kw: expected['invoice'])
    monkeypatch.setattr(db, 'get_tenant_config', lambda *a, **kw: {'quote_auto_draft': 'propose'})
    saved = []
    monkeypatch.setattr(db, 'save_invoice_settings_checked', lambda tid, version, fields, mode, **kw: saved.append((version, fields, mode)))
    monkeypatch.setattr(dashboard, '_held_used', lambda *a: None)
    page = client.get('/app/invoices/settings')
    raw = html.unescape(re.search(r'name="invoice_snapshot" value="([^"]*)"', page.text)[1])
    assert json.loads(raw) == expected
    response = client.post('/app/invoices/settings', data={'invoice_snapshot': raw,
        'payment_terms': 'On receipt', 'quote_auto_draft': 'off'}, follow_redirects=False)
    assert response.status_code == 303
    assert saved[0][0] == expected and saved[0][1]['extras']['retained'] == 'yes' and saved[0][2] == 'off'


@pytest.mark.parametrize('raw', [None, '', '{}', '[]', '{bad', '{"invoice":[],"quote_auto_draft":"propose"}'])
def test_unversioned_or_malformed_forms_do_not_write(client, monkeypatch, raw):
    def forbidden(*a, **kw): raise AssertionError('Must not write')
    monkeypatch.setattr(db, 'save_invoice_settings_checked', forbidden)
    response = client.post('/app/invoices/settings', data={} if raw is None else {'invoice_snapshot': raw})
    assert response.status_code == 409


@pytest.mark.parametrize('error,status,location', [
    (db.InvoiceSettingsStale('stale'), 303, '/app/invoices/settings?err=stale'),
    (RuntimeError('lost response'), 503, None)])
def test_conflict_or_uncertain_save_never_retries_or_marks_completion(client, monkeypatch, error, status, location):
    calls = []
    def fail(*a, **kw): calls.append(a); raise error
    monkeypatch.setattr(db, 'save_invoice_settings_checked', fail)
    monkeypatch.setattr(dashboard, '_held_used', lambda *a: pytest.fail('No completion after failed save'))
    response = client.post('/app/invoices/settings', data={'invoice_snapshot': json.dumps(snapshot()), 'quote_auto_draft': 'auto'},
        follow_redirects=False)
    assert response.status_code == status and len(calls) == 1
    assert response.headers.get('location') == location


STALE_MESSAGE = ('These settings changed after this form opened. The page now shows the latest saved values; '
                 'check them and save again.')


def test_stale_save_message_is_shown_on_the_page_and_only_for_that_error(client, monkeypatch):
    monkeypatch.setattr(db, 'get_invoice_settings', lambda *a, **kw: snapshot()['invoice'])
    monkeypatch.setattr(db, 'get_tenant_config', lambda *a, **kw: {'quote_auto_draft': 'propose'})
    stale = client.get('/app/invoices/settings?err=stale')
    assert stale.status_code == 200 and STALE_MESSAGE in stale.text
    assert STALE_MESSAGE not in client.get('/app/invoices/settings').text
    assert STALE_MESSAGE not in client.get('/app/invoices/settings?err=other').text
