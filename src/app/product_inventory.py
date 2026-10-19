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
    _entry('email', 'Email drafts and approval', 'src/app/pipeline.py src/app/agent.py src/app/owner_goals.py', 'tests/test_concierge_booking_claims.py', 'README.md docs/product-knowledge.md', '/ /capabilities /how-it-works'),
    _entry('knowledge', 'Business knowledge', 'src/app/knowledge.py src/app/knowledge_forms.py', 'tests/test_knowledge_edit_in_place.py', 'README.md docs/product-knowledge.md', '/capabilities /help'),
    _entry('scheduling', 'Services, capacity and booking rules', 'src/app/scheduling.py src/app/diary_config.py src/app/dashboard.py src/app/templates/app/diary_settings.html src/app/templates/app/diary_intake.html src/app/templates/app/_scheduling_settings.html', 'tests/test_booking_capacity_database.py tests/test_diary_settings_save.py tests/test_diary_split.py tests/test_setup_card_mode.py tests/test_setup_card_routes.py tests/test_setup_cards_database.py', 'docs/verified-behaviours.md docs/product-knowledge.md', '/capabilities /how-it-works'),
    _entry('calendar', 'Calendar connection and sync', 'src/app/calendar_sync.py', 'tests/test_concierge.py', 'README.md docs/product-knowledge.md', '/capabilities /help'),
    _entry('crm', 'Customer records and follow-ups', 'src/app/crm.py src/app/crm_fields.py', 'tests/test_crm.py', 'docs/verified-behaviours.md docs/product-knowledge.md', '/capabilities /help'),
    _entry('subjects', 'Separately tracked subjects and evidence', 'src/app/intake.py src/app/dependants_extract.py', 'tests/test_ladders.py', 'docs/verified-behaviours.md docs/product-knowledge.md', '/capabilities'),
    _entry('packages', 'Packages and credits', 'src/app/setup_card_packages.py', 'tests/test_setup_card_packages.py', 'docs/verified-behaviours.md docs/product-knowledge.md', '/capabilities / /help'),
    _entry('progress', 'Progress levels and prerequisites', 'src/app/ladders.py', 'tests/test_ladders.py', 'docs/verified-behaviours.md docs/product-knowledge.md', '/capabilities / /help'),
    _entry('money', 'Quotes, invoices and payment records', 'src/app/invoice_send.py src/app/quote_send.py src/app/payments.py src/app/quote_public.py src/app/dashboard.py src/app/concierge_actions.py', 'tests/test_portal_billing.py tests/test_quotes.py tests/test_quotes_phase2.py', 'docs/verified-behaviours.md docs/product-knowledge.md', '/capabilities /stripe'),
    _entry('people', 'People and permissions', 'src/app/people.py src/app/people_routes.py src/app/job_people.py src/app/templates/app/people_settings.html', 'tests/test_pages_index.py tests/test_people.py tests/test_job_people.py', 'docs/verified-behaviours.md docs/product-knowledge.md', '/capabilities /help'),
    _entry('portal', 'Customer portal and job sheets', 'src/app/portal.py src/app/job_sheet_public.py', 'tests/test_pages_index.py', 'README.md docs/product-knowledge.md', '/for-customers /capabilities'),
    _entry('mailboxes', 'Mailbox connections and overrides', 'src/app/mailbox.py src/app/oauth.py src/app/backlog.py src/app/db.py src/app/gmail.py src/app/graph.py src/app/dashboard.py src/app/onboarding.py src/app/templates/_backlog_status.html src/app/templates/onboarding/mailbox.html', 'tests/test_pages_index.py tests/test_mailbox_import_choice.py tests/test_backlog_status_database.py', 'README.md docs/operations/mailbox-import-choice.md docs/product-knowledge.md', '/help /how-it-works'),
    _entry('owner.assistant', 'Owner assistance, voice and confirmed actions', 'src/app/concierge_data.py src/app/concierge_actions.py src/app/concierge_tasks.py', 'tests/test_concierge_reads.py tests/test_concierge_confirm_first.py', 'docs/verified-behaviours.md docs/product-knowledge.md', '/capabilities /help'),
    _entry('product.help', 'Product help and setup proposals', 'src/app/concierge.py src/app/onboarding_journey.py src/app/settings_guide.py src/app/settings_recommend.py src/app/static/onboarding_journey.js src/app/setup_catalogue.py src/app/setup_card_details.py src/app/setup_cards.py src/app/static/concierge.js src/app/templates/_concierge.html src/app/templates/onboarding/base.html src/app/templates/app/settings.html src/app/help_investigation.py', 'tests/test_setup_catalogue.py tests/test_settings_recommend.py tests/test_setup_card_model.py tests/test_run10_batch.py tests/test_onboarding_journey.py tests/test_help_investigation.py', 'docs/product-knowledge.md docs/onboarding-journey.md docs/onboarding-goals-context.md docs/operations/help-investigation.md', '/help /pages'),
    _entry('billing', 'Plans and billing', 'src/app/plans.py src/app/billing.py src/app/usage.py', 'tests/test_plans_trial_banner.py', 'README.md docs/product-knowledge.md', '/stripe /#pricing /help'),
    _entry('installable', 'Installable app and notifications', 'src/app/push.py src/app/main.py src/app/static/manifest.json src/app/static/concierge-manifest.json src/app/templates/base.html src/app/templates/app/base.html', 'tests/test_public_seo_pages.py', 'README.md docs/product-knowledge.md', '/capabilities'),
    _entry('extensions', 'Restricted operational modules', 'src/app/operating_systems.py', 'tests/test_pages_index.py', 'docs/verified-behaviours.md docs/product-knowledge.md', '/capabilities /partners', 'restricted'),
    _entry('automation.scheduled', 'Scheduled plans', 'src/app/scheduled_plan.py src/app/scheduled_plan_routes.py src/app/templates/app/scheduled_plan.html', 'tests/test_scheduled_plan.py', 'docs/verified-behaviours.md docs/product-knowledge.md', '/pages', 'restricted'),
    _entry('outreach', 'Tenant outreach', 'src/app/tenant_outreach.py src/app/tenant_outreach_routes.py src/app/templates/app/outreach.html', 'tests/test_tenant_outreach.py', 'docs/verified-behaviours.md docs/product-knowledge.md', '/pages', 'restricted'),
    _entry('knowledge.review', 'Knowledge admission review', 'src/app/knowledge_guidance.py src/app/static/knowledge_guidance.js src/app/knowledge_capture.py src/app/knowledge_fidelity.py src/app/templates/_knowledge_source_coverage.html src/app/templates/_knowledge_coverage_review.html src/app/knowledge_review.py src/app/onboarding.py src/app/kb_website.py src/app/knowledge_source_evidence.py src/app/knowledge_source_review.py src/app/knowledge_source_routes.py src/app/jobs/purge_email_log.py src/app/static/knowledge_source_review.js src/app/static/knowledge_onboarding_review.js src/app/templates/_knowledge_review.html src/app/templates/knowledge_review.html src/app/templates/onboarding/knowledge.html', 'tests/test_knowledge_guidance.py tests/replays/knowledge_guidance.mjs tests/test_knowledge_capture.py tests/test_knowledge_fidelity.py tests/test_knowledge_review_workspace.py tests/test_knowledge_source_ui.py tests/test_knowledge_review.py tests/test_knowledge_review_routes.py tests/test_knowledge_source_review.py tests/test_knowledge_source_routes.py tests/test_knowledge_source_database.py', 'docs/architecture/knowledge-concierge-guidance.md docs/verified-behaviours.md docs/operations/knowledge-source-review.md docs/product-knowledge.md', '/pages', 'restricted'),
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
