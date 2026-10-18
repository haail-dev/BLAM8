"""Durable, draft-first scheduled occurrences over the existing business actions."""
from __future__ import annotations

import calendar
import logging
import uuid
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from app import agent, db, disclosure, mailbox, people, sms, trial, usage
from app import tenant_outreach as outreach

log = logging.getLogger(__name__)
KINDS = {'email': 'Email', 'sms': 'SMS', 'quote': 'Quote', 'invoice': 'Invoice',
         'booking': 'Booking / job hold', 'task': 'Task / reminder',
         'followup': 'Follow-up task', 'outreach': 'Outreach batch'}
DEFAULTS = {k: v for k, v in outreach.DEFAULTS.items() if k != 'mode'}
DEFAULTS['min_gap_seconds'] = 60


def now():
    return datetime.now(timezone.utc)


def parse(value):
    result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('A timezone is required.')
    return result


def local_time(value, zone):
    """Choose the first repeated autumn hour; reject nonexistent spring times."""
    wall = datetime.fromisoformat(value)
    if wall.tzinfo:
        raise ValueError('Enter a local date and time.')
    dt = wall.replace(tzinfo=ZoneInfo(zone), fold=0)
    if dt.astimezone(timezone.utc).astimezone(dt.tzinfo).replace(tzinfo=None) != wall:
        raise ValueError('That time is skipped by the daylight-saving clock change.')
    return dt.astimezone(timezone.utc)


def config(raw):
    return {k: v for k, v in outreach.validate_config({'mode': 'draft', **raw}).items() if k != 'mode'}


def recurrence(raw):
    freq = raw.get('frequency', 'once')
    interval = int(raw.get('interval', 1))
    if freq not in ('once', 'daily', 'weekly', 'monthly', 'custom') or not 1 <= interval <= 365:
        raise ValueError('Choose a valid repeat interval.')
    days = sorted(set(int(d) for d in raw.get('days', [])))
    if freq == 'custom' and (not days or any(d not in range(7) for d in days)):
        raise ValueError('Choose the repeat weekdays.')
    until = raw.get('until') or None
    if until:
        date.fromisoformat(until)
    return {'frequency': freq, 'interval': interval, 'days': days, 'until': until}


def next_occurrence(item):
    rule = recurrence(item.get('recurrence') or {})
    freq, step = rule['frequency'], rule['interval']
    if freq == 'once':
        return None
    prev = parse(item['next_at']).astimezone(ZoneInfo(item['timezone']))
    day = prev.date()
    if freq == 'monthly':
        month = day.year * 12 + day.month - 1 + step
        year, month = divmod(month, 12)
        anchor = int(item['recurrence'].get('anchor_day', day.day))
        day = date(year, month + 1, min(anchor, calendar.monthrange(year, month + 1)[1]))
    elif freq == 'custom':
        for offset in range(1, step * 7 + 8):
            candidate = day + timedelta(days=offset)
            weeks = (candidate - (day - timedelta(days=day.weekday()))).days // 7
            if weeks % step == 0 and candidate.weekday() in rule['days']:
                day = candidate
                break
    else:
        day += timedelta(days=step * (7 if freq == 'weekly' else 1))
    if rule['until'] and day > date.fromisoformat(rule['until']):
        return None
    # Keep the wall-clock rhythm. Move a nonexistent recurring hour forward
    # by its DST gap; fold=0 ensures the repeated hour runs only once.
    wall_time = time.fromisoformat(item['recurrence'].get('anchor_time', prev.strftime('%H:%M:%S')))
    dt = datetime.combine(day, wall_time).replace(tzinfo=prev.tzinfo, fold=0)
    return dt.astimezone(timezone.utc).isoformat()


def settings(tid, client=None):
    rows = outreach.table('schedule_settings', tid, client).limit(1).execute().data or []
    return rows[0] if rows else {'enabled': False, 'config': DEFAULTS.copy()}


def rpc(name, **kwargs):
    return db.db().rpc(name, {'p_' + k: v for k, v in kwargs.items()}).execute().data


def window(cfg, at=None):
    local = (at or now()).astimezone(ZoneInfo(cfg['timezone']))
    return (local.weekday() in cfg['working_days'] and local.date().isoformat() not in cfg['holidays']
            and cfg['start_time'] <= local.strftime('%H:%M') < cfg['end_time'])


def access(tid, user_id):
    tenant = db.get_tenant(tid)
    if not tenant or not trial.is_active(tenant):
        raise ValueError('Business access has expired.')
    member = db.get_member(tid, user_id)
    if not member or not member.get('active', True) or not people.is_admin(people.member(member)):
        raise ValueError('The planner owner no longer has permission.')


def contact(tid, cid):
    row = db.crm_get_contact(cid, tid) if cid else None
    if not row:
        raise ValueError('Choose a contact from this business.')
    if row.get('do_not_sequence'):
        raise ValueError('This contact is marked do not contact.')
    address = (row.get('email') or '').strip().lower()
    if address:
        if outreach.rows('outreach_suppressions', tid, order='email', email=address):
            raise ValueError('This contact has a permanent outreach stop.')
        if outreach.rows('ignored_contacts', tid, order='address', address=address):
            raise ValueError('This contact is ignored.')
        # Duplicate CRM rows must not circumvent a stop.
        if any(r.get('do_not_sequence') for r in outreach.rows('crm_contacts', tid, email=address)):
            raise ValueError('This address is marked do not contact.')
    return row


def dependencies(tid, item):
    dep = item.get('dependency') or {}
    if dep.get('kind') == 'after_payment':
        invoice = db.get_invoice(dep['invoice_id'], tid)
        if not invoice or invoice.get('status') != 'paid':
            raise ValueError('Waiting for payment.')
    elif dep.get('kind') == 'if_no_reply':
        target = contact(tid, item['payload'].get('contact_id'))
        logs = db.db().table('email_log').select('id').eq('tenant_id', tid).eq('kind', 'inbound')\
            .ilike('from_address', target['email']).gte('created_at', dep['since']).limit(1).execute().data
        if logs:
            raise ValueError('A reply has arrived; review this occurrence.')


def validate_item(tid, raw, user_id):
    kind, mode = raw.get('kind'), raw.get('mode', 'draft')
    if kind not in KINDS or mode not in ('draft', 'auto'):
        raise ValueError('Choose an action and mode.')
    if mode == 'auto' and kind not in ('email', 'sms'):
        raise ValueError('This action needs approval; automatic mode is available for messages only.')
    title = str(raw.get('title') or '').strip()
    if not 1 <= len(title) <= 150:
        raise ValueError('Give the item a title of up to 150 characters.')
    zone = raw.get('timezone', 'Europe/London'); ZoneInfo(zone)
    due = local_time(raw['run_at'], zone)
    rule = recurrence(raw.get('recurrence') or {})
    rule['anchor_day'] = due.astimezone(ZoneInfo(zone)).day
    rule['anchor_time'] = due.astimezone(ZoneInfo(zone)).strftime('%H:%M:%S')
    if rule['until'] and rule['until'] < due.astimezone(ZoneInfo(zone)).date().isoformat():
        raise ValueError('Repeat end must be on or after the first date.')
    if raw.get('catch_up', 'run_late') not in ('run_late', 'skip'):
        raise ValueError('Choose a catch-up policy.')
    payload = dict(raw.get('payload') or {})
    if len(str(payload)) > 12000:
        raise ValueError('The instruction is too long.')
    dep = dict(raw.get('dependency') or {})
    if dep.get('kind') not in (None, '', 'after_payment', 'if_no_reply'):
        raise ValueError('Choose a supported dependency.')
    if dep.get('kind') == 'after_payment' and not db.get_invoice(dep.get('invoice_id'), tid):
        raise ValueError('Choose an invoice from this business.')
    if dep.get('kind') == 'if_no_reply':
        contact(tid, payload.get('contact_id'))
        dep['since'] = parse(dep.get('since') or now().isoformat()).isoformat()
    item = {'tenant_id': tid, 'title': title, 'kind': kind, 'mode': mode,
            'next_at': due.isoformat(), 'timezone': zone, 'recurrence': rule,
            'catch_up': raw.get('catch_up', 'run_late'), 'payload': payload,
            'dependency': dep, 'created_by': user_id}
    inspect_action(tid, item)  # validate IDs, modules and recipient without composing
    return item


def inspect_action(tid, item):
    kind, p = item['kind'], item['payload']
    if kind in ('email', 'sms', 'task', 'followup'):
        target = contact(tid, p.get('contact_id'))
        if kind in ('task', 'followup'):
            if not db.module_active(tid, 'crm'):
                raise ValueError('CRM is not active.')
            return {'contact_id': target['id'], 'title': item['title'], 'description': p.get('brief', ''),
                    'due_date': parse(item['next_at']).astimezone(ZoneInfo(item['timezone'])).date().isoformat()}
        if not str(p.get('brief', '')).strip():
            raise ValueError('Write a message brief.')
        if kind == 'email':
            identity = outreach.identity(tid, p.get('mailbox_id'))
            return {'to': outreach.email(target['email']), 'mailbox_id': identity['id'], 'sender': identity['sender'],
                    'subject': str(p.get('subject') or item['title'])[:200]}
        phone = sms.normalize_e164(p.get('phone') or target.get('phone'))
        # SMS destination must be the selected contact's stored number.
        stored = sms.normalize_e164(target.get('phone'))
        if not phone or phone != stored or not db.sms_connection(tid) or db.sms_opted_out(tid, phone):
            raise ValueError('Choose a connected SMS number and a contact who has not opted out.')
        return {'to': phone, 'subject': item['title'], 'mailbox_id': None}
    if kind in ('quote', 'invoice'):
        from app import concierge_actions as actions
        operation = p.get('document_action', 'send')
        if operation not in ('create', 'send'):
            raise ValueError('Choose whether to create a draft document or send an existing one.')
        if operation == 'create':
            target = contact(tid, p.get('contact_id'))
            action = {'kind': kind + '_create', 'contact': target['email'],
                      'title': p.get('description') or item['title'], 'amount': p.get('amount'),
                      ('due_date' if kind == 'invoice' else 'valid_until'): p.get('document_date', '')}
        else:
            action = {'kind': kind + '_send', kind + '_id': p.get('document_id')}
        try:
            summary, payload, details, body = actions._run_stager(action['kind'], tid, None, action, None)
        except actions.ActionError as exc:
            raise ValueError(str(exc)) from exc
        recipient = payload.get('to') or payload.get('contact')
        found = db.crm_contact_by_email(tid, recipient)
        if not found:
            raise ValueError('Add the document recipient to CRM before scheduling.')
        contact(tid, found['id'])
        return {'action': action, 'payload': payload, 'summary': summary, 'details': details, 'body': body, 'to': recipient}
    if kind == 'booking':
        from app import scheduling
        if not db.module_active(tid, 'scheduling'):
            raise ValueError('Scheduling is not active.')
        target = contact(tid, p.get('contact_id'))
        day = date.fromisoformat(p.get('booking_date', ''))
        cfg = db.get_scheduling_config(tid) or {}
        # Specialised resource diaries must use their own booking flow.
        if cfg.get('windows') not in ('ampm', 'day') or any(db.module_active(tid, m) for m in ('tms', 'assets', 'practice', 'fsm')):
            raise ValueError('Create this resource booking in the diary first; this planner supports window holds.')
        err = scheduling.hold_error(cfg, day, p.get('window', ''), now())
        if err:
            raise ValueError('The booking cannot be held: ' + err)
        return {'booking_date': day.isoformat(), 'window': p['window'], 'contact': target['email'], 'summary': item['title']}
    campaign = outreach.get('outreach_campaigns', tid, p.get('campaign_id'))
    if not campaign:
        raise ValueError('Choose an outreach campaign from this business.')
    return {'campaign_id': campaign['id'], 'summary': 'Add eligible contacts to ' + campaign['name']}


async def prepare(run):
    item, tid = run['snapshot'], run['tenant_id']
    setting = settings(tid)
    current = outreach.get('schedule_items', tid, run['item_id'])
    if not setting['enabled'] or not window(setting['config']):
        raise ValueError('Plan paused or outside working window.')
    if current['status'] != 'active' or current['revision'] != item['revision']:
        raise ValueError('Item paused or changed.')
    access(tid, item['created_by'])
    dependencies(tid, item)
    data = inspect_action(tid, item)
    if item['kind'] in ('email', 'sms'):
        cfg = db.effective_config(tid, data.get('mailbox_id'))
        result = await agent.compose_outbound(cfg, 'A scheduled message requested by the business owner. '
                'Use only the supplied facts. Do not invent bookings, prices or commitments.'
                + (' Write a short plain SMS.' if item['kind'] == 'sms' else ''),
                item['payload']['brief'], tenant_id=tid, log_operation='scheduled_plan')
        data['body'] = result.text
        usage.record_anthropic(tid, result.model, result.usage, operation='scheduled_plan')
    if not rpc('schedule_prepare', tenant=tid, run=run['id'], token=run['token'], data=data):
        raise ValueError('Preparation lease ended; this result was discarded.')


async def execute(tid, run_id, *, auto=False, body=None):
    run = outreach.get('schedule_runs', tid, run_id)
    item = outreach.get('schedule_items', tid, run['item_id'])
    access(tid, item['created_by'])
    dependencies(tid, item)
    fresh = inspect_action(tid, {**item, 'next_at': run['scheduled_at']})
    prepared, kind = run['prepared'], item['kind']
    # A review refers to exactly the prepared recipient/action; changed records
    # require a new occurrence, never silently send to a different destination.
    if fresh != {k: v for k, v in prepared.items() if k != 'body'} and kind in ('email', 'sms'):
        raise ValueError('Sender or recipient changed; skip this occurrence and plan it again.')
    if kind in ('quote', 'invoice') and fresh != prepared:
        raise ValueError('The document changed; skip this occurrence and plan it again.')
    cfg = db.effective_config(tid, prepared.get('mailbox_id'))
    if auto and (kind not in ('email', 'sms') or not cfg.get('auto_send')):
        raise ValueError('The existing message settings require approval.')
    if kind == 'outreach' and (not outreach.settings(tid)['enabled'] or not
            outreach.get('outreach_campaigns', tid, prepared['campaign_id'])['enabled']):
        raise ValueError('Outreach and its campaign must be enabled separately.')
    text = body if body is not None else prepared.get('body', '')
    if kind in ('email', 'sms'):
        if not text.strip() or len(text) > 20000:
            raise ValueError('Write a message of 1–20,000 characters.')
        from app.concierge_booking_claims import check
        reason, _ = check(tid, prepared['to'], text, None)
        if reason:
            raise ValueError(reason)
        entry = db.get_email_log_entry(run['email_log_id'], tid)
        if not entry or entry['status'] != 'pending':
            raise ValueError('This draft is no longer pending.')
    claim = rpc('schedule_claim', tenant=tid, run=run_id, auto=auto)
    if claim != 'claimed':
        raise ValueError(claim)
    try:
        if kind in ('email', 'sms'):
            db.update_email_log_draft(run['email_log_id'], text, tid)
            outbound = disclosure.wrap(cfg, text, channel='sms' if kind == 'sms' else 'email') if auto else text
            if kind == 'email':
                await mailbox.send_mail(tid, prepared['to'], prepared['subject'], outbound,
                                        mailbox_id=prepared['mailbox_id'], expected_sender=prepared['sender'])
            else:
                await sms.send_sms(tid, prepared['to'], outbound, email_log_id=run['email_log_id'])
            result = 'Message sent'
        elif kind in ('quote', 'invoice'):
            from app import concierge_actions as actions
            result = (await actions.execute(tid, None, prepared['action']['kind'], prepared['payload'],
                                           edited_draft_text=body or prepared.get('body')))['summary']
        elif kind in ('task', 'followup'):
            created = db.crm_create_task(tid, **fresh)
            result = 'Task created: ' + str(created['id'])
        elif kind == 'booking':
            held = db.hold_booking_if_capacity(tid, fresh['booking_date'], fresh['window'], fresh['summary'],
                                               contact=fresh['contact'])
            if not held:
                raise ValueError('Capacity is no longer available.')
            result = 'Booking held for approval: ' + str(held['id'])
        else:
            result = 'Outreach enrollment: ' + str(outreach.enroll_list(tid, prepared['campaign_id']))
        if not rpc('schedule_complete', tenant=tid, run=run_id, result=result, auto=auto):
            raise RuntimeError('Completion was not recorded')
    except Exception as exc:
        db.db().table('schedule_runs').update({'status': 'unknown',
            'result': 'Outcome uncertain. Check the destination before resolving; never retry automatically.'})\
            .eq('tenant_id', tid).eq('id', run_id).eq('status', 'executing').execute()
        raise ValueError('Outcome uncertain. Pause the plan and check the destination before resolving it.') from exc
    return 'sent' if kind in ('email', 'sms') else result


async def send_draft(tid, entry, body, *, auto=False):
    found = outreach.rows('schedule_runs', tid, email_log_id=entry['id'])
    if not found:
        raise HTTPException(409, 'This scheduled draft has no occurrence record.')
    try:
        return await execute(tid, found[0]['id'], auto=auto, body=body)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


def finish_without_action(tid, run_id, status, reason, *, token=None):
    run = outreach.get('schedule_runs', tid, run_id)
    if run['status'] not in ('preparing', 'ready', 'blocked'):
        raise ValueError('An attempted action must be reconciled, never retried.')
    # CAS protects an approval racing a skip.
    query = db.db().table('schedule_runs').update({'status': status, 'result': reason,
              'finished_at': now().isoformat()}).eq('tenant_id', tid).eq('id', run_id)\
              .eq('status', run['status'])
    if token:
        query = query.eq('token', token)
    changed = query.execute().data
    if changed and run.get('email_log_id'):
        db.db().table('email_log').update({'status': 'skipped'}).eq('tenant_id', tid)\
            .eq('id', run['email_log_id']).eq('status', 'pending').execute()


def summarise(tid, cfg):
    local = now().astimezone(ZoneInfo(cfg['timezone']))
    if local.strftime('%H:%M') < cfg['end_time']:
        return
    start = datetime.combine(local.date(), datetime.min.time(), tzinfo=local.tzinfo).astimezone(timezone.utc)
    end = datetime.combine(local.date()+timedelta(days=1), datetime.min.time(), tzinfo=local.tzinfo).astimezone(timezone.utc)
    runs = db.db().table('schedule_runs').select('status').eq('tenant_id', tid).gte('scheduled_at', start.isoformat())\
        .lt('scheduled_at', end.isoformat()).limit(10000).execute().data or []
    counts = dict(Counter(r['status'] for r in runs))
    db.db().table('schedule_summaries').upsert({'tenant_id': tid, 'day': local.date().isoformat(),
            'counts': counts, 'updated_at': now().isoformat()}, on_conflict='tenant_id,day').execute()


async def run():
    """One server sweep. No external action is ever retried after reservation."""
    counts = Counter()
    tenants = []
    for offset in range(0, 100000, 500):
        page = db.db().table('schedule_settings').select('*').order('tenant_id').range(offset, offset+499).execute().data or []
        tenants.extend(page)
        if len(page) < 500:
            break
    for setting in tenants:
        tid, cfg = setting['tenant_id'], setting['config']
        try:
            summarise(tid, cfg)
            if not setting['enabled'] or not window(cfg):
                continue
            due = outreach.table('schedule_items', tid).eq('status', 'active').lte('next_at', now().isoformat())\
                .order('next_at').limit(100).execute().data or []
            for item in due:
                try:
                    token = str(uuid.uuid4())
                    reserved = rpc('schedule_reserve', tenant=tid, item=item['id'], due=item['next_at'],
                                   next=next_occurrence(item), token=token)
                    if not reserved:
                        continue
                    occurrence = reserved[0]
                    late = parse(item['next_at']).astimezone(ZoneInfo(item['timezone'])).date() < now().astimezone(ZoneInfo(item['timezone'])).date()
                    if late and item['catch_up'] == 'skip':
                        finish_without_action(tid, occurrence['id'], 'skipped', 'Missed day; catch-up policy is skip')
                    else:
                        try:
                            await prepare(occurrence)
                        except Exception as exc:
                            reason = str(exc)[:400] if isinstance(exc, ValueError) else 'Preparation failed; try preparing again.'
                            finish_without_action(tid, occurrence['id'], 'blocked', reason, token=occurrence['token'])
                    counts['prepared'] += 1
                except Exception:
                    log.exception('Scheduled item failed: %s', item['id'])
                    counts['failed'] += 1
            # A crash during composition is safe to flag: no action was attempted.
            stale = outreach.table('schedule_runs', tid).eq('status', 'preparing').lt('lease_until', now().isoformat()).limit(100).execute().data or []
            for occurrence in stale:
                finish_without_action(tid, occurrence['id'], 'blocked', 'Preparation interrupted; review and plan again')
            waiting = outreach.table('schedule_runs', tid).eq('status', 'blocked').eq('result', 'Waiting for payment.')\
                .order('last_checked_at').limit(100).execute().data or []
            for occurrence in waiting:
                outreach.update('schedule_runs', tid, occurrence['id'], {'last_checked_at': now().isoformat()})
                try:
                    dependencies(tid, occurrence['snapshot'])
                except ValueError:
                    continue
                recovered = rpc('schedule_retry_prepare', tenant=tid, run=occurrence['id'], token=str(uuid.uuid4()))
                if recovered:
                    try:
                        await prepare(recovered[0])
                    except Exception as exc:
                        reason = str(exc)[:400] if isinstance(exc, ValueError) else 'Preparation failed; try preparing again.'
                        finish_without_action(tid, occurrence['id'], 'blocked', reason, token=recovered[0]['token'])
            ready = outreach.table('schedule_runs', tid).eq('status', 'ready').eq('snapshot->>mode', 'auto')\
                .order('last_checked_at').limit(100).execute().data or []
            for occurrence in ready:
                if occurrence['snapshot']['mode'] != 'auto':
                    continue
                try:
                    await execute(tid, occurrence['id'], auto=True)
                except ValueError as exc:
                    outreach.update('schedule_runs', tid, occurrence['id'], {'result': str(exc)[:400]})
                except Exception:
                    log.exception('Scheduled action outcome needs review: %s', occurrence['id'])
                finally:
                    outreach.update('schedule_runs', tid, occurrence['id'], {'last_checked_at': now().isoformat()})
        except Exception:
            log.exception('Scheduled plan sweep failed for %s', tid)
            counts['failed'] += 1
    return dict(counts)
