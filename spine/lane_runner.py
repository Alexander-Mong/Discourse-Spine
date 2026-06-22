#!/usr/bin/env python3
"""
lane_runner.py - run a model lane, capture per-call token usage from the lane's OWN
json output, and append one normalized row to token-ledger.jsonl. Actuals, not estimates.

Two shipped lanes:
  claude     headless `claude -p ... --output-format json` (subscription auth, no API key)
  anthropic  Anthropic Messages API over HTTP (key from ANTHROPIC_API_KEY)

Adopted 2026-06-12 (ADR-light). Named bottleneck: model token/cost tracking for
evidence-governed dispatch. Field mapping lives in WORKER_PROFILES.md.

claude lane: usage from `.usage` (input/output/cache_read/cache_creation), model string
from the `.modelUsage` key. Subscription auth; the CLI also reports total_cost_usd, so
confirm whether `claude -p` draws on the Max plan or a separate credit lane before bulk runs.

Usage:
  python lane_runner.py claude    "PROMPT" [--model M]
  python lane_runner.py anthropic "PROMPT" [--model M]

  --ledger PATH   override ledger file (default: ./token-ledger.jsonl next to this script)
  --tag  STR      free-form label written into the row (e.g. a run id)
  --quiet         do not echo the model's text response to stdout

Notes:
  - Subscription auth (claude) or env key (anthropic); this script never hardcodes a key.
  - Strips CLAUDECODE from the child env so the CLI can nest under Claude Code.
  - Exit code mirrors the underlying lane's exit/status code.
"""
import argparse
import json
import os
import pathlib
import subprocess
import sys
from datetime import datetime, timezone

LEDGER_DEFAULT = pathlib.Path(__file__).resolve().parent / "token-ledger.jsonl"


def _child_env():
    env = dict(os.environ)
    env.pop("CLAUDECODE", None)          # let the CLI nest under Claude Code
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def _isum(*xs):
    """Sum the integer args, ignoring None/non-int; return None if none are ints."""
    vals = [x for x in xs if isinstance(x, int)]
    return sum(vals) if vals else None


class _Timeout:
    """Stand-in proc for a timed-out call: returncode 124 (matches `timeout(1)`)."""
    returncode = 124
    stdout = ""
    stderr = "timeout"


def _spawn(cmd, timeout=None, cwd=None, input_text=None):
    """Run a CLI portably. On Windows the npm bins are .cmd shims that CreateProcess
    won't launch directly, so route through the shell with proper quoting.
    A per-call `timeout` (seconds) guards against a CLI hanging forever. On timeout returns
    a returncode-124 stand-in rather than raising, so callers log+continue (no silent stall).
    `cwd` runs the child in an isolated dir - pass an EMPTY dir to stop these agentic
    CLIs from exploring the repo instead of answering the prompt.
    `input_text` is piped to the child's STDIN - multi-line prompts MUST go here, not on
    argv: shell=True routes argv through cmd.exe, which breaks a multi-line arg at newlines
    so the CLI receives a truncated prompt."""
    kw = dict(env=_child_env(), capture_output=True, text=True,
              encoding="utf-8", errors="replace", timeout=timeout, cwd=cwd,
              input=input_text)
    try:
        if os.name == "nt":
            return subprocess.run(subprocess.list2cmdline(cmd), shell=True, **kw)
        return subprocess.run(cmd, **kw)
    except subprocess.TimeoutExpired:
        return _Timeout()


def run_claude(prompt, model, effort, sandbox, timeout=None, cwd=None, no_tools=False):  # effort/sandbox unused; parity
    cmd = ["claude", "-p", "--output-format", "json"]   # prompt via STDIN
    if no_tools:
        # Disable all tools so claude -p answers the prompt directly instead of going
        # agentic in the cwd. Pair with an empty cwd.
        cmd += ["--tools", ""]
    if model:
        cmd += ["--model", model]
    proc = _spawn(cmd, timeout=timeout, cwd=cwd, input_text=prompt)

    data = {}
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        pass

    text = data.get("result", "") if isinstance(data, dict) else ""
    usage = data.get("usage", {}) if isinstance(data, dict) else {}

    inp = usage.get("input_tokens")
    cache_read = usage.get("cache_read_input_tokens")
    cache_creation = usage.get("cache_creation_input_tokens")
    out = usage.get("output_tokens")
    norm = {
        "input": inp,
        "cached_input": _isum(cache_read, cache_creation),
        "output": out,
        "reasoning": None,                                  # claude does not report a separate count
        "total": _isum(inp, cache_read, cache_creation, out),
    }

    # model: prefer the exact dated id claude reports; pick the modelUsage key with the most output.
    used_model = model
    mu = data.get("modelUsage") if isinstance(data, dict) else None
    if isinstance(mu, dict) and mu:
        used_model = max(mu.items(),
                         key=lambda kv: (kv[1] or {}).get("outputTokens", 0))[0]
    return text, norm, used_model, proc.returncode


# Direct-inference adapter (no agent harness; one call; key from env) ----------------
DEFAULT_MAX_TOKENS = 16384  # anthropic requires max_tokens. Votes (~250) stop far short of this;
# lens reads on dense/long transcripts emit large candidate arrays and were truncating at 2048
# (unterminated JSON -> anchor parse crash), so the cap is raised to fit a full lens read. It is a
# ceiling, not a target - small outputs are unaffected.


def _empty_norm():
    return {"input": None, "cached_input": None, "output": None,
            "reasoning": None, "total": None}


def run_anthropic(prompt, model, effort, sandbox, timeout=None, cwd=None):
    """Anthropic Messages API. Key: ANTHROPIC_API_KEY. Models: claude-haiku-4-5 /
    claude-sonnet-4-6 / claude-opus-4-8."""
    import requests  # lazy: the CLI-only (claude) path needs no requests installed
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return "missing ANTHROPIC_API_KEY", _empty_norm(), model, 1
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01",
               "content-type": "application/json"}
    #: Opus 4.8 deprecates `temperature` (HTTP 400 "temperature is deprecated for
    # this model"). Omit it for opus-4-8 (take the model default); haiku-4-5 /
    # sonnet-4-6 still accept temperature:0, so keep their determinism unchanged.
    body = {"model": model, "max_tokens": DEFAULT_MAX_TOKENS,
            "messages": [{"role": "user", "content": prompt}]}
    if "opus-4-8" not in (model or ""):
        body["temperature"] = 0
    try:
        r = requests.post("https://api.anthropic.com/v1/messages",
                          headers=headers, json=body, timeout=timeout)
    except requests.exceptions.Timeout:
        return "", _empty_norm(), model, 124
    except requests.exceptions.RequestException as exc:
        return f"transport_error: {exc}", _empty_norm(), model, 1
    if r.status_code != 200:
        return r.text, _empty_norm(), model, r.status_code
    data = r.json()
    text = ""
    for p in (data.get("content") or []):
        if isinstance(p, dict) and p.get("type") == "text":
            text = p.get("text", "") or ""
            break
    usage = data.get("usage") or {}
    inp = usage.get("input_tokens")
    out = usage.get("output_tokens")
    cache_read = usage.get("cache_read_input_tokens")
    cache_creation = usage.get("cache_creation_input_tokens")
    cached = _isum(cache_read, cache_creation)
    norm = {"input": inp, "cached_input": cached, "output": out, "reasoning": None,
            "total": _isum(inp, cache_read, cache_creation, out)}
    return text, norm, data.get("model") or model, 0


LANES = {"claude": run_claude, "anthropic": run_anthropic}


def main():
    ap = argparse.ArgumentParser(description="Headless lane runner with token ledger.")
    ap.add_argument("lane", choices=sorted(LANES))
    ap.add_argument("prompt")
    ap.add_argument("--model")
    ap.add_argument("--effort", choices=["low", "medium", "high"])
    ap.add_argument("--sandbox", choices=["read-only", "workspace-write"], default="read-only")
    ap.add_argument("--ledger", default=str(LEDGER_DEFAULT))
    ap.add_argument("--tag", default=None)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    text, norm, used_model, rc = LANES[args.lane](
        args.prompt, args.model, args.effort, args.sandbox
    )

    row = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "lane": args.lane,
        "model": used_model,
        "reasoning_effort": None,
        "sandbox": None,
        "tag": args.tag,
        "exit": rc,
        **norm,
    }
    with open(args.ledger, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    if not args.quiet and text:
        print(text)
    # usage to stderr so stdout stays clean for piping the model response
    print(json.dumps({k: row[k] for k in
                      ("lane", "model", "input", "cached_input", "output",
                       "reasoning", "total", "exit")}, ensure_ascii=False),
          file=sys.stderr)
    sys.exit(rc)


if __name__ == "__main__":
    main()
