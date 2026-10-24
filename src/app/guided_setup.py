"""Ordered explanations over native settings; cards remain the write authority.

The registry is code-owned. Neither model output nor browser input can choose a
route, selector, substep or execution adapter. Positions are metadata, not saves.
"""
from copy import deepcopy
import json
from app import onboarding_contract as contract, setup_catalogue, settings_recommend

VERSION = 1
ROUTES = {
    'diary_settings': '/app/diary/settings', 'booking_details': '/app/diary/intake',
    'dependants': '/app/diary/dependants', 'crm_fields': '/app/crm/fields',
    'crm_settings': '/app/crm/settings', 'invoice_settings': '/app/invoices/settings',
    'packages': '/app/invoices/settings',
}
PAGE_SUBSTEP = {'booking_details': 'diary.conditions', 'dependants': 'records.relationships',
    'crm_fields': 'records.information', 'crm_settings': 'records.progress',
    'invoice_settings': 'charges.terms', 'packages': 'charges.terms'}
IDENTITY = {'business_name', 'business_address', 'business_email', 'business_phone',
            'show_business_details', 'vat_number', 'company_number', 'business_vat_no', 'business_contact'}
METHODS = {'bank_name', 'bank_account_name', 'bank_sort_code', 'bank_account_number',
           'bank_iban', 'bank_bic', 'payment_instructions', 'payment_methods'}
ACTIVITIES = {'stay_mode', 'enabled', 'auto_confirm', 'confirmation_mode'}
CHARGES = {'stay_rate', 'stay_rate_unit', 'stay_deposit', 'stay_discount', 'late_cancel_hours'}
OPTIONAL = {
    'urgent_response':'Do you offer same-day or out-of-hours services?',
    'repeat_activity':'Do you need repeat bookings?',
    'credit_cancellation':'Do prepaid credits have cancellation rules?',
    'after_job':'Should completing work leave follow-up tasks?',
    'day_hours':'Do opening hours differ by weekday?',
    'party':'Do bookings need a party-size limit?',
    'courses':'Do you offer courses or enrolment with a waiting list?',
    'makeups':'Do you offer replacement sessions?',
    'delivery':'Do you offer delivery with these activities?',
    'packages':'Do you sell prepaid sessions or hours?',
    'wording':'Would you like different labels for bookings and participants?',
}

# Short product explanations, attached to native controls. These never choose
# a value; the card remains the only approval surface.
EXPLANATIONS = {
    'min_notice_hours': 'Notice is the minimum lead time before a new booking can start. It keeps last-minute requests outside the times the assistant can offer.',
    'horizon_days': 'The booking horizon limits how far ahead a new booking can be offered. It does not reserve time or confirm a booking.',
    'working_days': 'These are the weekdays when work may be booked. They can differ from the days you answer messages.',
    'open_time': 'This is the beginning of the bookable day. Individual activity, resource and date restrictions still apply.',
    'close_time': 'This is the end of the bookable day. Review it together with durations and any gap needed between bookings.',
    'timezone': 'Booking dates and times are interpreted in this timezone. Use the timezone in which this work is arranged.',
    'capacity_state': 'Capacity must be known before the assistant can offer availability. Unknown capacity stays unresolved; disabled booking stays off.',
    'capacity_per_window': 'This limits simultaneous capacity for the configured booking model. Separate resource limits and existing commitments still apply.',
    'slot_minutes': 'This controls the spacing of appointment slots. An activity can have its own duration, so slot spacing is not a promise about how long the work takes.',
    'buffer_minutes': 'The buffer leaves a gap between bookings. Check that it covers the preparation or turnaround you described.',
    'stay_mode': 'Booking mode selects appointment times or arrival-to-departure spans. Changing it does not confirm bookings or create capacity.',
    'service': 'Each activity can have its own duration, price and timing requirements. Review the named activity in the card before saving; other activities keep their existing values.',
    'resource': 'A resource describes a limited pool used by activities. Its capacity does not assign a person; team assignment is configured in People.',
    'field': 'This detail is collected by this editor. Its requirement and collection rules determine when it is requested; leaving a suggestion keeps the existing fields.',
    'group': 'This defines the kind of related record you track. Naming a kind does not make every person or booking require one.',
    'payment_terms': 'These terms appear on the documents you issue. They do not collect a payment or authorise a charge.',
    'default_deposit_percent': 'This is the proposed deposit percentage. Its confirmation rule is reviewed separately; this walkthrough takes no payment.',
}


def optional_need(entry):
    key=entry.get('key',entry['name'])
    if entry['page']=='packages':return 'packages'
    if key in ('service_urgent','service_out_of_hours'):return 'urgent_response'
    if key in ('service_after_job','job_done_when'):return 'after_job'
    if key.startswith('dayhours_'):return 'day_hours'
    if key.startswith('party_'):return 'party'
    if key.startswith('recurring_'):return 'repeat_activity'
    if key.startswith('makeup'):return 'makeups'
    if key.startswith('delivery_'):return 'delivery'
    if key in ('credits_spend','hours_unit','no_show_spends','excused_refunds','late_move_charged','late_cancel_hours'):return 'credit_cancellation'
    if key in ('courses_on','waitlist_on','waitlist_notify','waitlist_stage','booked_stage','offer_hours','reenrol_days','reenrol_notify'):return 'courses'
    if key.startswith('noun_'):return 'wording'
    return None


def destination(entry):
    page, key = entry['page'], entry.get('key', entry['name'])
    if page == 'diary_settings':
        return 'diary.activities' if key in ACTIVITIES else 'charges.terms' if key in CHARGES else 'diary.time'
    if page == 'invoice_settings' and key in IDENTITY:
        return 'charges.responsibility'
    if page == 'invoice_settings' and key in METHODS:
        return 'charges.methods'
    if page == 'dependants':
        return 'records.information' if entry['name'] == 'field' else 'records.progress' if entry['name'] == 'ladder' else 'records.relationships'
    return PAGE_SUBSTEP[page]


def registry():
    """Full catalogue accounting, including bounded native-editor fallbacks.

    Composite editors have positional native rows. Until those editors expose
    stable persisted row IDs, project no guessed row: highlight their actual
    region and use the existing stable proposal-line editor beside it.
    """
    result = {}
    for entry in setup_catalogue.entries():
        if entry['page'] not in ROUTES:
            continue  # Knowledge is reviewed in the first four wizard steps.
        sid = destination(entry)
        key = entry.get('key')
        anchor = 'setting.' + key if key else 'editor.' + entry['page']
        result[entry['id']] = dict(id=entry['id'], substep=sid, page=entry['page'],
            href=ROUTES[entry['page']], anchor=anchor, catalogue=[entry['id']],
            title=entry['label'], question=entry['question'], kind='decision',
            adapter='card' if entry['card_can_set'] else 'native_editor',
            projection='scalar' if key and entry['page'] != 'packages' else 'native_editor',
            why=entry['ask_when'], controls=entry['controls'],need=optional_need(entry))
    # The common notice/horizon decision is one small dependency batch.
    a, b = 'diary_settings/min_notice_hours', 'diary_settings/horizon_days'
    result[a].update(title='Booking notice and horizon',
        question='How much notice do you need, and how far ahead can customers book?', catalogue=[a, b],required=True)
    result.pop(b)
    from app import setup_card_invoices
    batches = [sorted(setup_card_invoices.REQUIRED_NEW | {'prices_include_vat'}),
               ['default_deposit_percent', 'deposit_on_confirm']]
    for keys in batches:
        ids = ['invoice_settings/'+key for key in keys]
        first = result[ids[0]]
        first.update(catalogue=ids, title='Document choices' if len(keys)>2 else 'Deposit choices',
            substep='charges.terms',required=len(keys)>2, question='Which of these document choices do you need? We can review each choice together before saving.' if len(keys)>2 else
            'Do you require a deposit, and should it apply when a booking is confirmed?')
        for identity in ids[1:]:
            result.pop(identity)
    # A newly proposed subject kind needs its first detail in the same native
    # acceptance. A may prepare the kind; B reviews that exact dependency batch.
    if 'dependants/field' in result:
        result['dependants/field']['catalogue'].append('dependants/group')
    # Catalogue coverage is not a questionnaire. Review related controls as one
    # owner decision, retaining the native IDs underneath the presentation.
    def batch(page, keys, title, question, why, *, substep=None):
        ids = [page + '/' + key for key in keys if page + '/' + key in result]
        if not ids:
            return
        first = result[ids[0]]
        first.update(title=title, question=question, why=why,
                     catalogue=[item for identity in ids for item in result[identity]['catalogue']])
        if substep:
            first['substep'] = substep
        for identity in ids[1:]:
            result.pop(identity)

    batch('diary_settings', ['working_days', 'open_time', 'close_time', 'timezone'],
          'When work can be booked',
          'Which days and times can work be booked? These may differ from the hours you answer enquiries.',
          'These choices define bookable hours. They do not establish that a person or resource is available.')
    batch('diary_settings', ['capacity_state', 'capacity_per_window'], 'Booking capacity',
          'How many bookings can you accommodate at the same time, or should booking remain off?',
          'Capacity controls what the assistant may offer. Leaving it unknown does not create availability.')
    batch('diary_settings', ['slot_minutes', 'windows', 'buffer_minutes'], 'Timing and gaps',
          'Do people choose an exact time or a time window, and do you need a gap between bookings?',
          'Timing describes the offered appointment; a buffer leaves space between commitments.')
    batch('diary_settings', ['blackout_dates', 'bank_holiday_nation'], 'Dates to keep unavailable',
          'Are there dates or public holidays when bookings should be unavailable?',
          'These dates restrict the bookable calendar without changing your normal hours.')
    batch('diary_settings', ['checkin_after', 'checkout_before', 'stay_min_nights'], 'Arrival and departure',
          'What arrival, departure and minimum-duration rules apply?',
          'These rules describe arrival-to-departure bookings. Review the supported limits before saving.')
    batch('diary_settings', ['stay_rate', 'stay_rate_unit', 'stay_deposit'], 'Charges for a date span',
          'How is an arrival-to-departure booking charged, and is a deposit required?',
          'The rate and its unit are reviewed together so the saved charge has a clear meaning.')
    # Optional topics are discovered once in their owning section. Selecting a
    # topic reveals one related decision rather than a question per database key.
    for need, prompt in OPTIONAL.items():
        for page, owner in sorted({(s['page'], s['substep']) for s in result.values()}):
            matching = [s for s in result.values() if s['page'] == page and s['substep'] == owner and s.get('need') == need]
            if len(matching) > 1:
                batch(page, [s['id'].split('/', 1)[1] for s in matching],
                      {'day_hours': 'Hours by weekday', 'courses': 'Enrolment and waiting lists',
                       'credit_cancellation': 'Credit and cancellation rules', 'delivery': 'Delivery choices',
                       'party': 'Group size', 'repeat_activity': 'Repeated bookings',
                       'makeups': 'Replacement sessions', 'wording': 'Your booking labels',
                       'packages': 'Prepaid balances'}.get(need, matching[0]['title']),
                      'What rules should apply to this part of your work?',
                      'Only the choices you supply will be proposed. We will review their effects before saving.')
    for category, title, question in ((IDENTITY, 'Details on documents',
            'Which business details should appear on your quotes and invoices?'),
            (METHODS, 'How people can pay', 'Which supported payment methods do you want to show?')):
        batch('invoice_settings', [s['id'].split('/', 1)[1] for s in list(result.values())
              if s['page'] == 'invoice_settings' and s['id'].split('/', 1)[1] in category],
              title, question, 'Review the details you choose to display. No payment is taken by this setup step.')
    if 'diary_settings/stay_mode' in result:
        result['diary_settings/stay_mode'].update(title='How bookings use time', required=True,
            question='Does a booking use an appointment time, or an arrival-to-departure span?',
            why='This selects the existing booking layout. It does not grant capacity or permission to confirm bookings.')
    if 'diary_settings/service' in result:
        result['diary_settings/service'].update(substep='diary.activities', title='The activities you arrange',
            question='Which activities should the diary handle? We can review their duration, timing and resource needs together.')
    return result


# Diary discovery stops (reports/diary-discovery-spec-2026-10-24.md, Parts 1–4):
# answers first, settings after. They stay on the real Diary settings page.
DISCOVERY = ('diaries', 'diary_fact', 'hours', 'profile')
DIARY_HREF = '/app/diary/settings'
DIARIES_STOP, HOURS_STOP, PROFILE_STOP = 'diary.discover.diaries', 'diary.ask.hours', 'diary.profile'
DIARY_TEXT = {
    'diary.example': 'Give me one example of something that goes in {label}.',
    'diary.starter': 'Who puts things in {label}?',
    'diary.date_source': 'Which date should {label} follow?',
    'diary.occupies': 'Does {label} take up someone’s time or somewhere’s space?',
    'diary.capacity': 'How many {label} can happen at the same time?',
    'diary.time_shape': 'How is the time set for {label}?',
    'diary.length': 'Roughly how long does one take?',
    'diary.repeats': 'Does {label} repeat?',
    'diary.repeats_detail': 'What does the repeat depend on?',
    'diary.agreement': 'Does anyone outside your team need to agree to the time?',
    'diary.agreement_who': 'Who needs to agree?',
}


def discovery_stops(context):
    from app import diary_profile
    state = context['state']
    stops = [dict(id=DIARIES_STOP, kind='diaries', substep='diary.activities', title='Your diaries',
        question='What do you keep a diary or calendar for? Pick any that fit, or tell me in your own words.',
        why='Each one becomes its own diary layer. I’ll ask a few short questions about each before suggesting any settings.',
        href=DIARY_HREF, anchor=None, question_id='diary.list', scopes=['organisation'], required=True)]
    for diary in contract.active_diaries(state):
        for question in diary_profile.applicable(state, diary['id']):
            spec = contract.question(question)
            stops.append(dict(id='diary.ask.'+diary['id']+'.'+question.split('.', 1)[1], kind='diary_fact',
                substep=spec['substep'], title=diary['label'], question=DIARY_TEXT[question].format(label=diary['label']),
                question_id=question, diary=diary['id'], scopes=['diary'], answer_kind=spec['kind'],
                choices=diary_profile.choices(state, diary['id'], question), why=spec['why'],
                href=DIARY_HREF, anchor=None, required=True))
    hours = contract.question('diary.hours')
    stops.append(dict(id=HOURS_STOP, kind='hours', substep='diary.time', title='Your hours',
        question=hours['text'], why=hours['why'], question_id='diary.hours', scopes=['organisation'],
        answer_kind='hours', href=DIARY_HREF, anchor=None, required=True))
    stops.append(dict(id=PROFILE_STOP, kind='profile', substep='diary.activities', title='Your diaries',
        question='Here is how I understand your diaries. Is that right?',
        why='Nothing is set up until you confirm. Settings questions follow only for diaries that can be set up.',
        question_id='diary.profile', scopes=['organisation'], href=DIARY_HREF, anchor=None, required=True))
    return stops


def _answer(state, question, scope_type='organisation', scope_id='organisation'):
    return state['answers'].get(contract.answer_key(question, scope_type, scope_id))


def discovery_cursor(state):
    """Where discovery continues: the first open question, then hours, then the profile."""
    from app import diary_profile
    listed = _answer(state, 'diary.list')
    if not listed or listed['state'] == 'unanswered':
        return DIARIES_STOP
    for diary in contract.active_diaries(state):
        question = diary_profile.next_question(state, diary['id'])
        if question:
            return 'diary.ask.'+diary['id']+'.'+question.split('.', 1)[1]
    hours = _answer(state, 'diary.hours')
    if not hours or hours['state'] == 'unanswered':
        return HOURS_STOP
    return PROFILE_STOP


def discovery_counts(state, answer):
    """Whether a discovery answer still counts: a removed diary's answers, or an
    answer to a question its diary no longer asks, cannot block a review."""
    if answer.get('scope_type') != 'diary':
        return True
    from app import diary_profile
    diary = state.get('diaries', {}).get(answer['scope_id'])
    return bool(diary) and diary['status'] != 'removed' and \
        answer['question'] in diary_profile.applicable(state, answer['scope_id'])


def profile_flags(context):
    """What a confirmed diary profile asks the settings walk to show (spec §5.6)."""
    from app import diary_profile
    state = context['state']
    confirmed = _answer(state, 'diary.profile')
    if not confirmed or confirmed['state'] != 'answered':
        return None
    ready = []
    for diary in contract.active_diaries(state):
        result = diary_profile.support(state, diary['id'], context['modules'])
        if result['state'] == 'ready' and result['kind'] == 'diary':
            ready.append(diary_profile.values(state, diary['id']))
    return dict(bookable=any(v['diary.starter'] in ('customers', 'both') for v in ready),
                timed=bool(ready),
                days=any(v['diary.time_shape'] == 'days' for v in ready),
                capacity=any(v['diary.capacity'] for v in ready))


# Settings stops a diary arranged by the business still needs. Booking rules,
# courses, credits and similar customer-booking controls need a bookable diary.
TIMED_STOPS = {'diary_settings/working_days', 'diary_settings/capacity_state', 'diary_settings/resource',
               'diary_settings/slot_minutes', 'diary_settings/blackout_dates', 'diary_settings/service'}


def settings_allowed(stop, flags):
    if flags is None or stop['page'] != 'diary_settings' or stop['substep'] not in ('diary.activities', 'diary.time'):
        return True
    if flags['bookable']:
        return True
    if stop['id'] == 'diary_settings/stay_mode':
        return flags['timed'] and flags['days']
    return flags['timed'] and stop['id'] in TIMED_STOPS


def orientation(context, substep):
    """The real diary, shown once Diary is set up rather than empty at the start."""
    office = 'scheduling' in context['modules']
    return dict(id='orientation.diary', kind='orientation', substep=substep,
        title='Your diary' if office else 'Your available diary settings',
        question='Here is your diary. As real work arrives it appears here, in the diaries you described. Looking around changes nothing.' if office else
            'The appointment diary is not available on this account. Your diary answers are kept, and unavailable choices remain unresolved.',
        href='/app/diary' if office else DIARY_HREF, anchor='orientation.diary' if office else None)


def coverage():
    return [dict(identity=identity, stop=stop['id'], substep=stop['substep'],
        adapter=stop['adapter'], anchor=stop['anchor'], href=stop['href'])
        for stop in registry().values() for identity in stop['catalogue'] if identity==stop['id'] or identity not in registry()]


def neutral():
    return dict(version=VERSION, content_version=VERSION, status='active', intro_ack=False,
        current='intro', explained={}, decisions={}, pending={}, scope_type='organisation',
        scope_id='organisation', question_revision=0)


def read(state):
    guide = state.get('guide')
    if not guide:
        return None
    if not isinstance(guide, dict) or guide.get('version') != VERSION:
        raise ValueError('This guided setup needs a supported version. Use the grouped review.')
    result=deepcopy(guide)
    for stop,pending in result.get('pending',{}).items():
        proposal=state['proposals'].get(pending.get('proposal'),{})
        if proposal.get('status') in ('applied','cancelled','partial'):
            result['decisions'][stop]=proposal['status']
    return result


def steps(context):
    state = context['state']
    subs = [s for group in contract.group_steps(context['modules'], context['cfg'], state, context['groups'])
            for s in group['substeps']]
    rows = registry()
    result = [dict(id='intro', kind='intro', title='Set up the app together',
        question='Next we’ll set up the app together. I’ll explain each area, ask about what you need, and show suggestions on its real page. Accept saves the displayed changes; Next only moves the guide.',
        href='/app/setup/guide', anchor=None, substep=None)]
    guide = read(state) or neutral()
    seen_capabilities=set()
    seen_needs=set()
    flags = profile_flags(context)
    last_diary = ([s['id'] for s in subs if s['id'].startswith('diary.')] or [None])[-1]
    for sub in subs:
        sid = sub['id']
        if sid == 'diary.activities':
            # Discovery replaces the empty-diary orientation and the yes/no time
            # gate: the confirmed profile decides time_occupancy instead.
            result.extend(discovery_stops(context))
        if sid == 'charges.responsibility':
            result.append(dict(id='orientation.money', kind='orientation', substep=sid, title='Your money',
                question='Quotes, invoices and payments appear here. We’ll review the relevant charging choices next. This guide creates no invoices or payments.',
                href='/app/money', anchor='orientation.money'))
        if sub['capability'] not in seen_capabilities and sub['capability'] != 'time_occupancy':
            result.append(dict(id='relevance.'+sid, kind='relevance', substep=sid,
                title=sub['label'], question=contract.CAPABILITIES[sub['capability']],
                href=sub['links'][0][0] if sub['links'] else '/app/setup/'+sid.split('.')[0],
                anchor=None, capability=sub['capability']))
            seen_capabilities.add(sub['capability'])
        if state['capabilities'].get(sub['capability']) in ('off', 'deferred'):
            continue
        candidates = [deepcopy(s) for s in rows.values() if s['substep'] == sid]
        question_specs = list(contract.QUESTIONS.values()) + [q for identity, q in state.get('routed_questions', {}).items()
                                                             if identity.startswith('knowledge.')]
        facts = [dict(id='question.'+q['id'], question_id=q['id'], kind='fact', title=sub['label'],
            question=q['text'], substep=sid, href=sub['links'][0][0].split('#')[0] if sub['links'] else '/app/setup/'+sid.split('.')[0],
            anchor=None, scopes=q['scopes'],required=True,answer_kind=q['kind'],choices=q.get('choices'))
            for q in question_specs if q['substep']==sid and not q['adapter'] and not q.get('discovery')
            and contract.need_selected(state,q.get('need'))]
        # CRM controls and diary editors remain behind their actual module gates.
        candidates = [s for s in candidates if not (s['page'].startswith('crm_') and 'crm' not in context['modules'])
                      and not (s['page'] in ('diary_settings', 'booking_details') and
                               not {'scheduling', 'tms', 'fsm'} & context['modules'])
                      and not (s['page'] == 'dependants' and not contract.subjects_available(context['modules']))
                      and settings_allowed(s, flags)]
        filtered=[]
        for stop in candidates:
            need=stop.get('need')
            if need:
                if need not in seen_needs:
                    filtered.append(dict(id='need.'+need,kind='need',need=need,substep=sid,title=sub['label'],
                        question=OPTIONAL[need],href=stop['href'],anchor=None,required=True))
                    seen_needs.add(need)
                selected=state['needs'].get(need) if need in contract.NEEDS else guide.get('needs',{}).get(need)
                if selected is not True:
                    continue
            # Overnight-only choices follow a saved or exact owner-selected
            # booking mode, never a business description or contact hours.
            key=stop['id'].split('/')[-1]
            if key != 'stay_mode' and (key.startswith('stay_') or key in ('checkin_after','checkout_before')):
                from app import intake
                chosen=state['answers'].get(__import__('app.onboarding_state',fromlist=['key_for']).key_for(
                    'setting.diary_settings.stay_mode','organisation','organisation'),{})
                if not intake.stay_mode(context['cfg']) and chosen.get('value') is not True:
                    continue
            stop['decision'] = guide['decisions'].get(stop['id'])
            filtered.append(stop)
            pending = guide['pending'].get(stop['id'])
            if pending:
                proposal = state['proposals'].get(pending['proposal'], {})
                # Explain the exact represented choices, never the whole
                # catalogue. One stable stop per affected native control.
                represented = {state['answers'].get(k, {}).get('question')
                               for k in proposal.get('answers', {})}
                for identity in stop['catalogue']:
                    entry = setup_catalogue.lookup(identity)
                    key = entry.get('key', entry['name'])
                    explanation_id='explain.'+(stop['id']+':'+key if identity!=stop['id'] and identity in rows else identity)
                    if entry['name'] == 'setting' and 'setting.'+entry['page']+'.'+key not in represented and guide['current']!=explanation_id:
                        continue
                    filtered.append(dict(id=explanation_id, kind='explanation', decision_id=stop['id'],
                        substep=sid, page=stop['page'], href=stop['href'], title=entry['label'],
                        question=EXPLANATIONS.get(key, 'Review '+entry['label'].lower()+' here. The card shows the exact proposed value and its difference from the current setting. Ask the assistant if its effect is unclear before accepting.'),
                        anchor='setting.'+key if entry.get('key') else 'editor.'+entry['page'],
                        catalogue=stop['catalogue'], focus_catalogue=identity))
        # A need-gated fact is asked right after its need stop, never before it.
        gated = [f for f in facts if contract.QUESTIONS.get(f['question_id'], {}).get('need')]
        ordered = [f for f in facts if f not in gated]
        for stop in filtered:
            ordered.append(stop)
            if stop['kind'] == 'need':
                ordered += [f for f in gated if contract.QUESTIONS[f['question_id']]['need'] == stop['need']]
        ordered += [f for f in gated if f not in ordered]
        result.extend(ordered)
        result.append(dict(id='review.'+sid, kind='review', substep=sid, title='Review '+sub['label'],
            question='Review the saved results and anything left for later. Continuing records your review; it does not make booking or payments ready.',
            href=candidates[-1]['href'] if candidates else result[-1]['href'], anchor=None,
            capability=sub['capability']))
        if sid == last_diary:
            result.append(orientation(context, sid))
    if last_diary and not any(s['id'] == 'orientation.diary' for s in result):
        # The last Diary section was skipped (its capability is off or deferred).
        index = max(i for i, s in enumerate(result) if str(s.get('substep') or '').startswith('diary.')) + 1
        result.insert(index, orientation(context, last_diary))
    result.append(dict(id='finished', kind='finished', substep=None, title='Your setup review',
        question='Your choices and deferred items are kept. You can revisit the grouped review at any time. Drafting stays under its separate explicit control.',
        href='/app/setup/' + (subs[0]['id'].split('.')[0] if subs else 'charges'), anchor=None))
    return result


def describe(context):
    guide = read(context['state'])
    if not guide:
        return dict(revision=context['state']['revision'], guide=None, start_href='/app/setup/guide')
    sequence = steps(context)
    stop = next((s for s in sequence if s['id'] == guide['current']), None)
    if stop is None and guide['current'].startswith('diary.'):
        # A changed answer can drop a discovery question; continue where discovery is.
        stop = next((s for s in sequence if s['id'] == discovery_cursor(context['state'])), None)
    if stop is None:
        # A removed module/decision cannot be silently certified as complete.
        stop = sequence[0]
    index = sequence.index(stop)
    groups = contract.group_steps(context['modules'], context['cfg'], context['state'], context['groups'])
    section = next((dict(group=g['label'], label=sub['label'], badge=contract.SUBSTEPS[sub['id']][1])
                    for g in groups for sub in g['substeps'] if sub['id']==stop.get('substep')), None)
    decision_id = stop.get('decision_id', stop['id'])
    unresolved=unresolved_required(context,stop['substep']) if stop['kind']=='review' else []
    return dict(section=section, progress=[dict(key=g['key'], done=g['done']) for g in groups],
        unresolved=[dict(id=s['id'],title=s['title']) for s in sequence if s['id'] in unresolved],
        remaining_sections=sum(context['state']['reviews'].get(sub['id'],{}).get('state') not in ('reviewed','off','deferred')
                               for group in groups for sub in group['substeps']),
        revision=context['state']['revision'], guide=guide, stop=stop,
        knowledge_conflicts=knowledge_conflicts(context['state'],stop),
        index=index, count=len(sequence), pending=(dict(guide['pending'][decision_id],
            settings_version=context['state']['proposals'][guide['pending'][decision_id]['proposal']]['settings_version'])
            if decision_id in guide['pending'] else None),
        answers=[dict(key=k, **a) for k, a in context['state']['answers'].items() if a['scope_type']==guide['scope_type']
            and a['scope_id']==guide['scope_id'] and a['state']=='answered' and (a['question']==stop.get('question_id') or
            ('/'.join((a['question'].split('.')[1], a['question'].split('.')[2])) if a['question'].startswith('setting.') else '') in stop.get('catalogue', []))],
        scopes=[dict(type='organisation',id='organisation',label='Whole business')]+
            [dict(type='activity',id=k,label=v['label']) for k,v in context['state']['scopes'].items()]+
            [dict(type='record_group',id=str(g['id']),label=g['label']) for g in context['groups']],
        summary_href='/app/setup/'+(stop.get('substep') or 'charges.terms').split('.')[0],
        discovery=discovery_view(context, guide, stop) if stop['kind'] in DISCOVERY else None)


def discovery_view(context, guide, stop):
    """Everything the panel needs to render one discovery stop."""
    from app import diary_profile
    state = context['state']
    saved = guide.get('diary') or {}
    date_sources = saved.get('date_sources', [])
    scope = ('diary', stop['diary']) if stop['kind'] == 'diary_fact' else ('organisation', 'organisation')
    found = _answer(state, stop['question_id'], *scope)
    prefill = (saved.get('prefills') or {}).get(scope[1], {}).get(stop['question_id'])
    if not prefill and stop['kind'] == 'hours':
        prefill = (saved.get('context_prefills') or {}).get('diary.hours')
    if not prefill and stop['kind'] == 'diary_fact' and stop['question_id'] == 'diary.date_source':
        seed = state['diaries'][stop['diary']]['seed'].get('date_source')
        if seed:
            prefill = dict(value=seed, words=None, source='suggestion')
    if prefill and found and found['state'] == 'answered' and found['value'] == prefill.get('value'):
        prefill = None
    return dict(
        diaries=[dict(id=d['id'], label=d['label'], label_provenance=d['label_provenance'])
                 for d in contract.active_diaries(state)],
        limit=contract.DIARY_LIMIT, suggestions=saved.get('suggestions', []),
        proposed_labels=saved.get('proposed_labels', []),
        answer=dict(state=found['state'], value=found['value']) if found else None,
        prefill=prefill,
        date_sources=[dict(id=d['id'], label=d['label']) for d in date_sources] + [
            dict(id='not_recorded', label='A date we don’t record yet')],
        profile=diary_profile.profile(state, context['modules'], date_sources) if stop['kind'] in ('profile', 'diaries') else None,
        gaps=saved.get('gaps', []))


def current(context, identity):
    description = describe(context)
    if not description.get('guide') or description['stop']['id'] != identity:
        raise ValueError('The current guide decision changed. Reload before continuing.')
    return description['guide'], description['stop']


def knowledge_conflicts(state,stop):
    result=[]
    for identity,entry in state['imports'].items():
        if entry.get('status')!='conflict':
            continue
        question=entry.get('question','')
        belongs=question==stop.get('question_id') or (question.startswith('setting.') and
            '/'.join(question.split('.')[1:]) in stop.get('catalogue',[]))
        if not belongs:
            continue
        result.append(dict(id=identity,question=question,message=entry['message'],earlier=entry['earlier'],
            candidate=entry.get('candidate')))
    return result


def unresolved_required(context, substep):
    """Absence of an answer is unresolved, even after every explanation was seen."""
    state=context['state'];progress=read(state) or neutral()
    capability=contract.SUBSTEPS[substep][3]
    if state['capabilities'].get(capability) not in ('needed','off','deferred'):
        return ['relevance.'+substep]
    missing=[]
    for stop in steps(context):
        if stop.get('substep')==substep and knowledge_conflicts(state,stop):
            missing.append(stop['id'])
            continue
        if stop.get('substep')!=substep or not stop.get('required'):
            continue
        decision=progress['decisions'].get(stop['id'])
        if decision in ('applied','cancelled','partial','not_applicable','reviewed_unchanged','needed','off'):
            continue
        if stop['kind']=='fact':
            answer=state['answers'].get(__import__('app.onboarding_state',fromlist=['key_for']).key_for(
                stop['question_id'],progress['scope_type'],progress['scope_id']))
            if answer and answer['state'] in ('answered','not_applicable'):
                continue
        if stop['kind'] in DISCOVERY:
            answer=_answer(state,stop['question_id'],*(('diary',stop['diary']) if stop['kind']=='diary_fact' else ()))
            if answer and answer['state'] in ('answered','not_applicable'):
                continue
        missing.append(stop['id'])
    return missing


def model_context(context, stop):
    return ('GUIDED SETUP: only the current small decision is being discussed. Ask one question, clarify ambiguity, '
        'and reuse exact saved answers. Product or unrelated questions do not advance the guide. Do not invent values. '
        'Use setup_changes for explicit choices within CURRENT CATALOGUE. If the owner explicitly supplies a later-area answer, '
        'hold its exact supported row with held=true and keep the current question. Never infer a later answer from a product question. '
        'No links, selectors or actions from prose. Do not repeat applied or left lines. Current question: '+stop['question']+'\n'
        'CURRENT CATALOGUE: '+str(stop.get('catalogue', []))+'\n'
        'Scope: '+str((read(context['state']) or neutral())['scope_type'])+'/'+str((read(context['state']) or neutral())['scope_id'])+'. Existing scoped answers: '+
        json.dumps(model_data(context,stop), ensure_ascii=False))


def model_data(context,stop):
    state=context['state'];progress=read(state) or neutral()
    data=contract.model_context(state)
    candidates=[a for a in state['answers'].values() if a['scope_type']==progress['scope_type'] and a['scope_id']==progress['scope_id']
                and a['substep']==stop.get('substep')]
    candidates.sort(key=lambda a:a['question']!=stop.get('question_id'))
    data['answers']=[]
    for answer in candidates:
        item={k:deepcopy(answer[k]) for k in ('question','scope_type','scope_id','state','value')}
        item['provenance']={k:v for k,v in answer['provenance'].items() if k in ('kind','reference','card_id','card_revision')}
        if len(json.dumps(data['answers']+[item],ensure_ascii=False))>24000:
            break
        data['answers'].append(item)
    data['omitted_answers']=len(candidates)-len(data['answers'])
    data['relevant_question_templates']=[q for q in data['relevant_question_templates'] if q['substep']==stop.get('substep')]
    return data
