"""Bounded owner-reviewed paragraph removal; immutable source evidence stays intact."""

from copy import deepcopy
import re

from app import (
    knowledge_fidelity as fidelity,
    knowledge_review,
    knowledge_source_evidence as evidence,
    setup_cards,
)


def paragraphs(text):
    """Server refs identify exact occurrences, never model-calculated offsets."""
    result = []
    for index, match in enumerate(
        re.finditer(r"\S(?:.*?\S)?(?=\n[ \t]*\n|$)", text, re.S)
    ):
        result.append(
            {
                "reference": f"paragraph:{index}:{evidence.digest(match.group())[:20]}",
                "start": match.start(),
                "end": match.end(),
                "quote": match.group(),
            }
        )
    return result


def scope(card, context, row, form, note_id, owner_request):
    """Use the assistant-displayed editable body; source attachments stay separate."""
    shown = knowledge_review.review_view(context, row, form)
    note = next((n for n in shown["entries"] if n["id"] == note_id), None)
    if not note:
        raise setup_cards.Invalid(
            "Choose the current note before requesting a wording edit."
        )
    line = next(l for l in card["lines"] if l["id"] == note_id)
    content = line["fields"]["content"]
    if len(content) > 4000:
        raise setup_cards.Invalid(
            "This displayed note is too long for a bounded assistant edit."
        )
    card["knowledge_edit_scope"] = {
        "id": note_id,
        "owner_request": owner_request,
        "paragraphs": paragraphs(content),
        "displayed_body": content,
        "effective_content": note["combined"],
        "form": deepcopy(form),
        "units": deepcopy(row["payload"].get("source_units", [])),
        "matches": [m["key"] for m in note["matches"]],
        "others": [
            {"id": n["id"], "label": n["label"], "content": n["combined"]}
            for n in shown["entries"]
            if n["id"] != note_id and form.get("action_" + n["id"]) == "keep"
        ],
    }
    return card


def removal(card, identity, content, references):
    edit = card.get("knowledge_edit_scope")
    if not references:
        return content, [], [], {}
    if (
        not edit
        or edit["id"] != identity
        or not isinstance(references, list)
        or len(references) > 12
    ):
        raise setup_cards.Invalid(
            "Paragraph removals are available only for the current note."
        )
    available = {p["reference"]: p for p in edit["paragraphs"]}
    if any(not isinstance(r, str) or r not in available for r in references) or len(
        set(references)
    ) != len(references):
        raise setup_cards.Invalid("Choose each exact current paragraph at most once.")
    selected = sorted([available[r] for r in references], key=lambda p: p["start"])
    cursor, pieces = 0, []
    for paragraph in selected:
        if content[paragraph["start"] : paragraph["end"]] != paragraph["quote"]:
            raise setup_cards.Stale(
                "The paragraph changed. Review the current note again."
            )
        pieces.append(content[cursor : paragraph["start"]])
        cursor = paragraph["end"]
    pieces.append(content[cursor:])
    after = "".join(pieces).strip()
    if not after:
        raise setup_cards.Invalid(
            "A wording edit cannot remove the entire note. Use the separate Leave out decision."
        )
    # Preserve every surviving character; separators may remain deliberately.
    retained = [after] + [n["content"] for n in edit["others"]]
    removed = "\n\n".join(p["quote"] for p in selected)
    for paragraph in selected:
        if (
            fidelity.QUALIFICATION.search(paragraph["quote"])
            or fidelity.CONSTRAINT.search(paragraph["quote"])
        ) and not any(
            fidelity._represented(paragraph["quote"], text) for text in retained
        ):
            raise setup_cards.Invalid(
                "This edit would remove a source caveat or negative limitation. It remains attached; review a narrower edit."
            )
    choices, patches = (
        [],
        {"action_" + identity: "", **{"match_" + key: "" for key in edit["matches"]}},
    )
    form = edit["form"]
    for unit in edit["units"]:
        if not fidelity._represented(unit["text"], content):
            continue
        destinations = [
            n
            for n in [{"id": identity, "label": "This note", "content": after}]
            + edit["others"]
            if fidelity._represented(unit["text"], n["content"])
        ]
        if destinations:
            # A duplicate occurrence can disappear without excluding its facts.
            if form.get("source_" + unit["id"]) == "note:" + identity and not any(
                n["id"] == identity for n in destinations
            ):
                patches["source_" + unit["id"]] = "note:" + destinations[0]["id"]
                choices.append(
                    {
                        "id": unit["id"],
                        "text": unit["text"],
                        "sources": unit["sources"],
                        "action": "retained",
                        "destinations": [n["label"] for n in destinations],
                        "reason": "",
                    }
                )
            continue
        if not fidelity._represented(unit["text"], removed):
            raise setup_cards.Invalid(
                "The paragraph contains only part of a source statement. Its other clauses cannot be excluded with this edit. Review a narrower correction."
            )
        if unit["kind"] in ("qualification", "constraint"):
            raise setup_cards.Invalid(
                "This edit would drop a unique source caveat or negative limitation. Nothing changed."
            )
        reason = edit["owner_request"].strip()
        if not reason or len(reason) > 1000:
            raise setup_cards.Invalid(
                "Keep the exact edit request within 1,000 characters when it also excludes source wording."
            )
        current = form.get("source_" + unit["id"], "")
        if current.startswith("note:") and current != "note:" + identity:
            raise setup_cards.Invalid(
                "That source statement is retained in another destination. Review its wording there before changing this choice."
            )
        patches["source_" + unit["id"]] = "exclude"
        patches["source_reason_" + unit["id"]] = reason
        choices.append(
            {
                "id": unit["id"],
                "text": unit["text"],
                "sources": unit["sources"],
                "action": "excluded",
                "destinations": [],
                "reason": reason,
            }
        )
    return after, deepcopy(selected), choices, patches


def clip_segments(text, segments, removals):
    """Retain exact source spans across deletion, including trim boundaries."""
    cuts = [(p["start"], p["end"]) for p in removals]
    keep, cursor = [], 0
    for start, end in cuts:
        if cursor < start:
            keep.append((cursor, start))
        cursor = end
    if cursor < len(text):
        keep.append((cursor, len(text)))
    untrimmed = "".join(text[a:b] for a, b in keep)
    left = len(untrimmed) - len(untrimmed.lstrip())
    right = len(untrimmed.rstrip())
    result, offset = [], 0
    for a, b in keep:
        for segment in segments:
            start = max(a, segment["start"])
            end = min(b, segment["end"])
            if start >= end:
                continue
            new_start = max(offset + start - a, left)
            new_end = min(offset + end - a, right)
            if new_start < new_end:
                result.append(
                    {
                        "start": new_start - left,
                        "end": new_end - left,
                        "source": deepcopy(segment["source"]),
                    }
                )
        offset += b - a
    return result


def binding(card):
    edit = card["knowledge_edit_scope"]
    line = next(l for l in card["lines"] if l["id"] == edit["id"])
    return {
        "id": edit["id"],
        "content_hash": evidence.digest(line["fields"]["content"]),
        "request_hash": evidence.digest(edit["owner_request"]),
        "form_hash": knowledge_review.fingerprint(edit["form"]),
    }
