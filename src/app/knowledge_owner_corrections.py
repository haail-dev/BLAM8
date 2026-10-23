"""Exact, owner-authored replacements; no website attribution or admission."""
from copy import deepcopy
from difflib import SequenceMatcher
import re

from app import knowledge_guidance, knowledge_fidelity, setup_cards


def replacements(card, identity, content, proposals):
    if not proposals:
        return content, [], {}, []
    request = card.get('knowledge_owner_request', '')
    if (not isinstance(proposals, list) or len(proposals) > 8 or not request
            or not re.search(r'\b(now|replace|correct|correction|update|change|remove|not|instead)\b', request, re.I)
            or ('?' in request and not re.search(r'\b(replace|correct|correction|update|change)\b', request, re.I))
            or re.search(r"\b(do not|don't)\s+(change|replace|update|correct)\b", request, re.I)
            or re.search(r'\b(if|maybe|perhaps|might)\b', request, re.I)):
        raise setup_cards.Invalid('Confirm the replacement fact in your own words before requesting a correction.')
    edits = []
    for proposal in proposals:
        if not isinstance(proposal, dict) or set(proposal) != {'before', 'after'}:
            raise setup_cards.Invalid('Supply exact existing wording and the owner replacement.')
        before, after = proposal['before'], proposal['after']
        if (not isinstance(before, str) or not before.strip() or before not in content
                or not isinstance(after, str) or before == after):
            raise setup_cards.Invalid('Choose one exact occurrence and a changed replacement value.')
        if not after.strip():
            start = content.rfind(before)
            if (not re.search(r'\b(duplicate[ds]?|repeat(?:ed|ing|s)?)\b', request, re.I)
                    or start == 0 or content[start-1] not in ' \n\r\t'
                    or not before.rstrip().endswith(('.', '!', '?'))
                    or not knowledge_fidelity._represented(before, content[:start])):
                raise setup_cards.Invalid('I can remove repeated wording only when the same statement remains in this note.')
            edits.append({'start':start, 'end':start+len(before), 'before':before, 'after':''})
            continue
        if content.count(before) != 1:
            raise setup_cards.Invalid('Please identify which occurrence should change.')
        owner_text = knowledge_guidance._owner_excerpt(after, request)
        if owner_text and not re.search(r'(?<!\w)'+re.escape(owner_text)+r'(?!\w)', request, re.I):
            owner_text = None
        if not owner_text:
            # A model may return a whole sentence for "we now close at 4 pm".
            # Unchanged words already belong to the draft; only newly introduced
            # complete tokens need owner evidence. Never accept novel model prose.
            old = re.findall(r'\s+|\w+|[^\w\s]', before)
            new = re.findall(r'\s+|\w+|[^\w\s]', after)
            changes = [new[j:k] for tag, a, b, j, k in SequenceMatcher(None, old, new, autojunk=False).get_opcodes() if tag != 'equal']
            if not changes or any(not ''.join(part).strip() or not knowledge_guidance._owner_excerpt(''.join(part).strip(), request) for part in changes):
                raise setup_cards.Invalid('Please tell me the new value to use. I will keep the rest of the note unchanged.')
            # Require word boundaries: a request for 14 must not authorise 4.
            if any(not re.search(r'(?<!\w)'+re.escape(''.join(part).strip())+r'(?!\w)', request, re.I) for part in changes):
                raise setup_cards.Invalid('Please confirm the exact new value for this note.')
            start = content.index(before)
            for tag, a, b, j, k in SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
                if tag != 'equal':
                    edits.append({'start':start+len(''.join(old[:a])), 'end':start+len(''.join(old[:b])),
                                  'before':''.join(old[a:b]), 'after':''.join(new[j:k])})
            continue
        if owner_text.casefold() == before.casefold():
            raise setup_cards.Invalid('The proposed wording does not change this note.')
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
