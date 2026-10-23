"""Durable review/answer metadata and adapters to existing atomic setup cards."""
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
from uuid import uuid4

from fastapi import HTTPException
from app import db, onboarding_contract as contract, setup_cards, setup_card_store, setup_card_routes

ANSWER_STATES = {'unanswered', 'answered', 'unknown', 'deferred', 'not_applicable'}
TOPIC_DESTINATIONS = {'prices': 'charges.terms', 'payment': 'charges.terms', 'rules': 'diary.conditions',
                      'process': 'diary.conditions', 'cadence': 'diary.time', 'safety': 'diary.activities',
                      'coverage': 'diary.time', 'hours': 'diary.time'}
# Exact code-owned meaning aliases only. Similar source wording cannot choose
# an executable consumer or turn response/contact policy into availability.
KNOWLEDGE_ALIASES = {('payment', 'When and how is payment due, including any deposits?'):
                     'setting.invoice_settings.payment_terms'}


def stamp():
    return datetime.now(timezone.utc).isoformat()


def resolve_question(state, identity):
    if identity in contract.QUESTIONS:
        return contract.question(identity)
    routed = state.get('routed_questions', {}).get(identity)
    if not routed or routed['substep'] not in contract.SUBSTEPS:
        raise ValueError('Unknown question identity.')
    return deepcopy(routed)


def key_for(identity, scope_type, scope_id):
    return sha256(json.dumps([identity, scope_type, scope_id], separators=(',', ':')).encode()).hexdigest()[:32]


def validate_scope(spec, scope_type, scope_id, context):
    groups = context['groups']
    if scope_type == 'record':
        row = db.get_requirement_instance(scope_id, context['tenant_id'], client=context['client'])
        if not row or row.get('archived_at'):
            raise ValueError('Choose a live record in this account.')
        groups = [row]
    contract.validate_scope(spec, scope_type, scope_id, context['state'], groups)


def load(ctx, *, knowledge=False):
    config = db.get_tenant_config(ctx.tenant_id, client=ctx.client) or {}
    try:
        state = contract.read_state(config.get('onboarding_state'))
    except ValueError as exc:
        raise HTTPException(503, str(exc)) from None
    modules = set(db.visible_modules(ctx.tenant_id, client=ctx.client))
    cfg = db.get_scheduling_config(ctx.tenant_id, client=ctx.client) or {}
    groups = db.list_requirement_groups(ctx.tenant_id, client=ctx.client, strict=True) if {'scheduling', 'tms', 'fsm'} & modules else []
    result = dict(state=state, config=config, cfg=cfg, modules=modules, groups=groups,
                  tenant_id=ctx.tenant_id, client=ctx.client,
                  invoice=db.get_invoice_settings(ctx.tenant_id, client=ctx.client) or {},
                  knowledge=None, knowledge_unavailable=False)
    if knowledge:
        try:
            from app import knowledge_review
            result['knowledge'] = knowledge_review.load(ctx.tenant_id)
        except HTTPException:
            result['knowledge_unavailable'] = True
    return result


def save(ctx, before, after, *, card_write=None):
    """Metadata and optional existing card write share one transaction/lock.

    No values in logs or exception diagnostics. An uncertain response is read
    back on reload; retry cannot duplicate the card's effects.
    """
    after['revision'] = before['revision'] + 1
    args = {'p_tenant': ctx.tenant_id, 'p_revision': before['revision'], 'p_state': after,
            'p_card_write': card_write}
    try:
        # This RPC is service-only. The HTTP boundary checks the authenticated
        # tenant and settings-manager permission; clients cannot submit patches.
        result = db.db().rpc('save_onboarding_state', args).execute().data
    except Exception as exc:
        if any(code in str(exc) for code in ('BLAM8_ONBOARDING_STALE', 'BLAM8_SETUP_STALE', 'BLAM8_SETTINGS_STALE')):
            raise HTTPException(409, 'The review, proposal or saved settings changed. Reload and review the current difference.') from None
        raise HTTPException(503, 'The save could not be confirmed. Reload to check before retrying.') from None
    if result != after:
        raise HTTPException(503, 'The save could not be confirmed. Reload to check before retrying.')
    from app.dashboard import _bust_setup_strip
    _bust_setup_strip(ctx)
    return after


def _confirmed_answer(state, spec, scope_type, scope_id, status, value, actor, *, provenance=None):
    key = key_for(spec['id'], scope_type, scope_id)
    old = state['answers'].get(key)
    if old and any(p['status'] == 'proposed' and key in p.get('answers', {p['answer']: p['answer_revision']}) for p in state['proposals'].values()):
        raise ValueError('Cancel the pending proposal before changing its answer.')
    answer = dict(question=spec['id'], topic=spec['topic'], capability=spec['capability'],
        substep=spec['substep'], scope_type=scope_type, scope_id=scope_id, state=status,
        value=value if status == 'answered' else None, revision=(old or {}).get('revision', 0) + 1,
        confirmed_at=stamp(), confirmed_by=actor,
        provenance=provenance or {'kind': 'owner_answer', 'reference': spec['id']},
        history=deepcopy((old or {}).get('history', [])))
    if old:
        answer['history'].append({k: v for k, v in (old | {'state': 'superseded', 'previous_state': old['state']}).items() if k != 'history'})
    state['answers'][key] = answer
    state['reviews'].pop(spec['substep'], None)
    state['position'] = spec['substep']
    state['scope_position'] = {'type': scope_type, 'id': scope_id}
    return key


def knowledge_candidates(knowledge):
    """Reuse saved owner decisions, never pending extraction or stale coverage.

    Stable identities come from the existing Knowledge question records. This
    does not reimplement its source filtering or effective-fact corrections.
    """
    if not knowledge:
        return []
    from app import knowledge_review
    view = knowledge_review.topic_view(knowledge)
    result = []
    for topic in view['topics']:
        for q in topic['questions']:
            # Do not turn generated defaults or unapproved extraction into tasks.
            saved = next((r for r in reversed(knowledge.get('reviews', [])) if r['state'] == 'saved'
                          and any(v['id'] == q['id'] for v in r['payload'].get('topic_reviews', {}).get(topic['key'], {}).get('questions', []))), None)
            if not saved:
                continue
            question = contract.bounded(q['question'], 1000)
            destination = TOPIC_DESTINATIONS[topic['key']]
            lower = question.lower()
            if 'agreement' in lower and any(word in lower for word in ('notice', 'cancel', 'end')):
                destination = 'charges.terms'
            status = q.get('status', 'unknown')
            # Expired coverage cannot certify that a historic answer is current.
            review_topic = saved['payload']['topic_reviews'][topic['key']]
            evidence = knowledge_review.fingerprint([(s['source'], s.get('mailbox_id'), s['content'])
                for s in sorted(knowledge['sources'], key=lambda s: (s['source'], s.get('mailbox_id') or ''))])
            current = (review_topic['scoped_evidence']==knowledge_review.topic_evidence(topic['key'],knowledge['sources'])
                       if 'scoped_evidence' in review_topic else review_topic.get('evidence')==evidence)
            explicit = any(item['question'] == question for item in saved['payload'].get('questions', []))
            if status == 'unknown' and not explicit:
                continue
            canonical = KNOWLEDGE_ALIASES.get((topic['key'], question))
            if canonical and status == 'answered':
                try:
                    contract.answer_value(contract.question(canonical), q.get('answer', ''))
                except ValueError:
                    canonical = None  # Keep the complete fact, never truncate to fit a setting.
            result.append(dict(id='knowledge.' + q['id'], substep=destination,
                text=question, kind='text', adapter=None, scopes=['organisation'], choices=None,
                capability=contract.SUBSTEPS[destination][3], topic=topic['key'], dependencies=[],
                why='Retained from an owner Knowledge review. This fact does not apply operational settings.',
                answer=q.get('answer', ''), state=('answered' if status == 'answered' and current else
                    'not_applicable' if status == 'not_applicable' and current else 'unknown'),
                canonical=canonical,
                evidence_revision=knowledge_review.fingerprint(q['id'],q.get('answer',''),status,current,
                    review_topic.get('scoped_evidence',review_topic.get('evidence'))), source_review=saved['id']))
    return result


def transact(ctx, context, body):
    before = context['state']
    if body.revision != before['revision']:
        raise HTTPException(409, 'The review changed. Reload before saving.')
    state = deepcopy(before)
    op, data = body.operation, body.data
    card_write = None
    try:
        if op == 'answer':
            _keys(data, {'question', 'scope_type', 'scope_id', 'state', 'value'})
            spec = resolve_question(state, data['question'])
            validate_scope(spec, data['scope_type'], data['scope_id'], context)
            if data['state'] not in ANSWER_STATES:
                raise ValueError('Choose an answer state.')
            value = contract.answer_value(spec, data['value']) if data['state'] == 'answered' else None
            _confirmed_answer(state, spec, data['scope_type'], data['scope_id'], data['state'], value, ctx.user_id)
        elif op == 'review':
            _keys(data, {'substep', 'state'})
            sid, status = data['substep'], data['state']
            if sid not in contract.SUBSTEPS or status not in ('unreviewed', 'reviewed', 'off', 'deferred'):
                raise ValueError('Choose a known section and review state.')
            available = {s['id'] for g in contract.group_steps(context['modules'], state=state) for s in g['substeps']}
            if sid not in available:
                raise ValueError('This section is unavailable.')
            capability = contract.SUBSTEPS[sid][3]
            decision = state['capabilities'].get(capability, 'undecided')
            if status == 'off' and decision != 'off':
                raise ValueError('Choose Not needed for this capability before reviewing it off.')
            unresolved = [a for a in state['answers'].values() if a['substep'] == sid and a['state'] in ('unknown', 'deferred', 'unanswered')]
            if status == 'reviewed' and unresolved:
                raise ValueError('Resolve the remaining questions or explicitly defer this section.')
            if status == 'reviewed' and unresolved_dependencies(state, sid):
                raise ValueError('Review the related records decision first, or explicitly defer this section.')
            state['reviews'][sid] = dict(state=status, at=stamp(), by=ctx.user_id)
            state['position'] = sid
        elif op == 'capability':
            _keys(data, {'capability', 'decision'})
            if data['capability'] not in contract.CAPABILITIES or data['decision'] not in ('needed', 'off', 'deferred', 'undecided'):
                raise ValueError('Choose an implemented capability and explicit review decision.')
            capability = data['capability']
            state['capabilities'][capability] = data['decision']
            state['position'] = next(sid for sid, spec in contract.SUBSTEPS.items() if spec[3] == capability)
            for sid, spec in contract.SUBSTEPS.items():
                if spec[3] == capability:
                    state['reviews'].pop(sid, None)
                    if data['decision'] in ('off', 'deferred'):
                        state['reviews'][sid] = dict(state=data['decision'], at=stamp(), by=ctx.user_id,
                                                     source='explicit_relevance_choice')
        elif op == 'need':
            _keys(data, {'need', 'selected'})
            if data['need'] not in contract.NEEDS or type(data['selected']) is not bool:
                raise ValueError('Choose a supported optional need explicitly.')
            state['needs'][data['need']] = data['selected']
            sid = contract.NEEDS[data['need']]['substep']
            state['reviews'].pop(sid, None)
            state['position'] = sid
        elif op == 'label':
            _keys(data, {'substep', 'text'})
            if data['substep'] not in contract.SUBSTEPS:
                raise ValueError('Unknown section identity.')
            text = contract.bounded(data['text'], 80, empty=True)
            old = state['labels'].get(data['substep'], {})
            state['labels'][data['substep']] = dict(text=text or contract.SUBSTEPS[data['substep']][2],
                provenance='owner_label', version=old.get('version', 0) + 1, at=stamp())
            state['position'] = data['substep']
        elif op == 'activity':
            _keys(data, {'service_name'})
            name = contract.bounded(data['service_name'], 80)
            services = [s for s in context['cfg'].get('service_types', []) if s.get('name') == name]
            if len(services) != 1:
                raise ValueError('Choose one unambiguous saved activity. Define it on Diary settings first.')
            if any(s['service_name'] == name for s in state['scopes'].values()):
                raise ValueError('This activity already has a stable identity.')
            activity_id = str(uuid4())
            state['scopes'][activity_id] = dict(service_name=name, label=name, registered_at=stamp())
            state['scope_position'] = {'type': 'activity', 'id': activity_id}
        elif op == 'reuse_about':
            _keys(data, {'question'})
            if data['question'] != 'activity.purpose':
                raise ValueError('The business description cannot supply this question meaning.')
            value = contract.bounded(context['config'].get('business_brief'), 2000)
            spec = contract.question('activity.purpose')
            key = key_for(spec['id'], 'organisation', 'organisation')
            if key in state['answers']:
                raise ValueError('An answer is already saved. Review the earlier description and explicitly edit your answer if it changed.')
            _confirmed_answer(state, spec, 'organisation', 'organisation', 'answered', value, ctx.user_id,
                provenance=dict(kind='about_you_owner_review', reference='business_brief',
                                evidence_revision=sha256(value.encode()).hexdigest()))
        elif op == 'import_knowledge':
            _keys(data, {'ids'})
            if not isinstance(data['ids'], list) or not data['ids'] or len(data['ids']) > 30 or any(not isinstance(v, str) for v in data['ids']):
                raise ValueError('Choose the saved Knowledge questions to reuse.')
            candidates = {q['id']: q for q in knowledge_candidates(context['knowledge'])}
            for identity in data['ids']:
                if identity not in candidates:
                    raise ValueError('This Knowledge decision could not be verified.')
                q = candidates[identity]
                target_id = q.get('canonical') or identity
                key = key_for(target_id, 'organisation', 'organisation')
                prior = state['answers'].get(key)
                # Changed sources propose conflicts. Never overwrite an owner answer.
                if prior:
                    if prior['provenance'].get('evidence_revision') != q['evidence_revision']:
                        state['imports'][identity] = dict(status='conflict', evidence_revision=q['evidence_revision'],
                            message='Knowledge changed. Keep the current answer or deliberately edit it after reviewing Knowledge.')
                    continue
                spec = contract.question(target_id) if q.get('canonical') else q
                if not q.get('canonical'):
                    state.setdefault('routed_questions', {})[identity] = {k: v for k, v in q.items() if k not in ('answer', 'state')}
                _confirmed_answer(state, spec, 'organisation', 'organisation', q['state'], q['answer'], ctx.user_id,
                    provenance=dict(kind='knowledge_owner_review', reference=q['source_review'], source_question=identity,
                                    evidence_revision=q['evidence_revision']))
                state['imports'][identity] = dict(status='reused', evidence_revision=q['evidence_revision'], question=target_id)
        elif op in ('propose', 'apply', 'cancel', 'reconcile'):
            state, card_write = proposal_action(ctx, context, state, op, data)
        else:
            raise ValueError('Unknown review operation.')
    except (ValueError, setup_cards.Invalid, TypeError, KeyError) as exc:
        if isinstance(exc, setup_cards.Stale):
            raise HTTPException(409, str(exc)) from None
        # Typed adapters supply bounded public validation messages. KeyError
        # and TypeError must never echo request values.
        raise HTTPException(400, str(exc) if isinstance(exc, ValueError) else 'Invalid review data.') from None
    state['last_action'] = dict(id=str(uuid4()), operation=op, at=stamp(), by=ctx.user_id)
    return save(ctx, before, state, card_write=card_write)


def _keys(data, keys):
    if set(data) != keys:
        raise ValueError('Supply the exact fields for this operation.')


def unresolved_dependencies(state, substep):
    dependencies = set()
    for answer in state['answers'].values():
        if answer['substep'] == substep and answer['state'] != 'not_applicable':
            spec = resolve_question(state, answer['question'])
            dependencies.update(d for d in spec.get('dependencies', [])
                                if state['reviews'].get(d, {}).get('state') not in ('reviewed', 'off'))
    return sorted(dependencies)


def proposal_action(ctx, context, state, operation, data):
    if operation == 'propose':
        _keys(data, {'answer'})
        answer = state['answers'].get(data['answer'])
        if not answer or answer['state'] != 'answered':
            raise ValueError('Choose an answered question.')
        spec = resolve_question(state, answer['question'])
        adapter = spec['adapter']
        if not adapter:
            raise ValueError('This is a fact for review, with no automatic settings consumer. Use the owning form for supported configuration.')
        page, row, field = adapter
        # Enforce current module and settings permissions on every write.
        _page_available(context, page)
        def already_proposed(key, a):
            return any(p.get('answers', {p['answer']: p['answer_revision']}).get(key) == a['revision']
                       and p['status'] in ('proposed', 'applied') for p in state['proposals'].values())
        if already_proposed(data['answer'], answer):
            raise ValueError('This answer already has a pending or applied proposal. Review its receipt before proposing again.')
        current = setup_card_store.load(ctx.tenant_id, page, client=ctx.client)
        revision = (current or {}).get('revision', 0)
        selected = {data['answer']: answer}
        if page == 'invoice_settings':
            # Required first-save/tax/deposit choices must be reviewed together.
            # Gather only explicit, same-scope answers with exact native adapters;
            # the visible preview discloses every effect before any write.
            for key, other in state['answers'].items():
                other_spec = resolve_question(state, other['question'])
                if other['state'] == 'answered' and other['scope_type'] == answer['scope_type'] and other['scope_id'] == answer['scope_id'] and (
                    other_spec['adapter'] and other_spec['adapter'][:2] == ('invoice_settings', 'setting')) and not already_proposed(key, other):
                    selected[key] = other
        changes = []
        for key, a in selected.items():
            _, row_name, native_field = resolve_question(state, a['question'])['adapter']
            fields = {'key': native_field, 'value': a['value']} if row_name == 'setting' else {
                'name': _service_name(a, state, context['cfg']), native_field: a['value']}
            source = {'kind': 'owner_said', 'reference': 'Onboarding answer ' + key + ' revision ' + str(a['revision'])}
            changes.append(dict(op='append', name=row_name, fields=fields, sources={k: source for k in fields}))
        card = setup_cards.change(page, current, revision, 'onboarding-answer', changes)
        old_ids = {v['id'] for v in (current or {}).get('lines', [])}
        line_ids = [v['id'] for v in card['lines'] if v['id'] not in old_ids]
        snapshot = setup_card_routes._snapshot(page, context['cfg'], ctx, card=card)
        preview = setup_card_routes._preview(card, line_ids, snapshot)
        # Store the preview identity at creation. A later preview never silently
        # rebases an earlier proposal over a newer manual save.
        proposal = dict(id=str(uuid4()), answer=data['answer'], answer_revision=answer['revision'],
            answers={key: a['revision'] for key, a in selected.items()},
            page=page, card_id=card['id'], line_ids=line_ids, card_revision=card['revision'],
            settings_version=preview['settings_version'], changes=preview['changes'],
            scope_type=answer['scope_type'], scope_id=answer['scope_id'], status='proposed', created_at=stamp(),
            consequence='Applies the displayed changes only. Booking and payment prerequisites remain enforced.')
        state['proposals'][proposal['id']] = proposal
        return state, dict(page=page, revision=revision, card=card, expected_settings=None, settings_patch=None)
    _keys(data, {'proposal'})
    proposal = state['proposals'].get(data['proposal'])
    if not proposal or proposal['status'] != 'proposed':
        raise ValueError('This proposal is no longer pending. Reload to see its receipt.')
    _page_available(context, proposal['page'])
    page = proposal['page']
    card = setup_card_store.load(ctx.tenant_id, page, client=ctx.client)
    if operation == 'reconcile' and (not card or card['id'] != proposal['card_id']):
        card = setup_card_store.latest_receipt(ctx.tenant_id, page, proposal['card_id'], client=ctx.client)
    if not card or card['id'] != proposal['card_id']:
        raise setup_cards.Stale('The proposal card changed. Reload and create a fresh proposal.')
    revision = card['revision']
    if operation == 'reconcile':
        selected = [v for v in card['lines'] if v['id'] in proposal['line_ids']]
        statuses = {v['status'] for v in selected}
        if len(selected) != len(proposal['line_ids']) or not statuses <= {'applied', 'left'}:
            raise ValueError('The proposal still needs review in its original card.')
        status = 'applied' if statuses == {'applied'} else 'cancelled' if statuses == {'left'} else 'partial'
        proposal.update(status=status, finished_at=stamp(), applied_card_revision=revision,
                        receipt_source='existing_setup_card', line_receipts={v['id']: v['status'] for v in selected})
        return state, None
    if operation == 'cancel':
        selected = [v for v in card['lines'] if v['id'] in proposal['line_ids']]
        if len(selected) != len(proposal['line_ids']) or any(v['status'] not in setup_cards.PENDING | {'applied', 'left'} for v in selected):
            raise setup_cards.Stale('The proposal was changed elsewhere. Reload its receipt.')
        pending = [v['id'] for v in selected if v['status'] in setup_cards.PENDING]
        if not pending:
            raise ValueError('These changes were already handled. Check their saved result.')
        cancelled = setup_cards.change(page, card, revision, 'onboarding-cancel', [{'op': 'remove', 'id': v} for v in pending])
        outcomes = {v['id']: 'left' if v['id'] in pending else v['status'] for v in selected}
        proposal.update(status='partial' if 'applied' in outcomes.values() else 'cancelled', finished_at=stamp(),
                        line_receipts=outcomes)
        return state, dict(page=page, revision=revision, card=cancelled, expected_settings=None, settings_patch=None)
    if revision != proposal['card_revision']:
        raise setup_cards.Stale('The proposal changed. Cancel it and review a fresh difference.')
    for key, expected_revision in proposal.get('answers', {proposal['answer']: proposal['answer_revision']}).items():
        answer = state['answers'][key]
        if answer['revision'] != expected_revision or answer['state'] != 'answered':
            raise setup_cards.Stale('The source answer changed. Review a fresh proposal.')
    snapshot = setup_card_routes._snapshot(page, context['cfg'], ctx, card=card)
    if setup_card_routes._version(snapshot) != proposal['settings_version']:
        raise setup_cards.Stale('Saved settings changed since this proposal. Cancel and preview a fresh difference.')
    accepted, patch = setup_cards.accept(card, revision, 'onboarding-apply', proposal['line_ids'], snapshot, actor=ctx.user_id)
    proposal.update(status='applied', finished_at=stamp(), applied_card_revision=accepted['revision'])
    return state, dict(page=page, revision=revision, card=accepted, expected_settings=snapshot, settings_patch=patch)


def _page_available(context, page):
    if page == 'diary_settings' and 'scheduling' not in context['modules']:
        raise ValueError('Scheduling is unavailable for this account.')
    if page == 'crm_settings' and 'crm' not in context['modules']:
        raise ValueError('Record workflows are unavailable for this account.')


def _service_name(answer, state, cfg):
    if answer['scope_type'] != 'activity':
        raise ValueError('This setting requires an activity identity.')
    scope = state['scopes'].get(answer['scope_id'])
    matches = [s for s in cfg.get('service_types', []) if scope and s.get('name') == scope['service_name']]
    if len(matches) != 1:
        raise ValueError('The saved activity was renamed or removed. Review its identity before applying.')
    return scope['service_name']
