"""
Phase 3: orchestrates the full pipeline -- fetch -> filter -> digest -> send
-- this is what you'd actually put on a cron job. Every run appends one
line to run.log (timestamp, item count, estimated model cost, success/
failure), dry-run included, so you can check a morning's run without
digging through terminal scrollback.

Usage:
    python main.py                    # full run: fetch, filter, curate, send
    python main.py --dry-run          # same, but prints instead of sending
                                       # and never touches sent.json
    python main.py --to someone@x.com # send to a different recipient
"""

import argparse
import logging
import os
from datetime import datetime, timezone

from digest import DigestGenerationError, generate_digest
from fetch import fetch_all
from filter import append_sent_links, filter_items
from send import build_subject, send_digest

LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run.log")

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def _log_run(status: str, item_count: int, cost_usd: float | None, detail: str = "") -> None:
    # Deliberately separate from the logging.basicConfig() output above --
    # that's for watching a run happen interactively; this is an
    # append-only, grep-able record of every run's outcome, meant to
    # answer "did this morning's cron job actually send" after the fact.
    timestamp = datetime.now(timezone.utc).isoformat()
    cost_str = f"${cost_usd:.4f}" if cost_usd is not None else "n/a"
    line = f"{timestamp}\tstatus={status}\titems={item_count}\tcost={cost_str}"
    if detail:
        line += f"\t{detail}"
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def run(dry_run: bool = False, to: str | None = None) -> None:
    raw_items = fetch_all()
    filtered = filter_items(raw_items)
    logger.info("%d candidates after filtering", filtered["count"])

    if not filtered["items"]:
        logger.warning("No candidates passed filtering -- nothing to send")
        _log_run("skipped", 0, None, "no candidates after filtering")
        return

    try:
        digest_items, usage = generate_digest(filtered["items"])
    except DigestGenerationError as e:
        logger.error("Digest generation failed: %s", e)
        _log_run("failed", 0, e.usage.get("estimated_cost_usd"), f"digest error: {e}")
        raise

    cost = usage.get("estimated_cost_usd")

    # digest.py's JSON schema deliberately has no "source" field -- Claude's
    # job is editorial content, not repeating metadata we already have and
    # could get wrong by trusting it to echo back correctly. Attach it here
    # from the original candidate list, keyed by the (schema-validated,
    # verbatim-copied) link.
    link_to_source = {c["link"]: c["source"] for c in filtered["items"]}
    for item in digest_items:
        item["source"] = link_to_source.get(item["link"], "Unknown source")

    if dry_run:
        print(f"\n[DRY RUN] {len(digest_items)} items -- not sending, sent.json not updated\n")
        print(f"Subject: {build_subject(digest_items)}\n")
        for item in digest_items:
            print(f"[{item['section']}] {item['headline']}  ({item['source']})")
            print(f"  {item['two_sentence_summary']}")
            print(f"  why it matters: {item['why_it_matters']}")
            print(f"  {item['link']}\n")
        _log_run("dry-run", len(digest_items), cost)
        return

    try:
        send_digest(digest_items, to=to)
    except Exception as e:
        logger.error("Send failed: %s", e)
        _log_run("failed", len(digest_items), cost, f"send error: {e}")
        raise

    # Only mark links as sent AFTER a successful send. If send_digest
    # raised above, we never reach here -- a failed send must NOT poison
    # sent.json, or those stories would silently vanish from tomorrow's
    # digest even though the reader never actually saw them today.
    append_sent_links([item["link"] for item in digest_items])
    logger.info("Sent %d items and updated sent.json", len(digest_items))
    _log_run("success", len(digest_items), cost)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch, filter, curate, and send today's digest.")
    parser.add_argument("--dry-run", action="store_true", help="Print the digest instead of sending; don't touch sent.json")
    parser.add_argument("--to", help="Override the recipient email for this run")
    args = parser.parse_args()

    run(dry_run=args.dry_run, to=args.to)
