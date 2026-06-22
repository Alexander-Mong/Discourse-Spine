# Precomputed sample: strong-model adjudication

Real output from `spine/stage4_adjudicate.py`. It re-judges every routed candidate on a
stronger model (claude-sonnet-4-6), blind to the cheap lane's verdict, then compares the two.
The point is to check the cheap Haiku lane against a stronger second opinion.

Generated with:

```bash
python spine/stage4_adjudicate.py --run-root runs/demo --lane claude --model claude-sonnet-4-6 --execute
```

## Files

- `adjudication_votes.jsonl`: 29 strong-model verdicts (tier `escalation`), one per routed
  candidate, in the same `llm_votes` schema. Written separately; the base verdicts are never
  shown to the adjudicator and never modified.
- `adjudication_report.json`: the comparison.

## Result on this run

- Compared: 29 routed candidates.
- Agreement: 25 of 29 (86%) between Sonnet and Haiku.
- Flips: 4 (2 the strong model confirmed that Haiku rejected, 2 the reverse).
- Confusion: confirm/confirm 18, reject/reject 7, base-reject/strong-confirm 2,
  base-confirm/strong-reject 2.

86% agreement between a cheap and a strong model on the same packs is the kind of number this
lane exists to produce. The 4 flips are the candidates a human reviewer would look at first.
