"""Shared authoring guidance; never a licence to rewrite approved Knowledge."""

SUBJECT_NOTES = (
    "Create subject-focused notes: one independently maintainable fact or closely related group "
    "of facts per note. Before writing, partition the supplied facts by the question each answers. A note must answer only that question. Check each sentence against its title; move sentences answering another question into their own note, retaining necessary qualifications. In particular, an overview must not also list service areas, exclusions, contact routes or operating hours. Give each note a specific short title describing its entire content, in "
    "the business's own vocabulary. Broad coverage categories are tags, not note boundaries. "
    "Separate business overview, included services, service exclusions, service area, opening "
    "hours and contact routes when the source supplies those distinct subjects; do not require "
    "these subjects or titles for every business. Split a new subject into a new note rather "
    "than placing it under an unrelated title. Do not split every sentence or price line: keep "
    "a coherent price schedule or ordered procedure together. Keep each amount, permission or "
    "rule with its applicable person, service, unit, conditions, exceptions and uncertainty. "
    "Repeat a necessary qualification when separating facts would otherwise change their scope. "
    "Do not repeat a general fact across notes just to fill them. Never infer missing facts "
    "or settings. Smaller notes are reviewable drafts, not approval to save or to split existing "
    "saved notes without the owner's request."
)


def partition_subjects(entry):
    """Partition exact model-authored claims only when they cover the entire draft.

    Subject assignment is model judgment; this guard proves preservation, not
    semantic correctness. Unsafe/incomplete assignments leave the original intact.
    No saved notes or owner edits enter this extraction-only function.
    """
    from copy import deepcopy
    normal = lambda text: ' '.join(text.split())
    text = normal(entry.get('content') or '')
    claims = {}
    for proof in entry.get('sources', []):
        if not isinstance(proof, dict):
            return [entry]
        subject, claim = (proof.get('subject') or '').strip(), normal(proof.get('claim') or '')
        if not subject or not claim:
            return [entry]
        if claim in claims and claims[claim][0].casefold() != subject.casefold():
            return [entry]
        claims.setdefault(claim, (subject, []))[1].append(proof)
    if len({s.casefold() for s, _ in claims.values()}) < 2:
        return [entry]
    cursor, ordered = 0, []
    while cursor < len(text):
        if text[cursor].isspace():
            cursor += 1
            continue
        options = [claim for claim in claims if text.startswith(claim, cursor)]
        if len(options) != 1:
            return [entry]
        claim = options[0]
        # Do not separate a clause from the remainder of its sentence.
        end = cursor + len(claim)
        if end < len(text) and (claim[-1] not in '.!?' or not text[end].isspace()):
            return [entry]
        ordered.append(claim); cursor = end
    if set(ordered) != set(claims):
        return [entry]
    groups = {}
    for claim in ordered:
        subject, proofs = claims[claim]
        group = groups.setdefault(subject.casefold(), {**deepcopy(entry), 'label': subject, 'content': '', 'sources': []})
        group['content'] += (' ' if group['content'] else '') + claim
        group['sources'].extend({**deepcopy(proof), 'claim': claim} for proof in proofs)
    return list(groups.values())
