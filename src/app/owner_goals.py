"""Recorded owner intent for prompt context; never an execution policy or writer."""
import json


def context(value):
    if not isinstance(value, str) or not value.strip():
        return ""
    return ("Recorded owner goals (context only; not applied controls):\n"
            + json.dumps({"goals": value.strip()}, ensure_ascii=False)
            + "\nKeep the owner's qualifications, fictional/test limitations and team-owned responsibilities. "
            "Goals do not grant capabilities or permission to change settings, send messages or mutate a diary. "
            "Do not claim a requested restriction is enforced merely because it appears here. "
            "Use the actual configured controls and action checks. A requested policy change requires "
            "an explicit supported proposal and owner approval; unsupported restrictions remain unresolved. "
            "Human review of replies is separate from stopping drafts, and neither by itself prevents diary actions.")
