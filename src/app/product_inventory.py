"""Evidence map for product descriptions; implementation is not a release claim.

This map owns editorial associations, never settings schemas or permission rules.
The runtime catalogue adapts those from their existing owners.
"""
from copy import deepcopy
from pathlib import Path


def _entry(identity, title, implementation, tests, documentation, surfaces, status='implemented'):
    return dict(id=identity, title=title, implementation=implementation.split(),
                tests=tests.split(), documentation=list(dict.fromkeys(documentation.split()+['docs/product-knowledge.md'])),
                surfaces=surfaces.split(), status=status)


_ENTRIES = [
    _entry('email', 'Email drafts and approval', 'src/app/pipeline.py src/app/agent.py', 'tests/test_concierge_booking_claims.py', 'README.md', '/ /capabilities /how-it-works'),
    _entry('knowledge', 'Business knowledge', 'src/app/knowledge.py src/app/knowledge_forms.py', 'tests/test_knowledge_edit_in_place.py', 'README.md', '/capabilities /help'),
    _entry('scheduling', 'Services, capacity and booking rules', 'src/app/scheduling.py src/app/diary_config.py src/app/templates/app/_scheduling_settings.html', 'tests/test_booking_capacity_database.py', 'docs/verified-behaviours.md', '/capabilities /how-it-works'),
    _entry('calendar', 'Calendar connection and sync', 'src/app/calendar_sync.py', 'tests/test_concierge.py', 'README.md', '/capabilities /help'),
    _entry('crm', 'Customer records and follow-ups', 'src/app/crm.py src/app/crm_fields.py', 'tests/test_crm.py', 'docs/verified-behaviours.md', '/capabilities /help'),
    _entry('subjects', 'Separately tracked subjects and evidence', 'src/app/intake.py src/app/dependants_extract.py', 'tests/test_ladders.py', 'docs/verified-behaviours.md', '/capabilities'),
    _entry('packages', 'Packages and credits', 'src/app/setup_card_packages.py', 'tests/test_setup_card_packages.py', 'docs/verified-behaviours.md', '/capabilities / /help'),
    _entry('progress', 'Progress levels and prerequisites', 'src/app/ladders.py', 'tests/test_ladders.py', 'docs/verified-behaviours.md', '/capabilities / /help'),
    _entry('money', 'Quotes, invoices and payment records', 'src/app/invoice_send.py src/app/quote_send.py src/app/payments.py', 'tests/test_portal_billing.py', 'docs/verified-behaviours.md', '/capabilities /stripe'),
    _entry('people', 'People and permissions', 'src/app/people.py src/app/job_people.py', 'tests/test_pages_index.py', 'docs/verified-behaviours.md', '/capabilities /help'),
    _entry('portal', 'Customer portal and job sheets', 'src/app/portal.py src/app/job_sheet_public.py', 'tests/test_pages_index.py', 'README.md', '/for-customers /capabilities'),
    _entry('mailboxes', 'Mailbox connections and overrides', 'src/app/mailbox.py src/app/oauth.py src/app/backlog.py src/app/db.py src/app/gmail.py src/app/graph.py src/app/dashboard.py src/app/onboarding.py src/app/templates/_backlog_status.html src/app/templates/onboarding/mailbox.html', 'tests/test_pages_index.py tests/test_mailbox_import_choice.py tests/test_backlog_status_database.py', 'README.md docs/operations/mailbox-import-choice.md', '/help /how-it-works'),
    _entry('owner.assistant', 'Owner assistance, voice and confirmed actions', 'src/app/concierge_data.py src/app/concierge_actions.py src/app/concierge_tasks.py', 'tests/test_concierge_reads.py tests/test_concierge_confirm_first.py', 'docs/verified-behaviours.md', '/capabilities /help'),
    _entry('product.help', 'Product help and setup proposals', 'src/app/concierge.py src/app/setup_catalogue.py src/app/setup_cards.py src/app/static/concierge.js src/app/templates/_concierge.html src/app/templates/onboarding/base.html', 'tests/test_setup_catalogue.py', 'docs/product-knowledge.md', '/help /pages'),
    _entry('billing', 'Plans and billing', 'src/app/plans.py src/app/billing.py src/app/usage.py', 'tests/test_plans_trial_banner.py', 'README.md', '/stripe /#pricing /help'),
    _entry('installable', 'Installable app and notifications', 'src/app/push.py src/app/main.py', 'tests/test_public_seo_pages.py', 'README.md', '/capabilities'),
    _entry('extensions', 'Restricted operational modules', 'src/app/operating_systems.py', 'tests/test_pages_index.py', 'docs/verified-behaviours.md', '/capabilities /partners', 'restricted'),
    _entry('automation.scheduled', 'Scheduled plans', 'src/app/scheduled_plan.py src/app/scheduled_plan_routes.py src/app/templates/app/scheduled_plan.html', 'tests/test_scheduled_plan.py', 'docs/verified-behaviours.md', '/pages', 'restricted'),
    _entry('outreach', 'Tenant outreach', 'src/app/tenant_outreach.py src/app/tenant_outreach_routes.py src/app/templates/app/outreach.html', 'tests/test_tenant_outreach.py', 'docs/verified-behaviours.md', '/pages', 'restricted'),
    _entry('knowledge.review', 'Knowledge admission review', 'src/app/knowledge_review.py src/app/onboarding.py src/app/kb_website.py src/app/knowledge_source_evidence.py src/app/knowledge_source_review.py src/app/knowledge_source_routes.py src/app/jobs/purge_email_log.py src/app/static/knowledge_source_review.js src/app/static/knowledge_onboarding_review.js src/app/templates/_knowledge_review.html src/app/templates/knowledge_review.html src/app/templates/onboarding/knowledge.html', 'tests/test_knowledge_review.py tests/test_knowledge_review_routes.py tests/test_knowledge_source_review.py tests/test_knowledge_source_routes.py tests/test_knowledge_source_database.py', 'docs/verified-behaviours.md docs/operations/knowledge-source-review.md', '/pages', 'restricted'),
]


def entries():
    return deepcopy(_ENTRIES)


def validate(items=None, root=None):
    root = Path(root) if root else Path(__file__).resolve().parents[2]
    items = entries() if items is None else items
    errors, seen = [], set()
    for item in items:
        identity = item['id']
        if identity in seen: errors.append(f'Duplicate capability: {identity}')
        seen.add(identity)
        if item['status'] not in {'implemented', 'restricted', 'disabled'}:
            errors.append(f'Unknown release classification: {identity}')
        for field in ('implementation', 'tests', 'documentation', 'surfaces'):
            if not item.get(field): errors.append(f'Missing {field}: {identity}')
        for field in ('implementation', 'tests', 'documentation'):
            for name in item[field]:
                path = (root / name).resolve()
                if not path.is_relative_to(root.resolve()) or not path.is_file():
                    errors.append(f'Missing {field} evidence: {identity}: {name}')
    return errors
