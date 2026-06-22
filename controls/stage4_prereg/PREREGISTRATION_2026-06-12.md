# Stage 4 Evaluator: Pre-Registration Record

**Registered (UTC):** 2026-06-12T20:53:45Z
**Status:** LOCKED. Corrections are **append-only** (see §10): no edits to the locked settings or sample/control ID lists.
**Governs:** the first Stage-4 evaluator baseline run. The protocol was pre-registered for the full corpus (596 routed candidates). The demo bundled in this repo reproduces the method at smaller scale: 29 routed candidates over 3 transcripts (see `precomputed/`).
**Spec:** the Stage-4 evaluator spec (design rationale). **The project decision report wins on conflict.**

> Purpose of pre-registering: the success bars, the negative controls, and the sample draws are fixed **before** the run, so results can't be back-fitted. This is the evaluator analog of `controls/ablation_protocol.md`.

---

## 1. Run target

- The protocol was pre-registered for the full corpus: **all 596 routed** candidates (high+medium): advice 546, reframe 34, caveat 14, question 2. (example: 0 routed, out of scope.) Run scope is the whole 596 in one baseline run; stratification is deferred.
- The bundled demo reproduces the method at smaller scale: **29 routed candidates over 3 transcripts** (see `precomputed/`). The full-corpus run is not included in this repo.

## 2. Pack (baseline)

- Candidate text + **fixed ±2–3 sentence context window**.
- **Feature-blind and posture-blind.** No Stage-2.5 feature records, no source/speaker metadata. Never the full transcript.
- Injection guard present in every pack.
- Prompt: `candidate_evaluate` v1 (`inputs/prompts/candidate_evaluate_v1.md`).

## 3. Output / schema

- `schema/llm_votes.schema.json` (revised 2026-06-12). Vote = `verdict` (confirm/reject/uncertain) · `inference_type` (stated/inferred) · `confidence` (high/low) · `rationale` · `rejection_reason` (seeded enum, required on reject) · `reason_note` (free-text, mineable, non-authoritative).
- Enums frozen in `schema/enums.json`: `verdict`, `inference_type`, `confidence`, `vote_tier`, `rejection_reason`.
- Mandatory provenance per vote: `pack_id`, `prompt_id`, `prompt_version`, `model`, `token_counts`, `authority_level=llm_vote`, `attempt`, `tier`. `is_control` set by the harness, never in the pack.

## 4. Models

- Base passes on **Haiku** and **Sonnet** (parallel, same packs), measuring actual cost/tokens. Then **likely Opus**.
- Cross-model agreement is a diagnostic (not a bar).

## 5. Batching (pre-registered before looking)

- **Batch size: 10** candidates per `claude -p` call.
- **Validation:** re-run the spot-check sample (§7) **one-per-call (isolated)** and compare to batched verdicts.
- **Threshold (batching declared safe iff both hold):** verdict agreement (isolated vs batched) **≥ 90%** AND confirm↔reject flip rate **≤ 5%**. Breach either → switch the run to one-per-call. (Uncertain↔{confirm,reject} flips count toward the 90% agreement figure but not the 5% confirm↔reject cap.)

## 6. Malformed output (decision report S4)

- **Zero retries** in the baseline. Schema-invalid votes are rejected + logged, not retried. Measure the malformed rate; add a single format-only retry later only if warranted.

## 7. Pre-registered samples (IDs frozen in sibling files)

- **Strong-model adjudication:** every routed candidate is re-judged on a strong model (default claude-sonnet-4-6), blind to the base verdict, and agreement plus flips are reported. The original full-corpus study used pre-registered stratified samples to hold down cost; this repo adjudicates the full routed set, which is small enough not to need sampling. The human reviewer's call is final.
- **Overlap:** spot-check ∩ human-accuracy = **9 items** (forced by tiny caveat/question routed counts). They measure different things; overlap does not bias either.

## 8. Negative controls

- `control_manifest.jsonl`: **30 hidden controls**: 20 real hard negatives mined from shadow candidates (carry real `ref_candidate_id` + rationale) + 5 constructed off-family + 5 constructed ASR-mangled. Mimicked families: advice 10 / reframe 7 / caveat 7 / question 6. All `expected_verdict: reject`. (The manifest defines 30; the committed demo run used 29.)
- Injected into the run flagged `is_control` **outside** the pack; the model sees them as ordinary candidates.
- Honesty: real hard-negative expected verdicts are careful human judgments (rationale recorded per item); constructed controls are unambiguous. Any control the reviewer disputes is pulled via an append-only correction (§10), not an edit.

## 9. Success bars (calibration-first)

The first run is a **calibration run**, not a graded exam. Final Stage-5 gate numbers are set from the observed distribution afterward. Pre-registered flags:

| Signal | Bar | Type |
|---|---|---|
| Hidden-control rejection rate | **≥ 90%** | Hard: failing to reject obvious controls is disqualifying |
| Human spot-check agreement (30-sample) | **≥ 75%** | Provisional flag, firm up post-calibration |
| Cross-model agreement (Haiku/Sonnet/Opus) | n/a | Diagnostic only |
| Uncertain rate | tracked | High rate ⇒ design problem, not just hard candidates |

- Uncertain candidates route deterministically to the **escalation tier** (Opus); escalated re-judgments carry `tier=escalation`, `attempt>1`.

## 10. Corrections log (append-only)

> Add a dated line for any post-registration change (e.g. a pulled control). Never edit §1–§9 in place.

- 2026-06-12: **Model set (revises §4) for the first/calibration pass.** The first run is a *cheap cross-vendor* calibration: **Claude Haiku** (`claude-haiku-4-5` via `claude -p --output-format json`) + **OpenAI `gpt-5.5` at `model_reasoning_effort=low`** (via codex; the ChatGPT account exposes no mini/nano model). **Gemini Flash is deferred**: its CLI is agentic and contaminates single-shot judgments and EOLs Jun 18. Sonnet/Opus and the uncertain→Opus escalation (§9) are unchanged; they apply to later passes, not this cheap calibration. Rationale: §7/§9 already frame run 1 as calibration (not a graded exam); cheap cross-vendor models maximize the cross-model agreement + control-discrimination diagnostics at near-zero spend. (Decision recorded this session.)
- 2026-06-12: **"Malformed" defined (refines §6).** A vote is *malformed* iff no schema-valid JSON object can be extracted from the model's response after a single fixed deterministic cleanup: strip surrounding markdown code fences, then take the first balanced top-level `{...}`. Well-formed JSON wrapped in code fences is **not** malformed (observed from Haiku via the runner). This cleanup is **not** a retry (no re-prompting the model); the zero-retries rule (§6) stands. The malformed rate is measured after this cleanup. (Pre-registered before the run.)
