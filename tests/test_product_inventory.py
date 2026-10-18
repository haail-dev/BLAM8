from copy import deepcopy
from app import product_inventory


def test_inventory_has_implementation_tests_documentation_and_public_associations():
    assert product_inventory.validate() == []
    rows = product_inventory.entries()
    assert {'packages','progress','knowledge.review','outreach','automation.scheduled'} <= {r['id'] for r in rows}
    rows[0]['implementation'].clear()
    assert product_inventory.entries()[0]['implementation']


def test_inventory_rejects_missing_evidence_duplicates_and_invented_release_status():
    rows = product_inventory.entries()
    rows.append(deepcopy(rows[0]))
    rows[0]['implementation'] = ['../outside.py']
    rows[0]['status'] = 'live-because-code-exists'
    errors = product_inventory.validate(rows)
    assert any('Duplicate' in e for e in errors)
    assert any('classification' in e for e in errors)
    assert any('evidence' in e for e in errors)
