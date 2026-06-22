"""stage4_adjudicate.py - strong-model adjudication of the routed candidates.

Re-judges every routed candidate on a STRONG model (default claude-sonnet-4-6) using the
SAME pack builder and prompt as the base cheap lane, so the adjudicator sees the identical
pack. It writes to a SEPARATE file (adjudication_votes.jsonl) and never touches the base
llm_votes.jsonl. It then compares the strong-model verdict against the base verdict per
candidate and reports agreement, flips, and a confusion matrix.

The adjudicator is blind to the base verdict: it makes a fresh judgment on the same pack,
and the base verdict is never shown to it. Same guardrails as the base run: one call per
item, zero retries, no batching, no prompt change.

CLI:
  python spine/stage4_adjudicate.py --run-root runs/demo [--lane claude]
                                    [--model claude-sonnet-4-6] [--timeout 120] [--execute]

Dry-run default: report how many candidates would be adjudicated; no calls.
Use --lane claude (the default) for the subscription CLI (no metered spend), or
--lane anthropic for a direct API key.
"""
from __future__ import annotations
import argparse
import json
import pathlib
import re
import shutil
import sys
import tempfile
from collections import defaultdict

_SPINE_DIR = pathlib.Path(__file__).resolve().parent
if str(_SPINE_DIR) not in sys.path:
    sys.path.insert(0, str(_SPINE_DIR))

import stage4_eval as s4  # reuse pack builder, parser, validator, lane fns


def main():
    ap = argparse.ArgumentParser(description="Strong-model adjudication of routed candidates.")
    ap.add_argument("--run-root", required=True)
    ap.add_argument("--model", default="claude-sonnet-4-6",
                    help="Strong adjudicator model (default claude-sonnet-4-6).")
    ap.add_argument("--lane", choices=["claude", "anthropic"], default="claude",
                    help="Backend for the strong model: claude (subscription CLI, no metered "
                         "spend) or anthropic (direct Messages API, key from env). Default claude.")
    ap.add_argument("--timeout", type=int, default=120)
    ap.add_argument("--execute", action="store_true")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    run_root = pathlib.Path(args.run_root).resolve()
    if not run_root.exists():
        print(f"ERROR: run-root does not exist: {run_root}", file=sys.stderr)
        sys.exit(1)

    # Base verdicts (non-control) for comparison, keyed by candidate_ref.
    votes_path = run_root / "llm_votes.jsonl"
    if not votes_path.exists():
        print(f"ERROR: no base votes at {votes_path}. Run stage4_eval first.", file=sys.stderr)
        sys.exit(1)
    base_votes = [v for v in s4._load_jsonl(votes_path) if not v["is_control"]]
    base = {v["candidate_ref"]: v["vote"]["verdict"] for v in base_votes}
    # Read the actual base model from the base votes (fall back to the conventional string).
    base_model = next((v.get("model") for v in base_votes if v.get("model")),
                      "claude-haiku-4-5")

    # Adjudicate every routed candidate (high + medium). Identical packs to the base run.
    packs = s4.load_routed_candidates(run_root)
    print(f"routed candidates to adjudicate: {len(packs)}  (base verdicts available: "
          f"{sum(1 for p in packs if p['candidate_ref'] in base)})")

    out_path = run_root / "adjudication_votes.jsonl"
    report_path = run_root / "adjudication_report.json"

    # Resume: skip refs already adjudicated with this model. Compare on the model id with any
    # trailing dated suffix (e.g. -20251001) stripped, so a vendor-returned dated id matches
    # the bare --model string.
    def _undated(m: str) -> str:
        return re.sub(r"-\d{8}$", "", m or "")

    done = set()
    if out_path.exists():
        for v in s4._load_jsonl(out_path):
            if _undated(v.get("model", "")) == _undated(args.model):
                done.add(v["candidate_ref"])
    if done:
        print(f"Resume: {len(done)} already adjudicated with {args.model}, skipping those.")

    if not args.execute:
        print(f"\nDRY-RUN: would adjudicate {len(packs) - len(done)} candidates on "
              f"{args.model} via the {args.lane} lane. Pass --execute to run.")
        return

    cfg = s4.LANE_CONFIGS[args.lane]
    fn = cfg["fn"]
    kwargs = cfg.get("kwargs", {})
    run_short = s4._ids.run_short(run_root.name)
    call_cwd = tempfile.mkdtemp(prefix="adjudicate_isolated_")

    adjudicated = [v for v in s4._load_jsonl(out_path)] if out_path.exists() else []
    seq = len(adjudicated)
    out_fh = open(out_path, "a", encoding="utf-8")
    try:
        for i, pack in enumerate(packs):
            if pack["candidate_ref"] in done:
                continue
            try:
                raw, norm, used_model, rc = fn(
                    pack["prompt"], args.model, cfg["effort"], cfg["sandbox"],
                    timeout=args.timeout, cwd=call_cwd, **kwargs)
            except Exception as exc:
                print(f"  LANE_FAIL {pack['candidate_ref']}: {exc}", file=sys.stderr)
                continue
            if rc != 0 or not (raw or "").strip():
                print(f"  LANE_FAIL {pack['candidate_ref']}: rc={rc} empty={not (raw or '').strip()}",
                      file=sys.stderr)
                continue
            parsed, perr = s4.parse_response(raw)
            if parsed is None:
                print(f"  MALFORMED {pack['candidate_ref']}: {perr}", file=sys.stderr)
                continue
            rec = s4.build_vote(pack, parsed, used_model, norm, seq, run_short, args.lane)
            rec["tier"] = "escalation"  # strong-model second pass = escalation (vote_tier enum)
            errs = s4.validate_vote(rec)
            if errs:
                print(f"  SCHEMA_INVALID {pack['candidate_ref']}: {errs}", file=sys.stderr)
                continue
            seq += 1
            out_fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            out_fh.flush()
            adjudicated.append(rec)
            if (i + 1) % 10 == 0 or (i + 1) == len(packs):
                print(f"  [{args.model}] {i + 1}/{len(packs)} processed", flush=True)
    finally:
        out_fh.close()
        shutil.rmtree(call_cwd, ignore_errors=True)

    # ── Comparison report (strong vs base) ──────────────────────────────────────
    strong = {v["candidate_ref"]: v["vote"]["verdict"] for v in adjudicated}
    pairs = [(base[r], strong[r]) for r in strong if r in base]
    n = len(pairs)
    agree = sum(1 for a, b in pairs if a == b)
    flips = [{"candidate_ref": r, "base": base[r], args.model: strong[r]}
             for r in strong if r in base and base[r] != strong[r]]
    confusion = defaultdict(int)
    for a, b in pairs:
        confusion[f"base_{a}__strong_{b}"] += 1

    report = {
        "adjudicator_model": args.model,
        "adjudicator_lane": args.lane,
        "base_model": base_model,
        "n_compared": n,
        "agree": agree,
        "agreement_rate": (agree / n if n else None),
        "n_flips": len(flips),
        "flips": flips,
        "confusion": dict(confusion),
    }
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"\nWrote {out_path}\nWrote {report_path}")
    print(json.dumps({k: v for k, v in report.items() if k != "flips"}, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
