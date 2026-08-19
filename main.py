"""
Phase 3, now Phase C: orchestrates the full pipeline -- fetch -> filter ->
route_day -> enrich -> select -> validate -> render_send -- as a LangGraph
graph (graph.py) instead of a linear chain of function calls. This is what
you'd actually put on a cron job. Every run appends one line to run.log
(timestamp, item count, estimated model cost, path taken through the
graph, retry count, success/failure), dry-run included, so you can check a
morning's run without digging through terminal scrollback.

Usage:
    python main.py                    # full run: fetch, filter, curate, send
                                       # (defaults to the first profile in profiles.json)
    python main.py --profile <id>     # run a specific profile by id
    python main.py --dry-run          # traverses the whole graph but stops
                                       # before render_send -- prints instead
                                       # of sending, never touches sent.json
    python main.py --to someone@x.com # override the recipient for this run
    python main.py --show-graph       # print the graph as Mermaid and exit
"""

import argparse
import logging
import os
from datetime import datetime, timezone

import digest
from graph import build_graph, print_mermaid
from profiles import get_profile, load_profiles
from send import build_subject

LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run.log")

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# Built once per process -- build_graph() just wires nodes together, no
# reason to redo that on every run() call.
_compiled_graph = None


def _get_graph():
    global _compiled_graph
    if _compiled_graph is None:
        _compiled_graph = build_graph()
    return _compiled_graph


def _log_run(
    status: str,
    profile_id: str,
    item_count: int,
    cost_usd: float | None,
    path: str = "",
    retries: int = 0,
    detail: str = "",
) -> None:
    # Deliberately separate from the logging.basicConfig() output above --
    # that's for watching a run happen interactively; this is an
    # append-only, grep-able record of every run's outcome, meant to
    # answer "did this morning's cron job actually send, and did it need
    # to retry" after the fact. app.py's /api/runs parses this generically
    # (any "key=value" tab-separated field), so adding path/retries here
    # is all that's needed for the Run History view to pick them up.
    timestamp = datetime.now(timezone.utc).isoformat()
    cost_str = f"${cost_usd:.4f}" if cost_usd is not None else "n/a"
    line = (
        f"{timestamp}\tprofile={profile_id}\tstatus={status}\titems={item_count}"
        f"\tcost={cost_str}\tpath={path}\tretries={retries}"
    )
    if detail:
        line += f"\t{detail}"
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def run(dry_run: bool = False, to: str | None = None, profile_id: str | None = None) -> dict:
    """Returns a small status dict ({"status", "item_count", "cost", ...})
    on every non-exceptional path (skipped/dry-run/success) -- app.py uses
    this to answer an API call without re-parsing run.log. A send failure
    still raises rather than returning a status, since that's what the CLI
    already expects to catch and exit non-zero on -- but a digest-curation
    failure no longer does: the graph's validate->select retry loop hard-
    caps at 2 attempts and then proceeds with the best available result
    instead of raising, so DigestGenerationError is no longer part of this
    function's contract.
    """
    if profile_id:
        stored = get_profile(profile_id)  # raises KeyError if unknown
    else:
        stored = load_profiles()[0]
        logger.info("No --profile given, defaulting to %r", stored["id"])

    recipient = to or stored.get("recipient_email")
    if not dry_run and not recipient:
        raise ValueError(f"Profile {stored['id']!r} has no recipient_email and no --to override was given")

    initial_state = {
        "raw_items": [],
        "candidates": [],
        "selected_items": [],
        "validation_errors": [],
        "attempt_count": 0,
        "day_type": "",
        "profile": stored,
        "run_metadata": {
            "path": [],
            "dry_run": dry_run,
            "to": to,
            "profile_id": stored["id"],
            "usage": digest._new_usage(),
        },
    }

    try:
        final_state = _get_graph().invoke(initial_state)
    except Exception as e:
        # Only a genuine send failure (send.send_digest raising inside
        # render_send_node) or an infrastructure error (e.g. fetch/filter
        # blowing up) reaches here -- digest-curation problems are handled
        # inside the graph's own retry loop and never propagate as an
        # exception. Mirrors the old "Send failed" handling: log, re-raise,
        # let the CLI exit non-zero / app.py's except Exception catch it.
        logger.error("Pipeline failed: %s", e)
        _log_run("failed", stored["id"], 0, None, detail=f"pipeline error: {e}")
        raise

    meta = final_state["run_metadata"]
    usage = meta.get("usage") or digest._new_usage()
    cost = usage.get("estimated_cost_usd")
    path_str = "->".join(meta.get("path", []))
    items = final_state["selected_items"]
    # attempt_count only increments inside select_node, which is only ever
    # reached when there were candidates to curate -- 0 candidates means
    # 0 retries by definition, regardless of attempt_count's raw value.
    retries = max(0, final_state["attempt_count"] - 1) if final_state["candidates"] else 0

    status = meta.get("status")  # explicit override: "success" (render_send ran) or "skipped" (week_ahead stub)
    if status not in ("success", "skipped"):
        if not final_state["candidates"]:
            status = "skipped"
        elif meta.get("dry_run"):
            status = "dry-run"
        else:
            status = "failed"  # shouldn't normally happen -- guard against an unexpected graph exit

    if status == "skipped":
        detail = meta.get("detail", "no candidates after filtering")
        logger.warning(detail)
        _log_run("skipped", stored["id"], 0, cost, path=path_str, retries=retries, detail=detail)
        return {"status": "skipped", "item_count": 0, "cost": cost}

    if status == "dry-run":
        print(f"\n[DRY RUN] {len(items)} items -- not sending, sent.json not updated\n")
        print(f"path: {path_str}  (retries: {retries})\n")
        print(f"Subject: {build_subject(items)}\n")
        for item in items:
            print(f"[{item.get('section','?')}] {item['headline']}  ({item.get('source', '?')})")
            print(f"  {item['two_sentence_summary']}")
            print(f"  why it matters: {item['why_it_matters']}")
            print(f"  {item['link']}\n")
        _log_run("dry-run", stored["id"], len(items), cost, path=path_str, retries=retries)
        return {"status": "dry-run", "item_count": len(items), "cost": cost, "items": items}

    if status == "failed":
        _log_run("failed", stored["id"], len(items), cost, path=path_str, retries=retries, detail="graph exited without reaching send or a known stop point")
        raise RuntimeError("Pipeline finished without sending, skipping, or a recognized stop -- see run.log")

    # success -- render_send_node already sent and updated sent.json
    logger.info("Sent %d items and updated sent.json", len(items))
    _log_run("success", stored["id"], len(items), cost, path=path_str, retries=retries)
    return {"status": "success", "item_count": len(items), "cost": cost}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch, filter, curate, and send today's digest.")
    parser.add_argument("--dry-run", action="store_true", help="Traverse the whole graph but stop before render_send; print instead of sending, don't touch sent.json")
    parser.add_argument("--to", help="Override the recipient email for this run")
    parser.add_argument("--profile", help="Profile id to run (defaults to the first profile in profiles.json)")
    parser.add_argument("--show-graph", action="store_true", help="Print the pipeline graph as a Mermaid diagram and exit")
    args = parser.parse_args()

    if args.show_graph:
        print_mermaid()
    else:
        run(dry_run=args.dry_run, to=args.to, profile_id=args.profile)
