# Discourse Spine: Pipeline Reference

> This reference describes the discourse-spine pipeline stage by stage: what each stage does, the mechanisms it uses, and the data it produces. Read it alongside the architecture and methodology docs in this folder for the design rationale and schema.

## Governing principle

Deterministic code handles everything that moves, anchors, counts, gates, and routes. The LLM does only bounded semantic judgment. Human judgment sits at promotion points.

## Pipeline overview

```
Stage 0      Stage 1       Stage 2        Stage 2.5      Stage 3          Stage 4        Stage 5
Harvest  --> Ingest   -->  Annotate  -->  Enrich   -->   Candidates  -->  LLM lane  -->  Human review
(yt-dlp)     (SRT->units)  (rules+terms)  (features)     (dedup+route)   (precision)     (overlay)
```

> **What is runnable here:** This repo ships the deterministic stages (ingest -> annotate -> enrich -> candidates) plus the Stage 4 LLM precision lane and a strong-model adjudication step. Stage 0 (harvest) and Stage 5 (promotion / human review) are described below for completeness so you can see the full design, but they are not included as runnable code in this repo.

> **Numbers in this doc:** Figures are reported for the bundled demo (the three example transcripts that ship in this repo, the thing you get when you clone and run `python run_demo.py`). Where a full-corpus figure is quoted, it is labelled "(full corpus)". Do not assume a full-corpus number describes the demo run.

All scripts default to **dry-run**. Pass `--execute` to write. Set `PYTHONIOENCODING=utf-8` before any Python run (on Windows PowerShell: `$env:PYTHONIOENCODING = "utf-8"`).

---

## Stage 0: Harvest

**Purpose:** Acquire raw YouTube data (audio, subtitles, and metadata) from the Tech Week panel sources (161 sources in the full corpus; the demo ships 3 example transcripts).

**Tools:** `python -m yt_dlp` with `--write-info-json --download-archive archive.txt`

**Outputs per source:**
- `.m4a` audio file
- `.srt` auto-generated subtitles
- `.info.json` full YouTube metadata (title, channel, upload date, duration, description, tags)
- `.jpg` thumbnail

**Location:** `data/examples/` (3 bundled example transcripts; the full corpus is harvested from public YouTube)

**Attribution tier:** Each source carries an attribution tier (A/B/C/D). In the demo this is set by `run_demo.py --attribution-tier` (default `D`); there is no separate source-registry file in this repo.

**Transcription:** The `.m4a` files are then processed through Buzz (local faster-whisper, large-v3 model) to produce higher-quality transcripts. The Buzz configuration is pre-registered in the project controls. Buzz outputs include `.srt`, `.txt`, `.vtt`, and a provenance file per source.

**Downstream feed:** Each `.srt` (plus its `.info.json` metadata) becomes one source for the ingest stage, keyed by a `source_id` derived from the file stem.

---

## Stage 1: Ingest (`spine/ingest.py`)

**Purpose:** Convert raw SRT transcripts into content-addressed units (sentences) with anchoring metadata.

**Input:** One `.srt` file per source (Buzz transcript preferred, yt-dlp fallback).

**Mechanism:**
1. **SRT parsing:** Strips timing codes and formatting, producing a normalized plain-text transcript.
2. **Sentence segmentation:** Splits the transcript into sentence-level units. Each unit is one sentence, the atomic span for all downstream annotation and judgment.
3. **Anchoring:** Each unit gets a character span (`char_start`, `char_end`) in the normalized transcript and a timecode reference from the SRT.
4. **Content-addressed IDs:** Unit IDs are generated via `spine/ids.py` as deterministic hashes of `(source_id, char_start, char_end)`. Never construct IDs by hand.
5. **Quality profiling:** Assesses segmentation quality (good/fair/poor based on sentence-length distribution) and records the attribution tier passed in for the run (`--attribution-tier`, default `D`).

**Outputs per source (in `runs/demo/<source_id>/`):**
| File | Contents |
|---|---|
| `normalized_transcript.txt` | Plain-text transcript with SRT artifacts stripped |
| `units.jsonl` | One JSON object per sentence: `unit_id`, `source_id`, `run_id`, `text`, `anchor` (char_span + timecode), `segmentation_quality` |
| `quality_profile.json` | Source-level stats: unit count, mean/median sentence length, segmentation quality grade, attribution tier |
| `source_lockfile.json` | Provenance: which SRT file was used, file hash, ingest timestamp |

**Scale:** The bundled demo ingests all 3 example transcripts into 2,649 units total. (Full corpus: 153 of 161 sources ingest cleanly, 8 skipped as empty-SRT short clips with no speech, for 41,912 units.)

---

## Stage 2: Annotate (`spine/annotate.py`)

**Purpose:** Apply deterministic rules and term lookups to every unit. A rule hit is a *recorded vote*, not truth. Precision comes later (LLM lane + human review).

**Mechanism:**
1. **Rule matching:** Loads the rule registry (`inputs/rule_registry.csv`) and applies each rule's regex or phrase pattern against every unit's text. A match produces an annotation with a `label_vote` (the cue family the rule votes for) and an `authority_level`.
2. **Term matching:** Loads term seeds (`inputs/term_seeds.csv`) and records term occurrences. An optional speaker gazetteer (`inputs/gazetteer.csv`) can supply confirmed names; it is absent in this repo and `annotate` skips it when it is not present. Terms produce `rank_boost` signals, not routing votes.

**Authority split:**
- **Active rules** produce `authority_level: rule_vote`. These votes count toward candidate routing.
- **Shadow rules** produce `authority_level: shadow`. They are recorded for recall analysis but excluded from routing decisions.

**Outputs per source (appended to the run directory):**
| File | Contents |
|---|---|
| `annotations.jsonl` | One record per (unit, rule) hit: `annotation_id`, `unit_ref`, `rule_id`, `rule_version`, `label_vote`, `trigger_text`, `authority_level`, `authority_mode` |
| `term_occurrences.jsonl` | One record per (unit, term) hit: `term_occurrence_id`, `term`, `canonical_form`, `category`, `role: rank_boost` |

### Rule registry (as of CP3 overhaul)

Five cue families: **advice**, **caveat**, **example**, **question**, **reframe**.

#### Active rules (produce routed votes)

| Rule ID | Family | Pattern type | What it catches | Precision est. |
|---|---|---|---|---|
| `advice_001a` | advice | regex | "you should [verb]" (excludes question-terminal sentences) | ~55-60% |
| `advice_001b` | advice | regex | "you need to / have to [verb]" (excludes interviewer prompts, questions) | ~50-55% |
| `advice_002` | advice | phrase | "the way to do this is" | moderate |
| `advice_003` | advice | regex | "my advice is / what I recommend" | high |
| `caveat_v2a` | caveat | regex | "be careful / watch out for / you need to be careful" | ~60% |
| `reframe_001` | reframe | regex | X-not-Y: "it's not [3-40 chars] it's" | ~65% |
| `reframe_v2a` | reframe | regex | Qualified X-not-Y: "it's not (just\|only\|about) ... it's" | ~65% |
| `reframe_v2b` | reframe | regex | "what (people\|founders\|everyone) (miss\|get wrong)" | ~70% |
| `question_001` | question | regex | "the (key\|real\|open\|hard) question is" | ~100% (n=2) |
| `question_003` | question | phrase | "who actually owns" | specific |

#### Shadow rules (recorded but not routed)

| Rule ID | Family | What it catches | Why shadow |
|---|---|---|---|
| `advice_001c` | advice | "you want to / you must [verb]" | ~30% precision; interviewer prompts, conversational |
| `caveat_001` | caveat | "that said / having said that / but to be fair" | Discourse-transition filler |
| `caveat_002` | caveat | "the (risk\|danger\|catch) is / one (caveat\|concern) is" | Moderate precision; kept for recall |
| `caveat_003` | caveat | "I worry/wonder that / I'm not sure whether" | Social politeness hedge |
| `caveat_v2b` | caveat | "the (risk\|danger\|catch\|downside\|mistake\|tricky part) is" | Problem description ≠ caveat |
| `caveat_v2c` | caveat | "it/that depends" | ~10% precision; social filler |
| `example_001` | example | "for example / for instance" | ~50% precision; catches trailing filler |
| `example_002` | example | "take X, then they" | Anaphoric; needs antecedent resolution |
| `example_004` | example | "think about companies like / look at what X is doing" | Name-dropping without substance |
| `reframe_002` | reframe | "the real question is" | Cross-fires with question family |
| `reframe_003` | reframe | "think of it as / reframe this / look at it differently" | Teaching analogies, not market reframes |
| `reframe_004` | reframe | "the better/right/real question/framing is" | Cross-fires with question family |
| `reframe_v2c` | reframe | "it turns out / the reality is / the truth is" | ~45-55% precision; good recall, needs LLM confirmation |
| `question_002` | question | Generic sentence ending with ? (10-120 chars) | ~15% precision; rhetorical tags, logistical Qs |

#### Removed at CP3

| Rule ID | Why removed |
|---|---|
| `example_003` | 0% precision; matched "the use case" / "the best case" (noun phrases, not examples) |
| `reframe_v2` "but actually" component | ~15% precision; just discourse transition |
| `reframe_v2` "the real X is" component | Cross-fires with question family |
| `caveat_v2` "the problem/challenge/issue is" component | Describes problems, not caveats |

---

## Stage 2.5: Feature enrich (`spine/enrich.py`)

**Purpose:** Compute additive, deterministic features over every unit and emit them as *standoff* records. Standoff means the features live in their own file and never mutate the units, annotations, or candidates. This stage has zero effect on routing, evidence-readiness, or ranking; it is observational only.

**Input:** `units.jsonl` for each source in the run root.

**Mechanism:** For each unit, three deterministic features are computed (no model, surface signals only):

| Feature | Role | Emits when |
|---|---|---|
| `surface_complexity_profile` | measurement | Always one record per unit (length tokens/chars + lexical density). Suppressed payload when `segmentation_quality` is `unpunctuated`/`window_mode` or the unit has fewer than 5 tokens. |
| `discourse_marker_profile` | cue | Only when at least one marker from `features/discourse_marker_lexicon.csv` fires. |
| `possible_visual_or_deictic_dependence` | warning | Only on a high-confidence visual/deictic phrase hit (e.g. "as you can see", "this chart"). |

Feature method versions and IDs are content-addressed via `spine/ids.py`; the discourse-marker version is derived at runtime from the lexicon CSV so a lexicon bump flows into the IDs automatically.

**Output per source:**
| File | Contents |
|---|---|
| `feature_records.jsonl` | One record per (unit, feature) emission: `feature_record_id`, `unit_ref`, `source_id`, `run_id`, `scope`, `method` (stage/name/version), `role`, `signal_basis`, `presentation`, `triggered`, `payload` |

**Scale:** `run_demo.py` runs enrich over the whole run root with `--execute` after the per-source loop. On the bundled demo it emits 3,395 feature records across the 3 transcripts.

---

## Stage 3: Candidates (`spine/candidates.py`)

**Purpose:** Cluster overlapping annotations into deduplicated candidate case files, assign routing buckets, apply adaptive caps.

**Mechanism:**

### 1. Clustering

Annotations are grouped by `(source_id, label_vote)`: same source, same cue family. Within each group, annotations whose character spans overlap or touch are merged into maximal clusters. Different families are never merged. One cluster = one candidate.

### 2. Primary annotation selection

Each cluster gets a deterministic primary annotation:
1. `active` beats `shadow`
2. Tiebreak: lowest `rule_id` lexicographically
3. Further tiebreak: lowest `char_start`

The primary annotation's anchor, rule ID, and family define the candidate's identity.

### 3. Routing buckets

Routing is based on **active vote count only** (shadow votes do not elevate):

| Bucket | Condition | Meaning |
|---|---|---|
| **high** | ≥2 active concurring votes | Multiple active rules agree on this span |
| **medium** | 1 active vote | Single active rule fired |
| **low** | 0 active votes | Shadow-only; observational |

### 4. Adaptive caps

Per-family, per-run volume control:
- Family count >200 routed → tighten to top-k (ranked by bucket then span order)
- Family count ≤200 → keep all routed
- Shadow-only candidates are **excluded** from routed caps; always kept

### 5. Evidence readiness

`validate.determine_evidence_ready()` is the single source of truth. A candidate is evidence-ready if:
- Anchor resolves (unit exists)
- Context window is present (≥1 neighbor unit)
- Attribution is confirmed or N/A (Tier C/D → N/A per GAP-3, not a blocker)

**Output per source:**
| File | Contents |
|---|---|
| `candidate_case_files.jsonl` | One record per candidate: `candidate_id`, `source_id`, `anchor`, `context_window`, `vote_refs`, `routing` (bucket + reason), `authority_level`, `attribution`, `evidence_ready`, `caps_applied` |

### Numbers on the bundled demo

`python run_demo.py` over the 3 example transcripts produces **388 candidates** total, of which **29 route** (high + medium bucket; all 29 are in the advice family) and the rest are shadow/low. All routed candidates have `evidence_ready: true`.

(Full corpus, post-CP3: 5,569 candidates total, 596 routed, 4,973 shadow. The full-corpus per-family split was advice 883/546, caveat 76/14, reframe 91/34, question 4,318/2, example 201/0.)

---

## Stage 4: LLM precision lane (`spine/stage4_eval.py`)

**Purpose:** Bounded semantic judgment over the routed candidates. For each routed candidate the model answers one question, given the candidate text and its context window: is this a genuine discourse cue in its category? It returns a structured confirm / reject / uncertain vote.

**Design constraints:**
- The model does not discover. It judges candidates already surfaced by the deterministic rules.
- Each judgment is independent (one call per candidate, no cross-candidate context, no batching, zero retries).
- Output is a structured vote: verdict (confirm/reject/uncertain) with confidence and a brief rationale, validated against `schema/llm_votes.schema.json` before it is trusted.
- Shadow / low-bucket candidates are excluded by default (they are the recall net for later analysis).

**Planted controls and the control bar:** Hidden control items (decoys, loaded from `controls/stage4_prereg/control_manifest.jsonl`, 30 by default) are mixed into the same batch as the real candidates. Every control's expected verdict is `reject`. The run is only trusted if the lane correctly rejects at least 90% of the controls (`CONTROL_BAR = 0.90`); the summary records whether that bar passed.

**CLI:**

```bash
# dry-run (default): build packs + report counts, NO model calls
python spine/stage4_eval.py --run-root runs/demo

# subscription CLI lane (no metered API spend), the routed set:
python spine/stage4_eval.py --run-root runs/demo --lane claude --execute

# direct Messages API lane instead (needs ANTHROPIC_API_KEY in .env):
python spine/stage4_eval.py --run-root runs/demo --lane anthropic --execute

# judge every candidate, including the low-bucket ones the router filters out:
python spine/stage4_eval.py --run-root runs/demo --lane claude --buckets all --execute
```

Key flags: `--lane claude|anthropic` (subscription CLI vs direct API), `--buckets` (comma-separated routing buckets to judge, default `high,medium`; pass `all` to include the `low` bucket and validate router recall), `--execute` (make live calls and write outputs; default is dry-run). Both lanes default to a cheap model (`claude-haiku-4-5`); every call runs from an isolated empty working directory so the agentic CLI cannot explore the repo instead of answering.

**Outputs (under run-root):**
| File | Contents |
|---|---|
| `llm_votes.jsonl` | One record per valid vote: `llm_vote_id`, `candidate_ref`, `source_id`, `run_id`, `pack_id`, `prompt_id`, `prompt_version`, `model`, `lane`, `is_control`, `authority_level`, `review_state`, `vote` (verdict + confidence + rationale), `token_counts` |
| `malformed_log.jsonl` | One record per failed call, tagged `failure_type`: `lane_failure` (transport/CLI failure, retried on resume) or `model_malformed` (reply could not be parsed/validated, not retried) |
| `stage4_summary.json` | Per-lane counts, verdict distribution, malformed rate, control-vote count, control rejection rate, and whether the 0.90 control bar passed |

**Scope:** 29 routed candidates on the bundled demo (596 routed in the full corpus).

**A committed sample (no key needed):** `precomputed/precision_lane/` holds a real run on the bundled data. It judged 414 items (385 candidates from a `--buckets all` run plus 29 hidden controls): 241 reject, 172 confirm, 1 uncertain. The lane correctly rejected 28 of 29 controls (96.6%), so the control bar passed.

---

## Strong-model adjudication (`spine/stage4_adjudicate.py`)

**Purpose:** A second governed check. A stronger model (default `claude-sonnet-4-6`) re-judges every routed candidate using the identical pack and prompt as the cheap lane, blind to the cheap lane's verdict, then reports where the two models agree, where they flip, and a confusion matrix. It writes to its own file and never touches `llm_votes.jsonl`.

**Guardrails:** Same as the base lane: one call per item, zero retries, no batching, no prompt change. The base verdict is never shown to the adjudicator.

**CLI:**

```bash
python spine/stage4_adjudicate.py --run-root runs/demo --lane claude --execute
```

Flags: `--model` (strong adjudicator, default `claude-sonnet-4-6`), `--lane claude|anthropic`, `--execute` (default dry-run).

**Outputs (under run-root):**
| File | Contents |
|---|---|
| `adjudication_votes.jsonl` | One strong-model vote per routed candidate (same vote schema as the base lane; `tier: escalation`) |
| `adjudication_report.json` | `adjudicator_model`, `base_model`, `n_compared`, `agree`, `agreement_rate`, `n_flips`, the list of flips, and the base-vs-strong confusion matrix |

**A committed sample:** `precomputed/adjudication/` holds a real run on the bundled data. The strong model and the cheap lane agreed on 25 of 29 routed candidates (86%), leaving 4 flips for a human to look at.

---

## Stage 5: Human review (described for completeness, not shipped)

**Purpose:** Final human overlay on LLM-confirmed candidates. Append-only CSV per `controls/review_protocol.md`.

**Design:** Human reviews the LLM's judgment + the original text with context. Can confirm, reject, or flag for discussion. Confirmed candidates become promoted evidence, the pipeline's deliverable.

---

## Operational reference

### Running the pipeline

`run_demo.py` is the real driver. It runs ingest -> annotate -> candidates per source and then enrich over the whole run root, writing everything under `runs/demo/<source_id>/`:

```bash
python run_demo.py
```

On Windows PowerShell, set the encoding first with `$env:PYTHONIOENCODING = "utf-8"`. Useful flags: `--examples` (directory of `.srt` transcripts), `--run-root` (output root), `--attribution-tier A|B|C|D` (default `D`). See the [README](../README.md) for the quickstart and the optional LLM lanes.

### Re-running after registry edits

The pipeline is designed for iterative rule development:
1. Edit `inputs/rule_registry.csv`
2. Re-run `python run_demo.py`. It overwrites `annotations.jsonl`, `term_occurrences.jsonl`, and `candidate_case_files.jsonl` per source
3. Compare the totals printed at the end of the run

### ID system

All IDs are content-addressed via `spine/ids.py`. Never construct IDs by hand.

| ID type | Derived from | Example prefix |
|---|---|---|
| `unit_id` | `(source_id, char_start, char_end)` | `unit_` |
| `annotation_id` | `(source_id, char_start, char_end, rule_id)` | `ann_` |
| `term_occurrence_id` | `(source_id, char_start, char_end, term)` | `term_` |
| `candidate_id` | `(source_id, char_start, char_end, rule_id)` | `cand_` |

### Directory structure

`run_demo.py` writes one subdirectory per source under the run root:

```
runs/demo/
└── <source_id>/
    ├── normalized_transcript.txt    # Stage 1
    ├── units.jsonl                  # Stage 1
    ├── quality_profile.json         # Stage 1
    ├── source_lockfile.json         # Stage 1
    ├── annotations.jsonl            # Stage 2
    ├── term_occurrences.jsonl       # Stage 2
    ├── feature_records.jsonl        # Stage 2.5
    └── candidate_case_files.jsonl   # Stage 3
```

The Stage 4 lanes write their outputs (`llm_votes.jsonl`, `malformed_log.jsonl`, `stage4_summary.json`, `adjudication_votes.jsonl`, `adjudication_report.json`) at the run root, alongside the source subdirectories.
