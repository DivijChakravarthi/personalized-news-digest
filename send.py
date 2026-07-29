"""
Phase 3: renders the digest as HTML (Jinja2) and sends it via Resend.

Run standalone (`python send.py`) to render a preview from placeholder
items to _preview.html (open it in a browser) and, if RESEND_API_KEY is
set, optionally send a real test email -- without needing to run the full
fetch -> filter -> digest pipeline first.
"""

import logging
import os
from datetime import datetime

import resend
from dotenv import load_dotenv
from jinja2 import Environment, FileSystemLoader, select_autoescape

from config import EMAIL, PROFILE

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")

_jinja_env = Environment(
    loader=FileSystemLoader(TEMPLATE_DIR),
    autoescape=select_autoescape(["html"]),
)


def render_digest_html(items: list[dict], date: datetime | None = None) -> str:
    """Groups items by section (preserving the order sections first appear
    in Claude's ranked list, not alphabetical -- that order already
    reflects what Claude judged most important) and renders the template.
    """
    date = date or datetime.now()
    sections_by_name: dict[str, list[dict]] = {}
    for item in items:
        sections_by_name.setdefault(item["section"], []).append(item)
    # "articles", not "items" -- a dict key named "items" collides with
    # the dict's own built-in .items() method under Jinja2's attribute
    # lookup (section.items resolves to the method, not the key).
    sections = [{"name": name, "articles": section_items} for name, section_items in sections_by_name.items()]

    template = _jinja_env.get_template("digest.html")
    # %-d (no leading zero) is a POSIX strftime extension -- fine on macOS/
    # Linux (what this is meant to run on), would need %#d on Windows.
    return template.render(sections=sections, date_str=date.strftime("%A, %B %-d, %Y"))


def build_subject(items: list[dict], date: datetime | None = None) -> str:
    date = date or datetime.now()
    date_str = date.strftime("%b %-d")
    if not items:
        return f"{date_str}: Digest (no items today)"
    return f"{date_str}: {items[0]['headline']}"


def send_digest(items: list[dict], to: str | None = None) -> dict:
    """Sends via Resend. Raises on failure rather than swallowing it --
    main.py decides what a failed send means for sent.json (see main.py:
    links are only appended after this returns successfully), so this
    function's only job is send-or-raise, not partial bookkeeping.
    """
    resend.api_key = os.environ["RESEND_API_KEY"]

    html = render_digest_html(items)
    subject = build_subject(items)
    recipient = to or EMAIL["to"]

    params = {
        "from": EMAIL["from"],
        "to": [recipient],
        "subject": subject,
        "html": html,
        "reply_to": EMAIL["reply_to"],
    }
    response = resend.Emails.send(params)
    logger.info("Sent digest to %s (Resend id: %s)", recipient, response.get("id") if hasattr(response, "get") else response)
    return response


if __name__ == "__main__":
    sample_items = [
        {
            "headline": "Sample headline for template preview",
            "two_sentence_summary": "This is a placeholder summary sentence. This is the second placeholder sentence.",
            "why_it_matters": "This is a placeholder why-it-matters line, shown in italics.",
            "section": PROFILE["sections"][0],
            "link": "https://example.com/test-article-1",
            "source": "Example Source",
        },
        {
            "headline": "Second sample item in a different section",
            "two_sentence_summary": "Another placeholder summary. Second sentence for this one too.",
            "why_it_matters": "Placeholder relevance line for the second item.",
            "section": PROFILE["sections"][1] if len(PROFILE["sections"]) > 1 else PROFILE["sections"][0],
            "link": "https://example.com/test-article-2",
            "source": "Another Example Source",
        },
    ]

    html = render_digest_html(sample_items)
    preview_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_preview.html")
    with open(preview_path, "w") as f:
        f.write(html)
    print(f"Rendered preview written to {preview_path} -- open it in a browser to check the template.")
    print(f"Subject line would be: {build_subject(sample_items)!r}")

    if os.environ.get("RESEND_API_KEY"):
        if input("RESEND_API_KEY is set -- send a real test email now? [y/N] ").strip().lower() == "y":
            send_digest(sample_items)
    else:
        print("RESEND_API_KEY not set -- skipping live send test.")
