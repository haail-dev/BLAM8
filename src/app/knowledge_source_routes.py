"""Source corrections are draft mutations; existing admission remains the writer."""
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from app import db, fairuse, knowledge_review, knowledge_source_evidence, knowledge_source_review, people, setup_cards
from app.auth import TenantCtx, require_tenant
from app.config import settings
from app.security import rate_limit, require_csrf_header
from app.setup_card_routes import _error

router = APIRouter(prefix='/onboarding/knowledge/review', dependencies=[
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


class DraftRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    version: str = Field(min_length=1, max_length=128)
    edits: dict = Field(default_factory=dict)


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
    return await knowledge_guidance.turn(ctx.tenant_id, context, row, body.form, body.question, body.focus_hash)


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


@router.post('/{review_id}/corrections', dependencies=[Depends(require_csrf_header)])
async def preview(review_id: UUID, body: ReviewRequest, ctx: TenantCtx = Depends(require_source_review)):
    try:
        row, evidence = state(ctx, review_id, body.version)
        if not fairuse.onboarding_allowed(ctx.tenant_id, 'concierge_onboard', client=ctx.client):
            raise HTTPException(429, 'Today\'s setup review allowance is used. You can still review manually.')
        card = knowledge_source_review.draft_card(row, evidence, body.edits)
        return await knowledge_source_review.answer(ctx.tenant_id, body.question, card, evidence)
    except Exception as exc:
        _error(exc)


@router.post('/{review_id}/corrections/apply', dependencies=[Depends(require_csrf_header)])
def apply(review_id: UUID, body: ApplyRequest, ctx: TenantCtx = Depends(require_source_review)):
    try:
        row, evidence = state(ctx, review_id, body.version)
        payload = knowledge_source_review.corrected_payload(row, evidence, body.review, body.edits,
            owner_request=body.owner_request,reviewer=ctx.user_id)
        result = db.db().rpc('revise_knowledge_review_sources', {
            'p_tenant': ctx.tenant_id, 'p_review': str(review_id),
            'p_expected': body.version, 'p_payload': payload}).execute().data
        if result is not True:
            raise setup_cards.Stale('The draft update could not be confirmed. Reload before retrying.')
        # No final admission token is returned: recheck overlaps against the
        # fresh context before saving. Existing form edits remain in the browser.
        return {'entries': payload['entries'], 'summary': 'Corrections applied to drafts. Check changes and overlaps before saving. Nothing saved to Knowledge.'}
    except Exception as exc:
        if 'BLAM8_KNOWLEDGE_REVIEW_STALE' in str(exc):
            exc = setup_cards.Stale('Knowledge or drafts changed. Reload before retrying.')
        _error(exc)
