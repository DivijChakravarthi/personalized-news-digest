"""
Template for profile.py. Copy this file to profile.py and edit it:

    cp profile.example.py profile.py

profile.py is gitignored so your personal reader profile never ends up in
version control -- only this generic example is tracked. See config.py for
the feed list and global settings (those aren't considered sensitive and
stay in version control).

This example describes a generic mid-level finance professional. Replace
"about" with a real description of who you're building this for, and
retune the keyword tiers to match -- add keywords for what they actually
care about, delete ones that don't apply, adjust weights. The tier
structure below (5/4/3/2/negative) is just an editing convention; filter.py
only ever sees one flat dict, so nothing breaks if you restructure it.
"""

PROFILE = {
    "name": "Reader",

    # Free-text context handed to Claude in digest.py so it understands who
    # it's curating for, beyond just keyword matches. Edit freely.
    "about": (
        "A mid-level finance professional interested in markets, interest "
        "rates, and M&A/deal activity. Already reads general financial news, "
        "so the digest should favor denser, less-obvious coverage -- rates "
        "and macro data, notable deals, and moves by major asset managers -- "
        "over front-page headlines already seen elsewhere."
    ),

    # Keyword -> weight. filter.py matches each keyword with a word-boundary
    # regex against title+summary (title hits worth 2x summary hits), sums
    # the weights of everything that matched, then keeps whatever clears
    # the score threshold (see DEFAULT_MIN_SCORE in config.py).
    #
    # Casing matters: an all-lowercase key matches case-insensitively; a
    # key with any uppercase letter matches case-sensitively -- use that
    # for short words that collide with common English (e.g. a bare "Fed"
    # would need to stay capitalized so it doesn't match "fed up"/"well-fed").
    # Plurals/possessives are handled automatically -- don't add "s" or
    # "'s" variants yourself, filter.py's stemming covers both directions.
    "keywords": {
        # --- Tier 1 (5): core daily focus -- markets and rates ---
        "federal reserve": 5,
        "Fed": 5,
        "rate cut": 5,
        "rate hike": 5,
        "yield curve": 5,
        "treasury yields": 5,
        "inflation": 5,
        "stock market": 5,
        "earnings": 5,
        "volatility": 5,
        "bond market": 5,
        "recession": 5,

        # --- Tier 2 (4): firms and deal activity worth tracking ---
        "private equity": 4,
        "venture capital": 4,
        "hedge fund": 4,
        "asset manager": 4,
        "private credit": 4,
        "fundraising": 4,
        "ipo": 4,
        "merger": 4,
        "acquisition": 4,
        "buyout": 4,

        # --- Tier 3 (3): macro that moves markets ---
        "cpi": 3,
        "gdp": 3,
        "jobs report": 3,
        "unemployment": 3,
        "ecb": 3,
        "bank of japan": 3,
        "dollar index": 3,
        "oil prices": 3,
        "tariffs": 3,
        "credit spreads": 3,

        # --- Tier 4 (2): regulatory and market structure ---
        "sec rule": 2,
        "antitrust": 2,
        "regulation": 2,
        "capital requirements": 2,
        "bankruptcy": 2,
        "clearing": 2,
        "margin": 2,
        "compliance": 2,
        "litigation": 2,
        "sanctions": 2,

        # --- Tier 5 (2): people moves worth tracking ---
        "ceo": 2,
        "executive appointment": 2,
        "fed appointment": 2,
        "board seat": 2,
        "resigns": 2,

        # --- Negative (-3): actively suppress, not just deprioritize ---
        "consumer tech review": -3,
        "celebrity": -3,
        "sports": -3,
        "personal finance tips": -3,
        "best savings account": -3,
        "retail investing advice": -3,
        "ai product launch": -3,
        "culture war": -3,
        "opinion": -3,
        "lifestyle": -3,
    },

    # Purely informational grouping passed to digest.py's prompt so Claude
    # buckets selections consistently; filter.py doesn't use this.
    "sections": ["Markets", "Deals", "Macro", "Regulatory"],
}
