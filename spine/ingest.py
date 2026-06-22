"""ingest.py - Stage 1: SRT -> locked, hashed, sentence-segmented spine with dual anchors.

Deterministic, stdlib only. Produces, into runs/<run_id>/:
  - normalized_transcript.txt   the canonical text every char_span indexes into
  - source_lockfile.json        SHA-256 over transcript + metadata (tamper-evident)
  - units.jsonl                 one unit per sentence, dual anchors (char span + timecode)
  - quality_profile.json        attribution tier + segmentation_quality + fragment stats

Design rulings honored:
  - normalized transcript is rebuilt from CUE TEXT ONLY; timecodes are never inlined.
  - dual anchors: char_span (PRIMARY, relative to norm profile) + timecode (SECONDARY).
    Each survives what breaks the other; re-transcription breaks both -> new source version.
  - segmentation: punctuation sentence-mode (CP1 freeze). Quality profile records the call.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from bisect import bisect_right
from datetime import datetime, timezone
from pathlib import Path

import ids

NORMALIZATION_PROFILE = "norm_v0_1"

# Frozen segmentation-quality policy (CP1). These thresholds classify a transcript's
# punctuation/fragment profile; do NOT retune without bumping the normalization profile.
CLEAN_TERMINAL_MIN = 0.6   # min terminal-punctuation rate for "punctuated_clean"
CLEAN_FRAGMENT_MAX = 0.25  # max <4-word fragment rate for "punctuated_clean"
NOISY_TERMINAL_MIN = 0.3   # min terminal-punctuation rate for "punctuated_noisy"

# Sentence boundary: one or more terminal marks followed by whitespace or end-of-text.
_SENT_END = re.compile(r"[.!?]+(?=\s|$)")
_WS = re.compile(r"\s+")
# SRT timestamp line: 00:00:01,000 --> 00:00:04,000
_TIME_LINE = re.compile(
    r"(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})"
)


def _norm_cue_text(raw: str) -> str:
    """norm_v0_1: newlines->space, collapse whitespace, strip. Casing/punctuation preserved."""
    return _WS.sub(" ", raw.replace("\n", " ")).strip()


def parse_srt(text: str) -> list[dict]:
    """Parse SRT into cues: [{srt_index, start, end, text}]. Tolerant of BOM and blank lines."""
    text = text.lstrip("﻿")
    blocks = re.split(r"\r?\n\r?\n", text.strip())
    cues = []
    for block in blocks:
        lines = [ln for ln in block.splitlines() if ln.strip() != ""]
        if not lines:
            continue
        time_idx = next((i for i, ln in enumerate(lines) if _TIME_LINE.search(ln)), None)
        if time_idx is None:
            continue
        m = _TIME_LINE.search(lines[time_idx])
        # Index is the line above the timestamp if numeric, else positional.
        srt_index = None
        if time_idx >= 1 and lines[time_idx - 1].strip().isdigit():
            srt_index = int(lines[time_idx - 1].strip())
        if srt_index is None:
            srt_index = len(cues) + 1
        cue_text = _norm_cue_text(" ".join(lines[time_idx + 1:]))
        if cue_text == "":
            continue
        cues.append({"srt_index": srt_index, "start": m.group(1), "end": m.group(2), "text": cue_text})
    return cues


def build_transcript(cues: list[dict]) -> tuple[str, list[tuple[int, int]]]:
    """Concatenate normalized cue texts with single-space joins.
    Returns (transcript, cue_ranges) where cue_ranges[i] = (char_start, char_end) of cue i."""
    parts, ranges, pos = [], [], 0
    for i, cue in enumerate(cues):
        if i > 0:
            parts.append(" ")
            pos += 1
        start = pos
        parts.append(cue["text"])
        pos += len(cue["text"])
        ranges.append((start, pos))
    return "".join(parts), ranges


def segment_sentences(transcript: str) -> list[tuple[int, int]]:
    """Char-accurate sentence spans over the transcript. Returns [(start, end)].
    Punctuation sentence-mode: split on terminal marks; trailing unterminated tail is a sentence."""
    spans, cursor = [], 0
    for m in _SENT_END.finditer(transcript):
        end = m.end()
        # advance start past leading whitespace
        start = cursor
        while start < end and transcript[start].isspace():
            start += 1
        if start < end:
            spans.append((start, end))
        cursor = end
    # trailing tail with no terminal punctuation
    tail_start = cursor
    while tail_start < len(transcript) and transcript[tail_start].isspace():
        tail_start += 1
    if tail_start < len(transcript):
        spans.append((tail_start, len(transcript)))
    return spans


def _cue_for_offset(offset: int, cue_ranges: list[tuple[int, int]]) -> int:
    """Return the cue index of the nearest cue whose start <= offset (index 0 if offset
    precedes all cues). cue_ranges from build_transcript are contiguous and sorted by start,
    so the nearest-preceding-start cue is exactly the containing cue when offset is inside one.
    bisect over the start offsets replaces the former O(n) linear scan with identical results."""
    starts = [s for s, _ in cue_ranges]
    pos = bisect_right(starts, offset) - 1
    return pos if pos > 0 else 0


def assess_quality(transcript: str, sent_spans, attribution_tier: str):
    """Compute segmentation_quality + fragment stats. CP1: sentence-mode is locked."""
    n = len(sent_spans)
    frag = 0
    terminal = 0
    for s, e in sent_spans:
        words = transcript[s:e].split()
        if len(words) < 4:
            frag += 1
        if transcript[s:e].rstrip()[-1:] in ".!?":
            terminal += 1
    frag_rate = round(frag / n, 4) if n else 0.0
    terminal_rate = round(terminal / n, 4) if n else 0.0
    if terminal_rate >= CLEAN_TERMINAL_MIN and frag_rate <= CLEAN_FRAGMENT_MAX:
        seg_quality = "punctuated_clean"
    elif terminal_rate >= NOISY_TERMINAL_MIN:
        seg_quality = "punctuated_noisy"
    else:
        seg_quality = "unpunctuated"
    return {
        "attribution_tier": attribution_tier,
        "segmentation_quality": seg_quality,
        "segmentation_mode": "sentence",
        "sentence_count": n,
        "fragment_rate_lt4w": frag_rate,
        "terminal_punctuation_rate": terminal_rate,
    }


def _sha256(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def ingest(srt_path: str, source_id_: str, run_id: str, out_root: str = "runs",
           attribution_tier: str = "D"):
    srt_path = Path(srt_path)
    raw = srt_path.read_text(encoding="utf-8", errors="strict")
    cues = parse_srt(raw)
    if not cues:
        raise ValueError(f"No cues parsed from {srt_path}")

    transcript, cue_ranges = build_transcript(cues)
    sent_spans = segment_sentences(transcript)
    quality = assess_quality(transcript, sent_spans, attribution_tier)

    run_dir = Path(out_root) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    # Canonical normalized transcript (the artifact every char_span indexes into).
    (run_dir / "normalized_transcript.txt").write_text(transcript, encoding="utf-8")

    # Lockfile: hash transcript + canonical metadata.
    transcript_hash = _sha256(transcript)
    meta = {
        "source_id": source_id_,
        "srt_filename": srt_path.name,
        "normalization_profile": NORMALIZATION_PROFILE,
        "cue_count": len(cues),
        "char_count": len(transcript),
        "sentence_count": len(sent_spans),
    }
    meta_hash = _sha256(json.dumps(meta, sort_keys=True, ensure_ascii=False))
    lockfile = {
        "source_id": source_id_,
        "run_id": run_id,
        "normalization_profile": NORMALIZATION_PROFILE,
        "transcript": {"sha256": transcript_hash, "path": "normalized_transcript.txt"},
        "metadata": {"sha256": meta_hash, **meta},
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    (run_dir / "source_lockfile.json").write_text(
        json.dumps(lockfile, indent=2, ensure_ascii=False), encoding="utf-8")

    # Units with dual anchors.
    units = []
    for (s, e) in sent_spans:
        ci = _cue_for_offset(s, cue_ranges)
        cue = cues[ci]
        units.append({
            "unit_id": ids.unit_id(source_id_, s),
            "source_id": source_id_,
            "run_id": run_id,
            "method": {"stage": "ingest", "normalization_profile": NORMALIZATION_PROFILE},
            "anchor": {
                "char_span": [s, e],
                "timecode": {"start": cue["start"], "end": cue["end"], "srt_index": cue["srt_index"]},
            },
            "text": transcript[s:e],
            "segmentation_quality": quality["segmentation_quality"],
            "source_hash_ref": "source_lockfile.json#transcript.sha256",
            "authority_level": "source",
            "review_state": "n/a",
        })
    with (run_dir / "units.jsonl").open("w", encoding="utf-8") as fh:
        for u in units:
            fh.write(json.dumps(u, ensure_ascii=False) + "\n")

    quality_profile = {"source_id": source_id_, "run_id": run_id, **quality}
    (run_dir / "quality_profile.json").write_text(
        json.dumps(quality_profile, indent=2, ensure_ascii=False), encoding="utf-8")

    return {"units": len(units), "cues": len(cues), "run_dir": str(run_dir),
            "segmentation_quality": quality["segmentation_quality"]}


def main():
    ap = argparse.ArgumentParser(description="Stage 1 ingest: SRT -> units + lockfile.")
    ap.add_argument("srt_path")
    ap.add_argument("source_id")
    ap.add_argument("run_id")
    ap.add_argument("--out-root", default="runs")
    ap.add_argument("--attribution-tier", default="D")
    args = ap.parse_args()
    result = ingest(args.srt_path, args.source_id, args.run_id, args.out_root, args.attribution_tier)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
