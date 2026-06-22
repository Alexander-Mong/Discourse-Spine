# Review Protocol: Discourse Spine v0.1

> Stage 5 (human review) is documented here for completeness; it is not part of the shipped slice (no review/ directory ships).

## Overlay mechanics

File: `review/human_review_overlay.csv`. **Append-only**: corrections are new rows, never edits. Text editor, UTF-8, `candidate_id` is the join key. **Never Excel.**

## Required fields per decision row

`candidate_id` · `decision` (promote | reject | defer | flag_for_tuning) · `reason` (one line, mandatory) · `llm_vote_seen` (true|false) · `reviewed_at` (ISO-8601) · `session_id`

## Attribution confirmation

Its own row type, not a field: `row_type=attribution_confirm` with candidate_id, speaker_id, confirmed_by, reviewed_at, session_id. No promoted evidence claims "X said Y" until this row exists.

## LLM anchoring acknowledgement

LLM votes are visible during review (assisted-by-default, decision report §2). `llm_vote_seen` records it honestly. Anchoring risk is accepted and named in v0.1. Blind mode is a v0.2 option the schema already supports.

## Worst-rule closing ritual

Every review session ends with exactly one of: (1) one rule edit in `inputs/rule_registry.csv` with version bump + the FP examples that triggered it, or (2) an explicit sparing row (`row_type=rule_spared`, rule_id, reason). The session is not closed without this step.
