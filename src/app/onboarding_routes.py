"""Grouped review over independent settings domains; no live engine activation."""
import json
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app import onboarding_contract as contract, onboarding_state as state_store
from app.auth import TenantCtx, require_onboarded, require_subscription
from app.security import require_csrf, require_csrf_header, rate_limit
from app.templating import templates

router = APIRouter(prefix='/app/setup', dependencies=[Depends(require_subscription),
    Depends(rate_limit('onboarding_review', limit=100, window_s=60))])


class Action(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: int = Field(strict=True, ge=0)
    operation: str = Field(max_length=30)
    data: dict


def require_manager(ctx):
    from app.help_investigation import Investigator
    if not Investigator(ctx).can_configure:
        raise HTTPException(403, 'Settings changes require a verified settings manager outside View as.')


def _group(context, identity):
    groups = contract.group_steps(context['modules'], context['cfg'], context['state'], context['groups'])
    found = next((g for g in groups if g['key'] == identity), None)
    if not found:
        raise HTTPException(404, 'This review group is unavailable for this account.')
    return groups, found


def questions_for(context, substep, scope_type, scope_id):
    state = context['state']
    specs = list(contract.QUESTIONS.values()) + list(state.get('routed_questions', {}).values())
    result = []
    for spec in specs:
        if spec['substep'] != substep['id'] or scope_type not in spec['scopes']:
            continue
        if spec['adapter']:
            page = spec['adapter'][0]
            if page == 'diary_settings' and 'scheduling' not in context['modules']:
                continue
            if page == 'crm_settings' and 'crm' not in context['modules']:
                continue
        key = state_store.key_for(spec['id'], scope_type, scope_id)
        answer = state['answers'].get(key)
        if not answer and not contract.need_selected(state, spec.get('need')):
            continue
        result.append(dict(spec, key=key, answer=answer, optional=bool(spec['adapter']),
                           is_open=not answer or answer['state'] == 'unanswered'))
    return result


def page_context(context, group, scope_type='organisation', scope_id='organisation'):
    state = context['state']
    if scope_type == 'activity' and scope_id not in state['scopes']:
        raise HTTPException(404, 'Activity scope not found.')
    if scope_type == 'record_group' and scope_id not in {str(g['id']) for g in context['groups']}:
        raise HTTPException(404, 'Record scope not found.')
    if scope_type == 'record':
        from app import db
        record = db.get_requirement_instance(scope_id, context['tenant_id'], client=context['client'])
        if not record or record.get('archived_at'):
            raise HTTPException(404, 'Record scope not found.')
    if scope_type not in ('organisation', 'activity', 'record_group', 'record') or (scope_type == 'organisation' and scope_id != 'organisation'):
        raise HTTPException(400, 'Choose a supported scope.')
    for sub in group['substeps']:
        sub['needs'] = [dict(spec, id=identity, selected=contract.need_selected(state, identity),
                            chosen=identity in state['needs'])
                        for identity, spec in contract.NEEDS.items() if spec['substep'] == sub['id']]
        sub['dependencies'] = state_store.unresolved_dependencies(state, sub['id'])
        sub['questions'] = questions_for(context, sub, scope_type, scope_id)
        if sub['decision'] in ('off', 'deferred', 'undecided'):
            # Retain saved answers; no new questioning for an unselected branch.
            sub['questions'] = [q for q in sub['questions'] if q['answer']]
    proposals = [p for p in state['proposals'].values() if any(
        a['substep'].startswith(group['key'] + '.') for key, a in state['answers'].items() if key == p['answer'])]
    for proposal in proposals:
        from app import settings_recommend
        proposal['settings_href'] = settings_recommend.PAGES[proposal['page']]['href']
        proposal['display_changes'] = [display_change(change) for change in proposal['changes']]
        for change, shown in zip(proposal['changes'], proposal['display_changes']):
            shown['receipt'] = proposal.get('line_receipts', {}).get(change['id'])
    candidates = state_store.knowledge_candidates(context['knowledge'])
    from app.booking_policy import compile_profile
    profile = compile_profile(context['cfg'])
    suggestion = None
    if group['key'] == 'records' and context['groups'] and 'records.relationships' not in state['labels']:
        plurals = [str(g.get('label_plural') or g.get('label') or '').strip() for g in context['groups']]
        if len(plurals) == 1 and plurals[0]:
            suggestion = 'Contacts and ' + plurals[0]
            if len(suggestion) > 80:
                suggestion = None
    return dict(review_state=state, review_group=group, review_limits=contract.LIMITS,
        earlier_description=context['config'].get('business_brief') or '',
        factual_needs=[dict(text=state_store.resolve_question(state, a['question'])['text'], scope=a['scope_type'])
                       for a in state['answers'].values() if a['state'] == 'answered'
                       and a['question'] in ('billing.grouping', 'activity.interval', 'response.urgent')],
        review_capabilities=contract.CAPABILITIES, scope_type=scope_type, scope_id=scope_id,
        review_scopes=state['scopes'], record_scopes=context['groups'], review_proposals=proposals,
        saved_services=context['cfg'].get('service_types', []), label_suggestion=suggestion,
        knowledge_candidates=[q for q in candidates if q['substep'].startswith(group['key'] + '.')],
        knowledge_unavailable=context['knowledge_unavailable'], import_states=state['imports'],
        booking_ready=profile.ready, booking_message=profile.message)


def display_change(change):
    def words(value):
        if value is None:
            return 'Not set'
        if type(value) is bool:
            return 'Yes' if value else 'No'
        return str(value)
    before, after = change.get('before') or {}, change.get('after') or {}
    keys = ['value'] if change['name'] == 'setting' else [
        key for key in ('name', 'minutes', 'price', 'timing', 'resource') if before.get(key) != after.get(key)]
    return dict(title=change['display'], values=[dict(label={'value': '', 'minutes': 'Minutes',
        'price': 'Price', 'timing': 'Timing', 'resource': 'Resource', 'name': 'Name'}[key],
        before=words(before.get(key)), after=words(after.get(key))) for key in keys])


@router.get('')
def index(ctx: TenantCtx = Depends(require_onboarded)):
    require_manager(ctx)
    context = state_store.load(ctx)
    groups = contract.group_steps(context['modules'], context['cfg'], context['state'], context['groups'])
    position = context['state'].get('position')
    target = next((g for g in groups if position and any(s['id'] == position for s in g['substeps'])), None)
    target = target or next((g for g in groups if not g['done']), groups[0])
    scope = context['state'].get('scope_position', {})
    # A deleted record must not strand resume. Its answer history remains saved.
    if scope.get('type') == 'record_group' and scope.get('id') not in {str(g['id']) for g in context['groups']}:
        scope = {}
    elif scope.get('type') == 'activity' and scope.get('id') not in context['state']['scopes']:
        scope = {}
    elif scope.get('type') == 'record':
        from app import db
        record = db.get_requirement_instance(scope['id'], ctx.tenant_id, client=ctx.client)
        if not record or record.get('archived_at'):
            scope = {}
    from urllib.parse import urlencode
    query = ('?' + urlencode({'scope_type': scope['type'], 'scope_id': scope['id']})) if scope else ''
    return RedirectResponse(target['href'] + query + ('#' + position if position else ''), status_code=303)


@router.get('/{group}')
def group_page(request: Request, group: str, scope_type: str = 'organisation', scope_id: str = 'organisation',
               ctx: TenantCtx = Depends(require_onboarded)):
    require_manager(ctx)
    context = state_store.load(ctx, knowledge=True)
    groups, found = _group(context, group)
    from app.dashboard import _ctx
    return templates.TemplateResponse(request, 'app/onboarding_group.html', _ctx('settings', ctx,
        review_groups=groups, review_error=None, review_saved=request.query_params.get('saved') == '1',
        **page_context(context, found, scope_type, scope_id)), headers={'Cache-Control': 'no-store'})


@router.post('/state', dependencies=[Depends(require_csrf_header)])
def mutate(body: Action, ctx: TenantCtx = Depends(require_onboarded)):
    require_manager(ctx)
    context = state_store.load(ctx, knowledge=body.operation == 'import_knowledge')
    return {'state': state_store.transact(ctx, context, body)}


@router.post('/action', dependencies=[Depends(require_csrf)])
async def form_action(request: Request, ctx: TenantCtx = Depends(require_onboarded)):
    require_manager(ctx)
    form = await request.form()
    group = form.get('group', 'diary')
    if group not in contract.GROUPS:
        raise HTTPException(400, 'Unknown group.')
    context = state_store.load(ctx, knowledge=form.get('operation') == 'import_knowledge')
    try:
        data = json.loads(form.get('data', '{}'))
        if form.get('operation') == 'answer':
            data['state'] = form.get('state', 'unanswered')
            spec = state_store.resolve_question(context['state'], data['question'])
            value = form.get('value', '')
            if spec['kind'] == 'boolean':
                if value not in ('true', 'false') and data['state'] == 'answered':
                    raise ValueError('Choose Yes or No explicitly.')
                value = value == 'true'
            # Numeric adapters validate decimal precision and range themselves.
            data['value'] = value
        elif form.get('operation') == 'label':
            data['text'] = form.get('text', '')
        elif form.get('operation') == 'activity':
            data['service_name'] = form.get('service_name', '')
        elif form.get('operation') == 'capability':
            data['decision'] = form.get('decision', 'undecided')
        elif form.get('operation') == 'need':
            if form.get('selected') not in ('true', 'false'):
                raise ValueError('Choose whether this topic is relevant.')
            data['selected'] = form.get('selected') == 'true'
        elif form.get('operation') == 'review':
            data['state'] = form.get('state', 'unreviewed')
        body = Action(revision=int(form['revision']), operation=form['operation'], data=data)
        after = state_store.transact(ctx, context, body)
    except (ValueError, ValidationError, KeyError):
        raise HTTPException(400, 'Check the review fields and reload before retrying.') from None
    # Explicit section resume and scope survive normal POST/redirect/reload.
    from urllib.parse import urlencode
    selected = after.get('scope_position') if body.operation == 'activity' else None
    query = urlencode({'saved': '1', 'scope_type': selected['type'] if selected else form.get('scope_type', 'organisation'),
                       'scope_id': selected['id'] if selected else form.get('scope_id', 'organisation')})
    anchor = after.get('position') or ''
    return RedirectResponse('/app/setup/' + group + '?' + query + ('#' + anchor if anchor else ''), status_code=303)
