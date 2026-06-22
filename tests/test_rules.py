"""Rule-fire + authority-split expectations on the golden fixture.

Asserts the CP3 rule-registry overhaul is actually enforced by annotate.py:
  - advice_001b + specific question_001/003 + reframe_001/v2a/v2b fire ACTIVE (rule_vote)
  - advice_001c, caveat, example, generic question_002, reframe_v2c fire SHADOW
  - terms fire as rank_boost
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
INPUTS = REPO / "inputs"


def _by_rule(annotations):
    out = {}
    for a in annotations:
        out.setdefault(a["rule_id"], []).append(a)
    return out


def test_reframe_fires_as_active(pipeline):
    # reframe_001 is the canonical X-not-Y reframe and fires active (rule_vote).
    rules = _by_rule(pipeline["annotations"])
    assert "reframe_001" in rules, "canonical X-not-Y reframe should fire on cue 2"
    for a in rules["reframe_001"]:
        assert a["authority_level"] == "rule_vote"   # active rules vote
        assert a["authority_mode"] == "active"


def test_reframe_v2_shadow_recall_net_present(pipeline):
    # reframe_v2 is split into v2a (active), v2b (active), v2c (shadow).
    # The fixture text fires reframe_v2a (active). reframe_v2c is the shadow recall
    # net; it doesn't fire on the fixture text but must exist in the registry.
    import csv
    registry_path = INPUTS / "rule_registry.csv"
    with open(registry_path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    shadow_reframe_ids = [r["rule_id"] for r in rows
                          if r["cue_family"] == "reframe" and r["authority_mode"] == "shadow"]
    assert "reframe_v2c" in shadow_reframe_ids, (
        "CP3 split: reframe_v2c must exist as shadow recall net in rule_registry.csv"
    )
    # Also confirm v2a fires on the fixture as active (CP3 promoted)
    rules = _by_rule(pipeline["annotations"])
    assert "reframe_v2a" in rules, "reframe_v2a should fire on cue 2 (it's not ... it's)"
    assert all(a["authority_mode"] == "active" for a in rules["reframe_v2a"])


def test_advice_fires_as_active(pipeline):
    # CP3: advice_001 split into advice_001a/b/c. "you need to" now matches advice_001b.
    rules = _by_rule(pipeline["annotations"])
    assert "advice_001b" in rules, "advice_001b should fire on 'you need to' in cue 3 (CP3 split)"
    for a in rules["advice_001b"]:
        assert a["authority_level"] == "rule_vote"   # advice_001b is active
        assert a["authority_mode"] == "active"


def test_example_kept_shadow(pipeline):
    """example_001 kept shadow: judged against a proper definition it confirms ~38%, below bar."""
    rules = _by_rule(pipeline["annotations"])
    assert "example_001" in rules, "example_001 should fire on 'For example' in cue 6"
    for a in rules["example_001"]:
        assert a["authority_mode"] == "shadow", "example_001 stays shadow"


def test_question_specific_active_generic_shadow(pipeline):
    rules = _by_rule(pipeline["annotations"])
    assert "question_003" in rules and rules["question_003"][0]["authority_mode"] == "active"
    if "question_002" in rules:  # generic question, throttled
        for a in rules["question_002"]:
            assert a["authority_mode"] == "shadow"
            assert a.get("gate") == "window_fallback"


def test_caveat_fires_as_shadow(pipeline):
    rules = _by_rule(pipeline["annotations"])
    assert "caveat_002" in rules, "caveat_002 should fire on 'The risk is' in cue 4"
    assert all(a["authority_mode"] == "shadow" for a in rules["caveat_002"])


def test_terms_fire_as_rank_boost(pipeline):
    terms = {t["term"] for t in pipeline["term_occurrences"]}
    assert {"foundation model", "unit economics", "inference cost", "open source"} <= terms
    for t in pipeline["term_occurrences"]:
        assert t["role"] == "rank_boost"


def test_taxonomy_is_cue_for_all_rule_votes(pipeline):
    for a in pipeline["annotations"]:
        assert a["taxonomy"] == "cue"
