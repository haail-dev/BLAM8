"""What the platform can do for each named diary, from its answers alone.

Code-owned and table-driven (reports/diary-discovery-spec-2026-10-24.md §4.3).
Labels never decide anything: equivalent answers under different names get the
same state. A diary that needs something not built is noted with its reason,
never mapped to a nearby setting (diary audit §5).
"""
from __future__ import annotations

STATES = ('ready', 'from_records', 'noted', 'unavailable', 'undecided')
REASONS = {
    'records_view': 'It will show these dates from your records once that view exists.',
    'records_date_missing': 'The date it follows isn’t recorded yet.',
    'combined_resources': 'Booking several kinds of resource together isn’t supported yet.',
    'third_party_agreement': 'Agreement from someone outside your team isn’t supported yet.',
    'rule_repeats': 'Repeats that depend on something are noted for a scheduled plan.',
    'module_scheduling': 'Customer booking needs the diary booking module, which isn’t on for this account.',
    'module_crm': 'Dated to-dos need the records module, which isn’t on for this account.',
    'not_classified': 'This combination isn’t covered yet.',
    'undecided': 'Some answers are still to decide.',
}
RESOURCE_KINDS = ('people', 'places', 'equipment')


def classify(answers, modules):
    """`answers` maps a discovery question id to its answered value (None when open,
    unsure or deferred). Returns {state, kind, reasons}."""
    starter = answers.get('diary.starter')
    occupies = answers.get('diary.occupies') or []
    if starter is None:
        return dict(state='undecided', kind=None, reasons=['undecided'])
    if starter == 'from_records':
        reasons = ['records_view']
        if answers.get('diary.date_source') == 'not_recorded':
            reasons.append('records_date_missing')
        return dict(state='from_records', kind='records', reasons=reasons)
    reasons = []
    if len([k for k in occupies if k in RESOURCE_KINDS]) > 1:
        reasons.append('combined_resources')
    if answers.get('diary.agreement') == 'someone_else':
        reasons.append('third_party_agreement')
    if answers.get('diary.repeats') == 'depends':
        reasons.append('rule_repeats')
    tasks = occupies == ['nothing'] and answers.get('diary.time_shape') == 'due_by'
    if starter in ('customers', 'both') and 'scheduling' not in modules:
        return dict(state='unavailable', kind='diary', reasons=['module_scheduling'] + reasons)
    if tasks and 'crm' not in modules:
        return dict(state='unavailable', kind='tasks', reasons=['module_crm'] + reasons)
    if reasons:
        return dict(state='noted', kind='tasks' if tasks else 'diary', reasons=reasons)
    if tasks:
        return dict(state='ready', kind='tasks', reasons=[])
    if not occupies:
        return dict(state='undecided', kind=None, reasons=['undecided'])
    if starter in ('customers', 'we_arrange', 'both', 'internal') and occupies != ['nothing']:
        return dict(state='ready', kind='diary', reasons=[])
    return dict(state='noted', kind=None, reasons=['not_classified'])


def wording(reasons):
    return [REASONS[r] for r in reasons]
