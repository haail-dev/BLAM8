"""Offline documentation impact accounting. Review receipts are author evidence,
not a claim of human approval, deployment or successful model answers.
"""
from hashlib import sha256
import json
from pathlib import Path
from app import product_inventory, product_guides

ROOT = Path(__file__).resolve().parents[2]
SHARED = ('src/app/product_inventory.py','src/app/product_maintenance.py','src/app/platform_capabilities.py','src/app/product_guides.py',
          'src/app/platform_content.py','src/app/platform_articles.json','src/app/platform_help.py',
          'src/app/platform_public.py','src/app/platform_facts.py','src/app/help_center.py','src/app/platform_kb_content.py',
          'src/app/concierge_routes.py',
          'src/app/main.py','src/app/pages_index.py','src/app/templating.py','src/app/setup_catalogue.py','src/app/setup_rows.py',
          'src/app/people.py','src/app/config.py')
CURRENT_DOCS = {'README.md','docs/product-knowledge.md','docs/verified-behaviours.md'}
# Internal tools need review evidence without becoming public capabilities.
OPERATOR_SOURCES = {'src/app/admin_outreach.py': (
    'tests/test_admin_outreach.py', 'tests/test_admin_outreach_sql.py',
    'docs/operations/founder-outreach.md',
    'db/migrations/20261018_admin_outreach_egress.sql',
    'db/migrations/20261018_admin_outreach_egress_rollback.sql')}
# Additional runtime sources associated with feature evidence but excluded from
# the product catalogue so review metadata cannot change its serialized content.
SOURCE_CAPABILITIES = {
    'src/app/booking_policy.py': 'scheduling',
    'src/app/setup_handoff.py': 'scheduling',
    'src/app/static/concierge_panel.js': 'owner.assistant',
    'src/app/static/concierge_sidebar.js': 'owner.assistant',
    'src/app/static/concierge_discovery.css': 'owner.assistant',
    'src/app/templates/app/_concierge_panel.html': 'owner.assistant',
    'src/app/templates/app/_concierge_history.html': 'owner.assistant',
    'src/app/static/concierge_busy.css': 'product.help',
}


def operator_signature(source, root=ROOT):
    files = (source, *OPERATOR_SOURCES[source])
    return sha256(json.dumps({name: (root/name).read_text(encoding='utf-8-sig').replace('\r\n','\n')
                             for name in files}, sort_keys=True).encode()).hexdigest()


def validate_operator_reviews(receipt, root=ROOT):
    records = receipt.get('operator_sources', {})
    errors = []
    if set(records) != set(OPERATOR_SOURCES):
        errors.append('Operator source review coverage changed.')
    for source in OPERATOR_SOURCES:
        record = records.get(source, {})
        if record.get('signature') != operator_signature(source, root):
            errors.append('Stale operator source review: ' + source)
        if len(record.get('reason', '').strip()) < 30:
            errors.append('Explain the operator source disposition: ' + source)
    return errors


def source_files(row, root=ROOT):
    files=set(row['implementation']+row['documentation'])
    files.update(name for name, capability in SOURCE_CAPABILITIES.items()
                 if capability == row.get('id') and (root/name).is_file())
    files.update(p for p in SHARED if (root/p).is_file())
    files.update(p.relative_to(root).as_posix() for p in (root/'src/app').glob('setup_card_*.py'))
    # These templates consume shared values; they must be reviewed when their
    # commercial claims change, including visible text and structured data.
    files.update(p.relative_to(root).as_posix() for p in (root/'src/app/templates').glob('*.html')
                 if any(token in p.read_text(encoding='utf-8') for token in
                        ('product_price','product_value','product_plans','product_commission',
                         'public_capabilities','_product_pricing.html')))
    return sorted(files)


def signature(row, root=ROOT):
    sources={name:sha256((root/name).read_text(encoding='utf-8-sig').replace('\r\n','\n').encode()).hexdigest()
             for name in source_files(row,root)}
    return sha256(json.dumps({'capability':row,'guide':product_guides.GUIDES[row['id']],
                             'sources':sources},sort_keys=True).encode()).hexdigest()


def commercial_surfaces(root=ROOT):
    """Derive affected public URLs from existing routes and shared-value users."""
    import ast
    source=(root/'src/app/main.py').read_text(encoding='utf-8')
    result=set()
    for node in ast.parse(source).body:
        if not isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)):continue
        paths=[d.args[0].value for d in node.decorator_list if isinstance(d,ast.Call)
               and isinstance(d.func,ast.Attribute) and d.func.attr=='get' and d.args
               and isinstance(d.args[0],ast.Constant)]
        if not paths:continue
        uses='platform_public.' in (ast.get_source_segment(source,node) or '')
        for call in ast.walk(node):
            if isinstance(call,ast.Call) and isinstance(call.func,ast.Attribute) and call.func.attr=='TemplateResponse':
                for arg in call.args:
                    if not isinstance(arg,ast.Constant) or not isinstance(arg.value,str):continue
                    template=root/'src/app/templates'/arg.value
                    if template.is_file():
                        uses=uses or any(token in template.read_text(encoding='utf-8') for token in
                                         ('product_price','product_value','product_plans','product_commission','public_capabilities','_product_pricing.html'))
        if uses:result.update(p for p in paths if '{' not in p)
    for article in json.loads((root/'src/app/platform_articles.json').read_text(encoding='utf-8')):
        if article['kind']=='help' and any('{{product:' in p for p in article['paragraphs']):
            result.add('/help/'+article['slug'])
    return result


def expected_surfaces(row, root=ROOT):
    surfaces=set(row['documentation']+row['surfaces']+['agent:'+row['id']])
    return surfaces | commercial_surfaces(root) if row['id']=='billing' else surfaces


def validate_receipts(receipt, root=ROOT):
    errors=[]
    if receipt.get('format')!=1:return ['Unsupported product review receipt format.']
    records=receipt.get('capabilities',{})
    expected={r['id'] for r in product_inventory.entries()}
    if set(records)!=expected:errors.append('Capability review coverage changed; add or retire its specific review.')
    for row in product_inventory.entries():
        record=records.get(row['id'],{})
        if row['id'] not in product_guides.GUIDES:
            errors.append('Missing authored guide: '+row['id'])
            continue
        if record.get('signature')!=signature(row,root):errors.append('Stale source review: '+row['id'])
        surfaces=record.get('surfaces',{})
        if set(surfaces)!=expected_surfaces(row,root):errors.append('Missing or obsolete surface review: '+row['id'])
        for name,review in surfaces.items():
            if review.get('disposition') not in {'updated','generated','reviewed_unchanged'} or len(review.get('reason','').strip())<30:
                errors.append('Explain the specific surface disposition: '+row['id']+': '+name)
            evidence=review.get('evidence',[])
            if not evidence or any(not (root/p).resolve().is_relative_to(root.resolve()) or not (root/p).is_file() for p in evidence):
                errors.append('Missing surface review evidence: '+row['id']+': '+name)
    return errors


def check_change(paths, receipt, root=ROOT):
    errors=validate_receipts(receipt,root) + validate_operator_reviews(receipt,root)
    owned={name for row in product_inventory.entries() for name in source_files(row,root)}
    owned.update(SOURCE_CAPABILITIES)
    for name in paths:
        if name.startswith('src/app/') and name.endswith(('.py','.html','.json')) and name not in owned and name not in OPERATOR_SOURCES:
            errors.append('Map changed runtime source to a capability: '+name)
    return errors


def document_inventory(root=ROOT):
    rows=[]
    for path in sorted((root/'docs').rglob('*.md')):
        name=path.relative_to(root).as_posix()
        head=path.read_text(encoding='utf-8-sig')[:1000]
        status=('current' if name in CURRENT_DOCS else 'historical' if '/archive/' in name or head.startswith('> Historical reference.')
                else 'design' if 'SCOPE — DRAFT' in head else 'review_required')
        rows.append({'path':name,'status':status})
    return rows


def report(root=ROOT):
    receipt=json.loads((root/'docs/product-change-reviews.json').read_text(encoding='utf-8'))
    errors=product_inventory.validate(root=root)
    if set(product_guides.GUIDES)!={r['id'] for r in product_inventory.entries()}:
        errors.append('Authored guide coverage differs from the capability inventory.')
    errors.extend(validate_receipts(receipt,root))
    errors.extend(validate_operator_reviews(receipt,root))
    docs=document_inventory(root)
    return {'capabilities':len(product_inventory.entries()),'errors':errors,
            'documents':{status:sum(d['status']==status for d in docs)
                         for status in ('current','historical','design','review_required')}}
