# Feature Interpretation Guide: Discourse Spine Stage 2.5

> **Status:** pre-registered the moment the reviewer signs off (then `controls/` rules apply: no edits without approval).
> Pairs with `schema/feature_envelope.schema.json` and `features/feature_registry.csv`.
> For each feature: *what it measures, what it must NOT be read as, and how it can mislead.*

## The one rule

**A feature is not a verdict.** It is a calibrated observation attached to a unit. It changes nothing
about routing, `evidence_ready`, or ranking (the envelope carries no such field; non-gating is structural,
not promised). The LLM (Stage 4) and the human (Stage 5) remain the only deciders. A feature's job is to make
their decision *better-informed*, never to pre-make it.

## Role taxonomy

| Role | What it asserts | How a reader may use it | How a reader may NOT use it |
|---|---|---|---|
| **measurement** | "Here is a reproducible count/measure of the surface." | Calibrate expectations; spot outliers. | As a quality/insight score. A measure is not a merit. |
| **cue** | "A surface marker associated with a discourse move is present." | Raise a hypothesis to check against the text. | As confirmation the move is real. Markers fire on filler too. |
| **warning** | "This unit may not stand alone; it may depend on something not in the case file." | Discount confidence; ask for more context. | As a reason to reject. A warning flags risk, not worthlessness. |

`signal_basis` is secondary: `deterministic_surface` (reproducible from text/lexicon; all three proof
features) vs `statistical_candidate` (model/corpus estimate; deferred, none here).

---

## F-proof-1 · `surface_complexity_profile` · **measurement**

- **Measures:** unit length (tokens, chars) and lexical density (content-word ratio).
- **Payload:** `{length_tokens, length_chars, lexical_density, suppressed: bool, suppression_reason}`.
- **Suppression (mandatory):** set `suppressed:true`, omit the numbers, when `segmentation_quality ∈
  {unpunctuated, window_mode}` **or** `length_tokens < 5`. Segmentation-fragile units produce meaningless
  measures (adversarial: "segmentation-fragility suppression"). A suppressed record is `triggered:false`.
- **Does NOT mean:** long ≠ substantive; short ≠ shallow; high lexical density ≠ insightful. It is a *shape*,
  not a *value*. Do not let "looks complex" stand in for "is worth promoting."
- **False-positive modes:** list/enumeration sentences inflate length; ASR run-ons merge clauses and inflate
  both metrics; quoted text inflates density without speaker substance.

## F-proof-2 · `discourse_marker_profile` · **cue**

- **Detects:** contrast / deontic / hedging markers via a **versioned lexicon** (each term carries a
  false-positive note). Lexicon version is recorded in `method.version`; any term change bumps it.
- **Payload:** `{markers: [{surface, category, lexicon_version, fp_note}], categories_present: [...]}`.
  `triggered:true` only when ≥1 marker fires.
- **Does NOT mean:** a contrast marker ("but", "not X but Y") ≠ a genuine reframe; a deontic ("should",
  "have to") ≠ genuine advice; a hedge ("might", "I think") ≠ a genuine caveat. The marker is the *question*,
  the text is the *answer*. This is exactly the overreading risk the guide exists to govern.
- **False-positive modes (per category):** *contrast*: discourse-transition filler ("but anyway"), intra-clause
  contrast with no claim; *deontic*: reported/quoted obligation, interviewer prompts ("you should ask…");
  *hedging*: social politeness ("I think" as deference), epistemic hedge on trivia.

## F-proof-3 · `possible_visual_or_deictic_dependence` · **warning**

- **Flags:** high-confidence phrases signalling the unit leans on something outside the text: a slide,
  gesture, or unresolved referent ("as you can see", "this chart", "over here", "that one", "look at this").
- **Payload:** `{phrases: [{surface, kind}], confidence}`. **High-confidence phrases only.** Precision over
  recall by design; a missed dependence is cheaper than a false alarm that discounts a good unit.
- **Does NOT mean:** dependence ≠ low value, and ≠ reject. It means *this case file may be incomplete on its
  own*: read with extra context, or expect the deictic to resolve in the context window.
- **False-positive modes:** metaphorical "see" ("see what I mean"); deixis that *does* resolve in the
  ±2-sentence window; generic "this/that" as a discourse pointer rather than an external referent.

---

## Adding a feature later

Every new feature lands in `features/feature_registry.csv` at `lifecycle_state=proposed` and gets a section
here **before** it may become `pack_visible`. No entry here → it stays `stored_only`. Cue/warning features
(the overreading-prone ones) may not enter a Stage-4 pack until their "does NOT mean" + FP modes are written.
