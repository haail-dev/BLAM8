"""Exact, owner-authored replacements; no website attribution or admission."""
from copy import deepcopy
import re

from app import knowledge_guidance, knowledge_fidelity, setup_cards


def replacements(card, identity, content, proposals):
    if not proposals:
        return content, [], {}, []
    request = card.get('knowledge_owner_request', '')
    if (not isinstance(proposals, list) or len(proposals) > 8 or not request
            or not re.search(r'\b(now|replace|correct|correction|update|change)\b', request, re.I)
            or ('?' in request and not re.search(r'\b(replace|correct|correction|update|change)\b', request, re.I))
            or re.search(r"\b(do not|don't)\s+(change|replace|update|correct)\b", request, re.I)
            or re.search(r'\b(if|maybe|perhaps|might)\b', request, re.I)):
        raise setup_cards.Invalid('Confirm the replacement fact in your own words before requesting a correction.')
    edits = []
    for proposal in proposals:
        if not isinstance(proposal, dict) or set(proposal) != {'before', 'after'}:
            raise setup_cards.Invalid('Supply exact existing wording and the owner replacement.')
        before, after = proposal['before'], proposal['after']
        if (not isinstance(before, str) or not before.strip() or content.count(before) != 1
                or not isinstance(after, str) or not after.strip() or before == after):
            raise setup_cards.Invalid('Choose one exact occurrence and a changed replacement value.')
        owner_text = knowledge_guidance._owner_excerpt(after, request)
        if not owner_text or owner_text == before:
            raise setup_cards.Invalid('Replacement wording must come from the owner request, not the website or model.')
        start = content.index(before)
        edits.append({'start':start, 'end':start+len(before), 'before':before, 'after':owner_text})
    edits.sort(key=lambda e:e['start'])
    if any(a['end'] > b['start'] for a,b in zip(edits, edits[1:])):
        raise setup_cards.Invalid('Replacement ranges must not overlap.')
    after, cursor = '', 0
    for edit in edits:
        after += content[cursor:edit['start']] + edit['after']
        cursor = edit['end']
    after += content[cursor:]
    patches = {'action_'+identity:''}
    choices = []
    for unit in card.get('knowledge_source_units', []):
        if (knowledge_fidelity._represented(unit['text'], content)
                and not knowledge_fidelity._represented(unit['text'], after)):
            if unit['kind'] == 'qualification':
                raise setup_cards.Invalid('Shared source qualifications must remain attached. Review this correction manually.')
            patches['source_'+unit['id']] = 'exclude'
            patches['source_reason_'+unit['id']] = request[:1000]
            choices.append({**deepcopy(unit), 'action':'superseded', 'destinations':[], 'reason':request})
    return after, edits, patches, choices


def segments(text, original, edits, request):
    result, cursor, offset = [], 0, 0
    for edit in edits + [{'start':len(text), 'end':len(text), 'after':''}]:
        for segment in original:
            start, end = max(cursor, segment['start']), min(edit['start'], segment['end'])
            if start < end:
                result.append({**deepcopy(segment), 'start':offset+start-cursor, 'end':offset+end-cursor})
        offset += edit['start']-cursor
        if edit['after']:
            result.append({'start':offset, 'end':offset+len(edit['after']),
                'source':{'kind':'owner_said', 'reference':request[:2000]}})
            offset += len(edit['after'])
        cursor = edit['end']
    return result
