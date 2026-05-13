"""
Shared template helpers — registered as Jinja globals in both the live
FastAPI app and the static-site build script. Keep them small and pure;
they're called from Jinja and from Python equally.
"""

from __future__ import annotations

import re
from urllib.parse import quote


_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slugify_topic(name: str) -> str:
    """Stable URL-safe slug for an OpenAlex topic name.

    Lowercase, non-alphanumerics collapsed to single hyphens, trimmed.
    """
    s = (name or "").lower()
    s = _SLUG_STRIP.sub("-", s).strip("-")
    return s or "topic"


def topic_url_live(name: str) -> str:
    """Topic URL for the live FastAPI app — uses URL-encoded display name."""
    return f"/topic/{quote(name)}"


def topic_url_static(name: str) -> str:
    """Topic URL for the static site — uses the slug, trailing slash so
    GitHub Pages serves the directory index."""
    return f"/topic/{slugify_topic(name)}/"
