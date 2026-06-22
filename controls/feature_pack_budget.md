# Feature Pack Budget: Stage-4 prompt instrumentation

> **Status:** pre-registered on reviewer signoff. Governs how (and how much) feature instrumentation enters the
> Stage-4 candidate pack. Operating directive: **store generously, present sparingly.**

> Note: the shipped Stage-4 baseline is **feature-blind** (per the pre-registration), so this budget governs
> feature-visible packs, which are future work.

## Token cap

**Proposed cap: ≤ 150 tokens** for the entire feature block appended to a candidate's Stage-4 prompt
(the block, not per-feature). **FINALIZE AT THE PHASE-3 GATE** (2026-06-12). The number is tuned
against the real Stage-4 prompt once that lane exists; 150 is the working ceiling for Phase 2 so features
are authored to fit.

- The feature block is **additive** to the existing pack (excerpt + ±2-sentence context window). It never
  displaces candidate text; if the block would exceed the cap, drop lowest-priority features first
  (warning > cue > measurement is the *keep* priority: keep warnings, shed measurements).
- Over-budget is a build error surfaced at pack assembly, never a silent truncation.

## What may appear (presentation policy)

Driven by the envelope's `presentation` field:

| `presentation` | In the pack? |
|---|---|
| `stored_only` | Never. Observational / ablation-pool only. |
| `pack_visible_when_triggered` | Only when `triggered:true`. |
| `always_visible` | Always (within budget). |

## Neutral framing (mandatory wording)

The block is introduced with a fixed, non-leading header and renders signals as observations, not judgments:

> **GOOD (neutral):**
> `Signals (not verdicts; deterministic surface observations; confirm against the text):`
> `· surface: 28 tokens, lexical density 0.55 [measurement]`
> `· marker: contrast "not … but" present [cue: a marker, not a confirmed reframe]`
> `· warning: possible visual dependence ("as you can see")`

> **BAD (leading, never do this):**
> `This is a strong reframe (confidence high). Promote.`  ← verdict language, fabricated confidence, a recommendation.

Rules: no "strong/weak", no "confidence", no "promote/reject", no role-free adjectives. Every cue line names
its role inline. The header's "not verdicts" clause is non-removable.
