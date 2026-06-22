"""Anchor round-trip + validator gate + ID-determinism + golden-master.

These are the CP2 exit checks. The invariant tests do NOT depend on hand-computed offsets;
they assert the properties the whole traceability story rests on.
"""
import hashlib
import json
import sys
from pathlib import Path

from conftest import scrub_run_id

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "spine"))
GOLDEN = REPO / "tests" / "fixtures" / "sample_source" / "expected"


def test_unit_anchor_roundtrip(pipeline):
    """PRIMARY anchor: transcript[char_span] re-resolves to the unit text, exactly."""
    t = pipeline["transcript"]
    for u in pipeline["units"]:
        s, e = u["anchor"]["char_span"]
        assert t[s:e] == u["text"], f"{u['unit_id']} does not re-resolve at [{s},{e}]"


def test_transcript_hash_matches_lockfile(pipeline):
    actual = "sha256:" + hashlib.sha256(pipeline["transcript"].encode("utf-8")).hexdigest()
    assert actual == pipeline["lockfile"]["transcript"]["sha256"]


def test_annotation_spans_match_units(pipeline):
    spans = {u["unit_id"]: u["anchor"]["char_span"] for u in pipeline["units"]}
    for a in pipeline["annotations"]:
        assert a["anchor"]["char_span"] == spans[a["unit_ref"]]
        s, e = a["anchor"]["char_span"]
        assert a["trigger_text"].lower() in pipeline["transcript"][s:e].lower()


def test_validators_green(pipeline):
    import validate
    structural, anchors, candidates = validate.run_all(
        str(pipeline["run_dir"]), str(pipeline["schema_dir"]))
    assert structural == [], f"structural errors: {structural}"
    assert anchors == [], f"anchor errors: {anchors}"
    # The candidate validator returns [] whether or not candidate_case_files.jsonl exists:
    # it skips silently when the file is absent, and reports no errors when present and valid.
    # Either way the gate is green here, independent of test-execution order. We assert the
    # green result directly rather than asserting the file's absence (which the shared
    # session run_dir does not guarantee once pipeline_with_candidates has run).
    assert candidates == [], f"candidate errors: {candidates}"


def test_candidate_id_is_content_addressed_and_stable(pipeline):
    """Re-deriving an ID from the same (source, span, rule, schema) yields the same id."""
    import ids
    a = pipeline["annotations"][0]
    s, e = a["anchor"]["char_span"]
    c1 = ids.candidate_id(a["source_id"], s, e, a["rule_id"])
    c2 = ids.candidate_id(a["source_id"], s, e, a["rule_id"])
    assert c1 == c2 and c1.startswith("cand_") and len(c1) == len("cand_") + 8


def test_golden_master(pipeline):
    """Approve-on-first-run golden. If expected/ is absent, write it and fail (lock it in a
    commit); thereafter, outputs must match byte-for-byte after run-ID scrub."""
    run_id = pipeline["meta"]["run_id"]
    current = {
        "units": scrub_run_id(pipeline["units"], run_id),
        "annotations": scrub_run_id(pipeline["annotations"], run_id),
        "term_occurrences": scrub_run_id(pipeline["term_occurrences"], run_id),
    }
    expected_path = GOLDEN / "expected_outputs.json"
    if not expected_path.exists():
        GOLDEN.mkdir(parents=True, exist_ok=True)
        expected_path.write_text(json.dumps(current, indent=2, ensure_ascii=False), encoding="utf-8")
        raise AssertionError("Golden master created. Review tests/fixtures/sample_source/expected/ "
                             "and commit it, then re-run to lock.")
    expected = json.loads(expected_path.read_text(encoding="utf-8"))
    # Compare only the keys this test owns (units/annotations/term_occurrences).
    # The candidates key lives in test_candidates.py::test_candidates_golden_master.
    for key in ("units", "annotations", "term_occurrences"):
        assert current[key] == expected[key], (
            f"pipeline output drifted from committed golden master (key: {key!r})"
        )
