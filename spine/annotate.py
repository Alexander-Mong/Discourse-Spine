"""annotate.py - Stage 2: versioned rule registry over 100% of units -> votes.

Deterministic, stdlib only (spaCy Matcher/PhraseMatcher is the post-CP2 swap once term
volume justifies it; pure-`re` keeps CP2 zero-dependency and runnable without installs).

Two outputs into runs/<run_id>/:
  - annotations.jsonl        one envelope per (unit, rule) hit: anchor + rule_id + version
                             + label_vote + trigger_text. authority split:
                               active rule  -> authority_level=rule_vote (counts toward routing)
                               shadow rule  -> authority_level=shadow    (recall net; no routed vote)
  - term_occurrences.jsonl   gazetteer/seed hits, role=rank_boost (boost rank, never gate routing)

A rule hit is a RECORDED VOTE, NOT TRUTH. Precision comes later (LLM lane + human).
The annotation anchor is the whole UNIT span (sentence); trigger_text records what fired.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path

import ids


def load_rules(registry_path: str):
    rules = []
    with open(registry_path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if not row.get("rule_id") or not row.get("pattern"):
                continue
            pattern_type = (row.get("pattern_type") or "regex").strip()
            try:
                matcher = (re.compile(row["pattern"]) if pattern_type == "regex"
                           else _phrase_to_regex(row["pattern"]))
            except re.error as exc:
                raise ValueError(f"Bad pattern for {row['rule_id']}: {exc}") from exc
            rules.append({
                "rule_id": row["rule_id"].strip(),
                "version": int(row.get("version", "1") or 1),
                "cue_family": row["cue_family"].strip(),
                "pattern_type": pattern_type,
                "authority_mode": (row.get("authority_mode") or "active").strip(),
                "gate": (row.get("gate") or "").strip(),
                "matcher": matcher,
            })
    return rules


def _phrase_to_regex(phrase: str):
    """Literal phrase -> case-insensitive regex. Whitespace flexible; standalone X/Y/Z = wildcard."""
    tokens = phrase.split()
    parts = []
    for t in tokens:
        if t in ("X", "Y", "Z"):
            parts.append(r"\w+")
        else:
            parts.append(re.escape(t))
    body = r"\s+".join(parts)
    lead = r"\b" if phrase[:1].isalnum() else ""
    tail = r"\b" if phrase[-1:].isalnum() else ""
    return re.compile(r"(?i)" + lead + body + tail)


def load_terms(term_seeds_path: str, gazetteer_path: str):
    """Returns [{term, canonical_form, category, matcher}]. Gazetteer rows override/extend seeds."""
    terms = {}
    if Path(term_seeds_path).exists():
        with open(term_seeds_path, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                term = (row.get("term") or "").strip()
                if not term or term.startswith("#"):
                    continue
                terms[term.lower()] = {"term": term, "canonical_form": term,
                                       "category": (row.get("category") or "").strip()}
    if Path(gazetteer_path).exists():
        with open(gazetteer_path, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                term = (row.get("term") or "").strip()
                if not term or term.startswith("#"):
                    continue
                terms[term.lower()] = {
                    "term": term,
                    "canonical_form": (row.get("canonical_form") or term).strip(),
                    "category": (row.get("category") or terms.get(term.lower(), {}).get("category", "")).strip(),
                }
    out = []
    for entry in terms.values():
        out.append({**entry, "matcher": re.compile(r"(?i)\b" + re.escape(entry["term"]) + r"\b")})
    return out


def _read_units(run_dir: Path):
    units = []
    with (run_dir / "units.jsonl").open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                units.append(json.loads(line))
    return units


def annotate(run_dir: str, registry_path: str, term_seeds_path: str, gazetteer_path: str):
    run_dir = Path(run_dir)
    units = _read_units(run_dir)
    rules = load_rules(registry_path)
    terms = load_terms(term_seeds_path, gazetteer_path)

    annotations, term_occurrences = [], []
    counts = {"active": 0, "shadow": 0, "terms": 0}

    for unit in units:
        text = unit["text"]
        sid = unit["source_id"]
        run_id = unit["run_id"]
        cstart, cend = unit["anchor"]["char_span"]
        tc_ref = unit["anchor"]["timecode"]["start"]

        for rule in rules:
            matches = list(rule["matcher"].finditer(text))
            if not matches:
                continue
            trigger = matches[0].group(0).strip()
            is_shadow = rule["authority_mode"] == "shadow"
            authority_level = "shadow" if is_shadow else "rule_vote"
            ann = {
                "annotation_id": ids.annotation_id(sid, cstart, cend, rule["rule_id"]),
                "source_id": sid,
                "run_id": run_id,
                "unit_ref": unit["unit_id"],
                "anchor": {"char_span": [cstart, cend], "timecode_ref": tc_ref},
                "method": {"stage": "annotate", "matcher": "re_" + rule["pattern_type"]},
                "rule_id": rule["rule_id"],
                "rule_version": rule["version"],
                "label_vote": rule["cue_family"],
                "taxonomy": "cue",
                "trigger_text": trigger,
                "authority_level": authority_level,
                "authority_mode": rule["authority_mode"],
                "review_state": "rule_matched",
            }
            if rule["gate"]:
                ann["gate"] = rule["gate"]
            annotations.append(ann)
            counts["shadow" if is_shadow else "active"] += 1

        for term in terms:
            if (m := term["matcher"].search(text)):
                trig = m.group(0)
                term_occurrences.append({
                    "term_occurrence_id": ids.term_occurrence_id(sid, cstart, cend, term["term"]),
                    "source_id": sid,
                    "run_id": run_id,
                    "unit_ref": unit["unit_id"],
                    "anchor": {"char_span": [cstart, cend], "timecode_ref": tc_ref},
                    "method": {"stage": "annotate", "matcher": "re_phrase"},
                    "term": term["term"],
                    "canonical_form": term["canonical_form"],
                    "category": term["category"],
                    "role": "rank_boost",
                    "trigger_text": trig,
                    "authority_level": "shadow",
                    "review_state": "rule_matched",
                })
                counts["terms"] += 1

    with (run_dir / "annotations.jsonl").open("w", encoding="utf-8") as fh:
        for a in annotations:
            fh.write(json.dumps(a, ensure_ascii=False) + "\n")
    with (run_dir / "term_occurrences.jsonl").open("w", encoding="utf-8") as fh:
        for t in term_occurrences:
            fh.write(json.dumps(t, ensure_ascii=False) + "\n")

    return {"units": len(units), "annotations": len(annotations),
            "term_occurrences": len(term_occurrences), **counts}


def main():
    ap = argparse.ArgumentParser(description="Stage 2 annotate: rules + term pass over units.")
    ap.add_argument("run_dir")
    ap.add_argument("--registry", default="inputs/rule_registry.csv")
    ap.add_argument("--term-seeds", default="inputs/term_seeds.csv")
    ap.add_argument("--gazetteer", default="inputs/gazetteer.csv")
    args = ap.parse_args()
    print(json.dumps(annotate(args.run_dir, args.registry, args.term_seeds, args.gazetteer), indent=2))


if __name__ == "__main__":
    main()
