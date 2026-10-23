"""One field contract for Voice review and server advancement."""
from fastapi import HTTPException
import json

REQUIRED = {'agent_name': 'agent or team name', 'tone': 'tone of voice', 'sign_off': 'sign-off'}
SNAPSHOT_FIELDS = ('business_brief', 'agent_name', 'tone', 'sign_off', 'voice_notes',
                   'voice_profile_source', 'voice_learning_status', 'voice_profile',
                   'voice_sample_draft')


def snapshot(values):
    return {key:values.get(key) for key in SNAPSHOT_FIELDS}


def save(ctx, raw_expected, values):
    """The original form snapshot is checked inside the same transaction as saving."""
    from app import db, people
    require(values)
    if ctx.viewing_as:raise HTTPException(403,'Voice cannot be changed while viewing as another member.')
    member=db.get_member(ctx.tenant_id,ctx.user_id)
    if not people.can_do(people.member(member) if member else None,'settings','change'):
        raise HTTPException(403,'Changing Voice requires settings permission.')
    try:
        expected = json.loads(raw_expected)
        if not isinstance(expected,dict) or set(expected)!=set(SNAPSHOT_FIELDS):raise ValueError()
    except (TypeError,ValueError):
        raise HTTPException(409,'This Voice form has no current saved version. Reload before saving.') from None
    try:
        result=db.db().rpc('save_onboarding_voice', {'p_tenant':ctx.tenant_id,
            'p_expected':expected,'p_values':values,'p_required':list(REQUIRED)}).execute().data
        if result is not True:raise ValueError()
    except Exception as exc:
        if 'BLAM8_VOICE_STALE' in str(exc):
            raise HTTPException(409,'Voice changed since this form opened. Your local entries remain; reload to compare before saving.') from None
        raise HTTPException(503,'Voice could not be saved. Your local entries remain; check the saved values before retrying.') from None


def missing(values):
    return [label for key, label in REQUIRED.items()
            if not isinstance(values.get(key), str) or not values[key].strip()]


def complete(values):
    return not missing(values)


def require(values):
    absent = missing(values)
    if absent:
        raise HTTPException(422, 'Complete Voice before continuing: ' + ', '.join(absent) + '.')


def require_saved(ctx):
    from app import db
    values = db.get_tenant_config(ctx.tenant_id, client=ctx.client) or {}
    require(values)
    return values
