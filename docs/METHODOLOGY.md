# Methodology: governed AI workflows

This document explains the discipline behind the pipeline. The goal is to extract
high-value, provenance-anchored evidence from a large speech corpus (Tech Week startup/AI
panel transcripts) without the two failure modes that make LLM extraction untrustworthy:
hallucinated content (a quote that was never said) and impressionistic selection (keeping a
line because it sounds memorable, not because it is load-bearing).

The governing rule is a strict division of labor: deterministic code does everything that
moves, anchors, counts, gates, or routes; the LLM does only bounded semantic judgment on
pre-selected candidates; the human does only promotion and publication. Every model output is
treated as a claim to verify, not an answer to accept. See [`ARCHITECTURE.md`](ARCHITECTURE.md)
for how that rule maps onto each stage.

The first half below is shipped and checkable in this repository. The second half describes
work from the full project; those sections are marked, and their run data is not in this repo.

## Shipped evidence (you can run and check this)

These are the strongest demonstration of the discipline, because a reader can reproduce them.

**Deterministic, content-addressed core.** Ingest, annotation, candidate routing, and
enrichment are pure functions of files plus registries. The shipped core imports nothing beyond
the Python standard library. Identifiers are content-addressed (`source_id` + char span +
`rule_id` + schema version), so a rerun or a rule edit never orphans a downstream decision.
Same input, same IDs, same output, covered by 52 tests.

**Provenance walk-back, validated every run.** Every unit carries dual anchors: a character
span (primary) and a timecode (secondary), both pointing back to a SHA-256 lockfile of the
source. An anchor-resolution validator re-resolves each recorded span against the hashed source
on every run, which is what turns the traceability walk-back from a claim into a proof. A
failing validator writes nothing downstream.

**Planted-control precision lane.** Stage 4 asks the model one bounded question per routed
candidate: is this really advice? Hidden decoy controls are mixed into each batch, and a run is
trusted only if the model rejects them. The committed sample
([`precomputed/precision_lane/`](../precomputed/precision_lane/)) is 414 verdicts on the
bundled data (385 candidates plus 29 hidden controls):

- The model rejected 28 of 29 controls (96.6%), so the control bar passed.
- It confirmed 19 of 29 routed candidates (66%) and 152 of 356 non-routed candidates (43%).
  The higher rate on routed candidates is evidence the deterministic router orders candidates
  sensibly; the 43% on the non-routed set shows the router trades recall for cost.

**Blind strong-model adjudication.** A second governed step
([`precomputed/adjudication/`](../precomputed/adjudication/)) re-judges the routed candidates
on a stronger model, blind to the cheap lane's verdict, then reports agreement. The two models
agreed on 25 of 29 candidates (86%), leaving 4 flips for a human to look at. A second model is
a check, not an oracle: nothing is promoted on agreement alone.

**Deterministic jargon floor.** The lowest-risk artifact is a domain-vocabulary dictionary
built with no model in the path (`jargon/build_jargon.py`). It scores words and phrases by
keyness (how much more often they appear here than in everyday English, using the offline
`wordfreq` package), filters by spread across panels, and keeps short usage examples. It cannot
hallucinate, because it only counts what is verbatim in the corpus.

## The hard problem: judging value (from the full project)

*(From the full project; the code and run data are not in this repo.)*

Anchoring and counting are the tractable half. The genuinely hard problem is judging which
extracted spans are worth surfacing. The honest history is a sequence of falsified designs.

**v0: judge each lens against its own rule.** Asking "does this span satisfy the extraction
rule?" measures recall and compliance, not value, so it rewards dredging. A pre-registered
adversarial control proved this: a deliberately-junk decoy extractor outscored both production
lenses. A judge that a designed-junk extractor can beat is not measuring value. (The control
*protocols* ship in [`controls/`](../controls/); the ablation run data does not.)

**v1: a single expert perspective.** "Would a market analyst save this?" hard-codes one stratum
of relevance. Value is not a scalar property of a span; it is a relation between an idea, an
audience, and a task, so one perspective is one slice.

**v2: a multi-perspective value panel**, anchored on known-good and known-junk spans, judged
against an external standard, and aggregated by union so a minority audience's value is not
averaged away.

**The retraction.** A small-sample run of v2 looked clean. The de-noised, larger-sample run
retracted that result: the junk decoy no longer cleanly floored, and the anchors did not cleanly
bracket. Root cause: span-value is not the same as lens-quality. The surviving, defensible claim
is narrower: the panel floors off-the-shelf junk and tops the high-value constructs (`mistake`,
`caveat`, `conditional_advice`); the mid-tier is muddy and is not claimed as solved.

The broader conclusion is that you cannot certify universal value, because even a human expert
is single-perspective. So the method certifies the objective, provable signals a human can
ground (verbatim anchoring, specificity, cross-speaker frequency, consensus,
construct-membership) and estimates perspectival value per-audience, attaching the objective
signature and letting the consumer weight it. One corollary: consensus-frequency is its own
value axis. A platitude every experienced founder repeats carries near-zero marginal
information to a veteran, yet its ubiquity is evidence of its importance, so the system reports
"N of M independent speakers affirm X" and lets the audience decide.

## The calibration loop (from the full project)

*(From the full project; the code and run data are not in this repo.)*

The objective layer (does this span instantiate construct X?) is where reliability can be
earned cheaply, without extensive gold labeling:

1. **Adversarial anchors as ground truth.** For each construct, a few hand-confirmed clear-yes,
   clear-no, and hard near-miss spans (look-alikes that should not tag, which catch over-firing).
   Authoring clear examples, not labeling hundreds, is the high-leverage human touch.
2. **Multi-label, not forced-choice.** Each span is judged per-construct, independently, so it
   can carry several tags at once. This dissolves the attractor bias that made a few labels
   absorb everything in a wide forced choice.
3. **Calibrate, localize, sharpen, re-probe.** The anchors localized which constructs misfired
   and why; sharpening the definitions (with human rulings on genuinely fuzzy boundaries)
   converged to 100% on the calibration set.
4. **Held-out test, frozen judge.** Tested on new edge spans the judge was never tuned on,
   `quant_anchor` and `caveat_narrow` both validated at 100% held-out. The open frontier is
   `evidence_type`: its statistic-as-evidence boundary generalizes at about 80% held-out. That
   residual is reported as-is rather than re-tuned away.

The calibrated judge then ran across the full corpus as one governed evaluation pass: the
multi-label pool covered 5,521 spans across 3 calibrated constructs at 99.86% parse, with the
other seven constructs left gated on their dossiers (a named cut, not a silent gap). Two results
mattered: the calibration corrected a human label (a bare statistic mislabeled as evidence), and
the anchors showed what not to build. The bottleneck was definitional clarity, not judge count,
so the expensive cross-vendor judge panel was avoided. When cross-vendor agreement was measured,
two vendors' votes carried barely more than one independent vote.

## The refinement flywheel (from the full project)

*(From the full project; the code and run data are not in this repo.)*

The calibration above started as a one-time, hand-run pass. The next step was to make it
repeatable and governed, so the system improves itself without sliding into quiet self-tuning.
It feeds two outputs (the confirmed findings, and the rules-vs-model coverage comparison) back
into two refinements:

- **Refine the rules** where they missed real finds. The model surfaced 175 confirmed advice
  spans the keyword rules never flagged; mining them exposed a concrete gap (the rules catch
  "you should / you need to" but miss the "make sure X" imperative). New cues are admitted only
  if they clear a specificity gate, and they enter as observed-only until a later round measures
  their precision.
- **Sharpen the model's judgment** on its own borderline cases. Near-threshold calls are routed
  into a tuning queue; the most-flagged construct gets a proposed sharper definition, re-probed
  live on held-out anchors.

A sharpening is promoted only if it strictly improves held-out accuracy. In the first run it did
not (frozen and sharpened scored identically), so the loop declined to promote it and named its
own next action. Every promotion is staged, measured, reversible, and passes through the human
gate; the rules are fixed within a run and refined only between runs.

## What this is

This is a governed evaluation-and-evidence workflow: the discipline that sits around models to
make their output trustworthy. Retrieval and generation were not the problem to solve here;
auditability and trust were, so the LLM is confined to a single bounded judgment call and
deterministic code owns the rest. For the per-stage mechanism and the alternatives each
component beat, see [`ARCHITECTURE.md`](ARCHITECTURE.md).

To be clear about where the evidence lives: the precision lane, the blind adjudication, the
deterministic jargon floor, the content-addressed core, and the anchor-resolution validator
ship in this repository and can be run and checked here. The corpus-scale results (the value
panel, the calibration loop, and the refinement flywheel) are from the full project and are
reproducible there, not in this repo.

## Known limitations (measured)

These are limits the work knows about and reports rather than hides.

- **The control bar bounds false-confirms only.** The planted controls are all "should-reject"
  spans, so the bar measures specificity: the model not confirming junk. It does not bound the
  false-reject rate, so a model that over-rejects would still pass. The confirm side is guarded
  by a separate human spot-check, not by this gate.
- **The control rejection rate blends easy and hard decoys.** The battery mixes genuinely hard
  near-misses with deliberately trivial off-family and ASR-garbled decoys, so a model could miss
  several hard negatives and still clear the 90% bar on the easy ones. Reporting the
  hard-negative and constructed rejection rates separately is more honest than the blended number.
- **`injection_guard` is a provenance label, not a per-vote control.** It records that the prompt
  carries the injection-guard clause. The real defense against adversarial transcript text is the
  control bar, which measures whether such spans actually flip a verdict.
- **The keyword rules have known recall gaps.** They anchor advice on second-person "you" and
  miss bare imperatives ("start small", "talk to your customers"); the caveat family thinly
  covers genuine epistemic qualification of a claim. Recovering what the rules miss is the job of
  the recall lane (described above, not shipped here).
