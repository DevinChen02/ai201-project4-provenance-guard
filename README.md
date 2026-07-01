# Provenance Guard

A content-attribution service: a creator submits text, and the system estimates
whether it was **AI-generated** or **human-written**, returns a **calibrated
confidence score** and a plain-language transparency label, records every
decision in an **audit log**, and lets creators **appeal** a classification.

> [`planning.md`](planning.md) is the authoritative specification. This README
> tracks implementation status and validation results.

## Demo video

A short end-to-end walkthrough: three-zone
classification, one correct call, one root-caused false positive, and the
appeal + audit-log recourse. 

<div>
    <a href="https://www.loom.com/share/8aef3b485d3b4324928ca33a252d14c8">
      <p>ai201-project4-provenance-guard demo video - Watch Video</p>
    </a>
    <a href="https://www.loom.com/share/8aef3b485d3b4324928ca33a252d14c8">
      <img style="max-width:300px;" src="https://cdn.loom.com/sessions/thumbnails/8aef3b485d3b4324928ca33a252d14c8-f692c83bd56bf482-full-play.gif#t=0.1">
    </a>
  </div>

## Status

| Milestone | Scope | State |
|---|---|---|
| M3 | `POST /submit`, Signal 1 (LLM), SQLite persistence, audit log, `GET /log` | ✅ done |
| M4 | Signal 2 (stylometry) + confidence scoring combining both signals | ✅ done |
| **M5** | **Transparency-label rendering, appeals (`POST /appeal`, `GET /review`), rate limiting** | ✅ done |

## Submission checklist → where each piece lives

| Required section | Where in this README |
|---|---|
| Setup & run | **Setup & run** |
| Architecture & API contract | **API** (+ `planning.md` §Architecture) |
| Detection signals **+ reasoning** | **Detection signals** → *Why these two signals* |
| Confidence scoring **+ two contrasting examples** | **Confidence scoring** → *Two submissions, very different confidence scores* |
| Evaluation report summary (key metrics) | **Evaluation report summary** |
| Three transparency-label variants (exact text) | **Milestone 5 · §1** → *Typed description* |
| Appeals workflow | **Milestone 5 · §2** |
| Rate limiting | **Milestone 5 · §3** |
| Audit log (≥ 3 entries) | **Milestone 5 · §4** |
| Known limitations | **Known limitations** |
| Spec reflection | **Spec reflection** |
| AI usage | **AI usage** |
| Demo video (+ script) | **Demo video** · [`demo-script.md`](demo-script.md) |

## Setup & run

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
echo "GROQ_API_KEY=sk-..." > .env        # your Groq key (git-ignored)

python app.py                            # http://127.0.0.1:5000
python signals.py                        # test signals + scoring in isolation
```

> **macOS port note:** if `python app.py` reports port 5000 in use, it is almost
> certainly the **AirPlay Receiver** (Control Center), which binds `*:5000` by
> default. Disable *AirPlay Receiver* in System Settings, or run on another port.
> The M5 evidence below was captured on port 5001; the status codes and JSON
> bodies are identical on any port.

## API

| Method & path | Body / params | Returns | Rate limit |
|---|---|---|---|
| `POST /submit` | `{text, creator_id?}` | `content_id, ai_probability, confidence, confidence_band, label_variant, label_text, signals, status` | 10/min, 100/day |
| `POST /appeal` | `{content_id, creator_reasoning}` | `appeal_id, content_id, status:"under_review", original_decision` | 5/min, 20/day |
| `GET /review` | — | open-appeal reviewer queue | 30/min |
| `GET /content/<id>` | path id | full stored decision record | 30/min |
| `GET /log` | `?limit=` | recent audit-log entries (newest first) | 30/min |
| `GET /health` | — | `{status:"ok"}` | default |

## Detection signals (two independent detectors, planning.md §3)

| | Signal 1 — LLM classifier | Signal 2 — Stylometry |
|---|---|---|
| Kind | Semantic / holistic | Structural / statistical |
| How | Groq `llama-3.3-70b-versatile`, temp 0, strict JSON | Pure Python, no libraries |
| Output | `p_llm ∈ [0,1]` + rationale | `p_stylo ∈ [0,1]` + raw features |
| Blind spot | Unstable, overconfident, flags polished human prose | Genre-blind, length-sensitive |

Because their errors are **uncorrelated**, agreement raises confidence and
disagreement lowers it.

**Signal 2 features** (each normalised to an AI-likeness contribution, then a
weighted mean → `p_stylo`):

| Feature | Weight | AI tendency |
|---|---|---|
| Sentence-length burstiness (coeff. of variation) | 0.50 | low variance ⇒ AI |
| Punctuation diversity (distinct expressive marks) | 0.25 | few marks ⇒ AI |
| Average sentence length | 0.20 | clusters near a comfortable mean ⇒ AI |
| Type-token ratio | 0.05 | weak/length-sensitive (see findings) |

### Why these two signals, and why this scoring

**Why one semantic and one structural signal (not two of a kind).** The design
bet is *independence*. Signal 1 reads **meaning** — "does this *sound* machine-
written?" — and catches generic diction, over-smooth transitions, hedging ("it's
important to note"), and missing lived detail. Signal 2 never reads meaning at
all; it only measures **shape** — how much sentence length varies, how varied the
punctuation is, how diverse the vocabulary is. Because one is semantic and the
other purely structural, they fail on *different* inputs: a repetitive poem fools
the structural signal (looks uniform ⇒ AI-like) but not the semantic one (reads
human); a dry technical paragraph can lean AI structurally while the LLM still
recognises domain specificity. Two signals of the *same* kind (say two LLM
prompts) would share the same blind spots, so their agreement would tell you
nothing. Uncorrelated errors are the whole point.

**Why the LLM is weighted 0.60 and stylometry 0.40.** The LLM sees things
statistics can't (semantics, cliché density, factual specificity), so it earns
the larger share — but it is also the *less stable* signal (at temperature 0 the
same casual review still swung `p_llm` between 0.20 and 0.50 across runs).
Stylometry is dumber but deterministic: the same text always returns the same
number. So stylometry is a cheap, steady corroborator that *anchors* the LLM's
noisier read rather than driving the call. 60/40 = "trust the smarter signal
more, keep a stable second opinion."

**Why disagreement lowers confidence instead of being averaged away.** This is
the key scoring choice. If the signals split hard — `p_llm=0.85`, `p_stylo=0.20`
— a plain average reads 0.59 and would *confidently* mislabel the text "leaning
AI." Instead `confidence = directional · (1 − 0.5·disagreement)` collapses that
to ≈0.40 and the text lands **Uncertain**. Averaging hides conflict; penalising
it surfaces conflict as honesty, so a borderline human writer is never publicly
branded "AI" because one noisy signal fired.

**What I'd change before deploying this for real.**
- **Replace LLM self-assessment with a purpose-built detector.** Asking a
  generative model to grade text is weak ground truth — it guesses, and it is
  overconfident. In production I'd swap Signal 1 for a classifier fine-tuned on
  labelled human/AI pairs with *calibrated* probabilities, and keep the LLM only
  for the human-readable rationale.
- **Calibrate on a real labelled corpus.** The thresholds (0.65 / 0.35, C ≥ 0.75)
  and weights (0.60/0.40 and the stylometry sub-weights) are reasoned but
  hand-tuned on ~a dozen samples. I'd fit them on a held-out set and report
  precision/recall *per genre* instead of trusting round numbers.
- **Make stylometry genre-aware.** One burstiness threshold treats a poem, a
  legal brief, and a text message alike. I'd condition the normalisation on
  detected genre/length so formal-but-human prose isn't structurally punished
  (see *Known limitations*).
- **Close the appeal loop.** Appeals currently stop at `under_review`. A
  reviewer's uphold/overturn decision is exactly the labelled data needed to
  recalibrate — feeding it back turns the audit log into a training set.
- **Move storage off in-memory/SQLite** to Redis (rate limits) and a real DB
  (audit log) so limits and history survive restarts and scale across processes.

## Confidence scoring (planning.md §4)

```
ai_probability = 0.60·p_llm + 0.40·p_stylo
disagreement   = |p_llm − p_stylo|
directional    = max(ai_probability, 1 − ai_probability)
confidence     = directional · (1 − 0.5·disagreement)   # conflict drags confidence down
```

Confidence bands: `≥0.85` high · `0.75–0.85` good · `0.60–0.75` low · `<0.60` very low.

Three zones (not a binary flip at 0.5):

```
word_count < 40                          → Uncertain (too little signal)
ai_probability ≥ 0.65 and confidence ≥ 0.75 → Likely AI-generated
ai_probability ≤ 0.35 and confidence ≥ 0.75 → Likely human-written
otherwise                                → Uncertain
```

### Two submissions, very different confidence scores

To show the confidence number actually **moves** with the input (it is not a
decorative constant), here are two real submissions from the Milestone-4 live run
that landed **0.30 apart** in confidence.

**High confidence — C = 0.79 → Likely human-written.**

> Input: *"ok so i finally tried that new ramen place downtown and honestly?
> underwhelming. the broth was fine but they put WAY too much sodium in it and i
> was thirsty for like three hours after. my friend got the spicy version…"*

| p_llm | p_stylo | disagreement | ai_probability | confidence |
|---|---|---|---|---|
| 0.20 | 0.217 | **0.02** | 0.207 | **0.79 (good)** |

Both signals independently read "human" *and* they agree (Δ = 0.02), so nothing
drags confidence down: `directional = max(0.207, 0.793) = 0.793`, penalty
`(1 − 0.5·0.02) ≈ 0.99`, `C ≈ 0.79`. With `ai_probability ≤ 0.35` and `C ≥ 0.75`
→ **Likely human-written**.

**Lower confidence — C = 0.49 → Uncertain.**

> Input: *"I've been thinking a lot about remote work lately. There are genuine
> tradeoffs — flexibility and no commute on one side, isolation and blurred
> work-life boundaries on the other. Studies show productivity varies widely…"*
> (a human-edited AI paragraph)

| p_llm | p_stylo | disagreement | ai_probability | confidence |
|---|---|---|---|---|
| 0.60 | 0.437 | **0.16** | 0.535 | **0.49 (very low)** |

Here both signals sit near the middle *and* pull apart (Δ = 0.16): the combined
probability (0.535) is barely off a coin-flip, and the disagreement penalty drags
`C` below 0.5 — `0.535 · (1 − 0.5·0.16) ≈ 0.49`. Neither directional zone is met
→ **Uncertain**.

**The point:** the *same* scoring code produced **0.79** for one submission and
**0.49** for another — a 30-point spread driven by (a) how far the combined
probability sits from 0.5 and (b) how much the two signals disagree. A constant
would have printed the same number twice; this doesn't.

### Scoring verified against the §4 worked-examples table

`python signals.py` reproduces every row of the planning §4 table **exactly**
(`ai_probability`, `confidence`, and zone), confirming the implementation did not
silently diverge from the spec:

| Case | p_llm | p_stylo | ai_prob | confidence | Label |
|---|---|---|---|---|---|
| Clear AI | 0.92 | 0.80 | 0.872 | 0.82 | Likely AI-generated |
| Clear human | 0.10 | 0.20 | 0.140 | 0.82 | Likely human-written |
| Signals conflict | 0.85 | 0.20 | 0.590 | 0.40 | Uncertain |
| Genuine middle | 0.55 | 0.48 | 0.522 | 0.50 | Uncertain |

## Evaluation report summary

The system was evaluated on two small, deliberately curated sets — a **4-row
scoring oracle** (the planning §4 worked examples, deterministic) and a **4-input
live-pipeline set** spanning genres (casual human, corporate AI, formal human
abstract, human-edited AI). These are hand-picked diagnostic cases, **not** a
large labelled benchmark; the thresholds and weights are reasoned and hand-tuned
on ~a dozen samples, so treat the numbers below as evidence the pipeline behaves
as designed, not as a population accuracy claim. Detail for every row is in the
two sections that follow.

| Metric | Result |
|---|---|
| **Scoring correctness** (vs planning §4 oracle) | **4/4 rows reproduced exactly** — `ai_probability`, `confidence`, and zone all match; verified every run by `python signals.py` |
| **AI/human separation** | `ai_probability` spans **0.207 → 0.862**; casual-human input (0.207) and AI/formal inputs (0.730–0.862) cluster cleanly apart |
| **Confidence is non-constant** | `confidence` ranges **0.49 → 0.80** across the four inputs (a ~31-point spread) — the disagreement + directional penalties measurably move the number |
| **Label-variant coverage** | **3/3** transparency variants exercised (likely-human, likely-AI, uncertain) |
| **Correct call (narrated in demo)** | Casual ramen review → **Likely human-written**, C = 0.79; both signals independently agree (Δ = 0.02) |
| **Known error rate on the set** | **1 false positive** — the formal human abstract mislabeled **Likely AI-generated** (C = 0.80); **0** false "AI" brands on the casual-human input. Root cause: the burstiness blind spot **both** signals share (see *Known limitations*) |
| **Robustness** | Stylometry + scoring are fully deterministic; the LLM signal is stochastic and **degrades to a neutral 0.5 on API error** (observed live), which the disagreement penalty absorbs rather than crashing `/submit` |

**Headline:** clean separation between clearly-human and clearly-AI inputs, all
three label zones reached, confidence that demonstrably moves with the input, and
**one well-understood, root-caused false positive** on formal human prose — the
exact harm case the "estimate, not proof" wording and the appeal path exist for.

## M4 validation — four deliberately chosen inputs (live pipeline)

Representative run with the two signals in agreement (both scores printed
separately, per the milestone's debugging guidance):

| Input | p_llm | p_stylo | ai_prob | confidence | Δ | Label |
|---|---|---|---|---|---|---|
| Clear AI (corporate buzzwords) | 0.80 | 0.625 | **0.730** | 0.67 (low) | 0.18 | Uncertain — leans AI |
| Clear human (casual ramen review) | 0.20 | 0.217 | **0.207** | 0.79 (good) | 0.02 | **Likely human-written** |
| Formal human (economics abstract) | 0.80 | 0.955 | **0.862** | 0.80 (good) | 0.16 | **Likely AI-generated** ⚠️ |
| Edited AI (remote-work paragraph) | 0.60 | 0.437 | **0.535** | 0.49 (very low) | 0.16 | Uncertain |

Scores span **0.207 → 0.862**, three distinct label categories appear, and the
clearly-human and clearly-AI inputs are well separated.

### Findings

- **Signals diverge on the formal human abstract.** Stylometry rates the polished,
  uniform economics abstract *more* machine-like (`p_stylo=0.955`) than the actual
  AI paragraph (`0.625`, which contains one long sentence that adds burstiness).
  The LLM's semantic read is what tells them apart — exactly the independence the
  two-signal design relies on.
- **Designed false positive (⚠️).** The economics abstract lands *Likely
  AI-generated* because **both** signals agree it is uniform and hedged. This is
  the harm case in planning §7.3; the label is framed as an estimate and offers an
  appeal path (M5). The AI threshold is deliberately 0.65 (not 0.50) to reduce
  such false positives.
- **LLM instability observed directly** (planning §3 blind spot). On repeated live
  submissions the same casual-human review drew `p_llm` swings between **0.20 and
  0.50**. When it reads 0.50 the two signals disagree (Δ≈0.28), the disagreement
  penalty drops confidence to ≈0.53, and the item falls back to **Uncertain**
  instead of a wrong label — demonstrating *why* disagreement lowers confidence
  rather than being averaged away.
- **Misbehaving sub-signal caught and fixed.** Type-token ratio saturated at
  0.86–0.90 for all four short inputs; the first mapping pushed it to 1.0
  everywhere, biasing every text toward AI. It was re-centred and down-weighted to
  0.05, and burstiness (the robust discriminator) now carries 0.50.

## Audit log records both signals

Every `/submit` writes an `audit_log` row capturing **each signal's individual
score** alongside the **combined** result (`GET /log`):

```json
{
  "event_type": "submission",
  "content_id": "c_359c2a60",
  "p_llm": 0.8,
  "p_stylo": 0.955,
  "ai_probability": 0.862,
  "confidence": 0.8,
  "label_variant": "high_confidence_ai",
  "signals": {
    "p_llm": 0.8, "p_stylo": 0.955, "disagreement": 0.155,
    "features": { "sentence_length_variance": 0.256, "type_token_ratio": 0.86,
                  "punctuation_diversity": 0, "avg_sentence_length": 21.5 },
    "llm_rationale": "Formal tone, generic phrasing, lack of specific examples."
  }
}
```

## Milestone 5 — production layer

Four features turn the detection pipeline into a usable service: a transparency
label that **varies with confidence**, an **appeals** workflow, per-IP **rate
limiting**, and a **complete structured audit log**. All four were exercised
end-to-end against the live Groq-backed server (see the macOS port note above —
evidence captured on port 5001; identical on any port).

### 1. Transparency label — three variants (planning.md §5)

`/submit` renders the label text from the confidence score, so both the
**variant** and the substituted **{band} — {confidence}%** change with the input;
it is never fixed boilerplate. All three variants were reached with live
submissions:

| Variant (`label_variant`) | Reached by | ai_prob | confidence |
|---|---|---|---|
| `high_confidence_ai` | uniform, hedged, generic essay | 0.795 | 0.79 (good) |
| `high_confidence_human` | bursty, punctuation-varied personal anecdote | 0.136 | 0.79 (good) |
| `uncertain` | 17-word caption (forced by the <40-word length gate) | 0.204 | 0.79 (good) |

**Typed description — the exact display text of all three variants.** These are
the literal strings `/submit` returns; the *only* part that varies per submission
is the parenthetical `({band} — {confidence}% confidence)` clause (both halves
are derived from the confidence score — shown here at each variant's live
C = 0.79). Everything else is fixed:

> **A — `high_confidence_ai`:** "Likely AI-generated. Our system estimates this
> text was mostly produced by an AI writing tool (**good confidence — 79%
> confidence**). This estimate combines an AI-language analysis with statistical
> writing-pattern checks. It is an automated estimate, not proof, and it can be
> wrong. If you wrote this yourself, you can appeal this label."

> **B — `high_confidence_human`:** "Likely human-written. Our system found no
> strong signs of AI generation in this text (**good confidence — 79%
> confidence**). This estimate combines an AI-language analysis with statistical
> writing-pattern checks. It is an automated estimate, not a guarantee. If you
> disagree with this label, you can appeal it."

> **C — `uncertain`:** "Attribution uncertain. Our system could not reliably
> determine whether this text was written by a human or an AI (**good confidence
> — 79% confidence**). The signals were weak, in disagreement, or the text was
> too short to judge. We are not labeling this content as either. You may add a
> voluntary disclosure, or request a human review."

(The uncertain case above is the length-gate path — the directional confidence
was fine, but a 17-word input is forced to *Uncertain* per §4, and the label's
"too short to judge" clause explains it. The §4 table and M4 validation above
also show uncertain reached via low confidence / signal disagreement.)

### 2. Appeals workflow (planning.md §6)

`POST /appeal` accepts `content_id` + `creator_reasoning`. It validates the id
(`404` if unknown), snapshots the original decision, writes an `appeals` record,
flips content status `labeled → under_review`, and logs an `appeal_received`
event. No automated re-classification — the item waits in `GET /review`.

```bash
curl -s -X POST http://localhost:5000/appeal \
  -H "Content-Type: application/json" \
  -d '{"content_id": "c_87194b6a", "creator_reasoning": "I wrote this myself from personal experience. I am a non-native English speaker and my writing style may appear more formal than typical."}' | python -m json.tool
```

Response:

```json
{
  "appeal_id": "a_a1a3f94e",
  "content_id": "c_87194b6a",
  "status": "under_review",
  "message": "Your appeal has been received. This content is now under review; a human reviewer will re-examine the original classification.",
  "original_decision": {
    "ai_probability": 0.795, "confidence": 0.79,
    "label_variant": "high_confidence_ai", "p_llm": 0.8, "p_stylo": 0.788
  }
}
```

`GET /log` then shows the appeal entry with `"status": "under_review"` and the
populated `appeal_reasoning`:

```json
{
  "id": 15,
  "timestamp": "2026-07-01T04:21:25.559937+00:00",
  "event_type": "appeal_received",
  "content_id": "c_87194b6a",
  "appeal_id": "a_a1a3f94e",
  "ai_probability": 0.795,
  "confidence": 0.79,
  "label_variant": "high_confidence_ai",
  "p_llm": 0.8,
  "p_stylo": 0.788,
  "status": "under_review",
  "appeal_reasoning": "I wrote this myself from personal experience. I am a non-native English speaker and my writing style may appear more formal than typical.",
  "signals": { "original_decision": { "ai_probability": 0.795, "confidence": 0.79,
               "label_variant": "high_confidence_ai", "p_llm": 0.8, "p_stylo": 0.788 } }
}
```

The item also surfaces in the reviewer queue `GET /review`, carrying the
creator's reasoning next to the contested per-signal breakdown:

```json
{
  "appeal_id": "a_a1a3f94e",
  "content_id": "c_87194b6a",
  "creator_reasoning": "I wrote this myself from personal experience. ...",
  "original_label_variant": "high_confidence_ai",
  "original_confidence": 0.79,
  "original_ai_probability": 0.795,
  "original_signals": { "p_llm": 0.8, "p_stylo": 0.788 },
  "current_status": "under_review",
  "text_excerpt": "Artificial intelligence has become an increasingly important topic in contemporary discourse. ..."
}
```

### 3. Rate limiting (Flask-Limiter, planning.md §8)

Per-client-IP limits with an in-memory store for the dev build (production would
point `storage_uri` at Redis). The numbers balance a real writer's own usage
against scripted abuse — they are **not arbitrary**:

| Endpoint | Limit | Reasoning |
|---|---|---|
| `POST /submit` | **10 / min, 100 / day** | Each call makes a Groq LLM request (latency + token cost + free-tier caps). 10/min comfortably covers a person iterating on their own drafts and live demos; 100/day bounds daily token spend and blocks a script flooding the system. |
| `POST /appeal` | **5 / min, 20 / day** | Appeals are rare human actions. A tight cap stops appeal-flooding and review-queue harassment while never constraining a genuine creator. |
| `GET /log`, `/review`, `/content/<id>` | **30 / min** | Read-only and cheap, but still bounded to discourage scraping the audit log. |
| Global default | **200 / day, 50 / hour** | Backstop for any route without an explicit limit. |

Exceeding a limit returns HTTP **429** with a JSON body. Evidence — 12 rapid
`POST /submit` requests against the 10/min limit; the first 10 return `200`, the
rest `429`:

```
request  1 -> 200
request  2 -> 200
request  3 -> 200
request  4 -> 200
request  5 -> 200
request  6 -> 200
request  7 -> 200
request  8 -> 200
request  9 -> 200
request 10 -> 200
request 11 -> 429
request 12 -> 429
```

The 429 response body is JSON, not HTML:

```json
{ "error": "rate limit exceeded", "detail": "10 per 1 minute" }
```

### 4. Complete audit log

`GET /log` returns structured JSON rows (newest first). Every submission **and**
every appeal is captured, and each row now carries everything the milestone
requires:

- `timestamp`, `content_id`
- attribution result (`label_variant`) + combined `ai_probability`
- `confidence`
- **both** individual signal scores (`p_llm`, `p_stylo`) plus the full nested
  `signals` breakdown (features + LLM rationale)
- `status` (`labeled` / `under_review`) — i.e. **whether an appeal has been filed**
- `appeal_reasoning` (populated on appeal events)

A submission row (see also the M4 example above) sets `"status": "labeled"`; the
appeal row shown in §2 sets `"status": "under_review"` with the reasoning. The
live run produced well over three structured entries spanning three submissions
(one per label variant) and one appeal.

> **Storage note:** the SQLite `audit_log` table gained `status` and
> `appeal_reasoning` columns in M5. `init_db()` migrates pre-M5 databases in
> place with `ALTER TABLE` (older rows simply carry `null` for the new fields),
> so no data is lost and a fresh database is not required.

## Known limitations

This is a probabilistic estimator built on two imperfect signals, and it fails in
specific, predictable ways — not just "it needs more data."

**1. Formal, uniform human writing is mislabeled "AI" — the signature false
positive.** This is not hypothetical; the Milestone-4 run reproduced it. A genuine
human economics abstract scored **p_stylo = 0.955** and **p_llm = 0.80**, combined
to **ai_probability = 0.862**, and was labeled **Likely AI-generated** with *good*
confidence (C = 0.80). The cause is a *property of Signal 2*, not a data shortage:
the burstiness feature (weighted 0.50) equates **low sentence-length variance**
with **AI**. But polished formal prose — academic abstracts, legal writing,
technical documentation — is *also* low-variance and low-punctuation-diversity by
convention. Signal 1 doesn't rescue it, because that same text genuinely *is*
hedged and generic-sounding, which is exactly what the LLM is told to read as AI.
When **both** signals share the blind spot, the disagreement penalty (the system's
main safety net) can't fire — the signals agree, confidence stays high, and the
wrong label sticks. **Non-native English writers are the group most exposed here**,
because more formal, templated phrasing is common in second-language academic
writing; the system would disproportionately flag them — a fairness problem, not
just an accuracy one. The "estimate, not proof" wording and the appeal path exist
precisely because this failure is unavoidable with these signals.

**2. Repetitive human art (poetry, song lyrics, aphorisms) reads structurally
AI-like.** Refrains and short even lines produce low burstiness *and* low
type-token ratio — Signal 2's exact "AI" fingerprint. Here the system usually
degrades gracefully (Signal 1 reads it human → signals disagree → Uncertain rather
than a false "AI" brand), but the outcome is still "we couldn't tell" on writing a
human clearly authored.

**3. Human-edited AI ("hybrid") text.** The system estimates a single holistic
origin; it has no notion of partial authorship. Someone who rewrites AI output —
restoring sentence-length variety, softening the tells — moves *both* signals
toward human. The Milestone-4 edited remote-work paragraph shows the boundary: it
landed Uncertain, but a little more editing tips it to "human." The system can
only guess "mostly AI" vs "mostly human," never "AI-assisted."

**4. Short text is unusable.** Under 40 words there aren't enough sentences for
stable variance and the LLM has little to judge, so the length gate forces
Uncertain — correct behavior, but it means tweets, captions, and headlines get no
verdict at all.

## Spec reflection

**Where the spec guided the implementation.** `planning.md` §4 didn't just
describe the scoring in prose — it committed to a **worked-examples table** with
exact expected outputs (e.g. "Signals conflict: p_llm 0.85, p_stylo 0.20 →
ai_probability 0.590, confidence 0.40, Uncertain"). That table became a **test
oracle**: I encoded all four rows into `signals.py`'s `__main__` block, and
`python signals.py` asserts the code reproduces every row *exactly* before it is
wired into the endpoint. It caught a real bug — my first `confidence` formula
produced 0.59 on the conflict row instead of 0.40, and the table flagged it
immediately. Writing the *numeric contract* before the code, and diffing against
it, is the single thing that kept the scoring honest.

**Where the implementation diverged, and why.** The spec (§3) presented the four
stylometric features as a roughly co-equal set and even listed type-token ratio
(TTR) *second*, implying it was a primary discriminator. The implementation
diverged: TTR is now weighted **0.05** and burstiness **0.50**. The reason
surfaced only during Milestone-4 validation — on every short test input TTR
saturated at 0.86–0.90 regardless of authorship, and the first normalization
mapped that saturated band to ≈1.0, biasing *every* submission toward "AI."
Rather than follow the spec's implied weighting, I re-centred TTR around neutral,
demoted it to a near-cosmetic 0.05, and promoted burstiness (the one feature that
actually separated the samples) to 0.50. The spec *anticipated* this — it called
the weights "a tunable constant, revisited after the §4 validation run" — so the
divergence lives inside the spec's own contract, but the specific weights are an
empirical override of its starting point. (A smaller, cosmetic divergence: the
`/appeal` endpoint's primary field is `creator_reasoning`, with the spec's
`reason` kept only as an accepted alias.)

## AI usage

I used GitHub Copilot as an implementation assistant, driving it from
`planning.md` one focused unit at a time and verifying each in isolation before
wiring it in (the workflow in the spec's *AI Tool Plan*). Three concrete
instances:

**1. Signal 1 — Groq classifier with strict JSON.**
*Directed:* "Write `llm_score(text)` calling Groq `llama-3.3-70b-versatile` at
temperature 0, returning `{p_ai, rationale}` as JSON." *Produced:* a function that
called the API and ran `json.loads` on the raw reply — but with no guard against
malformed output and no bound on the probability. *Revised/overrode:* I added
`response_format={"type": "json_object"}` to *force* valid JSON instead of hoping
the model complied; wrapped the call in a try/except that degrades to a neutral
`p_llm = 0.5` (with an `error` key) so one bad API call can't 500 the `/submit`
endpoint; and added `_clamp01()` after the model occasionally returned values like
`1.2`. The graceful degradation and clamping were mine — the AI's version would
have crashed on the first malformed or out-of-range response.

**2. Stylometry normalization — caught the AI's TTR bias.**
*Directed:* "Implement `stylometry_score` with burstiness, TTR, punctuation
diversity, and average sentence length, each normalized to [0,1] and combined as
a weighted mean." *Produced:* a reasonable four-feature function, but with roughly
equal weights and a TTR mapping that pushed short texts to ≈1.0. *Revised/
overrode:* running it on the Milestone-4 samples, every input skewed AI because
TTR saturated; I re-centred the TTR normalization, cut its weight to 0.05, and
raised burstiness to 0.50. I kept the AI's feature *extraction* (the regexes and
coefficient-of-variation math were fine) but overrode its *weighting and
normalization* — which is where the real judgment lived.

**3. Confidence combination — rejected a plain average.**
*Directed:* "Combine `p_llm` and `p_stylo` into `ai_probability` and a
`confidence` value." *Produced:* an initial version that combined the
probabilities by simple average and set `confidence = max(p, 1−p)`, with **no**
disagreement term. *Revised/overrode:* that reports "leaning AI" (0.59) on the
spec's hard conflict case. I replaced it with the spec's
`confidence = directional · (1 − 0.5·disagreement)` so signal conflict *lowers*
confidence, and verified it against the §4 table. The AI produced plausible-
looking math; I overrode it with the spec's uncertainty-aware formula, which is
the entire point of the project.

