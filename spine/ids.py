"""ids.py - content-addressed ID derivation for the Discourse Spine.

SINGLE SOURCE OF TRUTH for ID format. No other module constructs an ID by hand;
every stage imports from here. Stdlib only.

Design (from TRACE_PAGE_HANDWRITTEN_V0_1.md):
  - unit_id        : source_id + zero-padded char-start (deterministic, anchor-scoped)
  - annotation_id  : source_id + span + rule_id + schema-version (anchor+rule scoped)
  - term_occ_id    : source_id + span + term-slug (anchor+term scoped)
  - candidate_id   : CONTENT-ADDRESSED  "cand_" + sha256(source|span|rule|schema)[:8]
                     -> survives reruns and registry edits; never orphans a human decision
  - llm_vote_id    : pack-scoped (run_short + pack + seq); NOT content-addressed (a vote is
                     archival-reproducible, not operationally reproducible)
  - evidence_id    : "ev_" + candidate_id

Authority flows DOWN, data flows FORWARD; IDs are the join keys for the whole graph.
"""
from __future__ import annotations

import hashlib
import re

SCHEMA_VERSION = "0.1"


def schema_tag(schema_version: str = SCHEMA_VERSION) -> str:
    """'0.1' -> 's0_1'  (ID-safe form of the schema version)."""
    return "s" + schema_version.replace(".", "_")


def _slug(text: str) -> str:
    """Lowercase, collapse any run of non-alphanumerics to a single underscore."""
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def source_id(text: str, index: int) -> str:
    """Harvest-time source id, e.g. source_id('awslux_kickoff', 1) -> 'src_awslux_kickoff_01'.
    `text` is slugified here (lowercased, non-alphanumerics collapsed); `index` disambiguates collisions."""
    return f"src_{_slug(text)}_{index:02d}"


def unit_id(source_id_: str, char_start: int) -> str:
    """Deterministic, anchor-scoped. char-start zero-padded to >=6 digits (never truncated)."""
    return f"unit_{source_id_}_{char_start:06d}"


def annotation_id(source_id_: str, char_start: int, char_end: int, rule_id: str,
                  schema_version: str = SCHEMA_VERSION) -> str:
    """Anchor+rule scoped. Joins source, span, rule_id and ID-safe schema version."""
    return f"ann_{source_id_}__{char_start}-{char_end}__{rule_id}__{schema_tag(schema_version)}"


def term_occurrence_id(source_id_: str, char_start: int, char_end: int, term: str) -> str:
    """Anchor+term scoped. Joins source, span and the slugified term."""
    return f"term_{source_id_}__{char_start}-{char_end}__{_slug(term)}"


def candidate_id(source_id_: str, char_start: int, char_end: int, rule_id: str,
                 schema_version: str = SCHEMA_VERSION) -> str:
    """CONTENT-ADDRESSED. Stable across reruns: identical (source, span, rule, schema) -> identical id.
    Matches TRACE_PAGE derivation exactly: sha256("source|start-end|rule|schema")[:8]."""
    key = f"{source_id_}|{char_start}-{char_end}|{rule_id}|{schema_version}"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]
    return f"cand_{digest}"


def llm_vote_id(run_short: str, pack_id: str, seq: int) -> str:
    """Pack-scoped (not content-addressed). run_short is the short run token (e.g. 'a3f1')."""
    return f"llmv_run_{run_short}_{pack_id}_{seq:03d}"


def evidence_id(candidate_id_: str) -> str:
    return f"ev_{candidate_id_}"


def feature_record_id(unit_ref: str, method_name: str, method_version: str) -> str:
    """CONTENT-ADDRESSED. Stable across reruns: identical (unit_ref, method_name, method_version) -> identical id.
    Derives feat_ from sha256(unit_ref|method_name|method_version)[:8]."""
    key = f"{unit_ref}|{method_name}|{method_version}"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]
    return f"feat_{digest}"


def pack_id(prompt_id: str, prompt_version: int, candidate_id_: str,
            context_before: str, candidate_text: str, context_after: str) -> str:
    """CONTENT-ADDRESSED pack id. Identical pack content -> identical id, so the SAME
    candidate's evaluator pack is byte-stable across lanes/models (pack_id constant, model
    varies). Excludes model + nondeterministic fields by design."""
    key = "|".join([prompt_id, str(prompt_version), candidate_id_,
                    context_before, candidate_text, context_after])
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]
    return f"pack_{digest}"


def run_short(run_id: str) -> str:
    """Extract the trailing short token from a run_id like 'run_2026-06-12T0930_a3f1' -> 'a3f1'."""
    return run_id.rsplit("_", 1)[-1]


if __name__ == "__main__":
    # Smoke check: reproduce the trace-page derivation shape.
    sid = "src_awslux_kickoff_01"
    print("unit       ", unit_id(sid, 4120))
    print("annotation ", annotation_id(sid, 4120, 4196, "reframe_001"))
    print("candidate  ", candidate_id(sid, 4120, 4196, "reframe_001"))
    print("term       ", term_occurrence_id(sid, 4120, 4140, "foundation model"))
    print("llm_vote   ", llm_vote_id("a3f1", "pack_07", 1))
    print("evidence   ", evidence_id(candidate_id(sid, 4120, 4196, "reframe_001")))
    print("feature    ", feature_record_id(unit_id(sid, 4120), "surface_complexity_profile", "1.0.0"))
