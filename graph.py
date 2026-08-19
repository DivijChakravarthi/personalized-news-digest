"""
Phase C: the pipeline as a LangGraph graph, instead of the linear function
calls main.run() used to make directly. Every node wraps an EXISTING
function from fetch.py/filter.py/digest.py/send.py -- none of their
internals were rewritten for this; see digest.py's _generate_digest_attempt
for the one deliberate extraction (a single-attempt version of the Claude
call, so the graph -- not digest.py -- owns the retry loop).

The retry loop (select -> validate -> select) is the actual point of this
refactor: `validate` runs a NEW check generate_digest() never had --
"does this headline plausibly describe the article at its own link" -- by
comparing headline tokens against the candidate's original title and its
URL slug. A single-pass pipeline had no way to catch a headline/article
mismatch; this one gets up to two more tries with the specific mismatch
named in the prompt.

Run standalone (`python graph.py`) to print the graph as Mermaid --
same as `python main.py --show-graph`, without touching main.py's CLI
plumbing.
"""

import logging
import re
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from html import unescape
from typing import TypedDict

import feedparser
from langgraph.graph import END, StateGraph

import digest
import send
from fetch import fetch_all
from filter import append_sent_links, filter_items
from profiles import to_internal_profile

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# validate -> select retries at most this many times before the graph gives
# up correcting and sends whatever the last attempt produced.
MAX_SELECT_ATTEMPTS = 2

# enrich: only bother fetching an article's page for candidates whose feed
# summary is this short -- a feed that already hands back a real paragraph
# doesn't need the extra network round trip.
SHORT_SUMMARY_THRESHOLD = 200
ENRICH_MAX_WORKERS = 6
ENRICH_TIMEOUT_SECONDS = 6
ENRICH_MAX_BYTES = 200_000  # cap how much of the page we read looking for a <meta> tag

_META_DESC_RE = [
    re.compile(
        r'<meta[^>]+(?:name=["\']description["\']|property=["\']og:description["\'])[^>]*content=["\']([^"\']*)["\']',
        re.IGNORECASE,
    ),
    re.compile(
        r'<meta[^>]+content=["\']([^"\']*)["\'][^>]*(?:name=["\']description["\']|property=["\']og:description["\'])',
        re.IGNORECASE,
    ),
]

_STOPWORDS = {
    "the", "a", "an", "to", "of", "in", "on", "for", "and", "or", "at", "is",
    "its", "this", "that", "with", "as", "by", "from", "after", "before",
    "over", "under", "into", "amid", "amidst", "says", "say", "said", "new",
    "how", "why", "what", "who", "are", "was", "were", "be", "been", "will",
    "would", "could", "can", "than", "his", "her", "their", "our", "your",
}


class PipelineState(TypedDict):
    raw_items: list[dict]
    candidates: list[dict]
    selected_items: list[dict]
    validation_errors: list[str]
    attempt_count: int
    day_type: str
    profile: dict  # stored (on-disk) shape -- nodes derive the internal shape via to_internal_profile() as needed
    run_metadata: dict


def _touch(state: PipelineState, node_name: str, **extra) -> dict:
    """Every node returns its run_metadata through this -- LangGraph's
    default reducer for a plain dict key is "last write wins", not a
    merge, and nearly every node needs to append to run_metadata["path"]
    without clobbering what other nodes already wrote there. So each node
    reads the current run_metadata and returns a full updated copy rather
    than a partial one.
    """
    meta = dict(state.get("run_metadata") or {})
    meta["path"] = [*meta.get("path", []), node_name]
    meta.update(extra)
    return meta


# --- fetch / filter / route_day -----------------------------------------


def fetch_node(state: PipelineState) -> dict:
    stored = state["profile"]
    raw_items = fetch_all(stored["feeds"])
    return {"raw_items": raw_items, "run_metadata": _touch(state, "fetch")}


def filter_node(state: PipelineState) -> dict:
    stored = state["profile"]
    profile = to_internal_profile(stored)
    filtered = filter_items(state["raw_items"], profile)
    return {
        "candidates": filtered["items"],
        "run_metadata": _touch(
            state, "filter", raw_count=len(state["raw_items"]), candidate_count=filtered["count"]
        ),
    }


def route_day_node(state: PipelineState) -> dict:
    day_type = "weekend" if datetime.now().weekday() >= 5 else "weekday"
    return {"day_type": day_type, "run_metadata": _touch(state, "route_day")}


def _route_day_condition(state: PipelineState) -> str:
    return state["day_type"]


def week_ahead_node(state: PipelineState) -> dict:
    # Stub -- weekend digests (e.g. "what to watch this week" instead of
    # "what happened today") aren't built yet. Routing here is correct;
    # there's just nothing to do yet but say so and end the run cleanly
    # rather than falling through to a weekday-shaped select/send.
    logger.info("week_ahead: weekend digest not implemented yet -- skipping run")
    return {
        "selected_items": [],
        "run_metadata": _touch(
            state, "week_ahead", status="skipped", detail="week_ahead not implemented yet (weekend run)"
        ),
    }


# --- enrich ----------------------------------------------------------------


def _extract_description(html: str) -> str | None:
    for pattern in _META_DESC_RE:
        m = pattern.search(html)
        if m:
            desc = unescape(m.group(1)).strip()
            if desc:
                return desc
    return None


def _fetch_description(url: str) -> str | None:
    """Best-effort GET of `url` looking for a meta description. Any
    failure (timeout, non-200, no matching tag, decode error) returns
    None -- enrich_node treats that as "leave the summary as-is", never
    as something that should fail the run.
    """
    try:
        # feedparser.USER_AGENT is set by config.py's import-time side
        # effect (fetch.py already triggers that import); reusing it here
        # keeps a single identifying UA/contact-email string across every
        # outbound request this project makes, same as fetch.py's own
        # redirect-resolution HEAD request does.
        req = urllib.request.Request(url, headers={"User-Agent": feedparser.USER_AGENT})
        with urllib.request.urlopen(req, timeout=ENRICH_TIMEOUT_SECONDS) as resp:
            raw = resp.read(ENRICH_MAX_BYTES)
        html = raw.decode("utf-8", errors="ignore")
    except Exception:
        return None
    return _extract_description(html)


def enrich_node(state: PipelineState) -> dict:
    candidates = state["candidates"]
    to_enrich = [c for c in candidates if len(c.get("summary") or "") < SHORT_SUMMARY_THRESHOLD]
    if not to_enrich:
        return {"run_metadata": _touch(state, "enrich", enriched_count=0)}

    descriptions: dict[str, str] = {}
    # Capped concurrency + a short per-request timeout is what keeps this
    # from turning a 30s run into a multi-minute one on a slow candidate
    # pool -- worst case is ENRICH_TIMEOUT_SECONDS, not
    # ENRICH_TIMEOUT_SECONDS * len(to_enrich).
    with ThreadPoolExecutor(max_workers=ENRICH_MAX_WORKERS) as pool:
        futures = {pool.submit(_fetch_description, c["link"]): c["link"] for c in to_enrich}
        for future in as_completed(futures):
            link = futures[future]
            try:
                desc = future.result()
            except Exception:
                desc = None
            if desc:
                descriptions[link] = desc

    if descriptions:
        for c in candidates:
            if c["link"] in descriptions:
                c["summary"] = descriptions[c["link"]]

    return {
        "candidates": candidates,
        "run_metadata": _touch(state, "enrich", enriched_count=len(descriptions), enrich_attempted=len(to_enrich)),
    }


# --- select / validate -----------------------------------------------------


def _build_corrective_note(errors: list[str]) -> str:
    if not errors:
        return ""
    return "Note: your previous response had these problems -- fix them this time:\n" + "\n".join(
        f"- {e}" for e in errors
    ) + "\n\n"


def select_node(state: PipelineState) -> dict:
    candidates = state["candidates"]
    attempt_count = state["attempt_count"] + 1
    usage = dict(state["run_metadata"].get("usage") or digest._new_usage())

    if not candidates:
        # Nothing for Claude to curate -- filter_node already found zero
        # keyword-relevant items. Skip the API call entirely rather than
        # spending a call on an empty candidate list (generate_digest()
        # has always short-circuited the same way).
        return {
            "selected_items": [],
            "validation_errors": [],
            "attempt_count": attempt_count,
            "run_metadata": _touch(state, "select", usage=usage),
        }

    profile = to_internal_profile(state["profile"])
    schema = digest._build_json_schema(profile["sections"])
    client = digest._get_client()
    retry_note = _build_corrective_note(state["validation_errors"])

    items, errors, _ = digest._generate_digest_attempt(candidates, profile, schema, client, retry_note, usage)

    if items is None:
        logger.warning("select attempt %d: %s", attempt_count, "; ".join(errors))
        return {
            # Keep whatever the previous attempt produced (if any) as the
            # best-available fallback -- an attempt that fails outright
            # (refusal/malformed JSON) shouldn't erase a previous
            # attempt's usable-but-imperfect result.
            "selected_items": state.get("selected_items", []),
            "validation_errors": errors,
            "attempt_count": attempt_count,
            "run_metadata": _touch(state, "select", usage=usage),
        }

    return {
        "selected_items": items,  # raw, pre-diversity-cap -- validate_node finalizes this
        "validation_errors": [],
        "attempt_count": attempt_count,
        "run_metadata": _touch(state, "select", usage=usage),
    }


def _tokenize(text: str) -> set[str]:
    words = re.findall(r"[a-zA-Z0-9]+", (text or "").lower())
    return {w for w in words if len(w) >= 3 and w not in _STOPWORDS}


def _slug_tokens(url: str) -> set[str]:
    path = urllib.parse.urlparse(url or "").path
    return _tokenize(path.replace("-", " ").replace("_", " ").replace("/", " "))


def _check_headline_plausibility(items: list[dict], candidates: list[dict]) -> list[str]:
    """NEW check generate_digest()'s own _validate() never had: does this
    headline plausibly describe the article it links to? Claude is asked
    to write its OWN headline (not copy the source's), so an exact-text
    match is never expected -- what's checked instead is whether the
    headline shares at least one substantive word with either the
    candidate's original title or its URL slug. Zero overlap with BOTH is
    the signature of a genuine mismatch (Claude paired the right link with
    the wrong story, or vice versa), not just a stylistic rewrite.
    """
    title_by_link = {c["link"]: c["title"] for c in candidates}
    errors = []
    for item in items:
        link = item.get("link", "")
        headline = item.get("headline", "")
        original_title = title_by_link.get(link, "")
        headline_tokens = _tokenize(headline)
        reference_tokens = _slug_tokens(link) | _tokenize(original_title)
        if headline_tokens and reference_tokens and not (headline_tokens & reference_tokens):
            errors.append(
                f"headline {headline!r} shares no keywords with its linked article's own title "
                f"({original_title!r}) or URL -- likely a headline/article mismatch, double-check "
                f"the link {link!r} actually belongs to this headline"
            )
    return errors


def validate_node(state: PipelineState) -> dict:
    candidates = state["candidates"]
    if not candidates:
        return {"validation_errors": [], "run_metadata": _touch(state, "validate")}

    raw_items = state["selected_items"]
    existing_errors = state["validation_errors"]
    if not raw_items and existing_errors:
        # select_node's own attempt failed outright (refusal/malformed
        # JSON/business-rule violation already reported by
        # _generate_digest_attempt) -- nothing new to check here, just
        # let those errors flow into the retry-routing decision.
        return {"run_metadata": _touch(state, "validate")}

    profile = to_internal_profile(state["profile"])
    parsed = {"items": raw_items}
    errors = digest._validate(parsed, candidates, profile)
    errors += _check_headline_plausibility(raw_items, candidates)

    if errors:
        return {"validation_errors": errors, "run_metadata": _touch(state, "validate")}

    # Clean -- enforce source/theme diversity caps deterministically (this
    # is "checked" by construction: _enforce_diversity_caps() can't
    # produce a result that violates them, so there's nothing to retry
    # here, only to apply). Attach "source" now (not in render_send) so
    # main.py's dry-run print path -- which runs after the graph, outside
    # any node -- has it too.
    final_items = digest._enforce_diversity_caps(raw_items, candidates)
    link_to_source = {c["link"]: c["source"] for c in candidates}
    for item in final_items:
        item["source"] = link_to_source.get(item["link"], "Unknown source")

    return {
        "selected_items": final_items,
        "validation_errors": [],
        "run_metadata": _touch(state, "validate"),
    }


def _route_after_validate(state: PipelineState) -> str:
    if state["validation_errors"] and state["attempt_count"] < MAX_SELECT_ATTEMPTS:
        return "retry"
    if not state["candidates"] or state["run_metadata"].get("dry_run"):
        return "stop"
    return "send"


# --- render_send -------------------------------------------------------


def render_send_node(state: PipelineState) -> dict:
    items = state["selected_items"]
    stored = state["profile"]
    recipient = state["run_metadata"].get("to") or stored.get("recipient_email")

    send.send_digest(items, to=recipient)
    append_sent_links([item["link"] for item in items])

    return {"run_metadata": _touch(state, "render_send", status="success")}


# --- graph assembly ---------------------------------------------------


def build_graph():
    graph = StateGraph(PipelineState)

    graph.add_node("fetch", fetch_node)
    graph.add_node("filter", filter_node)
    graph.add_node("route_day", route_day_node)
    graph.add_node("enrich", enrich_node)
    graph.add_node("week_ahead", week_ahead_node)
    graph.add_node("select", select_node)
    graph.add_node("validate", validate_node)
    graph.add_node("render_send", render_send_node)

    graph.set_entry_point("fetch")
    graph.add_edge("fetch", "filter")
    graph.add_edge("filter", "route_day")
    graph.add_conditional_edges("route_day", _route_day_condition, {"weekday": "enrich", "weekend": "week_ahead"})
    graph.add_edge("enrich", "select")
    graph.add_edge("select", "validate")
    graph.add_conditional_edges("validate", _route_after_validate, {"retry": "select", "stop": END, "send": "render_send"})
    graph.add_edge("render_send", END)
    graph.add_edge("week_ahead", END)

    return graph.compile()


def print_mermaid() -> None:
    print(build_graph().get_graph().draw_mermaid())


if __name__ == "__main__":
    print_mermaid()
