"""Source corrections are draft mutations; existing admission remains the writer."""
import json
import logging
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from app import db, fairuse, knowledge_review, knowledge_source_evidence, knowledge_source_review, people, setup_cards
from app.auth import TenantCtx, require_tenant
from app.config import settings
from app.security import rate_limit, require_csrf_header
from app.setup_card_routes import _error

log = logging.getLogger(__name__)

from app.device_sync import SyncRoute
router = APIRouter(prefix='/onboarding/knowledge/review', route_class=SyncRoute, dependencies=[
    Depends(rate_limit('knowledge_source_review', limit=20, window_s=60))])


def require_source_writer(ctx):
    if ctx.viewing_as:
        raise HTTPException(403, 'Source corrections are unavailable while viewing as another member.')
    member = db.get_member(ctx.tenant_id, ctx.user_id)
    if not people.can_do(people.member(member) if member else None, 'settings', 'change'):
        raise HTTPException(403, 'Changing Knowledge needs settings permission.')
    return ctx


def require_source_review(ctx: TenantCtx = Depends(require_tenant)):
    if not settings.knowledge_source_review_enabled:
        raise HTTPException(404, 'Source corrections are not enabled.')
    return require_source_writer(ctx)


def _review_error(exc):
    if isinstance(exc, HTTPException):
        raise exc
    name = type(exc).__name__
    detail = str(exc).casefold()
    provider_errors = {'AuthenticationError', 'PermissionDeniedError', 'RateLimitError',
                       'InternalServerError', 'APIConnectionError', 'APITimeoutError',
                       'APIStatusError', 'OverloadedError', 'BadRequestError'}
    if name in provider_errors or any(term in detail for term in
            ('credit balance', 'insufficient credit', 'billing', 'api key', 'overloaded', 'rate limit')):
        log.warning('Knowledge review model service unavailable (%s)', name)
        if 'credit balance' in detail or 'insufficient credit' in detail:
            raise HTTPException(503, 'The Knowledge review AI service has no available API credit. The service account owner must restore its balance. Your review choices remain on this page; you can still review the current note manually.') from None
        raise HTTPException(503, 'The Knowledge review AI service is unavailable right now. Your review choices remain on this page; you can still review the current note manually.') from None
    _error(exc)


class DraftRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    version: str = Field(min_length=1, max_length=128)
    edits: dict = Field(default_factory=dict)
    note_id: str = Field(default='', max_length=128)
    focus_hash: str = Field(default='', max_length=128)
    form: dict[str, str] = Field(default_factory=dict, max_length=4000)


class ReviewRequest(DraftRequest):
    question: str = Field(min_length=1, max_length=4000)


class ApplyRequest(DraftRequest):
    review: dict
    owner_request: str = Field(default='', max_length=4000)


class GuideRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    form: dict[str, str] = Field(max_length=4000)
    question: str = Field(default='', max_length=4000)
    focus_hash: str = Field(default='', max_length=128)


@router.post('/{review_id}/guide', dependencies=[Depends(require_csrf_header)])
async def guide(review_id: UUID, body: GuideRequest, ctx: TenantCtx = Depends(require_source_review)):
    # This route reads stored drafts and prepares local choices only. The existing
    # admission endpoint remains the sole writer of saved Knowledge.
    from app import knowledge_guidance
    if sum(len(k)+len(v) for k,v in body.form.items())>350000 or any(len(k)>200 or len(v)>60000 for k,v in body.form.items()):
        raise HTTPException(422, 'This review is too large. Split it before continuing.')
    context = knowledge_review.load(ctx.tenant_id)
    row = next((r for r in context['reviews'] if r['id'] == str(review_id)), None)
    if not row:
        raise HTTPException(404, 'Review not found.')
    if knowledge_guidance.needs_model(body.question) and not fairuse.onboarding_allowed(ctx.tenant_id, 'concierge_onboard', client=ctx.client):
        raise HTTPException(429, 'Today\'s setup review allowance is used. Your review choices remain on the page.')
    try:
        result = await knowledge_guidance.turn(ctx.tenant_id, context, row, body.form, body.question, body.focus_hash)
        if not result.get('correction_requested') and (result.get('patches') or not knowledge_guidance.needs_model(body.question)):
            merged = {**body.form, **result.get('patches',{})}
            fresh, stored, choices = knowledge_review.checkpoint(ctx.tenant_id, context, row, merged)
            # Recompute focus under the confirmed revision; never replay a stale choice.
            shown = knowledge_guidance.present(fresh, stored, choices)
            result.update({k:shown[k] for k in ('focus','focus_hash','version','save_ready')})
            result['checkpoint'] = {'version':fresh['version'],
                'reviewed':{k:v for k,v in choices.items() if k.startswith('reviewed_')}}
        return result
    except Exception as exc:
        _review_error(exc)


def state(ctx, review_id, version):
    context = knowledge_review.load(ctx.tenant_id)
    row = next((r for r in context['reviews'] if r['id'] == str(review_id)), None)
    if not row:
        raise HTTPException(404, 'Review not found.')
    if row['state'] != 'pending' or context['version'] != version:
        raise setup_cards.Stale('Knowledge or drafts changed. Reload before reviewing.')
    identity = row['payload'].get('source_snapshot')
    if not identity:
        raise setup_cards.Invalid('This older draft has no retained source snapshot. Review it manually.')
    return row, knowledge_source_evidence.load(ctx.tenant_id, identity)


def local_review(row, body):
    """Read-only local choices; these never become stored body edits implicitly."""
    if not body.form:
        return None
    form = knowledge_review.review_form(row, body.form)
    if (form.get('version') != body.version or sum(len(k)+len(v) for k,v in form.items())>350000
            or any(len(k)>200 or len(v)>60000 for k,v in form.items())):
        raise setup_cards.Stale('The local review changed or is too large. Show the current note before requesting corrections.')
    if any(k.startswith('action_') and v.startswith('merge_') for k,v in form.items()):
        raise setup_cards.Invalid('Finish or undo the local merge choice before requesting source corrections. Your review edits are preserved.')
    for entry in row['payload']['entries']:
        for field in ('label','content'):
            name = field+'_'+entry['id']
            if name in form and form[name] != body.edits.get(entry['id'],{}).get(field,entry[field]):
                raise setup_cards.Stale('The local text and correction snapshot differ. Review it again.')
    local = knowledge_review.review_view({'version':body.version,'reviews':[row],'sources':[],'brief':''},row,form)
    result = {'notes':[{'id':n['id'],'effective_content':n['combined']} for n in local['entries']],
              'source_choices':{k:v for k,v in form.items() if k.startswith('source_') and v}}
    if sum(len(n['effective_content']) for n in result['notes'])>180000:
        raise setup_cards.Invalid('There is too much retained wording for this correction request. Undo unwanted website wording first. Your edits remain in the form.')
    return result


def correction_card(ctx, row, evidence, body, owner_request):
    card = knowledge_source_review.draft_card(row, evidence, body.edits)
    if body.note_id:
        from app import knowledge_guidance, knowledge_note_edits
        context = knowledge_review.load(ctx.tenant_id)
        _, item = knowledge_guidance.current(context, row, body.form)
        target = item.get('id') if item['kind']=='note' else None
        if not body.focus_hash or body.focus_hash != knowledge_guidance.identity(item) or target != body.note_id:
            raise setup_cards.Stale('The current note changed. Show it again before requesting a wording edit.')
        card = knowledge_note_edits.scope(card, context, row, knowledge_review.review_form(row, body.form), body.note_id, owner_request)
    card['knowledge_local_review'] = local_review(row, body)
    card['knowledge_owner_request'] = owner_request
    card['knowledge_source_units'] = row['payload'].get('source_units', [])
    return card


@router.post('/{review_id}/corrections', dependencies=[Depends(require_csrf_header)])
async def preview(review_id: UUID, body: ReviewRequest, ctx: TenantCtx = Depends(require_source_review)):
    try:
        row, evidence = state(ctx, review_id, body.version)
        if not fairuse.onboarding_allowed(ctx.tenant_id, 'concierge_onboard', client=ctx.client):
            raise HTTPException(429, 'Today\'s setup review allowance is used. You can still review manually.')
        card = correction_card(ctx, row, evidence, body, body.question)
        return await knowledge_source_review.answer(ctx.tenant_id, body.question, card, evidence)
    except Exception as exc:
        _review_error(exc)


@router.post('/{review_id}/corrections/apply', dependencies=[Depends(require_csrf_header)])
def apply(review_id: UUID, body: ApplyRequest, ctx: TenantCtx = Depends(require_source_review)):
    try:
        row, evidence = state(ctx, review_id, body.version)
        local = local_review(row,body)
        card = correction_card(ctx, row, evidence, body, body.owner_request)
        checked = knowledge_source_review.validate(card, evidence, body.review)
        payload = knowledge_source_review.corrected_payload(row, evidence, body.review, body.edits,
            owner_request=body.owner_request,reviewer=ctx.user_id,card=card)
        if local:
            payload['source_review_history'][-1]['local_review_hash'] = knowledge_review.fingerprint(local)
            if len(json.dumps(payload,allow_nan=False))>350000:
                raise setup_cards.Invalid('The correction audit is too large. Review a smaller batch. Nothing changed.')
        result = db.db().rpc('revise_knowledge_review_sources', {
            'p_tenant': ctx.tenant_id, 'p_review': str(review_id),
            'p_expected': body.version, 'p_payload': payload}).execute().data
        if result is not True:
            raise setup_cards.Stale('The draft update could not be confirmed. Reload before retrying.')
        # No final admission token is returned: recheck overlaps against the
        # fresh context before saving. Existing form edits remain in the browser.
        # The advanced stored version lets device checkpoints keep those edits
        # across a reload; the form's own version changes only with a fresh preview.
        patches = {k:v for change in checked['changes'] for k,v in change.get('patches', {}).items()}
        changed_ids = {change['id'] for change in checked['changes']}
        return {'entries': [entry for entry in payload['entries'] if entry['id'] in changed_ids], 'patches': patches,
                'version': knowledge_review.load(ctx.tenant_id)['version'],
                'summary': 'Corrections and listed source-review choices applied to drafts. Check changes and overlaps, then confirm the edited note before saving. Nothing saved to Knowledge.'}
    except Exception as exc:
        if 'BLAM8_KNOWLEDGE_REVIEW_STALE' in str(exc):
            exc = setup_cards.Stale('Knowledge or drafts changed. Reload before retrying.')
        _error(exc)
