"""Public topics shared with the versioned product content bundle."""
from app.platform_content import public_topics

TOPICS = public_topics()
BY_SLUG = {t["slug"]: t for t in TOPICS}
