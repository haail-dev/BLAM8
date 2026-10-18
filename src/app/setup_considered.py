"""Bounded assistant notes about controls; never executable settings."""
from copy import deepcopy

from app import setup_catalogue, setup_cards


def merge(page, current, updates, turn):
    if not isinstance(updates, list) or len(updates) > 100:
        raise setup_cards.Invalid("Supply at most one hundred considered controls.")
    result = {item['key']: deepcopy(item) for item in current}
    seen = set()
    for item in updates:
        if not isinstance(item, dict) or set(item) != {'key', 'state'}:
            raise setup_cards.Invalid("A considered control needs its catalogue key and state.")
        key, state = item['key'], item['state']
        if not isinstance(key, str) or key in seen or state not in ('needs_answer', 'left_unchanged', 'remove'):
            raise setup_cards.Invalid("Invalid or repeated considered control.")
        try:
            entry = setup_catalogue.lookup(key)
        except ValueError:
            raise setup_cards.Invalid("Unknown considered control.") from None
        if entry['page'] != page:
            raise setup_cards.Invalid("The considered control belongs to another page.")
        seen.add(key)
        if state == 'remove':
            result.pop(key, None)
        else:
            result[key] = {'key': key, 'state': state, 'source_turn': turn,
                           'label': entry['label'], 'question': entry['question'],
                           'href': entry['where_on_page']}
    if len(result) > 100:
        raise setup_cards.Invalid("Too many considered controls on this page.")
    return list(result.values())
