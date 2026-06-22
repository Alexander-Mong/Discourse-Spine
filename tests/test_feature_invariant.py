"""test_feature_invariant.py - Stage 2.5 additive-invariant regression tests.

Guarantees:
  1. STANDOFF: enrich --execute must leave units.jsonl, annotations.jsonl, and
     candidate_case_files.jsonl BYTE-IDENTICAL (same SHA-256 before and after).
  2. VALIDITY: every record in feature_records.jsonl validates against
     feature_envelope.schema.json.
  3. UNIT-REF RESOLUTION: every unit_ref in feature_records.jsonl resolves to a
     real unit_id in units.jsonl.
  4. NON-GATING: no feature record contains any of the keys:
     routing, evidence_ready, bucket, rank.
  5. PRESENCE: feature_records.jsonl is created.
  6. SCHEMA PATTERN: every feature_record_id matches ^feat_[0-9a-f]{8}$.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

from conftest import load_jsonl

REPO = Path(__file__).resolve().parents[1]
SPINE = REPO / "spine"
SCHEMA = REPO / "schema"

sys.path.insert(0, str(SPINE))

_FEAT_ID_PAT = re.compile(r"^feat_[0-9a-f]{8}$")
_GATING_KEYS = {"routing", "evidence_ready", "bucket", "rank"}


def _sha256_file(path: Path) -> str:
    data = path.read_bytes()
    return hashlib.sha256(data).hexdigest()


def _load_feature_schema():
    schema = json.loads((SCHEMA / "feature_envelope.schema.json").read_text(encoding="utf-8"))
    enums = json.loads((SCHEMA / "enums.json").read_text(encoding="utf-8"))
    return schema, enums


def _validate_record(record, schema, enums):
    """Thin wrapper around validate.check; returns list of error strings."""
    import validate as _val
    errors = []
    _val.check(record, schema, "feature_record", enums, errors)
    return errors


# ── main invariant test ───────────────────────────────────────────────────────

def test_enrich_additive_invariant(pipeline_with_candidates):
    """Full additive-invariant regression: run enrich --execute, assert standoff guarantee."""
    import enrich as enrich_mod

    run_dir: Path = pipeline_with_candidates["run_dir"]

    # The fixture run dir has exactly one source dir (sample_source produces one source dir).
    # But the pipeline fixture uses a flat run_dir (units.jsonl directly in run_dir).
    # enrich.py expects source dirs INSIDE run_root; adapt by using run_dir.parent as run_root
    # and confirming run_dir itself is the one source dir.
    #
    # Actually: the fixture writes units.jsonl directly into run_dir (e.g. tmp/runs0/run_fixture/).
    # enrich_run scans for subdirs of run_root containing units.jsonl.
    # Since run_dir IS the leaf (no subdirs with units.jsonl), we pass run_dir.parent as run_root.
    # Verify that run_dir has units.jsonl before proceeding.

    assert (run_dir / "units.jsonl").exists(), "Fixture must have units.jsonl"
    assert (run_dir / "annotations.jsonl").exists(), "Fixture must have annotations.jsonl"
    assert (run_dir / "candidate_case_files.jsonl").exists(), "Fixture must have candidate_case_files.jsonl"

    # Snapshot hashes BEFORE enrich
    hash_units_before = _sha256_file(run_dir / "units.jsonl")
    hash_ann_before = _sha256_file(run_dir / "annotations.jsonl")
    hash_cands_before = _sha256_file(run_dir / "candidate_case_files.jsonl")

    # Run enrich --execute.  run_root is the parent; run_dir is the source dir.
    run_root = run_dir.parent
    result = enrich_mod.enrich_run(str(run_root), execute=True)

    # ── (1) STANDOFF: byte-identical hashes ──────────────────────────────────
    hash_units_after = _sha256_file(run_dir / "units.jsonl")
    hash_ann_after = _sha256_file(run_dir / "annotations.jsonl")
    hash_cands_after = _sha256_file(run_dir / "candidate_case_files.jsonl")

    assert hash_units_before == hash_units_after, (
        "units.jsonl was mutated by enrich — standoff guarantee violated"
    )
    assert hash_ann_before == hash_ann_after, (
        "annotations.jsonl was mutated by enrich — standoff guarantee violated"
    )
    assert hash_cands_before == hash_cands_after, (
        "candidate_case_files.jsonl was mutated by enrich — standoff guarantee violated"
    )

    # ── (2) PRESENCE: feature_records.jsonl created ───────────────────────────
    feat_path = run_dir / "feature_records.jsonl"
    assert feat_path.exists(), "feature_records.jsonl was not created by enrich --execute"

    records = load_jsonl(feat_path)
    assert len(records) > 0, "feature_records.jsonl is empty"

    # ── (3) VALIDITY: every record validates against the schema ───────────────
    schema, enums = _load_feature_schema()
    for i, rec in enumerate(records):
        errs = _validate_record(rec, schema, enums)
        assert not errs, f"feature_records.jsonl[{i}] validation errors: {errs}"

    # ── (4) SCHEMA PATTERN: every feature_record_id matches ^feat_[0-9a-f]{8}$ ──
    for i, rec in enumerate(records):
        assert _FEAT_ID_PAT.match(rec["feature_record_id"]), (
            f"feature_records.jsonl[{i}]: feature_record_id {rec['feature_record_id']!r} "
            f"does not match ^feat_[0-9a-f]{{8}}$"
        )

    # ── (5) UNIT-REF RESOLUTION: every unit_ref resolves to a real unit ───────
    units = load_jsonl(run_dir / "units.jsonl")
    unit_ids = {u["unit_id"] for u in units}
    for i, rec in enumerate(records):
        assert rec["unit_ref"] in unit_ids, (
            f"feature_records.jsonl[{i}]: unit_ref {rec['unit_ref']!r} "
            f"does not resolve to any unit_id in units.jsonl"
        )

    # ── (6) NON-GATING: no gating keys present ───────────────────────────────
    for i, rec in enumerate(records):
        found_gating = _GATING_KEYS & set(rec.keys())
        assert not found_gating, (
            f"feature_records.jsonl[{i}]: gating key(s) {found_gating} found in feature record — "
            f"non-gating guarantee violated"
        )
        # Also check payload (belt-and-suspenders)
        payload_keys = _GATING_KEYS & set(rec.get("payload", {}).keys())
        assert not payload_keys, (
            f"feature_records.jsonl[{i}]: gating key(s) {payload_keys} found in payload"
        )


# ── feature-count and structure spot-checks ───────────────────────────────────

def test_surface_complexity_one_per_unit(pipeline_with_candidates):
    """surface_complexity_profile must emit exactly one record per unit."""
    import enrich as enrich_mod

    run_dir: Path = pipeline_with_candidates["run_dir"]
    run_root = run_dir.parent
    enrich_mod.enrich_run(str(run_root), execute=True)

    records = load_jsonl(run_dir / "feature_records.jsonl")
    scp_records = [r for r in records if r["method"]["name"] == "surface_complexity_profile"]

    units = pipeline_with_candidates["units"]
    assert len(scp_records) == len(units), (
        f"Expected one surface_complexity_profile per unit ({len(units)}), got {len(scp_records)}"
    )


def test_surface_complexity_suppression(pipeline_with_candidates):
    """Short units (< 5 tokens) must be suppressed with triggered=False."""
    import enrich as enrich_mod

    run_dir: Path = pipeline_with_candidates["run_dir"]
    run_root = run_dir.parent
    enrich_mod.enrich_run(str(run_root), execute=True)

    records = load_jsonl(run_dir / "feature_records.jsonl")
    scp_records = {r["unit_ref"]: r for r in records if r["method"]["name"] == "surface_complexity_profile"}

    units = pipeline_with_candidates["units"]
    for u in units:
        n_tokens = len(u["text"].split())
        seg_q = u.get("segmentation_quality", "")
        rec = scp_records[u["unit_id"]]
        if n_tokens < 5 or seg_q in {"unpunctuated", "window_mode"}:
            assert rec["triggered"] is False, (
                f"Unit {u['unit_id']!r} (tokens={n_tokens}, seg={seg_q!r}) "
                f"should be suppressed but triggered=True"
            )
            assert rec["payload"]["suppressed"] is True
        else:
            assert rec["triggered"] is True, (
                f"Unit {u['unit_id']!r} (tokens={n_tokens}, seg={seg_q!r}) "
                f"should NOT be suppressed but triggered=False"
            )
            assert "length_tokens" in rec["payload"]
            assert "length_chars" in rec["payload"]
            assert "lexical_density" in rec["payload"]


def test_discourse_marker_only_on_hit(pipeline_with_candidates):
    """discourse_marker_profile records must only appear when triggered=True."""
    import enrich as enrich_mod

    run_dir: Path = pipeline_with_candidates["run_dir"]
    run_root = run_dir.parent
    enrich_mod.enrich_run(str(run_root), execute=True)

    records = load_jsonl(run_dir / "feature_records.jsonl")
    dmp_records = [r for r in records if r["method"]["name"] == "discourse_marker_profile"]

    for rec in dmp_records:
        assert rec["triggered"] is True, (
            f"discourse_marker_profile record for {rec['unit_ref']!r} has triggered=False — "
            "should only emit on a hit"
        )
        assert len(rec["payload"]["markers"]) >= 1
        assert len(rec["payload"]["categories_present"]) >= 1


def test_deictic_only_on_hit(pipeline_with_candidates):
    """possible_visual_or_deictic_dependence records must only appear when triggered=True."""
    import enrich as enrich_mod

    run_dir: Path = pipeline_with_candidates["run_dir"]
    run_root = run_dir.parent
    enrich_mod.enrich_run(str(run_root), execute=True)

    records = load_jsonl(run_dir / "feature_records.jsonl")
    pvd_records = [r for r in records if r["method"]["name"] == "possible_visual_or_deictic_dependence"]

    for rec in pvd_records:
        assert rec["triggered"] is True
        assert len(rec["payload"]["phrases"]) >= 1
        assert rec["payload"]["confidence"] == "high"


def test_feature_record_id_deterministic(pipeline_with_candidates):
    """feature_record_id must be deterministic: running enrich twice gives same IDs."""
    import enrich as enrich_mod

    run_dir: Path = pipeline_with_candidates["run_dir"]
    run_root = run_dir.parent
    enrich_mod.enrich_run(str(run_root), execute=True)

    records_1 = load_jsonl(run_dir / "feature_records.jsonl")
    ids_1 = {r["feature_record_id"] for r in records_1}

    enrich_mod.enrich_run(str(run_root), execute=True)
    records_2 = load_jsonl(run_dir / "feature_records.jsonl")
    ids_2 = {r["feature_record_id"] for r in records_2}

    assert ids_1 == ids_2, "feature_record_id is not deterministic across reruns"


def test_enrich_dry_run_writes_nothing(pipeline_with_candidates):
    """Dry-run must not write feature_records.jsonl."""
    import enrich as enrich_mod

    run_dir: Path = pipeline_with_candidates["run_dir"]
    # Remove any existing feature_records.jsonl first
    feat_path = run_dir / "feature_records.jsonl"
    if feat_path.exists():
        feat_path.unlink()

    run_root = run_dir.parent
    enrich_mod.enrich_run(str(run_root), execute=False)

    assert not feat_path.exists(), (
        "Dry-run created feature_records.jsonl — it must write nothing"
    )


def test_method_stage_is_enrich(pipeline_with_candidates):
    """Every feature record must have method.stage == 'enrich'."""
    import enrich as enrich_mod

    run_dir: Path = pipeline_with_candidates["run_dir"]
    run_root = run_dir.parent
    enrich_mod.enrich_run(str(run_root), execute=True)

    records = load_jsonl(run_dir / "feature_records.jsonl")
    for i, rec in enumerate(records):
        assert rec["method"]["stage"] == "enrich", (
            f"Record {i}: method.stage={rec['method']['stage']!r}, expected 'enrich'"
        )
