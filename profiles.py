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
    the caller shouldn't have to invent a unique id itself. Everything else
    is passed through as given (app.py fills in keywords/negative_keywords
    via keyword_gen before calling this, same as update_profile).
    """
    profiles = load_profiles()
    base_slug = _slugify(data.get("name", "profile"))
    existing_ids = {p["id"] for p in profiles}
    slug = base_slug
    suffix = 2
    while slug in existing_ids:
        slug = f"{base_slug}-{suffix}"
        suffix += 1

    new_profile = {**data, "id": slug}
    profiles.append(new_profile)
    save_profiles(profiles)
    return new_profile


# Fields that describe WHO the reader is, as opposed to delivery mechanics
# (recipient_email, feeds) or output shape (sections). If none of these
# changed between the stored profile and an incoming PUT, the existing
# keyword taxonomy is still valid and app.py skips re-generating it.
IDENTITY_FIELDS = ["name", "age", "industry", "position", "company", "about", "sections", "topics_to_avoid"]


def identity_changed(existing: dict, incoming: dict) -> bool:
    return any(existing.get(f) != incoming.get(f) for f in IDENTITY_FIELDS)


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


def _synthesize_about(stored: dict) -> str:
    """digest.py's prompt leans on profile["about"] for "who is this
    reader" context. Now that the UI collects that as structured fields
    (age/industry/position/company) rather than one free-text box, this
    stitches them into an about-shaped paragraph so a profile filled in
    entirely via structured fields (empty "about") still gives Claude a
    real identity to curate for -- any free-text "about" the reader also
    wrote is appended after it, not replaced.
    """
    role_bits = [b for b in (stored.get("position"), stored.get("company")) if b]
    role_line = " at ".join(role_bits)
    if stored.get("industry"):
        role_line = f"{role_line} ({stored['industry']})" if role_line else stored["industry"]

    lead_bits = []
    if stored.get("name"):
        lead_bits.append(stored["name"])
    if stored.get("age"):
        lead_bits.append(f"age {stored['age']}")
    lead = ", ".join(lead_bits)

    identity_line = f"{lead}{', ' if lead and role_line else ''}{role_line}.".strip() if (lead or role_line) else ""

    parts = [p for p in (identity_line, stored.get("about", "")) if p]
    return "\n\n".join(parts)


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
        "about": _synthesize_about(stored),
        "keywords": {**stored.get("keywords", {}), **stored.get("negative_keywords", {})},
        "sections": stored.get("sections", []),
    }
