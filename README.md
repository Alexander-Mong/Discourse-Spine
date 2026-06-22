# Discourse Spine

Discourse Spine pulls structured, source-anchored evidence out of long-form panel
transcripts. It separates the work into two layers: deterministic Python code does anything
that can be done deterministically (parsing, anchoring, counting, routing), and a language
model is used only for the narrow judgment calls that genuinely need it. Every model
judgment is checked against a planted control before it is trusted.

The repository ships with three real Tech Week panel transcripts (about 70 to 90 minutes
each), so you can clone it and watch the pipeline produce real output in a few seconds. The
core needs no API key and no network.

## What this demonstrates

If you are evaluating the work, here is what it shows:

- A clear boundary between deterministic code and model judgment, enforced in the code and in
  the dependency list (the core imports nothing beyond the standard library).
- Real provenance. Every extracted item carries a character span and a timecode back to a
  hashed copy of the source, so any finding can be traced to the exact words spoken.
- Tested, reproducible behavior: same input, same IDs, same output, covered by 52 tests.
- A governed way to use a language model, where its output is treated as a claim to verify,
  measured against planted decoys, not as an answer to accept.

## Quickstart (no keys, no network)

```bash
git clone <this-repo>
cd discourse-spine        # your cloned directory
pip install -r requirements.txt
python run_demo.py
```

This runs the deterministic pipeline over the three bundled transcripts:

```
ingest      SRT       ->  character-anchored units + a SHA-256 lockfile of the source
annotate    units     ->  rule matches + domain-term occurrences
candidates  matches   ->  routed evidence candidates
enrich      run       ->  deterministic feature records (kept separate from the source text)
```

Output on the three bundled transcripts:

| units | rule matches | candidates | feature records |
|------:|-------------:|-----------:|----------------:|
| 2,649 | 391 | 388 | 3,395 |

Results are written to `runs/demo/<source_id>/`. Each unit carries two anchors (a character
span and an SRT timecode) back to a hashed source, so any downstream finding can be walked
back to the transcript.

### Run the tests

```bash
python -m pytest
# 52 passed
```

The core is plain Python standard library. The only third-party packages are `wordfreq` (for
the jargon builder) and `pytest` (for the tests).

## Jargon dictionary (also no model)

A deterministic dictionary of domain vocabulary. It scores words and phrases by how much more
often they appear here than in everyday English (using the offline `wordfreq` package),
filters by spread across panels, and keeps short usage examples. No model, no scoring of
"value", which makes it the lowest-risk artifact in the project.

```bash
python jargon/build_jargon.py --run runs/demo --out runs/demo/_jargon
```

On the three panels it surfaces the actual vocabulary in use: customer discovery, pain
points, ai agents, first customer.

## LLM precision lane (Stage 4, optional)

This is the one model-judgment lane included as runnable code. For each routed candidate it
asks one question: is this really advice? The question is wrapped in a planted control.
Hidden decoy candidates are mixed into the batch, and the run is only trusted if the model
correctly rejects them.

You need one of:

- the `claude` CLI on your PATH (uses your subscription, no API key), or
- an API key in `.env` (copy `.env.example`, then `pip install -r requirements-llm.txt`).

```bash
# subscription CLI, the routed candidates (the default the pipeline sends to the model):
python spine/stage4_eval.py --run-root runs/demo --lane claude --execute

# a direct API lane instead (needs ANTHROPIC_API_KEY in .env):
python spine/stage4_eval.py --run-root runs/demo --lane anthropic --execute

# judge every candidate, including the low-confidence ones the router filters out:
python spine/stage4_eval.py --run-root runs/demo --lane claude --buckets all --execute
```

You do not need to run it to see what it does. A real sample is committed under
[`precomputed/precision_lane/`](precomputed/precision_lane/): 414 verdicts on the bundled data
(385 candidates plus 29 hidden controls). On that run the model rejected 28 of 29 controls
(96.6%), so the control bar passed. It confirmed 19 of 29 routed candidates (66%) and 152 of
356 non-routed candidates (43%). The higher rate on routed candidates shows the deterministic
router orders candidates sensibly, and the 43% on the non-routed set shows the router trades
recall for cost. See that folder's README for how to read the output.

### Strong-model adjudication

A second governed step (`spine/stage4_adjudicate.py`) re-judges the routed candidates on a
stronger model (Sonnet), blind to the cheap lane's verdict, then reports where the two agree
and disagree. On the bundled data the two models agreed on 25 of 29 candidates (86%), leaving
4 flips for a human to look at. Sample output is in
[`precomputed/adjudication/`](precomputed/adjudication/).

## Repository layout

```
run_demo.py             one command that runs the deterministic pipeline over the 3 transcripts
spine/
  ids.py                deterministic, content-addressed identifiers
  ingest.py             SRT to anchored units + lockfile
  annotate.py           rule and term matching
  candidates.py         routes matches into evidence candidates
  enrich.py             deterministic features, kept separate from source text
  validate.py           schema and structural checks
  stage4_eval.py        the LLM precision lane (judgment plus a planted control)
  stage4_adjudicate.py  blind second-model check of the cheap lane
  lane_runner.py        cross-platform LLM call adapters (keys from environment)
jargon/build_jargon.py  the deterministic vocabulary dictionary
inputs/                 the rule and term registries plus prompt templates
features/  schema/      feature definitions and JSON schemas
controls/               planted-control protocols and pre-registration notes
data/examples/          the 3 bundled .srt transcripts
precomputed/            real sample output from the LLM lane (so no key is needed to see it)
docs/                   architecture, pipeline reference, methodology
tests/                  the test suite (52 tests: the deterministic core plus the LLM-lane glue)
```

## The three bundled transcripts

All three are public Tech Week YouTube panels. Each one stresses a different part of the
system:

| Transcript | Length | Stresses |
|---|---|---|
| Founder Sales in the AI Era: Winning Your First 10 Customers | ~73 min | dense, concrete advice |
| An A2A World: Who Builds It, Who Wins, and What's Still Missing | ~85 min | forecasts and disagreement |
| IEEE and the Future of AI, NYC Tech Week Panel | ~91 min | many speakers, technical vocabulary |

## Scope

This is a runnable slice of a larger project, not the whole thing.

- It bundles 3 transcripts. The full corpus is 161 public Tech Week panels harvested (about 62
  hours), of which 153 ingest cleanly into units. You rebuild the full corpus on your own
  machine from public sources (see `docs/`).
- Two further lanes exist in the larger project and are described in
  [`docs/METHODOLOGY.md`](docs/METHODOLOGY.md) but are not shipped as runnable code here: a
  recall lane (what did the rules miss?) and a founder-advice digest. Both need a
  corpus-scale harvest.
- On cost: running the LLM lane on a Claude Max subscription draws the flat plan and adds no
  metered, per-call API charges. It is not free. API-key lanes are billed by the vendor.

## How to trust the output

- Provenance, end to end. Every unit carries two anchors back to its source: a character
  span and an SRT timecode. The character span indexes into a normalized transcript whose
  SHA-256 is recorded in `source_lockfile.json`, so a finding walks all the way back through
  its candidate and annotation to the exact words spoken and the byte range they occupy.
  Nothing is paraphrased or re-generated along the chain, so you can check any finding's span
  against the transcript yourself and get the same bytes.
- Pre-registration: controls and decoys are fixed before a run (`controls/`).
- Determinism: the core is reproducible, pinned by the test suite with golden-master outputs
  and content-addressed IDs.

Start with [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the design rationale, or
[`docs/PIPELINE_REFERENCE.md`](docs/PIPELINE_REFERENCE.md) for the stage-by-stage reference.

## License

MIT. See [`LICENSE`](LICENSE).
