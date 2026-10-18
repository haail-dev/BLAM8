"""Owner/manager planner: form controls, calendar and occurrence review."""
from datetime import date, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from app import db, guards, scheduled_plan as plan, tenant_outreach as data
from app.auth import TenantCtx, require_subscription
from app.dashboard import _ctx
from app.security import require_csrf
from app.templating import templates


def owner(ctx: TenantCtx = Depends(require_subscription)):
    guards.require_admin(ctx)
    return ctx


router = APIRouter(prefix='/app/plan', tags=['scheduled-plan'])


def redirect(message='', error=''):
    return RedirectResponse('/app/plan?' + urlencode({'msg': message, 'err': error}), 303)


@router.get('')
def page(request: Request, ctx: TenantCtx = Depends(owner)):
    setting = plan.settings(ctx.tenant_id, ctx.client)
    today = plan.now().astimezone(ZoneInfo(setting['config']['timezone'])).date()
    try:
        start = date.fromisoformat(request.query_params.get('start', today.isoformat()))
        span = 14 if request.query_params.get('days') == '14' else 7
    except ValueError:
        raise HTTPException(400, 'Use a valid calendar date.')
    items = data.rows('schedule_items', ctx.tenant_id, ctx.client)
    runs = data.rows('schedule_runs', ctx.tenant_id, ctx.client)
    days = [{'date': start + timedelta(days=n), 'entries': []} for n in range(span)]
    zone = ZoneInfo(setting['config']['timezone'])
    for item in items:
        occurrence = dict(item)
        for _ in range(500):
            if not occurrence.get('next_at') or item['status'] == 'cancelled':
                break
            at = plan.parse(occurrence['next_at']).astimezone(zone)
            if at.date() >= start + timedelta(days=span):
                break
            if at.date() >= start:
                days[(at.date()-start).days]['entries'].append({'item': item, 'at': at, 'run': None,
                    'movable': occurrence['next_at'] == item['next_at']})
            occurrence['next_at'] = plan.next_occurrence(occurrence)
    for run in runs:
        at = plan.parse(run['scheduled_at']).astimezone(zone)
        if start <= at.date() < start + timedelta(days=span):
            days[(at.date()-start).days]['entries'].append({'item': run['snapshot'], 'at': at,
                                                          'run': run, 'movable': False})
    for day in days:
        day['entries'].sort(key=lambda e: e['at'])
    def options(table, columns):
        return (ctx.client or db.db()).table(table).select(columns).eq('tenant_id', ctx.tenant_id).limit(1000).execute().data or []
    summaries = data.table('schedule_summaries', ctx.tenant_id, ctx.client).order('day', desc=True).limit(14).execute().data or []
    return templates.TemplateResponse(request, 'app/scheduled_plan.html', _ctx('scheduled_plan', ctx,
        setting=setting, days=days, start=start, span=span, today=today, items=items,
        overdue=[i for i in items if i['status']=='active' and i.get('next_at') and plan.parse(i['next_at'])<plan.now()],
        review=[r for r in runs if r['status'] in ('ready', 'blocked', 'unknown', 'executing')],
        summaries=summaries, kinds=plan.KINDS,
        contacts=db.crm_list_contacts(ctx.tenant_id, limit=1000, client=ctx.client),
        identities=data.identities(ctx.tenant_id, ctx.client),
        invoices=options('invoices', 'id,invoice_ref,customer_email,status'),
        quotes=options('quotes', 'id,quote_ref,customer_email,status'),
        campaigns=options('outreach_campaigns', 'id,name'),
        msg=request.query_params.get('msg', ''), err=request.query_params.get('err', '')))


@router.post('/settings', dependencies=[Depends(require_csrf)])
async def settings(request: Request, ctx: TenantCtx = Depends(owner)):
    form = await request.form()
    try:
        cfg = plan.config({'timezone': str(form.get('timezone', 'Europe/London')),
            'working_days': [int(v) for v in form.getlist('working_days')],
            'holidays': str(form.get('holidays', '')).split(), 'start_time': str(form.get('start_time', '09:00')),
            'end_time': str(form.get('end_time', '17:00')), 'daily_cap': int(form.get('daily_cap', 50)),
            'min_gap_seconds': int(form.get('min_gap_seconds', 60))})
        db.db().table('schedule_settings').upsert({'tenant_id': ctx.tenant_id,
                'enabled': form.get('enabled') == 'on', 'config': cfg}, on_conflict='tenant_id').execute()
        return redirect('Plan settings saved.')
    except (ValueError, TypeError) as exc:
        return redirect(error=str(exc))


@router.post('/items', dependencies=[Depends(require_csrf)])
async def create(request: Request, ctx: TenantCtx = Depends(owner)):
    f = await request.form()
    try:
        kind = str(f.get('kind', ''))
        payload = {k: str(f.get(k, '')).strip() for k in ('contact_id','mailbox_id','subject','brief','phone',
                    'booking_date','window','campaign_id','document_action','amount','description','document_date')}
        payload['document_id'] = str(f.get('quote_id' if kind == 'quote' else 'invoice_id', ''))
        item = plan.validate_item(ctx.tenant_id, {'title': f.get('title'), 'kind': kind,
            'mode': f.get('mode', 'draft'), 'run_at': f.get('run_at'), 'timezone': f.get('timezone'),
            'catch_up': f.get('catch_up', 'run_late'), 'payload': payload,
            'recurrence': {'frequency': f.get('frequency', 'once'), 'interval': f.get('interval', 1),
                           'days': f.getlist('repeat_days'), 'until': f.get('until')},
            'dependency': {'kind': f.get('dependency', ''), 'invoice_id': f.get('payment_invoice_id')}}, ctx.user_id)
        db.db().table('schedule_items').insert(item).execute()
        return redirect('Item planned. It will run only when the plan is enabled.')
    except (ValueError, TypeError, KeyError) as exc:
        return redirect(error=str(exc))


@router.post('/items/{item_id}', dependencies=[Depends(require_csrf)])
async def change(item_id: UUID, request: Request, ctx: TenantCtx = Depends(owner)):
    f = await request.form()
    try:
        item = data.get('schedule_items', ctx.tenant_id, str(item_id))
        action = f.get('action')
        fields = {'revision': item['revision'] + 1}
        if action in ('pause', 'resume', 'cancel'):
            fields['status'] = {'pause':'paused', 'resume':'active', 'cancel':'cancelled'}[action]
        elif action == 'edit':
            title = str(f.get('title', '')).strip()
            mode = str(f.get('mode', 'draft'))
            brief = str(f.get('brief', '')).strip()
            if not 1 <= len(title) <= 150 or len(brief) > 8000:
                raise ValueError('Use a title up to 150 characters and a brief up to 8,000 characters.')
            if mode not in ('draft','auto') or (mode == 'auto' and item['kind'] not in ('email','sms')):
                raise ValueError('Automatic mode is available for messages only.')
            fields.update(title=title, mode=mode, payload={**item['payload'], 'brief':brief,
                          'subject':str(f.get('subject', item['payload'].get('subject','')))[:200]})
            plan.inspect_action(ctx.tenant_id, {**item, **fields, 'next_at':item['next_at'] or plan.now().isoformat()})
        elif action == 'move':
            if not item.get('next_at'):
                raise ValueError('This occurrence has already been reserved; review it below.')
            fields['next_at'] = plan.local_time(str(f.get('run_at')), item['timezone']).isoformat()
            moved = plan.parse(fields['next_at']).astimezone(ZoneInfo(item['timezone']))
            fields['recurrence'] = {**item['recurrence'], 'anchor_day': moved.day,
                                    'anchor_time': moved.strftime('%H:%M:%S')}
        elif action == 'skip':
            if not item.get('next_at'):
                raise ValueError('No future occurrence to skip.')
            if not plan.rpc('schedule_skip_future', tenant=ctx.tenant_id, item=str(item_id),
                            due=item['next_at'], next=plan.next_occurrence(item)):
                raise ValueError('This occurrence changed. Reload the calendar.')
            return redirect('Occurrence skipped and recorded in the plan history.')
        else:
            raise ValueError('Choose an item action.')
        # Revision CAS prevents two edits silently replacing one another.
        query = db.db().table('schedule_items').update(fields).eq('tenant_id', ctx.tenant_id)\
            .eq('id', str(item_id)).eq('revision', item['revision'])
        query = query.eq('next_at', item['next_at']) if item.get('next_at') else query.is_('next_at', 'null')
        changed = query.execute().data
        if not changed:
            raise ValueError('This item changed while you were editing. Reload the calendar.')
        return redirect('Item updated. Existing prepared drafts from its old revision cannot run.')
    except (ValueError, TypeError, KeyError) as exc:
        return redirect(error=str(exc))


@router.post('/runs/{run_id}', dependencies=[Depends(require_csrf)])
async def review(run_id: UUID, request: Request, ctx: TenantCtx = Depends(owner)):
    f = await request.form()
    try:
        action = f.get('action')
        if action == 'approve':
            await plan.execute(ctx.tenant_id, str(run_id), body=f.get('body'))
        elif action == 'skip':
            plan.finish_without_action(ctx.tenant_id, str(run_id), 'skipped', 'Skipped by the owner')
        elif action == 'retry_prepare':
            import uuid
            recovered = plan.rpc('schedule_retry_prepare', tenant=ctx.tenant_id, run=str(run_id), token=str(uuid.uuid4()))
            if not recovered:
                raise ValueError('Only an unattempted preparation can be retried while the plan is running.')
            try:
                await plan.prepare(recovered[0])
            except Exception as exc:
                reason = str(exc)[:400] if isinstance(exc, ValueError) else 'Preparation failed; try preparing again.'
                plan.finish_without_action(ctx.tenant_id, str(run_id), 'blocked', reason, token=recovered[0]['token'])
                raise ValueError(reason) from exc
        elif action in ('confirm_done', 'close_unknown'):
            if not f.get('checked'):
                raise ValueError('Check the destination before recording an outcome.')
            if not plan.rpc('schedule_reconcile', tenant=ctx.tenant_id, run=str(run_id), done=action=='confirm_done'):
                raise ValueError('Pause the plan and wait five minutes after the attempt before resolving.')
        else:
            raise ValueError('Choose a review action.')
        return redirect('Occurrence updated.')
    except (ValueError, TypeError, KeyError) as exc:
        return redirect(error=str(exc))
