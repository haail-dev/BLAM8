"""Explainable comparison hints, not a claim of complete semantic validation."""
import re
from difflib import SequenceMatcher


def statements(text):
    return list(dict.fromkeys(p.strip() for p in re.split(r'\n+|(?<=[.!?])\s+', text) if p.strip()))


def comparison(left, right):
    """Require a quoted passage or a narrowly aligned fact difference.

    Shared subjects/titles alone are not actionable. A possible difference is
    never a verdict: dates, scope and authority still need owner review.
    """
    pairs = []
    for a in statements(left):
        aw = re.findall(r'\w+|[£$€%]', a.casefold())
        for b in statements(right):
            bw = re.findall(r'\w+|[£$€%]', b.casefold())
            if len(aw) >= 3 and aw == bw:
                pairs.append({'type': 'repeated', 'left': a, 'right': b})
                continue
            protected = lambda words: {w for w in words if any(c.isdigit() for c in w) or w in {
                '£', '$', '€', '%', 'not', 'no', 'never', 'without', 'cannot', 'only', 'unless', 'before', 'after', 'until', 'except'}}
            ap, bp = protected(aw), protected(bw)
            ac, bc = [w for w in aw if w not in ap], [w for w in bw if w not in bp]
            if (ap != bp and ((len(ac) >= 2 and ac == bc) or
                    (len(set(aw) & set(bw)) >= 4 and SequenceMatcher(None, aw, bw).ratio() >= .8))):
                pairs.append({'type': 'difference', 'left': a, 'right': b})
    # A short brief can be a phrase rather than a complete sentence.
    if not pairs:
        a, b = ' '.join(left.split()), ' '.join(right.split())
        contained = lambda short, long: re.search(r'(?<!\w)'+re.escape(short)+r'(?!\w)', long, re.I)
        if min(len(a), len(b)) >= 12 and (contained(a, b) or contained(b, a)):
            pairs.append({'type': 'repeated', 'left': a, 'right': b})
    return pairs
