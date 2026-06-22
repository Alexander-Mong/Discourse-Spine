"""CP3 candidate tests: dedup determinism, lane/bucket/count assertions,
candidate-completeness validator, candidate_id round-trip, golden master."""
import json
import sys
from pathlib import Path

from conftest import scrub_run_id

REPO = Path(__file__).resolve().parents[1]
SPINE = REPO / "spine"
SCHEMA = REPO / "schema"
GOLDEN = REPO / "tests" / "fixtures" / "sample_source" / "expected"

sys.path.insert(0, str(SPINE))


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _by_family(candidates):
    out = {}
    for c in candidates:
        out.setdefault(c["caps_applied"]["family"], []).append(c)
    return out


# ---------------------------------------------------------------------------
# Dedup determinism: running build_candidates twice produces identical IDs
# ---------------------------------------------------------------------------

def test_dedup_determinism(pipeline_with_candidates):
    """Re-running candidates on the same run_dir yields identical candidate_ids."""
    import candidates as cands_mod

    run_dir = pipeline_with_candidates["run_dir"]
    first_run = [c["candidate_id"] for c in pipeline_with_candidates["candidates"]]

    # Run again
    cands_mod.build_candidates(str(run_dir))

    def _jsonl(name):
        p = run_dir / name
        return [json.loads(l) for l in p.read_text("utf-8").splitlines() if l.strip()]

    second_run = [c["candidate_id"] for c in _jsonl("candidate_case_files.jsonl")]
    assert first_run == second_run, (
        f"Dedup not deterministic: first={first_run}, second={second_run}"
    )


# ---------------------------------------------------------------------------
# 5-candidate count + lane/bucket assertions
# ---------------------------------------------------------------------------

def test_candidate_count_is_five(pipeline_with_candidates):
    assert len(pipeline_with_candidates["candidates"]) == 5, (
        f"Expected 5 candidates, got {len(pipeline_with_candidates['candidates'])}"
    )


def test_routed_pool_is_three(pipeline_with_candidates):
    # Routed = advice (advice_001b) + question (question_003) + reframe (reframe_001) = 3,
    # i.e. every candidate whose authority_level is rule_vote.
    routed = [c for c in pipeline_with_candidates["candidates"]
              if c["authority_level"] == "rule_vote"]
    assert len(routed) == 3, f"Expected 3 routed candidates, got {len(routed)}"


def test_shadow_lane_is_two(pipeline_with_candidates):
    # Shadow lane = caveat + example = 2 (reframe routes to the rule_vote lane, not shadow).
    shadow = [c for c in pipeline_with_candidates["candidates"]
              if c["authority_level"] == "shadow"]
    assert len(shadow) == 2, f"Expected 2 shadow candidates, got {len(shadow)}"


def test_advice_candidate_routed_medium(pipeline_with_candidates):
    by_fam = _by_family(pipeline_with_candidates["candidates"])
    advice = by_fam["advice"]
    assert len(advice) == 1
    c = advice[0]
    assert c["authority_level"] == "rule_vote"
    assert c["routing"]["bucket"] == "medium"
    # advice fires as advice_001b, whose content-addressed id is cand_6f9a3ab9.
    assert c["candidate_id"] == "cand_6f9a3ab9"
    assert len(c["vote_refs"]) == 1


def test_question_candidate_routed_medium(pipeline_with_candidates):
    by_fam = _by_family(pipeline_with_candidates["candidates"])
    question = by_fam["question"]
    assert len(question) == 1
    c = question[0]
    assert c["authority_level"] == "rule_vote"
    assert c["routing"]["bucket"] == "medium"
    assert c["candidate_id"] == "cand_a8e990e7"
    # question cluster has question_002 (shadow) + question_003 (active) -> 2 vote_refs
    assert len(c["vote_refs"]) == 2


def test_reframe_candidate_routed_high(pipeline_with_candidates):
    # reframe_001 + reframe_v2a both fire active -> 2 active votes -> bucket=high.
    by_fam = _by_family(pipeline_with_candidates["candidates"])
    reframe = by_fam["reframe"]
    assert len(reframe) == 1
    c = reframe[0]
    assert c["authority_level"] == "rule_vote"  # reframe is active, so it routes as a rule_vote
    assert c["routing"]["bucket"] == "high"      # 2 active votes route to the high bucket
    assert c["candidate_id"] == "cand_fdcaa2e0"  # content-addressed on (span + reframe_001)
    assert len(c["vote_refs"]) == 2


def test_caveat_candidate_shadow_low(pipeline_with_candidates):
    by_fam = _by_family(pipeline_with_candidates["candidates"])
    caveat = by_fam["caveat"]
    assert len(caveat) == 1
    c = caveat[0]
    assert c["authority_level"] == "shadow"
    assert c["routing"]["bucket"] == "low"
    assert c["candidate_id"] == "cand_6f68bc50"
    assert len(c["vote_refs"]) == 2


def test_example_candidate_shadow_low(pipeline_with_candidates):
    by_fam = _by_family(pipeline_with_candidates["candidates"])
    example = by_fam["example"]
    assert len(example) == 1
    c = example[0]
    assert c["authority_level"] == "shadow"
    assert c["routing"]["bucket"] == "low"
    assert c["candidate_id"] == "cand_06b4b35c"
    assert len(c["vote_refs"]) == 1


def test_all_candidates_kept(pipeline_with_candidates):
    """Fixture volume is tiny; all candidates should be kept=true."""
    for c in pipeline_with_candidates["candidates"]:
        assert c["caps_applied"]["kept"] is True, (
            f"{c['candidate_id']}: expected kept=true, got {c['caps_applied']['kept']}"
        )


def test_all_rank_in_family_is_one(pipeline_with_candidates):
    """Each family has exactly 1 candidate in the fixture, so rank_in_family=1 for all."""
    for c in pipeline_with_candidates["candidates"]:
        assert c["caps_applied"]["rank_in_family"] == 1, (
            f"{c['candidate_id']}: rank_in_family={c['caps_applied']['rank_in_family']}"
        )


# ---------------------------------------------------------------------------
# Candidate-completeness validator passes
# ---------------------------------------------------------------------------

def test_candidate_completeness_validator_passes(pipeline_with_candidates):
    import validate
    errors = validate.validate_candidates(str(pipeline_with_candidates["run_dir"]))
    assert errors == [], f"Candidate validator errors: {errors}"


def test_all_validators_green(pipeline_with_candidates):
    import validate
    structural, anchors, candidates = validate.run_all(
        str(pipeline_with_candidates["run_dir"]), str(SCHEMA)
    )
    assert structural == [], f"Structural errors: {structural}"
    assert anchors == [], f"Anchor errors: {anchors}"
    assert candidates == [], f"Candidate errors: {candidates}"


# ---------------------------------------------------------------------------
# candidate_id round-trip: re-derives from primary annotation span + rule
# ---------------------------------------------------------------------------

def test_candidate_id_round_trip(pipeline_with_candidates):
    """For each candidate, re-derive its id from the annotations in vote_refs and confirm match."""
    import ids

    annotations = pipeline_with_candidates["annotations"]
    ann_by_id = {a["annotation_id"]: a for a in annotations}

    for cand in pipeline_with_candidates["candidates"]:
        vote_refs = cand["vote_refs"]
        resolved = [ann_by_id[r] for r in vote_refs if r in ann_by_id]
        assert resolved, f"{cand['candidate_id']}: no vote_refs resolved"

        def _pkey(a):
            auth_rank = 0 if a.get("authority_mode") == "active" else 1
            return (auth_rank, a["rule_id"], a["anchor"]["char_span"][0])

        primary = sorted(resolved, key=_pkey)[0]
        p_start, p_end = primary["anchor"]["char_span"]
        expected = ids.candidate_id(cand["source_id"], p_start, p_end, primary["rule_id"])
        assert cand["candidate_id"] == expected, (
            f"candidate_id mismatch: stored={cand['candidate_id']!r} "
            f"re-derived={expected!r} from ({cand['source_id']}, "
            f"{p_start}-{p_end}, {primary['rule_id']!r})"
        )


# ---------------------------------------------------------------------------
# Golden master
# ---------------------------------------------------------------------------

def test_candidates_golden_master(pipeline_with_candidates):
    """Candidates must match the committed golden (run-ID scrubbed)."""
    run_id = pipeline_with_candidates["meta"]["run_id"]
    current = scrub_run_id(pipeline_with_candidates["candidates"], run_id)

    expected_path = GOLDEN / "expected_outputs.json"
    assert expected_path.exists(), "Golden file missing; run pipeline once and commit it."

    golden = json.loads(expected_path.read_text(encoding="utf-8"))
    assert "candidates" in golden, (
        "Golden file has no 'candidates' key; regenerate expected_outputs.json."
    )
    assert current == golden["candidates"], (
        "Candidate output drifted from committed golden master"
    )


# ---------------------------------------------------------------------------
# Negative enforcement test: mutated evidence_ready triggers validator failure
# ---------------------------------------------------------------------------

def test_evidence_ready_enforcement_gate_bites(pipeline_with_candidates):
    """
    Systemic guarantee: if evidence_ready in candidate_case_files.jsonl is mutated
    to a wrong value (stale, hand-edited, or tampered), validate_candidates MUST
    return at least one error.

    Procedure:
      1. Read the live JSONL from the run_dir used by the session fixture.
      2. Mutate the first candidate's evidence_ready to a sentinel wrong value.
      3. Write the mutated JSONL to a temp file in the same run_dir (overwrite).
      4. Assert validate_candidates reports >=1 error (gate bites).
      5. Restore the original JSONL — always, even if the assert fails.
    """
    import validate
    import tempfile

    run_dir = pipeline_with_candidates["run_dir"]
    cand_path = run_dir / "candidate_case_files.jsonl"

    original_text = cand_path.read_text(encoding="utf-8")
    lines = [l for l in original_text.splitlines() if l.strip()]
    assert lines, "candidate_case_files.jsonl is empty — cannot run enforcement test"

    # Mutate first candidate: flip evidence_ready to a clearly wrong sentinel
    first = json.loads(lines[0])
    original_er = first["evidence_ready"]
    # Use the opposite type to guarantee mismatch regardless of current value
    mutated_er = "blocked:INJECTED_TAMPER_SENTINEL" if original_er is True else True
    first["evidence_ready"] = mutated_er
    mutated_lines = [json.dumps(first, ensure_ascii=False)] + lines[1:]
    mutated_text = "\n".join(mutated_lines) + "\n"

    try:
        cand_path.write_text(mutated_text, encoding="utf-8")
        errors = validate.validate_candidates(str(run_dir))
        assert errors, (
            f"ENFORCEMENT GATE FAILED: validate_candidates returned no errors after "
            f"evidence_ready was mutated from {original_er!r} to {mutated_er!r} on "
            f"candidate {first['candidate_id']!r}. The validator must re-derive and "
            f"reject stale/tampered values."
        )
        # Confirm the error message is specific and identifies the mismatch
        mismatch_errors = [e for e in errors if "evidence_ready mismatch" in e]
        assert mismatch_errors, (
            f"Validator caught an error but not the expected evidence_ready mismatch. "
            f"Got errors: {errors}"
        )
    finally:
        # Always restore — even if asserts above fail
        cand_path.write_text(original_text, encoding="utf-8")
