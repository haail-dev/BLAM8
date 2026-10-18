"""Read-only product discovery, adapted from the executors' own catalogues.

No tenant IDs, records, industry recipes, database calls or write authority here.
Availability is descriptive; every real executor still enforces its late gates.
"""
from dataclasses import dataclass
from hashlib import sha256
import json
from app import product_inventory, setup_catalogue, people, plans


@dataclass(frozen=True)
class Context:
    active: frozenset = frozenset()
    preview: frozenset = frozenset()
    member: dict | None = None


# These are discovery associations, not replacements for route/executor gates.
_POLICY = {
    'email': ('inbox', None), 'knowledge': ('settings', None),
    'scheduling': ('diary', 'scheduling'), 'calendar': ('diary', 'scheduling'),
    'crm': ('crm', 'crm'), 'subjects': ('crm', 'crm'),
    'packages': ('crm', 'crm'), 'progress': ('crm', 'crm'),
    'money': ('money', None), 'people': ('settings', None),
    'portal': ('settings', None), 'mailboxes': ('settings', None),
    'owner.assistant': ('today', 'concierge'), 'product.help': ('today', None),
    'billing': ('settings', None), 'installable': ('today', None),
    'extensions': ('settings', None), 'automation.scheduled': ('settings', None),
    'outreach': ('settings', None), 'knowledge.review': ('settings', None),
}


def availability(entry, context=None):
    door, module = _POLICY[entry['id']]
    if context is None: return 'context_required'
    if not people.can_open(context.member, door): return 'permission_required'
    if entry['status'] == 'disabled': return 'disabled'
    if entry['status'] == 'restricted': return 'restricted'
    if module and module not in context.active:
        return 'preview_only' if module in context.preview else 'module_required'
    return 'available_subject_to_configuration'


def catalogue(context=None):
    from app import concierge_data, concierge_actions
    visible = (context.active | context.preview) if context else frozenset()
    full = 'crm' in visible
    member = context.member if context else None
    definitions = setup_catalogue.entries()
    rows = []
    for entry in product_inventory.entries():
        door, module = _POLICY[entry['id']]
        rows.append({**entry, 'availability':availability(entry,context),
                     'module':module, 'door':door, 'can_explain':True,
                     'can_execute':False,
                     'limits':'Discovery does not grant access or perform work. Real routes validate configuration, permissions and confirmation.',
                     'plans':[p['key'] for p in plans.CATALOG if p.get('public')
                              and (module is None or module in plans.BASE_PLAN_MODULES.get(p['key'],set()))]})
    reads = concierge_data.tools_for(full, member) if context else []
    # Reuse both the module allowlist and the exact per-action permission helper.
    actions = sorted(k for k in concierge_actions.allowed_kinds(full, 'scheduling' in visible)
                     if context and 'concierge' in context.active and concierge_actions.may_do(member,k))
    result = {'capabilities':rows, 'setup':definitions,
              'reads':[{'id':t['name'],'description':t.get('description','')} for t in reads],
              'actions':[{'id':k,'confirmation_required':True,'execute_here':False} for k in actions],
              'can_propose_settings':bool(context and people.can_do(member,'settings','change'))}
    # Hash definitions, not tenant values, names, permissions or private records.
    result['definition_version'] = sha256(json.dumps({'inventory':product_inventory.entries(),
        'setup':definitions, 'reads':concierge_data.TOOLS,
        'actions':sorted(concierge_actions.allowed_kinds(True,True)), 'policy':_POLICY,
        'plans':plans.CATALOG, 'plan_modules':{k:sorted(v) for k,v in plans.BASE_PLAN_MODULES.items()}},sort_keys=True).encode()).hexdigest()
    return result
