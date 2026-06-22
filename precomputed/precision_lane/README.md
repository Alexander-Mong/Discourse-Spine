# Precomputed sample: LLM precision lane (Stage 4)

These files are real output from running the precision lane over all candidates from the
three bundled demo transcripts. They are committed so you can see what the lane produces
without running it yourself (no key or CLI needed).

Generated with:

```bash
python spine/stage4_eval.py --run-root runs/demo --lane claude --buckets all --execute
```

## Files

- `llm_votes.jsonl`: 414 verdicts in the `llm_votes` schema (385 candidates + 29 controls).
  Each row records the candidate reference, the verdict and rationale, an injection-guard
  flag, the per-call token counts, the lane, and an `is_control` flag.
- `stage4_summary.json`: the run summary (votes, control rejection, token totals).

## What the run shows

Normally the pipeline routes only the high and medium confidence candidates to the model.
This run used `--buckets all`, so it also judged the 356 low confidence candidates the router
would normally filter out. That lets the result test the router itself.

- Controls: 28 of 29 planted decoys rejected (96.6%). The control bar (reject at least 90% of
  decoys) passed, so the lane's verdicts are trustworthy for this run.
- Routed candidates (medium bucket): 19 of 29 confirmed as real advice (66%).
- Non-routed candidates (low bucket): 152 of 356 confirmed (43%).
- Malformed responses: 4 of 418 packs (about 1%).

The routed-vs-low split (the 66% and 43% figures) is not a field in `llm_votes.jsonl`; it is
derived by joining each vote's candidate back to the router's bucket label in the
`candidate_case_files`.

The routed set confirms at a higher rate than the set the router dropped (66% vs 43%), which
shows the router is ordering candidates sensibly. The low bucket is not junk, though. About
43% of it is still real advice, so the router trades recall for cost. Recovering that missed
advice is the job of the separate recall lane described in
[`../../docs/METHODOLOGY.md`](../../docs/METHODOLOGY.md).

The control result is the point of the lane. The model is only trusted on the candidates
because it correctly threw out the planted decoys mixed into the same batch.
