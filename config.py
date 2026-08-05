"""
Global settings only -- no feeds, no profile. Reader profiles (each with
their own feeds, keyword weights, and recipient email) live in
profiles.json instead. See profiles.py for the loader and
profiles.example.json for the template/structure.
"""

import os

import feedparser
from dotenv import load_dotenv

# Needed here (not just in digest.py/send.py) because this module reads
# DIGEST_* env vars at import time, below -- and config.py is typically the
# first of our own modules anything else imports, so if we don't load .env
# here, nothing downstream can rely on it having happened yet.
load_dotenv()


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

# send.py. Genuinely global, unlike "to"/"reply_to" -- every profile sends
# through the same Resend sender identity, only the recipient differs (see
# each profile's "recipient_email" in profiles.json). Must be on a domain
# verified in your Resend account -- resend.dev's shared sandbox address
# works for testing without verifying your own domain, but Resend will
# reject anything else until you do.
EMAIL_FROM = "News Digest <onboarding@resend.dev>"

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
