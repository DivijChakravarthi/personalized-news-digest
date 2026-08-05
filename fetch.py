"""
Pulls every feed in a given feed list (each profile in profiles.json has
its own) and normalizes entries into plain dicts:
{title, link, summary, source, published}

Run standalone (`python fetch.py`) to sanity-check that feeds are alive and
what raw volume they produce, before any filtering happens -- uses the
first profile in profiles.json.
"""

import calendar
import logging
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone

import feedparser

import config  # noqa: F401 -- sets feedparser.USER_AGENT as a side effect on import

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<[^<]+?>")

# Feeds that link through a tracking redirect instead of the article
# directly. Only URLs matching these markers pay the extra HTTP round trip
# in _resolve_redirect() -- everything else is used as-is.
_REDIRECT_LINK_MARKERS = (
    "marketwatch.com/bulletins/redirect/",
    # add more tracking/redirect URL patterns here as you spot them
)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # Returning None here makes urllib raise HTTPError(301/302/...) instead
    # of silently following it -- that's what lets _resolve_redirect() read
    # the Location header off the redirect response itself.
    def redirect_request(self, *args, **kwargs):
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirect)


def _resolve_redirect(url: str) -> str | None:
    """Follow a known tracking-redirect link (e.g. MarketWatch bulletins)
    to its canonical article URL.

    Can't just GET the link and see where it ends up: the destination
    article page 401s bot traffic even though the redirect itself works
    fine, so "follow and check the result" fails on the wrong step. Instead
    this sends a single HEAD request with redirects disabled and reads the
    Location header directly off the 3xx response, then strips the
    tracking query string (?g=...&mod=...). Returns None if the link can't
    be resolved -- a dead or unresolvable tracking link is worse to ship
    in the digest than dropping the item entirely.
    """
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": feedparser.USER_AGENT})
        _NO_REDIRECT_OPENER.open(req, timeout=8)
        return url  # 2xx -- wasn't actually a redirect, nothing to resolve
    except urllib.error.HTTPError as e:
        if 300 <= e.code < 400 and e.headers.get("Location"):
            return e.headers["Location"].split("?")[0]
        logger.warning("Redirect resolution for %s returned status %d", url, e.code)
        return None
    except Exception as e:
        logger.warning("Failed to resolve redirect link %s: %s", url, e)
        return None


def _parse_published(entry) -> datetime:
    # Feeds inconsistently populate published_parsed vs updated_parsed (and
    # some skip both). Try both before falling back to "now" -- silently
    # defaulting to now is deliberate: it's safer for a fresh, undated item
    # to survive the recency filter than for a bad/missing date to kill it.
    #
    # feedparser already normalizes published_parsed/updated_parsed to UTC
    # regardless of the feed's original timezone (EST, GMT, +0530, whatever
    # -- it parses the offset and converts). calendar.timegm() is the
    # correct way to turn that UTC struct_time into a Unix timestamp.
    # time.mktime() would be wrong here: it assumes its input is LOCAL
    # time, so feeding it an already-UTC struct silently shifts every
    # timestamp by this machine's UTC offset -- and that offset (and thus
    # the bug) would silently change depending on what timezone the script
    # happens to run in.
    for key in ("published_parsed", "updated_parsed"):
        struct = entry.get(key)
        if struct:
            return datetime.fromtimestamp(calendar.timegm(struct), tz=timezone.utc)
    return datetime.now(timezone.utc)


def _clean_summary(entry) -> str:
    # Many feeds (WSJ, CNBC, Forbes...) embed raw HTML in <description>.
    # Strip tags so downstream code (keyword matching, the Claude prompt)
    # sees plain text.
    raw = entry.get("summary") or entry.get("description") or ""
    return _TAG_RE.sub("", raw).strip()


def fetch_feed(feed: dict) -> list[dict]:
    """Fetch one feed. Never raises -- a dead feed just yields an empty list."""
    items = []
    try:
        parsed = feedparser.parse(feed["url"])
        # bozo=1 means the XML was malformed; still use whatever entries
        # feedparser managed to salvage, only bail if there's nothing usable.
        if parsed.bozo and not parsed.entries:
            raise parsed.get("bozo_exception", ValueError("empty/malformed feed"))

        for entry in parsed.entries:
            link = entry.get("link", "").strip()
            if any(marker in link for marker in _REDIRECT_LINK_MARKERS):
                resolved = _resolve_redirect(link)
                if resolved is None:
                    continue  # drop -- couldn't resolve to a canonical article link
                link = resolved

            items.append(
                {
                    "title": entry.get("title", "").strip(),
                    "link": link,
                    "summary": _clean_summary(entry),
                    "source": feed["name"],
                    "published": _parse_published(entry),
                }
            )
    except Exception as e:
        logger.warning("Failed to fetch %s (%s): %s", feed["name"], feed["url"], e)
    return items


def fetch_all(feeds: list[dict]) -> list[dict]:
    all_items = []
    for feed in feeds:
        feed_items = fetch_feed(feed)
        logger.info("%-35s %d items", feed["name"], len(feed_items))
        all_items.extend(feed_items)
    logger.info("Fetched %d items total from %d feeds", len(all_items), len(feeds))
    return all_items


if __name__ == "__main__":
    from profiles import load_profiles

    profile = load_profiles()[0]
    print(f"Testing with profile {profile['id']!r} ({len(profile['feeds'])} feeds)\n")

    items = fetch_all(profile["feeds"])
    print(f"\n{len(items)} total items fetched\n")
    for it in items[:15]:
        print(f"- [{it['source']}] {it['title']}")
