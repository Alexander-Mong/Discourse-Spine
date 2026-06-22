"""Shared pytest fixtures: run the CP2 pipeline once over the golden sample into a temp dir."""
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]          # projects/discourse-spine/
SPINE = REPO / "spine"
SCHEMA = REPO / "schema"
INPUTS = REPO / "inputs"
FIXTURE = REPO / "tests" / "fixtures" / "sample_source"

sys.path.insert(0, str(SPINE))


def load_jsonl(path):
    """Load a JSONL file into a list of dicts (blank lines skipped). Shared test helper."""
    path = Path(path)
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def scrub_run_id(rows, run_id):
    """Run-ID scrub so golden masters are reproducible across runs. Shared test helper."""
    return json.loads(json.dumps(rows).replace(run_id, "RUNID"))


@pytest.fixture(scope="session")
def pipeline(tmp_path_factory):
    """Run ingest + annotate over the fixture SRT; return paths + loaded records."""
    import ingest
    import annotate

    meta = json.loads((FIXTURE / "meta.json").read_text(encoding="utf-8"))
    out_root = tmp_path_factory.mktemp("runs")
    ingest.ingest(
        str(FIXTURE / "sample.srt"), meta["source_id"], meta["run_id"],
        out_root=str(out_root), attribution_tier=meta["attribution_tier"],
    )
    run_dir = out_root / meta["run_id"]
    annotate.annotate(
        str(run_dir), str(INPUTS / "rule_registry.csv"),
        str(INPUTS / "term_seeds.csv"), str(INPUTS / "gazetteer.csv"),
    )

    return {
        "run_dir": run_dir, "schema_dir": SCHEMA, "meta": meta,
        "transcript": (run_dir / "normalized_transcript.txt").read_text(encoding="utf-8"),
        "lockfile": json.loads((run_dir / "source_lockfile.json").read_text(encoding="utf-8")),
        "units": load_jsonl(run_dir / "units.jsonl"),
        "annotations": load_jsonl(run_dir / "annotations.jsonl"),
        "term_occurrences": load_jsonl(run_dir / "term_occurrences.jsonl"),
    }


@pytest.fixture(scope="session")
def pipeline_with_candidates(pipeline):
    """Extend the CP2 pipeline fixture by running candidates (stage 3).
    Returns the full pipeline dict with an added 'candidates' key."""
    import candidates as cands_mod

    run_dir = pipeline["run_dir"]
    cands_mod.build_candidates(str(run_dir))

    result = dict(pipeline)
    result["candidates"] = load_jsonl(run_dir / "candidate_case_files.jsonl")
    return result
