"""enrich.py - Stage 2.5: additive, standoff feature computation over units.

Reads units.jsonl from each source dir in a run root.
For each unit computes 3 features and emits feature_records.jsonl (separate from
units/annotations/candidates — the standoff guarantee).

This stage has ZERO effect on routing/evidence_ready/ranking.  The envelope has no
such field; non-gating is structural, not promised.

Three features (contract: features/feature_registry.csv + controls/feature_interpretation_guide.md):
  1. surface_complexity_profile (measurement) — ALWAYS one record per unit. Suppressed when
     segmentation_quality in {unpunctuated, window_mode} or length_tokens < 5.
  2. discourse_marker_profile (cue) — emits only when >=1 marker fires.
  3. possible_visual_or_deictic_dependence (warning) — emits only on a hit (high-confidence only).

CLI:
  python enrich.py <run_root>           # dry-run: print summary, write nothing
  python enrich.py <run_root> --execute # write feature_records.jsonl into each source dir

Conventions match annotate.py / candidates.py: default dry-run, --execute to write.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path

# Allow running from repo root or spine/
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import ids as _ids

# ── repo root relative to this file ──────────────────────────────────────────
_REPO = _HERE.parent
_SCHEMA_DIR = _REPO / "schema"
_FEATURES_DIR = _REPO / "features"

# ── registry constants (locked contract values) ───────────────────────────────
_REGISTRY = {
    "surface_complexity_profile": {
        "version": "1.0.0",
        "role": "measurement",
        "signal_basis": "deterministic_surface",
        "presentation": "pack_visible_when_triggered",
    },
    "discourse_marker_profile": {
        # version is NOT stored here — derived from discourse_marker_lexicon.csv at runtime
        # via _get_lexicon_version() so lexicon bumps flow automatically into method.version
        "role": "cue",
        "signal_basis": "deterministic_surface",
        "presentation": "pack_visible_when_triggered",
    },
    "possible_visual_or_deictic_dependence": {
        "version": "1.0.0",
        "role": "warning",
        "signal_basis": "deterministic_surface",
        "presentation": "pack_visible_when_triggered",
    },
}

# ── suppression triggers for surface_complexity_profile ──────────────────────
_SUPPRESS_SEG_QUALITIES = {"unpunctuated", "window_mode"}
_SUPPRESS_MIN_TOKENS = 5

# ── high-confidence visual/deictic phrase list ────────────────────────────────
# Precision over recall (contract: F-proof-3).  Each entry is a 3-tuple:
# (pattern, surface, kind).
# kind values: "visual_reference", "deictic_gesture", "deictic_pronoun"
_DEICTIC_PHRASES: list[tuple[re.Pattern, str, str]] = []

_DEICTIC_RAW = [
    # (surface_phrase, kind)
    ("as you can see", "visual_reference"),
    ("as we can see", "visual_reference"),
    ("look at this", "deictic_gesture"),
    ("look at that", "deictic_gesture"),
    ("look at these", "deictic_gesture"),
    ("look at those", "deictic_gesture"),
    ("over here", "deictic_gesture"),
    ("over there", "deictic_gesture"),
    ("right here", "deictic_gesture"),
    ("right there", "deictic_gesture"),
    ("this chart", "visual_reference"),
    ("this slide", "visual_reference"),
    ("this graph", "visual_reference"),
    ("this diagram", "visual_reference"),
    ("this figure", "visual_reference"),
    ("this table", "visual_reference"),
    ("this image", "visual_reference"),
    ("this picture", "visual_reference"),
    ("this screenshot", "visual_reference"),
    ("that chart", "visual_reference"),
    ("that slide", "visual_reference"),
    ("that graph", "visual_reference"),
    ("that one", "deictic_pronoun"),
    ("this one", "deictic_pronoun"),
    ("these ones", "deictic_pronoun"),
    ("those ones", "deictic_pronoun"),
    ("up here", "deictic_gesture"),
    ("down here", "deictic_gesture"),
    ("on the left", "visual_reference"),
    ("on the right", "visual_reference"),
    ("in the middle", "visual_reference"),
]

for _surface, _kind in _DEICTIC_RAW:
    _pat = re.compile(r"(?i)\b" + re.escape(_surface) + r"\b")
    _DEICTIC_PHRASES.append((_pat, _surface, _kind))


# ── lexicon loading ───────────────────────────────────────────────────────────

_LEXICON_COLUMNS = ("surface", "category", "fp_note", "version")


def _load_lexicon(lexicon_path: Path) -> list[dict]:
    """Load the discourse marker lexicon CSV.  Returns list of dicts with compiled matcher.

    Raises a located ValueError if a required column is missing, instead of a
    bare KeyError, so a malformed CSV reports which column and file is at fault.
    """
    rows = []
    with lexicon_path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        header = reader.fieldnames or []
        missing = [c for c in _LEXICON_COLUMNS if c not in header]
        if missing:
            raise ValueError(
                f"{lexicon_path}: lexicon CSV missing required column(s) "
                f"{missing}; header was {header}"
            )
        for n, row in enumerate(reader, 2):  # row 1 is the header
            try:
                surface = row["surface"].strip()
                category = row["category"].strip()
                fp_note = row["fp_note"].strip()
                version = row["version"].strip()
            except (KeyError, AttributeError) as exc:
                raise ValueError(
                    f"{lexicon_path}:{n}: malformed lexicon row {row!r} ({exc})"
                ) from exc
            if not surface:
                continue
            # Handle "not X but Y" pattern (contains X placeholder)
            if "X" in surface.split() or "Y" in surface.split():
                # Build a permissive regex: not ... but
                pat = re.compile(r"(?i)\bnot\b.{1,60}?\bbut\b")
            else:
                pat = re.compile(r"(?i)\b" + re.escape(surface) + r"\b")
            rows.append({
                "surface": surface,
                "category": category,
                "fp_note": fp_note,
                "version": version,
                "matcher": pat,
            })
    return rows


_LEXICON: list[dict] | None = None


def _get_lexicon() -> list[dict]:
    global _LEXICON
    if _LEXICON is None:
        _LEXICON = _load_lexicon(_FEATURES_DIR / "discourse_marker_lexicon.csv")
    return _LEXICON


_LEXICON_VERSION: str | None = None


def _parse_semver(v: str) -> tuple[int, int, int]:
    """Parse a 'MAJOR.MINOR.PATCH' string into a comparable int tuple."""
    parts = v.strip().split(".")
    try:
        return (int(parts[0]), int(parts[1]), int(parts[2]))
    except (IndexError, ValueError):
        return (0, 0, 0)


def _get_lexicon_version() -> str:
    """Return the max semantic version found in the loaded lexicon's ``version`` column.

    Derived at runtime so any term-add that bumps the CSV version flows automatically
    into method.version (and therefore into feat_ id derivation) without a code edit.
    """
    global _LEXICON_VERSION
    if _LEXICON_VERSION is None:
        rows = _get_lexicon()
        versions = [row["version"] for row in rows if row.get("version")]
        if not versions:
            _LEXICON_VERSION = "0.0.0"
        else:
            best = max(versions, key=_parse_semver)
            _LEXICON_VERSION = best
    return _LEXICON_VERSION


# ── schema + enum loading for validation ─────────────────────────────────────

def _load_schema_and_enums():
    schema = json.loads((_SCHEMA_DIR / "feature_envelope.schema.json").read_text(encoding="utf-8"))
    enums = json.loads((_SCHEMA_DIR / "enums.json").read_text(encoding="utf-8"))
    return schema, enums


_SCHEMA_CACHE: tuple | None = None


def _get_schema():
    global _SCHEMA_CACHE
    if _SCHEMA_CACHE is None:
        _SCHEMA_CACHE = _load_schema_and_enums()
    return _SCHEMA_CACHE


# ── minimal validator (reuses validate.py's check machinery) ──────────────────

def _validate_record(record: dict) -> list[str]:
    """Validate a feature record against feature_envelope.schema.json.
    Returns list of error strings (empty = valid)."""
    # Import the shared schema checker from validate.py (same spine dir)
    import validate as _val
    schema, enums = _get_schema()
    errors: list[str] = []
    _val.check(record, schema, "feature_record", enums, errors)
    return errors


# ── tokenisation ─────────────────────────────────────────────────────────────
# Simple whitespace tokeniser matching the contract definition.
# "tokens" = whitespace-split words (consistent and reproducible).
# Lexical density = content tokens / total tokens.
# Content tokens = tokens that are NOT purely punctuation and NOT stopwords.

_STOPWORDS = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "shall", "can", "to", "of", "in", "for",
    "on", "with", "at", "by", "from", "as", "into", "through", "during",
    "before", "after", "above", "below", "between", "each", "few", "more",
    "most", "other", "some", "such", "no", "nor", "not", "only", "own",
    "same", "so", "than", "too", "very", "just", "and", "but", "or",
    "if", "it", "its", "that", "this", "these", "those", "i", "you",
    "he", "she", "we", "they", "what", "which", "who", "whom", "there",
    "here", "when", "where", "why", "how", "all", "both", "any", "about",
    "up", "out", "then", "also", "s", "t", "re", "ve", "ll", "d", "m",
})


def _tokenise(text: str) -> list[str]:
    return text.split()


def _is_content_token(tok: str) -> bool:
    cleaned = re.sub(r"[^\w]", "", tok).lower()
    if not cleaned:
        return False
    return cleaned not in _STOPWORDS


def _lexical_density(tokens: list[str]) -> float:
    if not tokens:
        return 0.0
    content = sum(1 for t in tokens if _is_content_token(t))
    return round(content / len(tokens), 4)


# ── feature computations ──────────────────────────────────────────────────────

def _require(unit: dict, key: str) -> object:
    """Fetch a required unit field, raising a located ValueError (not a bare
    KeyError) if it is missing, so a malformed unit reports which key and unit."""
    try:
        return unit[key]
    except (KeyError, TypeError) as exc:
        uid = unit.get("unit_id", "<unknown>") if isinstance(unit, dict) else "<non-dict>"
        raise ValueError(f"unit {uid!r}: malformed unit, missing required key {key!r} ({exc})") from exc


def _compute_surface_complexity(unit: dict) -> dict:
    """Compute surface_complexity_profile for a unit.  Always returns one record dict."""
    seg_q = unit.get("segmentation_quality", "")
    text = _require(unit, "text")
    tokens = _tokenise(text)
    n_tokens = len(tokens)

    suppress = seg_q in _SUPPRESS_SEG_QUALITIES or n_tokens < _SUPPRESS_MIN_TOKENS
    triggered = not suppress

    if suppress:
        suppression_reason = (
            f"segmentation_quality={seg_q!r}" if seg_q in _SUPPRESS_SEG_QUALITIES
            else f"length_tokens={n_tokens} < {_SUPPRESS_MIN_TOKENS}"
        )
        payload = {
            "suppressed": True,
            "suppression_reason": suppression_reason,
        }
    else:
        payload = {
            "length_tokens": n_tokens,
            "length_chars": len(text),
            "lexical_density": _lexical_density(tokens),
            "suppressed": False,
            "suppression_reason": "",
        }

    feat = _REGISTRY["surface_complexity_profile"]
    unit_ref = _require(unit, "unit_id")
    return {
        "feature_record_id": _ids.feature_record_id(unit_ref, "surface_complexity_profile", feat["version"]),
        "unit_ref": unit_ref,
        "source_id": _require(unit, "source_id"),
        "run_id": _require(unit, "run_id"),
        "scope": "unit",
        "method": {
            "stage": "enrich",
            "name": "surface_complexity_profile",
            "version": feat["version"],
        },
        "role": feat["role"],
        "signal_basis": feat["signal_basis"],
        "presentation": feat["presentation"],
        "triggered": triggered,
        "payload": payload,
    }


def _compute_discourse_marker(unit: dict) -> dict | None:
    """Compute discourse_marker_profile.  Returns None if no markers fire."""
    text = _require(unit, "text")
    lexicon = _get_lexicon()
    fired: list[dict] = []
    seen_surfaces: set[str] = set()

    for entry in lexicon:
        if entry["matcher"].search(text):
            surface = entry["surface"]
            # Deduplicate: one entry per surface per unit (same surface can't fire twice)
            if surface in seen_surfaces:
                continue
            seen_surfaces.add(surface)
            fired.append({
                "surface": surface,
                "category": entry["category"],
                "lexicon_version": entry["version"],
                "fp_note": entry["fp_note"],
            })

    if not fired:
        return None

    categories_present = sorted({m["category"] for m in fired})
    feat = _REGISTRY["discourse_marker_profile"]
    version = _get_lexicon_version()  # derived from lexicon CSV, tracks bumps automatically
    unit_ref = _require(unit, "unit_id")
    return {
        "feature_record_id": _ids.feature_record_id(unit_ref, "discourse_marker_profile", version),
        "unit_ref": unit_ref,
        "source_id": _require(unit, "source_id"),
        "run_id": _require(unit, "run_id"),
        "scope": "unit",
        "method": {
            "stage": "enrich",
            "name": "discourse_marker_profile",
            "version": version,
        },
        "role": feat["role"],
        "signal_basis": feat["signal_basis"],
        "presentation": feat["presentation"],
        "triggered": True,
        "payload": {
            "markers": fired,
            "categories_present": categories_present,
        },
    }


def _compute_deictic(unit: dict) -> dict | None:
    """Compute possible_visual_or_deictic_dependence.  Returns None if no phrases fire."""
    text = _require(unit, "text")
    fired: list[dict] = []
    seen_surfaces: set[str] = set()

    for pat, surface, kind in _DEICTIC_PHRASES:
        if pat.search(text):
            if surface in seen_surfaces:
                continue
            seen_surfaces.add(surface)
            fired.append({"surface": surface, "kind": kind})

    if not fired:
        return None

    feat = _REGISTRY["possible_visual_or_deictic_dependence"]
    unit_ref = _require(unit, "unit_id")
    return {
        "feature_record_id": _ids.feature_record_id(unit_ref, "possible_visual_or_deictic_dependence", feat["version"]),
        "unit_ref": unit_ref,
        "source_id": _require(unit, "source_id"),
        "run_id": _require(unit, "run_id"),
        "scope": "unit",
        "method": {
            "stage": "enrich",
            "name": "possible_visual_or_deictic_dependence",
            "version": feat["version"],
        },
        "role": feat["role"],
        "signal_basis": feat["signal_basis"],
        "presentation": feat["presentation"],
        "triggered": True,
        "payload": {
            "phrases": fired,
            "confidence": "high",
        },
    }


# ── source-dir processing ─────────────────────────────────────────────────────

def _load_units(source_dir: Path) -> list[dict]:
    units_path = source_dir / "units.jsonl"
    if not units_path.exists():
        return []
    rows = []
    with units_path.open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def _enrich_source_dir(source_dir: Path, execute: bool) -> dict:
    """Process one source dir.  Returns a summary dict."""
    units = _load_units(source_dir)
    if not units:
        return {"source_dir": source_dir.name, "units": 0, "records": 0, "skipped": 0}

    records: list[dict] = []
    validation_errors: list[str] = []
    counts = {
        "surface_complexity_profile": {"total": 0, "triggered": 0},
        "discourse_marker_profile": {"total": 0, "triggered": 0},
        "possible_visual_or_deictic_dependence": {"total": 0, "triggered": 0},
    }

    for unit in units:
        # 1. surface_complexity_profile — always one record
        scp = _compute_surface_complexity(unit)
        errs = _validate_record(scp)
        if errs:
            validation_errors.extend(f"[surface_complexity_profile/{unit['unit_id']}] {e}" for e in errs)
        else:
            records.append(scp)
            counts["surface_complexity_profile"]["total"] += 1
            if scp["triggered"]:
                counts["surface_complexity_profile"]["triggered"] += 1

        # 2. discourse_marker_profile — only on hit
        dmp = _compute_discourse_marker(unit)
        if dmp is not None:
            errs = _validate_record(dmp)
            if errs:
                validation_errors.extend(f"[discourse_marker_profile/{unit['unit_id']}] {e}" for e in errs)
            else:
                records.append(dmp)
                counts["discourse_marker_profile"]["total"] += 1
                counts["discourse_marker_profile"]["triggered"] += 1

        # 3. possible_visual_or_deictic_dependence — only on hit
        pvd = _compute_deictic(unit)
        if pvd is not None:
            errs = _validate_record(pvd)
            if errs:
                validation_errors.extend(f"[possible_visual_or_deictic_dependence/{unit['unit_id']}] {e}" for e in errs)
            else:
                records.append(pvd)
                counts["possible_visual_or_deictic_dependence"]["total"] += 1
                counts["possible_visual_or_deictic_dependence"]["triggered"] += 1

    if validation_errors:
        raise RuntimeError(
            f"Validation errors in {source_dir.name} — aborting:\n" +
            "\n".join(f"  {e}" for e in validation_errors)
        )

    if execute:
        out_path = source_dir / "feature_records.jsonl"
        with out_path.open("w", encoding="utf-8") as fh:
            for rec in records:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    return {
        "source_dir": source_dir.name,
        "units": len(units),
        "records_total": len(records),
        "surface_complexity_profile": counts["surface_complexity_profile"],
        "discourse_marker_profile": counts["discourse_marker_profile"],
        "possible_visual_or_deictic_dependence": counts["possible_visual_or_deictic_dependence"],
        "written": execute,
    }


# ── run-root processing ───────────────────────────────────────────────────────

def enrich_run(run_root: str, execute: bool = False) -> dict:
    """Enrich all source dirs under run_root.  Returns aggregate summary."""
    run_root_path = Path(run_root)
    if not run_root_path.exists():
        raise FileNotFoundError(f"run_root not found: {run_root}")

    # Discover source dirs: any subdir containing units.jsonl
    source_dirs = sorted(
        d for d in run_root_path.iterdir()
        if d.is_dir() and (d / "units.jsonl").exists()
    )

    if not source_dirs:
        raise FileNotFoundError(f"No source dirs with units.jsonl found under {run_root}")

    totals = {
        "sources": 0,
        "units": 0,
        "records_total": 0,
        "surface_complexity_profile": {"total": 0, "triggered": 0},
        "discourse_marker_profile": {"total": 0, "triggered": 0},
        "possible_visual_or_deictic_dependence": {"total": 0, "triggered": 0},
    }
    per_source: list[dict] = []

    for sd in source_dirs:
        result = _enrich_source_dir(sd, execute=execute)
        per_source.append(result)
        totals["sources"] += 1
        totals["units"] += result["units"]
        totals["records_total"] += result["records_total"]
        for feat in ("surface_complexity_profile", "discourse_marker_profile",
                     "possible_visual_or_deictic_dependence"):
            totals[feat]["total"] += result[feat]["total"]
            totals[feat]["triggered"] += result[feat]["triggered"]

    return {
        "run_root": str(run_root_path),
        "execute": execute,
        "totals": totals,
        "per_source": per_source,
    }


# ── CLI ────────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="Stage 2.5 enrich: additive standoff feature records over units.")
    ap.add_argument("run_root",
                    help="Run root directory (contains source subdirs with units.jsonl). "
                         "E.g. runs/demo")
    ap.add_argument("--execute", action="store_true",
                    help="Actually write feature_records.jsonl into each source dir. "
                         "Default is dry-run (print summary only, write nothing).")
    args = ap.parse_args()

    result = enrich_run(args.run_root, execute=args.execute)
    t = result["totals"]

    mode = "EXECUTE" if args.execute else "DRY-RUN"
    print(f"[enrich {mode}] run_root={result['run_root']}")
    print(f"  sources={t['sources']}  units={t['units']}  records_total={t['records_total']}")
    print(f"  surface_complexity_profile:           "
          f"total={t['surface_complexity_profile']['total']}  "
          f"triggered={t['surface_complexity_profile']['triggered']}  "
          f"suppressed={t['surface_complexity_profile']['total'] - t['surface_complexity_profile']['triggered']}")
    print(f"  discourse_marker_profile:             "
          f"total={t['discourse_marker_profile']['total']}  "
          f"triggered={t['discourse_marker_profile']['triggered']}")
    print(f"  possible_visual_or_deictic_dependence: "
          f"total={t['possible_visual_or_deictic_dependence']['total']}  "
          f"triggered={t['possible_visual_or_deictic_dependence']['triggered']}")
    if not args.execute:
        print("  (dry-run: no files written)")
    else:
        print(f"  feature_records.jsonl written to {t['sources']} source dir(s)")


if __name__ == "__main__":
    main()
