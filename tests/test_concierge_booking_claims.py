"""Offline regression for the Ann Fowler rig draft and its approval boundaries."""
import asyncio
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from app import concierge_booking_claims as guard, concierge_actions as actions
from app import concierge_draft, agent, dashboard, pipeline
from app.auth import TenantCtx


BOOKING = {"booking_ref": "BK-1015", "booking_date": "2026-09-28", "status": "proposed"}
BAD = ("Hi Ann, your emergency callout is booked in for this afternoon "
       "(Monday 28 September, ref BK-1015). Our engineer will be with you during the afternoon window.")
GOOD = "Hi Ann, BK-1015 is pencilled in for 28 September. Please confirm."


@pytest.mark.parametrize('status', ['proposed', 'held', 'offered', 'cancelled', '', 'expired'])
def test_rig_overclaim_is_blocked(status):
    assert guard.violation(BAD, [{**BOOKING, 'status': status}])


@pytest.mark.parametrize('text', [GOOD, "BK-1015 is not yet confirmed. Please confirm.",
    "BK-1015 has not yet been confirmed. Please confirm.",
    "BK-1015 isn't booked. Please confirm.", "BK-1015 is held for you; please confirm."])
def test_provisional_wording_is_allowed(text):
    assert not guard.violation(text, [BOOKING])


@pytest.mark.parametrize('text', [BAD, 'BK-1015 is confirmed.',
    'You are booked for 28 September.', 'Your appointment is booked for 2026-09-28.'])
def test_confirmed_remains_allowed(text):
    assert not guard.violation(text, [{**BOOKING, 'status': 'confirmed'}])


@pytest.mark.parametrize('text', ['You are booked for 28 September.',
    'Your appointment is confirmed on 2026-09-28.',
    'BK-1015 is not confirmed, but it is booked in.',
    'BK-1015 is held. It is confirmed.', 'BK-1015 is all set.',
    'BK-1015: we will attend.', 'BK-1015 is **booked in**.',
    'BK-1015 is BOOKED IN.', 'BK-1015 is booked   in.'])
def test_date_only_pronouns_and_negation_do_not_bypass(text):
    assert guard.violation(text, [BOOKING])


def test_unbacked_reference_date_and_empty_lookup_hold():
    confirmed = [{**BOOKING, 'status': 'confirmed'}]
    assert guard.violation('BK-9999 is confirmed.', confirmed)
    assert guard.violation('BK-9999. It is confirmed.', confirmed)
    assert guard.violation('You are booked for 29 September.', confirmed)
    assert guard.violation('29 September. You are booked.', confirmed)
    assert guard.violation(BAD, [])
    assert not guard.violation('We received your message.', [])
    assert not guard.violation('BK-1015 is cancelled.', [{**BOOKING, 'status': 'cancelled'}])


def test_multi_booking_claim_requires_every_record():
    rows = [BOOKING, {**BOOKING, 'booking_ref': 'BK-1016', 'status': 'confirmed'}]
    assert guard.violation('BK-1015 and BK-1016 are confirmed.', rows)
    assert not guard.violation('BK-1016 is confirmed; BK-1015 is held, please confirm.', rows)


def wire(monkeypatch, replacement=GOOD, status='proposed'):
    calls = []
    monkeypatch.setattr(guard, 'read_bookings', lambda *a, **k: [{**BOOKING, 'status': status}])
    monkeypatch.setattr(guard.db, 'effective_config', lambda *a, **k: {})
    monkeypatch.setattr('app.usage.record_anthropic', lambda *a, **k: None)

    async def compose(cfg, purpose, facts, **kwargs):
        calls.append((purpose, facts))
        return SimpleNamespace(text=replacement, model='offline', usage={})
    monkeypatch.setattr(agent, 'compose_outbound', compose)
    return calls


@pytest.mark.parametrize('replacement,held', [(GOOD, False), (BAD, True), ('', True)])
def test_repair_once_with_reason_then_hold(monkeypatch, replacement, held):
    calls = wire(monkeypatch, replacement)
    text, reason = asyncio.run(guard.repair('tenant', 'ann@example.com', BAD))
    assert len(calls) == 1
    assert 'proposed' in calls[0][1] and 'BK-1015' in calls[0][1]
    assert bool(reason) is held
    assert text == (replacement or BAD)


def test_lookup_failure_is_could_not_verify_not_rule_failure(monkeypatch):
    def fail(*a, **k):
        raise RuntimeError('offline')
    monkeypatch.setattr(guard, 'read_bookings', fail)
    reason, rows = guard.check('tenant', 'ann@example.com', BAD)
    assert 'Could not verify' in reason and 'lookup failed' in reason
    assert rows == []


def test_stager_holds_on_card_and_executor_rechecks_edits_and_state(monkeypatch):
    wire(monkeypatch)
    monkeypatch.setattr(actions.db, 'get_conversation', lambda *a, **k: [{'id': 'log'}])
    monkeypatch.setattr(actions.db, 'get_email_log_entry', lambda *a, **k: {'id': 'log'})
    writes = []
    monkeypatch.setattr(actions.db, 'update_email_log_draft', lambda *a, **k: writes.append(a))
    monkeypatch.setattr(actions.db, 'set_email_log_status', lambda *a, **k: None)
    monkeypatch.setattr(actions.db, 'log_agent_event', lambda *a, **k: None)
    monkeypatch.setattr(actions.events, 'publish', lambda *a, **k: None)
    summary, payload, details, text = actions._stage_draft_reply('tenant', None,
        {'contact': 'ann@example.com', 'draft_text': BAD})
    assert 'Held' in summary and 'proposed' in details[0]
    with pytest.raises(actions.ActionError, match='proposed'):
        asyncio.run(actions._exec_draft_reply('tenant', None, payload, None))
    assert not writes
    asyncio.run(actions._exec_draft_reply('tenant', None, payload, GOOD))
    assert writes[-1][1] == GOOD
    monkeypatch.setattr(guard, 'read_bookings', lambda *a, **k: [{**BOOKING, 'status': 'cancelled'}])
    with pytest.raises(actions.ActionError, match='cancelled'):
        asyncio.run(actions._exec_draft_reply('tenant', None, payload, GOOD))
    assert len(writes) == 1


def test_bulk_repairs_each_draft_only(monkeypatch):
    calls = wire(monkeypatch)
    action = {'kind': 'bulk', 'items': [
        {'kind': 'draft_reply', 'contact': 'ann@example.com', 'draft_text': BAD},
        {'kind': 'update_contact', 'content': 'unchanged'}]}
    assert asyncio.run(guard.repair_action(action, 'tenant'))
    assert len(calls) == 1 and action['items'][0]['draft_text'] == GOOD
    assert action['items'][1]['content'] == 'unchanged'


@pytest.mark.parametrize('voice_mode', [False, True])
@pytest.mark.parametrize('replacement', [GOOD, BAD])
def test_actual_business_answer_repairs_customer_body_and_removes_stale_owner_copy(
        monkeypatch, voice_mode, replacement):
    import json
    from app import concierge
    calls = wire(monkeypatch, replacement)
    action = {'kind': 'draft_reply', 'contact': 'ann@example.com', 'draft_text': BAD}
    answer = {'reply': 'I propose to send: ' + BAD, 'links': [], 'action': action}
    block = (SimpleNamespace(type='text', text=json.dumps(answer)) if voice_mode else
             SimpleNamespace(type='tool_use', name='answer', id='answer-1', input=answer))
    async def create(**kwargs):
        return SimpleNamespace(content=[block], usage={}, model='offline', stop_reason='end_turn')
    monkeypatch.setattr(concierge.llm, 'create', create)
    monkeypatch.setattr(concierge, 'client', lambda: None)
    result = asyncio.run(concierge.answer_business('Update Ann', tools=[], tool_executor=None,
                                                  tenant_id='tenant', voice_mode=voice_mode))
    assert len(calls) == 1 and result.action['draft_text'] == replacement
    assert BAD not in result.reply and 'card' in result.reply


def test_confirmed_draft_does_not_trigger_regeneration(monkeypatch):
    calls = wire(monkeypatch, status='confirmed')
    text, reason = asyncio.run(guard.repair('tenant', 'ann@example.com', BAD))
    assert text == BAD and not reason and not calls


@pytest.mark.parametrize('mode', ['new', 'reply'])
def test_voice_second_failure_holds_with_reason_and_never_auto_sends(monkeypatch, mode):
    from tests.test_concierge_voice_draft import _wire, WAITING, CTX
    calls = _wire(monkeypatch, WAITING, auto_send=True)
    repair_calls = wire(monkeypatch, BAD, status='offered')
    cleared, categories = [], []
    monkeypatch.setattr(concierge_draft, '_retire_earlier_drafts', lambda *a: 0)
    monkeypatch.setattr(concierge_draft.db, 'update_email_log_draft', lambda *a, **k: cleared.append(a))
    monkeypatch.setattr(concierge_draft.db, 'set_email_log_category', lambda *a, **k: categories.append(k))
    async def redraft(*a, **k):
        calls['redraft'].append(k)
        return BAD
    monkeypatch.setattr(pipeline, 'redraft_email_log', redraft)
    result, card = asyncio.run(concierge_draft.draft(CTX,
        {'contact': 'ann@example.com', 'mode': mode, 'instruction': 'Update Ann'}, member=None))
    assert not result['ok'] and card['status'] == 'empty'
    assert 'offered' in card['reason'] and not card['text']
    assert not calls['send']
    if mode == 'new':
        assert len(repair_calls) == 2  # initial compose + exactly one repair
        assert not calls['insert']  # no phantom empty pending row on Today
        assert card['log_id'] is None
    else:
        assert len(calls['redraft']) == 2
        assert cleared[-1][1] == '' and categories[-1]['reason'] == card['reason']


@pytest.mark.parametrize('mode', ['new', 'reply'])
def test_a_hold_offers_both_ways_forward_not_a_dead_end(monkeypatch, mode):
    from tests.test_concierge_voice_draft import _wire, WAITING, CTX
    _wire(monkeypatch, WAITING)
    wire(monkeypatch, BAD, status='offered')
    monkeypatch.setattr(concierge_draft, '_retire_earlier_drafts', lambda *a: 0)
    monkeypatch.setattr(concierge_draft.db, 'update_email_log_draft', lambda *a, **k: None)
    monkeypatch.setattr(concierge_draft.db, 'set_email_log_category', lambda *a, **k: None)
    async def redraft(*a, **k):
        return BAD
    monkeypatch.setattr(pipeline, 'redraft_email_log', redraft)
    result, _ = asyncio.run(concierge_draft.draft(CTX,
        {'contact': 'ann@example.com', 'mode': mode, 'instruction': 'Tell Ann'}, member=None))
    msg = result['message']
    assert 'call draft_email AGAIN' in msg and 'pencilled in' in msg
    assert 'Do NOT call show_draft' in msg


def test_show_draft_rewords_a_held_draft_instead_of_dead_ending(monkeypatch):
    wire(monkeypatch, GOOD, status='proposed')   # the one corrective rewrite is provisional
    monkeypatch.setattr(concierge_draft.db, 'get_conversation', lambda *a, **k: [{
        'id': 'log', 'kind': 'owner_message', 'status': 'pending',
        'contact_name': 'Ann', 'draft_reply': BAD}])
    monkeypatch.setattr(dashboard, '_require_their_log', lambda *a: None)
    saved = []
    monkeypatch.setattr(concierge_draft.db, 'update_email_log_draft',
                        lambda log_id, text, *a, **k: saved.append((log_id, text)))
    result, card = asyncio.run(concierge_draft.show(TenantCtx(tenant_id='tenant', client=None),
        {'contact': 'ann@example.com'}, member=None))
    # Not sendable by a bare yes: the owner hears the new wording first.
    assert result['ok'] and card['status'] == 'left' and card['text'] == GOOD
    assert saved == [('log', GOOD)] and 'NOT on the send screen' in result['message']
    # The second call (owner said yes to the pencilled-in email) stages it.
    monkeypatch.setattr(concierge_draft.db, 'get_conversation', lambda *a, **k: [{
        'id': 'log', 'kind': 'owner_message', 'status': 'pending',
        'contact_name': 'Ann', 'draft_reply': GOOD}])
    result, card = asyncio.run(concierge_draft.show(TenantCtx(tenant_id='tenant', client=None),
        {'contact': 'ann@example.com'}, member=None))
    assert card['status'] == 'pending' and result['status'] == 'pending'


def test_show_draft_that_cannot_be_reworded_holds_with_the_way_forward(monkeypatch):
    wire(monkeypatch, BAD, status='offered')
    monkeypatch.setattr(concierge_draft.db, 'get_conversation', lambda *a, **k: [{
        'id': 'log', 'kind': 'owner_message', 'status': 'pending',
        'contact_name': 'Ann', 'draft_reply': BAD}])
    monkeypatch.setattr(dashboard, '_require_their_log', lambda *a: None)
    saved = []
    monkeypatch.setattr(concierge_draft.db, 'update_email_log_draft', lambda *a, **k: saved.append(a))
    result, card = asyncio.run(concierge_draft.show(TenantCtx(tenant_id='tenant', client=None),
        {'contact': 'ann@example.com'}, member=None))
    assert not result['ok'] and card['status'] == 'empty' and not card['text'] and not saved
    assert 'call draft_email AGAIN' in result['message']


@pytest.mark.parametrize('mode', ['new', 'reply'])
def test_a_silently_reworded_draft_is_reported_so_speech_matches_the_card(monkeypatch, mode):
    from tests.test_concierge_voice_draft import _wire, WAITING, CTX
    _wire(monkeypatch, WAITING)
    wire(monkeypatch, GOOD, status='offered')   # the repair rewrite is provisional
    monkeypatch.setattr(concierge_draft, '_retire_earlier_drafts', lambda *a: 0)
    monkeypatch.setattr(concierge_draft.db, 'update_email_log_draft', lambda *a, **k: None)
    monkeypatch.setattr(concierge_draft.db, 'set_email_log_category', lambda *a, **k: None)
    texts = iter([BAD, GOOD])
    async def redraft(*a, **k):
        return next(texts)
    monkeypatch.setattr(pipeline, 'redraft_email_log', redraft)
    async def compose(cfg, purpose, facts, **k):
        return SimpleNamespace(text=BAD if 'Correct only' not in purpose else GOOD,
                               model='m', usage={})
    monkeypatch.setattr(agent, 'compose_outbound', compose)
    result, card = asyncio.run(concierge_draft.draft(CTX,
        {'contact': 'ann@example.com', 'mode': mode, 'instruction': 'Tell Ann she is booked in'},
        member=None))
    assert result['ok'] and result['status'] == 'pending' and card['status'] == 'pending'
    assert 'reworded' in result['message'] and 'not yet confirmed' in result['message']
    assert "ask ONLY 'Shall I send it?'" in result['message']


@pytest.mark.parametrize('status', ['proposed', 'cancelled', 'completed'])
def test_send_rechecks_customer_text_before_any_write(monkeypatch, status):
    wire(monkeypatch, status=status)
    monkeypatch.setattr(dashboard.db, 'get_email_log_entry', lambda *a, **k: {
        'id': 'log', 'kind': 'owner_message', 'from_address': 'ann@example.com',
        'draft_reply': GOOD})  # The unsafe text below is the owner's edit.
    writes = []
    monkeypatch.setattr(dashboard.db, 'update_email_log_draft', lambda *a, **k: writes.append(a))
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        asyncio.run(dashboard.send_approved(TenantCtx(tenant_id='tenant', client=None), 'log', BAD))
    assert exc.value.status_code == 409 and status in exc.value.detail
    assert 'Confirm BK-1015 in the diary first, or change the wording' in exc.value.detail
    assert not writes


def test_bringing_back_a_stale_draft_shows_hold_reason(monkeypatch):
    wire(monkeypatch, status='cancelled')
    monkeypatch.setattr(concierge_draft.db, 'get_conversation', lambda *a, **k: [{
        'id': 'log', 'kind': 'owner_message', 'status': 'pending',
        'contact_name': 'Ann', 'draft_reply': BAD}])
    monkeypatch.setattr(dashboard, '_require_their_log', lambda *a: None)
    result, card = asyncio.run(concierge_draft.show(TenantCtx(tenant_id='tenant', client=None),
        {'contact': 'ann@example.com'}, member=None))
    assert not result['ok'] and card['status'] == 'empty'
    assert 'cancelled' in card['reason'] and not card['text']


def test_diary_read_is_exact_tenant_and_contact_and_includes_cancelled():
    seen = []
    class Query:
        def __getattr__(self, name):
            def call(*args):
                seen.append((name, args))
                return SimpleNamespace(data=[BOOKING]) if name == 'execute' else self
            return call
    assert guard.read_bookings('tenant-a', 'ann@example.com', Query()) == [BOOKING]
    assert ('eq', ('tenant_id', 'tenant-a')) in seen
    assert ('eq', ('contact', 'ann@example.com')) in seen
    assert not any(name == 'in_' or args[:1] == ('status',) for name, args in seen)


TODAY = date(2026, 9, 28)


def test_unanchored_confirmation_ignores_cancelled_and_finished_history():
    rows = [
        {**BOOKING, 'booking_ref': 'BK-1000', 'status': 'cancelled', 'booking_date': '2026-09-01'},
        {**BOOKING, 'booking_ref': 'BK-1001', 'status': 'completed', 'booking_date': '2026-09-02'},
        {**BOOKING, 'booking_ref': 'BK-1002', 'status': 'proposed', 'booking_date': '2026-09-27'},
        {**BOOKING, 'status': 'confirmed'}]
    assert not guard.violation("Great, that's all confirmed.", rows, business_today=TODAY)
    assert guard.violation('BK-1000 is confirmed.', rows, business_today=TODAY)
    assert guard.violation('1 September is confirmed.', rows, business_today=TODAY)
    assert guard.violation('BK-1000. It is confirmed.', rows, business_today=TODAY)


@pytest.mark.parametrize('status,day', [('cancelled', '2026-09-29'), ('completed', '2026-09-29'),
    ('confirmed', '2026-09-27'), ('proposed', '2026-09-27'), ('confirmed', None)])
def test_unanchored_without_a_live_record_cannot_verify(status, day):
    assert 'Could not verify' in guard.violation("That's all confirmed.", [
        {**BOOKING, 'status': status, 'booking_date': day}], business_today=TODAY)


@pytest.mark.parametrize('status', ['proposed', 'held', 'offered'])
def test_unanchored_mixed_live_states_hold_as_ambiguous(status):
    reason = guard.violation('It is held for you.', [
        {**BOOKING, 'status': 'confirmed'},
        {**BOOKING, 'booking_ref': 'BK-1016', 'status': status}], business_today=TODAY)
    assert 'states differ' in reason and 'Name the booking reference' in reason


def test_unanchored_same_live_states_and_provisional_wording():
    rows = [BOOKING, {**BOOKING, 'booking_ref': 'BK-1016'}]
    assert not guard.violation('Held for you, please confirm.', rows, business_today=TODAY)
    assert 'not confirmed' in guard.violation('All confirmed.', rows, business_today=TODAY)
    assert not guard.violation('All confirmed.', [{**b, 'status': 'confirmed'} for b in rows],
                               business_today=TODAY)


@pytest.mark.parametrize('zone,allowed', [('Pacific/Honolulu', True), ('Pacific/Auckland', False)])
def test_unanchored_today_uses_business_timezone(monkeypatch, zone, allowed):
    wire(monkeypatch, status='confirmed')
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 28, 23, 30, tzinfo=timezone.utc).astimezone(tz)
    monkeypatch.setattr(guard, 'datetime', Clock)
    monkeypatch.setattr(guard.db, 'get_scheduling_config', lambda *a, **k: {'timezone': zone})
    reason, _ = guard.check('tenant', 'ann@example.com', "That's all confirmed.")
    assert bool(reason) is not allowed


def test_missing_timezone_holds_only_unanchored_claims(monkeypatch):
    wire(monkeypatch, status='confirmed')
    monkeypatch.setattr(guard.db, 'get_scheduling_config', lambda *a, **k: {})
    assert 'business timezone' in guard.check('tenant', 'ann@example.com', 'All confirmed.')[0]
    assert not guard.check('tenant', 'ann@example.com', 'BK-1015 is confirmed.')[0]


@pytest.mark.parametrize('text', [
    'Thanks for having us on Monday. Your visit was completed and the invoice is attached.',
    'BK-1015 was completed and the invoice is attached.',
    'BK-1015 was scheduled for Monday.', 'BK-1015 had been confirmed.',
    '28 September was reserved for you. The work has been completed.'])
def test_completed_past_tense_passes(text):
    assert not guard.violation(text, [{**BOOKING, 'status': 'completed'}], business_today=TODAY)


@pytest.mark.parametrize('status', ['completed', 'cancelled'])
@pytest.mark.parametrize('text', [
    'BK-1015 is booked in.', 'BK-1015: we will attend.',
    'BK-1015 was completed, and we will attend tomorrow.',
    'BK-1015 was scheduled for Monday. It is confirmed.',
    'BK-1015 was booked in.', 'BK-1015 was scheduled for next Monday.'])
def test_completed_or_cancelled_future_and_firm_claims_still_hold(status, text):
    assert guard.violation(text, [{**BOOKING, 'status': status}], business_today=TODAY)


@pytest.mark.parametrize('dest', ['/app/conversations/view?contact=ann%40example.com', '/app/concierge'])
def test_send_refusal_returns_to_thread_but_concierge_keeps_409(monkeypatch, dest):
    from fastapi import HTTPException
    from starlette.datastructures import FormData
    from urllib.parse import parse_qs, urlsplit
    reason = ('BK-1015: status is proposed, not confirmed. '
              'Confirm BK-1015 in the diary first, or change the wording.')
    class Request:
        async def form(self):
            return FormData()
    async def refuse(*a, **k):
        raise HTTPException(status_code=409, detail=reason)
    monkeypatch.setattr(dashboard, 'send_approved', refuse)
    send = dashboard.send_draft(Request(), 'log', BAD, return_to=dest,
                                ctx=TenantCtx(tenant_id='tenant', client=None))
    if dest == '/app/concierge':
        with pytest.raises(HTTPException) as exc:
            asyncio.run(send)
        assert exc.value.status_code == 409 and exc.value.detail == reason
    else:
        response = asyncio.run(send)
        assert response.status_code == 303
        assert parse_qs(urlsplit(response.headers['location']).query)['send_err'] == [reason]


CANCELLED_DUP = {**BOOKING, 'booking_ref': 'BK-1014', 'status': 'cancelled'}


def test_a_cancelled_duplicate_on_the_same_day_does_not_veto_the_live_record():
    rows = [CANCELLED_DUP, BOOKING]
    assert not guard.violation('Your callout on 28 September is pencilled in. Please confirm.', rows)
    # the live record still governs: a firm claim against a proposed booking is held
    assert 'not confirmed' in guard.violation('You are booked for 28 September.', rows)
    # with only the cancelled record, the date still cannot be promised
    assert 'cancelled' in guard.violation('You are booked for 28 September.', [CANCELLED_DUP])
    # naming the cancelled record by reference is still caught
    assert 'cancelled' in guard.violation('BK-1014 is booked in.', rows)


def test_repair_gives_the_rewrite_live_records_only_and_forbids_mentioning_cancelled_ones(monkeypatch):
    calls = wire(monkeypatch, GOOD)
    monkeypatch.setattr(guard, 'read_bookings', lambda *a, **k: [CANCELLED_DUP, BOOKING])
    asyncio.run(guard.repair('tenant', 'ann@example.com', BAD))
    purpose, facts = calls[0]
    assert 'BK-1015' in facts and 'BK-1014' not in facts and 'cancelled' not in facts.lower().replace('never mention cancelled', '')
    import inspect
    assert 'Never mention cancelled or superseded bookings' in inspect.getsource(guard.repair)


def test_a_held_typed_draft_offers_both_ways_forward(monkeypatch):
    wire(monkeypatch, BAD)
    monkeypatch.setattr(guard, 'read_bookings', lambda *a, **k: [BOOKING])
    action = {'kind': 'draft_reply', 'contact': 'ann@example.com', 'draft_text': BAD}
    assert asyncio.run(guard.repair_action(action, 'tenant'))
    reply = guard.repair_reply(action)
    assert 'held that draft card and sent nothing' in reply and 'pencilled in' in reply
    assert 'confirm the booking in the diary' in reply and reply.endswith('Which would you like?')
    clean = {'kind': 'draft_reply', 'hold_reason': ''}
    assert guard.repair_reply(clean).startswith('Please review the customer draft card')


def test_show_draft_of_a_clean_draft_says_it_is_back_unchanged_not_that_it_was_reworded(monkeypatch):
    wire(monkeypatch, GOOD, status='proposed')
    monkeypatch.setattr(concierge_draft.db, 'get_conversation', lambda *a, **k: [{
        'id': 'log', 'kind': 'owner_message', 'status': 'pending',
        'contact_name': 'Ann', 'draft_reply': GOOD}])
    monkeypatch.setattr(dashboard, '_require_their_log', lambda *a: None)
    writes = []
    monkeypatch.setattr(concierge_draft.db, 'update_email_log_draft', lambda *a, **k: writes.append(a))
    result, card = asyncio.run(concierge_draft.show(TenantCtx(tenant_id='tenant', client=None),
        {'contact': 'ann@example.com'}, member=None))
    assert result['status'] == 'pending' and card['status'] == 'pending' and not writes
    assert 'back on the owner' in result['message'] and 'unchanged' in result['message']
    assert 'reworded' not in result['message']


# ── confirm-first (docs/plan-concierge-confirm-first.md): a code per hold ─────────
FUTURE_TODAY = date(2026, 9, 28)   # BOOKING is on the 28th: not past


@pytest.mark.parametrize('status,code', [('proposed', 'firm_on_proposed'), ('held', 'firm_on_proposed'),
                                          ('offered', 'firm_on_offered'), ('cancelled', 'not_live')])
def test_each_hold_has_a_code(status, code):
    reason = guard.violation(BAD, [{**BOOKING, 'status': status}], business_today=FUTURE_TODAY)
    assert guard.classify_hold(reason) == code and guard.hold_ref(reason) == 'BK-1015'


def test_unverifiable_and_unheld_codes():
    assert guard.classify_hold(guard.violation(BAD, [])) == 'unverified'
    assert guard.classify_hold('') == ''
    assert guard.hold_ref('Could not verify the booking state.') is None


def test_a_past_proposed_booking_is_a_move_first_hold():
    reason = guard.violation(BAD, [BOOKING], business_today=date(2026, 9, 30))
    assert guard.classify_hold(reason) == 'past_date'
    assert '28th Sep' in reason
    assert 'Move it to a new date first' in reason and 'confirm' not in reason.lower().replace('nothing can promise', '')


def test_proposed_text_never_asks_the_customer_to_confirm_but_offered_does():
    proposed = guard.violation(BAD, [BOOKING], business_today=FUTURE_TODAY)
    assert 'waits on you' in proposed and 'ask the customer to confirm' not in proposed
    assert 'Confirm BK-1015 in the diary first, or change the wording' in proposed   # the send gate's 409 text
    offered = guard.violation(BAD, [{**BOOKING, 'status': 'offered'}], business_today=FUTURE_TODAY)
    assert 'ask the customer to confirm' in offered


def test_the_check_still_holds_every_unbacked_firm_claim():
    for status in ('proposed', 'held', 'offered', 'cancelled'):
        assert guard.violation(BAD, [{**BOOKING, 'status': status}], business_today=FUTURE_TODAY)
    assert not guard.violation(BAD, [{**BOOKING, 'status': 'confirmed'}], business_today=FUTURE_TODAY)
