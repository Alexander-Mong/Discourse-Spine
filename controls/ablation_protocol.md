# Ablation Protocol: Discourse Spine v0.1

> **Pre-registered protocol; the ablation step is not included in this repo (future work).**

**Pre-registered:** Checkpoint 0 (2026-06-12, before pipeline code). **Status: locked. Do not edit after the first ablation window opens.**

## Pre-registered success criterion

Routing wins if routed candidates yield **≥ +2 useful items per 10 reviewed** vs random windows at comparable per-item review time. "Useful" = the reviewer would act on it: verdicts `promote`, `interesting_fp`, or `flag_for_tuning` count as useful; `reject` does not. Measured on the blinded sheet before unblinding.

## Kill criterion

If, after one rule-tuning pass, routed candidates still don't clear +2, stop expanding rules; demo story = traceability harness + honest negative result.

## Source structure

3 sources processed through the full pipeline. Round-1 windows drawn from **2** of them (avoids one video's luck). The **3rd held out entirely** for the round-2 re-test after the worst-rule tuning pass. Fresh windows are never seen in round 1 or the CP3 eyeball.

## Sample and honesty

10 routed (post-cap) + 10 random windows matched for length from the same 2 sources. n=20, described everywhere as a smoke test, not a study.

## CP3-viewed-ID exclusion

Candidate IDs eyeballed at the CP3 gate (≤5, logged in the run manifest) are excluded from the ablation pool before sampling.

## Blinding mechanics

An ablation script (not included in this repo) strips condition labels and all routing metadata (bucket, reason, cue families). Reviewer sees excerpt + context window only. Either pool-all-20-shuffled, or review all 10 random before opening the routed list. Record which was used.

## Recording

Per item, before unblinding: verdict (`promote | interesting_fp | flag_for_tuning | reject`) + seconds_per_item.

## Unblinding and result

The ablation script (not included in this repo) reattaches labels, computes useful-count and per-item time per condition, and delta vs the +2 threshold, writing the round-1 results table (`ablation_round1_table.csv`).

## Feature-level ablation (future work)

A second, finer question uses the same blinding discipline: do the role-tagged
Stage-2.5 features improve Stage-4 semantic review over a feature-less pack, and
over a negative control? The pre-registered design renders the same candidates
into four pack variants, labels stripped before review:

1. **baseline**: candidate pack, no feature block.
2. **feature-light**: measurement and cue features only.
3. **feature-warning**: feature-light plus the visual/deictic-dependence warning.
4. **negative-control**: one real feature's payloads with the unit-to-payload
   mapping shuffled across the sampled units, so the block has identical surface,
   format, and token cost as a real feature but its values are decoupled from the
   unit. This isolates real signal from the "extra analysis is present"
   expectancy effect.

A feature condition counts as lift only if it beats **both** baseline and the
negative control on acceptance and agreement at comparable review time. Beating
baseline but not the negative control is an expectancy effect, not signal. This
comparison is documented for completeness; it is not run in this repo.

## Publish-either-way pre-commitment

The result is recorded in the run report whichever way it lands. A negative result is valid, publishable evidence of the method working as a method.
