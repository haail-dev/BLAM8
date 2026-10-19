from copy import deepcopy
import json
from pathlib import Path
from app import product_inventory, product_maintenance as maintenance, product_guides


def receipt():
    return json.loads((maintenance.ROOT/'docs/product-change-reviews.json').read_text(encoding='utf-8'))


def test_current_product_reviews_cover_every_capability_and_mapped_surface():
    assert maintenance.report()['errors']==[]
    assert set(product_guides.GUIDES)=={r['id'] for r in product_inventory.entries()}


def test_missing_stale_or_unexplained_surface_review_fails():
    original=receipt()
    for tamper in ('missing','stale','blank','evidence'):
        value=deepcopy(original);record=value['capabilities']['packages']
        if tamper=='missing':record['surfaces'].pop('agent:packages')
        elif tamper=='stale':record['signature']='old'
        elif tamper=='blank':record['surfaces']['agent:packages']['reason']='waived'
        else:record['surfaces']['agent:packages']['evidence']=['../missing']
        assert maintenance.validate_receipts(value),tamper


def test_new_unmapped_runtime_source_and_changed_definition_require_accounting(monkeypatch):
    assert maintenance.check_change(['src/app/concierge_routes.py','src/app/platform_kb_content.py'],receipt())==[]
    assert any('Map changed runtime' in e for e in maintenance.check_change(['src/app/new_capability.py'],receipt()))
    assert maintenance.check_change(['src/app/booking_policy.py','src/app/setup_handoff.py'],receipt())==[]
    monkeypatch.setitem(product_guides.GUIDES,'packages','Different package behaviour requires review.')
    assert any('Stale source review: packages' in e for e in maintenance.validate_receipts(receipt()))


def test_unreviewed_docs_are_reported_as_debt_not_silently_certified(tmp_path):
    docs=tmp_path/'docs';(docs/'archive').mkdir(parents=True)
    (docs/'old.md').write_text('Old claims',encoding='utf-8')
    (docs/'archive/fact.md').write_text('Historical fact',encoding='utf-8')
    (docs/'product-knowledge.md').write_text('Maintained reference',encoding='utf-8')
    states={r['path']:r['status'] for r in maintenance.document_inventory(tmp_path)}
    assert states=={'docs/old.md':'review_required','docs/archive/fact.md':'historical','docs/product-knowledge.md':'current'}


def test_ci_runs_the_product_gate_without_models_or_optional_bypass():
    workflow=(maintenance.ROOT/'.github/workflows/offline-tests.yml').read_text(encoding='utf-8')
    assert 'python scripts/ci/check_product_change.py --base' in workflow
    assert 'fetch-depth: 0' in workflow
    assert 'continue-on-error' not in workflow


def test_commercial_review_includes_comparisons_machine_briefing_and_specific_help():
    surfaces=maintenance.commercial_surfaces()
    assert {'/','/capabilities','/partners','/llms.txt','/vs/tradify',
            '/vs/jobber','/help/how-much-does-blam8-cost'} <= surfaces
    row=next(r for r in product_inventory.entries() if r['id']=='billing')
    assert surfaces <= maintenance.expected_surfaces(row)


def test_commission_only_template_is_bound_to_source_review(tmp_path):
    templates=tmp_path/'src/app/templates'
    templates.mkdir(parents=True)
    path=templates/'commission.html'
    path.write_text("{{ product_commission('starter') }}",encoding='utf-8')
    row={'implementation':[], 'documentation':[]}
    assert 'src/app/templates/commission.html' in maintenance.source_files(row,tmp_path)


def test_operator_job_has_separate_bound_evidence_without_public_capability():
    source = 'src/app/admin_outreach.py'
    assert maintenance.check_change([source], receipt()) == []
    assert not any(source in row['implementation'] for row in product_inventory.entries())
    changed = receipt()
    changed['operator_sources'][source]['signature'] = 'old'
    assert any('Stale operator' in e for e in maintenance.check_change([source], changed))
    changed['operator_sources'].clear()
    assert maintenance.validate_operator_reviews(changed)


def test_associated_presentation_sources_are_included_in_source_signatures():
    row = next(row for row in product_inventory.entries() if row['id'] == 'owner.assistant')
    assert {'src/app/templates/app/_concierge_panel.html',
            'src/app/templates/app/_concierge_history.html',
            'src/app/static/concierge_panel.js'} <= set(maintenance.source_files(row))
