"""Detection signals and confidence scoring for Provenance Guard.

Two independent detectors plus the scoring stage that combines them, per
planning.md §3–§4:

* **Signal 1** — LLM classifier (Groq ``llama-3.3-70b-versatile``): a holistic,
  semantic read returning ``p_llm`` plus a one-sentence rationale.
* **Signal 2** — pure-Python **stylometry**: structural statistics (burstiness,
  type-token ratio, punctuation diversity, average sentence length) combined
  into ``p_stylo``.
* **Confidence scoring** — combines the two probabilities into
  ``ai_probability``, measures signal ``disagreement``, and derives a
  disagreement-penalised ``confidence`` plus the three-zone ``label_variant``.

Run this file directly to sanity-check the signals and the scoring math in
isolation before they are wired into the endpoint (planning.md M3/M4 "Verify"):

    python signals.py
"""
import json
import math
import re

from groq import Groq

MODEL = "llama-3.3-70b-versatile"

SYSTEM_PROMPT = (
    "You are a forensic text-attribution classifier. Your job is to estimate the "
    "probability that a passage of text was written by an AI language model rather "
    "than by a human. Weigh signals such as generic diction, over-smooth "
    "transitions, hedging phrases, uniform sentence rhythm, and an absence of "
    "specific lived detail. You are producing a probabilistic estimate, not a "
    "definitive verdict. Respond with strict JSON only."
)

USER_PROMPT_TEMPLATE = (
    "Analyze the text between the triple quotes and respond with a JSON object "
    "containing exactly two keys:\n"
    '  "p_ai": a number from 0.0 to 1.0 — the probability the text is AI-generated\n'
    '  "rationale": one concise sentence explaining the estimate\n\n'
    'Text:\n"""{text}"""'
)

_client = None


def _get_client():
    """Lazily construct the Groq client (reads GROQ_API_KEY from the env)."""
    global _client
    if _client is None:
        _client = Groq()
    return _client


def _clamp01(value):
    """Coerce a model-provided value into a probability in [0.0, 1.0]."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return 0.5
    return max(0.0, min(1.0, value))


def llm_score(text):
    """Signal 1 assessment.

    Returns ``{"p_llm": float, "rationale": str}``. Calls Groq at temperature 0
    with forced strict-JSON output. On any API or parsing failure it degrades
    gracefully to a neutral 0.5 with an explanatory note (and an ``error`` key)
    so a single bad call never crashes the submission endpoint.
    """
    try:
        client = _get_client()
        response = client.chat.completions.create(
            model=MODEL,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": USER_PROMPT_TEMPLATE.format(text=text)},
            ],
        )
        data = json.loads(response.choices[0].message.content)
        rationale = str(data.get("rationale", "")).strip() or "No rationale provided."
        return {"p_llm": _clamp01(data.get("p_ai")), "rationale": rationale}
    except Exception as exc:  # external boundary: degrade gracefully, never crash
        return {
            "p_llm": 0.5,
            "rationale": f"Signal 1 unavailable ({type(exc).__name__}); defaulted to 0.5.",
            "error": str(exc),
        }


# ---------------------------------------------------------------------------
# Signal 2 — Stylometric heuristics (pure Python), per planning.md §3.
# ---------------------------------------------------------------------------
# Four measurable writing statistics, each normalised to an "AI-likeness"
# contribution in [0, 1], then combined with a weighted mean into ``p_stylo``.
# The raw feature values are returned for transparency and logged in the audit
# trail. Reliable features (burstiness, sentence length) carry more weight than
# the noisier ones (punctuation diversity, type-token ratio).

_SENTENCE_SPLIT_RE = re.compile(r"[.!?]+|\n+")
_WORD_RE = re.compile(r"[A-Za-z0-9']+")

# "Expressive" punctuation whose *variety* tends to mark human writing; AI text
# leans on a small, comma-and-period-heavy repertoire (planning.md §3).
_EXPRESSIVE_PUNCT = set(";:—–-()[]{}\"'!?…,")

# Per-feature weights (sum to 1.0). Burstiness is by far the most robust
# discriminator on real inputs, so it dominates. TTR is the weakest: on short
# texts it saturates near 0.85–0.90 regardless of authorship, so it is kept for
# transparency but given the smallest weight.
_STYLO_WEIGHTS = {
    "burstiness": 0.50,
    "sentence_length": 0.20,
    "punctuation": 0.25,
    "ttr": 0.05,
}


def _split_sentences(text):
    """Split into non-empty sentences on terminal punctuation or newlines."""
    return [s.strip() for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]


def _tokenize(text):
    """Lower-cased word tokens (letters/digits/apostrophes), punctuation stripped."""
    return _WORD_RE.findall(text.lower())


def _burstiness_cv(sentence_lengths):
    """Coefficient of variation (std / mean) of sentence lengths, or ``None``.

    Length-normalised burstiness: humans mix very short and very long sentences
    (high CV); AI decoding flattens rhythm toward a mean (low CV). Undefined
    with fewer than two sentences.
    """
    if len(sentence_lengths) < 2:
        return None
    mean_len = sum(sentence_lengths) / len(sentence_lengths)
    if mean_len == 0:
        return None
    variance = sum((n - mean_len) ** 2 for n in sentence_lengths) / len(sentence_lengths)
    return math.sqrt(variance) / mean_len


def stylometry_score(text):
    """Signal 2 assessment — pure-Python stylometry.

    Returns ``{"p_stylo": float, "features": {...}}`` where ``features`` holds
    the raw, human-readable measurements (returned and logged per planning.md
    §3). ``p_stylo`` is the weighted mean of four per-feature AI-likeness
    contributions in [0, 1]. Never raises: degenerate input yields a neutral
    0.5 rather than an exception.
    """
    words = _tokenize(text)
    total_words = len(words)
    sentences = _split_sentences(text)
    sentence_lengths = [len(_tokenize(s)) for s in sentences]
    sentence_lengths = [n for n in sentence_lengths if n > 0]

    if total_words == 0:
        return {
            "p_stylo": 0.5,
            "features": {
                "sentence_length_variance": None,
                "type_token_ratio": 0.0,
                "punctuation_diversity": 0,
                "avg_sentence_length": 0.0,
            },
        }

    # --- Raw measurements ---
    cv = _burstiness_cv(sentence_lengths)
    avg_sentence_length = (
        sum(sentence_lengths) / len(sentence_lengths) if sentence_lengths else total_words
    )
    ttr = len(set(words)) / total_words
    punctuation_diversity = len({ch for ch in text if ch in _EXPRESSIVE_PUNCT})

    # --- Normalise each measurement to an AI-likeness contribution in [0, 1] ---
    # Burstiness: low CV -> AI-like. Casual human writing bursts (CV ≳ 0.6);
    # AI and formal prose flatten toward a mean (CV ≲ 0.4). Neutral 0.5 when
    # undefined (single sentence).
    burst_ai = 0.5 if cv is None else _clamp01((0.66 - cv) / 0.42)

    # Average sentence length: AI clusters near a comfortable mean; very short,
    # choppy sentences read human. Ramps from ~8 words (human) to ~22 (AI-like).
    length_ai = _clamp01((avg_sentence_length - 8.0) / 14.0)

    # Punctuation: few distinct expressive marks -> AI-like (comma/period heavy);
    # a varied repertoire (dashes, semicolons, parens, ellipses) reads human.
    punct_ai = _clamp01((3 - punctuation_diversity) / 3.0)

    # Type-token ratio: humans repeat unevenly (lower TTR in casual prose); AI
    # holds a steady, moderate-to-high diversity. Weakest, length-sensitive
    # feature (saturates on short inputs), so it is centred near neutral and
    # carries the smallest weight.
    ttr_ai = _clamp01(0.5 + (ttr - 0.80) * 2.0)

    p_stylo = (
        _STYLO_WEIGHTS["burstiness"] * burst_ai
        + _STYLO_WEIGHTS["sentence_length"] * length_ai
        + _STYLO_WEIGHTS["punctuation"] * punct_ai
        + _STYLO_WEIGHTS["ttr"] * ttr_ai
    )

    return {
        "p_stylo": _clamp01(p_stylo),
        "features": {
            "sentence_length_variance": round(cv, 3) if cv is not None else None,
            "type_token_ratio": round(ttr, 3),
            "punctuation_diversity": punctuation_diversity,
            "avg_sentence_length": round(avg_sentence_length, 1),
        },
    }


# ---------------------------------------------------------------------------
# Confidence scoring — combine the two signals, per planning.md §4.
# ---------------------------------------------------------------------------
# The LLM sees semantics the statistics cannot, so it carries the larger weight;
# stylometry is the cheaper, steadier corroborator. Signal *disagreement* is
# treated as genuine uncertainty and penalises confidence rather than being
# averaged away.

MIN_WORDS = 40          # planning.md §4 length gate: shorter texts -> forced uncertain
WEIGHT_LLM = 0.60
WEIGHT_STYLO = 0.40

LABEL_HEADLINES = {
    "high_confidence_ai": "Likely AI-generated",
    "high_confidence_human": "Likely human-written",
    "uncertain": "Attribution uncertain",
}

# Full plain-language transparency-label text, one template per variant, taken
# verbatim from planning.md §5. Each carries two placeholders that are filled at
# render time so the label always states the confidence honestly:
#   {band}       -> the plain confidence word from confidence_band() (§4 table)
#   {confidence} -> round(confidence * 100), the confidence as a percentage
# Because both placeholders derive from the confidence score, the rendered label
# text changes with the score rather than being fixed boilerplate (M5 goal).
LABEL_TEMPLATES = {
    "high_confidence_ai": (
        "Likely AI-generated. Our system estimates this text was mostly produced "
        "by an AI writing tool ({band} — {confidence}% confidence). This estimate "
        "combines an AI-language analysis with statistical writing-pattern checks. "
        "It is an automated estimate, not proof, and it can be wrong. If you wrote "
        "this yourself, you can appeal this label."
    ),
    "high_confidence_human": (
        "Likely human-written. Our system found no strong signs of AI generation "
        "in this text ({band} — {confidence}% confidence). This estimate combines "
        "an AI-language analysis with statistical writing-pattern checks. It is an "
        "automated estimate, not a guarantee. If you disagree with this label, you "
        "can appeal it."
    ),
    "uncertain": (
        "Attribution uncertain. Our system could not reliably determine whether "
        "this text was written by a human or an AI ({band} — {confidence}% "
        "confidence). The signals were weak, in disagreement, or the text was too "
        "short to judge. We are not labeling this content as either. You may add a "
        "voluntary disclosure, or request a human review."
    ),
}


def confidence_band(confidence):
    """Map a confidence value to the plain word shown to readers (planning.md §4)."""
    if confidence >= 0.85:
        return "high confidence"
    if confidence >= 0.75:
        return "good confidence"
    if confidence >= 0.60:
        return "low confidence"
    return "very low confidence"


def render_label_text(variant, confidence):
    """Render a variant's full transparency-label text (planning.md §5).

    Substitutes the plain confidence ``{band}`` word and the ``{confidence}``
    percentage (``round(C * 100)``) into the variant template, so the label
    text reflects the actual confidence score instead of being fixed text.
    """
    return LABEL_TEMPLATES[variant].format(
        band=confidence_band(confidence),
        confidence=round(confidence * 100),
    )


def select_variant(ai_probability, confidence, word_count):
    """Three-zone label selection (planning.md §4 thresholds).

    A text inside the AI probability band still lands in ``uncertain`` if the
    two signals conflict enough to pull confidence below 0.75 — that is how
    genuine uncertainty is expressed instead of a hard flip at 0.5.
    """
    if word_count < MIN_WORDS:
        return "uncertain"
    if ai_probability >= 0.65 and confidence >= 0.75:
        return "high_confidence_ai"
    if ai_probability <= 0.35 and confidence >= 0.75:
        return "high_confidence_human"
    return "uncertain"


def combine_signals(p_llm, p_stylo, word_count):
    """Combine both signals into the calibrated decision (planning.md §4).

    Returns ``ai_probability`` (weighted mean), ``disagreement`` (absolute gap
    between the signals), ``confidence`` (directional strength discounted by
    disagreement), the plain-language ``confidence_band``, the three-zone
    ``label_variant``, its short ``label_headline``, and the fully rendered
    ``label_text`` (planning.md §5). Reproduces the §4 worked-examples table
    exactly.
    """
    ai_probability = WEIGHT_LLM * p_llm + WEIGHT_STYLO * p_stylo
    disagreement = abs(p_llm - p_stylo)
    directional = max(ai_probability, 1.0 - ai_probability)
    confidence = directional * (1.0 - 0.5 * disagreement)
    variant = select_variant(ai_probability, confidence, word_count)
    return {
        "ai_probability": ai_probability,
        "disagreement": disagreement,
        "confidence": confidence,
        "confidence_band": confidence_band(confidence),
        "label_variant": variant,
        "label_headline": LABEL_HEADLINES[variant],
        "label_text": render_label_text(variant, confidence),
    }


if __name__ == "__main__":
    import os

    from dotenv import load_dotenv

    load_dotenv()

    # --- 1. Verify the scoring math reproduces the planning.md §4 worked examples ---
    print("=== Confidence scoring vs planning.md §4 worked-examples table ===")
    worked_examples = [
        # name, p_llm, p_stylo, expected ai_prob, expected confidence, expected variant
        ("Clear AI", 0.92, 0.80, 0.872, 0.82, "high_confidence_ai"),
        ("Clear human", 0.10, 0.20, 0.140, 0.82, "high_confidence_human"),
        ("Signals conflict", 0.85, 0.20, 0.590, 0.40, "uncertain"),
        ("Genuine middle", 0.55, 0.48, 0.522, 0.50, "uncertain"),
    ]
    all_ok = True
    for name, p_llm, p_stylo, exp_p, exp_c, exp_var in worked_examples:
        r = combine_signals(p_llm, p_stylo, word_count=100)
        ok = (
            round(r["ai_probability"], 3) == exp_p
            and round(r["confidence"], 2) == exp_c
            and r["label_variant"] == exp_var
        )
        all_ok = all_ok and ok
        print(
            f"[{'OK ' if ok else 'BAD'}] {name:<17} "
            f"ai_prob={r['ai_probability']:.3f} (exp {exp_p})  "
            f"conf={r['confidence']:.2f} (exp {exp_c})  "
            f"disagree={r['disagreement']:.2f}  "
            f"variant={r['label_variant']} (exp {exp_var})"
        )
    print(f"\nWorked-examples table reproduced exactly: {all_ok}")

    # --- 1b. Verify all three transparency-label variants render (planning.md §5) ---
    # The label text must vary with the confidence score, so we render each
    # variant at a representative confidence and print the full text to confirm
    # it matches the spec wording and that {band}/{confidence}% are substituted.
    print("\n=== Transparency-label variants (planning.md §5) ===")
    label_checks = [
        ("high_confidence_ai", 0.82),
        ("high_confidence_human", 0.82),
        ("uncertain", 0.50),
    ]
    for variant, conf in label_checks:
        text = render_label_text(variant, conf)
        print(f"\n--- {variant} (C={conf:.2f}) ---")
        print(f"  headline : {LABEL_HEADLINES[variant]}")
        print(f"  band     : {confidence_band(conf)} — {round(conf * 100)}%")
        print(f"  text     : {text}")
    distinct = len({render_label_text(v, c) for v, c in label_checks})
    print(f"\nAll three variants render distinct text: {distinct == 3}")

    # --- 2. Milestone-4 test inputs (deliberately chosen across the range) ---
    milestone_samples = {
        "clear_ai": (
            "Artificial intelligence represents a transformative paradigm shift in "
            "modern society. It is important to note that while the benefits of AI "
            "are numerous, it is equally essential to consider the ethical "
            "implications. Furthermore, stakeholders across various sectors must "
            "collaborate to ensure responsible deployment."
        ),
        "clear_human": (
            "ok so i finally tried that new ramen place downtown and honestly? "
            "underwhelming. the broth was fine but they put WAY too much sodium in "
            "it and i was thirsty for like three hours after. my friend got the "
            "spicy version and said it was better. probably won't go back unless "
            "someone drags me there"
        ),
        "borderline_formal_human": (
            "The relationship between monetary policy and asset price inflation has "
            "been extensively studied in the literature. Central banks face a "
            "fundamental tension between their mandate for price stability and the "
            "unintended consequences of prolonged low interest rates on equity and "
            "real estate valuations."
        ),
        "borderline_edited_ai": (
            "I've been thinking a lot about remote work lately. There are genuine "
            "tradeoffs — flexibility and no commute on one side, isolation and "
            "blurred work-life boundaries on the other. Studies show productivity "
            "varies widely by individual and role type."
        ),
    }

    print("\n=== Signal 2 (stylometry) standalone ===")
    for name, sample in milestone_samples.items():
        s2 = stylometry_score(sample)
        print(f"\n--- {name} (words={len(sample.split())}) ---")
        print(f"  p_stylo  = {s2['p_stylo']:.3f}")
        print(f"  features = {json.dumps(s2['features'])}")

    # --- 3. Full pipeline (both signals + scoring) when a live key is available ---
    if os.environ.get("GROQ_API_KEY"):
        print("\n=== Full pipeline (both signals + confidence scoring) ===")
        for name, sample in milestone_samples.items():
            wc = len(sample.split())
            s1 = llm_score(sample)
            s2 = stylometry_score(sample)
            r = combine_signals(s1["p_llm"], s2["p_stylo"], wc)
            print(f"\n--- {name} (words={wc}) ---")
            print(f"  p_llm   = {s1['p_llm']:.3f}  ({s1['rationale']})")
            print(f"  p_stylo = {s2['p_stylo']:.3f}  {json.dumps(s2['features'])}")
            print(
                f"  => ai_probability={r['ai_probability']:.3f}  "
                f"confidence={r['confidence']:.2f} ({r['confidence_band']})  "
                f"disagreement={r['disagreement']:.2f}"
            )
            print(f"  => label: {r['label_variant']} — {r['label_headline']}")
    else:
        print(
            "\n[note] GROQ_API_KEY not set — skipped the live Signal 1 / full-pipeline "
            "run. Stylometry and scoring math above are API-free."
        )
