#!/usr/bin/env python3
"""Deterministic jargon / vernacular dictionary builder — v1.2.

v1.2 cleanup pass for the jargon dictionary:
  - LEMMA: merge inflectional/possessive variants onto a canonical headword (startups->startup).
  - TERM_STOP: drop curated junk unigrams (cetera, panelists, ...) — still deterministic, no LLM.
  - GENERIC_PHRASES extended with MC/panel scaffolding + geographic branding.
  - blind_spots.json: probe expected-but-absent terms (RAG/moat/fine-tuning/Claude...) with the cause.


Reads ingested `units.jsonl` (one per source) and builds a domain jargon dictionary:
  - F1/F9  word + n-gram frequency
  - F8     KEYNESS = distinctiveness vs general English (wordfreq baseline)
  - F10    collocations (multi-word jargon), filtered to distinctive phrases
  - KWIC   concordance lines per term, source-anchored
  - Term Discourse Cards (F12): freq, cross-panel spread, keyness, category, collocates, KWIC

Three deterministic signals separate jargon from noise:
  1. KEYNESS  — log10(corpus_freq / general_english_freq). High = used here far more than in
                everyday English. (wordfreq general-English baseline; offline, no API.)
  2. CROSS-PANEL SPREAD — a term must appear in >= MIN_DOCFREQ panels to enter the dictionary
                (filters one-off personal names; keeps shared vernacular).
  3. CAPITALIZATION RATIO — fraction of source occurrences that are capitalized. High ratio =>
                proper noun (company/product/person); low => common-noun concept. Used to SPLIT
                the dictionary into "named entities & acronyms" vs "concepts & vernacular".

Phrases are kept only if they contain >= 1 distinctive token (component keyness >= PHRASE_KEY_MIN),
which drops generic conversational collocations ("little bit") while keeping compound jargon
("generative ai"). Known limit: compounds of only-common words ("machine learning") can be dropped —
a true general-English PHRASE baseline (which wordfreq lacks) would fix this.

Deterministic: no wall-clock, no randomness, sorted outputs, fixed thresholds.

Usage:
    python build_jargon.py --source <dir-with-units.jsonl>   # single-source probe
    python build_jargon.py --run <run-dir>                   # whole corpus
    [--out jargon/out] [--topk 120] [--min-count 4] [--min-docfreq 2]
"""
import argparse
import json
import math
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

from wordfreq import word_frequency, zipf_frequency

STOP = set("""
a an the and or but if then else when while of to in on at by for with from into over under
again further is am are was were be been being have has had do does did doing will would shall
should can could may might must this that these those i you he she it we they me him her us them
my your his its our their mine yours hers ours theirs as so than too very just only also not no
nor about above below up down out off here there all any both each few more most other some such
what which who whom whose how why where because between through during before after their there's
i'm you're we're they're it's that's don't doesn't didn't can't won't i've you've we've gonna
wanna kind sort like really actually basically literally okay yeah yes know think going get got
one two three lot lots thing things stuff way ways going go went said say says saying right
mean means well much many being able re ve ll let lets us oh hey um uh
""".split())

WORD_RE = re.compile(r"[a-z][a-z'\-]*[a-z]|[a-z]")
CAP_RE = re.compile(r"\b[A-Z][A-Za-z'\-]*\b")      # capitalized words in original-case text
GEN_FLOOR = 1e-8        # general-English frequency floor for out-of-vocabulary terms
PHRASE_KEY_MIN = 0.5    # a phrase must contain >=1 token at least this distinctive
CAP_ENTITY_MIN = 0.6    # cap-ratio above this => classed as a named entity / acronym
MAX_KWIC = 4
MAX_COLLOC = 6
MAX_KWIC_CAND = 80       # candidates collected per term before scoring (the best of these is shown)

# KWIC example scoring (deterministic, v1.2): pick the cleanest, most *illustrative* occurrence — a
# self-contained sentence that shows the term in natural use — not the first one or the longest one.
# All signals are string-shape based (length, punctuation, opener, filler density). No randomness.
KWIC_LEN_LO, KWIC_LEN_HI = 45, 165     # one clean sentence fits this band; below = fragment, above = run-on
EXPLAIN_CUES = (" is ", " are ", " means ", " refers ", " because ", " so that ",
                " which ", " where ", " when you ", " lets you ", " allows ", " helps ")
# Hedge / filler sentence-openers — strong signal of rambling, low-information speech.
FILLER_OPEN_STRONG = ("i think", "i mean", "i guess", "you know", "one last thing", "i know i",
                      "to be honest", "honestly", "basically", "like,", "well,", "yeah", "okay",
                      "um", "uh", "i would say", "i'd say", "the thing is")
FILLER_OPEN_MILD = ("so ", "so,", "and ", "and so", "and then", "but ", "but,", "now,", "actually",
                    "right,", "look,", "i mean,")
# Filler phrases anywhere in the sentence — each occurrence dings the score.
FILLER_INLINE = (" like ", "you know", "kind of", "sort of", " i mean", " um ", " uh ", "right?",
                 "i think", " gonna ", " wanna ")
# Event-announcement / boilerplate tokens that make a unit a poor definitional example.
ANNOUNCE_CUES = ("hosting", "rsvp", "join us", "register", "panel on", "fireside",
                 "tuesday", "monday", "wednesday", "thursday", "friday", "saturday", "sunday",
                 "may the", "june the", "at the seaport", "doors open", "happy hour", "meetup")
# Speaker self-identification / introductions — clean sentences, but a publish-risk (name private
# founders) and a poor *definitional* example. De-prioritized so they don't win on cleanliness alone.
SELFID_CUES = ("my name is", "founder and ceo", "founder and cto", "co-founder and", "cofounder and",
               "is the founder", "is the ceo", "is the co-founder", "is the cto", "welcome to the stage",
               "please welcome", " ceo of ", " founder of ", " co-founder of ", "i'm the ceo",
               "i'm the founder", "i am the founder", "i am the ceo")
DATE_RE = re.compile(r"\b\d{1,2}(st|nd|rd|th)\b|\b\d{1,2}:\d{2}\b|\bpm\b|\bam\b")


def score_kwic(cand, term):
    """Deterministic quality score for one KWIC candidate (higher = better)."""
    text = cand["text"].strip()
    low = text.lower()
    n = len(text)
    score = 0.0

    # 1) One-clean-sentence length band; fragments and run-ons both penalized.
    if KWIC_LEN_LO <= n <= KWIC_LEN_HI:
        score += 3.0
    elif n < KWIC_LEN_LO:
        score -= 2.5 + (KWIC_LEN_LO - n) / 28.0
    else:
        score -= 1.0 + (n - KWIC_LEN_HI) / 90.0

    # 2) Complete-sentence shape: starts capitalized, ends with terminal punctuation.
    if text[:1].isupper():
        score += 0.8
    if text.endswith((".", "!")):
        score += 1.4
    elif text.endswith("?"):
        score += 0.2                       # a question can still illustrate, but reads less cleanly
    elif text.endswith(("...", "…")):
        score -= 2.2                       # truncated fragment
    else:
        score -= 1.2                       # no terminal punctuation => cut off

    # 3) Hedge / filler openers (rambling speech).
    if any(low.startswith(op) for op in FILLER_OPEN_STRONG):
        score -= 1.8
    elif any(low.startswith(op) for op in FILLER_OPEN_MILD):
        score -= 0.8

    # 4) Filler density anywhere in the sentence.
    score -= 0.7 * sum(low.count(f) for f in FILLER_INLINE)

    # 5) Run-on penalty: many clause/sentence breaks read as a ramble, not an example.
    if sum(low.count(c) for c in ".!?") >= 3:
        score -= 1.5
    if low.count(",") >= 4:
        score -= 0.9

    # 6) Event-announcement / date boilerplate, and speaker self-introductions.
    score -= 1.5 * sum(1 for cue in ANNOUNCE_CUES if cue in low)
    score -= 2.0 * sum(1 for cue in SELFID_CUES if cue in low)
    if DATE_RE.search(low):
        score -= 1.5

    # 7) Term placement: penalize term-as-first-word; reward real context on both sides + a cue.
    first = re.findall(r"[a-z][a-z'\-]*", low)
    if first and first[0] == term.split()[0]:
        score -= 1.2
    pos = low.find(term.lower())
    if pos != -1:
        if pos >= 12 and (n - (pos + len(term))) >= 12:
            score += 0.6
        window = low[max(0, pos - 60):pos + len(term) + 60]
        if any(cue in window for cue in EXPLAIN_CUES):
            score += 1.2
    return score


def best_kwic(cands, term):
    """Score candidates, return top MAX_KWIC; stable tie-break by source_id + char_span."""
    ordered = sorted(
        cands,
        key=lambda c: (-score_kwic(c, term), c["source_id"], tuple(c["char_span"])),
    )
    return ordered[:MAX_KWIC]

# Caption boilerplate: an ASR repetition-hallucination loop, e.g. "Speaker names and company
# names [and company names]*" repeated. ~145 units corpus-wide. Caught by prefix at analytics level;
# the upstream ingest fix is tracked separately.
def is_boilerplate(text):
    return text.strip().lower().startswith("speaker names and company names")

# ============================================================================
# ==== CORPUS-SPECIFIC TUNING - EDIT THESE FOR YOUR OWN DATA ====
# ----------------------------------------------------------------------------
# Everything in this block (GENERIC_PHRASES, LEMMA, TERM_STOP, BLIND_SPOT_PROBES)
# is hand-tuned to the bundled NYC / Tech Week corpus: it encodes that corpus's
# place names (new york, silicon valley, boston tech week), its MC/panel
# scaffolding, a curated founder/company stoplist for privacy, and known ASR
# mishears in these specific captions (e.g. Claude heard as "cloud", Llama as
# "lama"). These lists are NOT general-purpose.
#
# If you are cloning this tool to run on a DIFFERENT corpus, review and replace
# the contents of this block. Keeping the bundled values will silently filter
# the wrong phrases and probe blind spots that do not apply to your data.
# The scoring logic above and below this block is corpus-agnostic; only this
# block needs retuning.
# ============================================================================

# Generic conversational / place / event collocations that survive the keyness filter but aren't jargon.
GENERIC_PHRASES = {
    "little bit", "make sure", "making sure", "years ago", "et cetera", "every single", "long time",
    "same time", "every day", "totally agree", "couple of months", "first time", "feel like",
    "talk about", "talk little", "talk little bit", "great question", "makes sense", "come back",
    "want to make", "want to talk", "trying to figure", "what's happening", "even though", "we'll see",
    "tech week", "new york", "new york tech", "york tech", "new york tech week", "los angeles",
    # MC / panel-discourse scaffolding (v1.2): question-handoffs, applause, intros — not domain jargon.
    "last question", "next question", "good question", "round of applause", "raise your hand",
    "super excited", "super important", "let's talk", "company called", "company names", "quite bit",
    "want to build", "build something", "trying to build", "building ai", "company names and",
    # Geographic / event branding that rides in on a distinctive token but isn't vernacular.
    "silicon valley", "york tech week", "boston tech", "boston tech week", "startup boston",
    "boston area", "san francisco",
}

# v1.2 cleanup — deterministic lemma merge (variant -> canonical headword). Applied at tokenization
# so count, cross-panel doc-freq, and cap-ratio all aggregate correctly onto the canonical form.
# Conservative: only clear inflectional/possessive variants of the SAME term (no semantic merges).
LEMMA = {
    "startups": "startup", "startup's": "startup",
    "workflows": "workflow",
    "llms": "llm",
    "chatbots": "chatbot",
    "mcps": "mcp",
    "iterating": "iterate",
    "onboarded": "onboarding",
    "explainable": "explainability",
    "dashboarding": "dashboards",
    "bootstrapping": "bootstrapped",
    "openai's": "openai",
    "nvidia's": "nvidia",
    "ai's": "ai",
}

# v1.2 cleanup — unigrams to drop entirely. A curated stoplist (same class as STOP/GENERIC_PHRASES;
# still 100% deterministic — no LLM). Each is junk, not vernacular:
#   cetera        ASR mangle of "et cetera"
#   panelists     MC / panel scaffolding ("a round of applause for our panelists")
#   quote-unquote conversational filler
#   croasis       a single private founder's company (privacy) — 2 panels only
#   openclaw      ASR-suspect fragment — 2 panels only
TERM_STOP = {"cetera", "panelists", "quote-unquote", "croasis", "openclaw", "oftentimes"}

# v1.2 — terms a reader expects but the method MISSES, with the pipeline cause. Probed against the live
# corpus counts (below) and written to blind_spots.json so the page can show the gap honestly.
BLIND_SPOT_PROBES = [
    ("RAG", ["rag"], "homograph",
     "Keyness divides by general-English frequency; 'rag' is a common English word, so a real count is suppressed below the cut."),
    # NOTE: 'moat' is intentionally NOT probed here — it CLEARED the cut (keyness 1.653) and is in the
    # dictionary. The homograph penalty is graduated: a term right at the edge can survive (moat), while a
    # more common word (seed) cannot. Listing moat as a "miss" would contradict the grid.
    ("seed (round)", ["seed"], "homograph",
     "'seed' is everyday English; the funding sense can't be separated by frequency alone."),
    ("prompt", ["prompt"], "homograph",
     "Ordinary word ('prompt reply'); keyness can't tell the AI sense apart."),
    ("fine-tuning", ["fine-tuning", "tuning"], "tokenization",
     "Hyphenated/compound term; the pieces fall below threshold or never assemble into one headword."),
    ("product-market fit / PMF", ["pmf", "product-market"], "tokenization",
     "Acronym + hyphenated phrase; neither form reaches the count/keyness bar as a single unit."),
    ("open-source", ["open-source"], "tokenization",
     "Compound that splits or stays under the cut depending on punctuation in the caption."),
    ("context window", ["context window"], "tokenization",
     "A two-word term of art; only assembled as a bigram, and the unigram baseline can't score phrases well."),
    ("Claude (heard as 'cloud')", ["claude", "cloud"], "asr",
     "The proper noun is mis-transcribed ('cloud code' = Claude Code), so the real form fragments below threshold."),
    ("Llama (heard as 'lama')", ["llama", "lama"], "asr",
     "ASR mangles the model name; neither spelling clears the bar."),
]
# ==== END CORPUS-SPECIFIC TUNING ====


def tokenize(text):
    return [LEMMA.get(t, t) for t in WORD_RE.findall(text.lower()) if len(t) > 1]


def ngrams(tokens, n):
    return [tuple(tokens[i:i + n]) for i in range(len(tokens) - n + 1)]


def content_ngram(gram):
    return gram[0] not in STOP and gram[-1] not in STOP


def iter_units(path):
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)


def keyness(term, count, total_tokens):
    p_corpus = count / total_tokens
    p_gen = word_frequency(term, "en")
    oov = p_gen <= 0
    p_gen_eff = p_gen if not oov else GEN_FLOOR
    return round(math.log10(p_corpus / p_gen_eff), 3), oov, round(zipf_frequency(term, "en"), 2)


def _anchor_char_span(rec):
    """Return rec's anchor char_span, or None if the record lacks a valid one.

    Tolerant of malformed units.jsonl: every other field uses rec.get(...), so the
    anchor access must not be the one place that hard-crashes on a bad record.
    """
    anchor = rec.get("anchor")
    if not isinstance(anchor, dict):
        return None
    span = anchor.get("char_span")
    if span is None:
        return None
    return span


def count_pass(sources):
    """Pass 1: unigram/bigram/trigram counts, cross-panel doc-freq, capitalization.

    Returns (uni, bi, tri, df_uni, cap, total_tokens, n_units, n_sources).
    """
    uni, bi, tri = Counter(), Counter(), Counter()
    df_uni, cap = Counter(), Counter()
    total_tokens = n_units = n_sources = 0
    for src in sources:
        up = src / "units.jsonl"
        if not up.exists():
            continue
        n_sources += 1
        seen = set()
        for rec in iter_units(up):
            raw = rec.get("text", "")
            if is_boilerplate(raw):
                continue
            toks = tokenize(raw)
            n_units += 1
            total_tokens += len(toks)
            uni.update(t for t in toks if t not in STOP)
            bi.update(g for g in ngrams(toks, 2) if content_ngram(g))
            tri.update(g for g in ngrams(toks, 3) if content_ngram(g))
            cap.update(LEMMA.get(w.lower(), w.lower()) for w in CAP_RE.findall(raw) if w.lower() not in STOP)
            seen.update(t for t in toks if t not in STOP)
        for t in seen:
            df_uni[t] += 1
    return uni, bi, tri, df_uni, cap, total_tokens, n_units, n_sources


def score_terms(uni, df_uni, cap, total_tokens, min_count, min_df, topk):
    """Score unigrams by keyness + category, gated by count + cross-panel spread.

    Returns the top-k scored term dicts (sorted by keyness, count, term).
    """
    scored = []
    for term, count in uni.items():
        if count < min_count or df_uni[term] < min_df or term in TERM_STOP:
            continue
        key, oov, zipf = keyness(term, count, total_tokens)
        cap_ratio = round(cap.get(term, 0) / count, 2)
        category = "entity" if cap_ratio >= CAP_ENTITY_MIN else "concept"
        scored.append({"term": term, "count": count, "doc_freq": df_uni[term], "keyness": key,
                       "oov": oov, "zipf_general": zipf, "cap_ratio": cap_ratio, "category": category})
    scored.sort(key=lambda d: (-d["keyness"], -d["count"], d["term"]))
    return scored[:topk]


def extract_phrases(uni, bi, tri, total_tokens, topk):
    """Build the distinctive multi-word jargon list (>=1 token over PHRASE_KEY_MIN)."""
    def phrase_distinctive(phrase):
        best = -9.0
        for tok in phrase.split():
            if tok in STOP or tok not in uni:
                continue
            k, _, _ = keyness(tok, uni[tok], total_tokens)
            best = max(best, k)
        return best >= PHRASE_KEY_MIN

    def phrase_list(counter, floor):
        items = [(" ".join(g), c) for g, c in counter.items()
                 if c >= floor and " ".join(g) not in GENERIC_PHRASES and phrase_distinctive(" ".join(g))]
        items.sort(key=lambda kv: (-kv[1], kv[0]))
        return items

    phrases = phrase_list(bi, 3)[:topk] + phrase_list(tri, 3)[:topk // 2]
    phrases.sort(key=lambda kv: (-kv[1], kv[0]))
    return phrases


def collect_kwic(sources, top_terms, phrases):
    """Pass 2: collect + score KWIC candidates for top unigrams and phrases.

    Returns a dict term -> top MAX_KWIC scored candidates.
    """
    target_uni = {d["term"] for d in top_terms}
    target_phrase = {p for p, _ in phrases}
    kwic = defaultdict(list)
    for src in sources:
        up = src / "units.jsonl"
        if not up.exists():
            continue
        sid = src.name
        for rec in iter_units(up):
            text = rec.get("text", "")
            span = _anchor_char_span(rec)
            if span is None:
                continue
            low = text.lower()
            toks = set(tokenize(text))
            for term in target_uni & toks:
                if len(kwic[term]) < MAX_KWIC_CAND:
                    kwic[term].append({"source_id": sid, "char_span": span, "text": text.strip()})
            for phr in target_phrase:
                if phr in low and len(kwic[phr]) < MAX_KWIC_CAND:
                    kwic[phr].append({"source_id": sid, "char_span": span, "text": text.strip()})

    # Score collected candidates and keep the top MAX_KWIC (best displayed first).
    return {term: best_kwic(cands, term) for term, cands in kwic.items()}


def collocates(term, bi):
    hits = [(" ".join(g), c) for g, c in bi.items() if term in g]
    hits.sort(key=lambda kv: (-kv[1], kv[0]))
    return [h for h, _ in hits[:MAX_COLLOC]]


def write_term_cards(outdir, top_terms, bi, kwic):
    """Build term cards (freq/keyness/collocates/KWIC) and write term_cards.jsonl.

    Returns the cards list (reused by the other writers).
    """
    cards = [{**d, "collocates": collocates(d["term"], bi), "kwic": kwic.get(d["term"], [])} for d in top_terms]
    (outdir / "term_cards.jsonl").write_text(
        "\n".join(json.dumps(c, ensure_ascii=False) for c in cards), encoding="utf-8", newline="\n")
    return cards


def write_freq_full(outdir, cards, uni, n_sources, n_units, total_tokens, min_count, min_df):
    (outdir / "freq_full.json").write_text(json.dumps({
        "n_sources": n_sources, "n_units": n_units, "total_tokens": total_tokens, "vocab_size": len(uni),
        "min_count": min_count, "min_docfreq": min_df,
        "n_concepts": sum(1 for c in cards if c["category"] == "concept"),
        "n_entities": sum(1 for c in cards if c["category"] == "entity"),
    }, indent=2), encoding="utf-8", newline="\n")


def write_blind_spots(outdir, cards, uni, bi, total_tokens):
    """Probe expected-but-absent terms against live counts; write blind_spots.json."""
    card_terms = {c["term"] for c in cards}

    def probe(form):
        if " " in form:
            c = bi.get(tuple(form.split()), 0)
        else:
            c = uni.get(form, 0)
        if c == 0:
            return None
        key, _, zipf = keyness(form, c, total_tokens)
        return {"form": form, "count": c, "keyness": key, "zipf_general": zipf,
                "in_dictionary": form in card_terms}

    blind = []
    for display, forms, cause, why in BLIND_SPOT_PROBES:
        variants = [p for p in (probe(f) for f in forms) if p]
        blind.append({"display": display, "cause": cause, "why": why, "variants": variants,
                      "in_dictionary": any(v["in_dictionary"] for v in variants)})
    (outdir / "blind_spots.json").write_text(json.dumps(blind, indent=2, ensure_ascii=False),
                                             encoding="utf-8", newline="\n")


def write_markdown(outdir, cards, phrases, kwic, uni, n_sources, n_units, total_tokens, min_count, min_df):
    """Render the human-readable jargon_dictionary.md."""
    concepts = [c for c in cards if c["category"] == "concept"]
    entities = [c for c in cards if c["category"] == "entity"]

    def table(rows, n):
        out = ["| Term | Count | Panels | Keyness | Example |", "|---|---|---|---|---|"]
        for c in rows[:n]:
            ex = (c["kwic"][0]["text"][:88] + "…") if c["kwic"] else ""
            out.append(f"| {c['term']} | {c['count']} | {c['doc_freq']} | {c['keyness']} | {ex} |")
        return out

    md = ["# Tech Week — Jargon & Vernacular Dictionary (v1.2, deterministic)\n",
          f"_Corpus: {n_sources} panels · {n_units:,} units · {total_tokens:,} tokens · vocab {len(uni):,}. "
          f"Keyness = log10(corpus-freq ÷ general-English-freq); higher = more distinctive to this domain. "
          f"Terms gated at count ≥ {min_count} and presence in ≥ {min_df} panels. 100% deterministic — "
          f"no LLM._\n",
          "\n## Concepts & vernacular (by keyness)\n",
          "_The conceptual language of the field — the ideas a newcomer must learn to decode._\n",
          *table(concepts, 50),
          "\n## Named entities, products & acronyms (by keyness)\n",
          "_High capitalization in source ⇒ companies, products, models, people (the who/what landscape)._\n",
          *table(entities, 40),
          "\n## Multi-word jargon (collocations)\n",
          "_Distinctive phrases (filtered to those with ≥1 domain-distinctive token)._\n",
          "| Phrase | Count | Example |", "|---|---|---|"]
    for phr, c in phrases[:50]:
        ex = (kwic[phr][0]["text"][:88] + "…") if kwic.get(phr) else ""
        md.append(f"| {phr} | {c} | {ex} |")
    (outdir / "jargon_dictionary.md").write_text("\n".join(md) + "\n", encoding="utf-8", newline="\n")


def print_summary(cards, phrases, uni, n_sources, n_units, total_tokens, min_df):
    concepts = [c for c in cards if c["category"] == "concept"]
    entities = [c for c in cards if c["category"] == "entity"]
    print(f"sources={n_sources} units={n_units} tokens={total_tokens} vocab={len(uni)} "
          f"| concepts={len(concepts)} entities={len(entities)} phrases={len(phrases)} (min_df={min_df})")
    print("\nTOP 20 CONCEPTS (keyness):")
    for c in concepts[:20]:
        print(f"  key={c['keyness']:6}  n={c['count']:4}  panels={c['doc_freq']:3}  cap={c['cap_ratio']:.2f}  {c['term']}")
    print("\nTOP 15 NAMED ENTITIES / ACRONYMS:")
    for c in entities[:15]:
        print(f"  key={c['keyness']:6}  n={c['count']:4}  cap={c['cap_ratio']:.2f}  {c['term']}")
    print("\nTOP 20 PHRASES:")
    for phr, c in phrases[:20]:
        print(f"  {c:4}  {phr}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source")
    ap.add_argument("--run")
    ap.add_argument("--out", default="jargon/out")
    ap.add_argument("--topk", type=int, default=120)
    ap.add_argument("--min-count", type=int, default=4)
    ap.add_argument("--min-docfreq", type=int, default=2)
    args = ap.parse_args()

    if args.source:
        sources = [Path(args.source)]
    elif args.run:
        sources = sorted(p.parent for p in Path(args.run).glob("*/units.jsonl"))
    else:
        ap.error("pass --source or --run")

    # ---- Pass 1: counts + cross-panel doc-freq + capitalization ----
    uni, bi, tri, df_uni, cap, total_tokens, n_units, n_sources = count_pass(sources)
    if total_tokens == 0:
        print("no tokens — check paths", file=sys.stderr)
        sys.exit(1)

    # ---- Score unigrams + extract distinctive phrases ----
    min_df = 1 if n_sources == 1 else args.min_docfreq
    top_terms = score_terms(uni, df_uni, cap, total_tokens, args.min_count, min_df, args.topk)
    phrases = extract_phrases(uni, bi, tri, total_tokens, args.topk)

    # ---- Pass 2: KWIC for top unigrams + top phrases ----
    kwic = collect_kwic(sources, top_terms, phrases)

    # ---- Write outputs ----
    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    cards = write_term_cards(outdir, top_terms, bi, kwic)
    write_freq_full(outdir, cards, uni, n_sources, n_units, total_tokens, args.min_count, min_df)
    write_blind_spots(outdir, cards, uni, bi, total_tokens)
    write_markdown(outdir, cards, phrases, kwic, uni, n_sources, n_units, total_tokens, args.min_count, min_df)

    print_summary(cards, phrases, uni, n_sources, n_units, total_tokens, min_df)


if __name__ == "__main__":
    main()
