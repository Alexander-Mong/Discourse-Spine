"""stage4_eval.py — Stage-4 evaluator runner for the Discourse Spine.

CLI:
  python spine/stage4_eval.py --run-root runs/demo
                               [--limit N]
                               [--lane claude|anthropic]
                               [--execute]

Dry-run default: build packs + report counts, NO model calls.
--execute: make the live calls and write outputs.

Outputs (under run-root):
  llm_votes.jsonl       all valid votes, all lanes (model field distinguishes)
  malformed_log.jsonl   malformed / schema-invalid responses
  stage4_summary.json   per-lane counts + control rejection rate + cross-lane agreement

Spec: Stage-4 Evaluator Spec 2026-06-12 (locked governance artifact).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import sys
import tempfile
from collections import Counter, defaultdict
from typing import Optional

# ── Insert spine directory onto sys.path (lane_runner + ids are vendored here) ──
_SPINE_DIR = pathlib.Path(__file__).resolve().parent
if str(_SPINE_DIR) not in sys.path:
    sys.path.insert(0, str(_SPINE_DIR))

from lane_runner import run_claude, run_anthropic  # noqa: E402  (vendored — see spine/lane_runner.py)

import ids as _ids  # noqa: E402

# ── Constants ────────────────────────────────────────────────────────────────
PROMPT_ID = "candidate_evaluate"
PROMPT_VERSION = 1
SCHEMA_DIR = _SPINE_DIR.parent / "schema"
PROMPT_TEMPLATE_PATH = (
    _SPINE_DIR.parent / "inputs" / "prompts" / "candidate_evaluate_v1.md"
)
CONTROL_MANIFEST_PATH = (
    _SPINE_DIR.parent / "controls" / "stage4_prereg" / "control_manifest.jsonl"
)

# Control-bar threshold: a lane must correctly reject at least this fraction of the
# hidden controls to pass. All 30 controls are expected_verdict="reject".
CONTROL_BAR = 0.90

# NOTE: every lane call runs from an isolated EMPTY cwd so the agentic CLI (claude)
# cannot explore the repo instead of answering. The runner passes cwd=<tmp>.
# anthropic is listed FIRST: its position is load-bearing for _lane_of_model, which
# resolves a model shared by both lanes (claude-haiku-4-5) to the direct anthropic lane.
LANE_CONFIGS = {
    # anthropic: direct Messages API (HTTP, key from env), no agent harness, one-per-call.
    "anthropic": dict(fn=run_anthropic, model="claude-haiku-4-5", effort=None, sandbox=None, kwargs={}),
    # claude: subscription CLI; --tools "" disables all tools so it answers directly, not agentically.
    "claude": dict(fn=run_claude, model="claude-haiku-4-5", effort=None, sandbox=None,
                   kwargs={"no_tools": True}),
}

# ── Schema / enum loading ─────────────────────────────────────────────────────
def _load_json(path):
    return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))


def _load_jsonl(path):
    rows = []
    with open(path, encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{path}:{n}: invalid JSON: {exc}") from exc
    return rows


_VOTE_SCHEMA: Optional[dict] = None
_ENUMS: Optional[dict] = None


def _get_schema_and_enums():
    global _VOTE_SCHEMA, _ENUMS
    if _VOTE_SCHEMA is None:
        _VOTE_SCHEMA = _load_json(SCHEMA_DIR / "llm_votes.schema.json")
        _ENUMS = _load_json(SCHEMA_DIR / "enums.json")
    return _VOTE_SCHEMA, _ENUMS


# ── validate.py _check, re-used verbatim ─────────────────────────────────────
_TYPE = {"object": dict, "array": list, "string": str, "integer": int,
         "number": (int, float), "boolean": bool}


def _is_type(value, t):
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if t == "boolean":
        return isinstance(value, bool)
    if t == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, _TYPE.get(t, object))


def _check(value, schema, path, enums, errors):
    t = schema.get("type")
    if t is not None:
        types = t if isinstance(t, list) else [t]
        if not any(_is_type(value, tt) for tt in types):
            errors.append(f"{path}: expected type {t}, got {type(value).__name__}")
            return
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: expected const {schema['const']!r}, got {value!r}")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} not in enum {schema['enum']}")
    if "enumRef" in schema:
        allowed = enums.get(schema["enumRef"], [])
        if value not in allowed:
            errors.append(f"{path}: {value!r} not in enums[{schema['enumRef']}]")
    if "pattern" in schema and isinstance(value, str):
        if not re.search(schema["pattern"], value):
            errors.append(f"{path}: {value!r} does not match /{schema['pattern']}/")
    if "minLength" in schema and isinstance(value, str) and len(value) < schema["minLength"]:
        errors.append(f"{path}: shorter than minLength {schema['minLength']}")
    if "minimum" in schema and isinstance(value, (int, float)) and value < schema["minimum"]:
        errors.append(f"{path}: {value} < minimum {schema['minimum']}")
    if isinstance(value, dict) and (schema.get("type") == "object" or "properties" in schema):
        props = schema.get("properties", {})
        for req in schema.get("required", []):
            if req not in value:
                errors.append(f"{path}: missing required field '{req}'")
        if schema.get("additionalProperties") is False:
            for k in value:
                if k not in props:
                    errors.append(f"{path}: unexpected field '{k}'")
        for k, v in value.items():
            if k in props:
                _check(v, props[k], f"{path}.{k}", enums, errors)
    if isinstance(value, list) and schema.get("type") == "array":
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{path}: fewer than minItems {schema['minItems']}")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path}: more than maxItems {schema['maxItems']}")
        if "items" in schema:
            for i, item in enumerate(value):
                _check(item, schema["items"], f"{path}[{i}]", enums, errors)


def validate_vote(vote_obj: dict) -> list[str]:
    """Return list of schema errors (empty = valid)."""
    schema, enums = _get_schema_and_enums()
    errors: list[str] = []
    # Handle the if/then on vote.verdict=reject -> rejection_reason required
    _check(vote_obj, schema, "vote", enums, errors)
    # Manual if/then enforcement (the schema checker does not handle if/then)
    inner = vote_obj.get("vote", {})
    if isinstance(inner, dict) and inner.get("verdict") == "reject":
        if "rejection_reason" not in inner:
            errors.append("vote.vote: verdict=reject requires rejection_reason")
    return errors


# ── Prompt template ───────────────────────────────────────────────────────────
_PROMPT_TEMPLATE: Optional[str] = None


def _get_template() -> str:
    global _PROMPT_TEMPLATE
    if _PROMPT_TEMPLATE is None:
        _PROMPT_TEMPLATE = PROMPT_TEMPLATE_PATH.read_text(encoding="utf-8")
    return _PROMPT_TEMPLATE


def fill_template(family: str, candidate_text: str,
                  context_before: str, context_after: str) -> str:
    t = _get_template()
    t = t.replace("{{family}}", family)
    t = t.replace("{{candidate_text}}", candidate_text)
    t = t.replace("{{context_before}}", context_before)
    t = t.replace("{{context_after}}", context_after)
    return t


# ── Pack dataclass (plain dict) ────────────────────────────────────────────────
def make_pack(candidate_ref: str, source_id: str, run_id: str,
              family: str, candidate_text: str,
              context_before: str, context_after: str,
              is_control: bool) -> dict:
    pid = _ids.pack_id(PROMPT_ID, PROMPT_VERSION, candidate_ref,
                       context_before, candidate_text, context_after)
    prompt = fill_template(family, candidate_text, context_before, context_after)
    pack = {
        "candidate_ref": candidate_ref,
        "source_id": source_id,
        "run_id": run_id,
        "family": family,
        "pack_id": pid,
        "prompt": prompt,
        "is_control": is_control,
        # Raw components retained so batch/other consumers can re-template without re-resolving.
        "candidate_text": candidate_text,
        "context_before": context_before,
        "context_after": context_after,
    }
    # BUG-2 guard: validate at build time so a bare-title / unfilled-placeholder misfire aborts
    # during the dry-run (which builds every pack) at zero model spend — not after dispatch.
    _assert_prompt_payload(pack, prompt)
    return pack


# ── Context builder ────────────────────────────────────────────────────────────
def build_context(unit_ref: str, units_by_charstart: list[dict],
                  n_before: int, n_after: int) -> tuple[str, str]:
    """Return (context_before, context_after) as joined sentence strings."""
    # Find the index of the anchor unit
    idx = None
    for i, u in enumerate(units_by_charstart):
        if u["unit_id"] == unit_ref:
            idx = i
            break
    if idx is None:
        return "", ""
    before_units = units_by_charstart[max(0, idx - n_before):idx]
    after_units = units_by_charstart[idx + 1:idx + 1 + n_after]
    context_before = " ".join(u["text"] for u in before_units)
    context_after = " ".join(u["text"] for u in after_units)
    return context_before, context_after


# ── Source loader ──────────────────────────────────────────────────────────────
def load_source(src_dir: pathlib.Path):
    """Load units (sorted by char_start) and annotations (by id) for a source dir."""
    units_raw = _load_jsonl(src_dir / "units.jsonl")
    units_sorted = sorted(units_raw, key=lambda u: u["anchor"]["char_span"][0])

    ann_by_id = {}
    ann_path = src_dir / "annotations.jsonl"
    if ann_path.exists():
        for a in _load_jsonl(ann_path):
            ann_by_id[a["annotation_id"]] = a

    return units_sorted, ann_by_id


# ── Candidate loader ───────────────────────────────────────────────────────────
def load_routed_candidates(run_root: pathlib.Path, limit: Optional[int] = None,
                           buckets: tuple = ("high", "medium")) -> list[dict]:
    """Load candidates from run_root whose routing bucket is in `buckets`.

    Defaults to the routed set (high + medium). Pass ("high","medium","low") to judge
    every candidate, including the ones the router filtered out, which validates the
    router's recall. Each item is a pack dict."""
    packs = []
    for src_dir in sorted(run_root.iterdir()):
        if not src_dir.is_dir():
            continue
        ccf = src_dir / "candidate_case_files.jsonl"
        if not ccf.exists():
            continue
        try:
            units_sorted, ann_by_id = load_source(src_dir)
        except Exception as exc:
            print(f"  WARNING: could not load source {src_dir.name}: {exc}", file=sys.stderr)
            continue

        units_by_id = {u["unit_id"]: u for u in units_sorted}

        for line in ccf.open(encoding="utf-8"):
            if not line.strip():
                continue
            c = json.loads(line)
            if c["routing"]["bucket"] not in buckets:
                continue

            # Resolve candidate text via unit_ref
            unit_ref = c["anchor"]["unit_ref"]
            unit = units_by_id.get(unit_ref)
            if unit is None:
                print(f"  WARNING: unit_ref {unit_ref!r} not found; skipping candidate {c['candidate_id']}", file=sys.stderr)
                continue
            candidate_text = unit["text"]

            # Resolve family via primary annotation's label_vote
            primary_ann_id = c["vote_refs"][0]
            primary_ann = ann_by_id.get(primary_ann_id)
            if primary_ann is None:
                print(f"  WARNING: primary annotation {primary_ann_id!r} not found; skipping candidate {c['candidate_id']}", file=sys.stderr)
                continue
            family = primary_ann["label_vote"]

            # Build context
            n_before = c["context_window"].get("sentences_before", 2)
            n_after = c["context_window"].get("sentences_after", 2)
            context_before, context_after = build_context(
                unit_ref, units_sorted, n_before, n_after
            )

            pack = make_pack(
                candidate_ref=c["candidate_id"],
                source_id=c["source_id"],
                run_id=c["run_id"],
                family=family,
                candidate_text=candidate_text,
                context_before=context_before,
                context_after=context_after,
                is_control=False,
            )
            packs.append(pack)

            if limit is not None and len(packs) >= limit:
                return packs

    return packs


# ── Control loader ─────────────────────────────────────────────────────────────
def _synthetic_cand_ref(control_id: str) -> str:
    """Deterministic synthetic candidate_ref for controls with no ref_candidate_id.
    Derives cand_<sha8> from the control_id."""
    digest = hashlib.sha256(control_id.encode("utf-8")).hexdigest()[:8]
    return f"cand_{digest}"


def load_controls() -> list[dict]:
    """Return the control packs.

    All controls have expected_verdict="reject" (the summary's reject-based control
    rejection rate relies on this), so no per-control expectation metadata is returned.
    """
    controls_raw = _load_jsonl(CONTROL_MANIFEST_PATH)
    packs = []

    for ctl in controls_raw:
        control_id = ctl["control_id"]
        ref_cid = ctl.get("ref_candidate_id")
        candidate_ref = ref_cid if ref_cid else _synthetic_cand_ref(control_id)
        family = ctl["mimicked_family"]
        text = ctl["text"]
        # Context is empty for controls (no resolved source context)
        context_before = ""
        context_after = ""
        source_id = ctl.get("ref_source_id") or f"ctl_synthetic_{control_id}"
        run_id = f"control_{control_id}"

        pack = make_pack(
            candidate_ref=candidate_ref,
            source_id=source_id,
            run_id=run_id,
            family=family,
            candidate_text=text,
            context_before=context_before,
            context_after=context_after,
            is_control=True,
        )
        pack["control_id"] = control_id
        packs.append(pack)

    return packs


# ── Response parser ────────────────────────────────────────────────────────────
def _extract_first_json_object(text: str) -> Optional[str]:
    """Extract the first balanced top-level {...} substring from text.

    String-aware: braces that appear inside a quoted JSON string value (e.g. a
    rationale containing a literal '}') do not change the brace depth, and a quote
    preceded by a backslash escape does not open/close a string. Without this, a
    rationale like "use } carefully" would close the object early and truncate it."""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def parse_response(raw_text: str) -> tuple[Optional[dict], Optional[str]]:
    """Parse a model response into (parsed_dict, error_reason).
    Returns (dict, None) on success, (None, reason) on failure.
    Deterministic cleanup only — no retry, no LLM re-call.
    """
    text = raw_text.strip()
    # Strip code fences: ```json ... ``` or ``` ... ```
    fence_match = re.match(r"^```(?:json)?\s*([\s\S]*?)\s*```$", text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1).strip()
    # Extract first balanced top-level { }
    json_str = _extract_first_json_object(text)
    if json_str is None:
        return None, "no_json_object_found"
    try:
        obj = json.loads(json_str)
    except json.JSONDecodeError as exc:
        return None, f"json_parse_error: {exc}"
    if not isinstance(obj, dict):
        return None, f"parsed_value_not_dict: {type(obj).__name__}"
    return obj, None


# ── Vote builder ───────────────────────────────────────────────────────────────
def build_vote(pack: dict, parsed_vote: dict, used_model: str,
               norm: dict, seq: int, run_short_token: str, lane: str) -> dict:
    """Assemble the full llm_vote record from pack + parsed response."""
    pid = pack["pack_id"]
    vote_id = _ids.llm_vote_id(run_short_token, pid, seq)

    # token_counts: prompt = input + cached_input, completion = output + reasoning
    prompt_tokens = (norm.get("input") or 0) + (norm.get("cached_input") or 0)
    completion_tokens = (norm.get("output") or 0) + (norm.get("reasoning") or 0)

    return {
        "llm_vote_id": vote_id,
        "candidate_ref": pack["candidate_ref"],
        "source_id": pack["source_id"],
        "run_id": pack["run_id"],
        "pack_id": pid,
        "prompt_id": PROMPT_ID,
        "prompt_version": PROMPT_VERSION,
        "model": used_model,
        "lane": lane,
        # Provenance label only: it records that the prompt carries an injection-guard
        # clause, NOT a per-vote verification. The real injection control is the planted
        # control bar (it measures whether adversarial spans actually flip the verdict).
        "injection_guard": "present",
        "attempt": 1,
        "tier": "base",
        "is_control": pack["is_control"],
        "authority_level": "llm_vote",
        "review_state": "llm_reviewed",
        "vote": parsed_vote,
        "token_counts": {
            "prompt": prompt_tokens,
            "completion": completion_tokens,
        },
    }


# ── Summary builder ────────────────────────────────────────────────────────────
def build_summary(votes: list[dict], malformed: list[dict],
                  lane_names: list[str]) -> dict:
    """Build stage4_summary.json content."""
    summary = {}

    # Per-lane stats
    for lane in lane_names:
        lane_votes = [v for v in votes if (v.get("lane") or _lane_of_model(v["model"])) == lane]
        lane_malformed = [m for m in malformed
                          if m.get("lane") == lane and m.get("failure_type") == "model_malformed"]
        lane_failures = [m for m in malformed
                         if m.get("lane") == lane and m.get("failure_type") == "lane_failure"]

        verdict_dist: dict[str, int] = defaultdict(int)
        for v in lane_votes:
            verdict_dist[v["vote"]["verdict"]] += 1

        n_votes = len(lane_votes)
        n_uncertain = verdict_dist.get("uncertain", 0)
        uncertain_rate = n_uncertain / n_votes if n_votes > 0 else None

        # Control rejection rate
        control_votes = [v for v in lane_votes if v["is_control"]]
        n_controls = len(control_votes)
        # All controls have expected_verdict="reject"
        n_correctly_rejected = sum(
            1 for v in control_votes
            if v["vote"]["verdict"] == "reject"
        )
        control_rejection_rate = n_correctly_rejected / n_controls if n_controls > 0 else None
        control_bar_passed = (control_rejection_rate is not None and
                               control_rejection_rate >= CONTROL_BAR)

        token_counts_total = {
            "prompt": sum(v["token_counts"]["prompt"] for v in lane_votes),
            "completion": sum(v["token_counts"]["completion"] for v in lane_votes),
        }

        n_attempted = n_votes + len(lane_malformed)
        summary[lane] = {
            "votes": n_votes,
            "model_malformed": len(lane_malformed),
            "lane_failures": len(lane_failures),
            "malformed_rate": (len(lane_malformed) / n_attempted if n_attempted else None),
            "verdict_distribution": dict(verdict_dist),
            "uncertain_rate": uncertain_rate,
            "control_votes": n_controls,
            "control_rejection_rate": control_rejection_rate,
            "control_bar_0_90_passed": control_bar_passed,
            "token_counts_total": token_counts_total,
        }

    # Cross-lane agreement on shared candidates (non-control)
    if len(lane_names) > 1:
        # Group non-control votes by candidate_ref
        by_cand: dict[str, dict[str, str]] = defaultdict(dict)
        for v in votes:
            if not v["is_control"]:
                lane = v.get("lane") or _lane_of_model(v["model"])
                by_cand[v["candidate_ref"]][lane] = v["vote"]["verdict"]

        shared = {cref: verdicts for cref, verdicts in by_cand.items()
                  if len(verdicts) == len(lane_names)}
        n_shared = len(shared)
        n_agree = sum(1 for verdicts in shared.values()
                      if len(set(verdicts.values())) == 1)
        summary["cross_lane_agreement"] = {
            "shared_candidates": n_shared,
            "fully_agreed": n_agree,
            "agreement_rate": (n_agree / n_shared if n_shared > 0 else None),
        }

    return summary


def _lane_of_model(model: str) -> str:
    """Map a model string back to a lane name for summary grouping + the resume done-set.

    Resolves against LANE_CONFIGS (exact, then substring to absorb vendor-returned dated
    ids like 'claude-haiku-4-5-20251001'). The anthropic lane is listed FIRST, so the model
    shared by both shipped lanes (claude-haiku-4-5: anthropic & claude) resolves to the
    direct anthropic lane - correct for the shipped usage where the CLI lane is the fallback.
    """
    for lane, cfg in LANE_CONFIGS.items():            # exact match wins
        if cfg.get("model") == model:
            return lane
    for lane, cfg in LANE_CONFIGS.items():            # then substring (dated ids)
        m = cfg.get("model")
        if m and m in model:
            return lane
    # Last-resort family heuristic (only for unconfigured model strings, e.g. a strong
    # adjudicator model like claude-sonnet-4-6 that no base lane is configured with).
    model_lower = model.lower()
    if any(k in model_lower for k in ("claude", "haiku", "sonnet", "opus")):
        return "claude"
    return model  # use model string as lane name if unrecognized


# ── Batch processing ───────────────────────────────────────────────────────────
def _emit(fh, record: dict, lst: list):
    """Append a record to its jsonl file (flushed immediately) and the in-memory list.
    Incremental + flushed so an interrupted/killed run keeps every completed result."""
    fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    fh.flush()
    os.fsync(fh.fileno())
    lst.append(record)


# Placeholders the deterministic pack builder must fill; if any survive into the dispatched
# prompt the template drifted (a renamed/removed token) and the payload never interpolated.
_PACK_PLACEHOLDERS = ("{{family}}", "{{candidate_text}}",
                      "{{context_before}}", "{{context_after}}")


def _assert_prompt_payload(pack: dict, prompt: str) -> None:
    """Fail fast if the assembled candidate_evaluate prompt carries no candidate payload.

    On 2026-06-12 ~35 CC threads were dispatched the bare prompt-template TITLE
    ("# Prompt: candidate_evaluate (v1)") with nothing interpolated, burning an agentic
    tool-loop each on an empty task. A correctly built pack ALWAYS contains its own
    candidate_text (fill_template inserts it verbatim) and has every {{placeholder}} filled;
    if either is false we refuse to dispatch rather than spend a call on a guaranteed-useless
    prompt. Raises ValueError — a systemic assembly bug should abort loudly, not log-and-continue
    its way through the whole batch. Runs in dry-run too, so a regression surfaces at zero spend."""
    pid = pack.get("pack_id")
    cref = pack.get("candidate_ref")
    cand = (pack.get("candidate_text") or "").strip()
    if not cand:
        raise ValueError(
            f"candidate_evaluate misfire: pack {pid} ({cref}) has empty candidate_text — "
            f"no payload to judge; refusing to dispatch.")
    if cand not in prompt:
        raise ValueError(
            f"candidate_evaluate misfire: assembled prompt for pack {pid} ({cref}) does not "
            f"contain its candidate payload — looks like a bare template title, not a filled "
            f"pack (prompt_len={len(prompt)}); refusing to dispatch.")
    leaked = [p for p in _PACK_PLACEHOLDERS if p in prompt]
    if leaked:
        raise ValueError(
            f"candidate_evaluate misfire: assembled prompt for pack {pid} ({cref}) still has "
            f"unfilled placeholders {leaked} — the prompt template drifted; refusing to dispatch.")


def process_batch(batch: list[dict], lane_name: str, cfg: dict,
                  run_short_token: str, execute: bool,
                  votes_out: list, malformed_out: list,
                  seq_counter: list, votes_fh, malformed_fh,
                  timeout: Optional[int] = None, call_cwd: Optional[str] = None):
    """Process one batch of packs for one lane. Writes each result incrementally.

    malformed_log rows carry a `failure_type`:
      - "lane_failure"   transport/CLI failure (nonzero rc, timeout=rc124, empty text) —
                         NOT model output; eligible for re-attempt on resume.
      - "model_malformed" the model replied but the reply could not be parsed/validated —
                         a real malformed-rate data point; NOT retried (S4 zero-retries).
    """
    if len(batch) > 1:
        raise NotImplementedError(
            "batch>1 needs a registered multi-candidate wrapper prompt — not yet registered"
        )
    pack = batch[0]
    prompt = pack["prompt"]

    # BUG-2 guard: final gate before dispatch — refuse to send a misfired prompt that is just the
    # template title with no interpolated payload (see _assert_prompt_payload). make_pack already
    # validates at build time (covers the dry-run); this re-check guards any pack reaching dispatch.
    _assert_prompt_payload(pack, prompt)

    if not execute:
        return  # dry-run: no calls

    fn = cfg["fn"]
    model = cfg["model"]
    effort = cfg["effort"]
    sandbox = cfg["sandbox"]
    extra = cfg.get("kwargs", {})

    def _fail(failure_type, reason, used_model=None, raw_text=""):
        _emit(malformed_fh, {
            "candidate_ref": pack["candidate_ref"],
            "pack_id": pack["pack_id"],
            "lane": lane_name,
            "model": used_model or model,
            "failure_type": failure_type,
            "raw_text": raw_text,
            "reason": reason,
        }, malformed_out)

    try:
        raw_text, norm, used_model, returncode = fn(
            prompt, model, effort, sandbox, timeout=timeout, cwd=call_cwd, **extra)
    except Exception as exc:
        _fail("lane_failure", f"lane_exception: {exc}")
        return

    # Transport/CLI failure (incl. timeout -> rc 124) or empty body: not model output.
    if returncode != 0 or not (raw_text or "").strip():
        _fail("lane_failure",
              f"returncode={returncode}, empty={not (raw_text or '').strip()}",
              used_model, raw_text or "")
        return

    parsed_vote, parse_error = parse_response(raw_text)
    if parsed_vote is None:
        _fail("model_malformed", parse_error, used_model, raw_text)
        return

    seq = seq_counter[0]
    seq_counter[0] += 1
    vote_record = build_vote(pack, parsed_vote, used_model, norm, seq, run_short_token, lane_name)

    errors = validate_vote(vote_record)
    if errors:
        _fail("model_malformed", f"schema_invalid: {'; '.join(errors)}", used_model, raw_text)
        return

    _emit(votes_fh, vote_record, votes_out)


# ── Main (orchestrator + extracted helpers) ─────────────────────────────────────
def _parse_buckets(buckets_arg: str) -> tuple:
    """Resolve the --buckets arg into a tuple of routing buckets to judge."""
    arg = buckets_arg.strip().lower()
    if arg == "all":
        return ("high", "medium", "low")
    return tuple(b.strip() for b in arg.split(",") if b.strip())


def _load_all_packs(args, run_root: pathlib.Path) -> tuple[list[dict], list[dict], tuple]:
    """Load candidate + control packs and print the dry-run-style report.

    Returns (candidate_packs, control_packs, buckets). buckets is returned so the
    caller's label matches the buckets actually loaded.
    """
    print("Loading routed candidates...", flush=True)
    buckets = _parse_buckets(args.buckets)
    candidate_packs = load_routed_candidates(run_root, limit=args.limit, buckets=buckets)
    # Label the buckets actually in use (was hardcoded "high+medium", wrong under --buckets all).
    print(f"  Routed candidates ({'+'.join(buckets)}): {len(candidate_packs)}")

    print("Loading controls...", flush=True)
    control_packs = load_controls()
    if args.max_controls is not None:
        control_packs = control_packs[:args.max_controls]
    print(f"  Controls: {len(control_packs)}")

    family_counts: Counter = Counter()
    for p in candidate_packs:
        family_counts[p["family"]] += 1
    print("  Per-family breakdown (candidates):")
    for fam, cnt in sorted(family_counts.items()):
        print(f"    {fam}: {cnt}")

    return candidate_packs, control_packs, buckets


def _preload_resume_state(votes_path: pathlib.Path, malformed_path: pathlib.Path):
    """Preload existing votes + model_malformed rows for resume.

    Returns (all_votes, all_malformed, done). `done` is a per-(candidate,lane) set so we
    never re-judge a recorded result (preserves S4 zero-retries). lane_failure rows are
    NOT in `done` -> they get re-attempted on resume (transport failure, not model output).
    """
    all_votes: list[dict] = []
    all_malformed: list[dict] = []
    done: set[tuple[str, str]] = set()
    if votes_path.exists():
        for v in _load_jsonl(votes_path):
            all_votes.append(v)
            done.add((v["candidate_ref"], v.get("lane") or _lane_of_model(v["model"])))
    if malformed_path.exists():
        for m in _load_jsonl(malformed_path):
            all_malformed.append(m)
            if m.get("failure_type") == "model_malformed":
                # Mirror the vote path: derive the lane the same way so the done-set key matches.
                done.add((m["candidate_ref"], m.get("lane") or _lane_of_model(m["model"])))
    if done:
        print(f"Resume: {len(done)} (candidate,lane) results already recorded — skipping those.",
              flush=True)
    return all_votes, all_malformed, done


def _run_execution(all_packs, active_lanes, run_short_token, args,
                   votes_path, malformed_path, all_votes, all_malformed, done):
    """Run the live execution loop across the active lanes, writing results incrementally."""
    print(f"\nExecuting on lanes: {active_lanes} (timeout {args.timeout}s/call)", flush=True)

    # Isolated EMPTY cwd for every CLI call so the agentic lane can't explore the repo.
    call_cwd = tempfile.mkdtemp(prefix="stage4_isolated_")

    votes_fh = open(votes_path, "a", encoding="utf-8")
    malformed_fh = open(malformed_path, "a", encoding="utf-8")
    try:
        # ONE monotonic seq counter shared across ALL lanes. llm_vote_id is
        # (run_short, pack_id, seq) with NO model/lane component, and pack_id is identical
        # across lanes by design (content-addressed, model excluded — see ids.pack_id). A
        # per-lane counter would make lane-A pack[i] and lane-B pack[i] share (pack_id, seq)
        # -> IDENTICAL llm_vote_id. A shared counter gives lane-A 0..N-1, lane-B N..2N-1, etc.
        # On resume it continues past every already-recorded vote — seq increments only on a
        # successful vote, so len(all_votes) is exactly the next unused seq.
        seq_counter = [len(all_votes)]
        for lane_name in active_lanes:
            cfg = LANE_CONFIGS[lane_name]
            print(f"\n  Lane: {lane_name} (model={cfg['model']})", flush=True)

            for i, pack in enumerate(all_packs):
                if (pack["candidate_ref"], lane_name) in done:
                    continue
                process_batch(
                    [pack], lane_name, cfg, run_short_token,
                    execute=True,
                    votes_out=all_votes,
                    malformed_out=all_malformed,
                    seq_counter=seq_counter,
                    votes_fh=votes_fh,
                    malformed_fh=malformed_fh,
                    timeout=args.timeout,
                    call_cwd=call_cwd,
                )
                if (i + 1) % 10 == 0 or (i + 1) == len(all_packs):
                    print(f"    [{lane_name}] {i + 1}/{len(all_packs)} packs processed", flush=True)
    finally:
        votes_fh.close()
        malformed_fh.close()


def main():
    ap = argparse.ArgumentParser(
        description="Stage-4 evaluator runner for the Discourse Spine.")
    ap.add_argument("--run-root", required=True,
                    help="Path to the run root dir, e.g. runs/demo")
    ap.add_argument("--limit", type=int, default=None,
                    help="Cap number of real candidates (for smoke runs)")
    ap.add_argument("--max-controls", type=int, default=None,
                    help="Cap number of hidden controls (for smoke runs; default all 30)")
    ap.add_argument("--buckets", default="high,medium",
                    help="Comma-separated routing buckets to judge (default: high,medium, the "
                         "routed set). Pass 'all' to judge every candidate including the 'low' "
                         "bucket the router filters out (validates router recall).")
    ap.add_argument("--lane",
                    choices=["claude", "anthropic"], default="claude",
                    help="Which lane to run (default: claude). claude = subscription CLI (no "
                         "metered spend); anthropic = direct Messages API (key from env).")
    ap.add_argument("--timeout", type=int, default=120,
                    help="Per-call timeout in seconds (default 120; guards against CLI hangs)")
    ap.add_argument("--fresh", action="store_true",
                    help="Delete existing outputs and start over (default: resume — skip done packs)")
    ap.add_argument("--execute", action="store_true",
                    help="Make live model calls and write outputs. Default: dry-run only.")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    run_root = pathlib.Path(args.run_root)
    if not run_root.is_absolute():
        run_root = pathlib.Path.cwd() / run_root
    run_root = run_root.resolve()

    if not run_root.exists():
        print(f"ERROR: run-root does not exist: {run_root}", file=sys.stderr)
        sys.exit(1)

    # run_short_token: derived from the run folder name (not candidate run_id sub-path)
    run_short_token = _ids.run_short(run_root.name)   # e.g. 'demo'

    active_lanes = [args.lane]

    candidate_packs, control_packs, _buckets = _load_all_packs(args, run_root)
    all_packs = candidate_packs + control_packs
    total_packs = len(all_packs)
    print(f"  Total packs to evaluate: {total_packs} per lane, "
          f"{total_packs * len(active_lanes)} across all lanes")

    if not args.execute:
        print("\nDRY-RUN complete (no model calls made). Pass --execute to run live.")
        return

    # ── Execute ────────────────────────────────────────────────────────────────
    votes_path = run_root / "llm_votes.jsonl"
    malformed_path = run_root / "malformed_log.jsonl"
    summary_path = run_root / "stage4_summary.json"

    if args.fresh:
        for p in (votes_path, malformed_path):
            if p.exists():
                p.unlink()
        print("--fresh: cleared existing outputs.", flush=True)

    all_votes, all_malformed, done = _preload_resume_state(votes_path, malformed_path)

    _run_execution(all_packs, active_lanes, run_short_token, args,
                   votes_path, malformed_path, all_votes, all_malformed, done)

    n_lane_fail = sum(1 for m in all_malformed if m.get("failure_type") == "lane_failure")
    n_model_malformed = sum(1 for m in all_malformed if m.get("failure_type") == "model_malformed")
    print(f"\nVotes: {len(all_votes)} -> {votes_path}")
    print(f"Malformed log: {len(all_malformed)} rows "
          f"({n_model_malformed} model_malformed, {n_lane_fail} lane_failure) -> {malformed_path}")

    # ── Summary ──────────────────────────────────────────────────────────────────
    summary = build_summary(all_votes, all_malformed, active_lanes)
    with open(summary_path, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    print(f"Wrote summary -> {summary_path}")
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
