"""
Generates a reader's keyword/weight taxonomy from their structured profile
(name, age, industry, position, company, plus optional free-text "about"
and topics to avoid) via one Claude call. filter.py still scores items
against a flat {keyword: weight} dict exactly as before -- this just
populates that dict automatically instead of requiring someone to hand-
author 100+ weighted keywords in the frontend.

Run standalone (`python keyword_gen.py`) to generate + print a keyword
set for the first stored profile, without touching profiles.json.
"""

import json
import logging

import anthropic
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

MODEL = "claude-sonnet-5"
# All auto-generated negative keywords share one suppression weight -- the
# UI collects *what* to avoid (topics_to_avoid), not *how much*, so there's
# no per-keyword weight for Claude to assign here.
NEGATIVE_WEIGHT = -3
TARGET_KEYWORD_COUNT = "80-120"

_client = None


class KeywordGenerationError(RuntimeError):
    pass


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env
    return _client


_SCHEMA = {
    "type": "object",
    "properties": {
        "keywords": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": (
                            "1-4 words. Lowercase for ordinary phrases. Bare acronyms/proper "
                            "nouns (Fed, CPI, FOMC, Powell) keep their natural capitalization "
                            "instead -- the filter matches those case-sensitively, which is "
                            "what keeps 'Fed' from firing on unrelated lowercase text."
                        ),
                    },
                    "weight": {
                        "type": "integer",
                        "enum": [1, 2, 3, 4, 5],
                        "description": "5 = central to their day-to-day, 1 = loosely related",
                    },
                },
                "required": ["keyword", "weight"],
                "additionalProperties": False,
            },
        },
        "negative_keywords": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Terms to actively suppress, expanded from the reader's 'topics to avoid'. Empty if none given.",
        },
    },
    "required": ["keywords", "negative_keywords"],
    "additionalProperties": False,
}


def _build_prompt(profile: dict) -> str:
    lines = [f"Name: {profile.get('name') or '(not given)'}"]
    if profile.get("age"):
        lines.append(f"Age: {profile['age']}")
    if profile.get("industry"):
        lines.append(f"Industry: {profile['industry']}")
    if profile.get("position"):
        lines.append(f"Position/role: {profile['position']}")
    if profile.get("company"):
        lines.append(f"Company: {profile['company']}")
    if profile.get("about"):
        lines.append(f"Additional context: {profile['about']}")
    if profile.get("sections"):
        lines.append(f"The digest is bucketed into these sections: {', '.join(profile['sections'])}")
    if profile.get("topics_to_avoid"):
        lines.append(f"Topics to actively avoid: {', '.join(profile['topics_to_avoid'])}")
    identity = "\n".join(lines)

    return f"""You are building a keyword-relevance filter for a daily news digest, for this reader:

{identity}

Generate {TARGET_KEYWORD_COUNT} keywords or short phrases -- companies, competitors,
people, regulators, data releases, financial/technical instruments, jargon, whatever a
person in this exact role would want a news filter to catch -- that a mechanical
keyword-matching pass can use to find relevant articles for this reader. Assign each a
weight from 5 (central to their day-to-day, e.g. their own employer or core subject
matter) down to 1 (loosely related, worth surfacing but not a priority).

Favor precise, specific terms over generic ones ("basis trade" over "finance"; a named
competitor over "competitors"). Keywords should be short (1-4 words) and not overlap
heavily in meaning with each other.

Real headlines lean on short forms, not the compounds you might reach for by default --
"Fed minutes", not "federal reserve minutes"; "CPI print", not "consumer price index
report". A keyword-matching filter that only knows the long/compound form (e.g. "fed dot
plot", "fed funds rate", "federal reserve balance sheet") will silently miss the plain
short form ("Fed" on its own) that headlines actually use most often. So for every entity,
event type, or acronym that has both a common short form and a longer/compound form,
include BOTH as separate keyword entries -- e.g. both "fed" and "federal reserve balance
sheet", both "cpi" and "cpi report", both "powell" and "jerome powell". Write bare
acronyms in their natural capitalization (e.g. "Fed", "CPI", "FOMC", "ECB", "BOJ", "BOE",
"SEC", "GDP") rather than forcing them lowercase -- the filter treats a mixed-case keyword
as case-sensitive, which also avoids false hits on unrelated lowercase words (a
capitalized "Fed" won't fire on "fed up"). Multi-word phrases should stay lowercase as
usual.

Also generate a short list of negative keywords: specific terms that should be actively
suppressed even if they'd otherwise match, expanded from the "topics to avoid" above into
related terms. Leave this list empty if no topics to avoid were given.

Respond with JSON only, matching the required schema."""


def generate_keywords(profile: dict) -> tuple[dict, dict]:
    """Returns (keywords, negative_keywords) -- flat {keyword: weight}
    dicts, the same shape filter.py has always consumed. Retries once on
    a refusal or malformed JSON; there's no ground truth to validate
    against beyond "did we get a non-empty keyword list back."
    """
    client = _get_client()
    prompt = _build_prompt(profile)

    for attempt in (1, 2):
        with client.messages.stream(
            model=MODEL,
            max_tokens=16000,
            output_config={"format": {"type": "json_schema", "schema": _SCHEMA}},
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            response = stream.get_final_message()

        if response.stop_reason == "refusal":
            logger.warning("Keyword generation declined (attempt %d)", attempt)
            continue

        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            logger.warning("Keyword generation: invalid JSON (attempt %d)", attempt)
            continue

        keywords = {}
        for item in parsed.get("keywords", []):
            # Not force-lowercased: filter.py's _build_keyword_pattern()
            # treats a mixed-case keyword as case-sensitive on purpose (see
            # the schema description above) -- lowercasing here would
            # silently turn every generated "Fed"/"CPI"/"Powell" into a
            # case-insensitive match, undoing the whole point of asking for
            # natural capitalization.
            kw = item["keyword"].strip()
            if kw:
                keywords[kw] = item["weight"]

        if not keywords:
            logger.warning("Keyword generation: empty keyword list (attempt %d)", attempt)
            continue

        negative_keywords = {}
        for kw in parsed.get("negative_keywords", []):
            kw = kw.strip().lower()
            if kw:
                negative_keywords[kw] = NEGATIVE_WEIGHT

        return keywords, negative_keywords

    raise KeywordGenerationError("Claude did not return a usable keyword list after 2 attempts")


if __name__ == "__main__":
    from profiles import load_profiles

    stored = load_profiles()[0]
    print(f"Generating keywords for profile {stored['id']!r}...\n")
    keywords, negative_keywords = generate_keywords(stored)
    print(f"{len(keywords)} keywords, {len(negative_keywords)} negative keywords\n")
    for kw, w in sorted(keywords.items(), key=lambda kv: -kv[1]):
        print(f"  {w}  {kw}")
    print()
    for kw in negative_keywords:
        print(f"  avoid  {kw}")
