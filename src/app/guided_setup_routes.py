"""Permission checked guide metadata and conversation on existing card adapters."""
from copy import deepcopy
from uuid import uuid4
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field
from app import guided_setup as guide, onboarding_state as state_store, onboarding_contract as contract
from app import setup_cards, setup_card_store, setup_card_routes, setup_card_model, db
from app.auth import TenantCtx, require_onboarded, require_subscription
from app.onboarding_routes import require_manager
from app.security import require_csrf_header, rate_limit
from app.templating import templates

router = APIRouter(prefix='/app/setup/guide', dependencies=[Depends(require_subscription),
    Depends(rate_limit('guided_setup', limit=60, window_s=60))])


class Action(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: int = Field(strict=True, ge=0)
    stop: str = Field(max_length=120)
    operation: str = Field(max_length=30)
    choice: str | None = Field(default=None, max_length=120)
    scope_type: str | None = Field(default=None, max_length=30)
    scope_id: str | None = Field(default=None, max_length=80)


class HistoryTurn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    role: Literal['user','assistant']
    content: str = Field(max_length=4000)


class Turn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: int = Field(strict=True, ge=0)
    stop: str = Field(max_length=120)
    question: str = Field(min_length=1, max_length=4000)
    path: str = Field(max_length=200)
    history: list[HistoryTurn] = Field(default_factory=list, max_length=12)


class TypedAnswer(BaseModel):
    model_config=ConfigDict(extra='forbid')
    revision: int=Field(strict=True,ge=0)
    stop: str=Field(max_length=120)
    state: Literal['answered','unknown','deferred','not_applicable']
    value: str | bool | int | float | None


@router.post('/answer',dependencies=[Depends(require_csrf_header)])
def typed_answer(body:TypedAnswer,ctx:TenantCtx=Depends(require_onboarded)):
    context=context_for(ctx,body.revision)
    try:
        current,stop=guide.current(context,body.stop)
        if stop['kind']!='fact' or current['status']!='active':
            raise ValueError('Use the current factual question, or the native settings editor for settings.')
        spec=state_store.resolve_question(context['state'],stop['question_id'])
        state_store.validate_scope(spec,current['scope_type'],current['scope_id'],context)
        value=contract.answer_value(spec,body.value) if body.state=='answered' else None
        state=deepcopy(context['state'])
        state_store._confirmed_answer(state,spec,current['scope_type'],current['scope_id'],body.state,value,ctx.user_id)
        resolve_owner_import(state,spec['id'])
        state['guide']['decisions'][stop['id']]=body.state
        return save(ctx,context,state)
    except (ValueError,KeyError) as exc:
        raise HTTPException(400,str(exc) if isinstance(exc,ValueError) else 'Invalid answer.') from None


def context_for(ctx, revision=None):
    require_manager(ctx)
    context = state_store.load(ctx)
    if revision is not None and revision != context['state']['revision']:
        raise HTTPException(409, 'The guide changed. Your text is kept; reload its current question before trying again.')
    if any(a['provenance'].get('source_question') and not a['provenance'].get('owner_accepted')
           for a in context['state']['answers'].values()):
        context = state_store.load(ctx, knowledge=True)
        if revision is not None and revision != context['state']['revision']:
            raise HTTPException(409, 'Setup changed. Reload the current question.')
        reconcile_knowledge(ctx,context,context['state'])
    return context


def save(ctx, context, state, card_write=None):
    state['last_action'] = dict(id=str(uuid4()), operation='guide', at=state_store.stamp(), by=ctx.user_id)
    saved = state_store.save(ctx, context['state'], state, card_write=card_write)
    return guide.describe({**context, 'state': saved})


@router.get('')
def introduction(request: Request, ctx: TenantCtx = Depends(require_onboarded)):
    context = context_for(ctx)
    from app.dashboard import _ctx
    return templates.TemplateResponse(request, 'app/guided_setup.html', _ctx('settings', ctx,
        guide_description=guide.describe(context)), headers={'Cache-Control': 'no-store'})


@router.get('/state')
def read(ctx: TenantCtx = Depends(require_onboarded)):
    return guide.describe(context_for(ctx))


@router.post('/action', dependencies=[Depends(require_csrf_header)])
def action(body: Action, ctx: TenantCtx = Depends(require_onboarded)):
    context = context_for(ctx, body.revision)
    if body.operation == 'retry_knowledge' or body.operation == 'start' and not (context['state'].get('guide') or {}).get('intro_ack'):
        context = state_store.load(ctx, knowledge=True)
        if context['state']['revision'] != body.revision:
            raise HTTPException(409, 'Setup changed. Reload before starting the walkthrough.')
    state = deepcopy(context['state'])
    card_write = None
    try:
        if body.operation != 'scope' and (body.scope_id is not None or body.scope_type is not None):
            raise ValueError('Scope fields belong to the scope action only.')
        if body.operation == 'start':
            if body.stop != 'intro' or body.choice is not None:
                raise ValueError('Start from the introduction.')
            if (state.get('guide') or {}).get('intro_ack'):
                raise ValueError('Setup has already started. Resume your current question.')
            if not state.get('guide'):
                state['guide'] = guide.neutral()
            reuse_confirmed_answers(ctx, context, state)
            state['guide'].update(status='active', intro_ack=True)
            state['guide']['current'] = guide.steps({**context, 'state': state})[1]['id']
        else:
            current, stop = guide.current(context, body.stop)
            current = state['guide']
            if body.operation == 'retry_knowledge':
                if body.choice is not None:
                    raise ValueError('Retry Knowledge does not choose an answer.')
                reuse_confirmed_answers(ctx,context,state)
            elif body.operation in ('knowledge_use_updated','knowledge_keep_earlier'):
                card_write = resolve_knowledge_choice(ctx,context,state,stop,body)
            elif body.operation in ('associate','refresh_preview'):
                if stop['kind'] not in ('decision','explanation') or body.choice is not None:
                    raise ValueError('Choose a current settings decision.')
                stop=guide.registry()[stop.get('decision_id',stop['id'])]
                card = setup_card_store.load(ctx.tenant_id,stop['page'],client=ctx.client)
                if not card:
                    raise ValueError('No existing card is available for this question.')
                link_card(ctx,context,state,stop,card,card,preserve_knowledge=True)
            elif body.operation == 'scope':
                if body.choice is not None:
                    raise ValueError('Unexpected scope choice.')
                spec = state_store.resolve_question(state,stop['question_id']) if stop['kind']=='fact' else {'scopes':['organisation']}
                state_store.validate_scope(spec,body.scope_type,body.scope_id,context)
                current.update(scope_type=body.scope_type,scope_id=body.scope_id)
                current['question_revision'] += 1
            elif body.operation in ('pause', 'resume'):
                if body.choice is not None:
                    raise ValueError('Unexpected guide choice.')
                current['status'] = 'paused' if body.operation == 'pause' else 'active'
            elif body.operation == 'revisit':
                target=next((s for s in guide.steps(context) if s['id']==body.choice),None)
                if stop['kind']!='review' or not target or target['substep']!=stop['substep'] or target['kind']=='review':
                    raise ValueError('Choose a question in this section review.')
                current['current']=target['id'];current['question_revision']+=1
            elif body.operation == 'need':
                if stop['kind']!='need' or body.choice not in ('needed','off','deferred'):
                    raise ValueError('Choose whether this topic is relevant.')
                if stop['need'] in contract.NEEDS:
                    state['needs'][stop['need']]=body.choice=='needed'
                else:
                    current.setdefault('needs',{})[stop['need']]=body.choice=='needed'
                current['decisions'][stop['id']]=body.choice
                _move({**context,'state':state},current,stop,1)
            elif body.operation == 'relevance':
                if stop['kind'] != 'relevance' or body.choice not in ('needed', 'off', 'deferred'):
                    raise ValueError('Choose whether this section is relevant.')
                state['capabilities'][stop['capability']] = body.choice
                if body.choice == 'needed':
                    for sid, sub in contract.SUBSTEPS.items():
                        if sub[3] == stop['capability']:
                            state['reviews'].pop(sid,None)
                for sid, sub in contract.SUBSTEPS.items():
                    if sub[3] == stop['capability'] and body.choice in ('off', 'deferred'):
                        state['reviews'][sid] = dict(state=body.choice, at=state_store.stamp(), by=ctx.user_id)
                current['decisions'][stop['id']] = body.choice
                _move({**context, 'state': state}, current, stop, 1)
            elif body.operation in ('next', 'back'):
                if body.choice is not None:
                    raise ValueError('Next and Back only change position.')
                current['explained'][stop['id']] = guide.VERSION
                _move(context, current, stop, 1 if body.operation == 'next' else -1)
            elif body.operation == 'keep_current':
                if stop['kind']!='decision' or body.choice is not None:
                    raise ValueError('Choose a current settings decision.')
                if stop['id'] in current['pending'] and state['proposals'][current['pending'][stop['id']]['proposal']]['status']=='proposed':
                    raise ValueError('Resolve the pending suggestion or defer it before keeping the current settings.')
                current['decisions'][stop['id']]='reviewed_unchanged'
                _move(context,current,stop,1)
            elif body.operation in ('later', 'not_needed', 'review'):
                if body.choice is not None:
                    raise ValueError('Unexpected guide choice.')
                status = 'deferred' if body.operation == 'later' else 'not_applicable' if body.operation == 'not_needed' else 'reviewed'
                if stop['kind'] == 'review':
                    pending = [p for p in state['proposals'].values() if p['status'] == 'proposed' and
                               any(state['answers'].get(k, {}).get('substep') == stop['substep'] for k in p.get('answers', [p['answer']]))]
                    if body.operation == 'review' and pending:
                        raise ValueError('Pending suggestions remain. Save or leave them, or explicitly defer this section.')
                    unresolved = [a for a in state['answers'].values() if a['substep']==stop['substep'] and
                                  a['state'] in ('unknown','deferred','unanswered')]
                    if body.operation=='review' and (unresolved or state_store.unresolved_dependencies(state,stop['substep'])):
                        raise ValueError('Related or unanswered decisions remain. Resolve them or explicitly defer this section.')
                    if body.operation=='review' and guide.unresolved_required({**context,'state':state},stop['substep']):
                        raise ValueError('Required decisions have no answer or explicit review yet. Review those questions, keep current settings explicitly, or defer this section.')
                    state['reviews'][stop['substep']] = dict(state='deferred' if status != 'reviewed' else status,
                        at=state_store.stamp(), by=ctx.user_id)
                current['decisions'][stop['id']] = status
                _move(context, current, stop, 1)
            elif body.operation == 'finish':
                if stop['kind'] != 'finished' or body.choice is not None:
                    raise ValueError('Review the remaining sections first.')
                available = [s['id'] for g in contract.group_steps(context['modules'], state=state) for s in g['substeps']]
                if any(state['reviews'].get(s, {}).get('state') not in ('reviewed', 'off', 'deferred') for s in available):
                    raise ValueError('Some sections remain unreviewed. Use the grouped review or explicitly defer them.')
                if any(state['reviews'].get(s,{}).get('state')=='reviewed' and
                       guide.unresolved_required({**context,'state':state},s) for s in available):
                    raise ValueError('Required decisions remain unanswered. Review or explicitly defer their sections.')
                current['status'] = 'finished'
            else:
                raise ValueError('Unknown guide operation.')
    except (ValueError, KeyError) as exc:
        raise HTTPException(400, str(exc) if isinstance(exc, ValueError) else 'Invalid guide data.') from None
    card_write = card_write or prepare_known_choice(ctx, context, state)
    return save(ctx, context, state, card_write)


def reuse_confirmed_answers(ctx, context, state):
    """Carry already-reviewed facts into setup without changing any setting.

    The Knowledge adapter verifies saved reviews and current source coverage.
    Raw extracts and generated questions never acquire owner approval here.
    """
    from hashlib import sha256
    description = context['config'].get('business_brief')
    key = state_store.key_for('activity.purpose', 'organisation', 'organisation')
    if description and key not in state['answers']:
        # Do not truncate a confirmed description to make it fit an answer.
        try:
            value = contract.bounded(description, 2000)
        except ValueError:
            value = None
        if value:
            state_store._confirmed_answer(state, contract.question('activity.purpose'),
                'organisation', 'organisation', 'answered', value, ctx.user_id,
                provenance=dict(kind='about_you_owner_review', reference='business_brief',
                                evidence_revision=sha256(value.encode()).hexdigest()))
    reconcile_knowledge(ctx,context,state)
    for question in state_store.knowledge_candidates(context.get('knowledge')):
        identity = question.get('canonical') or question['id']
        key = state_store.key_for(identity, 'organisation', 'organisation')
        prior = state['answers'].get(key)
        if prior:
            continue
        spec = contract.question(identity) if question.get('canonical') else question
        if not question.get('canonical'):
            state.setdefault('routed_questions', {})[identity] = {k: v for k, v in question.items() if k not in ('answer', 'state')}
        state_store._confirmed_answer(state, spec, 'organisation', 'organisation',
            question['state'], question['answer'], ctx.user_id,
            provenance=dict(kind='knowledge_owner_review', reference=question['source_review'],
                            source_question=question['id'], evidence_revision=question['evidence_revision']))
        state['imports'][question['id']] = dict(status='reused',
            evidence_revision=question['evidence_revision'], question=identity)


def resolve_owner_import(state,question):
    for imported in state['imports'].values():
        if imported.get('question')==question:
            imported['status']='owner_confirmed'


def reconcile_knowledge(ctx,context,state):
    """Invalidate imported authority, retaining history and all saved settings.

    Reads derive this view without persisting it. The next explicit mutation
    commits it atomically. Acceptance also checks this view, so an old card or
    a missing source cannot bypass the unresolved answer.
    """
    unavailable=bool(context.get('knowledge_unavailable'))
    if state.get('guide'):
        state['guide']['knowledge_unavailable']=unavailable
    candidates={q['id']:q for q in state_store.knowledge_candidates(context.get('knowledge'))}
    for key,answer in list(state['answers'].items()):
        provenance=answer['provenance'];identity=provenance.get('source_question')
        if not identity or provenance.get('owner_accepted'):
            continue
        candidate=candidates.get(identity)
        if not unavailable and candidate and candidate['evidence_revision']==provenance.get('evidence_revision') and provenance['kind']!='knowledge_conflict':
            continue
        imported=state['imports'].setdefault(identity,{})
        earlier=deepcopy(imported.get('earlier') or {k:answer[k] for k in ('state','value')})
        imported.update(status='conflict',question=answer['question'],earlier=earlier,
            evidence_revision=candidate['evidence_revision'] if candidate else None,
            candidate=deepcopy(candidate) if not unavailable else None,
            message='Knowledge could not be checked. Review this earlier answer before using it.' if unavailable else
                    'Knowledge changed or was removed. Review this earlier answer before using it.')
        if provenance['kind']!='knowledge_conflict':
            history=deepcopy(answer.get('history',[]))
            history.append({k:v for k,v in (answer|{'state':'superseded','previous_state':answer['state']}).items() if k!='history'})
            state['answers'][key]={**answer,'state':'unknown','value':None,'revision':answer['revision']+1,
                'history':history,'provenance':{**provenance,'kind':'knowledge_conflict'}}
        state['reviews'].pop(answer['substep'],None)
        if state.get('guide'):
            state['guide']['decisions'].pop('question.'+answer['question'],None)


def resolve_knowledge_choice(ctx,context,state,stop,body):
    conflict=next((c for c in guide.knowledge_conflicts(state,stop) if c['id']==body.choice),None)
    if not conflict:
        raise ValueError('Review a changed Knowledge answer for this current question.')
    imported=state['imports'][body.choice]
    chosen=imported.get('candidate') if body.operation=='knowledge_use_updated' else imported['earlier']
    if not chosen or chosen['state'] not in ('answered','not_applicable'):
        raise ValueError('No current reviewed answer is available. Answer this question explicitly or defer it.')
    spec=state_store.resolve_question(state,imported['question'])
    value=chosen.get('answer',chosen.get('value')) if chosen['state']=='answered' else None
    value=contract.answer_value(spec,value) if chosen['state']=='answered' else None
    key=state_store.key_for(spec['id'],'organisation','organisation')
    pending=next((p for p in state['proposals'].values() if p['status']=='proposed' and key in p.get('answers',{})),None)
    if pending:
        adapter=spec.get('adapter')
        if not adapter or adapter[1]!='setting' or chosen['state']!='answered':
            raise ValueError('Leave the pending suggestion before changing this answer.')
        card=setup_card_store.load(ctx.tenant_id,pending['page'],client=ctx.client)
        if not card or card['id']!=pending['card_id']:
            raise ValueError('The pending suggestion changed. Reload it before resolving Knowledge.')
        line=next((l for l in card['lines'] if l['id'] in pending['line_ids'] and l['name']=='setting' and l['fields']['key']==adapter[2] and l['status'] in setup_cards.PENDING),None)
        if not line:
            raise ValueError('The pending suggestion changed. Reload it before resolving Knowledge.')
        changed=setup_cards.change(card['page'],card,card['revision'],str(uuid4()),[
            dict(op='set',id=line['id'],fields={'value':value},sources={'value':dict(kind='owner_said',reference='Owner explicitly resolved changed Knowledge')})])
        target=guide.registry()[stop.get('decision_id',stop['id'])]
        link_card(ctx,context,state,target,card,changed,confirmed_lines={line['id']})
        return dict(page=card['page'],revision=card['revision'],card=changed,expected_settings=None,settings_patch=None)
    state_store._confirmed_answer(state,spec,'organisation','organisation',chosen['state'],value,ctx.user_id)
    resolve_owner_import(state,spec['id'])
    decision=stop.get('decision_id',stop['id'])
    state['guide']['pending'].pop(decision,None)
    state['guide']['decisions'].pop(decision,None)
    return None


def prepare_known_choice(ctx, context, state):
    """An exact prior scalar answer can populate a card, never saved settings.

    Runs only within a deliberate guide mutation. Reads/reloads are inert and
    no semantic matching, defaults, or model calls are involved.
    """
    progress=state.get('guide') or {}
    stop=guide.registry().get(progress.get('current'))
    if not stop or progress.get('status')!='active' or stop['id'] in progress.get('pending',{}) or stop['id'] in progress.get('decisions',{}):
        return None
    if guide.knowledge_conflicts(state,stop):
        return None
    changes=[]
    for identity in stop['catalogue']:
        page,key=identity.split('/',1)
        answer=state['answers'].get(state_store.key_for('setting.'+page+'.'+key,'organisation','organisation'))
        if not answer or answer['state']!='answered':
            continue
        spec=state_store.resolve_question(state,answer['question'])
        if spec.get('adapter') != [page,'setting',key] and spec.get('adapter') != (page,'setting',key):
            continue
        source=dict(kind='owner_said',reference='Confirmed setup answer '+answer['question']+' revision '+str(answer['revision']))
        changes.append(dict(op='append',name='setting',fields={'key':key,'value':answer['value']},
                            sources={'key':source,'value':source}))
    if not changes:
        return None
    state_store._page_available(context,stop['page'])
    card=setup_card_store.load(ctx.tenant_id,stop['page'],client=ctx.client)
    # An existing pending row is authoritative. Associate it without replacing
    # a more recent card edit with an older ledger answer.
    if card and any(l['status'] in setup_cards.PENDING and _allowed(stop,card,dict(op='set',id=l['id'])) for l in card['lines']):
        link_card(ctx,context,state,stop,card,card,preserve_knowledge=True)
        return None
    revision=(card or {}).get('revision',0)
    changed=setup_cards.change(stop['page'],card,revision,str(uuid4()),changes)
    link_card(ctx,context,state,stop,card,changed,preserve_knowledge=True)
    return dict(page=stop['page'],revision=revision,card=changed,expected_settings=None,settings_patch=None)


def _move(context, current, stop, direction):
    sequence = guide.steps(context)
    index = next(i for i, s in enumerate(sequence) if s['id'] == stop['id'])
    current['current'] = sequence[max(0, min(len(sequence)-1, index+direction))]['id']
    current['question_revision'] += 1
    next_stop=sequence[max(0,min(len(sequence)-1,index+direction))]
    if current['scope_type'] not in next_stop.get('scopes',['organisation']):
        current.update(scope_type='organisation',scope_id='organisation')


def _allowed(stop, card, patch):
    if patch['op'] in ('set', 'remove'):
        line = next((l for l in (card or {}).get('lines', []) if l['id'] == patch['id']), None)
        if not line:
            return False
        name, fields = line['name'], line['fields']
    else:
        name, fields = patch['name'], patch['fields']
    identity = stop['page']+'/'+(fields.get('key', '') if name == 'setting' else name)
    return identity in stop['catalogue']


@router.post('/turn', dependencies=[Depends(require_csrf_header)])
async def turn(body: Turn, ctx: TenantCtx = Depends(require_onboarded)):
    context = context_for(ctx, body.revision)
    try:
        current, stop = guide.current(context, body.stop)
        if current['status'] != 'active' or body.path != stop['href'].split('#')[0]:
            raise ValueError('Open the current guide page before answering.')
        if guide.knowledge_conflicts(context['state'],stop):
            raise ValueError('Resolve the changed Knowledge answer shown here before asking for another suggestion. You can also answer a factual question directly.')
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    from app import concierge, concierge_routes, usage
    cards = setup_card_store.load_all(ctx.tenant_id, client=ctx.client)
    tenant = concierge_routes._tenant_context(ctx) or {}
    tenant['onboarding_answers']=guide.model_data(context,stop)
    tenant['setup_card_context'] = setup_card_model.context(cards, body.path, context['cfg'])+'\n'+guide.model_context(context, stop)
    if stop['kind']=='fact':
        tenant['guided_question'] = state_store.resolve_question(context['state'],stop['question_id'])
    history = concierge.sanitize_history([t.model_dump() for t in body.history], 12, 4000)
    try:
        result = await concierge.answer_help(body.question, history, tenant=tenant, tenant_id=ctx.tenant_id)
    except Exception:
        return dict(**guide.describe(context), reply='I couldn’t interpret that just now. Your reply is kept. Try again or use the existing settings editor for this question.', fallback=True)
    usage.record_anthropic(ctx.tenant_id, result.model, result.usage, operation='concierge_help', module='concierge')
    try:
        latest=context_for(ctx,body.revision)
        if guide.knowledge_conflicts(latest['state'],stop):
            raise setup_cards.Stale('Knowledge changed while preparing the suggestion. Review its current answer first.')
        entries, _ = setup_card_model.normalise(setup_card_model.unpack(result.setup_changes or []), cards)
        if stop['kind']=='fact' and not entries:
            values = result.guide_answers or []
            if not values:
                return dict(**guide.describe(context), reply=result.reply, fallback=False)
            if len(values)!=1 or set(values[0])!={'question','state','value'} or values[0]['question']!=stop['question_id']:
                raise ValueError('The answer did not identify this exact question. Please clarify it.')
            spec=state_store.resolve_question(context['state'],stop['question_id'])
            item=values[0]
            if item['state'] not in state_store.ANSWER_STATES-{'unanswered'}:
                raise ValueError('Choose an explicit answer state.')
            state_store.validate_scope(spec,current['scope_type'],current['scope_id'],context)
            value=contract.answer_value(spec,item['value']) if item['state']=='answered' else None
            state=deepcopy(context['state'])
            state_store._confirmed_answer(state,spec,current['scope_type'],current['scope_id'],item['state'],value,ctx.user_id)
            resolve_owner_import(state,spec['id'])
            state['guide']['decisions'][stop['id']]=item['state']
            output=save(ctx,context,state)
            return dict(**output,reply='Your answer is kept for this question and scope. It has not changed settings.',fallback=False)
        if not entries:
            return dict(**guide.describe(context), reply=result.reply, fallback=False)
        if len(entries)!=1:
            raise ValueError('Please answer one settings area at a time. The current question is kept.')
        entry = entries[0]
        if entry['page'] in ('crm_fields','crm_settings') and 'crm' not in context['modules']:
            raise ValueError('This record editor is unavailable for this account.')
        if entry['page']=='diary_settings' and 'scheduling' not in context['modules']:
            raise ValueError('These diary proposals need the Scheduling module. Use the available native editor.')
        card = cards.get(entry['page'])
        target=guide.registry()[stop['decision_id']] if stop['kind']=='explanation' else stop
        held=stop['kind'] not in ('decision','explanation') or entry['page']!=stop.get('page') or not all(_allowed(stop,card,p) for p in entry['changes'])
        if held:
            target=next((s for s in guide.registry().values() if s['page']==entry['page'] and
                         all(_allowed(s,card,p) for p in entry['changes'])),None)
            if target is None:
                raise ValueError('Those values do not identify one supported question. Please clarify them.')
            # Holding is metadata and a pending native card only. It does not
            # select relevance, move the current question or apply settings.
            state_store._page_available(context,target['page'])
        patches=_deduplicate(card,entry['changes'])
        if any(source.get('kind')=='assumed' for p in patches for source in p.get('sources',{}).values()):
            raise ValueError('A choice was assumed rather than supplied. Please answer it explicitly; no suggestion was stored.')
        if held:
            patches=[dict(p,held=True) if p['op']!='remove' else p for p in patches]
        # No later provider result can overwrite a newer owner or device turn.
        context_for(ctx, body.revision)
        changed = setup_cards.change(entry['page'], card, entry['revision'], str(uuid4()), patches)
        state = deepcopy(context['state'])
        link_card(ctx, context, state, target, card, changed)
        output = save(ctx, context, state, dict(page=entry['page'], revision=entry['revision'], card=changed,
                      expected_settings=None, settings_patch=None))
        return dict(**output, reply=('Kept that suggestion for '+target['title']+'. Your current question stays here; no settings were changed.' if held else
            'Your suggestion is ready to review. It is not saved to settings. Edit, Accept or Leave the displayed changes.'), fallback=False)
    except (ValueError, setup_cards.Invalid) as exc:
        raise HTTPException(409 if isinstance(exc, setup_cards.Stale) else 422, str(exc)) from None


def _deduplicate(card,patches):
    result=[]
    for original in patches:
        patch=deepcopy(original)
        if patch['op']=='append':
            fields=patch['fields'];keys=('key',) if patch['name']=='setting' else ('kind','label') if patch['name']=='field' else (
                ('who','label') if patch['name']=='ladder' else ('label',) if patch['name'] in ('group','contact_field','note') else ('name',))
            matches=[l for l in (card or {}).get('lines',[]) if l['status'] in setup_cards.PENDING and l['name']==patch['name'] and
                     all(fields.get(k)==l['fields'].get(k) for k in keys)]
            if len(matches)>1:
                raise setup_cards.Stale('Several pending rows match this question. Review their stable line IDs first.')
            if matches:
                patch.pop('name');patch.update(op='set',id=matches[0]['id'])
        result.append(patch)
    return result


def link_card(ctx, context, state, stop, before, card, *, preserve_knowledge=False, confirmed_lines=()):
    """Link exact native lines to scoped answers, updating their superseded history.

    The original owner statement stays in history when an edited operational
    setting replaces its representation. No raw conversation is used as a value.
    """
    selected = [l for l in card['lines'] if l['status'] in setup_cards.PENDING and
                _allowed(stop, card, dict(op='set', id=l['id']))]
    if not selected:
        return
    if preserve_knowledge and guide.knowledge_conflicts(state,stop):
        raise setup_cards.Invalid('Resolve the changed Knowledge answer before refreshing this suggestion.')
    snapshot = setup_card_routes._snapshot(card['page'], context['cfg'], ctx, card=card)
    try:
        preview = setup_card_routes._preview(card, [l['id'] for l in selected], snapshot)
    except setup_cards.Invalid:
        preview = dict(settings_version=setup_card_routes._version(snapshot), changes=[])
    answers = {}
    previous_fields={l['id']:l['fields'] for l in (before or {}).get('lines',[])}
    for line in selected:
        for spec,scope_type,scope_id,value in _line_answers(context,state,stop,card,line):
            key = state_store.key_for(spec['id'], scope_type, scope_id)
            prior=state['answers'].get(key,{})
            explicit=not preserve_knowledge and (line['id'] in confirmed_lines or previous_fields.get(line['id'])!=line['fields'])
            inherited=prior.get('provenance',{}) if not explicit else {}
            if inherited.get('kind')=='knowledge_conflict':
                raise setup_cards.Invalid('Resolve the changed Knowledge answer before editing other choices in this group.')
            # A validated card edit supersedes its linked setting answer, while
            # preserving a distinct purpose, policy or original business fact.
            for p in state['proposals'].values():
                if key in p.get('answers', {}) and p['status'] == 'proposed':
                    if p['card_id'] != card['id']:
                        raise setup_cards.Stale('An earlier proposal for this answer still needs review.')
                    p['status'] = 'superseded'
            source_key=spec.get('adapter',('', '', ''))[-1] if spec.get('adapter') else None
            sources=[line['sources'].get(source_key)] if source_key else list(line['sources'].values())
            status='unknown' if any(s and s.get('kind')=='assumed' for s in sources) else 'answered'
            provenance=dict(kind='validated_proposal', reference=line['id'], card_id=card['id'],
                            card_revision=card['revision'], sources=deepcopy(line['sources']))
            if inherited.get('source_question'):
                provenance.update({k:inherited[k] for k in ('source_question','evidence_revision')})
            state_store._confirmed_answer(state, spec, scope_type, scope_id, status, value,
                ctx.user_id, provenance=provenance)
            if not inherited.get('source_question'):
                resolve_owner_import(state,spec['id'])
            answers[key] = state['answers'][key]['revision']
    previous = (state['guide']['pending'].get(stop['id']) or {}).get('proposal')
    identity = str(uuid4())
    state['proposals'][identity] = dict(id=identity, answer=next(iter(answers)),
        answer_revision=next(iter(answers.values())), answers=answers, page=card['page'], card_id=card['id'],
        line_ids=[l['id'] for l in selected], card_revision=card['revision'], settings_version=preview['settings_version'],
        changes=preview['changes'], scope_type='organisation', scope_id='organisation', status='proposed',
        created_at=state_store.stamp(), consequence='Only the displayed native changes can be saved.')
    if previous:
        state['proposals'][identity]['previous_proposal'] = previous
    state['guide']['pending'][stop['id']] = dict(proposal=identity, page=card['page'], card_id=card['id'],
        revision=card['revision'], ids=[l['id'] for l in selected])
    state['guide']['decisions'].pop(stop['id'],None)


def _line_answers(context,state,stop,card,line):
    """Native row identity is checked before assigning a durable answer scope."""
    fields=line['fields']
    scope_type,scope_id='organisation','organisation'
    if line['name']=='service':
        bound=next(((key,s) for key,s in state['scopes'].items() if s.get('proposal_line')==line['id']),None)
        if not bound:
            matches=[(key,s) for key,s in state['scopes'].items() if s['service_name']==fields['name']]
            if len(matches)>1:
                raise ValueError('This activity identity is ambiguous. Review its saved identity first.')
            bound=matches[0] if matches else None
        if bound:
            scope_id,scope=bound
            if scope.get('status')=='saved' and scope['service_name']!=fields['name']:
                raise setup_cards.Stale('This activity was renamed. Review its identity in the native editor.')
            scope.update(proposal_line=line['id'],service_name=fields['name'],label=fields['name'])
        else:
            scope_id=str(uuid4())
            saved=any(s['name']==fields['name'] for s in context['cfg'].get('service_types',[]))
            state['scopes'][scope_id]=dict(service_name=fields['name'],label=fields['name'],
                proposal_line=line['id'],status='saved' if saved else 'proposed',registered_at=state_store.stamp())
        scope_type='activity'
        for field,qid in (('minutes','service.duration'),('price','service.price')):
            if field in fields:
                spec=contract.question(qid)
                yield spec,scope_type,scope_id,contract.answer_value(spec,fields[field])
    elif line['name']=='field' and card['page']=='dependants':
        groups=[g for g in context['groups'] if g['label']==fields.get('kind')]
        if len(groups)>1:
            raise ValueError('This related-record identity is ambiguous. Review its native editor.')
        if groups:
            scope_type,scope_id='record_group',str(groups[0]['id'])
    qid = 'setting.'+card['page']+'.'+fields['key'] if line['name'] == 'setting' else 'guide.'+line['id']
    value = fields['value'] if line['name'] == 'setting' else deepcopy(fields)
    if line['name']=='service':
        value={k:v for k,v in value.items() if k not in ('minutes','price')}
    if qid in contract.QUESTIONS:
        spec = contract.question(qid)
    else:
        spec = dict(id=qid, text=stop['question'], substep=stop['substep'], topic=qid,
            capability=contract.SUBSTEPS[stop['substep']][3], kind='text', adapter=None,
            scopes=[scope_type], dependencies=[])
        state.setdefault('routed_questions', {})[qid] = spec
    yield spec,scope_type,scope_id,value


def commit_linked_card(ctx, page, revision, card, *, snapshot=None, patch=None):
    """Existing card endpoints call this for guide-linked cards only.

    Card, answer revision, proposal receipt and settings use the same existing
    service RPC. The guided migration extends its native-page allowlist; no
    parallel operational writer is added.
    """
    config = db.get_tenant_config(ctx.tenant_id, client=ctx.client) or {}
    current = guide.read(contract.read_state(config.get('onboarding_state')))
    if not current or not any(p.get('card_id') == card['id'] for p in current['pending'].values()):
        return None
    require_manager(ctx)
    context = context_for(ctx)
    state = deepcopy(context['state'])
    last=card.get('last_action') or {}
    touched=set(last.get('ids') or [c['id'] for c in last.get('changes',[])])
    for stop_id, pending in list(current['pending'].items()):
        if pending['card_id'] != card['id']:
            continue
        if not touched.intersection(pending['ids']):
            continue
        stop = guide.registry().get(stop_id)
        if not stop:
            raise setup_cards.Stale('This guide decision is unavailable. Review its original card.')
        proposal = state['proposals'][pending['proposal']]
        if proposal['status']=='superseded':
            continue
        if snapshot is not None:
            if any(state['answers'][key]['provenance']['kind']=='knowledge_conflict' for key in proposal['answers']):
                raise setup_cards.Stale('Knowledge changed. Resolve the earlier answer in the guide before accepting this suggestion.')
            if setup_card_routes._version(snapshot) != proposal['settings_version']:
                raise setup_cards.Stale('Saved settings changed since this guided proposal. Review the current difference explicitly before accepting.')
            if proposal['card_revision'] != revision:
                raise setup_cards.Stale('The linked proposal changed. Review its current difference explicitly before accepting.')
            for key, expected in proposal['answers'].items():
                if state['answers'][key]['revision'] != expected:
                    raise setup_cards.Stale('The linked answer changed. Review it before accepting.')
        selected = [l for l in card['lines'] if l['id'] in pending['ids']]
        if len(selected) != len(pending['ids']):
            raise setup_cards.Stale('The guide lines changed. Reload their saved result.')
        for line in selected:
            for scope in state['scopes'].values():
                if scope.get('proposal_line')==line['id'] and line['status'] in ('applied','left'):
                    scope['status']='saved' if line['status']=='applied' else 'left'
        previous_card = (context['config'].get('setup_cards') or {}).get(page) or {}
        old_fields = {l['id']: l['fields'] for l in previous_card.get('lines',[])}
        edited = any(l['status'] in setup_cards.PENDING and old_fields.get(l['id']) != l['fields'] for l in selected)
        if edited:
            link_card(ctx,context,state,stop,previous_card,card)
        elif any(l['status'] in ('applied', 'left') for l in selected):
            statuses = {l['status'] for l in selected}
            terminal = statuses <= {'applied','left'}
            proposal.update(status=('applied' if statuses == {'applied'} else 'cancelled' if statuses == {'left'} else 'partial') if terminal else 'proposed',
                line_receipts={l['id']: l['status'] for l in selected}, finished_at=state_store.stamp())
            if terminal:
                state['guide']['decisions'][stop_id] = proposal['status']
            for line in selected:
                if line['status']!='applied':
                    continue
                for spec,scope_type,scope_id,value in _line_answers(context,state,stop,card,line):
                    key=state_store.key_for(spec['id'],scope_type,scope_id)
                    answer=state['answers'].get(key)
                    if answer:
                        answer['provenance']['owner_accepted']=True
                        resolve_owner_import(state,spec['id'])
                    if not answer or answer['state']=='answered':
                        continue
                    previous_status=proposal['status'];proposal['status']='confirming'
                    state_store._confirmed_answer(state,spec,scope_type,scope_id,'answered',value,ctx.user_id,
                        provenance=dict(kind='owner_accepted_proposal',reference=line['id'],card_id=card['id'],
                                        card_revision=card['revision'],sources=deepcopy(line['sources'])))
                    proposal['status']=previous_status
                    proposal['answers'][key]=state['answers'][key]['revision']
                    proposal['answer_revision']=proposal['answers'][proposal['answer']]
        else:
            link_card(ctx, context, state, stop, None, card,preserve_knowledge=True)
    state_store.save(ctx, context['state'], state, card_write=dict(page=page, revision=revision, card=card,
        expected_settings=snapshot, settings_patch=patch))
    return card
