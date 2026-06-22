"""candidates.py - Stage 3: dedup + cluster + route + cap -> candidate case files.

Reads from runs/<run_id>/:
  - annotations.jsonl   (8 annotations from stage 2)
  - units.jsonl         (6 units with text + segmentation_quality + timecode)
  - quality_profile.json (attribution tier)

Writes:
  - candidate_case_files.jsonl  one candidate per maximal overlapping cluster within a family

Dedup rule: group by source_id, then within source cluster annotations of the SAME
cue family (label_vote) whose char_spans overlap or touch. Maximal overlapping run =
one cluster = one candidate. Different families are NEVER merged.

Primary annotation selection (deterministic):
  1. authority_mode=="active" (rule_vote) beats shadow
  2. tiebreak: lowest rule_id lexicographically
  3. further tiebreak: lowest char_start

Routing:
  - Count ACTIVE votes in cluster only (shadow does not elevate)
  - high   >= 2 active concurring votes
  - medium =  1 active vote
  - low      0 active votes (shadow-only)

Adaptive caps (per family, this run's volume):
  - more than 200 routed candidates -> tighten: keep the top 100 (ranked by bucket then span order), drop rest
  - family count < 10  -> widen: keep all
  - Shadow-only candidates excluded from routed caps; their kept=true always

evidence_ready logic (owned by validate.determine_evidence_ready — single source of truth):
  - true if anchor resolves + context present + attribution confirmed-or-N/A
  - "blocked:<reason>" otherwise
  Tier C/D attribution -> N/A (not a blocker per GAP-3).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow running from repo root or spine/
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import ids as _ids
from validate import determine_evidence_ready


def _load_jsonl(path: Path):
    rows = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def _spans_overlap_or_touch(a_start, a_end, b_start, b_end):
    """Two spans overlap or touch if neither is strictly before the other."""
    return a_start <= b_end and b_start <= a_end


def _cluster_annotations(annotations):
    """
    Group by (source_id, label_vote), then cluster overlapping/touching spans
    into maximal runs. Returns list of clusters, each cluster = list of annotations.
    """
    # Group by (source_id, family)
    groups: dict[tuple, list] = {}
    for ann in annotations:
        key = (ann["source_id"], ann["label_vote"])
        groups.setdefault(key, []).append(ann)

    clusters = []
    for (sid, family), anns in groups.items():
        # Sort by char_start so we can do a single linear sweep
        sorted_anns = sorted(anns, key=lambda a: (a["anchor"]["char_span"][0],
                                                   a["anchor"]["char_span"][1]))
        # Greedy maximal-overlap clustering
        current_cluster = [sorted_anns[0]]
        cluster_start = sorted_anns[0]["anchor"]["char_span"][0]
        cluster_end   = sorted_anns[0]["anchor"]["char_span"][1]

        for ann in sorted_anns[1:]:
            a_start, a_end = ann["anchor"]["char_span"]
            if _spans_overlap_or_touch(cluster_start, cluster_end, a_start, a_end):
                current_cluster.append(ann)
                cluster_start = min(cluster_start, a_start)
                cluster_end   = max(cluster_end,   a_end)
            else:
                clusters.append(current_cluster)
                current_cluster = [ann]
                cluster_start, cluster_end = a_start, a_end
        clusters.append(current_cluster)

    return clusters


def _pick_primary(cluster):
    """Deterministic primary selection: active > shadow; tiebreak rule_id lex; tiebreak char_start."""
    def sort_key(ann):
        auth_rank = 0 if ann["authority_mode"] == "active" else 1
        return (auth_rank, ann["rule_id"], ann["anchor"]["char_span"][0])
    return sorted(cluster, key=sort_key)[0]


def _count_active(cluster):
    return sum(1 for a in cluster if a["authority_mode"] == "active")


def _compute_context_window(primary_ann, units_by_source):
    """Count neighbor units within same source before/after this unit. Default 2/2, clamped."""
    unit_ref = primary_ann["unit_ref"]
    source_id = primary_ann["source_id"]
    source_units = units_by_source.get(source_id, [])
    # Units are ordered by char_start
    unit_list = sorted(source_units, key=lambda u: u["anchor"]["char_span"][0])
    unit_ids = [u["unit_id"] for u in unit_list]
    try:
        idx = unit_ids.index(unit_ref)
    except ValueError:
        return {"sentences_before": 0, "sentences_after": 0}
    before = min(2, idx)
    after  = min(2, len(unit_ids) - 1 - idx)
    return {"sentences_before": before, "sentences_after": after}


def _apply_caps(family_candidates):
    """
    Adaptive caps based on this run's routed-candidate count per family.
    Shadow-only candidates are excluded from routed caps (kept=true, rank computed separately).

    Returns list of (candidate, rank_in_family, kept) tuples.
    """
    routed = [c for c in family_candidates if c["_authority_level"] == "rule_vote"]
    shadow = [c for c in family_candidates if c["_authority_level"] != "rule_vote"]

    n_routed = len(routed)

    # Determine keep-k for routed candidates
    if n_routed > 200:
        keep_k = 100  # tighten: when a family has more than 200 routed candidates, keep the top 100
    else:
        keep_k = n_routed  # < 10 or <=200: keep all

    # Rank routed by bucket (high first) then char_start
    bucket_order = {"high": 0, "medium": 1, "low": 2}
    routed_sorted = sorted(routed, key=lambda c: (
        bucket_order.get(c["_bucket"], 9),
        c["_primary_char_start"]
    ))

    result = []
    for rank, cand in enumerate(routed_sorted, start=1):
        kept = rank <= keep_k
        result.append((cand, rank, kept))

    # Shadow candidates: rank separately, always kept=true
    for rank, cand in enumerate(shadow, start=1):
        result.append((cand, rank, True))

    return result, n_routed, keep_k


def build_candidates(run_dir: str):
    run_dir = Path(run_dir)

    annotations_path = run_dir / "annotations.jsonl"
    if not annotations_path.exists():
        raise FileNotFoundError(
            f"annotations.jsonl not found in {run_dir}; run Stage 2 (annotate) first.")
    units_path = run_dir / "units.jsonl"
    if not units_path.exists():
        raise FileNotFoundError(
            f"units.jsonl not found in {run_dir}; run Stage 1 (segment) first.")
    quality_path = run_dir / "quality_profile.json"
    if not quality_path.exists():
        raise FileNotFoundError(
            f"quality_profile.json not found in {run_dir}; run Stage 1 (segment) first.")

    annotations = _load_jsonl(annotations_path)
    units_list  = _load_jsonl(units_path)
    quality     = json.loads(quality_path.read_text(encoding="utf-8"))

    attribution_tier = quality.get("attribution_tier", "D")
    run_id = quality.get("run_id", "")

    # Build unit lookup structures
    units_by_id = {u["unit_id"]: u for u in units_list}
    units_by_source: dict[str, list] = {}
    for u in units_list:
        units_by_source.setdefault(u["source_id"], []).append(u)

    # Cluster annotations
    clusters = _cluster_annotations(annotations)

    # Build candidate records (intermediate, with private _* fields for cap logic)
    intermediates = []
    for cluster in clusters:
        primary = _pick_primary(cluster)
        sid       = primary["source_id"]
        p_start, p_end = primary["anchor"]["char_span"]
        rule_id   = primary["rule_id"]
        family    = primary["label_vote"]

        cand_id = _ids.candidate_id(sid, p_start, p_end, rule_id)

        n_active = _count_active(cluster)
        has_active = any(a["authority_mode"] == "active" for a in cluster)
        authority_level = "rule_vote" if has_active else "shadow"

        # Bucket: count ACTIVE votes only
        if n_active >= 2:
            bucket = "high"
        elif n_active == 1:
            bucket = "medium"
        else:
            bucket = "low"

        # Routing reason
        if authority_level == "rule_vote":
            routing_reason = f"{n_active} active {family} vote{'s' if n_active != 1 else ''}"
        else:
            routing_reason = "shadow-only observational; excluded from routed caps + ablation"

        # vote_refs: sorted annotation_ids in cluster
        vote_refs = sorted(a["annotation_id"] for a in cluster)

        # Anchor from primary
        timecode_ref = primary["anchor"]["timecode_ref"]
        unit_ref = primary["unit_ref"]

        # Context window
        context_window = _compute_context_window(primary, units_by_source)

        # Segmentation quality from primary's unit
        unit = units_by_id.get(unit_ref)
        seg_quality = (unit.get("segmentation_quality") if unit
                       else quality.get("segmentation_quality"))

        # Attribution: tier from quality_profile; speaker unknown at this stage
        # Tier C/D -> N/A for evidence_ready (GAP-3), but store actual tier
        attribution = {"tier": attribution_tier, "speaker": ""}

        # Build the candidate dict first (without evidence_ready) so we can pass it
        # to determine_evidence_ready, which reads anchor.unit_ref and context_window.
        candidate_record = {
            "candidate_id": cand_id,
            "source_id": sid,
            "run_id": run_id,
            "anchor": {
                "char_span": [p_start, p_end],
                "timecode": timecode_ref,
                "unit_ref": unit_ref,
            },
            "context_window": context_window,
            "segmentation_quality": seg_quality,
            "vote_refs": vote_refs,
            "method": {"stage": "candidates", "dedup": "span_overlap_same_family"},
            "routing": {"bucket": bucket, "reason": routing_reason},
            "attribution": attribution,
            "authority_level": authority_level,
            "review_state": "candidate_assembled",
            # Private intermediates for cap logic
            "_family": family,
            "_authority_level": authority_level,
            "_bucket": bucket,
            "_primary_char_start": p_start,
        }

        # evidence_ready: OWNED by validate.determine_evidence_ready (single source of truth).
        # candidates.py defers entirely — no independent rule here.
        candidate_record["evidence_ready"] = determine_evidence_ready(
            candidate_record, units_by_id
        )

        intermediates.append(candidate_record)

    # Apply per-family caps
    by_family: dict[str, list] = {}
    for c in intermediates:
        by_family.setdefault(c["_family"], []).append(c)

    cap_log = {}
    final_candidates = []

    for family, fam_cands in sorted(by_family.items()):
        ranked, n_routed, keep_k = _apply_caps(fam_cands)
        cap_log[family] = {"total": len(fam_cands), "routed": n_routed, "keep_k": keep_k}
        for (cand, rank, kept) in ranked:
            # Strip private fields, attach caps_applied
            out = {k: v for k, v in cand.items() if not k.startswith("_")}
            out["caps_applied"] = {
                "family": family,
                "rank_in_family": rank,
                "kept": kept,
            }
            final_candidates.append(out)

    # Write output
    out_path = run_dir / "candidate_case_files.jsonl"
    with out_path.open("w", encoding="utf-8") as fh:
        for c in final_candidates:
            fh.write(json.dumps(c, ensure_ascii=False) + "\n")

    # Summary
    n_routed_total  = sum(1 for c in final_candidates if c["authority_level"] == "rule_vote")
    n_shadow_total  = sum(1 for c in final_candidates if c["authority_level"] == "shadow")
    return {
        "candidates": len(final_candidates),
        "routed": n_routed_total,
        "shadow": n_shadow_total,
        "per_family_caps": cap_log,
        "out": str(out_path),
    }


def main():
    ap = argparse.ArgumentParser(
        description="Stage 3 candidates: dedup + cluster + route + cap -> candidate_case_files.jsonl")
    ap.add_argument("run_dir", help="Path to run directory (e.g. runs/run_cp2check)")
    args = ap.parse_args()

    result = build_candidates(args.run_dir)

    print(f"Candidates: {result['candidates']}  "
          f"(routed={result['routed']}, shadow={result['shadow']})")
    print("Per-family caps:")
    for fam, info in sorted(result["per_family_caps"].items()):
        print(f"  {fam}: total={info['total']}, routed={info['routed']}, keep_k={info['keep_k']}")
    print(f"Output: {result['out']}")


if __name__ == "__main__":
    main()
