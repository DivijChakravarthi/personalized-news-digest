"""
Feed list + global settings. Your reader profile (the "about" text and
keyword tiers) lives in profile.py instead -- gitignored, so it doesn't
end up in version control. See profile.example.py for the template.
"""

import os

import feedparser
from dotenv import load_dotenv

# Needed here (not just in digest.py/send.py) because this module reads
# DIGEST_* env vars at import time, below -- and config.py is typically the
# first of our own modules anything else imports, so if we don't load .env
# here, nothing downstream can rely on it having happened yet.
load_dotenv()

try:
    from profile import PROFILE
except ImportError as e:
    raise ImportError(
        "profile.py not found. Copy profile.example.py to profile.py and fill in your reader profile."
    ) from e


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} not set. Copy .env.example to .env and fill in {name}.")
    return value


# SEC EDGAR (and some gov sites) reject feedparser's default User-Agent with
# a 403 -- SEC's fair-access policy requires a descriptive UA with a real
# contact address, which is exactly why this can't be hardcoded to a
# specific person's email -- it has to be whoever is actually running this.
feedparser.USER_AGENT = f"news-digest/1.0 (personal use; contact: {_require_env('DIGEST_CONTACT_EMAIL')})"

# How far back filter.py looks for "recent" items. Widened from 24h to 48h
# -- sent.json dedupe already prevents the same article being sent twice,
# so a wider window costs nothing and catches late-evening publishing plus
# low-frequency official sources (Fed/ECB/BoE/EDGAR filings) that only post
# a few times a week rather than daily.
MAX_AGE_HOURS = 48

# send.py / main.py. "from" must be on a domain verified in your Resend
# account -- resend.dev's shared sandbox address (onboarding@resend.dev)
# works for testing without verifying your own domain, but Resend will
# reject anything else until you do. reply_to is intentionally your own
# inbox, not the "from" address, so replying to the digest just emails you.
EMAIL = {
    "to": _require_env("DIGEST_TO"),
    "from": "News Digest <onboarding@resend.dev>",  # swap once you verify a domain in Resend
    "reply_to": _require_env("DIGEST_REPLY_TO"),
}

# NOT applied by filter.py right now -- the keyword score is a cheap
# pre-filter to keep the Claude call in digest.py a manageable size, not
# the thing that decides digest content, so filter_items() currently just
# keeps everything with score > 0 (capped at TOP_N) and lets Claude do the
# real curation. These are kept here in case stricter score-gating in
# filter.py itself is ever wanted again -- see filter.py's git history /
# the DEFAULT_MIN_SCORE-era filter_items() for how they were wired up.
DEFAULT_MIN_SCORE = 3
MIN_ITEMS_FLOOR = 6
MIN_POSSIBLE_THRESHOLD = 1

# name: shown as the item's source in the digest.
# url: must be a working RSS/Atom feed -- feedparser handles both.
# category: informational only right now (not used in scoring), kept in
#   case you want to weight whole feeds later.
FEEDS = [
    # --- Markets ---
    # WSJ Markets (feeds.content.dowjones.io/public/rss/RSSMarketsMain) was
    # dropped entirely: its live-coverage dispatches repeatedly mismatched
    # title/summary to the wrong article URL (confirmed on 2+ separate
    # items across different days) with no structural way to detect it
    # from the feed data -- every WSJ item's link is unreliable, not just
    # the ones we happened to catch.
    {"name": "CNBC Finance", "url": "https://www.cnbc.com/id/10000664/device/rss/rss.html", "category": "markets"},
    {"name": "CNBC Economy", "url": "https://www.cnbc.com/id/20910258/device/rss/rss.html", "category": "markets"},
    {"name": "MarketWatch Top Stories", "url": "https://feeds.content.dowjones.io/public/rss/mw_topstories", "category": "markets"},
    # MarketPulse itself is dead (confirmed stale on both the legacy domain
    # and its content.dowjones.io mirror -- this one isn't a domain-migration
    # issue, MarketWatch just stopped updating that specific feed). Bulletins
    # is the closest live equivalent (quick real-time market-moving items).
    {"name": "MarketWatch Bulletins", "url": "https://feeds.content.dowjones.io/public/rss/mw_bulletins", "category": "markets"},
    {"name": "Yahoo Finance", "url": "https://finance.yahoo.com/news/rssindex", "category": "markets"},
    {"name": "Investing.com", "url": "https://www.investing.com/rss/news.rss", "category": "markets"},
    {"name": "Seeking Alpha Market Currents", "url": "https://seekingalpha.com/market_currents.xml", "category": "markets"},
    {"name": "TheStreet", "url": "https://www.thestreet.com/.rss/full/", "category": "markets"},

    # --- Rates / FX (backfill for the WSJ Markets drop) ---
    # Investing.com's category-specific Forex feed, not their general "all
    # news" feed above -- keeps FX/rates stories from getting crowded out
    # by the general feed's 10-item cap. Content checked live: tightly on
    # this reader's lane (USD/JPY vol, Fed/BoE decisions, BoJ hawkishness).
    {"name": "Investing.com Forex", "url": "https://www.investing.com/rss/news_1.rss", "category": "markets"},
    {"name": "FXStreet", "url": "https://www.fxstreet.com/rss/news", "category": "markets"},

    # --- M&A / Deals ---
    {"name": "NYT DealBook", "url": "https://rss.nytimes.com/services/xml/rss/nyt/DealBook.xml", "category": "deals"},
    # Dealbreaker dropped: confirmed dormant, no real posts since 2026-04-09
    # (their homepage also 403s). Its "current" timestamp in the raw feed
    # was just the channel-level lastBuildDate auto-refreshing, not a new
    # post -- don't be fooled by that if re-checking later.

    # --- Macro ---
    {"name": "The Economist Finance & Economics", "url": "https://www.economist.com/finance-and-economics/rss.xml", "category": "macro"},

    # --- Official sources (central banks) ---
    {"name": "Federal Reserve - Press Releases (All)", "url": "https://www.federalreserve.gov/feeds/press_all.xml", "category": "official"},
    {"name": "Federal Reserve - Press Releases (Monetary Policy)", "url": "https://www.federalreserve.gov/feeds/press_monetary.xml", "category": "official"},
    {"name": "Federal Reserve - Speeches", "url": "https://www.federalreserve.gov/feeds/speeches.xml", "category": "official"},
    {"name": "ECB - Press Releases", "url": "https://www.ecb.europa.eu/rss/press.html", "category": "official"},
    {"name": "Bank of England - News", "url": "https://www.bankofengland.co.uk/rss/news", "category": "official"},
    # BIS and IMF still have no working public feed (BIS: every path 404s
    # or redirects to an HTML page; IMF: 403s everything). BOJ's own
    # English RSS works, unlike either of those.
    {"name": "Bank of Japan - What's New", "url": "https://www.boj.or.jp/en/rss/whatsnew.xml", "category": "official"},

    # --- Trade press ---
    {"name": "FT Alphaville", "url": "https://www.ft.com/alphaville?format=rss", "category": "trade"},
    {"name": "Risk.net", "url": "https://www.risk.net/feeds/rss", "category": "trade"},
    {"name": "Private Debt Investor", "url": "https://www.privatedebtinvestor.com/feed/", "category": "trade"},

    # SEC EDGAR filing feeds (8-K + 13D for a handful of specific companies)
    # were removed entirely: 260 items fetched, 0 selections, because
    # neither the atom feed's <title> nor <summary> carries the subject
    # company or any other usable signal -- every entry title is a generic
    # "8-K - Current report" / "SC 13D/A [Amend] - General Statement of
    # Acquisition of Beneficial Ownership", and <summary> is just filing
    # metadata (date/accession#/size). The company name only exists on the
    # linked index.htm page, which would need a second scrape per filing --
    # out of proportion for a title-only relevance check. Confirmed
    # directly against a live fetch before dropping, not assumed.
]
