"""Provenance Guard — Flask application (Milestone 5, production layer).

Endpoints:

* ``POST /submit``       — accepts ``{text, creator_id?}``, runs both detection
                           signals (Signal 1 Groq LLM + Signal 2 stylometry),
                           combines them into a calibrated confidence score,
                           renders the plain-language transparency label that
                           varies with that score, persists a content record,
                           writes an audit-log entry, and returns the decision.
                           Rate limited to 10/min, 100/day.
* ``POST /appeal``       — accepts ``{content_id, creator_reasoning}``, flips the
                           content's status to ``under_review``, records the
                           appeal alongside a snapshot of the original decision,
                           and writes an ``appeal_received`` audit entry.
                           Rate limited to 5/min, 20/day.
* ``GET  /review``       — the open-appeal review queue for a human reviewer.
* ``GET  /content/<id>`` — the full stored decision record for one submission.
* ``GET  /log``          — the most recent audit-log entries as JSON.
* ``GET  /health``       — liveness probe.

Shapes follow planning.md (§Architecture API contract, §5 labels, §6 appeals,
§8 rate limits, §9 data model). Rate limiting uses Flask-Limiter with in-memory
storage for the dev build (production would point ``storage_uri`` at Redis).
"""
import json
import uuid
from datetime import datetime, timezone

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

import db
from signals import combine_signals, llm_score, stylometry_score

load_dotenv()

app = Flask(__name__)
db.init_db()

# Rate limiting (planning.md §8). Per-client-IP counters; in-memory store is
# fine for the local/dev build. The global default is a backstop for any route
# without an explicit limit; expensive or abusable routes set tighter limits.
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["200 per day", "50 per hour"],
    storage_uri="memory://",
)


@app.errorhandler(429)
def ratelimit_handler(error):
    """Return a JSON 429 (not HTML) when a rate limit is exceeded."""
    return (
        jsonify(
            {
                "error": "rate limit exceeded",
                "detail": str(error.description),
            }
        ),
        429,
    )


def _now_iso():
    """Current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _new_content_id():
    """Generate a short, unique content id (planning.md uses the `c_` prefix)."""
    return "c_" + uuid.uuid4().hex[:8]


def _new_appeal_id():
    """Generate a short, unique appeal id (planning.md uses the `a_` prefix)."""
    return "a_" + uuid.uuid4().hex[:8]


@app.post("/submit")
@limiter.limit("10 per minute;100 per day")
def submit():
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or "text" not in data:
        return jsonify({"error": "Request body must be JSON with a 'text' field."}), 400

    text = data.get("text")
    if not isinstance(text, str) or not text.strip():
        return jsonify({"error": "'text' must be a non-empty string."}), 400

    creator_id = data.get("creator_id")
    word_count = len(text.split())

    # --- Two independent detection signals (planning.md §3) ---
    signal1 = llm_score(text)          # Signal 1: holistic semantic read (Groq LLM)
    signal2 = stylometry_score(text)   # Signal 2: structural statistics (pure Python)
    p_llm = round(signal1["p_llm"], 3)
    p_stylo = round(signal2["p_stylo"], 3)
    rationale = signal1["rationale"]
    features = signal2["features"]

    # --- Confidence scoring: combine the two signals (planning.md §4) ---
    score = combine_signals(p_llm, p_stylo, word_count)
    ai_probability = round(score["ai_probability"], 3)
    confidence = round(score["confidence"], 2)
    disagreement = round(score["disagreement"], 3)
    confidence_band = score["confidence_band"]
    label_variant = score["label_variant"]
    # Full plain-language transparency label (band + percentage + appeal path),
    # rendered from the confidence score so it varies per submission (§5).
    label_text = score["label_text"]

    content_id = _new_content_id()
    timestamp = _now_iso()

    # Both individual signal scores plus the combined breakdown — logged in full
    # so the audit trail (and a future reviewer) can see how the decision was made.
    signals = {
        "p_llm": p_llm,
        "p_stylo": p_stylo,
        "disagreement": disagreement,
        "features": features,
        "llm_rationale": rationale,
    }

    db.insert_content(
        {
            "content_id": content_id,
            "created_at": timestamp,
            "text": text,
            "word_count": word_count,
            "ai_probability": ai_probability,
            "confidence": confidence,
            "label_variant": label_variant,
            "label_text": label_text,
            "p_llm": p_llm,
            "p_stylo": p_stylo,
            "status": "labeled",
        }
    )

    db.insert_audit_log(
        {
            "timestamp": timestamp,
            "event_type": "submission",
            "content_id": content_id,
            "appeal_id": None,
            "ai_probability": ai_probability,
            "confidence": confidence,
            "label_variant": label_variant,
            "p_llm": p_llm,
            "p_stylo": p_stylo,
            "signals_json": json.dumps(signals),
            "status": "labeled",
            "appeal_reasoning": None,
            "detail": (
                f"creator_id={creator_id}; word_count={word_count}; "
                f"confidence_band={confidence_band}"
            ),
        }
    )

    return jsonify(
        {
            "content_id": content_id,
            "creator_id": creator_id,
            "status": "labeled",
            "ai_probability": ai_probability,
            "confidence": confidence,
            "confidence_band": confidence_band,
            "label_variant": label_variant,
            "label_text": label_text,
            "signals": signals,
        }
    )


@app.post("/appeal")
@limiter.limit("5 per minute;20 per day")
def appeal():
    """Receive a creator's appeal of a classification (planning.md §6).

    Sequence: validate the ``content_id`` (404 if unknown) -> snapshot the
    original decision -> write an appeal record -> flip the content status from
    ``labeled`` to ``under_review`` -> write an ``appeal_received`` audit entry
    that carries the reasoning and the original per-signal scores. No automated
    re-classification happens; the item now waits in the ``GET /review`` queue.
    """
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Request body must be JSON."}), 400

    content_id = data.get("content_id")
    if not isinstance(content_id, str) or not content_id.strip():
        return jsonify({"error": "'content_id' is required."}), 400

    # The milestone's field is `creator_reasoning`; accept `reason` as an alias
    # so either name works, matching planning.md §6.
    reasoning = data.get("creator_reasoning", data.get("reason"))
    if not isinstance(reasoning, str) or not reasoning.strip():
        return jsonify({"error": "'creator_reasoning' is required."}), 400

    content = db.get_content(content_id)
    if content is None:
        return jsonify({"error": f"Unknown content_id: {content_id}"}), 404

    creator_id = data.get("creator_id")
    appeal_id = _new_appeal_id()
    timestamp = _now_iso()

    # Snapshot of the original decision, preserved so the appeal records exactly
    # what was contested even if the content record is later changed.
    original = {
        "label_variant": content.get("label_variant"),
        "ai_probability": content.get("ai_probability"),
        "confidence": content.get("confidence"),
        "p_llm": content.get("p_llm"),
        "p_stylo": content.get("p_stylo"),
    }

    db.insert_appeal(
        {
            "appeal_id": appeal_id,
            "content_id": content_id,
            "created_at": timestamp,
            "reason": reasoning,
            "creator_id": creator_id,
            "original_label_variant": original["label_variant"],
            "original_confidence": original["confidence"],
            "status": "open",
        }
    )

    db.update_content_status(content_id, "under_review")

    db.insert_audit_log(
        {
            "timestamp": timestamp,
            "event_type": "appeal_received",
            "content_id": content_id,
            "appeal_id": appeal_id,
            "ai_probability": original["ai_probability"],
            "confidence": original["confidence"],
            "label_variant": original["label_variant"],
            "p_llm": original["p_llm"],
            "p_stylo": original["p_stylo"],
            "signals_json": json.dumps({"original_decision": original}),
            "status": "under_review",
            "appeal_reasoning": reasoning,
            "detail": f"appeal_id={appeal_id}; creator_id={creator_id}",
        }
    )

    return jsonify(
        {
            "appeal_id": appeal_id,
            "content_id": content_id,
            "status": "under_review",
            "message": (
                "Your appeal has been received. This content is now under review; "
                "a human reviewer will re-examine the original classification."
            ),
            "original_decision": original,
        }
    )


@app.get("/review")
@limiter.limit("30 per minute")
def review():
    """Return the open-appeal review queue (planning.md §6)."""
    return jsonify({"queue": db.get_review()})


@app.get("/content/<content_id>")
@limiter.limit("30 per minute")
def get_content(content_id):
    """Return the full stored decision record for one submission."""
    content = db.get_content(content_id)
    if content is None:
        return jsonify({"error": f"Unknown content_id: {content_id}"}), 404
    return jsonify(content)


@app.get("/log")
@limiter.limit("30 per minute")
def get_log():
    limit = request.args.get("limit", default=50, type=int)
    return jsonify({"entries": db.get_log(limit)})


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
