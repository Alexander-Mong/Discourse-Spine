#!/usr/bin/env python3
"""run_demo.py - Run the deterministic Discourse Spine end-to-end on the bundled
example transcripts.

No API keys, no network, no external binaries. Pure-stdlib pipeline:

    ingest      SRT  ->  char-anchored units + tamper-evident lockfile
    annotate    units  ->  rule firings + term occurrences
    candidates  firings ->  routed evidence candidates
    enrich      run    ->  deterministic feature records (standoff)

Each transcript is written to its own source subdirectory under the run root
(runs/demo/<source_id>/), which is the layout the corpus-level stages expect.

Usage:
    python run_demo.py
    python run_demo.py --examples data/examples --run-root runs/demo
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "spine"))

import ingest as ingest_mod        # noqa: E402
import annotate as annotate_mod    # noqa: E402
import candidates as candidates_mod  # noqa: E402
import enrich as enrich_mod        # noqa: E402

INPUTS = ROOT / "inputs"
REGISTRY = INPUTS / "rule_registry.csv"
TERM_SEEDS = INPUTS / "term_seeds.csv"
GAZETTEER = INPUTS / "gazetteer.csv"  # optional speaker gazetteer; annotate skips it if absent


def _count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open(encoding="utf-8") as fh:
        return sum(1 for line in fh if line.strip())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--examples", default=str(ROOT / "data" / "examples"),
                    help="Directory of example .srt transcripts.")
    ap.add_argument("--run-root", default=str(ROOT / "runs" / "demo"),
                    help="Output run root; each source gets its own subdir here.")
    ap.add_argument("--attribution-tier", default="D", choices=["A", "B", "C", "D"],
                    help="Source attribution tier A/B/C/D (default D).")
    args = ap.parse_args()

    examples_dir = Path(args.examples)
    run_root = Path(args.run_root)
    srts = sorted(examples_dir.glob("*.srt"))
    if not srts:
        print(f"No example .srt files found in {examples_dir}", file=sys.stderr)
        return 1

    for required in (REGISTRY, TERM_SEEDS):
        if not required.exists():
            print(f"Missing required input: {required}", file=sys.stderr)
            return 1

    if GAZETTEER.exists():
        print(f"Gazetteer: found at {GAZETTEER} - will be used for speaker annotation")
    else:
        print(f"Gazetteer: not found at {GAZETTEER} - skipping speaker gazetteer (optional)")

    print(f"Discourse Spine - deterministic demo over {len(srts)} transcript(s)")
    print(f"Run root: {run_root}\n")

    totals = {"units": 0, "annotations": 0, "terms": 0, "candidates": 0, "features": 0}
    for srt in srts:
        source_id = srt.stem
        run_dir = run_root / source_id

        ingest_mod.ingest(str(srt), source_id, source_id,
                          out_root=str(run_root), attribution_tier=args.attribution_tier)
        annotate_mod.annotate(str(run_dir), str(REGISTRY), str(TERM_SEEDS), str(GAZETTEER))
        candidates_mod.build_candidates(str(run_dir))

        counts = {
            "units": _count_lines(run_dir / "units.jsonl"),
            "annotations": _count_lines(run_dir / "annotations.jsonl"),
            "terms": _count_lines(run_dir / "term_occurrences.jsonl"),
            "candidates": _count_lines(run_dir / "candidate_case_files.jsonl"),
        }
        for k, v in counts.items():
            totals[k] += v
        print(f"  {source_id}")
        print(f"    units={counts['units']:>4}  annotations={counts['annotations']:>4}  "
              f"terms={counts['terms']:>4}  candidates={counts['candidates']:>4}")

    # Stage 2.5: deterministic feature enrichment over the whole run root.
    enrich_mod.enrich_run(str(run_root), execute=True)
    feature_total = sum(_count_lines(run_root / s.stem / "feature_records.jsonl") for s in srts)
    totals["features"] = feature_total

    print("\nTotals across corpus:")
    print(f"  units={totals['units']}  annotations={totals['annotations']}  "
          f"terms={totals['terms']}  candidates={totals['candidates']}  "
          f"features={totals['features']}")
    print(f"\nDone. Inspect per-source output under {run_root}/<source_id>/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
