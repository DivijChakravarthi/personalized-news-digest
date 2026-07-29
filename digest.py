"""
Phase 2: the one Claude API call that turns the keyword-filtered candidates
from filter.py into today's actual digest. fetch.py and filter.py are a
cheap mechanical pre-filter to keep this call small -- the real editorial
judgment (what's actually worth the reader's time, which stories are the
same story told three ways, what to say in plain English) happens here.

Run standalone (`python digest.py`) to fetch+filter live candidates and
print the digest Claude selects -- useful for iterating on the prompt
without touching send.py.
"""

import json
import logging

import anthropic
from dotenv import load_dotenv

from config import PROFILE
from fetch import fetch_all
from filter import filter_items

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# claude-sonnet-5 per the Anthropic API skill's default -- swap here if you
# want a cheaper/faster model for this task, nothing else needs to change.
MODEL = "claude-sonnet-5"
DIGEST_SIZE = 8
MAX_PER_SOURCE = 2
# No more than this many items may share the same "theme" (see the schema
# field below) -- stops e.g. 5 of 8 slots all being Fed/oil/Iran from
# different angles, even when no two of them are exact near-duplicates.
MAX_PER_THEME = 3
# We ask Claude to rank up to this many candidates (more than DIGEST_SIZE)
# so that after the diversity caps are enforced in Python, there's still a
# ranked backlog to backfill from -- e.g. if Private Debt Investor's best
# 4 stories are all in the top 8, the 3rd/4th get cut by the cap and the
# next-best items from OTHER sources (already written by Claude, already
# ranked) fill those slots instead of the digest just shrinking to 6.
CANDIDATE_BUFFER = 16

# claude-sonnet-5 pricing per the Anthropic API skill -- update if you
# change MODEL. Used only for the cost estimate main.py logs after each
# run. (Sonnet 5 has an introductory $2/$10 rate through 2026-08-31; this
# uses the standard post-introductory rate so the estimate doesn't quietly
# go stale once that discount ends.)
PRICE_PER_MTOK_INPUT = 3.00
PRICE_PER_MTOK_OUTPUT = 15.00

_client = None


class DigestGenerationError(RuntimeError):
    """Raised when Claude doesn't produce a usable digest after retrying.
    Carries `.usage` (tokens spent across all attempts, including the
    failed ones) so main.py can still log what the failed run cost.
    """

    def __init__(self, message: str, usage: dict):
        super().__init__(message)
        self.usage = usage


def _get_client() -> anthropic.Anthropic:
    # Lazy singleton so `import digest` (e.g. from main.py) doesn't require
    # ANTHROPIC_API_KEY to be set unless generate_digest() is actually called.
    global _client
    if _client is None:
        _client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env
    return _client


# Explicit schema handed to the API via output_config.format -- this is what
# makes "strict JSON only" actually strict: Claude's response is constrained
# to match this shape, not just asked nicely to. See _validate() below for
# the business rules this schema *can't* express (item count, links that
# must match a real candidate, section must be one of ours).
_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "headline": {
                        "type": "string",
                        "description": "Your own headline, not copied from the source",
                    },
                    "two_sentence_summary": {
                        "type": "string",
                        "description": "Exactly two sentences, entirely in your own words -- never reproduce article text",
                    },
                    "why_it_matters": {
                        "type": "string",
                        "description": "Why this matters to THIS specific reader, not a generic explanation",
                    },
                    "section": {
                        "type": "string",
                        "description": f"One of: {', '.join(PROFILE['sections'])}",
                    },
                    "theme": {
                        "type": "string",
                        "description": (
                            "A short (2-6 word) normalized label for the underlying story/event, "
                            "e.g. 'Fed July rate decision', 'Iran-Israel ceasefire'. Two items about "
                            "the same real-world event/story must get the IDENTICAL theme string, "
                            "even if their headlines differ or they come from different sources -- "
                            "this is used mechanically to detect thematic overlap, not shown to the reader."
                        ),
                    },
                    "link": {
                        "type": "string",
                        "description": "Copied verbatim from the candidate's link field",
                    },
                },
                "required": ["headline", "two_sentence_summary", "why_it_matters", "section", "theme", "link"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}


def _build_candidate_block(candidates: list[dict]) -> str:
    lines = []
    for i, item in enumerate(candidates, 1):
        # Summary truncated to keep the prompt from ballooning on the
        # (rare) feed that hands back a huge <description> blob -- 400
        # chars is plenty for Claude to judge relevance from.
        lines.append(
            f"{i}. [{item['source']}] {item['title']}\n"
            f"   link: {item['link']}\n"
            f"   summary: {item['summary'][:400]}"
        )
    return "\n".join(lines)


def _build_prompt(candidates: list[dict], retry_note: str = "") -> str:
    candidate_block = _build_candidate_block(candidates)
    sections = ", ".join(PROFILE["sections"])
    return f"""You are curating a daily news digest for one specific reader. Here is who they are:

{PROFILE['about']}

Below are {len(candidates)} candidate articles, pre-filtered by keyword relevance (a cheap
mechanical pass -- not a judgment of what actually belongs in the digest). Your job is
the real editorial judgment.

Select and RANK up to {CANDIDATE_BUFFER} candidates, best first, that this reader would
most want to see today. Only the top {DIGEST_SIZE} will actually be sent -- the rest of
your ranked list exists so a mechanical pass afterward can enforce source diversity
without the digest shrinking (see the cap rule below), so rank every item you include as
if it might be the one that makes the cut.

Rules:
- Rank up to {CANDIDATE_BUFFER} items, most digest-worthy first. If fewer than
  {DIGEST_SIZE} candidates are genuinely relevant and non-redundant, rank only those --
  don't pad with weak or repetitive picks just to fill the list.
- No more than {MAX_PER_SOURCE} items from the same source anywhere in your ranked list.
  Diversity of sources matters -- do not let one source dominate just because it happened
  to publish several relevant stories today.
- No more than {MAX_PER_THEME} items on the same underlying theme/story anywhere in your
  ranked list -- even across different sources and different sections. E.g. if the Fed
  meeting, oil prices, and Iran are all live stories today, do not let variations on that
  same cluster crowd out other parts of the reader's lane; prefer breadth across topics
  over depth on one. Give every item a "theme" label per the schema so this can be checked.
- Several candidates may cover the same underlying story from different outlets (e.g.
  three sources all reporting the same Fed decision). Cluster those together and include
  only the single best version -- never list near-duplicate stories separately.
- "headline" is your own headline, not the source's -- write it plainly, no clickbait.
- The headline and two_sentence_summary must describe the SPECIFIC article at that
  candidate's link -- not just restate whatever the candidate's title/summary says if
  that title/summary actually spans several unrelated topics (some feeds, WSJ's in
  particular, publish "Market Talk"-style roundup dispatches that blend multiple stories
  under one entry). If a candidate's given summary covers more than one unrelated topic,
  either write your headline/summary to describe the roundup as a whole -- not one thread
  cherry-picked out of it -- or skip that candidate entirely.
- "two_sentence_summary" must be exactly two sentences, entirely in your own words. Do
  not quote or closely paraphrase the source article -- the reader clicks the link for
  the full piece. Summarizing the gist is fine; reproducing sentences is not.
- "why_it_matters" must explain relevance to THIS reader specifically (their role and
  focus, per the profile above) -- not a generic "this is important because markets" line.
- "section" must be exactly one of: {sections}. Fed policy decisions, Fed leadership/
  personnel/politics, rate path, Treasury auctions, yield curve, swaps, and repo markets
  belong in "Rates & Derivatives" -- not "Macro". Reserve "Macro" for broader data
  releases (CPI, jobs, GDP), non-Fed central banks (ECB, BOJ, PBOC), FX, oil, and
  sovereign debt that move the book indirectly rather than being about the Fed itself.
- "link" must be copied verbatim from the candidate's link field below -- never invent
  or modify a URL.

{retry_note}Candidates:
{candidate_block}

Respond with JSON only, matching the required schema. List items in "items" in rank order."""


def _validate(parsed: dict, candidates: list[dict]) -> list[str]:
    """Business-rule checks the JSON schema can't express by itself: schema
    validation (already enforced by output_config.format) guarantees the
    *shape* is right, but not that the item count is sane, that "section"
    is one of OUR section names rather than a plausible-looking string
    Claude invented, or -- the important one -- that "link" is a URL that
    actually appeared in the candidate list rather than a hallucinated one.
    Returns a list of problem descriptions; empty means the response is
    good to use.
    """
    errors = []
    items = parsed.get("items")
    if not isinstance(items, list) or not items:
        return ["'items' is missing, not a list, or empty"]
    if len(items) > CANDIDATE_BUFFER:
        errors.append(f"got {len(items)} items, max is {CANDIDATE_BUFFER}")

    valid_links = {c["link"] for c in candidates}
    valid_sections = set(PROFILE["sections"])
    for i, item in enumerate(items):
        if item.get("link") not in valid_links:
            errors.append(f"item {i}: link {item.get('link')!r} doesn't match any candidate")
        if item.get("section") not in valid_sections:
            errors.append(f"item {i}: section {item.get('section')!r} not in {valid_sections}")
        # Loose sanity check, not a strict "exactly 2 sentences" count --
        # real sentence-splitting is fragile around abbreviations ("U.S.",
        # "Fed's"), so this only catches the obvious failure (no punctuation
        # at all, e.g. a single sentence fragment) rather than rejecting
        # valid two-sentence summaries over an off-by-one count.
        summary = item.get("two_sentence_summary", "")
        if not any(p in summary for p in (".", "!", "?")):
            errors.append(f"item {i}: two_sentence_summary has no sentence-ending punctuation")

    # Deliberately NOT checking the diversity caps (source/theme) here. A
    # retry only asks Claude to try again -- it can't guarantee compliance.
    # Both caps are enforced unconditionally in _enforce_diversity_caps()
    # after validation passes instead, which makes them structurally
    # impossible to violate in the final output, regardless of what Claude
    # returns.
    return errors


def _enforce_diversity_caps(items: list[dict], candidates: list[dict]) -> list[dict]:
    """Hard, deterministic enforcement of MAX_PER_SOURCE and MAX_PER_THEME
    together -- this is what makes them "can't be ignored" rather than "we
    asked nicely." Walks Claude's ranked list top to bottom, keeping each
    item only if BOTH its source and its theme are still under their cap,
    then backfills any slots that opened up by continuing further down the
    SAME ranked list. Both caps are checked in one pass over the full
    ranked buffer -- not two sequential passes -- because a sequential
    pass would apply the second cap only to whatever survived the first,
    with nothing left in reserve to backfill from; checking them together
    keeps the full CANDIDATE_BUFFER-deep backlog available to both.
    """
    link_to_source = {c["link"]: c["source"] for c in candidates}
    source_counts: dict[str, int] = {}
    theme_counts: dict[str, int] = {}
    kept = []
    dropped = []

    for item in items:
        if len(kept) >= DIGEST_SIZE:
            break
        source = link_to_source.get(item["link"], "unknown")
        theme = item.get("theme", "unknown")
        if source_counts.get(source, 0) >= MAX_PER_SOURCE:
            dropped.append((item, "source cap"))
            continue
        if theme_counts.get(theme, 0) >= MAX_PER_THEME:
            dropped.append((item, "theme cap"))
            continue
        kept.append(item)
        source_counts[source] = source_counts.get(source, 0) + 1
        theme_counts[theme] = theme_counts.get(theme, 0) + 1

    if dropped:
        logger.info(
            "Diversity caps trimmed %d item(s): %s",
            len(dropped), [f"{d['headline']} ({reason})" for d, reason in dropped],
        )
    return kept


def _new_usage() -> dict:
    return {"input_tokens": 0, "output_tokens": 0, "attempts": 0, "estimated_cost_usd": 0.0}


def _accumulate_usage(usage: dict, response) -> None:
    if response.usage is None:
        return
    usage["input_tokens"] += response.usage.input_tokens
    usage["output_tokens"] += response.usage.output_tokens
    usage["attempts"] += 1
    usage["estimated_cost_usd"] = (
        usage["input_tokens"] / 1_000_000 * PRICE_PER_MTOK_INPUT
        + usage["output_tokens"] / 1_000_000 * PRICE_PER_MTOK_OUTPUT
    )


def generate_digest(candidates: list[dict]) -> tuple[list[dict], dict]:
    """The one Anthropic API call for the whole pipeline. Retries once on
    malformed/invalid output -- "malformed" covers both JSON that fails to
    parse (shouldn't happen with output_config.format, but a refusal or SDK
    hiccup is real) and JSON that parses fine but breaks a business rule
    the schema can't express (wrong item count, a hallucinated link, an
    off-list section). The retry re-sends the same candidates with the
    specific problems appended, so Claude gets one chance to self-correct
    against the exact failure rather than guessing what went wrong.

    Returns (items, usage) -- usage accumulates tokens/cost across every
    attempt (including failed ones) so main.py can log run cost even when
    generation ultimately fails (see DigestGenerationError.usage).
    """
    usage = _new_usage()
    if not candidates:
        return [], usage

    client = _get_client()
    retry_note = ""

    for attempt in (1, 2):
        # Sonnet 5 runs adaptive thinking by default, and thinking tokens
        # count against max_tokens same as output text. 8000 was too tight
        # for CANDIDATE_BUFFER=16 fully-written items plus the theme field
        # (one run burned the whole budget on thinking before writing any
        # text). 16000 still wasn't enough once the candidate pool grew
        # past ~35 items (more candidates -> more to reason about -> more
        # thinking tokens). Streaming + 32000 gives real headroom and is
        # also just the right way to make a call this size -- non-streaming
        # requests above ~16K output risk SDK HTTP timeouts.
        with client.messages.stream(
            model=MODEL,
            max_tokens=32000,
            output_config={"format": {"type": "json_schema", "schema": _JSON_SCHEMA}},
            messages=[{"role": "user", "content": _build_prompt(candidates, retry_note)}],
        ) as stream:
            response = stream.get_final_message()
        _accumulate_usage(usage, response)

        # Claude Opus 5's safety classifiers can decline a request outright
        # (HTTP 200, not an error) -- must check before touching .content,
        # which is empty (pre-output decline) or partial (mid-stream) here.
        if response.stop_reason == "refusal":
            logger.warning("Claude declined the request (attempt %d)", attempt)
            retry_note = (
                "Note: your previous response was declined. This is a benign news "
                "curation task over public RSS headlines -- please complete it.\n\n"
            )
            continue

        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as e:
            logger.warning("Attempt %d: response wasn't valid JSON (%s)", attempt, e)
            retry_note = (
                "Note: your previous response was not valid JSON. Respond with JSON "
                "only, matching the schema exactly.\n\n"
            )
            continue

        errors = _validate(parsed, candidates)
        if not errors:
            return _enforce_diversity_caps(parsed["items"], candidates), usage

        logger.warning("Attempt %d: validation failed: %s", attempt, "; ".join(errors))
        retry_note = (
            "Note: your previous response had these problems -- fix them this time:\n"
            + "\n".join(f"- {e}" for e in errors) + "\n\n"
        )

    raise DigestGenerationError("Claude did not return a usable digest after 2 attempts", usage)


if __name__ == "__main__":
    raw_items = fetch_all()
    result = filter_items(raw_items)
    print(f"\n{result['count']} candidates passed filtering, sending to Claude...\n")

    digest, usage = generate_digest(result["items"])
    print(f"{len(digest)} items selected (~${usage['estimated_cost_usd']:.4f}, {usage['attempts']} attempt(s)):\n")
    for item in digest:
        print(f"[{item['section']}] {item['headline']}")
        print(f"  {item['two_sentence_summary']}")
        print(f"  Why it matters: {item['why_it_matters']}")
        print(f"  {item['link']}\n")
