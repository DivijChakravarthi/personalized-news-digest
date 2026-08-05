"""
JSON-backed profile storage. Replaces the old single profile.py -- profiles.json
holds a LIST of reader profiles, each with its own feeds, keyword weights,
and recipient email, so app.py's API can manage multiple profiles without a
database.

Gitignored, like profile.py was -- see profiles.example.json for the
template and structure.
"""

import json
import os
import re

PROFILES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "profiles.json")


def load_profiles() -> list[dict]:
    if not os.path.exists(PROFILES_FILE):
        raise RuntimeError(
            "profiles.json not found. Copy profiles.example.json to profiles.json and fill in your reader profile(s)."
        )
    with open(PROFILES_FILE) as f:
        try:
            profiles = json.load(f)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"profiles.json is not valid JSON: {e}") from e
    if not isinstance(profiles, list) or not profiles:
        raise RuntimeError("profiles.json must be a non-empty JSON list of profile objects.")
    return profiles


def save_profiles(profiles: list[dict]) -> None:
    with open(PROFILES_FILE, "w") as f:
        json.dump(profiles, f, indent=2)


def get_profile(profile_id: str) -> dict:
    for p in load_profiles():
        if p["id"] == profile_id:
            return p
    raise KeyError(f"No profile with id {profile_id!r}")


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "profile"


def create_profile(data: dict) -> dict:
    """id is server-generated (slug of "name", de-duplicated if needed) --
    the caller shouldn't have to invent a unique id itself. Missing
    collection fields default to empty rather than erroring, since a
    freshly-created profile in the UI starts blank and gets filled in.
    """
    profiles = load_profiles()
    base_slug = _slugify(data.get("name", "profile"))
    existing_ids = {p["id"] for p in profiles}
    slug = base_slug
    suffix = 2
    while slug in existing_ids:
        slug = f"{base_slug}-{suffix}"
        suffix += 1

    new_profile = {
        "id": slug,
        "name": data.get("name", ""),
        "recipient_email": data.get("recipient_email", ""),
        "about": data.get("about", ""),
        "sections": data.get("sections", []),
        "keywords": data.get("keywords", {}),
        "negative_keywords": data.get("negative_keywords", {}),
        "feeds": data.get("feeds", []),
    }
    profiles.append(new_profile)
    save_profiles(profiles)
    return new_profile


def update_profile(profile_id: str, data: dict) -> dict:
    """PUT semantics: full replacement of the profile body, id fixed from
    the URL (a PUT can't rename a profile's id -- that would break
    anything holding a reference to it, e.g. a scheduled run).
    """
    profiles = load_profiles()
    for i, p in enumerate(profiles):
        if p["id"] == profile_id:
            updated = {**data, "id": profile_id}
            profiles[i] = updated
            save_profiles(profiles)
            return updated
    raise KeyError(f"No profile with id {profile_id!r}")


def to_internal_profile(stored: dict) -> dict:
    """Reshapes a stored profile (profiles.json's on-disk schema) into the
    flat shape filter.py/digest.py actually consume: {"name", "about",
    "keywords", "sections"} with a single merged keywords dict (positive
    tiers + negative/suppression weights combined). This is deliberately
    the ONLY place that needs to know profiles.json's on-disk shape
    differs from what the scoring/prompt code expects -- filter.py and
    digest.py are unchanged from before multi-profile support existed.
    """
    return {
        "name": stored.get("name", ""),
        "about": stored.get("about", ""),
        "keywords": {**stored.get("keywords", {}), **stored.get("negative_keywords", {})},
        "sections": stored.get("sections", []),
    }
