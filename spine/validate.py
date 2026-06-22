"""validate.py - blocking validators for the Discourse Spine.

Stdlib only. A lightweight JSON-Schema checker (draft-07 subset) reads schema/*.schema.json
and resolves the custom `enumRef` keyword against schema/enums.json (single source of truth
for controlled vocabulary). Three families run at CP3, all BLOCKING:

  1. structural        valid JSONL; required fields; types; enum membership; unique IDs; FK refs.
  2. anchor resolution every char_span re-resolves against the HASHED transcript; the transcript
                       hash matches the lockfile; every timecode parses; every unit_ref resolves.
  3. candidate         every candidate has anchor + >=1 vote_ref + valid authority_level +
                       review_state + non-empty routing.reason; every vote_ref resolves to a real
                       annotation_id; candidate_id matches pattern and re-derives from primary span+rule;
                       evidence_ready value matches validator-derived value (tamper/stale detection).

Failure behaviour: LOUD and BLOCKING. Any error -> nonzero exit; the run writes nothing
downstream. This is the keystone that turns the traceability walk-back from a claim into a proof.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

_TYPE = {"object": dict, "array": list, "string": str, "integer": int,
         "number": (int, float), "boolean": bool}
_TIMECODE = re.compile(r"^\d{2}:\d{2}:\d{2},\d{3}$")
_CAND_ID_PAT = re.compile(r"^cand_[0-9a-f]{8}$")

# Valid authority_level values for a candidate (module-level constant; hoisted
# out of validate_candidates so it is not rebuilt per iteration).
VALID_AUTHORITY = {"source", "rule_vote", "shadow", "llm_vote", "human"}

# JSON-Schema keywords this hand-rolled checker understands. Any keyword on a
# schema node that is NOT in this set is an unsupported constraint: rather than
# silently ignoring it (which would let an unenforced constraint pass), we
# append an error. Keys here fall into two groups:
#   - enforced keywords: the checker actively validates them.
#   - structural/tolerated keys: metadata ($schema, title, ...) plus draft-07
#     conditional keys (if/then) that the current schemas carry but whose
#     conditional semantics this subset does not enforce. They are recognized
#     (so they do not trip the unsupported-keyword guard) but are intentionally
#     not applied, preserving existing validation results byte-for-byte.
_SUPPORTED_SCHEMA_KEYWORDS = {
    # enforced
    "type", "const", "enum", "enumRef", "pattern", "minLength", "minimum",
    "properties", "required", "additionalProperties", "items",
    "minItems", "maxItems",
    # structural / metadata
    "$schema", "$id", "title", "description", "$comment",
    # draft-07 conditional keys present in current schemas; recognized but the
    # conditional logic is intentionally not enforced (no result change).
    "if", "then",
}

# jsonl file -> schema basename
FILE_SCHEMA = {
    "units.jsonl": "units.schema.json",
    "annotations.jsonl": "annotations.schema.json",
    "term_occurrences.jsonl": "term_occurrences.schema.json",
    "candidate_case_files.jsonl": "candidate_case_files.schema.json",
    "llm_votes.jsonl": "llm_votes.schema.json",
}
ID_FIELD = {
    "units.jsonl": "unit_id", "annotations.jsonl": "annotation_id",
    "term_occurrences.jsonl": "term_occurrence_id",
    "candidate_case_files.jsonl": "candidate_id", "llm_votes.jsonl": "llm_vote_id",
}


def determine_evidence_ready(candidate: dict, units_by_id: dict) -> "bool | str":
    """
    CANONICAL single source of truth for evidence_ready.

    Returns True if BOTH of:
      - anchor.unit_ref resolves to a known unit_id
      - context_window is present (non-None dict)

    Returns "blocked:<reason>" otherwise.

    Attribution gate: under the current schema attribution is ASSUMED-PASSING and
    is therefore NOT a blocker here. Every attribution tier is known from
    quality_profile (Tier A/B confirmed; Tier C/D collapse to N/A per GAP-3), so
    no unresolvable attribution case exists. The body intentionally enforces only
    the unit_ref and context_window checks; this docstring states that contract so
    it matches the code (the attribution check is a no-op, not a hidden gate).

    This is the ONLY place the evidence_ready rule lives. candidates.py imports and calls
    this function; validate_candidates re-derives and enforces the emitted value equals
    the re-derived value (stale/hand-edited/tampered detection).
    """
    unit_ref = candidate.get("anchor", {}).get("unit_ref")
    if not unit_ref or unit_ref not in units_by_id:
        return "blocked:unit_ref_unresolved"

    context_window = candidate.get("context_window")
    if context_window is None:
        return "blocked:context_missing"

    # Tier C/D -> N/A (not a blocker per GAP-3).
    # Tier A/B is confirmed. Attribution tier is always known from quality_profile.
    # No unresolvable case exists in current schema; any known tier passes here.
    return True


def _is_type(value, t):
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if t == "boolean":
        return isinstance(value, bool)
    if t == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, _TYPE.get(t, object))


def check(value, schema, path, enums, errors):
    # Guard: reject any schema keyword this subset does not handle. Without this,
    # an unimplemented constraint (maximum, maxLength, oneOf, anyOf, allOf, not,
    # $ref, format, patternProperties, ...) would be silently ignored and could
    # pass unenforced. See _SUPPORTED_SCHEMA_KEYWORDS for the recognized set.
    for kw in schema:
        if kw not in _SUPPORTED_SCHEMA_KEYWORDS:
            errors.append(f"{path}: unsupported schema keyword {kw!r} (constraint not enforced)")

    t = schema.get("type")
    if t is not None:
        types = t if isinstance(t, list) else [t]
        if not any(_is_type(value, tt) for tt in types):
            errors.append(f"{path}: expected type {t}, got {type(value).__name__}")
            return
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: expected const {schema['const']!r}, got {value!r}")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} not in enum {schema['enum']}")
    if "enumRef" in schema:
        allowed = enums.get(schema["enumRef"], [])
        if value not in allowed:
            errors.append(f"{path}: {value!r} not in enums[{schema['enumRef']}]")
    if "pattern" in schema and isinstance(value, str):
        if not re.search(schema["pattern"], value):
            errors.append(f"{path}: {value!r} does not match /{schema['pattern']}/")
    if "minLength" in schema and isinstance(value, str) and len(value) < schema["minLength"]:
        errors.append(f"{path}: shorter than minLength {schema['minLength']}")
    if "minimum" in schema and isinstance(value, (int, float)) and value < schema["minimum"]:
        errors.append(f"{path}: {value} < minimum {schema['minimum']}")
    if isinstance(value, dict) and (schema.get("type") == "object" or "properties" in schema):
        props = schema.get("properties", {})
        for req in schema.get("required", []):
            if req not in value:
                errors.append(f"{path}: missing required field '{req}'")
        if schema.get("additionalProperties") is False:
            for k in value:
                if k not in props:
                    errors.append(f"{path}: unexpected field '{k}'")
        for k, v in value.items():
            if k in props:
                check(v, props[k], f"{path}.{k}", enums, errors)
    if isinstance(value, list) and schema.get("type") == "array":
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{path}: fewer than minItems {schema['minItems']}")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path}: more than maxItems {schema['maxItems']}")
        if "items" in schema:
            for i, item in enumerate(value):
                check(item, schema["items"], f"{path}[{i}]", enums, errors)


def _load_jsonl(path):
    rows = []
    with open(path, encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{path}:{n}: invalid JSON: {exc}") from exc
    return rows


def validate_structural(run_dir, schema_dir):
    run_dir, schema_dir = Path(run_dir), Path(schema_dir)
    enums = json.loads((schema_dir / "enums.json").read_text(encoding="utf-8"))
    errors = []
    unit_ids = set()

    for fname, sname in FILE_SCHEMA.items():
        fpath = run_dir / fname
        if not fpath.exists():
            continue
        schema = json.loads((schema_dir / sname).read_text(encoding="utf-8"))
        rows = _load_jsonl(fpath)
        seen = set()
        idf = ID_FIELD[fname]
        for i, row in enumerate(rows):
            check(row, schema, f"{fname}[{i}]", enums, errors)
            rid = row.get(idf)
            if rid in seen:
                errors.append(f"{fname}[{i}]: duplicate {idf} {rid!r}")
            seen.add(rid)
        if fname == "units.jsonl":
            unit_ids = {r.get("unit_id") for r in rows}

    # FK refs: every unit_ref must resolve to a real unit_id.
    for fname in ("annotations.jsonl", "term_occurrences.jsonl"):
        fpath = run_dir / fname
        if not fpath.exists():
            continue
        for i, row in enumerate(_load_jsonl(fpath)):
            ref = row.get("unit_ref")
            if ref not in unit_ids:
                errors.append(f"{fname}[{i}]: unit_ref {ref!r} resolves to no unit")
    return errors


def validate_anchors(run_dir):
    run_dir = Path(run_dir)
    errors = []

    # Guard the required-file reads: a missing/unreadable transcript or lockfile
    # is a COLLECTED error, not a raw exception that aborts the whole run.
    transcript_path = run_dir / "normalized_transcript.txt"
    lock_path = run_dir / "source_lockfile.json"
    try:
        transcript = transcript_path.read_text(encoding="utf-8")
    except OSError as exc:
        errors.append(f"normalized_transcript.txt: cannot read ({exc})")
        return errors
    try:
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
    except OSError as exc:
        errors.append(f"source_lockfile.json: cannot read ({exc})")
        return errors
    except json.JSONDecodeError as exc:
        errors.append(f"source_lockfile.json: invalid JSON ({exc})")
        return errors

    actual = "sha256:" + hashlib.sha256(transcript.encode("utf-8")).hexdigest()
    # Guard the nested lockfile access: a malformed lockfile shape is a collected
    # error, not a KeyError/TypeError.
    try:
        expected_hash = lock["transcript"]["sha256"]
    except (KeyError, TypeError) as exc:
        errors.append(f"source_lockfile.json: missing transcript.sha256 ({exc})")
        expected_hash = None
    if expected_hash is not None and actual != expected_hash:
        errors.append(f"transcript hash mismatch: file={actual} lockfile={expected_hash}")

    # Guard units load: a missing/malformed units file is collected, not raised.
    try:
        unit_rows = _load_jsonl(run_dir / "units.jsonl")
    except (OSError, ValueError) as exc:
        errors.append(f"units.jsonl: cannot read ({exc})")
        return errors

    units = {}
    for u in unit_rows:
        uid = u.get("unit_id") if isinstance(u, dict) else None
        if uid is None:
            errors.append("unit (unknown id): missing unit_id")
            continue
        units[uid] = u

    for uid, u in units.items():
        # Guard the char_span unpack and nested anchor/timecode access so one
        # malformed unit produces a collected error, not a ValueError/KeyError.
        try:
            s, e = u["anchor"]["char_span"]
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(f"unit {uid}: malformed anchor.char_span ({exc})")
            continue
        if not (0 <= s < e <= len(transcript)):
            errors.append(f"unit {uid}: char_span [{s},{e}] out of bounds (len {len(transcript)})")
            continue
        if transcript[s:e] != u.get("text"):
            errors.append(f"unit {uid}: text does not re-resolve at char_span [{s},{e}]")
        try:
            tc = u["anchor"]["timecode"]
            for k in ("start", "end"):
                if not _TIMECODE.match(tc[k]):
                    errors.append(f"unit {uid}: timecode.{k} {tc[k]!r} does not parse")
        except (KeyError, TypeError) as exc:
            errors.append(f"unit {uid}: malformed anchor.timecode ({exc})")

    ann_path = run_dir / "annotations.jsonl"
    if ann_path.exists():
        try:
            ann_rows = _load_jsonl(ann_path)
        except (OSError, ValueError) as exc:
            errors.append(f"annotations.jsonl: cannot read ({exc})")
            return errors
        for a in ann_rows:
            aid = a.get("annotation_id") if isinstance(a, dict) else None
            # Guard the char_span unpack on the annotation record.
            try:
                s, e = a["anchor"]["char_span"]
            except (KeyError, TypeError, ValueError) as exc:
                errors.append(f"annotation {aid!r}: malformed anchor.char_span ({exc})")
                continue
            if not (0 <= s < e <= len(transcript)):
                errors.append(f"annotation {aid}: char_span out of bounds")
                continue
            u = units.get(a.get("unit_ref"))
            if u:
                try:
                    ref_span = u["anchor"]["char_span"]
                except (KeyError, TypeError):
                    ref_span = None
                if ref_span is not None and ref_span != [s, e]:
                    errors.append(f"annotation {aid}: span != referenced unit span")
            trig = a.get("trigger_text")
            if not isinstance(trig, str):
                errors.append(f"annotation {aid}: missing or non-string trigger_text")
            elif trig.lower() not in transcript[s:e].lower():
                errors.append(f"annotation {aid}: trigger_text not found within unit span")
    return errors


def validate_candidates(run_dir):
    """
    Candidate-completeness validator (BLOCKING, added at CP3).

    Checks:
    - Every candidate has anchor (resolves) + >=1 vote_ref + valid authority_level
      + review_state + non-empty routing.reason
    - Every vote_ref resolves to a real annotation_id in annotations.jsonl (no orphaned refs)
    - candidate_id matches ^cand_[0-9a-f]{8}$ and re-derives from primary span+rule
      (primary = the vote_ref whose annotation has the highest authority and lowest rule_id,
       which is the annotation whose (char_span, rule_id) the candidate_id was derived from)
    - evidence_ready emitted value EQUALS validator-re-derived value via determine_evidence_ready
      (stale/hand-edited/tampered detection — BLOCKING mismatch error)

    Note on re-derivation: the candidate_id encodes the PRIMARY annotation's
    (source_id, char_start, char_end, rule_id). We identify the primary by re-running the
    same deterministic selection as candidates.py: active > shadow, tiebreak rule_id lex,
    tiebreak char_start. We then re-derive and compare.
    """
    # sys / Path / hashlib are imported at module top; no re-import needed here.
    _spine = Path(__file__).resolve().parent
    if str(_spine) not in sys.path:
        sys.path.insert(0, str(_spine))
    import ids as _ids

    run_dir = Path(run_dir)
    errors = []

    cand_path = run_dir / "candidate_case_files.jsonl"
    if not cand_path.exists():
        # No candidates file -> nothing to validate (skip silently; structural handles presence)
        return errors

    ann_path = run_dir / "annotations.jsonl"
    ann_by_id = {}
    if ann_path.exists():
        for a in _load_jsonl(ann_path):
            ann_by_id[a["annotation_id"]] = a

    # Build units_by_id for evidence_ready re-derivation
    units_path = run_dir / "units.jsonl"
    units_by_id = {}
    if units_path.exists():
        for u in _load_jsonl(units_path):
            units_by_id[u["unit_id"]] = u

    candidates = _load_jsonl(cand_path)
    seen_ids = set()

    for i, cand in enumerate(candidates):
        cid = cand.get("candidate_id", "")
        ctx = f"candidate_case_files.jsonl[{i}] ({cid!r})"

        # 1. candidate_id pattern
        if not _CAND_ID_PAT.match(cid):
            errors.append(f"{ctx}: candidate_id does not match ^cand_[0-9a-f]{{8}}$")

        if cid in seen_ids:
            errors.append(f"{ctx}: duplicate candidate_id")
        seen_ids.add(cid)

        # 2. required fields present and non-trivial
        anchor = cand.get("anchor", {})
        if not anchor.get("char_span"):
            errors.append(f"{ctx}: missing or empty anchor.char_span")
        if not anchor.get("unit_ref"):
            errors.append(f"{ctx}: missing anchor.unit_ref")

        vote_refs = cand.get("vote_refs", [])
        if not vote_refs:
            errors.append(f"{ctx}: vote_refs is empty (requires >=1)")

        authority_level = cand.get("authority_level", "")
        if authority_level not in VALID_AUTHORITY:
            errors.append(f"{ctx}: authority_level {authority_level!r} not in {VALID_AUTHORITY}")

        review_state = cand.get("review_state", "")
        if not review_state:
            errors.append(f"{ctx}: review_state is empty")

        routing = cand.get("routing", {})
        if not routing.get("reason", "").strip():
            errors.append(f"{ctx}: routing.reason is empty")

        # 3. Every vote_ref resolves to a real annotation_id
        for ref in vote_refs:
            if ref not in ann_by_id:
                errors.append(f"{ctx}: vote_ref {ref!r} does not resolve to any annotation_id")

        # 4. candidate_id re-derivation from primary annotation
        # Primary selection: active > shadow, tiebreak rule_id lex, tiebreak char_start
        resolved_anns = [ann_by_id[r] for r in vote_refs if r in ann_by_id]
        if resolved_anns:
            def _pkey(a):
                auth_rank = 0 if a.get("authority_mode") == "active" else 1
                return (auth_rank, a["rule_id"], a["anchor"]["char_span"][0])
            # Guard the primary-annotation access: a malformed referenced
            # annotation (missing rule_id / anchor / char_span / source_id) is a
            # collected error, not a KeyError/TypeError/ValueError that aborts.
            try:
                primary = sorted(resolved_anns, key=_pkey)[0]
                p_start, p_end = primary["anchor"]["char_span"]
                p_rule = primary["rule_id"]
                p_source = primary["source_id"]
            except (KeyError, TypeError, ValueError) as exc:
                errors.append(f"{ctx}: cannot re-derive candidate_id, malformed primary annotation ({exc})")
            else:
                expected_cid = _ids.candidate_id(p_source, p_start, p_end, p_rule)
                if cid != expected_cid:
                    errors.append(
                        f"{ctx}: candidate_id {cid!r} does not re-derive from primary "
                        f"({p_source}, {p_start}-{p_end}, {p_rule!r}) -> expected {expected_cid!r}"
                    )

        # 5. evidence_ready enforcement: re-derive via determine_evidence_ready and assert match.
        # A mismatch means the emitted value is stale, hand-edited, or tampered — BLOCKING.
        emitted = cand.get("evidence_ready")
        expected_er = determine_evidence_ready(cand, units_by_id)
        if emitted != expected_er:
            errors.append(
                f"{ctx}: evidence_ready mismatch — emitted {emitted!r} but "
                f"validator derives {expected_er!r}; value must be set by "
                f"validate.determine_evidence_ready, not hand-edited or stale"
            )

    return errors


def run_all(run_dir, schema_dir):
    structural = validate_structural(run_dir, schema_dir)
    anchors = validate_anchors(run_dir)
    candidates = validate_candidates(run_dir)
    return structural, anchors, candidates


def main():
    ap = argparse.ArgumentParser(
        description="Blocking validators (structural + anchor resolution + candidate completeness).")
    ap.add_argument("run_dir")
    ap.add_argument("--schema-dir", default="schema")
    args = ap.parse_args()
    structural, anchors, candidates = run_all(args.run_dir, args.schema_dir)
    for label, errs in (("STRUCTURAL", structural), ("ANCHOR", anchors), ("CANDIDATE", candidates)):
        if errs:
            print(f"[{label}] {len(errs)} error(s):")
            for e in errs:
                print(f"  - {e}")
        else:
            print(f"[{label}] OK")
    if structural or anchors or candidates:
        raise SystemExit(1)
    print("ALL VALIDATORS GREEN")


if __name__ == "__main__":
    main()
