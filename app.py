"""
Phase A: local Flask API wrapping the existing pipeline. No auth, no
database -- profiles.json is the only store, run.log is the only run
history. Meant to run alongside a local React dev server (Phase B);
Flask-CORS is enabled for that, not for any production deployment (this
is not designed or hardened to be exposed beyond localhost).

Run: python app.py  (http://localhost:5001)

Port 5001, not 5000 -- on macOS, port 5000 is claimed by the AirPlay
Receiver service by default, which silently intercepts requests before
they ever reach Flask (you'd see a 403 from "AirTunes" instead of your
route, which is a very confusing thing to debug blind).
"""

import logging
import os

from flask import Flask, jsonify, request
from flask_cors import CORS

import profiles as profile_store
from digest import DigestGenerationError, generate_digest
from fetch import fetch_all
from filter import filter_items
from keyword_gen import KeywordGenerationError, generate_keywords
from main import LOG_FILE
from main import run as run_pipeline

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)


@app.route("/api/profiles", methods=["GET"])
def list_profiles():
    return jsonify(profile_store.load_profiles())


@app.route("/api/profiles", methods=["POST"])
def create_profile():
    """A new profile has no keyword taxonomy yet, so generation always
    runs here (unlike PUT, there's no "did anything change" to check).
    """
    data = request.get_json(force=True, silent=True)
    if not data or not data.get("name"):
        return jsonify({"error": "profile must include at least a 'name'"}), 400
    try:
        data["keywords"], data["negative_keywords"] = generate_keywords(data)
    except KeywordGenerationError as e:
        return jsonify({"error": str(e)}), 502
    created = profile_store.create_profile(data)
    return jsonify(created), 201


@app.route("/api/profiles/<profile_id>", methods=["PUT"])
def update_profile(profile_id):
    """The frontend never sends keywords/negative_keywords -- they're not
    editable there anymore. Keywords are (re)generated from the identity
    fields (name/age/industry/position/company/about/sections/
    topics_to_avoid) only when one of those actually changed, so e.g.
    fixing a typo in a feed URL doesn't burn a Claude call.
    """
    data = request.get_json(force=True, silent=True)
    if not data:
        return jsonify({"error": "request body must be a JSON profile object"}), 400
    try:
        existing = profile_store.get_profile(profile_id)
    except KeyError:
        return jsonify({"error": f"no profile with id {profile_id!r}"}), 404

    if not existing.get("keywords") or profile_store.identity_changed(existing, data):
        try:
            data["keywords"], data["negative_keywords"] = generate_keywords(data)
        except KeywordGenerationError as e:
            return jsonify({"error": str(e)}), 502
    else:
        data["keywords"] = existing["keywords"]
        data["negative_keywords"] = existing["negative_keywords"]

    updated = profile_store.update_profile(profile_id, data)
    return jsonify(updated)


@app.route("/api/digest/preview", methods=["POST"])
def preview_digest():
    """Runs fetch -> filter -> select for one profile and returns the
    selected items WITH each one's filter score and matched keywords --
    the whole point of this endpoint is showing why something got picked,
    not just what got picked, so weights can be tuned from the UI without
    touching Python. Never sends.
    """
    data = request.get_json(force=True, silent=True) or {}
    profile_id = data.get("profile_id")
    if not profile_id:
        return jsonify({"error": "request body must include 'profile_id'"}), 400

    try:
        stored = profile_store.get_profile(profile_id)
    except KeyError:
        return jsonify({"error": f"no profile with id {profile_id!r}"}), 404

    profile = profile_store.to_internal_profile(stored)
    raw_items = fetch_all(stored["feeds"])
    filtered = filter_items(raw_items, profile)

    try:
        digest_items, usage = generate_digest(filtered["items"], profile)
    except DigestGenerationError as e:
        return jsonify({"error": str(e), "usage": e.usage}), 502

    # Look up each selected item's filter score/matched keywords by link
    # against the full scored candidate pool filter.py already built --
    # digest.py doesn't compute or return this itself, it's purely a
    # filter.py concept, joined back in here for display.
    score_by_link = {item["link"]: (score, matched) for score, item, matched in filtered["all_scored"]}
    source_by_link = {c["link"]: c["source"] for c in filtered["items"]}
    for item in digest_items:
        score, matched = score_by_link.get(item["link"], (None, []))
        item["score"] = score
        item["matched_keywords"] = matched
        item["source"] = source_by_link.get(item["link"], "Unknown source")

    return jsonify(
        {
            "profile_id": profile_id,
            "items": digest_items,
            "raw_count": len(raw_items),
            "candidate_count": filtered["count"],
            "usage": usage,
        }
    )


@app.route("/api/digest/send", methods=["POST"])
def send_digest_route():
    """Full pipeline for one profile, actually sends. Reuses main.run()
    rather than reimplementing it, so CLI and API runs behave identically
    (including writing to run.log and sent.json).
    """
    data = request.get_json(force=True, silent=True) or {}
    profile_id = data.get("profile_id")
    if not profile_id:
        return jsonify({"error": "request body must include 'profile_id'"}), 400
    to_override = data.get("to")

    try:
        result = run_pipeline(dry_run=False, to=to_override, profile_id=profile_id)
    except KeyError:
        return jsonify({"error": f"no profile with id {profile_id!r}"}), 404
    except DigestGenerationError as e:
        return jsonify({"error": str(e)}), 502
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return jsonify(result)


@app.route("/api/runs", methods=["GET"])
def list_runs():
    """Parses run.log's tab-separated lines (see main.py's _log_run) into
    JSON objects, most recent first. run.log is the only run history --
    no database, so this is just a text-file parse, not a query.
    """
    if not os.path.exists(LOG_FILE):
        return jsonify([])

    runs = []
    with open(LOG_FILE) as f:
        for line in f:
            line = line.rstrip("\n")
            if not line:
                continue
            parts = line.split("\t")
            entry = {"timestamp": parts[0]}
            detail_parts = []
            for part in parts[1:]:
                if "=" in part:
                    key, _, value = part.partition("=")
                    entry[key] = value
                else:
                    detail_parts.append(part)
            if detail_parts:
                entry["detail"] = " ".join(detail_parts)
            runs.append(entry)

    runs.reverse()
    return jsonify(runs)


if __name__ == "__main__":
    app.run(debug=True, port=5001)
