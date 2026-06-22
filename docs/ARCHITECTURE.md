# Discourse Spine: Architecture & Rationale (v0.1)

**Purpose:** the "how it works and why this tool, not the alternative" companion to the project's
decision rationale. Where the decision rationale argues the *rulings*,
this explains the *mechanism*. Three views: (A) the journey a text takes through the finished
system, stage by stage; (B) every tool/component in the abstract, with the alternative it beat;
(C) the build sequence and what each checkpoint is expected to produce.

**One-line architecture:** deterministic code does everything that *moves, anchors, counts,
gates, or routes*; the LLM does only *bounded semantic judgment on pre-selected candidates*,
recorded as versioned votes; humans do only *promotion and publication*. Authority flows down
(source → rule → LLM → human), data flows forward, later stages reference IDs and never rewrite
earlier records.

---

## Part A: What a text is run through (the runtime pipeline)

A source document (a Tech Week talk) passes through a sequence of stages plus a human pass. The
stages that ship in this repo are ingest, annotate, candidates, enrich (Stage 2.5), and the
Stage-4 LLM eval + adjudicate lanes; the harvest front-end and the Stage-5 human-review/report
stage are described here as design but are not shipped in this slice. Each stage is a pure
function of files + registries; each writes new records and mutates nothing upstream.

### Front-end: transcription (Buzz → faster-whisper / large-v3-turbo)
- **What:** audio → SRT (discrete punctuated cues with timecodes). For v0.1 the anchor is the
  existing YouTube SRT; Buzz re-transcription is the post-deadline upgrade path.
- **Why it matters:** every downstream anchor and cue pattern leans on sentence boundaries, so
  transcript *quality* (punctuation, casing, non-overlapping cues) is a hard input, not a
  preference. Per-video vocabulary prompts correct ASR name-mangling (measured: Hedra 1→12 correct).
- **Result:** one transcript artifact per source, to be hashed and locked.

### Stage 0: Harvest (yt-dlp, deterministic, free)
- **What:** pull `info.json` per video (title, description, chapters, uploader, date, duration,
  caption-track inventory). Extract a speaker roster from title+description (deterministic patterns
  first; messy prose → governed LLM roster prompt). Rank sources deterministically (manual captions
  > speaker list in description > chapters > 20–60 min) and machine-pick the pilots.
- **Why:** YouTube hands you source intelligence (rosters, sections, speaker hints) for free.
  Design for the optimal case and let each source's quality profile decide how much activates.
- **Result:** enriched source metadata, a human-confirmed speaker roster, chapter
  `section_candidates`, a logged pilot-selection decision. By design the confirmed roster auto-seeds
  the term gazetteer *and* the next transcription's vocabulary prompt (one input, two uses); both the
  roster and gazetteer files are planned, not present in this slice.

### Stage 1: Ingest (deterministic)
- **What:** parse each SRT cue into structured fields (index, start/end time, text); rebuild a
  **normalized transcript from cue text only** (no timecodes ever inlined); hash the transcript +
  metadata into a SHA-256 lockfile; write a quality profile (attribution tier A–D +
  `segmentation_quality`); emit `units.jsonl` where every unit carries **dual anchors**: a char
  span (primary) and a timecode (secondary).
- **Why:** anchors must be byte-stable across reruns, or the traceability walk-back is a claim
  rather than a proof. Dual anchors mean each survives what breaks the other (a normalization change
  breaks char spans but not timecodes; a re-transcription breaks both, which is *why* re-transcription
  is a new source version, never an in-place edit).
- **Result:** a locked, hashed, sentence-segmented spine with two independent ways to point at any span.

### Stage 2: Annotate (regex, deterministic)
> **As shipped in this repo, the annotate stage is standard-library regex only:** no spaCy and no
> pandas are imported at runtime, and the shipped core depends only on the Python standard library.
> The spaCy `Matcher`/`PhraseMatcher` and pandas usage discussed below are part of the larger
> project's design plan, not runtime dependencies of what ships here.

- **What:** run the versioned rule registry over 100% of the text → `annotations.jsonl`, where each
  hit is one envelope: anchor + `rule_id` + `rule_version` + `label_vote` + trigger text. Run a
  term/gazetteer pass → `term_occurrences.jsonl`. Active families (`advice`, `example`) carry real
  votes; **`reframe`/`caveat` run as shadow**: they fire and generate candidates but carry no
  standalone vote (their authority call happens at the LLM lane; see CP1 freeze).
- **Why:** rules are the **cheap recall layer**: inspectable, diffable, versionable, and affordable
  on every sentence. A rule hit is a *recorded vote, not truth*; precision comes later from the LLM
  and the human.
- **Result:** every cue and term occurrence in the source, anchored and attributed to the exact
  rule version that fired it.

### Stage 2.5: Enrich (deterministic, standoff)
- **What:** read `units.jsonl` and compute additive, standoff features per unit, emitted to a
  separate `feature_records.jsonl` (kept apart from units/annotations/candidates). The shipped
  features are a surface-complexity measurement, a discourse-marker cue profile, and a
  visual/deictic-dependence warning.
- **Why:** features are observations, not decisions. Keeping them in their own file and giving the
  envelope no routing/ranking field makes the non-gating guarantee structural rather than a promise:
  enrich has zero effect on `evidence_ready`, routing, or ranking.
- **Result:** one `feature_records.jsonl` per source, additive over the locked spine.

### Stage 3: Candidates + routing (deterministic)
- **What:** dedup overlapping hits (same source, overlapping span, compatible families → one case
  file, votes merged, refs preserved) → `candidate_case_files.jsonl`. Mint **content-addressed IDs**
  (`source_id` + char span + `rule_id` + schema version). Attach a routing bucket + one-line reason,
  apply caps (top-k overall + per family; flood >200 → tighten, drought <10 → widen one family,
  both logged), set `evidence_ready` **by validators only**, and carry a ±2–3 sentence context window.
- **Why:** routing decides what spends scarce review time and LLM tokens, so it must be explainable
  in one line ("routed because reframe + example co-fired within 2 sentences"). Content-addressed IDs
  mean a rerun or a rule edit never orphans a human decision downstream.
- **Result:** a capped, ranked, reviewable set of self-describing candidates.
- **GATE (CP3):** a human eyeballs ≤5 stage-3 candidates (IDs logged, excluded from the ablation
  pool) before the LLM lane is allowed to run.

### Stage 4: LLM lane (headless `claude -p`, bounded, vote-only)
- **What:** a deterministic **pack builder** selects a routed candidate, bounds the context, applies
  a token cap, inserts the injection guard, and assigns a `pack_id`. **The pipeline decides what the
  LLM sees, never the reverse, and never a full transcript.** The pack goes to a headless
  `claude -p --output-format json` subprocess; output is schema-validated (malformed = rejected +
  logged, **not** retried into compliance) → `llm_votes.jsonl`, each vote carrying `prompt_id`,
  `prompt_version`, model string, `pack_id`, token counts, authority `llm_vote`.
- **What it judges:** the discourse move of the candidate (reframe? advice? caveat?), sharp-vs-bland
  triage, claim-support ("does this excerpt say or imply X?"), and draft wording for human approval.
  The shadow `reframe`/`caveat` candidates get their authority call here; precision is measured as
  (LLM-confirmed ÷ regex-fired) toward the ≥80% promotion trigger.
- **Why:** surface rules catch "it's not X, it's Y"; only semantics catches a reframe phrased
  freshly. The LLM is the **precision layer**, but it only ever sees what a deterministic gate
  already decided was worth the tokens, and it can never overwrite anything upstream.
- **Result:** versioned semantic votes on the candidates that earned them.

### Stage 5: Human review + report (planned, not shipped in this slice)
- **What:** a human edits an **append-only CSV overlay** (`promote | reject | defer | flag_for_tuning`,
  mandatory one-line reason, `llm_vote_seen`, `reviewed_at`, `session_id`; attribution is its own row
  type, and no evidence says "X said Y" until confirmed). Promotions embed their **full evidence payload**
  (anchor, quote, source hash, vote refs) so they survive even if the run folder is deleted. A reporting
  step emits PRISMA-style flow counts, rule yield, a **worst-rule** section, and the ablation table,
  writing a promoted-findings record, a run report, and a one-page corpus card.
- **Why:** promotion and publication are irreducibly human judgment; rejections are training assets
  that feed the rule registry (each session closes with one rule edit or an explicit sparing). PRISMA
  counts make the system's honesty legible: what got filtered, and why.
- **Result:** 3–5 public-safe promoted findings, each with a provable trace behind it.

### The walk-back (the deliverable that proves the whole thing)
Any promoted finding walks backward (**evidence → candidate → votes → annotation → anchor →
hashed source**), and every hop names the method and version that produced it. That walk is only
*provable* (not merely *asserted*) because the anchor-resolution validator re-resolves every quote at
its recorded char span against the hashed source on every run.

---

## Part B: Each component in the abstract, and the alternative it beat

| Component | Abstract role | Chosen | Beat (and why) |
|---|---|---|---|
| **Transcription** | audio → anchorable text | Buzz wrapping faster-whisper / large-v3-turbo, local on the RTX 3080 | YouTube auto-captions (no punctuation/casing → guts sentence segmentation; rolling-caption duplication); Whisper/cloud STT APIs (per-minute billing vs local-first, free). large-v3-turbo = the speed/quality knee; turbo over full large-v3 because panels don't need the last accuracy point. |
| **Metadata harvest** | free source intelligence | `yt-dlp` `info.json` | YouTube Data API (key + quota + ToS friction). yt-dlp is a headless free CLI that matches the local-first, no-API budget. |
| **Integrity** | tamper-evident "this is the exact text we analyzed" | SHA-256 lockfile over transcript **and** metadata | trusting file paths/mtimes (not tamper-evident). A hash matches or it doesn't; this is the accountability layer. |
| **Cue/term matching** | cheap recall over 100% of text | stdlib regex as shipped here (the larger project plans spaCy `Matcher`/`PhraseMatcher`) | running the LLM over all text (cost + non-reproducible + un-diffable); ML/embedding classifiers (need labeled data, opaque, premature). Rules are inspectable, diffable, versionable, and a hit is a *vote, not truth*. |
| **IDs** | stable join keys for the whole graph | content-addressed (`source_id` + char span + `rule_id` + schema version) | run-scoped sequential IDs / UUIDs (orphan human decisions on rerun); LLM-minted IDs (destroy traceability outright). Content-addressing survives reruns and registry edits. |
| **LLM lane** | bounded semantic judgment, best-instrumented voter | headless `claude -p --output-format json` on the Max plan | Anthropic API (per-token billing; Max plan = zero marginal cost); local LLM (insufficient for fresh-phrased discourse judgment); using the LLM to *extract* rather than *judge* (breaks the boundary; it only votes on pre-bounded candidates). Named trade-off: votes are *archival*-reproducible (you can always prove which prompt/model/pack produced one) but not *operationally* reproducible (sampling isn't bit-stable). |
| **Storage / search** | persist + query records | JSONL files + `ripgrep` | SQLite (schema-migration overhead before there's a reader); vector DB / embeddings (deferred). JSONL is diffable, greppable, git-friendly, zero infra ("search = ripgrep until that hurts"). |
| **Human review** | record decisive judgment | append-only CSV overlay, text editor, keyed by `candidate_id` | a review UI / doccano / Prodigy (build cost before a second reviewer exists). Append-only = corrections are new rows; full audit history for free. Never Excel (it corrupts encodings/IDs). |
| **Validation** | gates that can't be sweet-talked | JSON-schema + custom validators; shipped here: structural, **anchor-resolution**, and candidate-completeness (pack-bounds and an overlay validator are planned, not in this slice) | trusting tool exit codes / LLM self-reports. Anchor-resolution is the keystone: it's what turns the traceability walk-back from a claim into a proof. Loud + blocking: a failing stage writes nothing downstream. |
| **Reporting** | legible honesty | PRISMA-style flow counts (borrowed from systematic-review methodology) + pandas | a bespoke metrics dashboard (premature). PRISMA shows exactly what was filtered at each step and why, the credibility signal a reviewer actually reads. |
| **Evaluation** | prove routing beats random without self-deception | pre-registered, blinded, publish-either-way ablation (n=20 smoke test) | an unblinded self-scored centerpiece (would contradict the project's own brand). Pre-registration + blinding + commit-to-publish is the whole point. |
| **Diarization** | who-said-what | **deferred** to v0.2 with a defined trigger; ship the ladder *fields* now | pyannote now (HF tokens, model agreements, setup cost unjustified until review shows sequence-shaped signal lives in Tier B/C sources). "Schema for the optimal case, compute for the proven case." |

**What the competence actually is:** not NLP, but *knowing where not to use the LLM*. The whole
split exists so that anything auditable/rerunnable is deterministic, and the LLM is confined to the
one thing only it can do (judge meaning), inside a slot that can't corrupt the record.

---

## Part C: Build sequence and expected results (CP0 → CP5)

| Checkpoint | What we build / tools | Expected result (exit gate) |
|---|---|---|
| **CP0** (done) | Hand-write the trace page (the schema spec); seed `rule_registry`/`term_seeds`; write & lock `ablation_protocol.md`. | Trace page exists, registries load, protocol committed. |
| **CP1** (done) | Throwaway spike (regex over wave-1) + re-cut/re-spike to freeze segmentation, cue families, term role. | **Design frozen:** sentence-mode locked; `advice`+`example` active; `question` throttled; terms = rank-boost; `reframe`/`caveat` → LLM-lane with shadow-rule promotion at ≥80% precision. |
| **CP2** (next) | `ids.py` (ID format), `schema/*.json` + enums, `ingest.py` (SRT→units, lockfile, quality profile), `annotate.py` (rules + active/shadow split, term pass), `validate.py` (structural + anchor), golden fixture + anchor round-trip test. | 3 pilot sources pass structural + anchor validators; fixture test green; anchor round-trip green. |
| **CP3** | `candidates.py` (dedup, content-addressed IDs, routing + caps, validator-set `evidence_ready`). | Candidate completeness validator green; human eyeballs ≤5 candidates (logged); **only then does the LLM lane unlock.** |
| **CP4** | `stage4_eval.py` (pack builder + injection guard + token cap → headless `claude -p` → schema-validated votes); an ablation step (its standalone script is not included in this repo); one full human review pass; ablation round 1. | Votes validated; ≥1 review session done; ablation table exists; worst-rule section has data. |
| **CP5** | a reporting step (PRISMA counts, rule yield, worst-rule, ablation table; its standalone script is not included in this repo); corpus card; demo bundle. | **v0.1 done:** 3 sources processed, all validators green, ablation verdict recorded (publish either way), ≥3 promoted public-safe findings, a finding spot-checked all the way back to the hashed SRT. |

**Cut order under deadline (never cut validators / fixture / ablation):** `turn_candidates` →
`term_occurrences` → number of LLM pack types. **Stack:** as shipped in this repo, Python standard
library only + headless `claude -p` (the shipped core imports nothing beyond the standard library);
the larger project's plan adds spaCy and pandas (reports only), which are not runtime dependencies
here. ~1,200–1,800 lines including validators and tests.

---

## How this maps back to the work-system

The Spine *is* the work-system's pattern made concrete: cheap deterministic generation (rules) →
explicit gates (validators + CP3) → deliberate promotion (human overlay) → full provenance (content-
addressed IDs + anchor resolution + the walk-back). The shadow-rule mechanism (a cheap deterministic
method that earns authority only after measuring ≥80% precision against a trusted label source) is the
newest reusable method this corpus produced, flagged as a candidate method pending human review.
