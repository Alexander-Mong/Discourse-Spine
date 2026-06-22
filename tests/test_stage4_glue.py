"""Unit tests for the deterministic glue in stage4_eval.

No network, no model: these cover parse_response / _extract_first_json_object,
validate_vote, and build_summary. Import path mirrors conftest (spine on sys.path).
"""
import sys
from pathlib import Path

import pytest

# Mirror conftest: put the spine dir on sys.path so `import stage4_eval` works.
SPINE = Path(__file__).resolve().parents[1] / "spine"
if str(SPINE) not in sys.path:
    sys.path.insert(0, str(SPINE))

import stage4_eval as s4  # noqa: E402


# ── parse_response / _extract_first_json_object ─────────────────────────────────
def _vote_json(verdict="reject", rationale="ok"):
    """A minimal well-formed candidate-evaluate JSON body as a raw string."""
    return (
        '{"rationale":"%s","verdict":"%s","inference_type":"stated",'
        '"confidence":"high","rejection_reason":"wrong_family"}'
        % (rationale, verdict)
    )


PARSE_CASES = [
    # (label, raw_text, expect_ok, expected_verdict)
    ("clean_json", _vote_json("reject"), True, "reject"),
    ("clean_confirm", _vote_json("confirm"), True, "confirm"),
    ("wrapped_in_prose",
     "Here is my judgment:\n" + _vote_json("reject") + "\nThanks!", True, "reject"),
    ("json_fence", "```json\n" + _vote_json("confirm") + "\n```", True, "confirm"),
    ("bare_fence", "```\n" + _vote_json("reject") + "\n```", True, "reject"),
    # Brace inside a string literal must not truncate the object.
    ("brace_in_string_value",
     '{"rationale":"use } carefully","verdict":"reject","inference_type":"stated",'
     '"confidence":"high","rejection_reason":"wrong_family"}', True, "reject"),
    # Trailing comma is invalid JSON -> parse failure.
    ("trailing_comma",
     '{"verdict":"reject","inference_type":"stated","confidence":"high",}', False, None),
    ("empty_string", "", False, None),
    ("no_json", "I cannot answer this.", False, None),
]


@pytest.mark.parametrize("label,raw,expect_ok,expected_verdict",
                         PARSE_CASES, ids=[c[0] for c in PARSE_CASES])
def test_parse_response(label, raw, expect_ok, expected_verdict):
    parsed, err = s4.parse_response(raw)
    if expect_ok:
        assert parsed is not None, f"{label}: expected parse success, got error {err!r}"
        assert err is None
        assert parsed["verdict"] == expected_verdict
    else:
        assert parsed is None, f"{label}: expected parse failure, got {parsed!r}"
        assert err, f"{label}: failure must carry a reason"


def test_extract_handles_brace_in_string():
    raw = '{"rationale":"a } b { c","verdict":"confirm"}'
    extracted = s4._extract_first_json_object(raw)
    assert extracted == raw  # whole object returned, not truncated at the inner '}'


# ── validate_vote (via build_vote) ──────────────────────────────────────────────
def _fake_pack():
    return {
        "candidate_ref": "cand_abcd1234",
        "source_id": "src_test",
        "run_id": "run_test",
        "pack_id": "pack_test_0001",
        "is_control": False,
    }


def _build_vote(parsed_vote, lane="anthropic"):
    norm = {"input": 100, "cached_input": 0, "output": 20, "reasoning": 0}
    return s4.build_vote(_fake_pack(), parsed_vote, "claude-haiku-4-5",
                         norm, seq=0, run_short_token="abcd", lane=lane)


def test_validate_vote_clean():
    parsed = {"verdict": "confirm", "inference_type": "stated",
              "confidence": "high", "rationale": "clearly an instance"}
    rec = _build_vote(parsed)
    assert s4.validate_vote(rec) == []


def test_validate_vote_reject_without_reason_errors():
    parsed = {"verdict": "reject", "inference_type": "stated",
              "confidence": "high", "rationale": "not an instance"}
    rec = _build_vote(parsed)
    errors = s4.validate_vote(rec)
    assert errors, "reject without rejection_reason must error"
    assert any("rejection_reason" in e for e in errors)


# ── build_summary ───────────────────────────────────────────────────────────────
def _vote_dict(candidate_ref, verdict, is_control, lane="anthropic",
               rejection_reason="wrong_family"):
    inner = {"verdict": verdict, "inference_type": "stated",
             "confidence": "high", "rationale": "r"}
    if verdict == "reject":
        inner["rejection_reason"] = rejection_reason
    return {
        "candidate_ref": candidate_ref,
        "model": "claude-haiku-4-5",
        "lane": lane,
        "is_control": is_control,
        "vote": inner,
        "token_counts": {"prompt": 100, "completion": 20},
    }


def test_build_summary_counts_and_control_bar():
    votes = [
        # 3 non-control: 2 confirm, 1 reject
        _vote_dict("cand_0001", "confirm", False),
        _vote_dict("cand_0002", "confirm", False),
        _vote_dict("cand_0003", "reject", False),
        # 10 controls: 9 reject (correct), 1 confirm (miss) -> 0.90 exactly -> passes
        *[_vote_dict(f"cand_ctl{i:02d}", "reject", True) for i in range(9)],
        _vote_dict("cand_ctl09", "confirm", True),
    ]
    summary = s4.build_summary(votes, malformed=[], lane_names=["anthropic"])
    lane = summary["anthropic"]

    assert lane["votes"] == 13
    assert lane["verdict_distribution"] == {"confirm": 3, "reject": 10}
    assert lane["control_votes"] == 10
    assert lane["control_rejection_rate"] == pytest.approx(0.90)
    assert lane["control_bar_0_90_passed"] is True


def test_build_summary_control_bar_fails_below_threshold():
    votes = [
        # 10 controls, only 8 correctly rejected -> 0.80 < 0.90 -> fails
        *[_vote_dict(f"cand_c{i:02d}", "reject", True) for i in range(8)],
        _vote_dict("cand_c08", "confirm", True),
        _vote_dict("cand_c09", "uncertain", True),
    ]
    summary = s4.build_summary(votes, malformed=[], lane_names=["anthropic"])
    lane = summary["anthropic"]
    assert lane["control_rejection_rate"] == pytest.approx(0.80)
    assert lane["control_bar_0_90_passed"] is False
