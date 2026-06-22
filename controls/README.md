# controls/

"Controls" here means the pre-registered governance artifacts that keep the
pipeline honest: the rules, success bars, negative controls, and interpretation
guides that were written down *before* a run so results cannot be back-fitted. A
pre-registration is locked at checkpoint; corrections are append-only.

The files split into two tiers.

## Live and reproducible in this demo

These govern the slice that actually runs in this repo (deterministic core +
jargon + the LLM precision lane + adjudication, over 3 transcripts).

- `stage4_prereg/PREREGISTRATION_2026-06-12.md` and
  `stage4_prereg/control_manifest.jsonl`: the Stage-4 pre-registration and the
  planted decoys (hard negatives, all `expected_verdict: reject`) that the
  precision lane scores itself against.
- `feature_interpretation_guide.md`: what each Stage-2.5 feature measures, what
  it must not be read as, and how it can mislead.
- `feature_pack_budget.md`: the token budget and neutral-framing policy for
  feature instrumentation in a Stage-4 pack.

## Documented method, not shipped as runnable code

These describe steps that are pre-registered but whose code is not part of this
repo.

- `ablation_protocol.md`: the routing and feature ablation. The ablation step
  (`ablate.py`) is not included; this is future work.
- `review_protocol.md`: the Stage-5 human review loop. No `review/` directory
  ships.
