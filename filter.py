"""
Takes raw items from fetch.py and narrows them down to candidates for
digest.py to hand to Claude: dedupe against sent.json, drop anything
outside the recency window, score by keyword relevance (against a given
profile's keywords), keep everything with a positive score (capped at
TOP_N).

The keyword score here is a cheap pre-filter to keep the Claude call in
digest.py a manageable size -- it is NOT the thing that decides what's in
the final digest. Claude does that. So this deliberately does not gate on
a score threshold; see DEFAULT_MIN_SCORE/MIN_ITEMS_FLOOR/MIN_POSSIBLE_THRESHOLD
in config.py if a stricter cutoff ever needs to come back.

Run standalone (`python filter.py`) to fetch live feeds, print what
survives filtering, and see the near-miss diagnostics below the cut line --
uses the first profile in profiles.json.
"""

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone

from config import MAX_AGE_HOURS
from fetch import fetch_all

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

SENT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sent.json")
TOP_N = 60

# --- ranking knobs -----------------------------------------------------
# Title hits count for more than summary hits -- a keyword in the headline
# is a stronger relevance signal than the same word buried in boilerplate.
TITLE_MULTIPLIER = 2
SUMMARY_MULTIPLIER = 1
# -------------------------------------------------------------------------

# Some feeds mix in non-article boilerplate (e.g. NYT DealBook always
# includes a "Sign Up for DealBook" newsletter promo with a fresh
# timestamp, so it'd otherwise pass the recency filter every single day).
# Add substrings here as you spot more of these.
_JUNK_LINK_SUBSTRINGS = ("/newsletters/signup/",)


def _build_keyword_pattern(keyword: str) -> re.Pattern:
    """Compile one profile keyword into a word-boundary regex.

    Two things this handles that plain substring matching didn't:

    - Case sensitivity is driven by how the keyword is written in
      config.py: an all-lowercase key ("federal reserve") matches
      case-insensitively as before; a key with any uppercase letter
      ("Fed") matches case-sensitively, so "Fed" doesn't fire on
      unrelated lowercase text like "fed up".

    - Singular/plural and possessive forms: the keyword's last word has a
      trailing "s" stripped (naive stemming, not real NLP), and the regex
      always allows an optional trailing "'s"/"s" back. So "oil prices"
      in config.py becomes a pattern that matches "oil price", "oil
      prices", or "oil price's" in the text -- and "Fed" matches "Fed",
      "Fed's", or "Feds". This is bidirectional: it doesn't matter which
      form (singular or plural) you write in config.py or which form the
      article uses.
    """
    words = keyword.split(" ")
    last = words[-1]
    # Guard against short words: stemming "ares" (4 letters) down to "are"
    # would recreate the exact collision problem this is meant to avoid
    # ("are" is one of the most common words in English). Require at least
    # 5 letters before the trailing "s" so what's left is still distinctive.
    if len(last) > 4 and last.endswith("s") and not last.endswith("ss"):
        words[-1] = last[:-1]
    stem = " ".join(words)

    pattern = r"\b" + re.escape(stem) + r"[’']?s?\b"
    flags = 0 if keyword != keyword.lower() else re.IGNORECASE
    return re.compile(pattern, flags)


def build_keyword_patterns(keywords: dict) -> dict[str, re.Pattern]:
    """Compile every keyword in a profile's keyword dict once. Multiple
    profiles now exist (each with different keywords), so this can no
    longer be a module-level constant computed from one global profile --
    callers compile once per run (see score_all()) and pass the result
    down, rather than recompiling per item per keyword."""
    return {kw: _build_keyword_pattern(kw) for kw in keywords}


def load_sent_links() -> set[str]:
    """sent.json is just {"links": [url, ...]}. Missing/corrupt -> empty set,
    so a bad file degrades to "nothing deduped" rather than crashing the run."""
    if not os.path.exists(SENT_FILE):
        return set()
    try:
        with open(SENT_FILE) as f:
            data = json.load(f)
        return set(data.get("links", []))
    except (json.JSONDecodeError, AttributeError):
        logger.warning("sent.json unreadable, treating as empty")
        return set()


def append_sent_links(new_links: list[str]) -> None:
    """Called by main.py after a digest is actually sent, not from here."""
    existing = load_sent_links()
    existing.update(new_links)
    with open(SENT_FILE, "w") as f:
        json.dump({"links": sorted(existing)}, f, indent=2)


def is_recent(item: dict, max_age_hours: int = MAX_AGE_HOURS) -> bool:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    return item["published"] >= cutoff


def score_item(item: dict, keywords: dict, patterns: dict[str, re.Pattern] | None = None) -> tuple[int, list[str]]:
    """The entire ranking algorithm lives here: for every profile keyword
    whose word-boundary regex matches, add weight*TITLE_MULTIPLIER if it
    hit the title and/or weight*SUMMARY_MULTIPLIER if it hit the summary
    (both can fire for the same keyword). Swap this out for TF-IDF/
    embeddings/whatever without touching filter_items() -- it only cares
    that this returns a comparable number plus a human-readable "what
    matched" list for the near-miss diagnostics below.

    `patterns` lets score_all() pass in patterns compiled once for the
    whole run instead of recompiling per item; omit it (e.g. for a one-off
    call, or the near-miss diagnostics below) and it compiles on the fly.
    """
    if patterns is None:
        patterns = build_keyword_patterns(keywords)
    title = item["title"]
    summary = item["summary"]
    score = 0
    matched = []
    for kw, weight in keywords.items():
        pattern = patterns[kw]
        if pattern.search(title):
            contribution = weight * TITLE_MULTIPLIER
            score += contribution
            matched.append(f"{kw} [title {contribution:+d}]")
        if pattern.search(summary):
            contribution = weight * SUMMARY_MULTIPLIER
            score += contribution
            matched.append(f"{kw} [summary {contribution:+d}]")
    return score, matched


def score_all(items: list[dict], profile: dict, sent_links: set[str] | None = None) -> list[tuple[int, dict, list[str]]]:
    """Dedupe/drop-stale/drop-junk, then score everything against `profile`
    (the internal shape from profiles.to_internal_profile()) -- no
    threshold applied yet. Returns (score, item, matched) sorted by score
    descending (ties broken by recency). filter_items() applies the actual
    cutoff to this; the __main__ block below also uses the unfiltered list
    directly for near-miss diagnostics.
    """
    if sent_links is None:
        sent_links = load_sent_links()

    keywords = profile["keywords"]
    patterns = build_keyword_patterns(keywords)

    scored = []
    for item in items:
        if not item["link"] or item["link"] in sent_links:
            continue
        if any(junk in item["link"] for junk in _JUNK_LINK_SUBSTRINGS):
            continue
        if not is_recent(item):
            continue
        score, matched = score_item(item, keywords, patterns)
        scored.append((score, item, matched))

    scored.sort(key=lambda t: (t[0], t[1]["published"]), reverse=True)
    return scored


def filter_items(items: list[dict], profile: dict, sent_links: set[str] | None = None, top_n: int = TOP_N) -> dict:
    """Returns {"items", "count", "all_scored"}.

    No threshold, no floor-relaxation: keeps everything with score > 0
    (i.e. matched at least one keyword, net of any negative-weight
    suppression), sorted by score descending, capped at top_n. Precision
    is Claude's job in digest.py, not this module's -- this just needs to
    hand over a manageably-sized, roughly-ranked candidate pool.
    """
    scored = score_all(items, profile, sent_links)
    selected = [t for t in scored if t[0] > 0][:top_n]
    return {
        "items": [t[1] for t in selected],
        "count": len(selected),
        "all_scored": scored,
    }


if __name__ == "__main__":
    from profiles import load_profiles, to_internal_profile

    stored = load_profiles()[0]
    profile = to_internal_profile(stored)
    print(f"Testing with profile {stored['id']!r}\n")

    raw_items = fetch_all(stored["feeds"])
    result = filter_items(raw_items, profile)

    print(f"\n{result['count']} / {len(raw_items)} items passed filtering (score > 0, capped at {TOP_N})\n")
    for item in result["items"]:
        score, matched = score_item(item, profile["keywords"])
        print(f"[{score:3d}] [{item['source']}] {item['title']}")
        print(f"      {item['link']}")
        print(f"      matched: {', '.join(matched)}")

    # Near-miss diagnostics: highest-scoring items that did NOT pass (score
    # <= 0), so you can see what's almost relevant and spot keyword gaps.
    passed_links = {i["link"] for i in result["items"]}
    near_misses = [t for t in result["all_scored"] if t[1]["link"] not in passed_links][:15]
    print(f"\n--- Top {len(near_misses)} near-misses (score <= 0) ---\n")
    for score, item, matched in near_misses:
        print(f"[{score:3d}] [{item['source']}] {item['title']}")
        print(f"      matched: {', '.join(matched) if matched else '(none)'}")
